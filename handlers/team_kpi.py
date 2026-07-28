"""
Рекурентні KPI команди — модуль Mini App «Команда» (концепція v2, крок 2).

Модель (рішення Олега 27.07, «відділовий дефолт + персональні перекриття»):
- **Норма** задається НА ВІДДІЛ: метрика (news/article) × період (week/month) ×
  ціль на людину. Одна цифра на відділ — Катя не адмініструє 17 окремих норм.
- **Облік ведеться ПО ЛЮДИНІ**: факт кожної людини рахується сам із БД сайту
  (nodes по owner_id, матч людини до users нормалізованим ПІБ — той самий
  механізм, що фото в team_projects). Журналістка нічого не звітує.
- **Перекриття (override)** — точкова правка інстансу «людина × норма × період»:
  інша ціль або 0 (= звільнена: відпустка, відрядження — щоб не рахувалось
  невиконанням), з нотаткою. Правка живе тільки в тому періоді, на який
  поставлена; наступний тиждень/місяць знову бере відділовий дефолт.

Періоди — за Києвом: тиждень від понеділка, місяць календарний. Факт — лише
опубліковане (status=1) і published <= now (гейт відкладених постів, див.
BUILDER_MONITOR_MODULE.md).

Таблиці (Нора): team_kpi_norms, team_kpi_overrides. Факти з MySQL сайту
кешуються на 5 хв (ліміти БД сайту); без DB_* факт = None, апка показує «—».
"""

import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import bot_db, db, team_roster
from handlers.team_projects import _norm_name

KYIV_TZ = ZoneInfo("Europe/Kiev")

KPI_METRICS = ("news", "article")
KPI_PERIODS = ("week", "month")

