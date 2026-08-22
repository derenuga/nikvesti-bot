#!/usr/bin/env python3
"""Витяг ЗОБОВ'ЯЗАНЬ із нори через Anthropic API (банк тем, docs/PROMISES_BANK.md §7).

Той самий дизайн, що дав якість у сутнісному шарі (`entity_backfill_api.py`):
ОДНА стаття на запит (фізична ізоляція, контекст-блид неможливий), строгий
структурований вивід (json_schema), правила таксономії — з одного файлу
`promise_extract_prompt.md`, системний блок кешується.

**Окремий виклик, а не «заодно з сутностями».** Розмивання задачі вимірювано
псує витяг (`ENTITY_LAYER_PLAN.md` §3.3.1), тому промпт тут свій і модель
читає статтю вдруге. Це свідома плата.

**Пре-фільтр за маркерами.** Прямий прогін 28к статей 2024–2026 коштує ~$170,
тож перед прогоном відсіюємо статті, у яких немає жодного мовленнєвого маркера
майбутньої дії. Список маркерів навмисно ШИРОКИЙ (recall важливіший за
precision: пропущена обіцянка втрачена назавжди, зайва стаття коштує пів
цента), і його ціна вимірюється, а не вгадується — див. `MARKERS` нижче і
крок 7 §7 плану.

Ціна Haiku 4.5 батчем: $0.50/1M вх, $2.50/1M вих (−50% від звичайних).
"""

import json
import os
import re
from datetime import datetime

import entity_pipeline as ep          # connect(), TEXT_CAP
import promise_pipeline as pp         # схема банку тем
from entity_backfill_api import chunk_articles   # ті самі ліміти Batch API

MODEL = "claude-haiku-4-5"
# Кап виводу з тим самим запасом, що у витягу сутностей: платимо за фактичний
# вихід, а обрив на капі дає непарсибельний JSON — двічі наступали на це в
# сутнісному шарі (див. коментар до MAX_TOKENS в entity_backfill_api).
MAX_TOKENS = 8000

PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "promise_extract_prompt.md")

# Оцінка токенів — НАВМИСНО ЗАВИЩЕНА, поки немає заміру.
#
# Вхід = текст статті (~3.0к, як у витягу сутностей) + системний промпт, а він
# тут утричі більший за сутнісний (19 КБ проти 5.5 КБ: сім розборів дали сім
# правил, і кожне коштує рядків). Кешування системного блоку зробить реальну
# цифру помітно нижчою, але ЦЕ ЧИСЛО Олег бачить перед тим, як натиснути
# «запустити», тож помилятись воно має в бік дорожче, а не дешевше.
# Уточнити після першого місяця: фактична вартість друкується у звіті
# /promise_scan.
EST_IN_PER_ART = 6000
EST_OUT_PER_ART = 400
PRICE_IN_BATCH = 0.50 / 1_000_000
PRICE_OUT_BATCH = 2.50 / 1_000_000
PRICE_IN = 1.00 / 1_000_000     # звичайний API (/promise_test, /promise_retest)
PRICE_OUT = 5.00 / 1_000_000

# ---------- Пре-фільтр: маркери мовленнєвого акту ----------
#
# Це ПРЕФІКСИ лексем для to_tsquery('simple', 'обіця:* | плану:* | …') — нора
# індексується без стемера, морфологія закривається префіксом (як у
# archive_search._stem).
#
# Список широкий свідомо. Головне правило витягу — «лови мовленнєвий акт, а не
# слово "обіцяв"» (§2.5), і фільтр не має права бути вужчим за нього: у
# еталонній статті 321833 слова «обіц» немає жодного разу, а зобов'язань там
# чотири — ловиться вона по «розроб», «плану», «розпоряд».
#
# Російські форми потрібні не менше за українські: до ~2023 сайт писав
# російською, тобто ВЕСЬ старий архів — а саме там лежать обіцянки, строк яких
# уже минув.
MARKERS = [
    # українська: зобов'язання прямо
    "обіця", "пообіця", "обіцян", "гаранту", "гарантува", "зобов", "запевн",
    # українська: планова й безособова форма — саме так влада вуалює
    "плану", "заплан", "передбач", "розпоряд", "доруч", "намір",
    # українська: дія, про яку зазвичай і йдеться
    "розроб", "заверш", "закінч", "перенес", "перенос", "введ", "відкри",
    "запуст", "розпочн", "розпочат", "збуду", "побуду", "відбуду", "будува",
    "збудова", "відремонт", "ремонтув", "капремонт", "реконструкц",
    "відновл", "створ", "виділ", "профінанс", "здійсн", "запровад",
    "модерніз", "експлуатац",
    # російська (архів до ~2023)
    "обеща", "пообеща", "гаранти", "обязу", "обязал", "планиру", "предусм",
    "разработ", "восстанов", "постро", "отремонт", "откро", "откры",
    "созда", "выдел", "намер", "приступ", "эксплуатац", "заверши", "перенос",
]


