#!/usr/bin/env python3
"""Водопровід банку тем над «лисячою норою» (docs/PROMISES_BANK.md §3–§5).

БЕЗ жодного виклику LLM — тільки схема й робота з Postgres бота, як
`entity_pipeline.py` для сутнісного шару. Усе, що потребує моделі (сам витяг і
суддя ланцюга), живе в `promise_extract_api.py` і `handlers/promises.py`.

**Три яруси (§2.6).** Одна обіцянка картини не дає в принципі:

    topic          «Гімназія (ліцей) №2»              ← одиниця банку тем
     └ commitment  зобов'язання конкретного актора     ← хто саме що винен
        └ revision переформулювання в часі             ← як мінялась дата й форма

**Що тут детерміноване, а що ні.** Модель віддає ВЛАСТИВОСТІ тексту (є дата,
є критерій, яка умова, який тип джерела). Клас перевірки `verifiability` і
мітка «популізм» рахуються з них ТУТ, кодом (§2.1): питати в моделі «це
популізм?» означало б просити її гадати про наміри. Через це в картці завжди
видно не вирок, а підставу — «немає дати · немає критерію · коментар у
соцмережі».

**Ідемпотентність.** Ревізія ключується статтею, дослівною цитатою і тим, чим
різняться зобов'язання, витягнуті з ОДНОГО речення (`_dedup_key` — там же
пояснено, чому самої цитати замало: у 294413 одна фраза несе два різні
горизонти). Повторний прогін місяця нічого не подвоїть, навіть якщо слід
спроби (`promise_attempts`) загубився. Перевірка йде ДО створення обіцянки —
інакше повтор лишав би порожню картку-сироту.

**Відкат.** `/promise_forget` не видаляє мовчки: знімок обіцянки з усіма
ревізіями лягає в `promise_purges`, звідки `/promise_restore` повертає її з
тим самим id. Той самий принцип, що `entity_merges` у сутнісному шарі.
"""

import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import entity_pipeline as ep   # connect(), norm(), loose_key() — спільні

# ---------- Таксономія (зафіксована ПОВНІСТЮ до першого прогону) ----------
#
# Урок ENTITY_LAYER_PLAN §3.1, за який уже платили: додати поле пізніше =
# перечитати корпус за гроші. Тому всі осі перелічені тут явно, навіть ті, що
# в першій ітерації майже не заповнюються.

# Наскільки твердо. Шкала називна (§2.6): «гарантував» (Кім) твердіше за
# «пообіцяли» (Кабмін) і набагато твердіше за «планують».
MODALITY = ("guaranteed", "promised", "planned", "considered", "hedged")
# Хто дзвонить у нагадуваннях (§5): «можуть перенести» — не зобов'язання, йому
# нема чого прострочувати. `considered` («розглядають можливість») стоїть на
# тій самій полиці за тією ж підставою — воно теж нікого ні до чого не
# зобов'язує. Обидва лежать у банку тем і рахуються, але не дзвонять.
RING_MODALITY = ("guaranteed", "promised", "planned")

# Звідки зобов'язання — вага спадає зліва направо (§2 висновок 3).
SOURCE_TYPE = ("tender", "government_decision", "official_statement",
               "social_comment", "journalist")
# Як перевіряти (§2.3). Дешевизна перевірки ПІДІЙМАЄ тему, а не відсіює.
VERIFICATION_METHOD = ("field_check", "document_request",
                       "official_statement", "data")
AUDIENCE = ("media", "community", "group")
POLARITY = ("do", "not_do")
PRECISION = ("day", "month", "quarter", "year", "vague")
# void — підстава зникла (договір розірвано), superseded — замінене новішим
# зобов'язанням (§2.6). Без них «зірвано» брехало б на підрядника, який нічого
# не зривав.
STATUS = ("expected", "done", "failed", "abandoned", "void", "superseded",
          "unknown")
VERIFIABILITY = ("measurable", "undated", "event_triggered", "unfalsifiable")

# Грейс за точністю дати (§5): «до кінця 2025» не означає «зранку 1 січня
# хтось звітує». Рік перевіряємо в середині січня, місяць — за тиждень після
# його кінця. Без цього банк тем стане будильником, який вимкнуть.
GRACE_DAYS = {"day": 3, "month": 7, "quarter": 14, "year": 14, "vague": 30}
DEFAULT_GRACE_DAYS = 14

# Скільки разів пробуємо статтю, перш ніж визнати її безнадійною. Урок
# сутнісного шару, за який уже заплачено (інцидент 11.07–01.08.2026, 403
# втрачені статті): стаття, чий витяг упав, МУСИТЬ лишатись у черзі — інакше
# дірка не затягнеться ніколи. Але й вічно крутити одну биту ноду не можна,
# звідси лічильник спроб.
MAX_ATTEMPTS = 3

# Скільки мовчання робить `undated`-обіцянку темою «давно не питали» (§5).
SILENCE_DAYS = 120
# Наскільки близький строк уже вважається «скоро».
SOON_DAYS = 45

DDL = r"""
CREATE TABLE IF NOT EXISTS topics (
    id           BIGSERIAL PRIMARY KEY,
    title        TEXT,
    entity_ids   BIGINT[] DEFAULT '{}',
    subject_keys TEXT[]   DEFAULT '{}',
    status       TEXT DEFAULT 'open',
    opened       BIGINT,
    last_event   BIGINT
);
CREATE INDEX IF NOT EXISTS idx_topics_entities ON topics USING gin (entity_ids);

CREATE TABLE IF NOT EXISTS commitments (
    id                    BIGSERIAL PRIMARY KEY,
    topic_id              BIGINT REFERENCES topics (id) ON DELETE SET NULL,
    title                 TEXT,
    subject               TEXT,
    subject_entity_id     BIGINT,
    subject_key           TEXT,
    owner_entity_id       BIGINT,
    owner_text            TEXT,
    audience              TEXT,
    status                TEXT DEFAULT 'expected',
    verifiability         TEXT,
    polarity              TEXT DEFAULT 'do',
    trigger_event         TEXT,
    deadline              BIGINT,
    deadline_precision    TEXT,
    criterion             TEXT,
    verification_method   TEXT,
    condition             TEXT,
    condition_self_judged BOOLEAN DEFAULT FALSE,
    actor_hidden          BOOLEAN DEFAULT FALSE,
    framed_as_promise     BOOLEAN DEFAULT FALSE,
    based_on_document     TEXT,
    amount                NUMERIC,
    modality              TEXT,
    source_type           TEXT,
    revisions             INT DEFAULT 0,
    first_seen            BIGINT,
    last_seen             BIGINT,
    checked_at            BIGINT,
    checked_by            TEXT,
    created               BIGINT
);
CREATE INDEX IF NOT EXISTS idx_commitments_subject ON commitments (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_commitments_subject_key ON commitments (subject_key);
CREATE INDEX IF NOT EXISTS idx_commitments_topic ON commitments (topic_id);
CREATE INDEX IF NOT EXISTS idx_commitments_deadline ON commitments (deadline);

-- Одна обіцянка може стосуватись кількох об'єктів одразу: «не дамо
-- приватизувати» сказано про ТРИ дитсадки (§2.4). Зв'язок багато-до-багатьох,
-- інакше довелось би або множити обіцянку, або губити два об'єкти з трьох.
CREATE TABLE IF NOT EXISTS commitment_objects (
    commitment_id BIGINT NOT NULL REFERENCES commitments (id) ON DELETE CASCADE,
    entity_id     BIGINT NOT NULL,
    PRIMARY KEY (commitment_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_commitment_objects_entity ON commitment_objects (entity_id);

CREATE TABLE IF NOT EXISTS commitment_revisions (
    id                    BIGSERIAL PRIMARY KEY,
    commitment_id         BIGINT NOT NULL REFERENCES commitments (id) ON DELETE CASCADE,
    article_id            BIGINT,
    stated_deadline       BIGINT,
    deadline_precision    TEXT,
    modality              TEXT,
    source_type           TEXT,
    promiser_entity_id    BIGINT,
    promiser_text         TEXT,
    promiser_role         TEXT,
    reported_by_entity_id BIGINT,
    reported_by_text      TEXT,
    condition             TEXT,
    trigger_event         TEXT,
    quote                 TEXT NOT NULL,
    link_confidence       TEXT,
    dedup                 TEXT UNIQUE,
    created               BIGINT
);
-- Чи знайшлась цитата в тексті статті ДОСЛІВНО. Порожнє = не звіряли (стара
-- ревізія або відкат зі знімка). §3 казав «немає дослівної цитати — немає
-- запису», але перевірялось лише «поле непорожнє»; тепер видно й фантазію.
ALTER TABLE commitment_revisions ADD COLUMN IF NOT EXISTS quote_verified BOOLEAN;
-- Що людина побачила, коли пішла перевіряти. Текстом, бо «не виконано, бо
-- підрядник зник» і «не виконано, перенесли на осінь» — різні історії, а
-- статус в обох однаковий.
ALTER TABLE commitments ADD COLUMN IF NOT EXISTS check_note TEXT;
-- og:image статті. Кеш, а не завантаження на кожен показ: сайт віддає вже
-- ресайзнутий .webp 600×315, але це все одно мережевий похід — тридцять
-- карток черги = тридцять запитів. Тягнемо ОДИН раз на статтю й назавжди.
ALTER TABLE articles ADD COLUMN IF NOT EXISTS og_image TEXT;
CREATE INDEX IF NOT EXISTS idx_revisions_commitment ON commitment_revisions (commitment_id);
CREATE INDEX IF NOT EXISTS idx_revisions_article ON commitment_revisions (article_id);

-- Слід витягу по кожній статті: скільки разів пробували, чому впало, і чи
-- проходила стаття пре-фільтр за маркерами (`marked`). Остання колонка —
-- єдиний спосіб потім заміряти ЦІНУ фільтра (§7 крок 7): скільки обіцянок
-- лежало в статтях, які фільтр не пропустив би.
CREATE TABLE IF NOT EXISTS promise_attempts (
    article_id BIGINT PRIMARY KEY,
    attempts   INT NOT NULL DEFAULT 0,
    last_error TEXT,
    done       BOOLEAN NOT NULL DEFAULT FALSE,
    marked     BOOLEAN,
    found      INT NOT NULL DEFAULT 0,
    updated    BIGINT
);

-- Журнал прибирань: /promise_forget видаляє обіцянку разом із ревізіями, тож
-- без знімка помилку не відкотиш ніяк (той самий принцип, що entity_merges).
CREATE TABLE IF NOT EXISTS promise_purges (
    id         BIGSERIAL PRIMARY KEY,
    payload    JSONB NOT NULL,
    reason     TEXT,
    decided_by TEXT,
    created    BIGINT,
    restored   BIGINT
);
-- `run` — номер ПРОГОНУ прибирання. Одна обіцянка відкочується за id знімка,
-- але масове прибирання (напр. чистка від немиколаївських, /promise_prune)
-- знімає сотні одразу, і повертати їх по одній неможливо фізично.
ALTER TABLE promise_purges ADD COLUMN IF NOT EXISTS run BIGINT;
CREATE INDEX IF NOT EXISTS idx_promise_purges_run ON promise_purges (run);
-- Номер прогону — послідовність, а не час: два прибирання в одну секунду
-- (тап по кнопці, потім одразу другий) злиплись би в один прогін, і відкат
-- одного повернув би обидва. Заодно 1, 2, 3 набирати руками легше за unix.
CREATE SEQUENCE IF NOT EXISTS promise_purge_run_seq;

-- «Ні, різні» — рішення, яке треба пам'ятати НАЗАВЖДИ.
--
-- Детектор дублів працює на сигналах, а не на знанні: «Побудувати
-- меморіальний комплекс на Центральному кладовищі» і «…у Корабельному
-- районі» мають однаковий строк (15.06.2028), спільного обіцяльника і схожу
-- назву — тобто всі три сигнали. Це два РІЗНІ комплекси, і жоден сигнал
-- цього не бачить. Без пам'яті пара поверталась би в екран щоразу, і людина
-- ухвалювала б те саме рішення до нескінченності (урок role_pairs).
CREATE TABLE IF NOT EXISTS promise_pairs (
    a          BIGINT NOT NULL,
    b          BIGINT NOT NULL,
    decided_by TEXT,
    created    BIGINT,
    PRIMARY KEY (a, b)
);
"""


