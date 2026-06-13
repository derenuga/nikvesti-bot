import os
from telegram.ext import ApplicationBuilder, CommandHandler
from handlers.google_analytics import analytics_handler, send_daily_report

TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

async def start(update, context):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту"
    )

async def status(update, context):
    await update.message.reply_text("✅ Бот працює. Все під контролем.")

async def schedule_jobs(app):
    app.job_queue.run_daily(
        send_daily_report,
        time=__import__('datetime').time(9, 0, 0),
        chat_id=CHAT_ID,
    )

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    app.post_init = schedule_jobs
    print("Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
