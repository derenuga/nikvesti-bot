import json
import os
from datetime import datetime, timedelta
from calendar import monthrange
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Metric, Dimension, OrderBy,
    FilterExpression, FilterExpressionList, Filter
)
from google.oauth2 import service_account
from googleapiclient.discovery import build as gapi_build
from handlers.ai_messages import generate_english_monthly_comment

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")
SC_SITE_URL = "sc-domain:nikvesti.com"
BASE_URL = "https://nikvesti.com"

MONTHS_UA = {
    1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
    5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
    9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
}


# ── Клієнти ──────────────────────────────────────────────────────────────────

def get_credentials():
    creds_dict = json.loads(GA4_CREDENTIALS)
    return service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/analytics.readonly",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ]
    )

def get_ga4_client():
    return BetaAnalyticsDataClient(credentials=get_credentials())

def get_sc_client():
    return gapi_build("searchconsole", "v1", credentials=get_credentials(), cache_discovery=False)


# ── GA4 фільтри ──────────────────────────────────────────────────────────────

def en_no_sg_filter():
    """AND(pagePath BEGINS_WITH /en/, NOT country = Singapore)"""
    return FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                FilterExpression(
                    filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                            value="/en/",
                        ),
                    )
                ),
                FilterExpression(
                    not_expression=FilterExpression(
                        filter=Filter(
                            field_name="country",
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.EXACT,
                                value="Singapore",
                            ),
                        )
                    )
                ),
            ]
        )
    )


# ── GA4 запити ───────────────────────────────────────────────────────────────

def get_en_summary(client, start_date, end_date):
    """Користувачі, сесії, перегляди, new users, engagement rate для EN (без SG).
    Returning users = activeUsers - newUsers (GA4 не має окремої метрики returningUsers).
    screenPageViewsPerSession — вбудована метрика GA4, точніша ніж ручне ділення.
    """
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="sessions"),
            Metric(name="screenPageViews"),
            Metric(name="newUsers"),
            Metric(name="engagementRate"),
            Metric(name="screenPageViewsPerSession"),
        ],
        dimension_filter=en_no_sg_filter(),
    )
    response = client.run_report(request)
    if not response.rows:
        return 0, 0, 0, 0, 0.0, 0.0
    row = response.rows[0].metric_values
    active_users  = int(row[0].value)
    sessions      = int(row[1].value)
    pageviews     = int(row[2].value)
    new_users     = int(row[3].value)
    eng_rate      = float(row[4].value)
    pps           = float(row[5].value)
    returning     = max(0, active_users - new_users)
    return active_users, sessions, pageviews, returning, eng_rate, pps


def get_en_top_pages(client, start_date, end_date, limit=5):
    """Топ матеріали EN-версії (без SG), сортування по activeUsers."""
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="screenPageViews"),
        ],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        dimension_filter=en_no_sg_filter(),
        limit=50,
    )
    response = client.run_report(request)
    results = []
    for row in response.rows:
        path = row.dimension_values[0].value
        title = row.dimension_values[1].value
        users = int(row.metric_values[0].value)
        views = int(row.metric_values[1].value)
        if path in ("/en/", "/en"):
            continue
        if "archive" in path:
            continue
        results.append((path, title, users, views))
        if len(results) == limit:
            break
    return results


def get_ua_top_pages(client, start_date, end_date, limit=5):
    """Топ матеріали UA-версії для порівняння."""
    ua_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                # тільки /news/, /articles/, /blog/ — не /en/ і не /ru/
                FilterExpression(
                    filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                            value="/news/",
                        ),
                    )
                ),
            ]
        )
    )
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        dimension_filter=ua_filter,
        limit=10,
    )
    response = client.run_report(request)
    results = []
    for row in response.rows:
        path = row.dimension_values[0].value
        title = row.dimension_values[1].value
        users = int(row.metric_values[0].value)
        if "archive" in path:
            continue
        results.append((path, title, users))
        if len(results) == limit:
            break
    return results


def get_en_top_countries(client, start_date, end_date, limit=5):
    """Топ країн для EN-версії (без SG)."""
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="country")],
        metrics=[Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        dimension_filter=en_no_sg_filter(),
        limit=limit,
    )
    response = client.run_report(request)
    return [
        (row.dimension_values[0].value, int(row.metric_values[0].value))
        for row in response.rows
    ]


def get_en_top_referrers(client, start_date, end_date, limit=8):
    """Конкретні сайти-реферери для EN-версії (без SG)."""
    referral_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                FilterExpression(
                    filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                            value="/en/",
                        ),
                    )
                ),
                FilterExpression(
                    not_expression=FilterExpression(
                        filter=Filter(
                            field_name="country",
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.EXACT,
                                value="Singapore",
                            ),
                        )
                    )
                ),
                FilterExpression(
                    filter=Filter(
                        field_name="sessionDefaultChannelGroup",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.EXACT,
                            value="Referral",
                        ),
                    )
                ),
            ]
        )
    )
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[Dimension(name="sessionSource")],
        metrics=[Metric(name="sessions")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        dimension_filter=referral_filter,
        limit=limit,
    )
    response = client.run_report(request)
    return [
        (row.dimension_values[0].value, int(row.metric_values[0].value))
        for row in response.rows
    ]


# ── Search Console ────────────────────────────────────────────────────────────