def ensure_schema(conn=None):
    """Ідемпотентно піднімає схему банку тем. Викликається ліниво перед першою
    операцією — як bot_db.ensure_schema."""
    own = conn is None
    conn = conn or ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(DDL)
    finally:
        if own:
            conn.close()


# ---------- Похідні поля (рахуються КОДОМ, не питаються в моделі) ----------

def derive_verifiability(item):
    """Клас перевірки з властивостей тексту (§2.1, §2.4, §2.5).

    Порядок умов — не косметика, кожна лінія має підставу:

    1. немає ні дати, ні критерію, ні документа-підстави → перевіряти НІЧИМ.
       Це і є формула популізму: «краще ніж було» не можна прострочити, бо
       нема чого прострочувати. Стоїть ПЕРШОЮ, бо тригер тут не рятує: навіть
       коли подія настане, сказати «виконано» все одно буде нічим.
    2. обіцянка НЕ робити або прив'язана до події → чекаємо подію. Для
       `not_do` перевірка інвертується: порушенням є ДІЯ, а мовчання означає,
       що обіцянку тримають, тому дедлайну там немає й бути не може (§2.4).
    3. є дата → можна прострочити.
    4. решта — дія зрозуміла, горизонту не дали (§2.5: «має розробити за
       розпорядженням» — без дати, але сильне).
    """
    has_deadline = bool(item.get("deadline"))
    has_criterion = bool((item.get("criterion") or "").strip())
    has_document = bool((item.get("based_on_document") or "").strip())
    if not has_deadline and not has_criterion and not has_document:
        return "unfalsifiable"
    if item.get("polarity") == "not_do" or (item.get("trigger_event") or "").strip():
        return "event_triggered"
    if has_deadline:
        return "measurable"
    return "undated"


SOURCE_WORD = {
    "tender": "умова тендеру",
    "government_decision": "рішення органу влади",
    "official_statement": "заява посадовця",
    "social_comment": "коментар у соцмережі",
    "journalist": "формулювання журналіста",
}

METHOD_WORD = {
    "field_check": "піти й подивитись",
    "document_request": "запит до органу",
    "official_statement": "спитати посадовця",
    "data": "реєстр або дані",
}

# Модальність підписуємо ІМЕННИКОМ, а не дієсловом. Дієслово тягне за собою
# рід і число («пообіцяв» / «пообіцяла» / «пообіцяли»), а обіцяльником буває і
# жінка, і колектив без імені («власники зупинкового комплексу»), і безособове
# «планують» — на будь-якому дієслові підпис почав би брехати. Іменник тримає
# при цьому всю шкалу: гарантія твердіша за обіцянку, обіцянка — за план.
MODALITY_WORD = {
    "guaranteed": "гарантія",
    "promised": "обіцянка",
    "planned": "план",
    "considered": "розгляд",
    "hedged": "припущення",
}

STATE_WORD = {
    "expected": "чекаємо",
    "done": "виконано",
    "failed": "зірвано",
    "abandoned": "закинуто",
    "void": "підстава зникла",
    "superseded": "замінено новішим",
    "unknown": "невідомо",
}


def populism_reason(row):
    """Підстава підказки «схоже на популізм» — і саме ПІДКАЗКИ, не вироку.

    Рішення Олега 03.08, після чотирьох прогонів приймання: брати обіцянку в
    роботу чи ні — вирішує людина, а бот показує факти. Тому:

    • мітка більше НЕ залежить від того, чи модель лишила `criterion` порожнім.
      Раніше залежала — і це була помилка проєктування: заява Кіма «Ми все
      повернемо краще ніж було» переставала бути популізмом рівно тому, що
      модель ввічливо вписала в критерій переказ самої обіцянки. Тобто мітка
      відображала дисципліну моделі, а не текст;
    • вмикається вона на тому, що з тексту читається НАДІЙНО: немає названої
      дати і немає документа-підстави;
    • а критерій, коли він є, показується ДОСЛІВНО — щоб людина за секунду
      побачила, що «відновлено краще, ніж було» це не критерій, і вирішила
      сама. Машина цього не розрізняє й не мусить.

    Не підказуємо там, де обіцянка й не мала мати дати: `not_do` перевіряється
    дією, а тригер уже називає, чого чекати.
    """
    if row.get("deadline") or (row.get("based_on_document") or "").strip():
        return None
    if row.get("polarity") == "not_do" or (row.get("trigger_event") or "").strip():
        return None
    parts = ["немає дати"]
    crit = (row.get("criterion") or "").strip()
    parts.append(f"критерій зі слів обіцяльника: «{crit}»" if crit
                 else "немає критерію")
    cond = (row.get("condition") or "").strip()
    if cond:
        parts.append(f"умова «{cond}»")
    src = SOURCE_WORD.get(row.get("source_type"))
    if src:
        parts.append(src)
    return " · ".join(parts)


def grace_seconds(precision):
    return GRACE_DAYS.get(precision, DEFAULT_GRACE_DAYS) * 86400


def rings(row):
    """Чи взагалі має право дзвонити ця обіцянка (§5).

    Не дзвонять: `hedged`/`considered` (нема чого прострочувати) і умовні —
    «після завершення війни» це горизонт поза контролем того, хто обіцяв, і
    рахувати таке зривом нечесно. Обидві лежать у банку й рахуються, але
    сповіщення по них не йде.
    """
    if row.get("status") != "expected":
        return False
    if row.get("modality") not in RING_MODALITY:
        return False
    if (row.get("condition") or "").strip():
        return False
    return True


def queue_class(row, now=None):
    """Клас черги — те, чим смуга кольору в макеті екрана кодує СПОСІБ
    перевірки, а не важливість (§6). Порядок той самий, що в /promises."""
    now = now or int(time.time())
    v = row.get("verifiability")
    if row.get("status") != "expected":
        return "closed"
    if v == "unfalsifiable":
        return "noproof"
    if v == "event_triggered":
        return "waiting"
    deadline = row.get("deadline")
    if deadline:
        due = int(deadline) + grace_seconds(row.get("deadline_precision"))
        if now > due:
            # Строк минув — це ФАКТ, і перевірка його не скасовує. Раніше тут
            # стояла умова `checked < deadline`, і обіцянка, яку щойно
            # подивились, провалювалась у `soon`: міст 2020 року отримував
            # підпис «Строк за 0 днів». Клас = стан обіцянки, а «щойно
            # дивились» — це терміновість, і вона знижується в priority().
            return "overdue"
        if int(deadline) - now <= SOON_DAYS * 86400:
            return "soon"
        return "open"
    silence_from = max(row.get("checked_at") or 0, row.get("last_seen") or 0)
    if silence_from and now - silence_from > SILENCE_DAYS * 86400:
        return "stale"
    return "open"


CLASS_ORDER = ["overdue", "soon", "waiting", "stale", "noproof", "open", "closed"]

CLASS_WORD = {
    "overdue": "строк минув",
    "soon": "скоро",
    "waiting": "чекає події",
    "stale": "давно не питали",
    "noproof": "перевірити нічим",
    "open": "строк попереду",
    "closed": "закрито",
}


def priority(row, now=None):
    """Сортування черги (§5: «Пріоритет, а не планка»).

    Спершу хотілось відсівати дрібне за сумою й органом влади — але кейс §2.3
    показав, що так відсіється найкраще: три дні строку і перевірка за десять
    хвилин пішки з фотоапаратом. Тому дешевизна перевірки ПІДІЙМАЄ тему, а
    гроші й орган лишились множником, а не перепусткою.
    """
    now = now or int(time.time())
    cls = queue_class(row, now)
    p = {"overdue": 100, "soon": 60, "waiting": 45,
         "stale": 40, "noproof": 10, "open": 20, "closed": 0}.get(cls, 0)
    method = row.get("verification_method")
    if method == "field_check":
        p += 40
    elif method == "document_request":
        p += 15
    deadline, first_seen = row.get("deadline"), row.get("first_seen")
    if deadline and first_seen and 0 < int(deadline) - int(first_seen) <= 60 * 86400:
        p += 25          # короткий горизонт — швидкий фоллоу-ап
    if row.get("amount"):
        p += 10
    if row.get("owner_entity_id"):
        p += 10
    if row.get("framed_as_promise"):
        p += 15          # редакція вже вважала це вартим фіксації
    # Щойно ходили дивитись — тема опускається, але з черги не зникає. Саме це
    # й обіцяє кнопка «Перевірили»: «тема йде вниз, але з банку не зникає».
    checked = row.get("checked_at") or 0
    if checked:
        days = (now - int(checked)) / 86400
        if days < 14:
            p -= 70
        elif days < 45:
            p -= 30
    if row.get("audience") == "media":
        p += 10          # «обіцяв редакції й не зробив» — окремий сюжет
    if not rings(row):
        p -= 30          # умовні й «можуть перенести» вниз, але з банку не зникають
    return p


# ---------- Дати ----------

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_deadline(value):
    """'YYYY-MM-DD' → unix кінця того дня (23:59:59 за Києвом ≈ UTC+3).

    Кінець дня, а не початок: «до 31 грудня» означає «включно з 31-м», і
    старт доби зробив би обіцянку простроченою на добу раніше строку.
    """
    if not value:
        return None
    m = _DATE_RE.match(str(value).strip())
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      23, 59, 59, tzinfo=timezone(timedelta(hours=3)))
    except ValueError:
        return None
    return int(dt.timestamp())


