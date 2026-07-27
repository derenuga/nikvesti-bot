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

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import bot_db, db, team_roster
from handlers.team_projects import _norm_name

KYIV_TZ = ZoneInfo("Europe/Kiev")

KPI_METRICS = ("news", "article")
KPI_PERIODS = ("week", "month")

MONTHS_UA = ["січень", "лютий", "березень", "квітень", "травень", "червень",
             "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]

FACT_CACHE_TTL = 300
_fact_cache = {}  # (metric, period_start_iso) -> (expires, {person: count})
_users_cache = {"at": 0.0, "map": {}}  # norm_name -> users.id

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_kpi_norms (
        id         BIGSERIAL PRIMARY KEY,
        dept       TEXT NOT NULL,
        metric     TEXT NOT NULL,
        period     TEXT NOT NULL,
        target     SMALLINT NOT NULL,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
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
    # Міграція 27.07: відділи зведено до реальної структури Creative/Newsroom —
    # норми, створені зі старими слагами, переносяться (ідемпотентно).
    "UPDATE team_kpi_norms SET dept = 'newsroom' "
    "WHERE dept IN ('журналістика', 'стрічка', 'переклад')",
    "UPDATE team_kpi_norms SET dept = 'creative' WHERE dept IN ('соцмережі', 'відео')",
]

_schema_done = False


def ensure_kpi_schema():
    global _schema_done
    if _schema_done:
        return
    for sql in _SCHEMA_STATEMENTS:
        bot_db.execute(sql)
    _schema_done = True


# ---------- Періоди (Київ) ----------

def period_bounds(period, today=None):
    """(start_date, end_date_exclusive) поточного періоду за Києвом."""
    today = today or datetime.now(KYIV_TZ).date()
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    start = today.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


def period_label(period, today=None):
    start, end = period_bounds(period, today)
    if period == "week":
        last = end - timedelta(days=1)
        return f"{start.day:02d}–{last.day:02d}.{last.month:02d}"
    return MONTHS_UA[start.month - 1]


def _period_ts_range(period):
    """(unix_start, unix_end) періоду — межі за київською північчю."""
    start, end = period_bounds(period)
    to_ts = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=KYIV_TZ).timestamp())
    return to_ts(start), to_ts(end)


# ---------- Норми (CRUD) ----------

def _row_to_norm(r):
    return {"id": r["id"], "dept": r["dept"], "metric": r["metric"],
            "period": r["period"], "target": r["target"]}


def list_norms():
    ensure_kpi_schema()
    return [_row_to_norm(r) for r in bot_db.query(
        "SELECT * FROM team_kpi_norms ORDER BY dept, period, metric")]


def add_norm(creator, dept, metric, period, target):
    ensure_kpi_schema()
    rows = bot_db.query(
        "INSERT INTO team_kpi_norms (dept, metric, period, target, created_by) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (dept, metric, period, int(target), creator),
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


def _overrides_for(norm_id, period):
    start, _ = period_bounds(period)
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


def fact_counts(metric, period):
    """{person: к-сть опублікованого цього періоду} для ВСІХ людей ростера,
    або None, якщо БД сайту недоступна. Кеш 5 хв на (metric, period_start)."""
    if not db.is_configured():
        return None
    start, _ = period_bounds(period)
    cache_key = (metric, period, start.isoformat())
    hit = _fact_cache.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    users = _user_id_map()
    person_by_uid = {}
    for person in team_roster.ROSTER:
        uid = users.get(_norm_name(person))
        if uid:
            person_by_uid[uid] = person
    result = {p: 0 for p in team_roster.ROSTER}
    if person_by_uid:
        ts_start, ts_end = _period_ts_range(period)
        rows = db.query(
            "SELECT owner_id, COUNT(*) AS c FROM nodes "
            "WHERE type = %s AND status = 1 "
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
    # Люди без матчу в users лишаються з 0 — але позначаємо їх None, щоб
    # апка показала «—», а не фейковий нуль (людина може писати під іншим ПІБ).
    matched = {_norm_name(p) for p in team_roster.ROSTER} & set(users.keys())
    for person in team_roster.ROSTER:
        if _norm_name(person) not in matched:
            result[person] = None
    _fact_cache[cache_key] = (time.monotonic() + FACT_CACHE_TTL, result)
    return result


# ---------- Зведення для API ----------

def kpi_payload(for_person=None):
    """Повне зведення KPI. for_person — лише норми відділу цієї людини і лише
    її рядок (журналістський вид); None — всі норми з усіма людьми (менеджер)."""
    norms = list_norms()
    if for_person:
        dept = team_roster.ROSTER[for_person]["dept"]
        norms = [n for n in norms if n["dept"] == dept]
    out = []
    for n in norms:
        if for_person:
            people = [for_person]
        else:
            people = [p for p, i in team_roster.ROSTER.items()
                      if i["dept"] == n["dept"] and not i["manager"]]
        overrides = _overrides_for(n["id"], n["period"])
        facts = fact_counts(n["metric"], n["period"])
        rows = []
        for person in people:
            ov = overrides.get(person)
            target = ov["target"] if ov else n["target"]
            fact = None if facts is None else facts.get(person)
            rows.append({
                "person": person,
                "fact": fact,
                "target": target,
                "base_target": n["target"],
                "overridden": ov is not None,
                "note": ov["note"] if ov else None,
                "excused": bool(ov and ov["target"] == 0),
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
