import asyncio
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_ERROR
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
from handlers.traffic_spikes import check_traffic_spikes
from handlers.builder_monitor import check_builder_staleness
from handlers.archive_mirror import run_archive_sync
from handlers.budget_snapshots import run_snapshot_check
from handlers.entity_layer import sync_entities_incremental
from handlers.notifier import notify_error
from handlers.ai_usage import send_monthly_ai_cost
from handlers.weekly_digest import send_weekly_digest
from handlers import analytics_store
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kiev")

CHAT_ID = os.environ.get("CHAT_ID")
_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
OUTAGE_DEBUG_CHAT_ID = int(_allowed.split(",")[0]) if _allowed else 56631818
CHANNEL_USERNAME = "nikvesti"

async def send_daily_report(bot):
    client = await asyncio.to_thread(get_ga4_client)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    yesterday_label = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
    users, sessions, pageviews = await asyncio.to_thread(get_stats, client, yesterday, yesterday)
    u2, s2, p2 = await asyncio.to_thread(get_stats, client, day_before, day_before)
    def diff(a, b):
        d = a - b
        return f"+{d}" if d > 0 else str(d)
    top_pages = await asyncio.to_thread(get_top_pages, client, yesterday, yesterday)
    # Осідання в пам'ять аналітики (Postgres): вчорашній день з топом сторінок.
    # Без зайвого GA4-запиту — цифри вже зібрані. Помилку ковтаємо, щоб збій
    # БД бота не зламав сам звіт у чат.
    try:
        await analytics_store.record_day(yesterday, users, sessions, pageviews, top_pages)
    except Exception as e:
        print(f"analytics_store: не вдалось зберегти день — {e}")
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

async def capture_daily_stats(bot):
    """Тихий щоденний захват вчорашньої аналітики в daily_stats (без поста).
    Щоденний звіт у чат прибрано (замість нього — тижневик), але пам'ять
    аналітики має наповнюватись щодня — інакше тижневик і NLQ-тренди без даних."""
    try:
        await analytics_store.capture_yesterday()
    except Exception as e:
        print(f"Помилка захвату щоденної аналітики: {e}")
        await notify_error(bot, "захват щоденної аналітики", e)


async def weekly_fox_digest(bot):
    """Тижневик Лиса — понеділок 09:30, у чат редакції."""
    await send_weekly_digest(bot, CHAT_ID)


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
        await notify_error(bot, "перевірка пошти", e)

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
        await notify_error(bot, "перевірка мовчання каналу", e)

async def run_check_documents(bot):
    try:
        await check_documents(bot)
    except Exception as e:
        print("Помилка перевірки документів: " + str(e))
        await notify_error(bot, "документи органів влади", e)

async def run_check_competitors(bot):
    try:
        await check_competitors(bot)
    except Exception as e:
        print("Помилка перевірки конкурентів: " + str(e))
        await notify_error(bot, "новини конкурентів", e)

async def run_check_law_enforcement(bot):
    try:
        await check_law_enforcement(bot)
    except Exception as e:
        print("Помилка перевірки правоохоронців: " + str(e))
        await notify_error(bot, "правоохоронні органи", e)

async def monthly_english_report(bot):
    """Місячний звіт EN-версії — запускається в останній день місяця о 19:00.
    build_english_report сам визначає, що це останній день, і звітує за поточний місяць."""
    await send_english_report(bot, CHAT_ID)

async def monthly_ai_cost(bot):
    """1-го числа Лис звітує Олегу про вартість AI за попередній місяць."""
    await send_monthly_ai_cost(bot, OUTAGE_DEBUG_CHAT_ID)

def _on_job_error(bot, event):
    """Слухач APScheduler: ловить будь-який виняток, що вилетів із задачі
    назовні (не був заглушений всередині), і шле алерт адміну. Викликається
    синхронно в loop-потоці — плануємо корутину через create_task."""
    job_id = getattr(event, "job_id", "?")
    exc = getattr(event, "exception", None) or Exception("невідома помилка")
    try:
        asyncio.get_event_loop().create_task(
            notify_error(bot, f"планувальник ({job_id})", exc)
        )
    except Exception as e:
        print(f"scheduler: не вдалось запланувати алерт — {e}")


