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
from datetime import date, datetime, timedelta
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

_schema_done = False


def ensure_kpi_schema():
    global _schema_done
    if _schema_done:
        return
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
        facts = fact_counts(n["metric"], n["period"], n["own"])
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
            fact_cache[key] = fact_counts(n["metric"], period, n["own"], offset)
        return fact_cache[key]

    # людина → її норми цього періоду (за фактичним відділом) з фактом
    per_person = {}
    for n in norms:
        overrides = _overrides_for(n["id"], period, offset)
        facts = facts_for(n)
        for person, info in team_roster.ROSTER.items():
            if info["manager"] or team_roster.effective_dept(person, depts) != n["dept"]:
                continue
            ov = overrides.get(person)
            if ov and ov["target"] == 0:
                continue  # звільнена цього періоду — у дашборд не тягнемо
            target = ov["target"] if ov else n["target"]
            fact = None if facts is None else facts.get(person)
            per_person.setdefault(person, {"dept": n["dept"], "norms": []})["norms"].append({
                "label": f"{target} {n['metric']}" + (" власних" if n["own"] else ""),
                "metric": n["metric"], "own": n["own"],
                "fact": fact, "target": target,
                "pct": None if fact is None or target <= 0 else min(100, round(fact / target * 100)),
                "done": fact is not None and target > 0 and fact >= target,
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