MONTHS_UA = ["січень", "лютий", "березень", "квітень", "травень", "червень",
             "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
MONTHS_UA_SHORT = ["січ", "лют", "бер", "кві", "тра", "чер",
                   "лип", "сер", "вер", "жов", "лис", "гру"]

# Підпис норми людською мовою. Раніше збиралось як f"{target} {metric}" і
# давало «60 news власних» — англійське слово в українському інтерфейсі, яке
# тепер бачать і журналістки у своїй помісячній динаміці.
_METRIC_WORDS = {
    "news": ("новина", "новини", "новин"),
    "article": ("стаття", "статті", "статей"),
}
_OWN_WORDS = ("власна", "власні", "власних")


def _plural_index(n):
    """0 — одна, 1 — дві-чотири, 2 — п'ять і більше (українські форми)."""
    n = abs(int(n))
    if n % 100 in range(11, 15):
        return 2
    if n % 10 == 1:
        return 0
    if n % 10 in (2, 3, 4):
        return 1
    return 2


def norm_label(metric, target, own):
    """«60 власних новин», «1 стаття», «3 новини»."""
    i = _plural_index(target)
    word = _METRIC_WORDS.get(metric, ("матеріал", "матеріали", "матеріалів"))[i]
    return f"{target} {_OWN_WORDS[i] + ' ' if own else ''}{word}"

FACT_CACHE_TTL = 300
_fact_cache = {}  # (metric, period_start_iso) -> (expires, {person: count})
_users_cache = {"at": 0.0, "map": {}}  # norm_name -> users.id

_SCHEMA_STATEMENTS = [
    # Пін правильного users.id за людиною — перебиває пошук за ПІБ. Потрібен,
    # коли в users кілька записів з тим самим іменем (звільнена + нинішня):
    # пошук за іменем брав би перший (найстаріший) id, і факт рахувався б під
    # порожнім акаунтом. /kpi_link виправляє це без деплою.
    """
    CREATE TABLE IF NOT EXISTS team_user_link (
        person       TEXT PRIMARY KEY,
        site_user_id BIGINT NOT NULL,
        updated_by   TEXT,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_kpi_norms (
        id         BIGSERIAL PRIMARY KEY,
        dept       TEXT NOT NULL,
        metric     TEXT NOT NULL,
        period     TEXT NOT NULL,
        target     SMALLINT NOT NULL,
        own        BOOLEAN NOT NULL DEFAULT FALSE,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Ідемпотентна міграція: прапорець «лише власні» (own_material) — 27.07,
    # Creative рахує власні матеріали, Newsroom — усі
    "ALTER TABLE team_kpi_norms ADD COLUMN IF NOT EXISTS own BOOLEAN NOT NULL DEFAULT FALSE",
    """
    CREATE TABLE IF NOT EXISTS team_kpi_overrides (
        norm_id      BIGINT NOT NULL,
        person       TEXT NOT NULL,
        period_start DATE NOT NULL,
        target       SMALLINT NOT NULL,
        note         TEXT,
        created_by   TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (norm_id, person, period_start)
    )
    """,
    # Міграція 27.07: відділи зведено до реальної структури (Creative /
    # Newsroom / digital) — норми зі старими слагами переносяться (ідемпотентно).
    "UPDATE team_kpi_norms SET dept = 'newsroom' "
    "WHERE dept IN ('журналістика', 'стрічка', 'переклад')",
    "UPDATE team_kpi_norms SET dept = 'digital' WHERE dept IN ('соцмережі', 'відео')",
]

# Відсутності: відпустка / лікарняна / відрядження. Живуть окремо від правок
# KPI, бо це факт про ЛЮДИНУ, а не про конкретну норму: одна відпустка має
# пригасити всі її норми — і тижневі, і місячні, і в усіх періодах, яких вона
# торкається. Правка (team_kpi_overrides) лишається ручним «я так вирішила»
# і має пріоритет над авто-перерахунком.
_SCHEMA_STATEMENTS.append("""
    CREATE TABLE IF NOT EXISTS team_absences (
        id         BIGSERIAL PRIMARY KEY,
        person     TEXT NOT NULL,
        start_date DATE NOT NULL,
        end_date   DATE NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'vacation',
        note       TEXT,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
""")
_SCHEMA_STATEMENTS.append(
    "CREATE INDEX IF NOT EXISTS idx_team_absences_person "
    "ON team_absences (person, start_date)")

# Скільки новин «важить» одна стаття у нормі на новини (Олег, 28.07).
# Причина: норма є лише на новини, а стаття коштує в рази дорожче за часом.
# Людям дозволено забивати на новини, коли вони в статті — але кружечок
# показував провал, і це демотивувало. Тепер стаття зараховується в норму
# новин із вагою ×3, тож робота видно, а не «нічого не зробила».
ARTICLE_WEIGHT = 3

ABSENCE_KINDS = ("vacation", "sick", "trip")
ABSENCE_TITLES = {"vacation": "відпустка", "sick": "лікарняна", "trip": "відрядження"}

_schema_lock = threading.Lock()
_schema_done = False


def ensure_kpi_schema():
    """Під блокуванням і в одній сесії — з тих самих причин, що
    team_tasks.ensure_team_schema (гонка перших паралельних запитів +
    з'єднання на кожен statement)."""
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        with bot_db.session():
            for sql in _SCHEMA_STATEMENTS:
                bot_db.execute(sql)
        _schema_done = True


# ---------- Періоди (Київ) ----------

def period_bounds(period, offset=0, today=None):
    """(start_date, end_date_exclusive) періоду за Києвом. offset — зсув
    у періодах назад/вперед (0 — поточний, -1 — попередній тиждень/місяць)."""
    today = today or datetime.now(KYIV_TZ).date()
    if period == "week":
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        return start, start + timedelta(days=7)
    # місяць: зсуваємо по індексу (year*12 + month), щоб не ловити переходи року
    idx = today.year * 12 + (today.month - 1) + offset
    start = date(idx // 12, idx % 12 + 1, 1)
    return start, (start + timedelta(days=32)).replace(day=1)


def period_label(period, offset=0, today=None):
    start, end = period_bounds(period, offset, today)
    if period == "week":
        last = end - timedelta(days=1)
        return f"{start.day:02d}–{last.day:02d}.{last.month:02d}"
    return f"{MONTHS_UA[start.month - 1]} {start.year}"


def _period_ts_range(period, offset=0):
    """(unix_start, unix_end) періоду — межі за київською північчю."""
    start, end = period_bounds(period, offset)
    to_ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=KYIV_TZ).timestamp())
    return to_ts(start), to_ts(end)


# ---------- Норми (CRUD) ----------

def _row_to_norm(r):
    return {"id": r["id"], "dept": r["dept"], "metric": r["metric"],
            "period": r["period"], "target": r["target"], "own": r["own"]}


def list_norms():
    ensure_kpi_schema()
    return [_row_to_norm(r) for r in bot_db.query(
        "SELECT * FROM team_kpi_norms ORDER BY dept, period, metric")]


def add_norm(creator, dept, metric, period, target, own=False):
    ensure_kpi_schema()
    rows = bot_db.query(
        "INSERT INTO team_kpi_norms (dept, metric, period, target, own, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (dept, metric, period, int(target), bool(own), creator),
    )
    return _row_to_norm(rows[0])


def update_norm(norm_id, target):
    ensure_kpi_schema()
    rows = bot_db.query(
        "UPDATE team_kpi_norms SET target = %s WHERE id = %s RETURNING *",
        (int(target), int(norm_id)),
    )
    return _row_to_norm(rows[0]) if rows else None


def delete_norm(norm_id):
    """Видаляє норму разом з її правками. Історію факту не чіпає — факт
    рахується з nodes і нікуди не зникає."""
    ensure_kpi_schema()
    bot_db.execute("DELETE FROM team_kpi_overrides WHERE norm_id = %s", (int(norm_id),))
    return bot_db.execute("DELETE FROM team_kpi_norms WHERE id = %s", (int(norm_id),))


# ---------- Правки по людині ----------

def set_override(creator, norm_id, person, target, note=None):
    """Ставить/оновлює правку людини на ПОТОЧНИЙ період норми.
    target=0 — звільнена цього періоду (відпустка/відрядження)."""
    ensure_kpi_schema()
    norm = next((n for n in list_norms() if n["id"] == int(norm_id)), None)
    if not norm:
        return None
    start, _ = period_bounds(norm["period"])
    bot_db.execute(
        """
        INSERT INTO team_kpi_overrides (norm_id, person, period_start, target, note, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (norm_id, person, period_start) DO UPDATE SET
            target = EXCLUDED.target, note = EXCLUDED.note,
            created_by = EXCLUDED.created_by, created_at = now()
        """,
        (int(norm_id), person, start, int(target), (note or "").strip() or None, creator),
    )
    return True


def clear_override(norm_id, person):
    """Прибирає правку поточного періоду — людина повертається на дефолт відділу."""
    ensure_kpi_schema()
    norm = next((n for n in list_norms() if n["id"] == int(norm_id)), None)
    if not norm:
        return 0
    start, _ = period_bounds(norm["period"])
    return bot_db.execute(
        "DELETE FROM team_kpi_overrides WHERE norm_id = %s AND person = %s AND period_start = %s",
        (int(norm_id), person, start),
    )


# ---------- Відсутності (відпустка/лікарняна/відрядження) ----------

def _row_to_absence(r):
    return {"id": r["id"], "person": r["person"],
            "start": r["start_date"].isoformat(), "end": r["end_date"].isoformat(),
            "kind": r["kind"], "title": ABSENCE_TITLES.get(r["kind"], r["kind"]),
            "note": r["note"] or "", "created_by": r["created_by"]}


def list_absences(person=None):
    ensure_kpi_schema()
    sql = ("SELECT id, person, start_date, end_date, kind, note, created_by "
           "FROM team_absences")
    params = []
    if person:
        sql += " WHERE person = %s"
        params.append(person)
    sql += " ORDER BY start_date DESC, id DESC"
    return [_row_to_absence(r) for r in bot_db.query(sql, params)]


def add_absence(creator, person, start, end, kind="vacation", note=None):
    ensure_kpi_schema()
    if kind not in ABSENCE_KINDS:
        kind = "vacation"
    if end < start:
        start, end = end, start
    rows = bot_db.query(
        "INSERT INTO team_absences (person, start_date, end_date, kind, note, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "RETURNING id, person, start_date, end_date, kind, note, created_by",
        (person, start, end, kind, (note or "").strip() or None, creator),
    )
    return _row_to_absence(rows[0])


def delete_absence(absence_id):
    ensure_kpi_schema()
    return bot_db.execute("DELETE FROM team_absences WHERE id = %s", (int(absence_id),))


def _workdays(start, end_exclusive):
    """Робочих днів (пн–пт) у [start, end). Вихідні не рахуємо: норма — про
    робочий вихід, і тиждень відпустки пн–пт має занулити тижневу ціль, а не
    лишити «дві сьомих»."""
    days = 0
    day = start
    while day < end_exclusive:
        if day.weekday() < 5:
            days += 1
        day += timedelta(days=1)
    return days


def absence_factor(person, start, end_exclusive, absences=None):
    """(частка періоду, яку людина працювала; скільки робочих днів пропущено).

    Ціль масштабується пропорційно: відпустка 8–17 липня з'їдає 8 робочих днів
    із 23 → ціль множиться на 15/23. Повністю пропущений період → 0.0, і
    людина просто випадає зі звіту, як звільнена правкою."""
    absences = list_absences(person) if absences is None else absences
    total = _workdays(start, end_exclusive)
    if not total:
        return 1.0, 0
    missed = 0
    for a in absences:
        a_start = date.fromisoformat(a["start"])
        a_end = date.fromisoformat(a["end"]) + timedelta(days=1)   # включно
        lo, hi = max(start, a_start), min(end_exclusive, a_end)
        if lo < hi:
            missed += _workdays(lo, hi)
    missed = min(missed, total)
    return (total - missed) / total, missed


def _absences_by_person(people=None):
    """{людина: [відсутності]} — одним запитом на весь дашборд."""
    out = {}
    for a in list_absences():
        if people is None or a["person"] in people:
            out.setdefault(a["person"], []).append(a)
    return out


def scaled_target(base_target, factor):
    """Ціль із урахуванням відсутності: округлюємо вниз, але не нижче 1, поки
    людина хоч скількись працювала — інакше тиждень із одним робочим днем
    перетворювався б на «0 із 0» і виглядав як виконаний."""
    if factor >= 1:
        return base_target
    if factor <= 0:
        return 0
    return max(1, int(base_target * factor))


def weighted_facts(metric, period, own=False, offset=0):
    """({людина: зважений факт}, {людина: скільки статей}) для норми.

    Для норми на НОВИНИ додаємо статті з вагою ARTICLE_WEIGHT: одна стаття
    закриває три новини. Для решти норм — звичайний факт і порожній бонус.
    Обидва запити кешовані в _fact_cache, тож зайвого походу в БД сайту немає."""
    base = fact_counts(metric, period, own, offset)
    if metric != "news" or base is None:
        return base, {}
    articles = fact_counts("article", period, own, offset) or {}
    out, bonus = {}, {}
    for person, value in base.items():
        count = articles.get(person) or 0
        bonus[person] = count
        out[person] = None if value is None else value + count * ARTICLE_WEIGHT
    return out, bonus


def _overrides_for(norm_id, period, offset=0):
    start, _ = period_bounds(period, offset)
    return {
        r["person"]: {"target": r["target"], "note": r["note"]}
        for r in bot_db.query(
            "SELECT person, target, note FROM team_kpi_overrides "
            "WHERE norm_id = %s AND period_start = %s",
            (int(norm_id), start),
        )
    }


# ---------- Факт із БД сайту ----------

def _user_id_map():
    """{нормалізоване ПІБ: users.id} з БД сайту, кеш 10 хв."""
    if not db.is_configured():
        return {}
    if time.monotonic() - _users_cache["at"] < 600 and _users_cache["map"]:
        return _users_cache["map"]
    rows = db.query("SELECT id, first_name, last_name FROM users")
    mapping = {}
    for r in rows:
        first = (r["first_name"] or "").strip()
        last = (r["last_name"] or "").strip()
        if not (first or last):
            continue
        mapping.setdefault(_norm_name(f"{first} {last}"), r["id"])
        mapping.setdefault(_norm_name(f"{last} {first}"), r["id"])
    _users_cache["at"] = time.monotonic()
    _users_cache["map"] = mapping
    return mapping


def get_user_links():
    """{людина: закріплений users.id} з Нори (перебиває пошук за ПІБ)."""
    ensure_kpi_schema()
    return {r["person"]: r["site_user_id"]
            for r in bot_db.query("SELECT person, site_user_id FROM team_user_link")}


def set_user_link(person, site_user_id, updated_by=None):
    ensure_kpi_schema()
    bot_db.execute(
        "INSERT INTO team_user_link (person, site_user_id, updated_by, updated_at) "
        "VALUES (%s, %s, %s, now()) ON CONFLICT (person) DO UPDATE SET "
        "site_user_id = EXCLUDED.site_user_id, updated_by = EXCLUDED.updated_by, updated_at = now()",
        (person, int(site_user_id), updated_by),
    )


def resolve_site_user_id(person, links=None, name_map=None):
    """users.id людини: закріплений пін (team_user_link) → пошук за ПІБ.
    links/name_map — вже прочитані, щоб не смикати БД у циклі."""
    links = links if links is not None else get_user_links()
    if person in links:
        return links[person]
    name_map = name_map if name_map is not None else _user_id_map()
    return name_map.get(_norm_name(person))


def fact_counts(metric, period, own=False, offset=0):
    """{person: к-сть опублікованого за період} для ВСІХ людей ростера, або
    None, якщо БД сайту недоступна. offset — зсув періоду (історія рахується
    з nodes, які тримають весь `published`). own=True — лише власні матеріали.
    Кеш 5 хв на (metric, period, own, offset)."""
    if not db.is_configured():
        return None
    start, _ = period_bounds(period, offset)
    cache_key = (metric, period, bool(own), start.isoformat())
    hit = _fact_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    users = _user_id_map()
    links = get_user_links()
    person_by_uid = {}
    for person in team_roster.ROSTER:
        uid = resolve_site_user_id(person, links, users)  # пін → пошук за ПІБ
        if uid:
            person_by_uid[uid] = person
    result = {p: 0 for p in team_roster.ROSTER}
    if person_by_uid:
        ts_start, ts_end = _period_ts_range(period, offset)
        own_cond = "AND own_material = 1 " if own else ""
        rows = db.query(
            "SELECT owner_id, COUNT(*) AS c FROM nodes "
            "WHERE type = %s AND status = 1 "
            f"{own_cond}"
            "AND published >= %s AND published < %s AND published <= UNIX_TIMESTAMP() "
            "AND owner_id IN ({}) GROUP BY owner_id".format(
                ",".join(["%s"] * len(person_by_uid))
            ),
            [metric, ts_start, ts_end, *person_by_uid.keys()],
        )
        for r in rows:
            person = person_by_uid.get(r["owner_id"])
            if person:
                result[person] = r["c"]
    # Люди без жодного users.id (ні піна, ні матчу за ПІБ) → None (апка малює
    # «—», а не фейковий 0 — людина може писати під іншим ПІБ).
    linked = set(person_by_uid.values())
    for person in team_roster.ROSTER:
        if person not in linked:
            result[person] = None
    _fact_cache[cache_key] = (time.monotonic() + FACT_CACHE_TTL, result)
    return result


# ---------- Діагностика факту (/kpi_debug) ----------

def kpi_debug(name_query):
    """Чому у людини такий факт: показує, до якого users.id її привʼязано за
    ПІБ, які ще є записи з таким прізвищем, і РОЗКЛАДКУ виходу за поточний
    місяць (тип × власний × owner_id). Одразу видно причину «0»: не той тип,
    не власні, або матеріали під іншим owner_id, ніж записано в users."""
    if not db.is_configured():
        return "БД сайту не налаштована."
    q = (name_query or "").strip()
    if not q:
        return "Вкажіть імʼя або прізвище."

    # 1) кого з ростера маємо на увазі
    person = next((p for p in team_roster.ROSTER if _norm_name(q) in _norm_name(p)), None)

    # 2) до якого id привʼязано (пін → пошук за ПІБ) — виробничий шлях
    links = get_user_links()
    matched_id = resolve_site_user_id(person, links) if person else None
    pinned = person in links if person else False

    # 3) усі users із таким прізвищем (ловить варіанти написання/дублі)
    last = q.split()[-1]
    urows = db.query(
        "SELECT id, first_name, last_name FROM users "
        "WHERE last_name LIKE %s OR first_name LIKE %s ORDER BY id",
        (f"%{last}%", f"%{last}%"),
    )

    # 4) розкладка виходу за поточний місяць по цих id
    ts_start, ts_end = _period_ts_range("month")
    ids = [r["id"] for r in urows]
    breakdown = []
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        breakdown = db.query(
            f"SELECT owner_id, type, own_material, COUNT(*) AS c FROM nodes "
            f"WHERE owner_id IN ({placeholders}) AND status = 1 "
            f"AND published >= %s AND published < %s AND published <= UNIX_TIMESTAMP() "
            f"GROUP BY owner_id, type, own_material ORDER BY owner_id, type, own_material",
            [*ids, ts_start, ts_end],
        )

    lines = [f"🔎 <b>{person or q}</b> — діагностика факту за {period_label('month')}"]
    pin_mark = " 📌 закріплено" if pinned else " (за ПІБ)"
    lines.append(f"Привʼязано до users.id: <b>{matched_id if matched_id else '❌ не знайдено'}</b>{pin_mark if matched_id else ''}")
    lines.append("\n<b>Записи users із цим прізвищем:</b>")
    for r in urows:
        mark = " ← привʼязано" if r["id"] == matched_id else ""
        lines.append(f"  id={r['id']}: {r['first_name']} {r['last_name']}{mark}")
    if not urows:
        lines.append("  (жодного — саме тому факт «—»)")

    lines.append("\n<b>Вихід за місяць (тип × власний × owner_id):</b>")
    if breakdown:
        for r in breakdown:
            own = "власне" if r["own_material"] == 1 else "рерайт"
            here = " ← рахує норма" if r["owner_id"] == matched_id else " ⚠️ інший id!"
            lines.append(f"  owner_id={r['owner_id']} · {r['type']} · {own}: <b>{r['c']}</b>{here}")
    else:
        lines.append("  (нічого не опубліковано цього місяця під цими id)")

    # Підказка: якщо весь вихід під іншим id, ніж привʼязаний — закріпити правильний
    other_ids = {r["owner_id"] for r in breakdown if r["owner_id"] != matched_id}
    if person and other_ids and not any(r["owner_id"] == matched_id for r in breakdown):
        best = max(other_ids, key=lambda i: sum(r["c"] for r in breakdown if r["owner_id"] == i))
        lines.append(
            f"\n⚠️ <b>Весь вихід під id={best}, а норма рахує під id={matched_id}.</b>\n"
            f"Виправити: <code>/kpi_link {person} {best}</code>"
        )
    else:
        lines.append(
            "\n<i>Норма «власних новин» рахує лише type=news + own_material=1 "
            "під привʼязаним id.</i>"
        )
    return "\n".join(lines)


# ---------- Зведення для API ----------

def kpi_payload(for_person=None):
    """Повне зведення KPI. for_person — лише норми відділу цієї людини і лише
    її рядок (журналістський вид); None — всі норми з усіма людьми (менеджер)."""
    norms = list_norms()
    # Відділи — фактичні (перекриття з team_dept, Катя переносить в апці)
    depts = team_roster.dept_overrides()
    if for_person:
        dept = team_roster.effective_dept(for_person, depts)
        norms = [n for n in norms if n["dept"] == dept]
    out = []
    for n in norms:
        if for_person:
            people = [for_person]
        else:
            people = [p for p, i in team_roster.ROSTER.items()
                      if not i["manager"]
                      and team_roster.effective_dept(p, depts) == n["dept"]]
        overrides = _overrides_for(n["id"], n["period"])
        facts, article_bonus = weighted_facts(n["metric"], n["period"], n["own"])
        p_start, p_end = period_bounds(n["period"])
        absences = _absences_by_person(set(people))
        rows = []
        for person in people:
            ov = overrides.get(person)
            # Ручна правка сильніша за арифметику: якщо Катя поставила ціль
            # руками, відпустку поверх неї не перераховуємо
            factor, missed = (1.0, 0) if ov else absence_factor(
                person, p_start, p_end, absences.get(person, []))
            target = ov["target"] if ov else scaled_target(n["target"], factor)
            fact = None if facts is None else facts.get(person)
            away = [a for a in absences.get(person, [])
                    if a["end"] >= p_start.isoformat()
                    and a["start"] < p_end.isoformat()]
            rows.append({
                "person": person,
                "fact": fact,
                "target": target,
                "base_target": n["target"],
                "overridden": ov is not None,
                "note": ov["note"] if ov else (
                    f"{away[0]['title']} · ціль {target} замість {n['target']}"
                    if missed and target else
                    (f"{away[0]['title']} · написала {fact} понад норму"
                     if missed and fact else
                     (away[0]["title"] if missed else None))),
                "excused": bool(ov and ov["target"] == 0) or (not ov and target == 0),
                "absence": away[0] if away else None,
                "missed_days": missed,
                "articles": article_bonus.get(person, 0),
                "article_weight": ARTICLE_WEIGHT,
                "done": fact is not None and target > 0 and fact >= target,
            })
        out.append({
            **n,
            "dept_title": team_roster.DEPT_TITLES.get(n["dept"], n["dept"]),
            "period_label": period_label(n["period"]),
            "rows": rows,
        })
    return {
        "norms": out,
        "week_label": period_label("week"),
        "month_label": period_label("month"),
        "site_db": db.is_configured(),
    }


def _person_month_buckets(uid, months):
    """{(рік,місяць): {(type, own_material): к-сть}} виходу людини за останні
    `months` місяців. ОДИН запит до nodes, бакетинг по київських місяцях."""
    start, _ = period_bounds("month", -(months - 1))
    ts_start = int(datetime(start.year, start.month, 1, tzinfo=KYIV_TZ).timestamp())
    rows = db.query(
        "SELECT published, type, own_material FROM nodes "
        "WHERE owner_id = %s AND status = 1 "
        "AND published >= %s AND published <= UNIX_TIMESTAMP()",
        (int(uid), ts_start),
    )
    buckets = {}
    for r in rows:
        dt = datetime.fromtimestamp(r["published"], KYIV_TZ)
        b = buckets.setdefault((dt.year, dt.month), {})
        key = (r["type"], 1 if r["own_material"] == 1 else 0)
        b[key] = b.get(key, 0) + 1
    return buckets


def kpi_person_history(person, months=12):
    """Помісячна динаміка виконання KPI людини (профіль): за кожен з останніх
    `months` місяців — факт/ціль по її місячних нормах і сукупний %.
    Історія — з nodes (весь published); норми поточні, правки — за період."""
    if person not in team_roster.ROSTER:
        return None
    dept = team_roster.effective_dept(person)
    norms = [n for n in list_norms() if n["period"] == "month" and n["dept"] == dept]
    uid = resolve_site_user_id(person) if db.is_configured() else None
    buckets = _person_month_buckets(uid, months) if uid else {}
    person_absences = list_absences(person)

    out_months = []
    for off in range(-(months - 1), 1):
        start, _ = period_bounds("month", off)
        b = buckets.get((start.year, start.month), {})
        m_norms, pcts = [], []
        month_end = period_bounds("month", off)[1]
        for n in norms:
            ov = _overrides_for(n["id"], "month", off).get(person)
            if ov and ov["target"] == 0:
                continue  # звільнена того місяця
            factor, _missed = (1.0, 0) if ov else absence_factor(
                person, start, month_end, person_absences)
            target = ov["target"] if ov else scaled_target(n["target"], factor)
            if not ov and target == 0:
                continue  # весь місяць у відпустці
            def bucket_count(metric):
                if n["own"]:
                    return b.get((metric, 1), 0)
                return b.get((metric, 0), 0) + b.get((metric, 1), 0)

            articles = 0
            if uid is None:
                fact = None
            else:
                fact = bucket_count(n["metric"])
                # Стаття важить три новини — та сама вага, що в поточних
                # цифрах, інакше динаміка місяців суперечила б кружечку
                if n["metric"] == "news":
                    articles = bucket_count("article")
                    fact += articles * ARTICLE_WEIGHT
            pct = None if fact is None or target <= 0 else min(100, round(fact / target * 100))
            if pct is not None:
                pcts.append(pct)
            m_norms.append({
                "label": norm_label(n["metric"], target, n["own"]),
                "fact": fact, "target": target, "pct": pct,
                "articles": articles, "article_weight": ARTICLE_WEIGHT,
                "done": fact is not None and target > 0 and fact >= target,
            })
        out_months.append({
            "label": f"{MONTHS_UA_SHORT[start.month - 1]} {str(start.year)[2:]}",
            "offset": off, "is_current": off == 0,
            "overall_pct": round(sum(pcts) / len(pcts)) if pcts else None,
            "norms": m_norms,
        })
    return {
        "person": person,
        "dept_title": team_roster.DEPT_TITLES.get(dept, dept),
        "has_norms": bool(norms),
        "site_db": db.is_configured(),
        "months": out_months,
    }


def kpi_dashboard(period, offset=0):
    """Звітний дашборд за період (з історією через offset): по кожній людині,
    що має норму цього типу періоду — факт/ціль і % виконання (для кільця
    навколо аватарки). Норми беруться поточні (історії норм не тримаємо),
    правки — за конкретний період (team_kpi_overrides по period_start),
    факт — з nodes за цей період."""
    if period not in KPI_PERIODS:
        period = "week"
    norms = [n for n in list_norms() if n["period"] == period]
    depts = team_roster.dept_overrides()

    # факти рахуємо раз на (metric, own) — не на кожну людину
    fact_cache = {}
    def facts_for(n):
        key = (n["metric"], n["own"])
        if key not in fact_cache:
            fact_cache[key] = weighted_facts(n["metric"], period, n["own"], offset)
        return fact_cache[key]

    # людина → її норми цього періоду (за фактичним відділом) з фактом
    per_person = {}
    p_start, p_end = period_bounds(period, offset)
    absences = _absences_by_person()
    for n in norms:
        overrides = _overrides_for(n["id"], period, offset)
        facts, article_bonus = facts_for(n)
        for person, info in team_roster.ROSTER.items():
            if info["manager"] or team_roster.effective_dept(person, depts) != n["dept"]:
                continue
            ov = overrides.get(person)
            if ov and ov["target"] == 0:
                continue  # звільнена цього періоду — у дашборд не тягнемо
            # Відпустка пропорційно знижує ціль (правка руками — сильніша)
            factor, missed = (1.0, 0) if ov else absence_factor(
                person, p_start, p_end, absences.get(person, []))
            target = ov["target"] if ov else scaled_target(n["target"], factor)
            fact = None if facts is None else facts.get(person)
            # Весь період у відпустці — у звіті їй нічого робити. АЛЕ якщо вона
            # все ж щось опублікувала (буває: дописала матеріал із відпустки),
            # ховати це не можна — робота зроблена, і має бути видно.
            if not ov and target == 0 and not fact:
                continue
            bucket = per_person.setdefault(
                person, {"dept": n["dept"], "norms": [], "missed_days": missed})
            bucket["missed_days"] = max(bucket["missed_days"], missed)
            bucket["norms"].append({
                "label": norm_label(n["metric"], target, n["own"]),
                "metric": n["metric"], "own": n["own"],
                "articles": article_bonus.get(person, 0),
                "article_weight": ARTICLE_WEIGHT,
                "fact": fact, "target": target,
                # Ціль 0 (відпустка) з фактом — це 100%: зробила більше, ніж
                # вимагалось. Ділити на нуль тут ні до чого.
                "pct": 100 if (target <= 0 and fact) else (
                    None if fact is None or target <= 0
                    else min(100, round(fact / target * 100))),
                "beyond": bool(target <= 0 and fact),
                "done": (fact is not None and target > 0 and fact >= target)
                        or bool(target <= 0 and fact),
            })

    people = []
    for person, d in per_person.items():
        pcts = [x["pct"] for x in d["norms"] if x["pct"] is not None]
        overall = round(sum(pcts) / len(pcts)) if pcts else None
        people.append({
            "person": person,
            "dept": d["dept"],
            "dept_title": team_roster.DEPT_TITLES.get(d["dept"], d["dept"]),
            "norms": d["norms"],
            "missed_days": d.get("missed_days", 0),
            "overall_pct": overall,
            "all_done": bool(d["norms"]) and all(x["done"] for x in d["norms"]),
        })
    # спершу хто відстає (менший %), звільнені/без норм не потрапили
    people.sort(key=lambda p: (p["overall_pct"] if p["overall_pct"] is not None else 999,
                               p["person"]))
    return {
        "period": period,
        "offset": offset,
        "label": period_label(period, offset),
        "is_current": offset == 0,
        "site_db": db.is_configured(),
        "people": people,
    }
