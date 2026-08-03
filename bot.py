import asyncio
import re
import os
import urllib.request
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, MessageReactionHandler, TypeHandler, filters
from handlers.google_analytics import analytics_handler
from handlers.analytics_store import analytics_backfill_handler, sc_backfill_handler
from handlers.weekly_digest import weekly_handler
from handlers.report_reminders import reports_check_handler
from handlers.social_store import social_capture_handler, social_backfill_fb_handler
from handlers.social_import import social_import_sheet_handler
from handlers.social_sheet import sheet_snapshot_handler, sheet_backfill_handler, sheet_format_handler, youtube_backfill_handler, tiktok_auth_handler
from handlers.social_sheet_legacy import sheet_migrate_legacy_handler
from handlers.scheduler import setup_scheduler, send_daily_report, check_email
from handlers.instagram import instagram_handler, send_weekly_instagram_report
from handlers.facebook import facebook_handler, send_weekly_facebook_report
from handlers.morning import morning_handler, send_morning_message
from handlers.prozorro import check_prozorro_tenders, diagnose_offset_jump, confirm_offset_jump
from handlers.documents import check_documents, test_documents, rebaseline_documents
from handlers.competitors import check_competitors
from handlers.law_enforcement import check_law_enforcement
from handlers.stat import stat_handler, stat_forget_handler
from handlers.query_router import handle_natural_language_query, reset_dialog
from handlers.reactions import handle_message_reaction
from handlers.english_report import english_report_handler
from handlers.energy_outage import outage_handler, outage_probe_handler, outage_export_handler, outage_geocode_handler
from handlers.traffic_spikes import traffic_handler
from handlers.telegram_stats import index_channel_post, backfill_channel_index
from handlers.ai_usage import aicost_handler
from handlers.db import dbtest_handler, dbquery_handler
from handlers.archive_mirror import (
    archive_backfill_handler, archive_status_handler, archive_sample_handler,
    archive_stop_handler, archive_report_handler, nora_sql_handler,
    nora_resync_handler, articles_backfill_handler,
)
from handlers.dossier import dossier_handler
from handlers.nora_article import nora_article_handler
from handlers.entity_layer import (
    entity_estimate_handler, entity_backfill_handler,
    entity_status_handler, entity_resume_handler, entity_recover_handler,
    entity_increment_on_handler, entity_increment_off_handler, entity_export_handler,
    entity_dedup_handler, entity_export_links_handler, entity_resync_handler,
)
from handlers.entity_merge import (
    entity_scale_handler, entity_classes_handler, entity_find_handler,
    entity_merge_handler, entity_merge_callback, entity_find_callback,
    entity_merge_log_handler, entity_unmerge_handler,
)
from handlers.entity_junk import (
    entity_junk_handler, entity_junk_callback, entity_junk_undo_handler,
    entity_docs_canon_handler, entity_docs_canon_callback,
    entity_org_forms_handler, entity_org_forms_callback, cards_fix_callback,
    nora_reindex_handler,
    entity_org_dupes_handler, entity_org_dupes_callback,
)
from handlers.entity_roles import (
    roles_handler, roles_dedup_handler, roles_canon_handler, roles_bulk_handler,
    roles_rename_handler, roles_forget_handler, roles_org_handler,
    roles_outliers_handler, roles_who_handler,
    roles_audit_handler, roles_merge_handler, roles_merge_callback,
    roles_fix_document, roles_fix_callback,
    roles_pair_callback, roles_org_callback, roles_bulk_callback,
)
from handlers.promises import (
    promises_handler, promise_show_handler, promise_test_handler,
    promise_estimate_handler, promise_scan_handler, promise_scan_callback,
    promise_resume_handler, promise_checked_handler, promise_forget_handler,
    promise_restore_handler, promise_retest_handler, promise_eval_handler,
    promise_status_handler, promise_increment_on_handler,
    promise_increment_off_handler, promise_prune_handler,
    promise_prune_callback, promise_prune_undo_handler,
    promise_prune_who_handler, promise_prune_who_callback,
    promise_dupes_handler, promise_merge_handler,
    promise_export_handler, promises_fix_callback,
    promise_notdupe_handler,
)
from handlers.tags_wikidata import tags_export_handler, tags_wiki_handler, tags_wiki_reset_handler
from handlers.budget_revisions import budget_load_handler, budget_status_handler, budget_headline_handler, budget_package_handler, budget_date_handler
from handlers.budget_snapshots import budget_execution_handler, budget_snapshot_check_handler, budget_execution_test_handler, budget_snapshot_reset_handler
from handlers.knowledge_graph import kg_handler
from handlers.builder_monitor import builder_handler, builder_test_handler, is_builder_nudge
from handlers.fb_missing import fbmissing_handler, fbmissing_test_handler
from handlers.news_archive import news_back_callback, news_select_callback, BACK_CALLBACK_DATA, SELECT_CALLBACK_PREFIX
from handlers.viber_mirror import mirror_channel_post, viber_setup_handler, viber_test_handler
from handlers.webapp import (start_webapp, team_handler, todo_handler,
                            todo_today_callback)
