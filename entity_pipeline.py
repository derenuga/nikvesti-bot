#!/usr/bin/env python3
"""Водопровід сутнісного шару над «лисячою норою» (крок C, docs/ENTITY_LAYER_PLAN).

БЕЗ жодного виклику LLM — тільки робота з нОрою (Postgres бота) через psycopg2:
схема, вибірка статей, злиття готового JSON у entities/article_entities
(`write_results` — спільне ядро для всіх каналів витягу).

**Прод-канал витягу — Batch API** (`entity_backfill_api.py`, з бота
`/entity_backfill`) і щогодинний авто-інкремент. Команди `fetch`/`next` нижче
лишились від експерименту з Max-суб-агентами (шлях Б): він виміряний і
відхилений — гірша якість при більшому розході токенів, числа в §3.3.1 плану.
Не використовувати для нових прогонів; тримаємо як робочі утиліти вивантаження.

URL нори береться з env NORA_URL (щоб пароль не лежав у репозиторії):
    export NORA_URL="postgresql://postgres:...@reseau.proxy.rlwy.net:46884/railway"

Команди:
    python3 entity_pipeline.py schema
        застосувати DDL (entities + article_entities + курсор entity_last_id).

    python3 entity_pipeline.py fetch 100 batch.json
        [ТЕСТ] вивантажити N свіжих опублікованих статей 2024–2026 у JSON
        [{id, published, title_ua, title_ru, text_ua, text_ru}].
        БЕЗ курсора — разова тестова вибірка (крок 2 §3.3.1).

    python3 entity_pipeline.py next 10000 batch.json
        [ПРОДАКШН, фазовий прогін] взяти наступні N необроблених статей,
        йдучи по id ВНИЗ від курсора entity_last_id (id ≈ хронологічний, тож
        це newest→oldest: 2026→…→2009, ровно фазування §3.3). Курсор НЕ рухає
        (щоб обрив до write не пропустив пачку) — рухає його write. Пише
        {"cursor_from": …, "articles": [...]}. Коли статей нижче курсора нема —
        друкує "прогін завершено".

    python3 entity_pipeline.py write results.json [batch.json]
        злити результат витягу. Формат results.json:
        [{"article_id": 320651,
          "entities": [
            {"kind":"person","subtype":null,
             "name_ua":"Олександр Сєнкевич","name_ru":"Александр Сенкевич",
             "role":"міський голова","salience":"mentioned"}, ...]}, ...]
        Злиття: точний збіг нормалізованого імені в межах kind (однофамільців
        НЕ зливаємо). mentions/first_seen/last_seen/role_last перераховуються з
        даних (ідемпотентно — повторний write безпечний).
        Якщо передано batch.json (продакшн-цикл) — курсор entity_last_id
        опускається до мінімального id пачки (весь діапазон позначається
        обробленим, навіть статті без сутностей), і друкується покриття.

    python3 entity_pipeline.py stats
        зведення: скільки сутностей по kind, топ за згадками, к-сть зв'язків.

    python3 entity_pipeline.py sample 50 qa.txt
        вибірка N випадкових статей з їх сутностями + врізка тексту — для
        ручної перевірки якості (§3.6: точність ≥90%, вигадані ролі ≤2%).

    python3 entity_pipeline.py reset
        ОЧИСТИТИ entities + article_entities і скинути курсор entity_last_id=0.
        Для чистого перегону тесту (щоб v2 не злився поверх v1-даних). Схему
        (таблиці) не чіпає. Питає підтвердження.
"""

import sys
import os
import re
import json
import random

ALLOWED_KINDS = {"person", "org", "place", "document", "event"}
ALLOWED_SALIENCE = {"main", "mentioned"}

# Витяг тексту, який віддаємо суб-агенту, обмежуємо, щоб контекст пачки був
# керованим (~1.2к токенів/стаття за планом; 8000 симв. ≈ з запасом).
TEXT_CAP = 8000

DDL = r"""
CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    subtype TEXT,
    name_ua TEXT,
    name_ru TEXT,
    aliases TEXT[] DEFAULT '{}',
    role_last TEXT,
    first_seen BIGINT,
    last_seen BIGINT,
    mentions INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entities_kind        ON entities (kind);
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameua ON entities (kind, lower(name_ua));
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameru ON entities (kind, lower(name_ru));
CREATE TABLE IF NOT EXISTS article_entities (
    article_id BIGINT NOT NULL,
    entity_id BIGINT NOT NULL,
    role_at_time TEXT,
    salience TEXT,
    PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities (entity_id);
INSERT INTO sync_state (key, value) VALUES ('entity_last_id', '0')
ON CONFLICT (key) DO NOTHING;
"""

