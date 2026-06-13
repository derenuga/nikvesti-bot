import os
import json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric
from google.oauth2 import service_account

TOKEN = os.environ.get("BOT_TOKEN")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")

def get_ga4_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я помічник редакції МикВісті 👋\n"
        "Команди:\n/start — привітання\n/status — перевірка\n/analytics — статистика сайту"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот працює. Все під контролем.")

async def analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = get_ga4_client()
        request = RunReportRequest(
            property=f"properties/{GA4_PROPERTY_ID}",
            date_ranges=[DateRange(start_date="yesterday", end_date="yesterday")],
            metrics=[
                Metric(name="activeUsers"),
                Metric(name="sessions"),
                Metric(name="screenPageViews"),
            ],
        )
        response = client.run_report(request)
        row = response.rows[0].metric_values
        users = row[0].value
        sessions = row[1].value
        pageviews = row[2].value

        await update.message.reply_text(
            f"📊 Статистика МикВісті за вчора:\n\n"
            f"👥 Користувачі: {users}\n"
            f"🔄 Сесії: {sessions}\n"
            f"📄 Перегляди: {pageviews}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analytics", analytics))
    print("Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