def marker_tsquery():
    """OR-запит префіксних лексем для articles.fts."""
    return " | ".join(f"{m}:*" for m in sorted(set(MARKERS)))


# ---------- Схема виводу ----------

COMMITMENT_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subject": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"}},
        # Опис саме тут, а не в загальному тексті промпту: дисципліна полів
        # звідти програє (три прогони приймання показали це на criterion і
        # condition). Живий провал 22.08 (id 294413): у статті поруч стояли
        # умова тендеру й слова посадовця про хід робіт, і витяг поставив
        # обіцяльником ПІДРЯДНИКА в обидва записи — тобто взяв його з
        # головного документа статті, а не з цитати. Наслідок не косметичний:
        # обіцяльник збігся, спрацювало правило недроблення, і найтвердіше
        # зобов'язання статті зникло.
        "promiser": {
            "type": ["string", "null"],
            "description": "Хто зобов'язаний САМЕ ЦИМ записом, за його "
                           "власною цитатою, а не за головним документом "
                           "статті. За умовою договору чи тендеру — "
                           "підрядник; за словами посадовця — він сам або "
                           "орган, який він представляє. Одна стаття легко "
                           "дає різних обіцяльників, і зводити їх до одного "
                           "не можна: саме на цьому полі тримається правило "
                           "«розділяй, коли різний обіцяльник».",
        },
        "promiser_role": {"type": ["string", "null"]},
        "owner": {"type": ["string", "null"]},
        "reported_by": {"type": ["string", "null"]},
        # УВАГА: `enum` стоїть лише на полях, які НЕ можуть бути null.
        #
        # Пара «"type": ["string","null"] + enum з null у списку» виглядає
        # природно і є валідним JSON Schema, але структурований вивід Anthropic
        # її відкидає: 400 invalid_request_error, «Enum value 'media' does not
        # match declared type». Прод-схема витягу сутностей (entity_backfill_api)
        # тримається того самого правила — там `subtype` теж має словник, і теж
        # без enum.
        #
        # Словник для таких полів живе у промпті й у `description` нижче, а в
        # базу його стереже pp._enum(): значення поза таксономією стає NULL, а
        # не потрапляє в реєстр мовчки.
        "audience": {"type": ["string", "null"],
                     "description": "media | community | group, або null"},
        # Опис у полі, а не лише в промпті: замір 22.08.2026 дав вісім
        # перевернутих записів з одинадцяти (73%), і механізм помилки один —
        # модель читає НЕГАТИВНЕ ЗВУЧАННЯ результату замість форми
        # зобовʼязання.
        "polarity": {
            "type": "string", "enum": list(pp.POLARITY),
            "description": "do — пообіцяли ВЧИНИТИ дію (хоч би дія полягала "
                           "в тому, щоб щось припинити, відключити, закрити "
                           "чи заборонити); not_do — пообіцяли УТРИМАТИСЬ "
                           "(«не дамо», «не будемо», «утримаємось»). Вирішує "
                           "форма зобовʼязання, а не те, чи негативно звучить "
                           "результат: «Відключити фонтан» і «Припинити "
                           "набір учнів» — це do.",
        },
        "modality": {"type": "string", "enum": list(pp.MODALITY)},
        "source_type": {"type": "string", "enum": list(pp.SOURCE_TYPE)},
        "deadline": {"type": ["string", "null"],
                     "description": "YYYY-MM-DD — остання дата періоду, або null"},
        "deadline_precision": {
            "type": ["string", "null"],
            "description": "day | month | quarter | year | vague, або null"},
        # Дисципліна ЦИХ трьох полів живе тут, а не тільки в промпті: модель
        # приймає рішення про поле, читаючи його опис, і тут воно не конкурує
        # з рештою тексту. Три прогони приймання показали, що загальне правило
        # у промпті тут програє — модель заповнювала criterion переказом самої
        # обіцянки, і мітка «популізм» (§2.1) не спрацьовувала взагалі.
        "criterion": {
            "type": ["string", "null"],
            "description": "Перевірювана ознака: що саме треба побачити, щоб "
                           "сказати «виконано». Пиши, лише якщо можеш назвати, "
                           "хто і як перевірить це за один день. Переказ самої "
                           "обіцянки («відновлено краще, ніж було», «стало "
                           "комфортніше») — НЕ критерій, тоді null. Порожнє "
                           "поле тут нормальне й очікуване.",
        },
        "verification_method": {
            "type": ["string", "null"],
            "description": "field_check | document_request | official_statement "
                           "| data, або null"},
        "condition": {
            "type": ["string", "null"],
            "description": "Умова, від якої залежить виконання, дослівно. Якщо "
                           "обіцянка звучить як «якщо…, то ми…» — усе, що після "
                           "«якщо», і є умовою; не пропускай її.",
        },
        "condition_self_judged": {
            "type": "boolean",
            "description": "true, коли настання умови оцінює САМ обіцяльник "
                           "(«якщо є нормальний заклад, хороший садочок»), а не "
                           "перевіряє хтось ззовні.",
        },
        "trigger_event": {"type": ["string", "null"]},
        "actor_hidden": {"type": "boolean"},
        "framed_as_promise": {"type": "boolean"},
        "based_on_document": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        # Тип мовленнєвого акту — ВЛАСТИВІСТЬ тексту, як modality, а не оцінка
        # важливості. Від нього залежить, чи потрапить запис у робочу чергу
        # редакції (commitment/rhetoric — так, решта — у тінь до розбору),
        # тому словник продубльовано тут: модель читає description поля
        # уважніше за загальний текст промпту (той самий урок, що criterion).
        "kind": {
            "type": "string", "enum": list(pp.KIND),
            "description": "commitment — зобов'язання влади чи підзвітного "
                           "місту актора про РЕЗУЛЬТАТ (збудувати, "
                           "відремонтувати, не допустити; сюди ж «має "
                           "розробити» робочої групи з документом-підставою). "
                           "rhetoric — публічна заява-обіцянка без критерію "
                           "(«повернемо краще, ніж було»). process — "
                           "процедурний крок: винести на розгляд, подати "
                           "пропозиції, розглянути на комісії, провести "
                           "засідання. routine — планова операційна "
                           "діяльність: прибирання, опалювальний сезон, "
                           "чергові виплати, «інспектори перевірятимуть». "
                           "ВИРІШУЄ НАСЛІДОК, А НЕ ДІЄСЛОВО: якщо після "
                           "виконання щось у місті СТАНЕ ІНАКШЕ для людей — "
                           "це commitment, хоч би як процедурно це звучало. "
                           "«Реорганізувати гімназію» (школа закриється), "
                           "«припинити роботу відділень лікарні», «продати "
                           "будівлю в парку», «розірвати договір на ₴3,9 млн», "
                           "«підвищити плату за навчання», «розрахуватися з "
                           "працівниками до 22 червня», «розпочати укладання "
                           "асфальту на 6-й Слобідській» — усе це commitment. "
                           "process і routine лишаються там, де результату "
                           "ще НЕМАЄ: готують, розглядають, звітують, "
                           "консультуються, спостерігають, або роблять те, що "
                           "робилося б і без заяви. "
                           "offtopic — приватні плани приватних акторів "
                           "(ферма, підприємець), обіцянки з судових справ "
                           "(хабар), інші міста й області, "
                           "загальнонаціональне за ПРЕДМЕТОМ. "
                           "ОБОВ'ЯЗОК, НАКЛАДЕНИЙ ЗЗОВНІ, — це process, а не "
                           "commitment: окрема ухвала суду, припис інспекції, "
                           "вимога прокуратури до органу влади. Запис "
                           "лишається (документ проігнорувати не можна), але "
                           "це не обіцянка влади, а звернення до неї, і в "
                           "черзі редакції йому не місце. "
                           "УВАГА: offtopic вирішує ПРЕДМЕТ, а не ранг "
                           "обіцяльника — держава, Кабмін, міністерство чи "
                           "область, які обіцяють щось МИКОЛАЄВУ або "
                           "Миколаївщині («виділити місту дотацію 1,18 млрд», "
                           "«передати профтехзаклади в комунальну "
                           "власність», «реконструювати очисні споруди "
                           "Миколаївщини»), це commitment. Публічне "
                           "зобов'язання приватника МІСТУ (демонтувати "
                           "огорожу) — теж commitment.",
        },
        "micro": {
            "type": "boolean",
            "description": "true, коли предмет дрібніший за об'єкт: один "
                           "під'їзд, одна лавка, одне дерево, одна виплата "
                           "одній людині. Цілий будинок, вулиця, сквер, "
                           "школа — false. Це про МАСШТАБ предмета, не про "
                           "суму й не про важливість.",
        },
        "scope": {
            "type": "string", "enum": list(pp.SCOPE),
            "description": "ЗОНА ПЕРЕВІРКИ — окрема вісь від kind, і питання "
                           "тут інше: чи візьметься за цю обіцянку саме наша "
                           "редакція. Три значення, і перевіряти їх треба "
                           "СТРОГО В ЦЬОМУ ПОРЯДКУ. "
                           "city — предмет самé МІСТО Миколаїв або його "
                           "мешканці, ХТО Б НЕ ОБІЦЯВ: міськрада, виконком, "
                           "департамент, райадміністрація, КП міста, а так "
                           "само держава, Кабмін, міністерство чи донор, коли "
                           "гроші й роботи йдуть Миколаєву. "
                           "oblast — АБО обіцяльник ОБЛАСНОГО рівня "
                           "(Миколаївська ОВА та її департаменти й "
                           "управління, обласна рада, обласні КП і установи, "
                           "обласна лікарня), і тоді КОМУ обіцяно — байдуже: "
                           "«Кім пообіцяв Вознесенську 40 шкільних автобусів» "
                           "це oblast; АБО предметом є МИКОЛАЇВЩИНА в цілому "
                           "(землі полігонів області, ліси області, обласні "
                           "дороги, громади області разом), хто б не обіцяв — "
                           "хоч Кабмін, хоч суд. "
                           "local — і тільки тоді, коли не підійшло жодне з "
                           "двох вище: предмет — ІНША ГРОМАДА області, а "
                           "обіцяльник не обласного рівня. Вознесенська, "
                           "Баштанська, Південноукраїнська, Первомайська, "
                           "Новоодеська міськради, сільські й селищні ради, "
                           "їхні КП і заклади про свій предмет. "
                           "НАЙДОРОЖЧА ПОМИЛКА ТУТ — прочитати «область» як "
                           "«не місто» і поставити local обіцянці ОВА. "
                           "Рівень обіцяльника вирішує НА КОРИСТЬ показу, а "
                           "не проти: сумніваєшся між oblast і local — став "
                           "oblast. Так само сумніваєшся, чи предмет "
                           "миколаївський (вулиця без міста, КП без міста, "
                           "«Миколаївводоканал», «Миколаївелектротранс») — "
                           "став city.",
        },
        "quote": {"type": "string"},
    },
    "required": ["title", "subject", "objects", "promiser", "promiser_role",
                 "owner", "reported_by", "audience", "polarity", "modality",
                 "source_type", "deadline", "deadline_precision", "criterion",
                 "verification_method", "condition", "condition_self_judged",
                 "trigger_event", "actor_hidden", "framed_as_promise",
                 "based_on_document", "amount", "kind", "micro", "scope",
                 "quote"],
    "additionalProperties": False,
}

