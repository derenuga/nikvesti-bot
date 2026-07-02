import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from handlers.google_analytics import get_ga4_client, get_stats, get_top_pages, BASE_URL
from handlers.gmail import get_unread_emails, get_oldest_unread_hours
from handlers.ai_messages import generate_email_reminder, generate_silence_reminder
from handlers.instagram import send_weekly_instagram_report
from handlers.facebook import send_weekly_facebook_report
from handlers.morning import send_morning_message
from handlers.prozorro import check_prozorro_tenders
from handlers.documents import check_documents
from handlers.competitors import check_competitors
from handlers.english_report import send_english_report
from handlers.law_enforcement import check_law_enforcement
from handlers.energy_outage import check_outage_changes
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kiev")

CHAT_ID = os.environ.get("CHAT_ID")
_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
OUTAGE_DEBUG_CHAT_ID = int(_allowed.split(",")[0]) if _allowed else 56631818
CHANNEL_USERNAME = "nikvesti"

async def send_daily_report(bot):
    client = get_ga4_client()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    yesterday_label = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
    users, sessions, pageviews = get_stats(client, yesterday, yesterday)
    u2, s2, p2 = get_stats(client, day_before, day_before)
    def diff(a, b):
        d = a - b
        return f"+{d}" if d > 0 else str(d)
    top_pages = get_top_pages(client, yesterday, yesterday)
    top_text = "\n".join([
        f'  {i+1}. <a href="{BASE_URL}{path}">{title}</a> — {views}'
        + (f'\n      👤 {author}' if author else '')
        for i, (path, title, views, author) in enumerate(top_pages)
    ])
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"📊 Статистика МикВісті за вчора ({yesterday_label}):\n\n"
            f"👥 Користувачі: {users} ({diff(users, u2)})\n"
            f"🔄 Сесії: {sessions} ({diff(sessions, s2)})\n"
            f"📄 Перегляди: {pageviews} ({diff(pageviews, p2)})\n\n"
            f"🔥 Топ-5 статей:\n{top_text}"
        ),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def check_email(bot, time_of_day):
    try:
        emails = get_unread_emails()
        if not emails:
            return
        hours = get_oldest_unread_hours(emails)
        if hours < 1:
            return
        message = await generate_email_reminder(emails, hours, time_of_day)
        await bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        print("Помилка перевірки пошти: " + str(e))

async def weekly_instagram(bot):
    await send_weekly_instagram_report(bot, CHAT_ID)

async def weekly_facebook(bot):
    await send_weekly_facebook_report(bot, CHAT_ID)

async def check_prozorro(bot):
    await check_prozorro_tenders(bot)

async def morning_greeting(bot):
    await send_morning_message(bot, CHAT_ID)

# Ескалація нагадувань про мовчання каналу: перше — після 2 год тиші,
# наступні — не раніше ніж через 1, 2, 4 (і далі по 4) години після попереднього.
# Новий пост у каналі скидає лічильник.
_silence_reminders = {"last_at": None, "count": 0}

async def check_channel_silence(bot, last_channel_post_time):
    try:
        # Вікно 10:00–18:00 і будні рахуємо за Києвом — сервер Railway працює в UTC
        kyiv_now = datetime.now(KYIV_TZ)
        if kyiv_now.weekday() >= 5:
            return
        if kyiv_now.hour < 10 or kyiv_now.hour >= 18:
            return
        # Тривалість тиші рахуємо в наївному серверному часі —
        # та сама шкала, що й datetime.now() в bot.py (last_channel_post_time)
        now = datetime.now()
        last_post = last_channel_post_time.get("time")
        if not last_post:
            return
        if _silence_reminders["last_at"] and last_post > _silence_reminders["last_at"]:
            _silence_reminders["last_at"] = None
            _silence_reminders["count"] = 0
        silence_hours = (now - last_post).total_seconds() / 3600
        if silence_hours >= 2:
            if _silence_reminders["last_at"]:
                cooldown_hours = min(2 ** (_silence_reminders["count"] - 1), 4)
                since_last = (now - _silence_reminders["last_at"]).total_seconds() / 3600
                if since_last < cooldown_hours:
                    return
            text = await generate_silence_reminder(CHANNEL_USERNAME, silence_hours)
            await bot.send_message(chat_id=CHAT_ID, text=text)
            _silence_reminders["last_at"] = now
            _silence_reminders["count"] += 1
    except Exception as e:
        print("Помилка перевірки мовчання каналу: " + str(e))

async def run_check_documents(bot):
    await check_documents(bot)

async def run_check_competitors(bot):
    await check_competitors(bot)

async def run_check_law_enforcement(bot):
    await check_law_enforcement(bot)

async def monthly_english_report(bot):
    """Місячний звіт EN-версії — запускається в останній день місяця о 19:00.
    build_english_report сам визначає, що це останній день, і звітує за поточний місяць."""
    await send_english_report(bot, CHAT_ID)

def setup_scheduler(bot, last_channel_post_time=None):
    if last_channel_post_time is None:
        last_channel_post_time = {"time": datetime.now()}
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(send_daily_report, "cron", hour=9, minute=0, args=[bot])
    scheduler.add_job(check_email, "cron", hour=13, minute=0, args=[bot, "afternoon"])
    scheduler.add_job(check_email, "cron", hour=16, minute=50, args=[bot, "evening"])
    scheduler.add_job(weekly_instagram, "cron", day_of_week="sun", hour=18, minute=0, args=[bot])
    scheduler.add_job(weekly_facebook, "cron", day_of_week="sun", hour=15, minute=0, args=[bot])
    scheduler.add_job(morning_greeting, "cron", hour=8, minute=15, args=[bot])
    scheduler.add_job(check_channel_silence, "cron", minute="*/30", args=[bot, last_channel_post_time])
    scheduler.add_job(check_prozorro, "cron", minute=0, args=[bot])
    scheduler.add_job(run_check_documents, "cron", minute=30, args=[bot])
    scheduler.add_job(run_check_competitors, "cron", minute=15, args=[bot])
    # Правоохоронці — три рази на день: 10:00, 13:00, 16:00
    scheduler.add_job(run_check_law_enforcement, "cron", hour=10, minute=0, args=[bot])
    scheduler.add_job(run_check_law_enforcement, "cron", hour=13, minute=0, args=[bot])
    scheduler.add_job(run_check_law_enforcement, "cron", hour=16, minute=0, args=[bot])
    # Місячний EN-звіт: останній день місяця о 19:00
    # day="last" — APScheduler cron syntax для останнього дня місяця
    scheduler.add_job(monthly_english_report, "cron", day="last", hour=19, minute=0, args=[bot])
    # Моніторинг змін у графіку відключень — кожні 5 хвилин, в особистий чат для налагодження
    scheduler.add_job(check_outage_changes, "interval", minutes=5, args=[bot, OUTAGE_DEBUG_CHAT_ID])
    scheduler.start()
    return scheduler
