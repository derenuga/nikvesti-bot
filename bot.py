import os
from telegram.ext import ApplicationBuilder, CommandHandler
from handlers.google_analytics import analytics_handler

TOKEN = os.environ.get("BOT_TOKEN")

async def start(update, context):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту"
    )

async def status(update, context):
    await update.message.reply_text("✅ Бот працює. Все під контролем.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analytics", analytics_handler))
    print("Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