from handlers.team_matching import (
    match_test_handler, match_scan_handler, match_estimate_handler,
    match_backfill_handler, match_tg_handler, match_cards_handler,
    match_requeue_handler,
)
from handlers.team_kpi import kpi_debug, set_user_link
from handlers import team_contacts, team_roster, team_tasks
from handlers.helpers import escape_html
from handlers.notifier import notify_error
from handlers.usage_report import usage_handler, display_name
from handlers import storage

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
CHANNEL_USERNAME = "nikvesti"

ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

last_channel_post_time = {"time": datetime.now()}

async def check_allowed(update, context):
    """Захист від спаму через глобальний пошук Telegram: блокує приватні повідомлення
    від користувачів поза ALLOWED_USER_IDS. Якщо змінна не задана — пускає всіх (дефолт)."""
    if not ALLOWED_USER_IDS:
        return
    if update.effective_chat and update.effective_chat.type == "private":
        if update.effective_user and update.effective_user.id not in ALLOWED_USER_IDS:
            if update.message:
                await update.message.reply_text("⛔ Доступ заборонено.")
            raise ApplicationHandlerStop

async def track_usage(update, context):
    """Тихий облік користування ботом для щоденного звіту адміну
    (handlers/usage_report.py): тут рахуються КОМАНДИ від людей; NLQ-питання
    і tools пише query_router, беки — news_archive. Ніколи не блокує обробку
    і не шумить — будь-яка помилка ковтається в лог."""
    try:
        msg = update.message
        user = update.effective_user
        if not msg or not user or user.is_bot:
            return
        text = msg.text or msg.caption or ""
        if not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lstrip("/").lower()
        if not command:
            return
        await asyncio.to_thread(
            storage.record_usage_command, user.id, display_name(user), command)
    except Exception as e:
        print(f"usage: не вдалось записати команду — {e}")

async def channel_post_handler(update, context):
    if update.channel_post and update.channel_post.chat.username == CHANNEL_USERNAME:
        last_channel_post_time["time"] = datetime.now()
        # Індекс для /stat: article_id → message_id (запис у storage — в потоці)
        await asyncio.to_thread(index_channel_post, update.channel_post)
        # Дзеркало у Viber (тихо вимкнено без VIBER_AUTH_TOKEN; репости/службові
        # пропускаються всередині). Помилку ковтаємо + алерт, щоб не зламати індекс.
        try:
            await mirror_channel_post(context.bot, update.channel_post)
        except Exception as e:
            print(f"viber mirror: не вдалось задзеркалити пост — {e}")
            await notify_error(context.bot, "дзеркало Viber", e)

