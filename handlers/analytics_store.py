"""
Пам'ять щоденної аналітики GA4 у власній БД бота (Postgres, handlers/bot_db.py).

Навіщо: раніше історія трафіку не зберігалась ніде — щоденний звіт о 09:00 тягнув
з GA4 вчорашній день, показував і викидав. Тепер кожен день осідає в таблиці
daily_stats, і порівняння тиждень-до-тижня / місяць-до-місяця, тренди й графіки
рахуються з локальної БД (миттєво, без повторних запитів у GA4). Це ж розблоковує
«Тижневик Лиса» і дешевий NLQ-tool get_traffic_history.

Два джерела наповнення:
1. Щоденний звіт (scheduler.send_daily_report) — record_day() дописує вчорашній
   день РАЗОМ із топом сторінок (без зайвого GA4-запиту: цифри вже зібрані).
2. /analytics_backfill [N] — разовий бекфіл N днів історії з GA4 одним запитом
   (dimension=date). Топ сторінок по днях бекфіл НЕ тягне (це N окремих запитів) —
   лишає top_pages=NULL; історичні тренди users/sessions/pageviews від цього не
   страждають, а топ накопичується щоденним звітом уперед.

Тихо пропускається, поки не налаштована БД бота (BOT_DATABASE_URL) — як archive_mirror.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Metric, Dimension, OrderBy,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build as gapi_build

from handlers import bot_db
from handlers.google_analytics import (
    get_ga4_client, get_stats, get_top_pages, GA4_PROPERTY_ID, GA4_CREDENTIALS,
)
from handlers.notifier import notify_error

# Search Console: домен-ресурс сайту (той самий, що в query_router/english_report)
SC_SITE_URL = "sc-domain:nikvesti.com"

# Типи пошуку Search Console, які реально віддає API (type у searchanalytics.query).
# AI Overviews / AI Mode Google НЕ віддає окремим типом — вони входять у 'web'
# (станом на 2026 у UI-звіті видно, в API — ні). Коли Google додасть тип в API,
# допишемо його сюди й у sc_daily_stats поллється новий розріз без інших змін.
SC_SEARCH_TYPES = ["web", "discover", "googleNews"]

# SC фіналізує дані із затримкою (~2-3 дні, останні дні неповні), тому щоденний
# захват перезбирає трейлінг-вікно й робить upsert — пізні дані виправляють ранні.
SC_RECAPTURE_DAYS = 4

# SC тримає лише ~16 місяців історії — бекфіл глибше просто поверне порожньо.
SC_MAX_HISTORY_DAYS = 480
# Чанк бекфілу SC: менший за GA4-річний, бо SC-звіт важчий і латентніший.
SC_BACKFILL_CHUNK_DAYS = 180

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# Дефолт бекфілу: квартал історії — достатньо для тиждень/місяць-до-місяця
# і не впирається у семплінг GA4 на денному розрізі.
DEFAULT_BACKFILL_DAYS = 90


def is_ready():
    return bot_db.is_configured()


# ---------- Запис ----------

def _top_pages_json(top_pages):
    """top_pages зі send_daily_report — list[(path, title, views, author)].
    Стискаємо у компактний JSON-рядок для JSONB (або None, якщо порожньо)."""
    if not top_pages:
        return None
    items = [
        {"path": path, "title": title, "views": views, "author": author}
        for (path, title, views, author) in top_pages
    ]
    return json.dumps(items, ensure_ascii=False)


async def record_day(date_str, users, sessions, pageviews, top_pages=None):
    """Дописує один день у daily_stats (upsert). Тихо виходить без БД бота.
    Викликається зі щоденного звіту — помилку ковтаємо, щоб не зламати звіт."""
    if not is_ready():
        return
    row = (date_str, users, sessions, pageviews, _top_pages_json(top_pages))
    await asyncio.to_thread(bot_db.upsert_daily_stats, [row])


async def capture_yesterday():
    """Тихо тягне вчорашній день з GA4 (users/sessions/pageviews + топ сторінок)
    і кладе в daily_stats — БЕЗ поста в чат. Це заміна запису, який раніше робив
    щоденний звіт о 09:00: сам звіт прибрано (замість нього — тижневик), але
    пам'ять аналітики має наповнюватись щодня, інакше тижневик і NLQ-тренди
    лишаться без свіжих даних. Тихо виходить без БД бота."""
    if not is_ready():
        return
    client = await asyncio.to_thread(get_ga4_client)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    users, sessions, pageviews = await asyncio.to_thread(get_stats, client, yesterday, yesterday)
    top_pages = await asyncio.to_thread(get_top_pages, client, yesterday, yesterday)
    await record_day(yesterday, users, sessions, pageviews, top_pages)
    # Заодно — денний розріз Search Console по типах (web/discover/googleNews).
    # Окремий try: збій SC (латентність, квота) не має валити захват GA4.
    try:
        await capture_search_console()
    except Exception as e:
        print(f"analytics_store: захват Search Console пропущено — {e}")


# ---------- Search Console: денний розріз по типах пошуку ----------

def _sc_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return gapi_build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


def _fetch_sc_by_type(start_date, end_date):
    """Кліки/покази Search Console по днях×типах у [start_date, end_date].
    Один запит на тип (dimension=date; SC віддає date як YYYY-MM-DD). Тип, який
    API не підтримує (напр. майбутній 'ai'), тихо пропускаємо. Повертає
    list[(date, search_type, clicks, impressions)]."""
    sc = _sc_client()
    rows = []
    for st in SC_SEARCH_TYPES:
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": ["date"],
            "type": st,
            "rowLimit": 1000,  # рядок = день; діапазон бекфілу < 1000 днів
        }
        try:
            resp = sc.searchanalytics().query(siteUrl=SC_SITE_URL, body=body).execute()
        except Exception as e:
            print(f"analytics_store: SC type={st} пропущено — {e}")
            continue
        for r in resp.get("rows", []):
            rows.append((
                r["keys"][0],
                st,
                int(round(r.get("clicks", 0))),
                int(round(r.get("impressions", 0))),
            ))
    return rows


async def capture_search_console():
    """Тихо тягне денний розріз SC по типах за трейлінг-вікно (SC_RECAPTURE_DAYS)
    і робить upsert у sc_daily_stats. Вікно, а не один день, — бо SC фіналізує
    дані із затримкою: повторний захват виправляє неповні свіжі дні. Тихо
    виходить без БД бота або без GA4_CREDENTIALS."""
    if not is_ready() or not GA4_CREDENTIALS:
        return
    end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=SC_RECAPTURE_DAYS)).strftime("%Y-%m-%d")
    rows = await asyncio.to_thread(_fetch_sc_by_type, start, end)
    if rows:
        await asyncio.to_thread(bot_db.upsert_sc_daily_stats, rows)


async def backfill_sc(days=DEFAULT_BACKFILL_DAYS):
    """Бекфіл денного розрізу SC по типах у sc_daily_stats. SC тримає ~16 місяців,
    тому глибину капимо SC_MAX_HISTORY_DAYS; заливаємо чанками. Повертає кількість
    залитих рядків (день×тип). Кидає без БД бота / без GA4_CREDENTIALS."""
    if not is_ready():
        raise RuntimeError("БД бота не налаштована (BOT_DATABASE_URL).")
    if not GA4_CREDENTIALS:
        raise RuntimeError("GA4_CREDENTIALS не задано — нема доступу до Search Console.")
    days = min(days, SC_MAX_HISTORY_DAYS)
    today = datetime.now()
    end = today - timedelta(days=1)
    chunk_start = today - timedelta(days=days)
    total = 0
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=SC_BACKFILL_CHUNK_DAYS - 1), end)
        rows = await asyncio.to_thread(
            _fetch_sc_by_type,
            chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"),
        )
        if rows:
            await asyncio.to_thread(bot_db.upsert_sc_daily_stats, rows)
            total += len(rows)
        chunk_start = chunk_end + timedelta(days=1)
    return total


# ---------- Бекфіл з GA4 ----------

def _fetch_daily_series_from_ga4(start_date, end_date):
    """Одним GA4-запитом (dimension=date) тягне users/sessions/pageviews по днях
    у діапазоні [start_date, end_date]. Повертає list[(date, users, sessions,
    pageviews, None)] — top_pages лишаємо NULL (див. док-стрінг модуля)."""
    client = get_ga4_client()
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="date")],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="sessions"),
            Metric(name="screenPageViews"),
        ],
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
        limit=100000,  # рядок = день; з запасом на будь-який чанк (кап знято)
    )
    response = client.run_report(request)
    rows = []
    for row in response.rows:
        raw = row.dimension_values[0].value  # GA4 формат: YYYYMMDD
        try:
            date_str = datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        rows.append((
            date_str,
            int(row.metric_values[0].value),
            int(row.metric_values[1].value),
            int(row.metric_values[2].value),
            None,
        ))
    return rows


# Розмір чанка бекфілу: GA4 не семплить денний розріз базових метрик, але великі
# діапазони ріжемо на ~річні шматки — стійкіше до лімітів і легше стежити за прогресом.
BACKFILL_CHUNK_DAYS = 365


async def backfill(days=DEFAULT_BACKFILL_DAYS):
    """Бекфіл N останніх днів історії трафіку з GA4 у daily_stats. Великі
    діапазони заливає чанками по BACKFILL_CHUNK_DAYS (можна роками — GA4 тримає
    стандартні звіти весь час існування ресурсу). Повертає кількість залитих днів.
    Кидає, якщо БД бота не налаштована."""
    if not is_ready():
        raise RuntimeError("БД бота не налаштована (BOT_DATABASE_URL).")
    today = datetime.now()
    end = today - timedelta(days=1)                # вчора — останній повний день
    chunk_start = today - timedelta(days=days)
    total = 0
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=BACKFILL_CHUNK_DAYS - 1), end)
        rows = await asyncio.to_thread(
            _fetch_daily_series_from_ga4,
            chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"),
        )
        if rows:
            await asyncio.to_thread(bot_db.upsert_daily_stats, rows)
            total += len(rows)
        chunk_start = chunk_end + timedelta(days=1)
    return total


# ---------- Читання (для NLQ-tool get_traffic_history) ----------

def get_daily_series(start_date, end_date):
    """Щоденна серія з daily_stats у діапазоні [start_date, end_date] включно.
    Синхронна (для виклику з NLQ через to_thread). list[dict] date/users/
    sessions/pageviews, від давніх до свіжих. [] якщо БД бота не налаштована."""
    if not is_ready():
        return []
    return bot_db.query(
        "SELECT to_char(date, 'YYYY-MM-DD') AS date, users, sessions, pageviews "
        "FROM daily_stats WHERE date BETWEEN %s AND %s ORDER BY date",
        (start_date, end_date),
    )


def get_sc_totals_by_type(start_date, end_date):
    """Сума кліків/показів Search Console по типах за період [start,end].
    list[dict] search_type/clicks/impressions, за спаданням кліків. [] без БД.
    Для NLQ: «скільки з пошуку проти Discover», «трафік без Discover»."""
    if not is_ready():
        return []
    return bot_db.query(
        "SELECT search_type, SUM(clicks) AS clicks, SUM(impressions) AS impressions "
        "FROM sc_daily_stats WHERE date BETWEEN %s AND %s "
        "GROUP BY search_type ORDER BY clicks DESC",
        (start_date, end_date),
    )


def get_sc_daily_series(start_date, end_date):
    """Денний розріз SC по типах у [start,end]. list[dict] date/search_type/
    clicks/impressions, від давніх до свіжих. Для трендів/графіків по каналах."""
    if not is_ready():
        return []
    return bot_db.query(
        "SELECT to_char(date, 'YYYY-MM-DD') AS date, search_type, clicks, impressions "
        "FROM sc_daily_stats WHERE date BETWEEN %s AND %s ORDER BY date, search_type",
        (start_date, end_date),
    )


# ---------- Команда /analytics_backfill ----------

async def analytics_backfill_handler(update, context):
    """/analytics_backfill [N] — залити N днів історії трафіку з GA4 (дефолт 90).
    Разова операція: далі daily_stats наповнюється сам щоденним звітом о 09:00."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована (BOT_DATABASE_URL) — нема куди зберігати аналітику."
        )
        return
    days = DEFAULT_BACKFILL_DAYS
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass
    msg = await update.message.reply_text(f"🦊 Заливаю {days} днів історії трафіку з GA4…")
    try:
        count = await backfill(days)
        info = await asyncio.to_thread(bot_db.ping)
        await msg.edit_text(
            f"✅ Залито {count} днів. У пам'яті аналітики: {info['daily_stats']} днів "
            f"({info['daily_stats_oldest']} — {info['daily_stats_newest']}).\n"
            "Далі daily_stats наповнюється сам щоденним звітом о 09:00."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        await notify_error(context.bot, "бекфіл аналітики", e)


# ---------- Команда /sc_backfill ----------

async def sc_backfill_handler(update, context):
    """/sc_backfill [N] — залити N днів денного розрізу Search Console по типах
    (web/discover/googleNews) у sc_daily_stats (дефолт 90; SC тримає ~16 міс).
    Далі наповнюється сам тихим захватом о 09:00 разом із GA4."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована (BOT_DATABASE_URL) — нема куди зберігати аналітику."
        )
        return
    if not GA4_CREDENTIALS:
        await update.message.reply_text(
            "🦊 GA4_CREDENTIALS не задано — нема доступу до Search Console."
        )
        return
    days = DEFAULT_BACKFILL_DAYS
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            pass
    msg = await update.message.reply_text(
        f"🦊 Заливаю розріз Search Console по типах за {days} днів…"
    )
    try:
        count = await backfill_sc(days)
        info = await asyncio.to_thread(bot_db.ping)
        await msg.edit_text(
            f"✅ Залито {count} рядків (день×тип). У пам'яті SC: "
            f"{info['sc_daily_stats']} днів "
            f"({info['sc_daily_stats_oldest']} — {info['sc_daily_stats_newest']}).\n"
            "Типи: web (з AI Overviews), discover, googleNews. "
            "Далі наповнюється сам щоденним захватом о 09:00."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        await notify_error(context.bot, "бекфіл Search Console", e)
