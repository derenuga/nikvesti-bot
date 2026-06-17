import os
from datetime import datetime
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, MessageReactionHandler, filters
from handlers.google_analytics import analytics_handler
from handlers.scheduler import setup_scheduler, send_daily_report, check_email
from handlers.instagram import instagram_handler, send_weekly_instagram_report
from handlers.facebook import facebook_handler, send_weekly_facebook_report
from handlers.morning import morning_handler, send_morning_message
from handlers.prozorro import check_prozorro_tenders, diagnose_offset_jump, confirm_offset_jump
from handlers.reactions import handle_message_reaction
from handlers import storage

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
CHANNEL_USERNAME = "nikvesti"

last_channel_post_time = {"time": datetime.now()}

async def channel_post_handler(update, context):
    if update.channel_post and update.channel_post.chat.username == CHANNEL_USERNAME:
        last_channel_post_time["time"] = datetime.now()

async def start(update, context):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту\n/report — звіт в групу\n/checkmail — перевірити пошту\n/instagram — статистика Instagram\n/igreport — тижневий Instagram звіт в групу\n/facebook — статистика Facebook\n/fbreport — тижневий Facebook звіт в групу\n/morning — ранкове привітання\n/prozorro — перевірити тендери Прозорро"
    )

async def status(update, context):
    await update.message.reply_text("Бот працює. Все під контролем.")

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

async def morning_handler_cmd(update, context):
    await morning_handler(update, context)

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

async def post_init(application):
    setup_scheduler(application.bot, last_channel_post_time)

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analytics", analytics_handler))
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
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))
    app.add_handler(MessageReactionHandler(handle_message_reaction))
    print("Bot started...")
    app.run_polling(allowed_updates=["message", "channel_post", "message_reaction"])

if __name__ == "__main__":
    main()