async def group_reply_to_bot(update, context):
    """Reply на повідомлення бота в чаті редакції — теж іде в Intent Router,
    але тільки для ALLOWED_USER_IDS (групу check_allowed не блокує, тут перевірка інлайн)
    і тільки в чаті редакції — щоб не реагувати на реплаї в каналі тендерів/документів."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    if str(update.effective_chat.id) != str(CHAT_ID):
        return
    if msg.reply_to_message.from_user.id != context.bot.id:
        return
    # Нагадування про білдер («хто оновить головну?») — це заклик до редакції,
    # а не питання до Лиса. Reply на нього («хто обновит?») не має йти в NLQ.
    if is_builder_nudge(msg.reply_to_message.text or msg.reply_to_message.caption):
        print(f"NLQ group reply: пропущено (reply на нагадування білдера) user_id={update.effective_user.id if update.effective_user else None}")
        return
    # Лог для розслідування group-reply leak (NLQ-дока, "Відомі проблеми"):
    # фіксуємо кожен reply на бота, щоб при повторенні інциденту була фактура
    user_id = update.effective_user.id if update.effective_user else None
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        print(f"NLQ group reply: відхилено user_id={user_id} chat_id={update.effective_chat.id}")
        return
    print(f"NLQ group reply: прийнято user_id={user_id} chat_id={update.effective_chat.id}")
    await handle_natural_language_query(update, context)

async def start(update, context):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту\n"
        "/report — звіт в групу\n/checkmail — перевірити пошту\n"
        "/instagram — статистика Instagram\n/igreport — тижневий Instagram звіт в групу\n"
        "/facebook — статистика Facebook\n/fbreport — тижневий Facebook звіт в групу\n"
        "/morning — ранкове привітання\n"
        "/prozorro — перевірити тендери Прозорро\n"
        "/documents — перевірити нові документи міськради\n"
        "/documents_test — тестовий пост документів в канал\n"
        "/competitors — перевірити новини конкурентів\n"
        "/law — перевірити новини правоохоронних органів\n"
        "/stat <url> — статистика матеріалу (Facebook + GA4)\n"
        "/english — місячний звіт англійської версії сайту\n"
        "/traffic — хто зараз на сайті + типовий трафік для цієї години\n"
        "/dossier <тема> — історія питання з 17-річного архіву\n"
        "/reset — забути контекст розмови з Лисом"
    )

async def status(update, context):
    await update.message.reply_text("Бот працює. Все під контролем.")

async def kpi_debug_handler(update, context):
    """/kpi_debug <імʼя/прізвище> — чому у людини такий факт KPI: привʼязка до
    users.id + розкладка виходу за місяць (тип × власний × owner_id)."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /kpi_debug <прізвище>, напр. /kpi_debug Бойченко")
        return
    report = await asyncio.to_thread(kpi_debug, " ".join(context.args))
    await update.message.reply_text(report, parse_mode="HTML", disable_web_page_preview=True)

async def kpi_link_handler(update, context):
    """/kpi_link <імʼя/прізвище> <users.id> — закріпити правильний users.id за
    людиною (перебиває пошук за ПІБ; для дублів акаунтів у users)."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if len(context.args) < 2 or not context.args[-1].isdigit():
        await update.message.reply_text("Використання: /kpi_link <прізвище> <users.id>, напр. /kpi_link Бойченко 44")
        return
    site_id = int(context.args[-1])
    name_q = " ".join(context.args[:-1])
    from handlers.team_projects import _norm_name
    person = next((p for p in team_roster.ROSTER if _norm_name(name_q) in _norm_name(p)), None)
    if not person:
        await update.message.reply_text(f"Не знайшла «{name_q}» у ростері.")
        return
    await asyncio.to_thread(set_user_link, person, site_id, display_name(update.effective_user))
    await update.message.reply_text(
        f"📌 {person} → users.id {site_id}. KPI перерахується (кеш ~5 хв).")

# IP, які KEY4 додав у whitelist БД сайту (звірка для /myip).
DB_WHITELIST_IPS = {"162.220.234.241", "162.220.234.242", "152.5.180.241"}

def _fetch_outbound_ip():
    """Реальний вихідний IP контейнера — щоб звірити з whitelist БД (діагностика timeout)."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None

