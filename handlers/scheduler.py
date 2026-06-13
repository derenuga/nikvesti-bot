import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from handlers.google_analytics import get_ga4_client, get_stats, get_top_pages, BASE_URL
from handlers.gmail import get_unread_emails, get_oldest_unread_hours
from handlers.ai_messages import generate_email_reminder
from datetime import datetime, timedelta

CHAT_ID = os.environ.get("CHAT_ID")

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
        for i, (path, title, views) in enumerate(top_pages)
    ])

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"Статистика МикВісті за вчора ({yesterday_label}):\n\n"
            f"Користувачі: {users} ({diff(users, u2)})\n"
            f"Сесії: {sessions} ({diff(sessions, s2)})\n"
            f"Перегляди: {pageviews} ({diff(pageviews, p2)})\n\n"
            f"Топ-5 статей:\n{top_text}"
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

def setup_scheduler(bot):
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_job(send_daily_report, "cron", hour=9, minute=0, args=[bot])
    scheduler.add_job(check_email, "cron", hour=13, minute=0, args=[bot, "afternoon"])
    scheduler.add_job(check_email, "cron", hour=16, minute=50, args=[bot, "evening"])
    scheduler.start()
    return scheduler