def fmt_date(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y") if ts else "—"


def plural(n, one, few, many):
    """Українське відмінювання після числа: 1 день · 2 дні · 5 днів.

    Дрібниця, але рядок «строк минув 6 р тому» читається як машинний лог, а
    банк тем має читатись як речення редактора.
    """
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def human_gap(seconds):
    """«2 дні», «7 місяців», «6 років» — без хвостів на кшталт «2134 днів»."""
    days = max(0, int(seconds) // 86400)
    if days < 45:
        return f"{days} {plural(days, 'день', 'дні', 'днів')}"
    months = days // 30
    if months < 18:
        return f"{months} {plural(months, 'місяць', 'місяці', 'місяців')}"
    years = days // 365
    return f"{years} {plural(years, 'рік', 'роки', 'років')}"


# ---------- Резолв сутностей ----------

def resolve_entity(cur, name, kinds=None):
    """Назва → id картки сутнісного шару, або None.

    Зіставлення тим самим ключем, що write_results сутностей: точний збіг
    нормалізованого імені, потім аліаси, потім обчислений ключ (організація
    без правової форми, вулиця з розкритим скороченням). Інакше «КП
    «Миколаївводоканал»» в обіцянці не знайшов би картку «Миколаївводоканал»,
    і ланцюг розірвався б на рівному місці.
    """
    n = ep.norm(name)
    if not n:
        return None
    kind_cond, params = "", [n, n]
    if kinds:
        kind_cond = " AND kind = ANY(%s)"
        params.append(list(kinds))
    cur.execute(
        "SELECT id FROM entities WHERE (lower(btrim(name_ua)) = %s "
        "   OR lower(btrim(name_ru)) = %s)" + kind_cond +
        " ORDER BY mentions DESC LIMIT 1", params)
    row = cur.fetchone()
    if row:
        return row[0]
    # аліаси (там лежать сирі написання, зняті злиттями й канонізацією)
    cur.execute(
        "SELECT id FROM entities WHERE EXISTS ("
        "  SELECT 1 FROM unnest(aliases) a WHERE lower(btrim(a)) = %s)"
        + (" AND kind = ANY(%s)" if kinds else "") +
        " ORDER BY mentions DESC LIMIT 1",
        ([n, list(kinds)] if kinds else [n]))
    row = cur.fetchone()
    if row:
        return row[0]
    for kind in (kinds or ("org", "place")):
        lk = ep.loose_key(kind, name)
        if not lk:
            continue
        cur.execute(
            "SELECT id FROM entities WHERE kind = %s AND ("
            "  lower(btrim(coalesce(name_ua,''))) = %s OR "
            "  lower(btrim(coalesce(name_ru,''))) = %s) "
            "ORDER BY mentions DESC LIMIT 1", (kind, lk, lk))
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def _enum(value, allowed):
    """Значення поза таксономією не пускаємо в базу мовчки — воно стає NULL.

    json_schema на боці API це вже стереже, але вибірки нижче припускають
    рівно ці набори, і одне ліве значення тихо вивалило б обіцянку з усіх
    фільтрів одразу.
    """
    v = (value or "").strip().lower() if isinstance(value, str) else None
    return v if v in allowed else None


def _amount(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------- Витік чужої мови в генерації ----------
#
# Реальний випадок 03.08: у назві «підвищити енергостійкість電транспорту» і в
# цитатах «Зараз續є інвентаризація», «в лікарнях області続встановлення». Це не
# мохібейк у норі й не биті дані сайту — 電 це «електрика», 續/続 це «триває».
# Модель (Haiku) підставляє ІЄРОГЛІФ замість українського слова: класичний
# витік мови в багатомовних моделях на low-resource мовах. Замір: 4 записи з
# 473, тобто ~1%.
#
# Лікується асиметрично, і саме тому двома різними шляхами:
#   • ЦИТАТА мусить бути дослівна, а текст статті лежить у норі — отже
#     полагодити можна БЕЗ моделі й без грошей, просто знайшовши справжній
#     фрагмент. Заразом це робить справжньою вимогу §3, яка досі перевіряла
#     лише «цитата непорожня», а не «цитата справді є в тексті»;
#   • НАЗВА написана моделлю, звіряти її нема з чим. Такий запис не пишемо
#     зовсім: стаття лишається в черзі й наступна спроба майже напевно дасть
#     чистий текст (це шум семплінгу, а не стала помилка).

def _suspicious_char(ch):
    """Символ, якого в українському тексті бути не може."""
    o = ord(ch)
    if o < 0x0250 or 0x0400 <= o <= 0x04FF:      # латиниця, кирилиця
        return False
    if 0x2000 <= o <= 0x206F or 0x20A0 <= o <= 0x20BF:
        return False                              # типографіка, валюти
    if o in (0x2116, 0x00B0, 0x00A0):             # №, °, нерозривний
        return False
    return unicodedata.category(ch) not in ("So", "Sk")


def has_glitch(text):
    return any(_suspicious_char(ch) for ch in (text or ""))


# Наголоси — і ТІЛЬКИ вони. Знімати всі комбіновані знаки (категорію Mn) не
# можна: у NFD українські «й» і «ї» самі складені з букви й такого знаку, і
# «Зеленський» перетворювався б на «Зеленськии». Тест це й зловив.
_STRESS = {"́", "̀"}


def clean_text(text):
    """Прибрати наголоси, які модель зрідка ставить у власних назвах
    («Миколаї́в»): у нашому корпусі їх не буває, а пошук вони ламають."""
    if not text:
        return text
    return unicodedata.normalize(
        "NFC", "".join(c for c in unicodedata.normalize("NFD", text)
                       if c not in _STRESS))


_MATCH_MAP = str.maketrans({"’": "'", "ʼ": "'", "‘": "'",
                            "«": '"', "»": '"', "“": '"', "”": '"',
                            "–": "-", "—": "-", "‑": "-",
                            " ": " "})


def _match_key(s):
    """Форма для зіставлення: цитата й текст можуть різнитись типографікою
    (апостроф, лапки, тире), і це не привід вважати цитату вигаданою."""
    return " ".join(clean_text(s or "").translate(_MATCH_MAP).lower().split())


def find_verbatim(quote, *texts):
    """Чи є цитата в тексті статті дослівно. Повертає фрагмент ОРИГІНАЛЬНИМ
    написанням (з тексту, не з відповіді моделі) або None."""
    key = _match_key(quote)
    if not key:
        return None
    for text in texts:
        if not text:
            continue
        # Індекс «позиція в ключі → позиція в оригіналі»: без нього повернути
        # оригінальне написання неможливо, а повертати нормалізоване не можна
        # — цитата має виглядати так, як у статті.
        norm, index = [], []
        prev_space = True
        for i, ch in enumerate(clean_text(text)):
            c = ch.translate(_MATCH_MAP).lower()
            if c.isspace():
                if prev_space:
                    continue
                norm.append(" ")
                index.append(i)
                prev_space = True
            else:
                norm.append(c)
                index.append(i)
                prev_space = False
        flat = "".join(norm)
        pos = flat.find(key)
        if pos < 0:
            continue
        start = index[pos]
        end = index[min(pos + len(key) - 1, len(index) - 1)] + 1
        return clean_text(text)[start:end].strip()
    return None


def repair_quote(quote, *texts, min_anchor=18):
    """Полагодити цитату з ієрогліфом, знайшовши справжній фрагмент у тексті.

    Беремо найдовший ЧИСТИЙ шматок цитати як якір, знаходимо його в статті й
    повертаємо оригінальний фрагмент тієї ж приблизно довжини. Якір має бути
    досить довгим, інакше «в місті» знайдеться будь-де.
    """
    if not quote:
        return None
    chunks, cur_chunk = [], []
    for ch in quote:
        if _suspicious_char(ch):
            chunks.append("".join(cur_chunk))
            cur_chunk = []
        else:
            cur_chunk.append(ch)
    chunks.append("".join(cur_chunk))
    anchor = max(chunks, key=len).strip()
    if len(anchor) < min_anchor:
        return None
    found = find_verbatim(anchor, *texts)
    if not found:
        return None
    for text in texts:
        if not text:
            continue
        clean = clean_text(text)
        at = clean.find(found)
        if at < 0:
            continue
        # Розгортаємо від якоря вліво до початку речення й вправо до кінця
        # приблизно тієї довжини, що була в цитаті.
        left = at
        while left > 0 and clean[left - 1] not in ".!?\n":
            left -= 1
        right = min(len(clean), max(at + len(found), left + len(quote) + 40))
        while right < len(clean) and clean[right] not in ".!?\n":
            right += 1
        return clean[left:right].strip()
    return found


def prepare(cur, item):
    """Сирий запис витягу → резолвлені id + похідні поля.

    Нічого не пише. Повертає dict, придатний і для пошуку кандидатів, і для
    вставки, і для показу в /promise_test (де в базу не йде взагалі нічого).
    """
    out = dict(item)
    # Наголоси в іменах («Миколаї́в») модель ставить зрідка, але вони ламають
    # і пошук, і зіставлення з картками сутностей — знімаємо на вході.
    for f in ("title", "subject", "promiser", "promiser_role", "owner",
              "reported_by", "criterion", "condition", "trigger_event",
              "based_on_document"):
        if isinstance(out.get(f), str):
            out[f] = clean_text(out[f])
    out["modality"] = _enum(item.get("modality"), MODALITY)
    out["source_type"] = _enum(item.get("source_type"), SOURCE_TYPE)
    out["verification_method"] = _enum(item.get("verification_method"),
                                       VERIFICATION_METHOD)
    out["audience"] = _enum(item.get("audience"), AUDIENCE)
    out["polarity"] = _enum(item.get("polarity"), POLARITY) or "do"
    out["deadline_precision"] = _enum(item.get("deadline_precision"), PRECISION)
    out["amount"] = _amount(item.get("amount"))
    out["deadline"] = parse_deadline(item.get("deadline"))
    if not out["deadline"]:
        # Точність без самої дати брехала б («year» без року) — крім `vague`,
        # яка саме й означає «горизонт названо, але не датою».
        if out.get("deadline_precision") != "vague":
            out["deadline_precision"] = None
    out["verifiability"] = derive_verifiability(out)
    out["subject_entity_id"] = resolve_entity(cur, item.get("subject"))
    out["subject_key"] = ep.norm(item.get("subject"))
    out["promiser_entity_id"] = resolve_entity(
        cur, item.get("promiser"), ("person", "org"))
    out["owner_entity_id"] = resolve_entity(cur, item.get("owner"), ("org",))
    out["reported_by_entity_id"] = resolve_entity(
        cur, item.get("reported_by"), ("person", "org"))
    obj_ids = []
    for name in item.get("objects") or []:
        eid = resolve_entity(cur, name)
        if eid and eid not in obj_ids:
            obj_ids.append(eid)
    if out["subject_entity_id"] and out["subject_entity_id"] not in obj_ids:
        obj_ids.insert(0, out["subject_entity_id"])
    out["object_ids"] = obj_ids
    return out


# ---------- Кандидати ланцюга (дешевий пре-фільтр БЕЗ AI) ----------

CANDIDATE_LIMIT = 8

# Поріг схожості назв. Живе тут, бо ним користуються ДВІ речі, які мусять
# збігатись: пре-фільтр кандидатів (щоб дубль не з'явився) і детектор дублів
# (щоб знайти вже наявні). Розійдуться — і детектор показуватиме пари, які
# пре-фільтр більше не створює, або навпаки.
DUPE_SIM = 0.4


def candidates(cur, prepared, limit=CANDIDATE_LIMIT):
    """Обіцянки, з якими ЦЯ може виявитись тією самою (§4 крок 1).

    Пре-фільтр копійчаний: спільна картка-предмет (сутнісний шар уже дає
    «гімназія №2» як ключ) або спільний нормалізований предмет, коли предмет
    сутністю не є («відкриті апаратні наради», §2.2). Полярність мусить
    збігатись: «зробити» і «не робити» — ніколи не одне зобов'язання.

    Рішення ухвалює суддя, не цей запит: один об'єкт може мати кілька
    незалежних обіцянок (реставрація, укриття, обладнання) — §4 крок 3.
    """
    eid = prepared.get("subject_entity_id")
    key = prepared.get("subject_key")
    conds, params = [], []
    if eid:
        conds.append("(c.subject_entity_id = %s OR EXISTS ("
                     "  SELECT 1 FROM commitment_objects o "
                     "  WHERE o.commitment_id = c.id AND o.entity_id = %s))")
        params.extend([eid, eid])
    if key:
        conds.append("c.subject_key = %s")
        params.append(key)
    # Третє джерело кандидатів: ТОЙ САМИЙ обіцяльник із ТИМ САМИМ строком.
    #
    # Потрібне через перейменування об'єкта. Умова тендеру «Житлопромбуд-8 мав
    # виконати роботи до 31.12.2024» переказана і в статті 2024 року про
    # ГІМНАЗІЮ №2, і в статті 2025-го про ЛІЦЕЙ №2 — це одне зобов'язання, але
    # предмети резолвляться в РІЗНІ картки, тож за предметом кандидат не
    # знаходиться і ланцюг рветься рівно там, де він найцінніший.
    #
    # Схожість назв тут не рятує: вимірювання на живих написаннях дало
    # similarity('гімназія №2', 'ліцей №2') = 0.12, тобто поріг, який зловив би
    # цю пару, зліпив би пів бази. А от «той самий актор + та сама дата» —
    # сигнал вузький і перевірюваний.
    #
    # Це джерело КАНДИДАТІВ, не рішення: зливає далі суддя, і він же відсіює
    # випадок «один підрядник, дві різні будівлі, один строк».
    promiser = prepared.get("promiser_entity_id")
    deadline = prepared.get("deadline")
    if promiser and deadline:
        conds.append(
            "(c.deadline = %s AND EXISTS ("
            "  SELECT 1 FROM commitment_revisions r2 "
            "  WHERE r2.commitment_id = c.id AND r2.promiser_entity_id = %s))")
        params.extend([deadline, promiser])
    # Четверте й п'яте джерела — БЕЗ жодної картки сутності.
    #
    # Додано 03.08 після першого місяця на обсязі, і це головна причина, чому
    # банк роздувся: усі три джерела вище вимагають або резолвнутої картки
    # (subject_entity_id, promiser_entity_id), або ТОЧНОГО збігу рядка
    # (subject_key). Жодне з них не спрацювало на парі «Організувати
    # прибирання та покос трави на майданчику „Казка"» / «…прибирання на
    # майданчику „Казка" у Корабельному районі»: майданчик картки не має,
    # адміністрація району теж, а ключі предмета різні на одне слово. Немає
    # кандидата — немає й ПИТАННЯ до судді, і другий запис лягає новою
    # обіцянкою. Саме тому суддя відпрацював 63 рази на 762 статті.
    #
    # Обидва нові джерела прив'язані до ОДНАКОВОГО СТРОКУ, і це не
    # обережність заради обережності: без нього текстова схожість зліпила б
    # «відремонтувати» й «освітити» ту саму вулицю, а similarity('гімназія
    # №2', 'ліцей №2') = 0.12 показує, що самій схожості вірити не можна в
    # принципі. Пара «той самий день + той самий актор (або схожа назва)» —
    # вузька, і рішення однаково ухвалює суддя.
    # Ключ `promiser` — сире написання з витягу; саме воно лягає в
    # revisions.promiser_text, тож порівнюємо з тим самим, що записано.
    promiser_text = (prepared.get("promiser") or "").strip().lower()
    if promiser_text and deadline:
        conds.append(
            "(c.deadline = %s AND (lower(coalesce(c.owner_text,'')) = %s "
            "  OR EXISTS (SELECT 1 FROM commitment_revisions r3 "
            "             WHERE r3.commitment_id = c.id "
            "               AND lower(coalesce(r3.promiser_text,'')) = %s)))")
        params.extend([deadline, promiser_text, promiser_text])
    title = (prepared.get("title") or "").strip()
    if title and deadline:
        conds.append("(c.deadline = %s AND similarity(c.title, %s) >= %s)")
        params.extend([deadline, title, DUPE_SIM])
    if not conds:
        return []
    params.extend([prepared.get("polarity") or "do", limit])
    cur.execute(
        "SELECT c.id, c.title, c.subject, c.deadline, c.modality, c.status, "
        "       c.criterion, c.topic_id, "
        "       (SELECT r.quote FROM commitment_revisions r "
        "         WHERE r.commitment_id = c.id ORDER BY r.id LIMIT 1) AS quote, "
        "       (SELECT r.promiser_text FROM commitment_revisions r "
        "         WHERE r.commitment_id = c.id ORDER BY r.id LIMIT 1) AS promiser "
        "FROM commitments c "
        f"WHERE ({' OR '.join(conds)}) AND coalesce(c.polarity, 'do') = %s "
        "ORDER BY c.last_seen DESC NULLS LAST LIMIT %s", params)
    cols = ("id", "title", "subject", "deadline", "modality", "status",
            "criterion", "topic_id", "quote", "promiser")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- Запис ----------

def _dedup_key(article_id, quote, title=None, deadline=None, polarity=None):
    """Ключ ідемпотентності ревізії.

    Спершу тут була пара «стаття + цитата» — і на першому ж реальному тексті
    вона почала ГУБИТИ дані. У статті 294413 одне речення Скарлата несе два
    різні горизонти одразу: «Основну частину робіт планується завершити до
    кінця цього року, але остаточне завершення може бути відкладене до 2025
    року». Це два зобов'язання з різними датами й різною модальністю (§2,
    висновок 2), і другого при ключі-цитаті просто не існувало б. Так само
    §2.2 вимагає віддати з ОДНОГО блоку і перевірювану обіцянку, і риторику.

    Тому в ключ входить ще й те, чим ці записи різняться. Ціна відома й
    прийнята: якщо модель на перечиті інакше сформулює `title`, з'явиться
    видимий дубль, який лікується /promise_forget. Мовчазна втрата
    обов'язкового запису гірша за помітний дубль, а всі шляхи, що можуть
    перечитати статтю (/promise_retest, повторний скан), спершу знімають з
    неї старе.
    """
    raw = "|".join([str(article_id), ep.norm(quote) or "", ep.norm(title) or "",
                    str(deadline or ""), polarity or ""])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def existing_revision(cur, article_id, prepared):
    """Цей самий запис із цієї статті вже є? Перевіряємо ДО створення
    обіцянки — інакше повторний прогін лишав би картку-сироту без ревізій."""
    cur.execute("SELECT id, commitment_id FROM commitment_revisions WHERE dedup = %s",
                (_dedup_key(article_id, prepared.get("quote"), prepared.get("title"),
                            prepared.get("deadline"), prepared.get("polarity")),))
    row = cur.fetchone()
    return {"id": row[0], "commitment_id": row[1]} if row else None


def _attach_topic(cur, prepared, commitment_id=None, existing_topic=None):
    """Тема, під якою живе зобов'язання (§2.6).

    Зшивається спільною КАРТКОЮ предмета — саме тому теми переживають
    роз'їхані картки перейменованого об'єкта: у `entity_ids` просто лежать
    обидві. Коли предмет сутністю не є, ключем стає нормалізований текст.
    """
    if existing_topic:
        return existing_topic
    now = int(time.time())
    eid, key = prepared.get("subject_entity_id"), prepared.get("subject_key")
    if eid:
        cur.execute("SELECT id FROM topics WHERE %s = ANY(entity_ids) LIMIT 1", (eid,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE topics SET last_event = %s WHERE id = %s", (now, row[0]))
            return row[0]
    if key:
        cur.execute("SELECT id FROM topics WHERE %s = ANY(subject_keys) LIMIT 1", (key,))
        row = cur.fetchone()
        if row:
            if eid:
                cur.execute(
                    "UPDATE topics SET entity_ids = "
                    "  (SELECT array_agg(DISTINCT x) FROM unnest(entity_ids || %s::bigint) x), "
                    "  last_event = %s WHERE id = %s", (eid, now, row[0]))
            return row[0]
    title = (prepared.get("subject") or prepared.get("title") or "").strip()
    cur.execute(
        "INSERT INTO topics (title, entity_ids, subject_keys, status, opened, last_event) "
        "VALUES (%s, %s, %s, 'open', %s, %s) RETURNING id",
        (title[:200], [eid] if eid else [], [key] if key else [], now, now))
    return cur.fetchone()[0]


COMMITMENT_FIELDS = (
    "topic_id", "title", "subject", "subject_entity_id", "subject_key",
    "owner_entity_id", "owner_text", "audience", "verifiability", "polarity",
    "trigger_event", "deadline", "deadline_precision", "criterion",
    "verification_method", "condition", "condition_self_judged", "actor_hidden",
    "framed_as_promise", "based_on_document", "amount", "modality", "source_type",
)


def record(cur, article, prepared, commitment_id=None, link_confidence=None):
    """Записати ревізію (і саму обіцянку, якщо вона нова).

    article — рядок нори (id, published). prepared — результат prepare().
    commitment_id — рішення судді про ланцюг (None = нова обіцянка).

    Повертає (commitment_id, статус: 'new' | 'revision' | 'dup').
    """
    quote = (prepared.get("quote") or "").strip()
    if not quote:
        # §3: немає дослівного фрагмента — обіцянку не записуємо. Найдешевший
        # спосіб не отримати реєстр галюцинацій.
        return None, "noquote"

    # Звірка цитати з ТЕКСТОМ СТАТТІ. Досі §3 перевіряв лише «поле непорожнє»,
    # тобто ловив мовчання моделі, але не її фантазію. Тексти лежать поруч —
    # у payload статті, — тож перевірка копійчана.
    texts = (article.get("text_ua"), article.get("text_ru"))
    verified = None
    if any(texts):
        exact = find_verbatim(quote, *texts)
        if exact:
            quote, verified = exact, True      # беремо написання СТАТТІ
        else:
            fixed = repair_quote(quote, *texts) if has_glitch(quote) else None
            if fixed:
                quote, verified = fixed, True
            else:
                verified = False
    if has_glitch(quote) or has_glitch(prepared.get("title") or ""):
        # Полагодити не вдалось. Пишемо — означає лишити в банку видимо биту
        # картку; краще лишити статтю в черзі: це шум семплінгу, і наступна
        # спроба майже напевно дасть чистий текст.
        return None, "glitch"
    prepared = dict(prepared, quote=quote)
    dup = existing_revision(cur, article["id"], prepared)
    if dup:
        return dup["commitment_id"], "dup"

    now = int(time.time())
    outcome = "revision"
    if commitment_id is None:
        topic_id = _attach_topic(cur, prepared)
        values = [
            topic_id, (prepared.get("title") or "")[:300], prepared.get("subject"),
            prepared.get("subject_entity_id"), prepared.get("subject_key"),
            prepared.get("owner_entity_id"), prepared.get("owner"),
            prepared.get("audience"), prepared.get("verifiability"),
            prepared.get("polarity") or "do", prepared.get("trigger_event"),
            prepared.get("deadline"), prepared.get("deadline_precision"),
            prepared.get("criterion"), prepared.get("verification_method"),
            prepared.get("condition"), bool(prepared.get("condition_self_judged")),
            bool(prepared.get("actor_hidden")), bool(prepared.get("framed_as_promise")),
            prepared.get("based_on_document"), prepared.get("amount"),
            prepared.get("modality"), prepared.get("source_type"),
        ]
        cur.execute(
            f"INSERT INTO commitments ({', '.join(COMMITMENT_FIELDS)}, status, created) "
            f"VALUES ({', '.join(['%s'] * len(COMMITMENT_FIELDS))}, 'expected', %s) "
            "RETURNING id", values + [now])
        commitment_id = cur.fetchone()[0]
        outcome = "new"

    cur.execute(
        "INSERT INTO commitment_revisions "
        "(commitment_id, article_id, stated_deadline, deadline_precision, modality, "
        " source_type, promiser_entity_id, promiser_text, promiser_role, "
        " reported_by_entity_id, reported_by_text, condition, trigger_event, quote, "
        " link_confidence, quote_verified, dedup, created) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (dedup) DO NOTHING",
        (commitment_id, article["id"], prepared.get("deadline"),
         prepared.get("deadline_precision"), prepared.get("modality"),
         prepared.get("source_type"), prepared.get("promiser_entity_id"),
         prepared.get("promiser"), prepared.get("promiser_role"),
         prepared.get("reported_by_entity_id"), prepared.get("reported_by"),
         prepared.get("condition"), prepared.get("trigger_event"), quote[:1000],
         link_confidence, verified,
         _dedup_key(article["id"], quote, prepared.get("title"),
                    prepared.get("deadline"), prepared.get("polarity")), now))

    for eid in prepared.get("object_ids") or []:
        cur.execute(
            "INSERT INTO commitment_objects (commitment_id, entity_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING", (commitment_id, eid))
    refresh(cur, commitment_id)
    return commitment_id, outcome


def refresh(cur, commitment_id):
    """Перерахувати стан обіцянки з її ревізій.

    Поточний горизонт, модальність, умова й тип джерела — це завжди стан
    НАЙСВІЖІШОЇ ревізії: ревізія міняє не тільки дату, а й форму зобов'язання
    (§2.2 — умовна стала датованою). Рахується з даних, тому ідемпотентно й
    не залежить від порядку заливки.
    """
    cur.execute(
        "SELECT r.stated_deadline, r.deadline_precision, r.modality, r.source_type, "
        "       r.condition, r.trigger_event "
        "FROM commitment_revisions r LEFT JOIN articles a ON a.id = r.article_id "
        "WHERE r.commitment_id = %s "
        "ORDER BY coalesce(a.published, r.created) DESC, r.id DESC LIMIT 1",
        (commitment_id,))
    last = cur.fetchone()
    cur.execute(
        "SELECT count(*), min(coalesce(a.published, r.created)), "
        "       max(coalesce(a.published, r.created)) "
        "FROM commitment_revisions r LEFT JOIN articles a ON a.id = r.article_id "
        "WHERE r.commitment_id = %s", (commitment_id,))
    n, first_seen, last_seen = cur.fetchone()
    if not last:
        cur.execute("UPDATE commitments SET revisions = 0 WHERE id = %s", (commitment_id,))
        return
    deadline, precision, modality, source_type, condition, trigger = last
    cur.execute(
        "UPDATE commitments SET deadline = %s, deadline_precision = %s, modality = %s, "
        "source_type = %s, condition = %s, "
        "trigger_event = coalesce(%s, trigger_event), "
        "revisions = %s, first_seen = %s, last_seen = %s WHERE id = %s",
        (deadline, precision, modality, source_type, condition, trigger,
         n, first_seen, last_seen, commitment_id))
    # Клас перевірки перераховуємо з НОВОГО стану: умовна обіцянка, що
    # отримала дату, більше не «без горизонту» (§2.2).
    cur.execute(
        "SELECT deadline, criterion, based_on_document, polarity, trigger_event "
        "FROM commitments WHERE id = %s", (commitment_id,))
    d, crit, doc, pol, trig = cur.fetchone()
    v = derive_verifiability({"deadline": d, "criterion": crit,
                              "based_on_document": doc, "polarity": pol,
                              "trigger_event": trig})
    cur.execute("UPDATE commitments SET verifiability = %s WHERE id = %s",
                (v, commitment_id))


def mark_attempt(cur, article_id, marked=None, error=None, done=False, found=0):
    """Слід витягу по статті. done=True — витяг пройшов (хай і з порожнім
    результатом), інакше стаття вічно виглядала б необробленою."""
    cur.execute(
        "INSERT INTO promise_attempts (article_id, attempts, last_error, done, marked, found, updated) "
        "VALUES (%s, 1, %s, %s, %s, %s, %s) "
        "ON CONFLICT (article_id) DO UPDATE SET "
        "  attempts = promise_attempts.attempts + 1, "
        "  last_error = EXCLUDED.last_error, "
        "  done = promise_attempts.done OR EXCLUDED.done, "
        "  marked = coalesce(EXCLUDED.marked, promise_attempts.marked), "
        "  found = promise_attempts.found + EXCLUDED.found, "
        "  updated = EXCLUDED.updated",
        (article_id, error, done, marked, int(found or 0), int(time.time())))


# ---------- Читання ----------

COMMITMENT_COLS = """
    c.id, c.topic_id, c.title, c.subject, c.subject_entity_id, c.subject_key,
    c.owner_entity_id, c.owner_text, c.audience, c.status, c.verifiability,
    c.polarity, c.trigger_event, c.deadline, c.deadline_precision, c.criterion,
    c.verification_method, c.condition, c.condition_self_judged, c.actor_hidden,
    c.framed_as_promise, c.based_on_document, c.amount, c.modality, c.source_type,
    c.revisions, c.first_seen, c.last_seen, c.checked_at, c.checked_by,
    c.check_note
"""

_COMMITMENT_KEYS = [s.strip().split(".")[-1]
                    for s in COMMITMENT_COLS.replace("\n", " ").split(",") if s.strip()]


def _rows(cur):
    return [dict(zip(_COMMITMENT_KEYS, r)) for r in cur.fetchall()]


def _decorate(rows, now=None):
    now = now or int(time.time())
    for r in rows:
        r["amount"] = float(r["amount"]) if r["amount"] is not None else None
        r["class"] = queue_class(r, now)
        r["priority"] = priority(r, now)
        r["populism"] = populism_reason(r)
        r["rings"] = rings(r)
    rows.sort(key=lambda r: (-r["priority"], r["deadline"] or 1 << 62))
    return rows


def list_queue(cur, cls=None, limit=20, now=None, author_id=None):
    """Черга банку тем: що горить сьогодні. Порядок — той самий, що стане
    головним екраном апки (§6): спершу те, що горить, а не алфавіт.

    `author_id` — users.id автора статті (нора зберігає `articles.owner_id`).
    Це «обіцянки з МОЇХ новин»: журналістка писала матеріал, у ньому влада
    щось пообіцяла, і саме їй природно повернутись і спитати. Фільтр по
    ревізіях, а не по обіцянці: ланцюг міг зшитись через двох авторів, і тоді
    тема законно з'явиться в обох.
    """
    # `closed` — окремий кошик перевіреного. Без нього обіцянка, яку людина
    # сходила й позначила зірваною, ЗНИКАЛА б з екрана: статус більше не
    # 'expected', а іншого входу до неї немає. А саме ці записи — головний
    # продукт банку: на них посилаються в наступному тексті.
    where = ["c.status <> 'expected'" if cls == "closed" else "c.status = 'expected'"]
    params = []
    if author_id:
        where.append(
            "EXISTS (SELECT 1 FROM commitment_revisions r "
            "        JOIN articles a ON a.id = r.article_id "
            "       WHERE r.commitment_id = c.id AND a.owner_id = %s)")
        params.append(int(author_id))
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c "
                f"WHERE {' AND '.join(where)}", params)
    rows = _decorate(_rows(cur), now)
    if cls and cls != "closed":
        rows = [r for r in rows if r["class"] == cls]
    return rows[:limit] if limit else rows


def facet_counts(rows):
    counts = {}
    for r in rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    return counts


def get(cur, commitment_id):
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c WHERE c.id = %s",
                (commitment_id,))
    rows = _decorate(_rows(cur))
    return rows[0] if rows else None


REVISION_COLS = """
    r.id, r.commitment_id, r.article_id, r.stated_deadline, r.deadline_precision,
    r.modality, r.source_type, r.promiser_text, r.promiser_role, r.reported_by_text,
    r.condition, r.trigger_event, r.quote, r.link_confidence, r.created
"""
_REVISION_KEYS = [s.strip().split(".")[-1]
                  for s in REVISION_COLS.replace("\n", " ").split(",") if s.strip()]


def revisions(cur, commitment_ids):
    """Ревізії кількох обіцянок разом, у хронології — це і є «історія
    питання», заради якої банк тем будувався."""
    if not commitment_ids:
        return []
    cur.execute(
        f"SELECT {REVISION_COLS}, coalesce(a.published, r.created) AS published "
        "FROM commitment_revisions r LEFT JOIN articles a ON a.id = r.article_id "
        "WHERE r.commitment_id = ANY(%s) "
        # Усередині ОДНІЄЇ статті кроки шикуються за горизонтом, а не за
        # порядком вставки: добре написана стаття несе кілька точок ланцюга
        # одразу (обіцянка 2021-го в беку, тендерний строк, свіжий перенос), і
        # без цього таймлайн читається як випадковий список.
        "ORDER BY coalesce(a.published, r.created), "
        "         coalesce(r.stated_deadline, 0), r.id",
        (list(commitment_ids),))
    keys = _REVISION_KEYS + ["published"]
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def topic_commitments(cur, topic_id, exclude=None):
    """Решта зобов'язань тієї самої теми (§2.6: на одну тему приходять різні
    актори — підрядник, ОВА, Кабмін, і це одна історія)."""
    if not topic_id:
        return []
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c WHERE c.topic_id = %s",
                (topic_id,))
    rows = _decorate(_rows(cur))
    return [r for r in rows if r["id"] != exclude]


def by_article(cur, article_id):
    """Що записано з цієї статті (§8: петля виправлень)."""
    cur.execute(
        f"SELECT {COMMITMENT_COLS} FROM commitments c "
        "WHERE EXISTS (SELECT 1 FROM commitment_revisions r "
        "              WHERE r.commitment_id = c.id AND r.article_id = %s)",
        (article_id,))
    return _decorate(_rows(cur))


def search(cur, term, limit=20):
    """Пошук по обіцяльнику, об'єкту або темі.

    Через сутнісний шар, а не по підрядку: «Сєнкевич» знайде і картку людини,
    і її аліаси. Для ОРГАНІЗАЦІЇ додатково підтягуються обіцянки її
    посадовців — саме тут окупається канон ролей (`v_entity_roles`
    +`role_canon.org_entity_id`): «ОВА» без нього віддавала б лише ті
    обіцянки, де ОВА сама є обіцяльником, а особиста обіцянка заступника не
    спливала б (§6.2).
    """
    like = f"%{(term or '').strip().lower()}%"
    if len(like) <= 3:
        return [], []
    cur.execute(
        "SELECT id, kind, coalesce(name_ua, name_ru) AS name FROM entities "
        "WHERE lower(coalesce(name_ua,'')) LIKE %s OR lower(coalesce(name_ru,'')) LIKE %s "
        "   OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) LIKE %s) "
        "ORDER BY mentions DESC LIMIT 8", (like, like, like))
    matched = [{"id": r[0], "kind": r[1], "name": r[2]} for r in cur.fetchall()]
    ids = [m["id"] for m in matched]
    org_ids = [m["id"] for m in matched if m["kind"] == "org"]

    conds = ["lower(c.title) LIKE %s", "lower(coalesce(c.subject,'')) LIKE %s"]
    params = [like, like]
    if ids:
        conds.append("c.subject_entity_id = ANY(%s)")
        params.append(ids)
        conds.append("c.owner_entity_id = ANY(%s)")
        params.append(ids)
        conds.append("EXISTS (SELECT 1 FROM commitment_objects o "
                     "        WHERE o.commitment_id = c.id AND o.entity_id = ANY(%s))")
        params.append(ids)
        conds.append("EXISTS (SELECT 1 FROM commitment_revisions r "
                     "        WHERE r.commitment_id = c.id AND ("
                     "          r.promiser_entity_id = ANY(%s) OR "
                     "          r.reported_by_entity_id = ANY(%s)))")
        params.extend([ids, ids])
    cur.execute("SELECT to_regclass('v_entity_roles')")
    has_roles = cur.fetchone()[0] is not None
    if org_ids and has_roles:
        # Афіліація людина↔організація з канону посад: обіцянка посадовця
        # знаходиться за назвою його органу. Без довідника ролей (порожня база,
        # свіжий інстанс) просто пропускаємо — пошук за прямими зв'язками
        # працює й так, а падати через відсутню view він не має права.
        conds.append(
            "EXISTS (SELECT 1 FROM commitment_revisions r "
            "        JOIN v_entity_roles vr ON vr.entity_id = r.promiser_entity_id "
            "                              AND vr.article_id = r.article_id "
            "        WHERE r.commitment_id = c.id AND vr.org_entity_id = ANY(%s))")
        params.append(org_ids)
    params.append(limit)
    cur.execute(
        f"SELECT {COMMITMENT_COLS} FROM commitments c "
        f"WHERE {' OR '.join(conds)} ORDER BY c.last_seen DESC NULLS LAST LIMIT %s",
        params)
    return _decorate(_rows(cur)), matched


# Чим міг закінчитись похід журналіста. Порядок = порядок кнопок в апці.
#
# Це головне, чого бракувало першій версії: «Перевірили» лише відсувало тему
# вниз, і ВИСНОВОК людини нікуди не записувався. А він і є продукт: обіцянка,
# перевірена й зірвана, — не «менш терміновий рядок черги», а факт, на який
# посилаються в наступному тексті й з якого рахується, хто скільки разів
# зривав. Без цього банк лишався списком справ, а не реєстром.
CHECK_OUTCOMES = {
    "done": "виконано",
    "failed": "не виконано",
    "expected": "ще в процесі",
}


def mark_checked(cur, commitment_id, who, outcome=None, note=None):
    """Позначити перевіреною — і записати ЧИМ скінчилось.

    `outcome=None` лишає статус як був (стара поведінка: «подивився, воно ще
    в процесі»). Явний `done`/`failed` закриває обіцянку, і вона виходить із
    черги, але з банку не зникає — саме заради цих двох значень банк і
    ведеться.
    """
    fields = ["checked_at = %s", "checked_by = %s"]
    params = [int(time.time()), who]
    if outcome in CHECK_OUTCOMES:
        fields.append("status = %s")
        params.append(outcome)
    if note:
        fields.append("check_note = %s")
        params.append(note[:500])
    params.append(commitment_id)
    cur.execute(f"UPDATE commitments SET {', '.join(fields)} WHERE id = %s", params)
    return cur.rowcount > 0


def data_bounds(cur):
    """Межі даних: з якого місяця банк узагалі щось знає (§6).

    Виводиться в КОЖНОМУ екрані. Порожнеча має бути пояснена, інакше екран
    виглядає зламаним, а не порожнім.
    """
    cur.execute(
        "SELECT min(published), max(published), count(*) FROM ("
        "  SELECT a.published FROM promise_attempts t JOIN articles a ON a.id = t.article_id"
        "  UNION"
        "  SELECT a.published FROM commitment_revisions r JOIN articles a ON a.id = r.article_id"
        ") s")
    row = cur.fetchone() or (None, None, 0)
    return {"from": row[0], "to": row[1], "articles": row[2] or 0}


# ---------- Забути / повернути ----------

def _snapshot(cur, commitment_id):
    """Повний знімок обіцянки для журналу: сама картка, ревізії, об'єкти й
    ТЕМА. Тема тут не зайва: поодинці вона переживає видалення обіцянки, але
    масова чистка вимітає й теми, що лишились порожніми, — і тоді відкат
    вставляв би обіцянку з посиланням на неіснуючу тему."""
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c WHERE c.id = %s",
                (commitment_id,))
    rows = _rows(cur)
    if not rows:
        return None
    snapshot = rows[0]
    snapshot["amount"] = float(snapshot["amount"]) if snapshot["amount"] is not None else None
    cur.execute(f"SELECT {REVISION_COLS} FROM commitment_revisions r "
                "WHERE r.commitment_id = %s ORDER BY r.id", (commitment_id,))
    revs = [dict(zip(_REVISION_KEYS, r)) for r in cur.fetchall()]
    cur.execute("SELECT entity_id FROM commitment_objects WHERE commitment_id = %s",
                (commitment_id,))
    objects = [r[0] for r in cur.fetchall()]
    topic = None
    if snapshot.get("topic_id"):
        cur.execute("SELECT id, title, entity_ids, subject_keys, status, opened, "
                    "last_event FROM topics WHERE id = %s", (snapshot["topic_id"],))
        row = cur.fetchone()
        if row:
            topic = dict(zip(("id", "title", "entity_ids", "subject_keys",
                              "status", "opened", "last_event"), row))
    return {"commitment": snapshot, "revisions": revs, "objects": objects,
            "topic": topic}


def forget(cur, commitment_id, reason=None, who=None, run=None):
    """Прибрати помилковий запис — зі знімком у журнал (§8).

    Без знімка помилку витягу не відкотиш: правити реєстр доведеться парами
    «лінк на реальну ситуацію + правило», і кожна така правка має бути
    зворотною.

    `run` — номер прогону для масового прибирання: за ним відкочується весь
    прогін одразу (/promise_prune_undo), бо сотні знімків по одному не
    повернути.
    """
    payload = _snapshot(cur, commitment_id)
    if not payload:
        return None
    snapshot, revs = payload["commitment"], payload["revisions"]
    cur.execute(
        "INSERT INTO promise_purges (payload, reason, decided_by, created, run) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (json.dumps(payload, ensure_ascii=False, default=str), reason, who,
         int(time.time()), run))
    purge_id = cur.fetchone()[0]
    cur.execute("DELETE FROM commitments WHERE id = %s", (commitment_id,))
    return {"purge_id": purge_id, "commitment": snapshot, "revisions": len(revs)}


