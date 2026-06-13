import os
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension, FilterExpression, Filter
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

def get_stats(client, start_date, end_date):
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="sessions"),
            Metric(name="screenPageViews"),
        ],
    )
    response = client.run_report(request)
    row = response.rows[0].metric_values
    return int(row[0].value), int(row[1].value), int(row[2].value)

def get_top_pages(client, start_date, end_date):
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value="/",
                    case_sensitive=False,
                ),
            ),
            not_expression=FilterExpression(
                filter=Filter(
                    field_name="pagePath",
                    string_filter=Filter.StringFilter(
                        match_type=Filter.StringFilter.MatchType.EXACT,
                        value="/",
                    ),
                )
            ),
        ),
        order_bys=[{"metric": {"metric_name": "screenPageViews"}, "desc": True}],
        limit=5,
    )
    response = client.run_report(request)
    return [(row.dimension_values[0].value, int(row.metric_values[0].value)) for row in response.rows]

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
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        day_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        yesterday_label = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")

        users, sessions, pageviews = get_stats(client, yesterday, yesterday)
        u2, s2, p2 = get_stats(client, day_before, day_before)

        def diff(a, b):
            d = a - b
            return f"+{d}" if d > 0 else str(d)

        top_pages = get_top_pages(client, yesterday, yesterday)
        top_text = "\n".join([f"  {i+1}. {page} — {views}" for i, (page, views) in enumerate(top_pages)])

        await update.message.reply_text(
            f"📊 Статистика МикВісті за вчора ({yesterday_label}):\n\n"
            f"👥 Користувачі: {users} ({diff(users, u2)})\n"
            f"🔄 Сесії: {sessions} ({diff(sessions, s2)})\n"
            f"📄 Перегляди: {pageviews} ({diff(pageviews, p2)})\n\n"
            f"🔥 Топ-5 сторінок:\n{top_text}"
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