async def myip(update, context):
    """/myip — вихідний IP Railway + чи він у whitelist БД сайту (діагностика конекту)."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    msg = await update.message.reply_text("🦊 Дивлюсь свій вихідний IP…")
    ip = await asyncio.to_thread(_fetch_outbound_ip)
    if not ip:
        await msg.edit_text("Не вдалось визначити вихідний IP (усі сервіси не відповіли).")
        return
    in_wl = ip in DB_WHITELIST_IPS
    verdict = (
        "✅ цей IP у whitelist БД — тоді причина timeout інша (порт/SSL)"
        if in_wl else
        "❌ цього IP НЕМАЄ у whitelist БД — саме тому 185.149.41.55 дропає конект (timeout)"
    )
    await msg.edit_text(
        f"Вихідний IP Railway: <b>{ip}</b>\n"
        f"Whitelist БД: {', '.join(sorted(DB_WHITELIST_IPS))}\n{verdict}",
        parse_mode="HTML",
    )

async def stat_backfill(update, context):
    """Разовий бэкфіл індексу постів каналу для /stat: гортає історію t.me/s
    і зберігає article_id → message_id, щоб старі матеріали знаходились миттєво.
    /stat_backfill [місяців] — за замовчуванням уся доступна історія."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    months = None
    if context.args:
        try:
            months = int(context.args[0])
        except ValueError:
            pass
    scope = f"за {months} міс" if months else "усю історію"
    msg = await update.message.reply_text(
        f"⏳ Індексую {scope} каналу @nikvesti для /stat... це може зайняти кілька хвилин."
    )
    try:
        indexed, seen, pages = await asyncio.to_thread(backfill_channel_index, months)
        await msg.edit_text(
            f"✅ Готово: проіндексовано {indexed} статей із {seen} постів "
            f"({pages} сторінок). Тепер /stat знаходить їх миттєво."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Помилка бэкфілу: {e}")

async def reset_cmd(update, context):
    """Скидання пам'яті діалогу з Лисом — нова розмова з чистого аркуша."""
    had_dialog = reset_dialog(update.effective_chat.id, update.effective_user.id)
    if had_dialog:
        await update.message.reply_text("🦊 Гаразд, забув попередню розмову. Питайте з чистого аркуша.")
    else:
        await update.message.reply_text("🦊 Та ми й не розмовляли — пам'ять і так чиста.")

async def report(update, context):
    await send_daily_report(context.bot)
    await update.message.reply_text("Звіт надіслано в групу!")

async def checkmail(update, context):
    await check_email(context.bot, "afternoon")
    await update.message.reply_text("Перевірив пошту!")

async def igreport(update, context):
    await send_weekly_instagram_report(context.bot, CHAT_ID)
    await update.message.reply_text("Звіт Instagram надіслано в групу!")

async def fbreport(update, context):
    await send_weekly_facebook_report(context.bot, CHAT_ID)
    await update.message.reply_text("Звіт Facebook надіслано в групу!")

async def prozorro_check(update, context):
    await check_prozorro_tenders(context.bot)
    await update.message.reply_text("Перевірив Прозорро!")

async def prozorro_test_jump(update, context):
    days_ago = 14
    if context.args:
        try:
            days_ago = int(context.args[0])
        except ValueError:
            pass
    await diagnose_offset_jump(context.bot, update.message.chat_id, days_ago)

async def prozorro_confirm_jump(update, context):
    days_ago = 14
    if context.args:
        try:
            days_ago = int(context.args[0])
        except ValueError:
            pass
    offset = await confirm_offset_jump(days_ago)
    await update.message.reply_text(f"Offset збережено: {offset}")

async def prozorro_reset_tender(update, context):
    if not context.args:
        await update.message.reply_text("Використання: /prozorro_reset_tender UA-2026-...")
        return
    tender_id = context.args[0]
    success = storage.reset_tender_taken(tender_id)
    if success:
        await update.message.reply_text(f"Тендер {tender_id} розблоковано — можна ставити реакцію знову.")
    else:
        await update.message.reply_text(f"Тендер {tender_id} не знайдено в storage.")

async def documents_check(update, context):
    await check_documents(context.bot)
    await update.message.reply_text("Перевірив документи!")

async def documents_test_cmd(update, context):
    sent = await test_documents(context.bot)
    await update.message.reply_text(f"Тестові пости надіслано в канал: {sent} джерела.")

async def documents_rebaseline_cmd(update, context):
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    report = await rebaseline_documents(context.bot)
    await update.message.reply_text(report)

async def competitors_check(update, context):
    await check_competitors(context.bot)
    await update.message.reply_text("Перевірив новини конкурентів!")

async def law_check(update, context):
    await check_law_enforcement(context.bot)
    await update.message.reply_text("Перевірив правоохоронні органи!")

async def post_init(application):
    setup_scheduler(application.bot, last_channel_post_time)
    # Mini App «Команда»: aiohttp у тому ж event loop. Збій веб-шару не має
    # класти бота — ловимо все і лишаємо тільки алерт у лог.
    try:
        await start_webapp(application)
    except Exception as e:
        print(f"webapp: не стартував — {e}")


async def shared_contact_handler(update, context):
    """Переслана в приват картка контакту → у базу редакції (Олег, 29.07).

    Чат бот НЕ сканує — це свідоме рішення: у базу потрапляє тільки те, що
    людина сама надіслала. Кладемо «як є», бо ПІБ і посаду все одно правлять
    руками: у телефонах записано хто як («Луков мер», «Ігор Микол.»).
    """
    msg = update.effective_message
    user = update.effective_user
    person = team_roster.resolve_person(user.id, user.username) if user else None
    if not person:
        return                      # чужинцю база редакції не відкривається
    c = msg.contact
    try:
        # vcard, а не username: у Telegram-контакту поля username НЕМАЄ
        # взагалі (є phone_number, first_name, last_name, user_id, vcard).
        # Зате у vCard з адресної книги телефона часто лежать усі номери,
        # організація і посада — половина того, що ми просили дозаповнити.
        saved, is_new = await asyncio.to_thread(
            team_contacts.save_shared_contact, person,
            c.first_name, c.last_name, c.phone_number, getattr(c, "vcard", None),
        )
    except Exception as e:
        await msg.reply_text(f"🦊 Не вдалось зберегти контакт: {e}")
        return
    await _reply_saved_contact(msg, saved, is_new)


async def _reply_saved_contact(msg, saved, is_new):
    verb = "Записав" if is_new else "Такий номер уже був — доповнив картку"
    lines = [f"🦊 {verb}: <b>{escape_html(saved['name'])}</b>"]
    if saved.get("role"):
        lines.append(escape_html(saved["role"]))
    for num in saved.get("phones") or []:
        lines.append(escape_html(num))
    lines.append("")
    lines.append("Додай теми — за ними потім і шукатимуть."
                 if saved.get("role") else
                 "Додай посаду й теми — за ними потім і шукатимуть.")
    await msg.reply_text(
        "\n".join(lines), parse_mode="HTML",
        # Кнопка веде ОДРАЗУ в базу, а не на головну: людина щойно надіслала
        # контакт, і дозаповнювати вона йде саме його (Олег, 29.07)
        reply_markup=team_tasks._open_app_markup("Відкрити контакти", "contacts"),
    )


# Номер у 9–15 цифр — це телефон, а не рік і не id статті. Перевіряємо саме
# кількість цифр, а не «схожість»: інакше «що було 2026-07-29» ловилось би як
# контакт. Не схоже на номер — тихо віддаємо повідомлення Лису, як і раніше.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{7,}\d")


async def maybe_contact_text_handler(update, context):
    """«А якщо абонента немає в телеграмі?» (Олег, 29.07) — тоді можна просто
    надіслати номер текстом: «Ігор Луков +380501112233».

    Хендлер стоїть ПЕРЕД природномовним шаром, тому мусить бути обережним:
    усе, що не схоже на телефон, віддаємо далі руками, а не ковтаємо."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    match = _PHONE_RE.search(text)
    digits = "".join(c for c in (match.group(0) if match else "") if c.isdigit())
    if not match or not (9 <= len(digits) <= 15):
        await handle_natural_language_query(update, context)
        return
    user = update.effective_user
    person = team_roster.resolve_person(user.id, user.username) if user else None
    if not person:
        await handle_natural_language_query(update, context)
        return
    name = (text[:match.start()] + " " + text[match.end():]).strip(" ,-–—:")
    try:
        saved, is_new = await asyncio.to_thread(
            team_contacts.save_shared_contact, person,
            name or "Без імені", "", match.group(0).strip(), None,
        )
    except Exception as e:
        await msg.reply_text(f"🦊 Не вдалось зберегти контакт: {e}")
        return
    await _reply_saved_contact(msg, saved, is_new)

def main():
    # concurrent_updates: без цього PTB обробляє апдейти строго по черзі,
    # і тап по inline-кнопці висить у "Loading", доки не дожується попередній
    # апдейт (наприклад, NLQ tool-цикл на 10-30 сек). Handlers написані
    # незалежними (стан — у storage під lock), паралельність безпечна.
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, check_allowed), group=-1)
    # Облік команд — у тій самій групі ПІСЛЯ check_allowed: заблоковані
    # чужинці не рахуються (ApplicationHandlerStop зупиняє групу до нас).
    app.add_handler(TypeHandler(Update, track_usage), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("usage", usage_handler))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("stat_backfill", stat_backfill))
    app.add_handler(CommandHandler("aicost", aicost_handler))
    app.add_handler(CommandHandler("dbtest", dbtest_handler))
    app.add_handler(CommandHandler("dbquery", dbquery_handler))
    app.add_handler(CommandHandler("dossier", dossier_handler))
    app.add_handler(CommandHandler("archive_backfill", archive_backfill_handler))
    app.add_handler(CommandHandler("articles_backfill", articles_backfill_handler))
    app.add_handler(CommandHandler("archive_stop", archive_stop_handler))
    app.add_handler(CommandHandler("archive_sample", archive_sample_handler))
    app.add_handler(CommandHandler("archive_status", archive_status_handler))
    app.add_handler(CommandHandler("archive_report", archive_report_handler))
    app.add_handler(CommandHandler("nora_sql", nora_sql_handler))
    app.add_handler(CommandHandler("nora_article", nora_article_handler))
    app.add_handler(CommandHandler("nora_resync", nora_resync_handler))
    app.add_handler(CommandHandler("entity_estimate", entity_estimate_handler))
    app.add_handler(CommandHandler("entity_backfill", entity_backfill_handler))
    app.add_handler(CommandHandler("entity_status", entity_status_handler))
    app.add_handler(CommandHandler("entity_resume", entity_resume_handler))
    app.add_handler(CommandHandler("entity_resync", entity_resync_handler))
    app.add_handler(CommandHandler("entity_recover", entity_recover_handler))
    app.add_handler(CommandHandler("entity_increment_on", entity_increment_on_handler))
    app.add_handler(CommandHandler("entity_increment_off", entity_increment_off_handler))
    app.add_handler(CommandHandler("entity_export", entity_export_handler))
    app.add_handler(CommandHandler("entity_dedup", entity_dedup_handler))
    app.add_handler(CommandHandler("entity_export_links", entity_export_links_handler))
    app.add_handler(CommandHandler("entity_scale", entity_scale_handler))
    app.add_handler(CommandHandler("entity_classes", entity_classes_handler))
    app.add_handler(CommandHandler("entity_find", entity_find_handler))
    app.add_handler(CommandHandler("entity_merge", entity_merge_handler))
    app.add_handler(CommandHandler("entity_merge_log", entity_merge_log_handler))
    app.add_handler(CommandHandler("entity_unmerge", entity_unmerge_handler))
    app.add_handler(CommandHandler("entity_junk", entity_junk_handler))
    app.add_handler(CommandHandler("entity_junk_undo", entity_junk_undo_handler))
    app.add_handler(CommandHandler("entity_org_dupes", entity_org_dupes_handler))
    app.add_handler(CommandHandler("entity_docs_canon", entity_docs_canon_handler))
    app.add_handler(CommandHandler("entity_org_forms", entity_org_forms_handler))
    app.add_handler(CommandHandler("nora_reindex", nora_reindex_handler))
    app.add_handler(CommandHandler("roles", roles_handler))
    app.add_handler(CommandHandler("roles_dedup", roles_dedup_handler))
    app.add_handler(CommandHandler("roles_canon", roles_canon_handler))
    app.add_handler(CommandHandler("roles_bulk", roles_bulk_handler))
    app.add_handler(CommandHandler("roles_rename", roles_rename_handler))
    app.add_handler(CommandHandler("roles_forget", roles_forget_handler))
    app.add_handler(CommandHandler("roles_org", roles_org_handler))
    app.add_handler(CommandHandler("roles_outliers", roles_outliers_handler))
    app.add_handler(CommandHandler("roles_who", roles_who_handler))
    app.add_handler(CommandHandler("roles_audit", roles_audit_handler))
    app.add_handler(CommandHandler("roles_merge", roles_merge_handler))
    app.add_handler(CommandHandler("promises", promises_handler))
    app.add_handler(CommandHandler("promise_show", promise_show_handler))
    app.add_handler(CommandHandler("promise_test", promise_test_handler))
    app.add_handler(CommandHandler("promise_eval", promise_eval_handler))
    app.add_handler(CommandHandler("promise_estimate", promise_estimate_handler))
    app.add_handler(CommandHandler("promise_scan", promise_scan_handler))
    app.add_handler(CommandHandler("promise_resume", promise_resume_handler))
    app.add_handler(CommandHandler("promise_status", promise_status_handler))
    app.add_handler(CommandHandler("promise_increment_on", promise_increment_on_handler))
    app.add_handler(CommandHandler("promise_increment_off", promise_increment_off_handler))
    app.add_handler(CommandHandler("promise_checked", promise_checked_handler))
    app.add_handler(CommandHandler("promise_forget", promise_forget_handler))
    app.add_handler(CommandHandler("promise_restore", promise_restore_handler))
    app.add_handler(CommandHandler("promise_retest", promise_retest_handler))
    app.add_handler(CommandHandler("promise_prune", promise_prune_handler))
    app.add_handler(CommandHandler("promise_export", promise_export_handler))
    app.add_handler(CommandHandler("promise_dupes", promise_dupes_handler))
    app.add_handler(CommandHandler("promise_merge", promise_merge_handler))
    app.add_handler(CommandHandler("promise_notdupe", promise_notdupe_handler))
    app.add_handler(CommandHandler("promise_prune_who", promise_prune_who_handler))
    app.add_handler(CommandHandler("promise_prune_undo", promise_prune_undo_handler))
    app.add_handler(CommandHandler("tags_export", tags_export_handler))
    app.add_handler(CommandHandler("tags_wiki", tags_wiki_handler))
    app.add_handler(CommandHandler("tags_wiki_reset", tags_wiki_reset_handler))
    app.add_handler(CommandHandler("kg", kg_handler))
    app.add_handler(CommandHandler("myip", myip))
    app.add_handler(CommandHandler("builder", builder_handler))
    app.add_handler(CommandHandler("builder_test", builder_test_handler))
    app.add_handler(CommandHandler("fbmissing", fbmissing_handler))
    app.add_handler(CommandHandler("fbmissing_test", fbmissing_test_handler))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    app.add_handler(CommandHandler("analytics_backfill", analytics_backfill_handler))
    app.add_handler(CommandHandler("sc_backfill", sc_backfill_handler))
    app.add_handler(CommandHandler("weekly", weekly_handler))
    app.add_handler(CommandHandler("reports", reports_check_handler))
    app.add_handler(CommandHandler("social_capture", social_capture_handler))
    app.add_handler(CommandHandler("social_backfill_fb", social_backfill_fb_handler))
    app.add_handler(CommandHandler("social_import_sheet", social_import_sheet_handler))
    app.add_handler(CommandHandler("sheet_snapshot", sheet_snapshot_handler))
    app.add_handler(CommandHandler("sheet_backfill", sheet_backfill_handler))
    app.add_handler(CommandHandler("sheet_format", sheet_format_handler))
    app.add_handler(CommandHandler("sheet_migrate_legacy", sheet_migrate_legacy_handler))
    app.add_handler(CommandHandler("youtube_backfill", youtube_backfill_handler))
    app.add_handler(CommandHandler("tiktok_auth", tiktok_auth_handler))
    app.add_handler(CommandHandler("team", team_handler))
    app.add_handler(CommandHandler("todo", todo_handler))
    app.add_handler(CommandHandler("match_test", match_test_handler))
    app.add_handler(CommandHandler("match_scan", match_scan_handler))
    app.add_handler(CommandHandler("match_estimate", match_estimate_handler))
    app.add_handler(CommandHandler("match_backfill", match_backfill_handler))
    app.add_handler(CommandHandler("match_tg", match_tg_handler))
    app.add_handler(CommandHandler("match_cards", match_cards_handler))
    app.add_handler(CommandHandler("match_requeue", match_requeue_handler))
    app.add_handler(CommandHandler("kpi_debug", kpi_debug_handler))
    app.add_handler(CommandHandler("kpi_link", kpi_link_handler))
    app.add_handler(CommandHandler("viber_setup", viber_setup_handler))
    app.add_handler(CommandHandler("viber_test", viber_test_handler))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("checkmail", checkmail))
    app.add_handler(CommandHandler("instagram", instagram_handler))
    app.add_handler(CommandHandler("igreport", igreport))
    app.add_handler(CommandHandler("facebook", facebook_handler))
    app.add_handler(CommandHandler("fbreport", fbreport))
    app.add_handler(CommandHandler("morning", morning_handler))
    app.add_handler(CommandHandler("prozorro", prozorro_check))
    app.add_handler(CommandHandler("prozorro_test_jump", prozorro_test_jump))
    app.add_handler(CommandHandler("prozorro_confirm_jump", prozorro_confirm_jump))
    app.add_handler(CommandHandler("prozorro_reset_tender", prozorro_reset_tender))
    app.add_handler(CommandHandler("documents", documents_check))
    app.add_handler(CommandHandler("documents_test", documents_test_cmd))
    app.add_handler(CommandHandler("documents_rebaseline", documents_rebaseline_cmd))
    app.add_handler(CommandHandler("competitors", competitors_check))
    app.add_handler(CommandHandler("law", law_check))
    app.add_handler(CommandHandler("stat", stat_handler))
    app.add_handler(CommandHandler("stat_forget", stat_forget_handler))
    app.add_handler(CommandHandler("english", english_report_handler))
    app.add_handler(CommandHandler("traffic", traffic_handler))
    app.add_handler(CommandHandler("outage", outage_handler))
    app.add_handler(CommandHandler("outage_probe", outage_probe_handler))
    app.add_handler(CommandHandler("outage_export", outage_export_handler))
    app.add_handler(CommandHandler("outage_geocode", outage_geocode_handler))
    app.add_handler(CommandHandler("budget_load", budget_load_handler))
    app.add_handler(CommandHandler("budget_status", budget_status_handler))
    app.add_handler(CommandHandler("budget_headline", budget_headline_handler))
    app.add_handler(CommandHandler("budget_date", budget_date_handler))
    app.add_handler(CommandHandler("budget_execution", budget_execution_handler))
    app.add_handler(CommandHandler("budget_snapshot_check", budget_snapshot_check_handler))
    app.add_handler(CommandHandler("budget_execution_test", budget_execution_test_handler))
    app.add_handler(CommandHandler("budget_snapshot_reset", budget_snapshot_reset_handler))
    # xlsx з підписом /budget_load: CommandHandler бачить лише text, caption — ні
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/budget_load"), budget_load_handler))
    # ZIP пакета рішення в приват — без команд: бот сам розбирає і нашаровує
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.FileExtension("txt"),
        roles_fix_document))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, budget_package_handler))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))
    # Переслана в приват картка контакту → у базу редакції. Ставимо ПЕРЕД
    # текстовим NLQ: contact — не текст, тож перехопити його інакше нема де.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.CONTACT, shared_contact_handler))
    # Текст із телефонним номером — теж контакт (коли абонента немає в
    # Telegram і карткою його не переслати). Хендлер сам вирішує: не схоже на
    # номер — передає далі в природномовний шар, тож NLQ нічого не втрачає.
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
        & filters.Regex(_PHONE_RE), maybe_contact_text_handler))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_natural_language_query))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND & ~filters.ChatType.PRIVATE, group_reply_to_bot))
    app.add_handler(MessageReactionHandler(handle_message_reaction))
    app.add_handler(CallbackQueryHandler(todo_today_callback, pattern="^todo_today:"))
    app.add_handler(CallbackQueryHandler(news_back_callback, pattern=f"^{BACK_CALLBACK_DATA}$"))
    app.add_handler(CallbackQueryHandler(news_select_callback, pattern=f"^{SELECT_CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(roles_pair_callback, pattern="^rdp:"))
    app.add_handler(CallbackQueryHandler(roles_org_callback, pattern="^rdo:"))
    app.add_handler(CallbackQueryHandler(roles_bulk_callback, pattern="^rb[cmro]:"))
    app.add_handler(CallbackQueryHandler(roles_merge_callback, pattern="^rmc:"))
    app.add_handler(CallbackQueryHandler(roles_fix_callback, pattern="^rfx:"))
    app.add_handler(CallbackQueryHandler(entity_merge_callback, pattern="^emg:"))
    app.add_handler(CallbackQueryHandler(entity_find_callback, pattern="^ef[nm]:"))
    app.add_handler(CallbackQueryHandler(entity_junk_callback, pattern="^ej[bp]:"))
    app.add_handler(CallbackQueryHandler(entity_org_dupes_callback, pattern="^ejo:"))
    app.add_handler(CallbackQueryHandler(entity_docs_canon_callback, pattern="^ejd:"))
    app.add_handler(CallbackQueryHandler(entity_org_forms_callback, pattern="^ejf:"))
    app.add_handler(CallbackQueryHandler(cards_fix_callback, pattern="^cfx:"))
    app.add_handler(CallbackQueryHandler(promise_scan_callback, pattern="^psc:"))
    app.add_handler(CallbackQueryHandler(promise_prune_callback, pattern="^ppr:"))
    app.add_handler(CallbackQueryHandler(promise_prune_who_callback, pattern="^ppw:"))
    app.add_handler(CallbackQueryHandler(promises_fix_callback, pattern="^pfx:"))
    print("Bot started...")
    # callback_query — обов'язково в allowed_updates, інакше Telegram не шле
    # натискання inline-кнопок і кнопка «Написати бек» мовчить.
    app.run_polling(allowed_updates=["message", "channel_post", "message_reaction", "callback_query"])

if __name__ == "__main__":
    main()