def restore(cur, purge_id):
    """Повернути прибрану обіцянку з тим самим id — разом із ревізіями."""
    cur.execute("SELECT payload, restored FROM promise_purges WHERE id = %s", (purge_id,))
    row = cur.fetchone()
    if not row:
        return None
    if row[1]:
        return {"already": True}
    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    c = payload["commitment"]
    # Спершу тема: масова чистка могла вимести її як порожню, і без цього
    # рядка обіцянка поверталась би з посиланням у нікуди.
    topic = payload.get("topic")
    if topic:
        tcols = [k for k in topic if k != "id"]
        cur.execute(
            f"INSERT INTO topics (id, {', '.join(tcols)}) "
            f"VALUES (%s, {', '.join(['%s'] * len(tcols))}) ON CONFLICT (id) DO NOTHING",
            [topic["id"]] + [topic[k] for k in tcols])
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('topics', 'id'), "
            "  greatest((SELECT max(id) FROM topics), 1))")
    cols = [k for k in c if k != "id"]
    cur.execute(
        f"INSERT INTO commitments (id, {', '.join(cols)}) "
        f"VALUES (%s, {', '.join(['%s'] * len(cols))}) ON CONFLICT (id) DO NOTHING",
        [c["id"]] + [c[k] for k in cols])
    for r in payload["revisions"]:
        rcols = [k for k in r if k != "id"]
        cur.execute(
            f"INSERT INTO commitment_revisions (id, {', '.join(rcols)}, dedup) "
            f"VALUES (%s, {', '.join(['%s'] * len(rcols))}, %s) "
            "ON CONFLICT (id) DO NOTHING",
            [r["id"]] + [r[k] for k in rcols]
            + [_dedup_key(r["article_id"], r["quote"], c.get("title"),
                          r.get("stated_deadline"), c.get("polarity"))])
    # Відкат злиття — це НЕ вставка рядків: ревізії нікуди не зникали, вони
    # висять на переможцеві. Вставка вище тихо нічого не зробила (id зайнятий),
    # і без цього рядка картка повернулась би порожньою, а черга далі
    # показувала б один запис замість двох.
    merged_into = payload.get("merged_into")
    if merged_into:
        ids = [r["id"] for r in payload["revisions"]]
        if ids:
            cur.execute("UPDATE commitment_revisions SET commitment_id = %s "
                        "WHERE id = ANY(%s)", (c["id"], ids))
        cur.execute("DELETE FROM commitment_objects WHERE commitment_id = %s "
                    "AND entity_id = ANY(%s)",
                    (merged_into, payload.get("objects") or []))
    for eid in payload.get("objects") or []:
        cur.execute("INSERT INTO commitment_objects (commitment_id, entity_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING", (c["id"], eid))
    cur.execute(
        "SELECT setval(pg_get_serial_sequence('commitments', 'id'), "
        "  greatest((SELECT max(id) FROM commitments), 1))")
    cur.execute(
        "SELECT setval(pg_get_serial_sequence('commitment_revisions', 'id'), "
        "  greatest((SELECT max(id) FROM commitment_revisions), 1))")
    refresh(cur, c["id"])
    if merged_into:
        refresh(cur, merged_into)   # у переможця поменшало ревізій
    cur.execute("UPDATE promise_purges SET restored = %s WHERE id = %s",
                (int(time.time()), purge_id))
    return {"commitment_id": c["id"], "revisions": len(payload["revisions"]),
            "unmerged_from": merged_into}


