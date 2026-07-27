"""
Аналітичний дашборд Mini App «Команда» — таб «Аналітика» на Головній.

Запит Олега (27.07): «хочу прокинутись, а в мене єбейша аналітика по всьому, що
ми трекаємо в норі». Один екран на питання «як ідуть справи»: сайт, пошук
Google, вихід редакції, соцмережі, гранти й топ матеріалів — за тиждень або
місяць, з порівнянням до попереднього періоду.

ЗВІДКИ ДАНІ (усе вже накопичене, нових джерел не заводимо):

| Блок | Джерело | Модуль, що наповнює |
|---|---|---|
| Сайт (users/sessions/pageviews по днях) | Нора `daily_stats` | analytics_store (09:00) |
| Топ матеріалів періоду | Нора `daily_stats.top_pages` | той самий захват |
| Пошук по типах (web/discover/googleNews) | Нора `sc_daily_stats` | analytics_store (09:00) |
| Соцмережі (підписники/перегляди/взаємодії) | Нора `social_stats` | social_store (нд) |
| Вихід редакції (новини/статті/власні/автори) | БД сайту `nodes` | CMS напряму |
| Гранти (проєкти, завдання, зараховане) | БД сайту + Нора `team_*` | Mini App |
| Підписники Telegram | t.me (живий знімок, кеш 1 год) | telegram_stats |

ЧОМУ ПОРІВНЯННЯ «ДЕНЬ-У-ДЕНЬ», А НЕ ПЕРІОД-У-ПЕРІОД. Поточний тиждень у вівторок
має два дні даних, а минулий — сім; пряме порівняння сум показувало б −70% і
щоранку брехало б. Тому для НЕПОВНОГО періоду попередній ріжеться до тієї самої
кількості днів (`comparable_days`), а для завершеного беруться обидва повністю.

ЩО ВВАЖАТИ ЧЕСНИМ:
- дані сайту й пошуку є лише до ВЧОРА (захват о 09:00), тож `data_end` майже
  завжди вчорашній день — його й показуємо підписом «дані до …»;
- Search Console фіналізує цифри із затримкою ~2-3 дні: свіжі дні неповні
  (analytics_store перезбирає трейлінг-вікно, але останній день усе одно нижчий);
- топ матеріалів зібраний із ДЕННИХ топ-5, тож матеріал, який щодня був шостим,
  у період не потрапить (сумарно міг би бути вище) — це топ «за помітністю», а
  не повний рейтинг;
- соцмережі — ТИЖНЕВІ зрізи (Meta історії не віддає): у місяць потрапляє сума
  знімків, чиї дати всередині місяця, а підписники — останній знімок;
- Нора й БД сайту деградують ОКРЕМО: недоступність однієї гасить свій блок, а
  не весь екран (`nora` / `site_db` у відповіді).

Кеш payload — 180 с на (period, offset): екран смикають по кілька разів
підряд (перемикачі тиждень/місяць і гортання), а половина цифр іде в MySQL сайту
з його лімітами.
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import bot_db, db, team_kpi, team_projects

KYIV_TZ = ZoneInfo("Europe/Kiev")

PERIODS = ("week", "month")

# Типи пошуку Search Console — фіксований порядок (він же порядок кольорів у
# частковій смузі на фронті: слот кольору йде за сутністю, не за величиною).
SEARCH_TYPES = [
    ("web", "Пошук"),
    ("discover", "Discover"),
    ("googleNews", "Google Новини"),
]

PAYLOAD_TTL = 180
_cache = {}   # (period, offset) -> (expires, payload)

TG_SUBS_TTL = 3600
_tg_cache = {"at": 0.0, "value": None}

# Скільки рядків показуємо у списках (топ матеріалів, автори, донори)
TOP_MATERIALS = 6
TOP_AUTHORS = 8
TOP_DONORS = 5


# ---------- Періоди ----------

def _iso(d):
    return d.isoformat()


def _ts(d):
    """unix-межа київської півночі дати."""
    return int(datetime(d.year, d.month, d.day, tzinfo=KYIV_TZ).timestamp())


def _ranges(period, offset, today=None):
    """Межі поточного й порівняльного періодів.

    Повертає dict із датами (date) і прапорцями. `data_end` — останній день, за
    який дані взагалі можуть існувати (не пізніше вчора: захват о 09:00 пише
    вчорашній день). `prev_end` дорівнює повному попередньому періоду для
    завершеного періоду і тій самій кількості днів — для поточного.

    today — лише для тестів (як у team_kpi.period_bounds)."""
    today = today or datetime.now(KYIV_TZ).date()
    start, end_excl = team_kpi.period_bounds(period, offset, today)
    prev_start, prev_end_excl = team_kpi.period_bounds(period, offset - 1, today)
    yesterday = today - timedelta(days=1)
    is_current = start <= today < end_excl
    last_day = end_excl - timedelta(days=1)
    data_end = min(last_day, yesterday)
    days_total = (end_excl - start).days
    elapsed = (data_end - start).days + 1 if data_end >= start else 0
    if is_current:
        # неповний період порівнюємо з тим самим відрізком попереднього
        prev_end = prev_start + timedelta(days=max(elapsed, 1) - 1)
    else:
        prev_end = prev_end_excl - timedelta(days=1)
    return {
        "start": start, "end": last_day, "data_end": data_end,
        "prev_start": prev_start, "prev_end": prev_end,
        "is_current": is_current, "days_total": days_total,
        "days": max(elapsed, 0),
        "comparable_days": (prev_end - prev_start).days + 1,
        "has_data": data_end >= start,
    }


# ---------- Дрібні хелпери ----------

def _sum(rows, key):
    """Сума колонки, NULL-и пропускаємо. None — якщо жодного значення немає."""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) if vals else None


def _delta(cur, prev):
    """Зміна у % до порівняльного періоду. None, коли рахувати нема від чого
    (немає одного зі значень або попередній нуль — «зростання з нуля» у
    відсотках безглузде)."""
    if cur is None or not prev:
        return None
    return round((cur - prev) / prev * 100)


def _in(rows, start, end, key="date"):
    a, b = _iso(start), _iso(end)
    return [r for r in rows if a <= r[key] <= b]


# ---------- Сайт (GA4 з daily_stats) ----------

def _site_block(rg):
    rows = bot_db.query(
        "SELECT to_char(date, 'YYYY-MM-DD') AS date, users, sessions, pageviews "
        "FROM daily_stats WHERE date BETWEEN %s AND %s ORDER BY date",
        (rg["prev_start"], rg["data_end"]),
    )
    cur = _in(rows, rg["start"], rg["data_end"])
    prev = _in(rows, rg["prev_start"], rg["prev_end"])
    metrics = {}
    for key in ("users", "sessions", "pageviews"):
        c, p = _sum(cur, key), _sum(prev, key)
        metrics[key] = {"value": c, "prev": p, "delta": _delta(c, p)}
    best = max((r for r in cur if r["users"] is not None),
               key=lambda r: r["users"], default=None)
    return {
        **metrics,
        "series": [{"date": r["date"], "users": r["users"] or 0,
                    "pageviews": r["pageviews"] or 0} for r in cur],
        "best_day": {"date": best["date"], "users": best["users"]} if best else None,
        "days_with_data": len(cur),
    }


def _clean_title(title, path):
    """GA4 віддає title сторінки як є — з хвостом «| МикВісті». У списку топу
    той хвіст займає півряда й нічого не додає."""
    t = (title or "").strip()
    for sep in (" | ", " — МикВісті", " - МикВісті"):
        if sep in t:
            t = t.split(sep)[0].strip()
    return t or path


def _top_materials(rg):
    """Топ матеріалів періоду з денних топ-5 (daily_stats.top_pages): сумуємо
    перегляди по шляху, лишаємо найкращі. days — у скількох денних топах
    матеріал взагалі був (сам собою показник «довгограючості»)."""
    rows = bot_db.query(
        "SELECT top_pages FROM daily_stats "
        "WHERE date BETWEEN %s AND %s AND top_pages IS NOT NULL ORDER BY date",
        (rg["start"], rg["data_end"]),
    )
    agg = {}
    for r in rows:
        for item in (r["top_pages"] or []):
            path = (item or {}).get("path")
            if not path:
                continue
            entry = agg.setdefault(path, {
                "path": path, "title": _clean_title(item.get("title"), path),
                "views": 0, "days": 0, "author": None,
            })
            entry["views"] += int(item.get("views") or 0)
            entry["days"] += 1
            if item.get("author") and not entry["author"]:
                entry["author"] = item["author"]
    top = sorted(agg.values(), key=lambda x: -x["views"])[:TOP_MATERIALS]
    for x in top:
        x["url"] = "https://nikvesti.com" + x["path"]
    return top


# ---------- Пошук (Search Console з sc_daily_stats) ----------

def _search_block(rg):
    rows = bot_db.query(
        "SELECT to_char(date, 'YYYY-MM-DD') AS date, search_type, clicks, impressions "
        "FROM sc_daily_stats WHERE date BETWEEN %s AND %s",
        (rg["prev_start"], rg["data_end"]),
    )
    cur = _in(rows, rg["start"], rg["data_end"])
    prev = _in(rows, rg["prev_start"], rg["prev_end"])
    clicks = _sum(cur, "clicks")
    types = []
    for key, label in SEARCH_TYPES:
        c = _sum([r for r in cur if r["search_type"] == key], "clicks")
        p = _sum([r for r in prev if r["search_type"] == key], "clicks")
        imp = _sum([r for r in cur if r["search_type"] == key], "impressions")
        types.append({
            "type": key, "label": label,
            "clicks": c or 0, "impressions": imp or 0,
            "prev": p, "delta": _delta(c, p),
            "share": round((c or 0) / clicks * 100) if clicks else 0,
        })
    return {
        "clicks": {"value": clicks, "prev": _sum(prev, "clicks"),
                   "delta": _delta(clicks, _sum(prev, "clicks"))},
        "impressions": {"value": _sum(cur, "impressions")},
        "types": types,
        "has_data": bool(cur),
    }


# ---------- Вихід редакції (nodes БД сайту) ----------

def _newsroom_block(rg):
    """Скільки вийшло за період: новини / статті / власні + топ авторів.

    ОДИН запит на обидва періоди: колонка `cur` розділяє поточний і
    порівняльний відрізок (ліміти БД сайту не дозволяють ходити двічі там, де
    можна раз). published <= now — гейт відкладених в адмінці."""
    ts_start, ts_end = _ts(rg["start"]), _ts(rg["data_end"] + timedelta(days=1))
    ts_prev_start = _ts(rg["prev_start"])
    ts_prev_end = _ts(rg["prev_end"] + timedelta(days=1))
    # Імʼя автора — через MAX(), а не в GROUP BY: під ONLY_FULL_GROUP_BY
    # (дефолт MySQL 5.7+) неагрегована колонка в select-листі впала б, а
    # групувати ще й по конкатенації імені ні до чого — воно однозначне для
    # owner_id. Вираз бакета в GROUP BY повторено дослівно з тієї ж причини:
    # алиас у GROUP BY — розширення MySQL, а не стандарт.
    rows = db.query(
        "SELECT n.owner_id AS owner_id, n.type AS type, n.own_material AS own, "
        "(n.published >= %s) AS cur, COUNT(*) AS c, "
        "MAX(TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,'')))) AS name "
        "FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
        "WHERE n.status = 1 AND n.type IN ('news', 'article') "
        "AND ((n.published >= %s AND n.published < %s) "
        "  OR (n.published >= %s AND n.published < %s)) "
        "AND n.published <= UNIX_TIMESTAMP() "
        "GROUP BY n.owner_id, n.type, n.own_material, (n.published >= %s)",
        (ts_start, ts_start, ts_end, ts_prev_start, ts_prev_end, ts_start),
    )

    def totals(is_cur):
        out = {"news": 0, "articles": 0, "own": 0, "total": 0}
        for r in rows:
            if bool(r["cur"]) != is_cur:
                continue
            c = int(r["c"])
            out["total"] += c
            out["news" if r["type"] == "news" else "articles"] += c
            if r["own"] == 1:
                out["own"] += c
        return out

    cur, prev = totals(True), totals(False)
    authors = {}
    for r in rows:
        if not r["cur"]:
            continue
        name = (r["name"] or "").strip() or f"id {r['owner_id']}"
        a = authors.setdefault(name, {"name": name, "count": 0, "own": 0,
                                      "news": 0, "articles": 0})
        c = int(r["c"])
        a["count"] += c
        a["news" if r["type"] == "news" else "articles"] += c
        if r["own"] == 1:
            a["own"] += c
    top = sorted(authors.values(), key=lambda a: (-a["count"], a["name"]))[:TOP_AUTHORS]
    return {
        "news": {"value": cur["news"], "prev": prev["news"],
                 "delta": _delta(cur["news"], prev["news"])},
        "articles": {"value": cur["articles"], "prev": prev["articles"],
                     "delta": _delta(cur["articles"], prev["articles"])},
        "own": {"value": cur["own"], "prev": prev["own"],
                "delta": _delta(cur["own"], prev["own"])},
        "own_share": round(cur["own"] / cur["total"] * 100) if cur["total"] else None,
        "total": cur["total"],
        "authors": top,
    }


# ---------- Соцмережі (тижневі зрізи social_stats + живий Telegram) ----------

def _followers_at(day):
    """{платформа: (підписники, дата знімка)} — останній знімок не пізніше
    дати. DISTINCT ON — щоб не тягти всю історію заради двох чисел."""
    rows = bot_db.query(
        "SELECT DISTINCT ON (platform) platform, followers, "
        "to_char(week_end, 'YYYY-MM-DD') AS week_end FROM social_stats "
        "WHERE followers IS NOT NULL AND week_end <= %s "
        "ORDER BY platform, week_end DESC",
        (day,),
    )
    return {r["platform"]: (r["followers"], r["week_end"]) for r in rows}


def _tg_subscribers():
    """Підписники каналу — живий знімок t.me (у норі їх немає). Кеш на годину;
    збій нічого не ламає: блок просто показує «—»."""
    if _tg_cache["value"] is not None and time.monotonic() - _tg_cache["at"] < TG_SUBS_TTL:
        return _tg_cache["value"]
    try:
        from handlers import telegram_stats

        value = telegram_stats.channel_subscribers()
    except Exception as e:
        print(f"team_analytics: підписники Telegram не прочитались — {e}")
        return _tg_cache["value"]
    if value:
        _tg_cache.update({"at": time.monotonic(), "value": value})
    return value


def _social_block(rg):
    rows = bot_db.query(
        "SELECT platform, to_char(week_end, 'YYYY-MM-DD') AS date, "
        "followers, reach, views, engagement, posts FROM social_stats "
        "WHERE week_end BETWEEN %s AND %s ORDER BY week_end",
        (rg["prev_start"], rg["data_end"]),
    )
    now_f = _followers_at(rg["data_end"])
    was_f = _followers_at(rg["start"] - timedelta(days=1))
    out = {}
    for platform, title in (("facebook", "Facebook"), ("instagram", "Instagram")):
        mine = [r for r in rows if r["platform"] == platform]
        cur = _in(mine, rg["start"], rg["data_end"])
        prev = _in(mine, rg["prev_start"], rg["prev_end"])
        followers, as_of = now_f.get(platform, (None, None))
        before = (was_f.get(platform) or (None, None))[0]
        block = {"title": title, "followers": followers, "followers_as_of": as_of,
                 "followers_delta": (followers - before)
                 if (followers is not None and before is not None) else None,
                 "snapshots": len(cur)}
        for key in ("views", "engagement", "posts"):
            c, p = _sum(cur, key), _sum(prev, key)
            block[key] = {"value": c, "prev": p, "delta": _delta(c, p)}
        out[platform] = block
    out["telegram"] = {"title": "Telegram", "subscribers": _tg_subscribers()}
    return out


# ---------- Гранти (проєкти + завдання + зараховане) ----------

def _grants_block(rg, projects):
    # Межі — timestamptz із київською таймзоною: done_at/published у Норі
    # timestamptz, а сира дата в параметрі трактувалась би за таймзоною сесії
    # Postgres (UTC на Railway) і зсувала б добу на три години.
    ts_start = datetime.combine(rg["start"], datetime.min.time(), tzinfo=KYIV_TZ)
    ts_end = datetime.combine(rg["data_end"] + timedelta(days=1),
                              datetime.min.time(), tzinfo=KYIV_TZ)
    open_tasks = bot_db.query(
        "SELECT count(*) AS c FROM team_creative_tasks WHERE status = 'open'"
    )[0]["c"]
    done_tasks = bot_db.query(
        "SELECT count(*) AS c FROM team_creative_tasks "
        "WHERE status = 'done' AND done_at >= %s AND done_at < %s",
        (ts_start, ts_end),
    )[0]["c"]
    # Зараховане виконання за період — за датою ПУБЛІКАЦІЇ, а не рішення:
    # ретроспектива закриває таски заднім числом, і по даті рішення липневі
    # матеріали впали б у серпень.
    counted = bot_db.query(
        "SELECT project_id, count(*) AS c FROM team_task_matches "
        "WHERE status IN ('auto', 'confirmed') "
        "AND published >= %s AND published < %s GROUP BY project_id",
        (ts_start, ts_end),
    )
    pending = bot_db.query(
        "SELECT count(*) AS c FROM team_task_matches WHERE status = 'pending'"
    )[0]["c"]
    by_id = {p["id"]: p for p in projects}
    donors = []
    for r in counted:
        p = by_id.get(r["project_id"])
        donors.append({
            "project_id": r["project_id"],
            "name": (p or {}).get("partner") or (p or {}).get("name") or "поза проєктами",
            "count": int(r["c"]),
        })
    donors.sort(key=lambda d: -d["count"])
    return {
        "projects_active": len(projects),
        "tasks_open": open_tasks,
        "tasks_done": done_tasks,
        "counted": sum(d["count"] for d in donors),
        "pending": pending,
        "donors": donors[:TOP_DONORS],
    }


# ---------- Збірка ----------

def _build(period, offset):
    rg = _ranges(period, offset)
    out = {
        "period": period,
        "offset": offset,
        "label": team_kpi.period_label(period, offset),
        "is_current": rg["is_current"],
        "range": {"start": _iso(rg["start"]), "end": _iso(rg["end"]),
                  "data_end": _iso(rg["data_end"])},
        "days": rg["days"],
        "days_total": rg["days_total"],
        "comparable_days": rg["comparable_days"],
        "prev_label": team_kpi.period_label(period, offset - 1),
        "nora": bot_db.is_configured(),
        "site_db": db.is_configured(),
    }
    if not rg["has_data"]:
        # Періоду ще не було жодного повного дня (наприклад, поточний тиждень у
        # понеділок до 09:00) — показувати нема чого, і це не помилка.
        out.update({"site": None, "search": None, "newsroom": None,
                    "social": None, "grants": None, "top": []})
        return out

    projects = []
    try:
        projects = team_projects.list_projects(True)
    except Exception as e:
        print(f"team_analytics: проєкти не прочитались — {e}")

    for key, fn in (
        ("site", lambda: _site_block(rg)),
        ("search", lambda: _search_block(rg)),
        ("top", lambda: _top_materials(rg)),
        ("social", lambda: _social_block(rg)),
        ("grants", lambda: _grants_block(rg, projects)),
        ("newsroom", lambda: _newsroom_block(rg)),
    ):
        try:
            out[key] = fn()
        except Exception as e:
            print(f"team_analytics: блок «{key}» не зібрався — {e}")
            out[key] = [] if key == "top" else None
    return out


def dashboard(period="week", offset=-1):
    """Payload аналітичного дашборда за період. period — week|month, offset —
    зсув назад (0 — поточний, -1 — попередній). Кеш 180 с."""
    if period not in PERIODS:
        period = "week"
    offset = max(-60, min(0, int(offset)))
    key = (period, offset)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    payload = _build(period, offset)
    _cache[key] = (time.monotonic() + PAYLOAD_TTL, payload)
    return payload