def setup_scheduler(bot, last_channel_post_time=None):
    if last_channel_post_time is None:
        last_channel_post_time = {"time": datetime.now()}
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")
    scheduler.add_listener(lambda event: _on_job_error(bot, event), EVENT_JOB_ERROR)
    # Щоденний звіт 09:00 у чат прибрано — замість нього тижневик (нижче).
    # О 09:00 лишається тихий захват вчорашнього дня в daily_stats (без поста),
    # щоб пам'ять аналітики наповнювалась і тижневик мав що порівнювати.
    scheduler.add_job(capture_daily_stats, "cron", hour=9, minute=0, args=[bot])
    # Тижневик Лиса — понеділок 09:30 (тиждень до тижня, топ, тендери, AI)
    scheduler.add_job(weekly_fox_digest, "cron", day_of_week="mon", hour=9, minute=30, args=[bot])
    scheduler.add_job(check_email, "cron", hour=13, minute=0, args=[bot, "afternoon"])
    scheduler.add_job(check_email, "cron", hour=16, minute=50, args=[bot, "evening"])
    scheduler.add_job(weekly_instagram, "cron", day_of_week="sun", hour=18, minute=0, args=[bot])
    scheduler.add_job(weekly_facebook, "cron", day_of_week="sun", hour=15, minute=0, args=[bot])
    scheduler.add_job(morning_greeting, "cron", hour=8, minute=15, args=[bot])
    scheduler.add_job(check_channel_silence, "cron", minute="*/30", args=[bot, last_channel_post_time])
    scheduler.add_job(check_prozorro, "cron", minute=0, args=[bot])
    scheduler.add_job(run_check_documents, "cron", minute=30, args=[bot])
    # Конкуренти — раз на 3 год (замість щогодини). Слоти включають 07:15:
    # це перша денна перевірка після нічного вікна (00:00–07:00), яка віддає
    # нічний буфер одним ранковим дайджестом. Нічні слоти (01:15, 04:15) не
    # шлють одразу, а складають у буфер до 07:15.
    scheduler.add_job(run_check_competitors, "cron", hour="1,4,7,10,13,16,19,22", minute=15, args=[bot])
    # Правоохоронці — три рази на день: 10:00, 13:00, 16:00
    scheduler.add_job(run_check_law_enforcement, "cron", hour=10, minute=0, args=[bot])
    scheduler.add_job(run_check_law_enforcement, "cron", hour=13, minute=0, args=[bot])
    scheduler.add_job(run_check_law_enforcement, "cron", hour=16, minute=0, args=[bot])
    # Місячний EN-звіт: останній день місяця о 19:00
    # day="last" — APScheduler cron syntax для останнього дня місяця
    scheduler.add_job(monthly_english_report, "cron", day="last", hour=19, minute=0, args=[bot])
    # Моніторинг змін у графіку відключень — кожні 5 хвилин, в особистий чат для налагодження
    scheduler.add_job(check_outage_changes, "interval", minutes=5, args=[bot, OUTAGE_DEBUG_CHAT_ID])
    # Детектор сплесків трафіку — кожні 30 хв (:05 і :35, щоб не збігатись
    # з тендерами/конкурентами/документами на :00/:15/:30)
    scheduler.add_job(check_traffic_spikes, "cron", minute="5,35", args=[bot])
    # Вартість AI за попередній місяць — 1-го числа о 10:00, в особистий чат Олегу
    scheduler.add_job(monthly_ai_cost, "cron", day=1, hour=10, minute=0, args=[bot])
    # Монітор білдера головної — у робочі години (9–21 Києвом), :10 і :40,
    # щоб не збігатися з рештою задач на :00/:05/:15/:30/:35
    scheduler.add_job(check_builder_staleness, "cron", hour="9-21", minute="10,40", args=[bot])
    # Інкрементальний sync дзеркала архіву — :50 (вільна хвилина в розкладі);
    # тихо пропускається, поки BOT_DATABASE_URL не задано або бекфіл не зроблено
    scheduler.add_job(run_archive_sync, "cron", minute=50, args=[bot])
    # Інкремент сутнісного шару — :55, після синку дзеркала (опт-ін /entity_increment_on)
    scheduler.add_job(sync_entities_incremental, "cron", minute=55, args=[bot])
    # Місячні снапшоти бюджету зі сторінки міськради — щодня об 11:20
    # (публікують на початку місяця нерегулярно; перевірка дешева, тиха,
    # коли нового немає; тихо пропускається без BOT_DATABASE_URL)
    scheduler.add_job(run_snapshot_check, "cron", hour=11, minute=20, args=[bot])
    scheduler.start()
    return scheduler