# ---------- Дублі: та сама обіцянка, записана двічі ----------
#
# Знайдено на першому ж місяці: «Організувати прибирання та покос трави на
# майданчику „Казка"» і «Організувати прибирання на майданчику „Казка" у
# Корабельному районі» — одна обіцянка адміністрації Корабельного району з
# одним строком, записана з двох статей. Суддя ланцюга їх не зшив, бо
# спрацював лише 63 рази на 762 статті: пре-фільтр кандидатів шукає спільну
# КАРТКУ предмета, а «майданчик „Казка"» у сутнісному шарі не завжди є.
#
# Тому другий, дешевий детектор — уже поверх записаного. Три сигнали разом, і
# кожен поодинці бреше:
#   • схожа назва (trgm) — сама по собі ловить різні обіцянки про той самий
#     об'єкт («відремонтувати» ~ «освітити» ту саму вулицю);
#   • ОДНАКОВИЙ строк — різні дії щодо одного об'єкта майже ніколи не мають
#     той самий день;
#   • спільний предмет або той самий обіцяльник.
# Зливає ЛЮДИНА: однакові назва+строк бувають і в двох сусідніх дитсадків.

_DUPE_WHERE = """
        WHERE a.status = 'expected' AND b.status = 'expected'
          AND similarity(a.title, b.title) >= %s
          AND coalesce(a.deadline, 0) = coalesce(b.deadline, 0)
          AND (a.subject_entity_id IS NOT DISTINCT FROM b.subject_entity_id
                   AND a.subject_entity_id IS NOT NULL
               OR coalesce(a.subject_key, '') = coalesce(b.subject_key, '')
                   AND coalesce(a.subject_key, '') <> ''
               OR lower(coalesce(a.owner_text, '')) = lower(coalesce(b.owner_text, ''))
                   AND coalesce(a.owner_text, '') <> '')
          AND NOT EXISTS (SELECT 1 FROM promise_pairs p
                          WHERE p.a = a.id AND p.b = b.id)
"""


