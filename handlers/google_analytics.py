import json
from datetime import datetime, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension, OrderBy
from google.oauth2 import service_account
import os
from handlers.helpers import parse_month_arg

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")
BASE_URL = "https://nikvesti.com"

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
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[Metric(name="screenPageViews")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=30,
    )
    response = client.run_report(request)
    results = []
    for row in response.rows:
        path = row.dimension_values[0].value
        title = row.dimension_values[1].value
        views = int(row.metric_values[0].value)
            if path in ("/", "", "/ru", "/en") or "archive" in path or not (path.startswith("/news") or path.startswith("/articles") or path.startswith("/blog")):
            continue
        results.append((path, title, views))
        if len(results) == 5:
            break
    return results

async def analytics_handler(update, context):
    try:
        args = context.args
        start_dt, end_dt, period_label = parse_month_arg(args)

        client = get_ga4_client()

        if start_dt:
            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = end_dt.strftime("%Y-%m-%d")
            users, sessions, pageviews = get_stats(client, start_date, end_date)
            top_pages = get_top_pages(client, start_date, end_date)
            top_text = "\n".join([
                f'  {i+1}. <a href="{BASE_URL}{path}">{title}</a> — {views}'
                for i, (path, title, views) in enumerate(top_pages)
            ])
            await update.message.reply_text(
                f"📊 Статистика МикВісті ({period_label}):\n\n"
                f"👥 Користувачі: {users}\n"
                f"🔄 Сесії: {sessions}\n"
                f"📄 Перегляди: {pageviews}\n\n"
                f"🔥 Топ-5 статей:\n{top_text}",
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        else:
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
            await update.message.reply_text(
                f"📊 Статистика МикВісті за вчора ({yesterday_label}):\n\n"
                f"👥 Користувачі: {users} ({diff(users, u2)})\n"
                f"🔄 Сесії: {sessions} ({diff(sessions, s2)})\n"
                f"📄 Перегляди: {pageviews} ({diff(pageviews, p2)})\n\n"
                f"🔥 Топ-5 статей:\n{top_text}",
                parse_mode="HTML",
                disable_web_page_preview=True
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