ARTICLE_OUT = {
    "type": "object",
    "properties": {
        "article_id": {"type": "integer"},
        "commitments": {"type": "array", "items": COMMITMENT_ITEM},
    },
    "required": ["article_id", "commitments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = None


def load_system_prompt():
    """Правила з promise_extract_prompt.md (єдине джерело таксономії), очищені
    від markdown-цитатних '> '."""
    with open(PROMPT_FILE, encoding="utf-8") as f:
        raw = f.read()
    lines = [re.sub(r"^> ?", "", ln) for ln in raw.splitlines() if ln.startswith(">")]
    body = "\n".join(lines).strip()
    body += ("\n\nРЕЖИМ ЦЬОГО ЗАПИТУ: вхід — РІВНО ОДНА стаття. Поверни "
             "JSON-об'єкт {\"article_id\": <id>, \"commitments\": [...]} лише "
             "для неї. Порожній список — нормальний результат.")
    return body


def get_system_prompt():
    global SYSTEM_PROMPT
    if SYSTEM_PROMPT is None:
        SYSTEM_PROMPT = load_system_prompt()
    return SYSTEM_PROMPT


# ---------- Вибірка статей ----------

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця",
            "субота", "неділя"]


def article_payload(row):
    """Рядок нори → вхід моделі.

    `published` і `weekday` передаються ОБОВ'ЯЗКОВО: без них відносний
    горизонт («за вихідні», «до кінця тижня») розв'язати неможливо, і
    найшвидша з усіх обіцянок — три дні строку — провалилась би в «без
    дати» (§2.3).

    Рядок без колонок тексту (META_COLS) дає ту саму форму без `text_*` —
    цього досить для зшивання ланцюга й судді, і не тягне в пам'ять мегабайти
    текстів, які вже нікуди не поїдуть.
    """
    aid, published, tua, tru = row[0], row[1], row[2], row[3]
    xua = row[4] if len(row) > 4 else None
    xru = row[5] if len(row) > 5 else None
    text_ua = (xua or "")[:ep.TEXT_CAP] or None
    text_ru = (xru or "")[:ep.TEXT_CAP] or None
    if text_ua and text_ru:
        text_ru = None      # одна мова достатня, як у витягу сутностей
    dt = datetime.fromtimestamp(int(published)) if published else None
    return {
        "id": aid,
        "published": dt.strftime("%Y-%m-%d") if dt else None,
        "weekday": WEEKDAYS[dt.weekday()] if dt else None,
        "title_ua": tua,
        "title_ru": tru,
        "text_ua": text_ua,
        "text_ru": text_ru,
    }


# Регіон матеріалу (коди сайту: 1 Миколаїв, 2 Україна, 3 Світ, 4 Херсон,
# 5 Одеса — ті самі, що використовує fb_missing).
#
# Банк тем веде підзвітність МІСЦЕВОЇ влади, тому бере лише region=1. Перший
# же прогін місяця без цього фільтра показав, у що це виливається: серед
# обіцяльників червня Зеленський (21), Укрзалізниця (11), Уряд України (10),
# серед об'єктів — залізничний вокзал Одеси. Загальнонаціональні обіцянки
# редакція не перевіряє, а в черзі вони витісняють миколаївські.
REGION_MYKOLAIV = 1

# Стаття «ще чекає витягу». Не просто «не done»: стаття, чий витяг УПАВ,
# лишається в черзі й повертається наступним скану — саме цього не робила
# курсорна схема сутностей, і дірка від збою не затягувалась ніколи. Але
# вічно крутити одну биту ноду теж не можна, звідси стеля спроб.
PENDING_COND = ("NOT EXISTS (SELECT 1 FROM promise_attempts t "
                " WHERE t.article_id = a.id AND (t.done OR t.attempts >= %s))")

SELECT_COLS = "a.id, a.published, a.title_ua, a.title_ru, a.text_ua, a.text_ru"
# Те саме без текстів: для лічильників, звітів і зшивання ланцюга сам текст не
# потрібен, а 28к статей по 8 КБ — це чверть гігабайта в пам'яті процесу бота.
META_COLS = "a.id, a.published, a.title_ua, a.title_ru"


def month_bounds(month):
    """'YYYY-MM' → ('YYYY-MM-01', 'YYYY-MM-01' наступного місяця)."""
    y, m = int(month[:4]), int(month[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


def fetch_range(from_date, to_date, marked_only=True, only_missing=True,
                limit=None, region=REGION_MYKOLAIV):
    """Статті діапазону до витягу зобов'язань.

    marked_only — пре-фільтр за маркерами (див. MARKERS).
    only_missing — пропускати ті, що вже пройшли витяг: повторний прогін не
    платить удруге. Свідомий перечит — це /promise_retest, не скан.

    Повертає (articles, stats), де stats — {total, marked, skipped}: саме з
    цих чисел рахується і оцінка вартості, і ціна пре-фільтра.
    """
    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(pp.DDL)
            window = ("a.published >= extract(epoch FROM %s::date) AND "
                      "a.published <  extract(epoch FROM %s::date)")
            params = [from_date, to_date]
            if region is not None:
                window += " AND a.region = %s"
                params.append(region)
            cur.execute(f"SELECT count(*) FROM articles a WHERE {window}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM articles a WHERE {window} "
                "AND a.fts @@ to_tsquery('simple', %s)",
                params + [marker_tsquery()])
            marked = cur.fetchone()[0]

            where = [window]
            sel_params = list(params)
            if marked_only:
                where.append("a.fts @@ to_tsquery('simple', %s)")
                sel_params.append(marker_tsquery())
            if only_missing:
                where.append(PENDING_COND)
                sel_params.append(pp.MAX_ATTEMPTS)
            sql = (f"SELECT {SELECT_COLS} FROM articles a "
                   f"WHERE {' AND '.join(where)} ORDER BY a.published")
            if limit:
                sql += " LIMIT %s"
                sel_params.append(int(limit))
            cur.execute(sql, sel_params)
            arts = [article_payload(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return arts, {"total": total, "marked": marked,
                  "skipped": (marked if marked_only else total) - len(arts)}


def count_range(from_date, to_date, marked_only=True, only_missing=True,
                region=REGION_MYKOLAIV):
    """Скільки статей у діапазоні / з маркерами / чекають витягу — САМІ ЧИСЛА.

    Окремо від fetch_range саме тому, що оцінка на роках («скільки коштуватиме
    2024–2026») інакше вивантажувала б у пам'ять тексти 28 тисяч статей, щоб
    порахувати їх довжину списку.
    """
    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(pp.DDL)
            window = ("a.published >= extract(epoch FROM %s::date) AND "
                      "a.published <  extract(epoch FROM %s::date)")
            params = [from_date, to_date]
            cur.execute(f"SELECT count(*) FROM articles a WHERE {window}", params)
            month_total = cur.fetchone()[0]
            if region is not None:
                window += " AND a.region = %s"
                params.append(region)
            cur.execute(f"SELECT count(*) FROM articles a WHERE {window}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM articles a WHERE {window} "
                "AND a.fts @@ to_tsquery('simple', %s)", params + [marker_tsquery()])
            marked = cur.fetchone()[0]
            where = [window]
            sel = list(params)
            if marked_only:
                where.append("a.fts @@ to_tsquery('simple', %s)")
                sel.append(marker_tsquery())
            if only_missing:
                where.append(PENDING_COND)
                sel.append(pp.MAX_ATTEMPTS)
            cur.execute(f"SELECT count(*) FROM articles a WHERE {' AND '.join(where)}", sel)
            pending = cur.fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "month_total": month_total, "marked": marked,
            "pending": pending, "region": region,
            "skipped": (marked if marked_only else total) - pending}


def fetch_ids(ids, with_text=True):
    """Конкретні статті за id — для /promise_test і /promise_retest."""
    cols = SELECT_COLS if with_text else META_COLS
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM articles a "
                        "WHERE a.id = ANY(%s) ORDER BY a.published",
                        ([int(i) for i in ids],))
            return [article_payload(r) for r in cur.fetchall()]
    finally:
        conn.close()


def marked_ids(cur, ids):
    """Які з цих статей проходять пре-фільтр — ТИМ САМИМ запитом, що й скан.

    Не підрядком по тексту: фільтр працює через tsvector нори, і друга,
    «приблизна» реалізація рано чи пізно розійшлася б із першою — а від цього
    числа залежить рішення, вмикати фільтр на роках чи ні (§7 крок 7).
    """
    ids = [int(i) for i in ids if i]
    if not ids:
        return set()
    cur.execute("SELECT id FROM articles WHERE id = ANY(%s) "
                "AND fts @@ to_tsquery('simple', %s)", (ids, marker_tsquery()))
    return {r[0] for r in cur.fetchall()}


# ---------- Запити до моделі ----------

def request_params():
    """Спільне тіло запиту — однакове для батча і для звичайного виклику,
    щоб /promise_test і масовий скан не розійшлись у поведінці.

    **`temperature = 0` — не косметика, а умова того, щоб приймання взагалі
    щось міряло.** До 22.08.2026 температура не задавалась ніде, тобто діяв
    дефолт 1.0, і витяг був випадковою величиною. Заміряно на еталонах: два
    прогони /promise_eval НА ОДНОМУ Й ТОМУ САМОМУ КОДІ дали різні набори
    червоних — {320092, 321833} проти {321833, 322324}, — а к-сть записів
    гуляла на ±1 у половині статей (317853: 2 і 3, 312757: 2 і 3, 322324:
    3 і 4). Тобто «40/42» нічого не означало: правило, полагоджене вчора,
    сьогодні падало саме собою, і навпаки.

    Наслідки ширші за приймання. Ідемпотентність банку тримається на ключі
    «стаття + цитата»: та сама стаття, перечитана вдруге (/promise_retest,
    /promise_resplit, повторний скан місяця), при температурі 1.0 давала
    ІНШІ цитати — отже інші ключі, отже дублі замість оновлення.

    Haiku 4.5 приймає sampling-параметри (на Opus 5 і Sonnet 5 їх уже
    прибрано — там 400), тож ставимо явно. Нуль не гарантує побітової
    відтворюваності — на боці провайдера лишається батчинг і залізо, — але
    прибирає головне джерело розкиду.
    """
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "system": [{"type": "text", "text": get_system_prompt(),
                    "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": ARTICLE_OUT}},
    }


def _make_request(client, art):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=str(art["id"]),
        params=MessageCreateParamsNonStreaming(
            messages=[{"role": "user",
                       "content": json.dumps(art, ensure_ascii=False)}],
            **request_params(),
        ),
    )


def extract_one(client, art):
    """Витяг однієї статті звичайним API. Повертає (commitments, usage)."""
    resp = client.messages.create(
        messages=[{"role": "user", "content": json.dumps(art, ensure_ascii=False)}],
        **request_params(),
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"вихід обрізано на max_tokens={MAX_TOKENS} — JSON недописаний")
    text = next((bl.text for bl in resp.content if bl.type == "text"), None)
    obj = json.loads(text)
    return obj.get("commitments", []), resp.usage


def estimate(n, batch=True):
    """(вартість, вх. токени, вих. токени) для n статей."""
    tin, tout = n * EST_IN_PER_ART, n * EST_OUT_PER_ART
    pin, pout = (PRICE_IN_BATCH, PRICE_OUT_BATCH) if batch else (PRICE_IN, PRICE_OUT)
    return tin * pin + tout * pout, tin, tout


__all__ = ["MODEL", "MARKERS", "marker_tsquery", "fetch_range", "count_range",
           "fetch_ids", "marked_ids", "extract_one", "estimate", "chunk_articles",
           "get_system_prompt", "month_bounds", "ARTICLE_OUT", "_make_request"]