# Слід спроб витягу. Живе тут, а не в handlers/entity_layer, бо потрібна двом
# модулям одразу: інкремент нею самозаліковується (повторює впалу статтю до
# 3 разів), а бэкфіл — щоб не платити вдруге за статті, які вже пройшли витяг
# і законно не мають сутностей (done).
# Пам'ять РІШЕНЬ про злиття. Без неї злиття не тримається: зіставлення в
# write_results іде тільки за name_ua/name_ru, аліаси в ньому не беруть участі —
# тож наступна стаття зі словом «Сєнкевич» створює картку заново, і людина
# зливає той самий дубль щотижня. Тут лежить рівно те, що ЛЮДИНА вже вирішила:
# «це написання належить оцій картці». Знімається разом із відкатом злиття.
MERGE_RULES_DDL = """
CREATE TABLE IF NOT EXISTS entity_merge_rules (
    kind      TEXT NOT NULL,
    norm      TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    merge_id  BIGINT,
    created   BIGINT,
    PRIMARY KEY (kind, norm)
)
"""

ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS entity_attempts (
    article_id BIGINT PRIMARY KEY,
    attempts   INT NOT NULL DEFAULT 0,
    last_error TEXT,
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    updated    BIGINT
)
"""


def get_url():
    # NORA_URL — зовнішній запуск (Mac/dev, публічний URL Railway);
    # BOT_DATABASE_URL/DATABASE_URL — запуск зсередини Railway (бот).
    url = (os.environ.get("NORA_URL")
           or os.environ.get("BOT_DATABASE_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        # RuntimeError, не sys.exit: модуль імпортує і бот — SystemExit
        # проскочив би повз його except Exception.
        raise RuntimeError("Не задано NORA_URL / BOT_DATABASE_URL / DATABASE_URL.")
    return url


def connect():
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 не встановлено. pip install psycopg2-binary")
    return psycopg2.connect(get_url(), connect_timeout=10)


# Декоративні лапки прибираємо з ключа злиття, щоб КП «Парки» / КП "Парки" /
# КП «Парки» (різні стилі лапок) не плодили окремих сутностей. Зберігається
# при цьому оригінальне написання (лапки лишаються в name_ua/name_ru для показу).
_QUOTES = dict.fromkeys(map(ord, "«»“”„‟\"'‘’`"), None)


def norm(s):
    """Нормалізоване ім'я для точного злиття: trim + collapse spaces + lower +
    прибирання декоративних лапок."""
    if not s:
        return None
    s = re.sub(r"\s+", " ", s.strip()).lower().translate(_QUOTES)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# Курований словник канону ключових локацій (домен: Миколаївщина + топ-згадувані).
# Зводить відмінки/рос. написання до називного, щоб «Миколаєв»/«Миколаєві»/
# «Миколаївщина» не плодили окремих сутностей. Це НЕ морфологія — лише
# страхувальна сітка над інструкцією суб-агенту віддавати називний відмінок
# (промпт гасить решту; на всьому архіві можливі інші міста в непрямих формах).
# Ключ — нормалізоване (lower) написання варіанта; значення — канон (name_ua, name_ru).
CANON_PLACE = {
    "миколаєв": ("Миколаїв", "Николаев"),
    "миколаєві": ("Миколаїв", "Николаев"),
    "миколаєва": ("Миколаїв", "Николаев"),
    "николаев": ("Миколаїв", "Николаев"),
    "nikolaev": ("Миколаїв", "Николаев"),
    "миколаївщина": ("Миколаївська область", "Николаевская область"),
    "миколаївщині": ("Миколаївська область", "Николаевская область"),
    "миколаївській області": ("Миколаївська область", "Николаевская область"),
    "миколаївської області": ("Миколаївська область", "Николаевская область"),
    "николаевщина": ("Миколаївська область", "Николаевская область"),
    "одесі": ("Одеса", "Одесса"),
    "одеси": ("Одеса", "Одесса"),
    "одесу": ("Одеса", "Одесса"),
    "одеській області": ("Одеська область", "Одесская область"),
    "херсонщина": ("Херсонська область", "Херсонская область"),
    "херсонщині": ("Херсонська область", "Херсонская область"),
    "херсоні": ("Херсон", "Херсон"),
    "києві": ("Київ", "Киев"),
    "києва": ("Київ", "Киев"),
    "україні": ("Україна", "Украина"),
    "україни": ("Україна", "Украина"),
    "вишневому": ("Вишневе", "Вишневое"),
}


def canon_place(name_ua, name_ru):
    """Звести відомий варіант локації до називного канону; інакше — без змін."""
    for v in (norm(name_ua), norm(name_ru)):
        if v in CANON_PLACE:
            return CANON_PLACE[v]
    return name_ua, name_ru


# ---------- Канон ПОСИЛАНЬ НА ЗАКОН (kind='document') ----------
#
# Та сама хвороба, що була в ролях, тільки на картках. Одна стаття кодексу
# живе десятком написань, і кожне створює власну картку:
#
#     стаття 191 КК
#     Кримінальний кодекс України, стаття 191
#     частина п'ята статті 191 Кримінального кодексу України
#     кримінальне провадження за частиною 4 статті 191 КК України
#
# Замір 02.08 по норі: 495 таких написань на 268 статей, 96 груп дублів.
# Кожна картка при цьому «одноразова» — тобто виглядає сміттям, хоч насправді
# це найцінніший клас документів: з нього виходить покажчик «за якими
# статтями ми писали справи».
#
# Лікується НЕ разовим злиттям, а обчисленням ключа: канонічна назва
# виводиться з тексту детерміновано, тому та сама стаття в наступній статті
# сайту потрапляє в ту саму картку — і в щогодинному інкременті, і в батчах
# архіву, і в /entity_resync. Промпт витягу просить те саме словами, але
# покладатись на нього не можна: це страхувальна сітка, як CANON_PLACE.
#
# ЧАСТИНА статті в назву НЕ входить свідомо. Картка — це стаття кодексу,
# частина/пункт — деталь конкретної справи: вона лишається і в сирому
# написанні (воно йде в аліаси), і в тексті самої статті сайту. Інакше
# «ч. 1 ст. 286» і «ч. 2 ст. 286» назавжди лишились би двома різними
# сутностями, і покажчик по статті не зібрався б ніколи.
CODE_PATTERNS = [
    # Порядок важливий: процесуальні кодекси перед матеріальними, інакше
    # «кримінальний процесуальний» ловився б як «кримінальний».
    ("КПК", (r"кримінальн\w*\s+процесуальн", r"\bкпк\b")),
    ("КК", (r"кримінальн\w*\s+кодекс", r"\bкк\b", r"\bкку\b")),
    ("КУпАП", (r"купап", r"кодекс\w*\s+україни\s+про\s+адміністративн",
               r"кодекс\w*\s+про\s+адміністративн")),
    ("ЦПК", (r"цивільн\w*\s+процесуальн", r"\bцпк\b")),
    ("ЦК", (r"цивільн\w*\s+кодекс", r"\bцк\b")),
    ("ГПК", (r"господарськ\w*\s+процесуальн", r"\bгпк\b")),
    ("ГК", (r"господарськ\w*\s+кодекс", r"\bгк\b")),
    ("ПК", (r"податков\w*\s+кодекс", r"\bпк\b")),
    ("КАС", (r"кодекс\w*\s+адміністративного\s+судочинства", r"\bкас\b")),
    ("КЗпП", (r"кодекс\w*\s+законів\s+про\s+працю", r"\bкзпп\b")),
    ("ЗК", (r"земельн\w*\s+кодекс", r"\bзк\b")),
    ("СК", (r"сімейн\w*\s+кодекс", r"\bск\b")),
    ("БК", (r"бюджетн\w*\s+кодекс", r"\bбк\b")),
    ("МК", (r"митн\w*\s+кодекс", r"\bмк\b")),
    ("Конституції", (r"конституці",)),
]

# «стат…» із цифрою одразу після — так ловляться і «стаття», і «статті», і
# «статею» з одруківкою, і «ст.». Вимога цифри рятує від «статусу»: без неї
# «Про статус ветеранів війни» читалось би як посилання на статтю.
_ART_RE = re.compile(
    r"\b(?:стат[а-яіїєґ']{0,4}|ст)\.?\s*№?\s*(\d+(?:[-–—]\d+)?)")

# Частини й пункти прибираємо ПЕРЕД перевіркою на складене посилання: у
# «стаття 115 пункт 6 частина 2» цифри 6 і 2 не є номерами статей, а от у
# «статті 191 та 209» друга цифра — саме стаття, хоч слова «стаття» перед нею
# немає. Без цієї різниці «191 та 209» звелось би до однієї 191-ї, тобто
# злиття збрехало б.
_PART_RE = re.compile(r"(?:частин\w*|пункт\w*|\bч\.|\bп\.|\bабз\w*)\s*№?\s*\d+")
_NUM_RE = re.compile(r"\d+(?:[-–—]\d+)?")
_BILL_RE = re.compile(r"законопро(?:є|е)кт", re.I)
# Суфікс номера — ЧАСТИНА номера, а не сміття: 9256-д це доопрацьований
# законопроєкт, 11256-2 — альтернативний, 4220-IX — номер скликання. Зрізати
# його означало б зліпити різні законопроєкти в один.
_BILL_NUM_RE = re.compile(
    r"№\s*(\d{3,6}(?:-[\dа-яіїєґa-z]{1,3})?)"
    r"|законопро(?:є|е)кт\w*\s+(\d{3,6}(?:-[\dа-яіїєґa-z]{1,3})?)", re.I)


def canon_document(name):
    """Канонічна назва посилання на закон або None, якщо це не воно.

    Зводимо лише те, що впізнали НАПЕВНО: рівно один номер статті плюс
    відомий кодекс. Складене посилання («статті 191 та 209», «статтями 109,
    436-2, 114-2») лишаємо як є — це не одна стаття, і зліпити його з
    окремою статтею означало б збрехати."""
    if not name:
        return None
    low = re.sub(r"\s+", " ", name.lower())
    nums = {m.group(1).replace("–", "-").replace("—", "-")
            for m in _ART_RE.finditer(low)}
    # Усі інші числа рядка (крім частин, пунктів і років) — теж кандидати в
    # номери статей: у «статті 191 та 209» друге число стоїть голим.
    rest = _PART_RE.sub(" ", low)
    others = {m.group(0).replace("–", "-").replace("—", "-")
              for m in _NUM_RE.finditer(rest)
              if not (m.group(0).isdigit() and 1900 <= int(m.group(0)) <= 2100)}
    if len(nums) == 1 and others <= nums:
        for code, pats in CODE_PATTERNS:
            if any(re.search(p, low) for p in pats):
                num = nums.pop()
                return (f"стаття {num} {code} України" if code != "Конституції"
                        else f"стаття {num} Конституції України")
    if _BILL_RE.search(low):
        # Шукаємо в ОРИГІНАЛІ, а не в lower: суфікс «4220-IX» це номер
        # скликання, і в нижньому регістрі він читається як помилка.
        m = _BILL_NUM_RE.search(name) or _BILL_NUM_RE.search(low)
        if m:
            return f"законопроєкт №{m.group(1) or m.group(2)}"
    return None


# ---------- Ключ ОРГАНІЗАЦІЇ (правова форма — не інша установа) ----------
#
# «Миколаївобленерго», «АТ «Миколаївобленерго»», «КП «Миколаївобленерго»»,
# «Акціонерне товариство «Миколаївобленерго»» — одна компанія під чотирма
# картками, бо витяг то пише правову форму, то ні. Замір по норі 02.08: 264
# групи, 590 карток, 7412 згадок — і це найбільші підприємства міста, тобто
# головні герої місцевих новин.
#
# На відміну від статей кодексу тут НЕ переписується назва: яка форма
# «правильна» — питання смаку, а не факту. Обчислюється лише КЛЮЧ зіставлення,
# по якому нове написання знаходить наявну картку.
#
# «КОП» у цьому списку немає свідомо, хоч виглядає як абревіатура форми: у
# Миколаєві так називають комунальне виробниче підприємство шкільного
# харчування, тобто це ІМʼЯ. Прибирання його як форми лишало порожній ключ.
ORG_FORMS = [
    # Порядок має значення: довші форми перед коротшими, інакше «міське
    # комунальне підприємство» зріжеться як «комунальне підприємство» й
    # лишить хвіст «міське» — саме на цьому «Міське КП «Миколаївводоканал»»
    # не злилось із рештою водоканалу (знайшов Олег 02.08).
    r"міськ\w*\s+комунальн\w*\s+підприємств\w*",
    r"обласн\w*\s+комунальн\w*\s+підприємств\w*",
    r"комунальн\w*\s+виробнич\w*\s+підприємств\w*",
    r"комунальн\w*\s+некомерційн\w*\s+підприємств\w*",
    r"комунальн\w*\s+підприємств\w*", r"комунальн\w*\s+установ\w*",
    r"комунальн\w*\s+заклад\w*", r"державн\w*\s+підприємств\w*",
    r"приватн\w*\s+підприємств\w*",
    r"приватн\w*\s+акціонерн\w*\s+товариств\w*",
    r"публічн\w*\s+акціонерн\w*\s+товариств\w*",
    r"акціонерн\w*\s+товариств\w*",
    r"товариств\w*\s+з\s+обмежен\w*\s+відповідальн\w*",
    r"\bкп\b", r"\bокп\b", r"\bкнп\b", r"\bку\b", r"\bкз\b", r"\bмкп\b",
    r"\bтов\b", r"\bпат\b", r"\bпрат\b", r"\bдп\b", r"\bат\b",
    # Родові слова, що грають ту саму роль, що й правова форма: «Автострада»
    # / «Група компаній «Автострада»» / «Компанія «Автострада»» — одне й те
    # саме (11 груп на замірі). «Асоціація», «спілка», «федерація» і «фонд»
    # сюди НЕ входять: вони частина самої назви («Асоціація міст України»
    # без них перестає бути собою).
    r"група компаній", r"\bкомпані\w*", r"\bфірм\w*", r"\bкорпораці\w*",
    r"\bконцерн\w*", r"\bхолдинг\w*", r"\bагрофірм\w*",
]
_ORG_FORM_RE = re.compile("|".join(ORG_FORMS))

# Ключ коротший за це не беремо: на двох-трьох символах абревіатури різних
# установ починають збігатися.
ORG_KEY_MIN = 3


def org_key(name):
    """Ключ зіставлення організації без правової форми, або None.

    Рахується для БУДЬ-ЯКОЇ назви, а не лише для тієї, що має форму: картка
    «Миколаївводоканал» мусить знаходитись написанням «КП
    «Миколаївводоканал»», а спільне в них лише цей ключ.

    None — коли ловити нема чого: назва складається з самої форми або
    лишилось менше ORG_KEY_MIN символів (на двох-трьох символах абревіатури
    різних установ починають збігатися)."""
    if not name:
        return None
    s = norm(name)
    if not s:
        return None
    s = _ORG_FORM_RE.sub(" ", s)
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    return s if len(s) >= ORG_KEY_MIN else None


# ---------- Ключ МІСЦЯ (скорочення типу) ----------
#
# Тут, на відміну від організацій, ТИП — це розрізнювач, а не шум: «вулиця
# Лесі Українки», «площа Лесі Українки» і «бульвар Лесі Українки» існують у
# Миколаєві одночасно і є різними об'єктами, а «Мирне» це село при «вулиці
# Мирній». Тому зрізати тип не можна ніколи — можна лише РОЗКРИТИ його
# скорочення, і цим вичерпується механічно безпечне.
#
# Замір по норі 02.08: саме на скороченнях сидить 290 груп і 580 карток
# («вул. Космонавтів» проти «вулиця Космонавтів»). Ширший ключ, що ловить ще
# й відмінки, додає всього 3 групи — і при цьому мусив би зліпити «Архітектора
# Старова» з «Архітектора Старого». Не варте того.
PLACE_ABBR = [
    (r"^вул\.?\s+", "вулиця "), (r"^просп\.?\s+", "проспект "),
    (r"^пров\.?\s+", "провулок "), (r"^пл\.?\s+", "площа "),
    (r"^бул\.?\s+", "бульвар "), (r"^наб\.?\s+", "набережна "),
    (r"^м\.\s+", "місто "), (r"^с\.\s+", "село "),
    (r"^смт\.?\s+", "селище "),
]


def place_key(name):
    """Ключ зіставлення місця: те саме ім'я з розкритим скороченням типу."""
    s = norm(name)
    if not s:
        return None
    for pat, full in PLACE_ABBR:
        s = re.sub(pat, full, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def loose_key(kind, name):
    """Другий ключ зіставлення для тих видів, де він визначений механічно."""
    if kind == "org":
        return org_key(name)
    if kind == "place":
        return place_key(name)
    return None


def get_state(cur, key, default=None):
    cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_state(cur, key, value):
    cur.execute(
        "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )


# ---------- schema ----------

def cmd_schema():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("SELECT to_regclass('entities'), to_regclass('article_entities')")
    print("schema:", cur.fetchone())
    cur.execute("SELECT value FROM sync_state WHERE key='entity_last_id'")
    print("cursor:", cur.fetchone())
    cur.execute("SELECT count(*) FROM articles")
    print("articles:", cur.fetchone()[0])
    cur.close()
    conn.close()
    print("OK")


# ---------- fetch ----------

def cmd_fetch(n, outpath):
    conn = connect()
    cur = conn.cursor()
    # Свіжі опубліковані 2024–2026 (де щільність сутностей вища). Нора вже
    # містить лише status=1 та published у минулому — додатковий фільтр не треба.
    cur.execute(
        """
        SELECT id, published, title_ua, title_ru, text_ua, text_ru
        FROM articles
        WHERE published >= extract(epoch FROM date '2024-01-01')
          AND published <  extract(epoch FROM date '2027-01-01')
        ORDER BY published DESC
        LIMIT %s
        """,
        (n,),
    )
    out = []
    for aid, pub, tua, tru, xua, xru in cur.fetchall():
        out.append({
            "id": aid,
            "published": int(pub) if pub is not None else None,
            "title_ua": tua,
            "title_ru": tru,
            "text_ua": (xua or "")[:TEXT_CAP] or None,
            "text_ru": (xru or "")[:TEXT_CAP] or None,
        })
    cur.close()
    conn.close()
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"fetched {len(out)} статей → {outpath}")
    if out:
        print(f"діапазон дат (unix): {out[-1]['published']} … {out[0]['published']}")


# ---------- next (продакшн-цикл по курсору, newest→oldest) ----------

def cmd_next(n, outpath):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT max(id) FROM articles")
    maxid = cur.fetchone()[0] or 0
    stored = int(get_state(cur, "entity_last_id", "0") or "0")
    # 0 = ще не починали → стеля вище за max(id); інакше йдемо нижче курсора.
    ceiling = (maxid + 1) if stored == 0 else stored
    cur.execute(
        """
        SELECT id, published, title_ua, title_ru, text_ua, text_ru
        FROM articles
        WHERE id < %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (ceiling, n),
    )
    arts = []
    for aid, pub, tua, tru, xua, xru in cur.fetchall():
        arts.append({
            "id": aid,
            "published": int(pub) if pub is not None else None,
            "title_ua": tua,
            "title_ru": tru,
            "text_ua": (xua or "")[:TEXT_CAP] or None,
            "text_ru": (xru or "")[:TEXT_CAP] or None,
        })
    cur.close()
    conn.close()
    if not arts:
        print("прогін завершено: статей нижче курсора немає "
              f"(курсор entity_last_id = {stored})")
        return
    payload = {"cursor_from": ceiling, "articles": arts}
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    low, high = arts[-1]["id"], arts[0]["id"]
    print(f"взято {len(arts)} статей id {high}…{low} → {outpath}")
    print(f"курсор поки НЕ рухаю (рухне write). Після write буде: {low}")


# ---------- write ----------

def write_results(data, batch_ids=None):
    """Ядро злиття — використовують і CLI (cmd_write), і бот (handlers/entity_layer).
    Існуючі сутності підвантажуються в пам'ять один раз, зіставлення (точний збіг
    нормалізованого імені в межах kind, для place через CANON_PLACE) робиться в
    Python, вставки йдуть execute_values пачкою.

    data — список {"article_id": ..., "entities": [...]}.
    batch_ids — id статей пачки next (курсорний прогін): при покритті ≥98%
    курсор entity_last_id опускається до min(batch_ids). None = діапазонний
    бэкфіл, курсор не чіпаємо. Повертає dict статистики."""
    from psycopg2.extras import execute_values

    conn = connect()
    cur = conn.cursor()

    # 1. Підвантажити наявні сутності в пам'ять (індекс за (kind, norm-ім'я)).
    cur.execute("SELECT id, kind, name_ua, name_ru, subtype, aliases, mentions FROM entities")
    recs = {}   # id -> запис (мутабельний)
    index = {}  # (kind, norm) -> id (за найбільшою кількістю згадок)
    # Другий індекс — за обчисленим ключем (організації без правової форми,
    # місця з розкритим скороченням типу). Без нього «КП «Миколаївводоканал»»
    # щоразу заводив нову картку поруч із «Миколаївводоканалом», і так у норі
    # виросло 264 групи дублів; на «вул.» проти «вулиця» — ще 290.
    loose = {}
    for eid, kind, nua, nru, sub, aliases, mentions in cur.fetchall():
        rec = {"id": eid, "kind": kind, "name_ua": nua, "name_ru": nru,
               "subtype": sub, "aliases": set(aliases or []),
               "mentions": mentions or 0, "dirty": False, "new": False}
        recs[eid] = rec
        for nm in (nua, nru):
            k = (kind, norm(nm))
            if k[1] is None:
                continue
            best = index.get(k)
            if best is None or recs[best]["mentions"] < rec["mentions"]:
                index[k] = eid
        if kind in ("org", "place"):
            for nm in (nua, nru):
                lk = loose_key(kind, nm)
                if not lk:
                    continue
                best = loose.get(lk)
                if best is None or recs[best]["mentions"] < rec["mentions"]:
                    loose[lk] = eid

    # Рішення людини про злиття — у той самий індекс. setdefault, а не
    # перезапис: якщо картка з таким іменем ЖИВА, вона головніша за правило
    # (правило лікує повторну появу, а не відбирає чужі згадки).
    try:
        cur.execute(MERGE_RULES_DDL)
        cur.execute("SELECT kind, norm, entity_id FROM entity_merge_rules")
        for kind, nrm, eid in cur.fetchall():
            if eid in recs and nrm:
                index.setdefault((kind, nrm), eid)
    except Exception as e:
        print(f"write_results: правила злиття не прочитані — {e}")

    new_recs = []       # записи на INSERT
    tmp_seq = [-1]      # тимчасові від'ємні id для нових (мапляться після вставки)

    def find_or_stage(kind, subtype, name_ua, name_ru):
        if kind == "place":
            name_ua, name_ru = canon_place(name_ua, name_ru)
        # Посилання на закон зводимо до обчисленої назви ЩЕ ДО зіставлення —
        # тоді «частина 5 статті 191 КК України» і «стаття 191 КК» потрапляють
        # в одну картку без жодного злиття, а сире написання лишається
        # аліасом (пошук по ньому має працювати).
        extra_alias = None
        if kind == "document":
            canon = canon_document(name_ua) or canon_document(name_ru)
            if canon and norm(canon) != norm(name_ua):
                extra_alias = name_ua
                name_ua = canon
        nu, nr = norm(name_ua), norm(name_ru)
        if not nu and not nr:
            return None
        hit = None
        for k in ((kind, nu), (kind, nr)):
            if k[1] and k in index:
                hit = index[k]
                break
        if hit is None and kind in ("org", "place"):
            # Правова форма — не інша установа («АТ «Миколаївобленерго»» це
            # той самий «Миколаївобленерго»), скорочення типу — не інша
            # вулиця. Сире написання піде в аліаси.
            for nm in (name_ua, name_ru):
                lk = loose_key(kind, nm)
                if lk and lk in loose:
                    hit = loose[lk]
                    break
        if hit is not None:
            rec = recs[hit]
            if not rec["name_ua"] and name_ua:
                rec["name_ua"] = name_ua; rec["dirty"] = True
            if not rec["name_ru"] and name_ru:
                rec["name_ru"] = name_ru; rec["dirty"] = True
            if not rec["subtype"] and subtype:
                rec["subtype"] = subtype; rec["dirty"] = True
            canon_norms = {norm(rec["name_ua"]), norm(rec["name_ru"])}
            for nm in (name_ua, name_ru, extra_alias):
                if nm and norm(nm) not in canon_norms and nm not in rec["aliases"]:
                    rec["aliases"].add(nm); rec["dirty"] = True
            for nm in (rec["name_ua"], rec["name_ru"]):
                kk = (kind, norm(nm))
                if kk[1] and kk not in index:
                    index[kk] = rec["id"]
            return rec["id"]
        tid = tmp_seq[0]
        tmp_seq[0] -= 1
        rec = {"id": tid, "kind": kind, "name_ua": name_ua, "name_ru": name_ru,
               "subtype": subtype,
               "aliases": {extra_alias} if extra_alias else set(),
               "mentions": 0, "dirty": True, "new": True}
        recs[tid] = rec
        new_recs.append(rec)
        for nm in (name_ua, name_ru):
            kk = (kind, norm(nm))
            if kk[1]:
                index.setdefault(kk, tid)
            if kind in ("org", "place"):
                lk = loose_key(kind, nm)
                if lk:
                    loose.setdefault(lk, tid)
        return tid

    # 2. Пройти результат, зібрати зв'язки (з тимчасовими id для нових сутностей).
    links = []   # [article_id, eid(may be temp), role, salience]
    n_articles = n_skipped = 0
    got_ids = set()
    for art in data:
        aid = art.get("article_id") or art.get("id")
        if aid is None:
            continue
        n_articles += 1
        got_ids.add(aid)
        for e in art.get("entities", []):
            kind = (e.get("kind") or "").strip().lower()
            sal = (e.get("salience") or "").strip().lower()
            if kind not in ALLOWED_KINDS or sal not in ALLOWED_SALIENCE:
                n_skipped += 1
                continue
            eid = find_or_stage(kind, e.get("subtype"),
                                e.get("name_ua"), e.get("name_ru"))
            if eid is None:
                n_skipped += 1
                continue
            role = e.get("role") or e.get("role_at_time") or None
            links.append([aid, eid, role, sal])

    # 3. Вставити нові сутності пачкою, змапити тимчасові id -> реальні.
    idmap = {}
    if new_recs:
        rows = [(r["kind"], r["subtype"], r["name_ua"], r["name_ru"],
                 sorted(r["aliases"])) for r in new_recs]
        inserted = execute_values(
            cur,
            "INSERT INTO entities (kind, subtype, name_ua, name_ru, aliases) "
            "VALUES %s RETURNING id",
            rows, fetch=True,
        )
        for r, row in zip(new_recs, inserted):
            idmap[r["id"]] = row[0]

    # 4. Оновити наявні сутності, що набули імені/алiаса/subtype цієї пачки.
    for rec in recs.values():
        if rec["new"] or not rec["dirty"]:
            continue
        cur.execute(
            "UPDATE entities SET name_ua = %s, name_ru = %s, subtype = %s, aliases = %s "
            "WHERE id = %s",
            (rec["name_ua"], rec["name_ru"], rec["subtype"],
             sorted(rec["aliases"]), rec["id"]),
        )

    # 5. Вставити зв'язки пачкою (тимчасові id -> реальні).
    # Дедуп (article_id, entity_id) last-wins ПЕРЕД вставкою: дві сутності
    # однієї статті можуть звестись до одного entity_id (напр. Миколаїв+Миколаєв
    # після canon_place). execute_values з ON CONFLICT не терпить дубль ключа в
    # одній пачці (CardinalityViolation) — тож згортаємо тут, як робив ON CONFLICT
    # у побудовному варіанті.
    touched = set()
    dedup = {}
    for aid, eid, role, sal in links:
        rid = idmap.get(eid, eid)
        dedup[(aid, rid)] = (role, sal)   # останній перемагає
        touched.add(rid)
    resolved = [(aid, rid, role, sal) for (aid, rid), (role, sal) in dedup.items()]
    if resolved:
        execute_values(
            cur,
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES %s "
            "ON CONFLICT (article_id, entity_id) DO UPDATE SET "
            "role_at_time = EXCLUDED.role_at_time, salience = EXCLUDED.salience",
            resolved,
        )
    n_links = len(resolved)

    # 6. Перерахунок агрегатів із даних — ідемпотентно, не залежить від порядку.
    cur.execute(
        """
        UPDATE entities e SET
            mentions   = s.cnt,
            first_seen = s.fmin,
            last_seen  = s.fmax
        FROM (
            SELECT ae.entity_id, count(*) AS cnt,
                   min(a.published) AS fmin, max(a.published) AS fmax
            FROM article_entities ae JOIN articles a ON a.id = ae.article_id
            GROUP BY ae.entity_id
        ) s
        WHERE e.id = s.entity_id
        """
    )
    # role_last = роль у найсвіжішій статті сутності (де роль текстуально є).
    cur.execute(
        """
        UPDATE entities e SET role_last = sub.role
        FROM (
            SELECT DISTINCT ON (ae.entity_id) ae.entity_id,
                   ae.role_at_time AS role
            FROM article_entities ae JOIN articles a ON a.id = ae.article_id
            WHERE ae.role_at_time IS NOT NULL AND ae.role_at_time <> ''
            ORDER BY ae.entity_id, a.published DESC
        ) sub
        WHERE e.id = sub.entity_id
        """
    )
    # Курсорний прогін (пачка next): опустити курсор до мінімального id пачки —
    # але ЛИШЕ при покритті ≥98%. Предохранитель: інакше діапазон із
    # неізвлеченими статтями позначився б обробленим і вони б ніколи не
    # потрапили в нору. Сутності/зв'язки комітяться в будь-якому разі
    # (write ідемпотентний — повторний прогін їх перезапише).
    stats = {"articles": n_articles, "links": n_links,
             "entities_touched": len(touched), "new_entities": len(new_recs),
             "skipped": n_skipped, "cursor": None, "coverage": None}
    if batch_ids:
        covered = len(got_ids & set(batch_ids))
        stats["coverage"] = (covered, len(batch_ids))
        if covered / len(batch_ids) >= 0.98:
            new_cur = min(batch_ids)
            set_state(cur, "entity_last_id", new_cur)
            stats["cursor"] = new_cur
    conn.commit()
    cur.close()
    conn.close()
    return stats


def cmd_write(path, batch_path=None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    batch_ids = None
    if batch_path:
        with open(batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        batch_arts = batch["articles"] if isinstance(batch, dict) else batch
        batch_ids = [a.get("id") for a in batch_arts if a.get("id") is not None]
    s = write_results(data, batch_ids)
    if s["coverage"]:
        covered, total = s["coverage"]
        if s["cursor"] is None:
            print(f"⚠️ покриття лише {covered}/{total} ({covered/total:.0%}) — "
                  f"КУРСОР НЕ РУХАЮ. Доведи витяг пачки до кінця й повтори write, "
                  f"або зменш розмір фази. Наявні сутності записані (ідемпотентно).")
        else:
            print(f"курсор entity_last_id → {s['cursor']} "
                  f"(оброблено діапазон, покриття {covered}/{total})")
            if covered < total:
                print(f"  {total - covered} статей без сутностей — теж позначені обробленими")
    print(f"статей оброблено: {s['articles']}, зв'язків: {s['links']}, "
          f"сутностей торкнулись: {s['entities_touched']}, пропущено (невалідні): {s['skipped']}")


# ---------- stats ----------

def cmd_stats():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities")
    print("усього сутностей:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM article_entities")
    print("усього зв'язків:", cur.fetchone()[0])
    print("\nпо kind:")
    cur.execute("SELECT kind, count(*) FROM entities GROUP BY kind ORDER BY count(*) DESC")
    for kind, c in cur.fetchall():
        print(f"  {kind:9} {c}")
    print("\nтоп-15 за згадками:")
    cur.execute(
        "SELECT kind, coalesce(name_ua, name_ru), role_last, mentions "
        "FROM entities ORDER BY mentions DESC LIMIT 15"
    )
    for kind, name, role, m in cur.fetchall():
        print(f"  [{kind}] {name} — {role or '—'} ({m})")
    cur.close()
    conn.close()


# ---------- sample (ручна перевірка якості §3.6) ----------

def cmd_sample(n, outpath):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT article_id FROM article_entities")
    ids = [r[0] for r in cur.fetchall()]
    random.shuffle(ids)
    ids = ids[:n]
    lines = []
    for aid in ids:
        cur.execute(
            "SELECT title_ua, title_ru, coalesce(text_ua, text_ru) FROM articles WHERE id = %s",
            (aid,),
        )
        r = cur.fetchone()
        title = (r[0] or r[1] or "—") if r else "—"
        # ПОВНИЙ текст (не врізка): сутності бувають у концівці статті («Нагадаємо…»),
        # коротка врізка давала ложні «галюцинації» при QA. Кап на патологічні лонгріди.
        body = (r[2] or "")[:12000] if r else ""
        if r and r[2] and len(r[2]) > 12000:
            body += "\n…[текст обрізано на 12000 симв. — довша стаття]"
        lines.append(f"===== article {aid}: {title}")
        lines.append(body)
        cur.execute(
            "SELECT e.kind, coalesce(e.name_ua, e.name_ru), ae.role_at_time, ae.salience "
            "FROM article_entities ae JOIN entities e ON e.id = ae.entity_id "
            "WHERE ae.article_id = %s ORDER BY ae.salience",
            (aid,),
        )
        for kind, name, role, sal in cur.fetchall():
            lines.append(f"    [{kind}/{sal}] {name} — {role or '—'}")
        lines.append("")
    cur.close()
    conn.close()
    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"вибірка {len(ids)} статей → {outpath} (звірити очима: точність ≥90%, вигадані ролі ≤2%)")


def cmd_reset():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities")
    ne = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM article_entities")
    nl = cur.fetchone()[0]
    ans = input(f"Очистити entities ({ne}) та article_entities ({nl}) "
                f"і скинути курсор? Введи 'yes': ")
    if ans.strip().lower() != "yes":
        print("скасовано")
        return
    cur.execute("TRUNCATE entities RESTART IDENTITY")
    cur.execute("TRUNCATE article_entities")
    set_state(cur, "entity_last_id", "0")
    print("очищено: entities, article_entities; курсор entity_last_id=0")
    cur.close()
    conn.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "schema":
        cmd_schema()
    elif cmd == "reset":
        cmd_reset()
    elif cmd == "fetch":
        cmd_fetch(int(sys.argv[2]), sys.argv[3])
    elif cmd == "next":
        cmd_next(int(sys.argv[2]), sys.argv[3])
    elif cmd == "write":
        cmd_write(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "sample":
        cmd_sample(int(sys.argv[2]), sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
