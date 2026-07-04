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

from handlers import bot_db
from handlers.google_analytics import get_ga4_client, get_stats, get_top_pages, GA4_PROPERTY_ID
from handlers.notifier import notify_error

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
        limit=400,
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


async def backfill(days=DEFAULT_BACKFILL_DAYS):
    """Бекфіл N останніх днів історії трафіку з GA4 у daily_stats.
    Повертає кількість залитих днів. Кидає, якщо БД бота не налаштована."""
    if not is_ready():
        raise RuntimeError("БД бота не налаштована (BOT_DATABASE_URL).")
    today = datetime.now()
    start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end = (today - timedelta(days=1)).strftime("%Y-%m-%d")  # вчора — останній повний день
    rows = await asyncio.to_thread(_fetch_daily_series_from_ga4, start, end)
    if rows:
        await asyncio.to_thread(bot_db.upsert_daily_stats, rows)
    return len(rows)


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
