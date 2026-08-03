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
        checked = row.get("checked_at") or 0
        if now > due and checked < int(deadline):
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


def prepare(cur, item):
    """Сирий запис витягу → резолвлені id + похідні поля.

    Нічого не пише. Повертає dict, придатний і для пошуку кандидатів, і для
    вставки, і для показу в /promise_test (де в базу не йде взагалі нічого).
    """
    out = dict(item)
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
        " link_confidence, dedup, created) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (dedup) DO NOTHING",
        (commitment_id, article["id"], prepared.get("deadline"),
         prepared.get("deadline_precision"), prepared.get("modality"),
         prepared.get("source_type"), prepared.get("promiser_entity_id"),
         prepared.get("promiser"), prepared.get("promiser_role"),
         prepared.get("reported_by_entity_id"), prepared.get("reported_by"),
         prepared.get("condition"), prepared.get("trigger_event"), quote[:1000],
         link_confidence,
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
    c.revisions, c.first_seen, c.last_seen, c.checked_at, c.checked_by
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


def list_queue(cur, cls=None, limit=20, now=None):
    """Черга банку тем: що горить сьогодні. Порядок — той самий, що стане
    головним екраном апки (§6): спершу те, що горить, а не алфавіт."""
    cur.execute(f"SELECT {COMMITMENT_COLS} FROM commitments c "
                "WHERE c.status = 'expected'")
    rows = _decorate(_rows(cur), now)
    if cls:
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


def mark_checked(cur, commitment_id, who):
    cur.execute("UPDATE commitments SET checked_at = %s, checked_by = %s WHERE id = %s",
                (int(time.time()), who, commitment_id))


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

def forget(cur, commitment_id, reason=None, who=None):
    """Прибрати помилковий запис — зі знімком у журнал (§8).

    Без знімка помилку витягу не відкотиш: правити реєстр доведеться парами
    «лінк на реальну ситуацію + правило», і кожна така правка має бути
    зворотною.
    """
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
    payload = {"commitment": snapshot, "revisions": revs, "objects": objects}
    cur.execute(
        "INSERT INTO promise_purges (payload, reason, decided_by, created) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (json.dumps(payload, ensure_ascii=False, default=str), reason, who,
         int(time.time())))
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
    cur.execute("UPDATE promise_purges SET restored = %s WHERE id = %s",
                (int(time.time()), purge_id))
    return {"commitment_id": c["id"], "revisions": len(payload["revisions"])}


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