def dupe_pairs(cur, limit=40, sim=DUPE_SIM):
    """Пари «схоже на один запис двічі». Read-only, нічого не зливає.

    Пари, про які людина вже сказала «різні», не повертаються ніколи.
    """
    cur.execute(
        "SELECT a.id, b.id, similarity(a.title, b.title) AS sim, "
        "       a.title, b.title, a.revisions, b.revisions, "
        "       coalesce(a.owner_text, ''), a.deadline "
        "FROM commitments a JOIN commitments b ON b.id > a.id "
        + _DUPE_WHERE + " ORDER BY sim DESC, a.id LIMIT %s", (sim, limit))
    return [{"a": a, "b": b, "sim": round(float(s), 2),
             "title_a": ta, "title_b": tb, "rev_a": ra, "rev_b": rb,
             "owner": owner, "deadline": dl}
            for a, b, s, ta, tb, ra, rb, owner, dl in cur.fetchall()]


def dupe_count(cur, sim=DUPE_SIM):
    cur.execute("SELECT count(*) FROM commitments a "
                "JOIN commitments b ON b.id > a.id" + _DUPE_WHERE, (sim,))
    return cur.fetchone()[0]


def reject_pair(cur, a, b, who=None):
    """«Ні, різні» — назавжди. Пара більше не з'явиться в жодному екрані.

    Порядок id нормалізуємо, бо в екрані пара могла приїхати будь-яким боком,
    а рішення про неї одне.
    """
    a, b = sorted((int(a), int(b)))
    if a == b:
        return False
    cur.execute("INSERT INTO promise_pairs (a, b, decided_by, created) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (a, b) DO NOTHING",
                (a, b, who, int(time.time())))
    return True