def get_sc_top_queries(sc_client, start_date, end_date, limit=8):
    """
    Топ пошукових запитів з Google для EN-версії.
    Фільтруємо по page: contains /en/
    Повертає список (query, clicks, impressions, position)
    """
    try:
        body = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": ["query"],
            "dimensionFilterGroups": [{
                "filters": [{
                    "dimension": "page",
                    "operator": "contains",
                    "expression": "/en/",
                }]
            }],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
        response = sc_client.searchanalytics().query(
            siteUrl=SC_SITE_URL, body=body
        ).execute()
        rows = response.get("rows", [])
        return [
            (
                r["keys"][0],
                int(r.get("clicks", 0)),
                int(r.get("impressions", 0)),
                round(r.get("position", 0), 1),
            )
            for r in rows
        ]
    except Exception as e:
        print(f"Search Console error: {e}")
        return []


# ── Утиліти ──────────────────────────────────────────────────────────────────

def get_prev_month_range(year, month):
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year
    last_day = monthrange(prev_year, prev_month)[1]
    return (
        f"{prev_year}-{prev_month:02d}-01",
        f"{prev_year}-{prev_month:02d}-{last_day:02d}",
    )


def format_diff(curr, prev):
    if prev == 0:
        return ""
    diff = curr - prev
    pct = round(diff / prev * 100)
    sign = "+" if diff >= 0 else ""
    return f" ({sign}{diff}, {sign}{pct}%)"


# ── Головна функція ───────────────────────────────────────────────────────────

async def build_english_report(year=None, month=None):
    """
    Збирає місячний звіт EN-версії.
    Без year/month — звітує за попередній місяць.
    Сінгапур виключено з усіх GA4 запитів.
    """
    now = datetime.now()

    if year is None or month is None:
        first_of_current = now.replace(day=1)
        last_month_end = first_of_current - timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    last_day = monthrange(year, month)[1]
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{last_day:02d}"
    period_label = f"{MONTHS_UA[month]} {year}"

    prev_start, prev_end = get_prev_month_range(year, month)

    ga4 = get_ga4_client()
    sc = get_sc_client()

    # GA4 дані
    users, sessions, pageviews, returning, eng_rate, pps = get_en_summary(ga4, start_date, end_date)
    users_prev, sessions_prev, pageviews_prev, _, _, _ = get_en_summary(ga4, prev_start, prev_end)
    top_en_pages = get_en_top_pages(ga4, start_date, end_date)
    top_countries = get_en_top_countries(ga4, start_date, end_date)
    top_referrers = get_en_top_referrers(ga4, start_date, end_date)

    # Search Console
    top_queries = get_sc_top_queries(sc, start_date, end_date)

    # Похідні метрики
    returning_pct = round(returning / users * 100) if users > 0 else 0
    pages_per_session = round(pps, 1)
    eng_pct = round(eng_rate * 100)

    # ── Формування повідомлення ──

    msg = (
        f"🇬🇧 <b>Англійська версія МикВісті — {period_label}</b>\n"
        f"<i>(без урахування ботів із Сінгапуру)</i>\n\n"
        f"👥 Користувачі: <b>{users}</b>{format_diff(users, users_prev)}\n"
        f"🔄 Сесії: <b>{sessions}</b>{format_diff(sessions, sessions_prev)}\n"
        f"📄 Перегляди: <b>{pageviews}</b>{format_diff(pageviews, pageviews_prev)}\n"
        f"🔁 Повторні читачі: <b>{returning}</b> ({returning_pct}% аудиторії)\n"
        f"⚡️ Залученість: <b>{eng_pct}%</b> · {pages_per_session} стор/сесію\n"
    )

    # Топ EN матеріали (без цифр у списку)
    if top_en_pages:
        msg += "\n🔥 <b>Топ EN матеріали:</b>\n"
        for i, (path, title, users_cnt, views_cnt) in enumerate(top_en_pages, 1):
            url = f"{BASE_URL}{path}"
            msg += f'  {i}. <a href="{url}">{title}</a>\n'

    # Топ країни
    if top_countries:
        msg += "\n🌍 <b>Топ країни:</b>\n"
        for i, (country, cnt) in enumerate(top_countries, 1):
            msg += f"  {i}. {country} — {cnt}\n"

    # Реферери
    if top_referrers:
        msg += "\n🔗 <b>Реферери:</b>\n"
        for source, cnt in top_referrers:
            msg += f"  • {source}: {cnt}\n"

    # Search Console
    if top_queries:
        msg += "\n🔍 <b>Пошукові запити (Google):</b>\n"
        for query, clicks, impressions, position in top_queries:
            msg += f"  • {query} — {clicks} кліків (поз. {position})\n"

    # AI-коментар
    ai_comment = await generate_english_monthly_comment(
        period_label=period_label,
        users=users, users_prev=users_prev,
        sessions=sessions, sessions_prev=sessions_prev,
        pageviews=pageviews, pageviews_prev=pageviews_prev,
        returning=returning, returning_pct=returning_pct,
        eng_pct=eng_pct, pages_per_session=pages_per_session,
        top_en_pages=top_en_pages,
        top_countries=top_countries,
        top_referrers=top_referrers,
        top_queries=top_queries,
    )

    msg += f"\n🦊 {ai_comment}\n\n"
    msg += "@diiessa @sereda_ka"

    return msg


async def send_english_report(bot, chat_id):
    """Відправляє місячний EN-звіт в чат."""
    try:
        msg = await build_english_report()
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Помилка EN-звіту: {e}")


async def english_report_handler(update, context):
    """Команда /english — ручний запуск звіту."""
    try:
        await update.message.reply_text("⏳ Збираю дані GA4 + Search Console для EN-версії...")
        msg = await build_english_report()
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
