import os
from telegram.ext import ApplicationBuilder, CommandHandler
from handlers.google_analytics import analytics_handler
from handlers.scheduler import setup_scheduler, send_daily_report, check_email
from handlers.instagram import instagram_handler

TOKEN = os.environ.get("BOT_TOKEN")

async def start(update, context):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту\n/report — звіт в групу\n/checkmail — перевірити пошту\n/instagram — статистика Instagram"
    )

async def status(update, context):
    await update.message.reply_text("Бот працює. Все під контролем.")

async def report(update, context):
    await send_daily_report(context.bot)
    await update.message.reply_text("Звіт надіслано в групу!")

async def checkmail(update, context):
    await check_email(context.bot, "afternoon")
    await update.message.reply_text("Перевірив пошту!")

async def post_init(application):
    setup_scheduler(application.bot)

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("checkmail", checkmail))
    app.add_handler(CommandHandler("instagram", instagram_handler))
    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