def rejected_pairs(cur, limit=50):
    """Що людина вже розвела. Потрібне, щоб рішення можна було переглянути —
    інакше помилкове «різні» ховає справжній дубль назавжди й мовчки."""
    cur.execute(
        "SELECT p.a, p.b, ca.title, cb.title, p.decided_by, p.created "
        "FROM promise_pairs p "
        "LEFT JOIN commitments ca ON ca.id = p.a "
        "LEFT JOIN commitments cb ON cb.id = p.b "
        "ORDER BY p.created DESC LIMIT %s", (limit,))
    return [{"a": a, "b": b, "title_a": ta, "title_b": tb,
             "by": by, "created": ts}
            for a, b, ta, tb, by, ts in cur.fetchall()]


def unreject_pair(cur, a, b):
    a, b = sorted((int(a), int(b)))
    cur.execute("DELETE FROM promise_pairs WHERE a = %s AND b = %s", (a, b))
    return cur.rowcount > 0


# ---------- Назва, за якою обіцянку не впізнати ----------
#
# Друга за частотою вада витягу після ієрогліфа, і значно шкідливіша:
# «Винести проєкт рішення про виділення землі на розгляд депутатів повторно» —
# а йшлося про землю ПІД МОДУЛЬНІ БУДИНКИ ДЛЯ ПЕРЕСЕЛЕНЦІВ, і саме заради
# цього слова новина існувала. Місто виділяє землю щотижня, тож десяток таких
# назв в одному реєстрі не розрізнити ніяк.
#
# Заразом це годує детектор дублів фальшивими парами: «…для земельної
# ділянки» (ритуальна служба) і «…для розміщення модульних будинків» (КП «Свій
# дім») схожі рівно тому, що з обох викинули предмет.
#
# Ознака конкретності рахується механічно: цифра, назва в лапках або власна
# назва (слово з великої не на початку — топонім, установа, прізвище). Це
# ПІДКАЗКА, а не вирок: правильна назва без жодного з трьох сигналів теж
# буває, тому команда лише показує список.

_VAGUE_HINT = ("проєкт рішення", "питання", "документаці", "заходи",
               "земельн", "земл", "об'єкт", "територі", "приміщенн",
               "робіт", "послуг", "інфраструктур", "мереж", "громад")


def _title_is_specific(title):
    t = (title or "").strip()
    if not t:
        return True
    if any(ch.isdigit() for ch in t):
        return True
    if "«" in t or '"' in t:
        return True
    return any(w[:1].isupper() for w in t.split()[1:])


def vague_titles(cur, limit=120):
    """Назви, за якими обіцянку не впізнати. Read-only."""
    cur.execute(
        "SELECT c.id, c.title, c.subject, "
        "  (SELECT r.quote FROM commitment_revisions r "
        "    WHERE r.commitment_id = c.id ORDER BY r.id LIMIT 1), "
        "  (SELECT r.article_id FROM commitment_revisions r "
        "    WHERE r.commitment_id = c.id ORDER BY r.id LIMIT 1) "
        "FROM commitments c WHERE c.status = 'expected' ORDER BY c.id")
    out = []
    for cid, title, subject, quote, aid in cur.fetchall():
        if _title_is_specific(title):
            continue
        low = (title or "").lower()
        if not any(w in low for w in _VAGUE_HINT):
            continue
        out.append({"id": cid, "title": title, "subject": subject,
                    "quote": quote, "article_id": aid})
        if len(out) >= limit:
            break
    return out


def glitched(cur, limit=200):
    """Записи з ієрогліфом — разова прибиральня накопиченого.

    Повертає і саму статтю, бо цитату лікуємо ЇЇ текстом, а не моделлю.
    """
    cur.execute(
        "SELECT c.id, c.title, r.id, r.quote, r.article_id, a.text_ua, a.text_ru "
        "FROM commitments c JOIN commitment_revisions r ON r.commitment_id = c.id "
        "LEFT JOIN articles a ON a.id = r.article_id ORDER BY c.id LIMIT %s",
        (limit * 20,))
    out = []
    for cid, title, rid, quote, aid, tua, tru in cur.fetchall():
        if not (has_glitch(title) or has_glitch(quote)):
            continue
        fixed_q = None
        if has_glitch(quote):
            fixed_q = repair_quote(quote, tua, tru) or find_verbatim(quote, tua, tru)
        out.append({"commitment_id": cid, "revision_id": rid, "article_id": aid,
                    "title": title, "quote": quote, "fixed_quote": fixed_q,
                    "title_broken": has_glitch(title)})
        if len(out) >= limit:
            break
    return out


def apply_glitch_fix(cur, revision_id=None, quote=None,
                     commitment_id=None, title=None):
    """Записати полагоджений текст. Дві різні речі свідомо в одній функції:
    цитата лікується автоматично з тексту статті, назва — тільки людиною."""
    done = []
    if revision_id and quote:
        cur.execute("UPDATE commitment_revisions SET quote = %s, quote_verified = true "
                    "WHERE id = %s", (quote, revision_id))
        done.append("quote")
    if commitment_id and title:
        cur.execute("UPDATE commitments SET title = %s WHERE id = %s",
                    (title[:300], commitment_id))
        done.append("title")
    return done


def export_all(cur):
    """Увесь банк одним списком — для розбору ПОЗА ботом.

    Той самий підхід, що `/roles_audit` і `/entity_junk`: коли рішень сотні,
    їх не натиснути по одному, і файл виявляється швидшим за будь-який
    інтерфейс. Тут — повний зріз: обіцянка, її ревізії з цитатами й лінками,
    похідні поля. Достатньо, щоб знайти дублі й сміття, не заглядаючи в базу.
    """
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c ORDER BY c.id")
    rows = _decorate(_rows(cur))
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["revisions_list"] = []
    cur.execute(
        f"SELECT {REVISION_COLS}, coalesce(a.published, r.created) AS published, "
        "a.slug, a.category, a.kind, coalesce(a.title_ua, a.title_ru) AS art_title "
        "FROM commitment_revisions r LEFT JOIN articles a ON a.id = r.article_id "
        "ORDER BY r.commitment_id, r.id")
    keys = _REVISION_KEYS + ["published", "slug", "category", "kind", "art_title"]
    for row in cur.fetchall():
        rev = dict(zip(keys, row))
        target = by_id.get(rev.pop("commitment_id"))
        if target is not None:
            target["revisions_list"].append(rev)
    return rows


def merge_commitments(cur, keep_id, dup_id, who=None, run=None):
    """Звести два записи в один: ревізії другого стають ревізіями першого.

    Це не видалення дубля, а СКЛЕЮВАННЯ ланцюга — рівно те, чого не зробив
    суддя. Обидві цитати лишаються доказами, просто тепер вони в одній
    картці, і в черзі стоїть один рядок замість двох.

    Знімок лягає в той самий журнал, але з міткою `merged_into`: відкат тут
    не «вставити рядки назад» (вони нікуди не зникали), а «перевісити ревізії
    на попередню картку», і restore розрізняє ці два випадки.
    """
    keep_id, dup_id = int(keep_id), int(dup_id)
    if keep_id == dup_id:
        return None
    payload = _snapshot(cur, dup_id)
    if not payload or not _snapshot(cur, keep_id):
        return None
    payload["merged_into"] = keep_id
    cur.execute(
        "INSERT INTO promise_purges (payload, reason, decided_by, created, run) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (json.dumps(payload, ensure_ascii=False, default=str),
         f"дубль → {keep_id}", who, int(time.time()), run or next_run(cur)))
    purge_id = cur.fetchone()[0]
    cur.execute("UPDATE commitment_revisions SET commitment_id = %s "
                "WHERE commitment_id = %s", (keep_id, dup_id))
    cur.execute("INSERT INTO commitment_objects (commitment_id, entity_id) "
                "SELECT %s, entity_id FROM commitment_objects "
                "WHERE commitment_id = %s ON CONFLICT DO NOTHING",
                (keep_id, dup_id))
    topic_id = payload["commitment"].get("topic_id")
    cur.execute("DELETE FROM commitments WHERE id = %s", (dup_id,))
    if topic_id:
        prune_orphan_topics(cur, [topic_id])
    refresh(cur, keep_id)
    return {"purge_id": purge_id, "keep": keep_id, "dropped": dup_id,
            "revisions": len(payload["revisions"])}


# ---------- Чистка банку від немиколаївських обіцянок ----------
#
# Перший прогін місяця пішов по ВСІХ статтях нори, і банк набрався
# загальнонаціональним: Зеленський, Укрзалізниця, Уряд України, вокзал Одеси.
# Редакція такі обіцянки не перевіряє, а в черзі вони витісняють миколаївські.
# Фільтр на вході вже стоїть (api.REGION_MYKOLAIV), тут прибираємо набране.
#
# Правило прибирання СВІДОМО вужче за «стаття не миколаївська»:
#   • беремо лише те, про що ТОЧНО знаємо, що воно поза Миколаєвом — стаття є
#     в норі й у неї проставлений інший регіон. Порожній регіон означає «не
#     знаємо», і на «не знаємо» нічого не видаляється;
#   • обіцянка, у якої хоч ОДНА ревізія приїхала з миколаївської статті,
#     лишається цілою. Ланцюг міг зшитись через дві статті, і викинути його
#     означало б втратити миколаївський факт заради немиколаївського.

_OUTSIDE = ("EXISTS (SELECT 1 FROM commitment_revisions r "
            "        JOIN articles a ON a.id = r.article_id "
            "       WHERE r.commitment_id = c.id "
            "         AND a.region IS NOT NULL AND a.region <> %s)")
_INSIDE = ("EXISTS (SELECT 1 FROM commitment_revisions r "
           "        JOIN articles a ON a.id = r.article_id "
           "       WHERE r.commitment_id = c.id AND a.region = %s)")


def prune_scan(cur, region, limit=12):
    """Що зніме чистка: кількість, приклади і — окремо — чого вона НЕ чіпає."""
    cur.execute(f"SELECT count(*) FROM commitments c "
                f"WHERE {_OUTSIDE} AND NOT {_INSIDE}", (region, region))
    total = cur.fetchone()[0]
    cur.execute(f"SELECT count(*) FROM commitments c "
                f"WHERE {_OUTSIDE} AND {_INSIDE}", (region, region))
    mixed = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*) FROM commitments c WHERE NOT EXISTS ("
        "  SELECT 1 FROM commitment_revisions r JOIN articles a ON a.id = r.article_id"
        "   WHERE r.commitment_id = c.id AND a.region IS NOT NULL)")
    unknown = cur.fetchone()[0]
    cur.execute(
        f"SELECT c.id, c.title, coalesce(r.promiser_text, c.owner_text) "
        f"FROM commitments c "
        f"LEFT JOIN LATERAL (SELECT promiser_text FROM commitment_revisions "
        f"                   WHERE commitment_id = c.id ORDER BY id LIMIT 1) r ON true "
        f"WHERE {_OUTSIDE} AND NOT {_INSIDE} "
        f"ORDER BY c.revisions DESC, c.id LIMIT %s", (region, region, limit))
    sample = [{"id": i, "title": t, "promiser": p} for i, t, p in cur.fetchall()]
    cur.execute(
        f"SELECT coalesce(r.promiser_text, c.owner_text) AS who, count(*) "
        f"FROM commitments c "
        f"LEFT JOIN LATERAL (SELECT promiser_text FROM commitment_revisions "
        f"                   WHERE commitment_id = c.id ORDER BY id LIMIT 1) r ON true "
        f"WHERE {_OUTSIDE} AND NOT {_INSIDE} "
        f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 8", (region, region))
    promisers = [(w, n) for w, n in cur.fetchall() if w]
    return {"total": total, "mixed": mixed, "unknown": unknown,
            "sample": sample, "promisers": promisers, "region": region}


def next_run(cur):
    """Номер наступного прогону прибирання."""
    cur.execute("SELECT nextval('promise_purge_run_seq')")
    return cur.fetchone()[0]


def prune(cur, region, reason=None, who=None, run=None):
    """Прибрати немиколаївські обіцянки — кожну зі знімком у журнал."""
    run = run or next_run(cur)
    cur.execute(f"SELECT c.id, c.topic_id FROM commitments c "
                f"WHERE {_OUTSIDE} AND NOT {_INSIDE} ORDER BY c.id",
                (region, region))
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    touched = sorted({r[1] for r in rows if r[1]})
    done = 0
    for cid in ids:
        if forget(cur, cid, reason=reason, who=who, run=run):
            done += 1
    topics = prune_orphan_topics(cur, touched)
    return {"run": run, "removed": done, "topics": topics}


def prune_orphan_topics(cur, topic_ids):
    """Теми, у яких після чистки не лишилось жодного зобов'язання.

    Лише СЕРЕД зачеплених тем: знімок кожної з них уже лежить у журналі разом
    зі своєю обіцянкою, тож відкат поверне і їх. Порожні теми з інших причин
    не чіпаємо взагалі — за ними знімка немає, і видалення було б незворотним.
    """
    if not topic_ids:
        return 0
    cur.execute("DELETE FROM topics t WHERE t.id = ANY(%s) AND NOT EXISTS ("
                "  SELECT 1 FROM commitments c WHERE c.topic_id = t.id)",
                (list(topic_ids),))
    return cur.rowcount or 0


def prune_undo(cur, run):
    """Відкотити ВЕСЬ прогін чистки. Порядок — зворотний до прибирання."""
    cur.execute("SELECT id FROM promise_purges WHERE run = %s AND restored IS NULL "
                "ORDER BY id DESC", (run,))
    ids = [r[0] for r in cur.fetchall()]
    back = sum(1 for pid in ids if (restore(cur, pid) or {}).get("commitment_id"))
    return {"run": run, "restored": back, "snapshots": len(ids)}


# ---------- Друга вісь чистки: ОБІЦЯЛЬНИК ----------
#
# Регіон ловить не все. Стаття може лежати в рубриці «Миколаїв» (бо там є
# місцева реакція чи місцевий бек), а зобов'язання в ній — загальнонаціональне:
# «Зеленський пообіцяв виплати всім ВПО». Редакція не перевірятиме його
# незалежно від рубрики, тож регіоном таке не приберемо ніколи.
#
# Робочий сигнал тут — сам АКТОР. Але вирішувати, чи «Укрзалізниця» нам чужа,
# машина не може: завтра вона обіцятиме приміський поїзд на Миколаїв. Тому бот
# лише НАЗИВАЄ кандидатів (хто найчастіше обіцяє в банку) і показує, що саме
# зникне, а команду дає людина.

_WHO_EXPR = ("coalesce((SELECT promiser_text FROM commitment_revisions "
             "          WHERE commitment_id = c.id AND promiser_text IS NOT NULL "
             "          ORDER BY id LIMIT 1), c.owner_text)")


def top_promisers(cur, limit=10, region=None):
    """Хто найбільше обіцяє в банку. З `region` — лише по статтях цього
    регіону, тобто «хто лишиться після чистки за регіоном»."""
    where = ""
    params = []
    if region is not None:
        where = (f"WHERE {_INSIDE}")
        params.append(region)
    cur.execute(
        f"SELECT {_WHO_EXPR} AS who, count(*) FROM commitments c {where} "
        "GROUP BY 1 HAVING count(*) > 0 ORDER BY 2 DESC NULLS LAST LIMIT %s",
        params + [limit])
    return [(w, n) for w, n in cur.fetchall() if w]


def prune_who_scan(cur, who, region=None, limit=10):
    """Що знесе чистка за обіцяльником — і скільки з цього МІСЦЕВЕ.

    Збіг за підрядком, бо в банку одна людина живе кількома написаннями
    («Володимир Зеленський», «Зеленський», «президент Зеленський»). Саме тому
    прев'ю показує написання окремо: підрядок може захопити зайве, і побачити
    це треба до видалення, а не після.
    """
    like = f"%{who.strip()}%"
    match = ("(EXISTS (SELECT 1 FROM commitment_revisions r "
             "         WHERE r.commitment_id = c.id AND r.promiser_text ILIKE %s) "
             " OR c.owner_text ILIKE %s)")
    cur.execute(f"SELECT count(*) FROM commitments c WHERE {match}", (like, like))
    total = cur.fetchone()[0]
    local = 0
    if region is not None:
        cur.execute(f"SELECT count(*) FROM commitments c "
                    f"WHERE {match} AND {_INSIDE}", (like, like, region))
        local = cur.fetchone()[0]
    cur.execute(
        f"SELECT {_WHO_EXPR} AS who, count(*) FROM commitments c WHERE {match} "
        "GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT %s", (like, like, limit))
    writings = [(w, n) for w, n in cur.fetchall() if w]
    cur.execute(
        f"SELECT c.id, c.title, {_WHO_EXPR} FROM commitments c WHERE {match} "
        "ORDER BY c.revisions DESC, c.id LIMIT %s", (like, like, limit))
    sample = [{"id": i, "title": t, "promiser": p} for i, t, p in cur.fetchall()]
    return {"who": who.strip(), "total": total, "local": local,
            "writings": writings, "sample": sample, "region": region}


def prune_who(cur, who, reason=None, decided_by=None, run=None):
    """Прибрати всі обіцянки цього обіцяльника — кожну зі знімком у журнал."""
    like = f"%{who.strip()}%"
    run = run or next_run(cur)
    cur.execute(
        "SELECT c.id, c.topic_id FROM commitments c WHERE "
        "(EXISTS (SELECT 1 FROM commitment_revisions r "
        "         WHERE r.commitment_id = c.id AND r.promiser_text ILIKE %s) "
        " OR c.owner_text ILIKE %s) ORDER BY c.id", (like, like))
    rows = cur.fetchall()
    touched = sorted({r[1] for r in rows if r[1]})
    done = sum(1 for cid, _ in rows
               if forget(cur, cid, reason=reason or f"не наш обіцяльник: {who}",
                         who=decided_by, run=run))
    return {"run": run, "removed": done,
            "topics": prune_orphan_topics(cur, touched)}


def purge_runs(cur, limit=10):
    """Останні прогони масового прибирання — для /promise_prune_undo без
    аргументу: номер прогону це unix-час, руками його не згадаєш."""
    cur.execute(
        "SELECT run, count(*), count(restored), min(created), min(reason) "
        "FROM promise_purges WHERE run IS NOT NULL "
        "GROUP BY run ORDER BY run DESC LIMIT %s", (limit,))
    return [{"run": r, "n": n, "restored": rs, "created": c, "reason": why}
            for r, n, rs, c, why in cur.fetchall()]


def drop_article(cur, article_id):
    """Зняти все, що записано з цієї статті — для перечитування після правки
    промпту (`/promise_retest`).

    Обіцянки, які лишились без жодної ревізії, видаляються: інакше в банку
    висіли б картки без єдиного доказу, а правило §3 «немає цитати — немає
    обіцянки» діяло б лише на вході.
    """
    cur.execute("SELECT DISTINCT commitment_id FROM commitment_revisions "
                "WHERE article_id = %s", (article_id,))
    touched = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM commitment_revisions WHERE article_id = %s", (article_id,))
    orphans = []
    for cid in touched:
        cur.execute("SELECT count(*) FROM commitment_revisions WHERE commitment_id = %s",
                    (cid,))
        if cur.fetchone()[0] == 0:
            cur.execute("DELETE FROM commitments WHERE id = %s", (cid,))
            orphans.append(cid)
        else:
            refresh(cur, cid)
    cur.execute("DELETE FROM promise_attempts WHERE article_id = %s", (article_id,))
    return {"touched": touched, "removed": orphans}
