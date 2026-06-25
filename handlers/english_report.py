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
from handlers.ai_messages import generate_english_monthly_comment

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


def get_prev_month_range(year, month):
    """Повертає (start_date, end_date) для попереднього місяця."""
    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year
    last_day = monthrange(prev_year, prev_month)[1]
    return (
        f"{prev_year}-{prev_month:02d}-01",
        f"{prev_year}-{prev_month:02d}-{last_day:02d}",
    )


def en_no_sg_filter():
    """
    GA4 фільтр: тільки сторінки /en/ І виключити Сінгапур.
    AND(pagePath BEGINS_WITH /en/, NOT country EXACT Singapore)
    """
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


def get_en_summary(client, start_date, end_date):
    """Користувачі та сесії для EN-версії (без Сінгапуру)."""
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        metrics=[
            Metric(name="activeUsers"),
            Metric(name="sessions"),
        ],
        dimension_filter=en_no_sg_filter(),
    )
    response = client.run_report(request)
    if not response.rows:
        return 0, 0
    row = response.rows[0].metric_values
    return int(row[0].value), int(row[1].value)


def get_en_top_pages(client, start_date, end_date, limit=5):
    """Топ матеріали EN-версії (без Сінгапуру)."""
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[
            Dimension(name="pagePath"),
            Dimension(name="pageTitle"),
        ],
        metrics=[Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        dimension_filter=en_no_sg_filter(),
        limit=50,
    )
    response = client.run_report(request)
    results = []
    for row in response.rows:
        path = row.dimension_values[0].value
        title = row.dimension_values[1].value
        views = int(row.metric_values[0].value)
        if path in ("/en/", "/en"):
            continue
        if "archive" in path:
            continue
        results.append((path, title, views))
        if len(results) == limit:
            break
    return results


def get_en_top_countries(client, start_date, end_date, limit=5):
    """Топ країн для EN-версії (без Сінгапуру)."""
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
    """
    Конкретні сайти-реферери для EN-версії (без Сінгапуру).
    Фільтруємо тільки Referral-сесії і дивимось sessionSource.
    """
    referral_filter = FilterExpression(
        and_group=FilterExpressionList(
            expressions=[
                # тільки /en/
                FilterExpression(
                    filter=Filter(
                        field_name="pagePath",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.BEGINS_WITH,
                            value="/en/",
                        ),
                    )
                ),
                # без Сінгапуру
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
                # тільки referral-канал
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


def format_diff(curr, prev):
    """Форматує різницю між місяцями зі знаком і відсотком."""
    if prev == 0:
        return ""
    diff = curr - prev
    pct = round(diff / prev * 100)
    sign = "+" if diff >= 0 else ""
    return f" ({sign}{diff}, {sign}{pct}%)"


async def build_english_report(year=None, month=None):
    """
    Збирає і форматує місячний звіт EN-версії.
    Якщо year/month не передані — звітує за попередній місяць.
    Сінгапур виключено з усіх запитів.
    Повертає готовий HTML-рядок для Telegram.
    """
    now = datetime.now()

    if year is None or month is None:
        first_of_current = now.replace(day=1)
        last_month_end = first_of_current - timedelta(days=1)
        year, month = last_month_end.year, last_month_end.month

    last_day = monthrange(year, month)[1]
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{last_day:02d}"

    MONTHS_UA = {
        1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
        5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
        9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
    }
    period_label = f"{MONTHS_UA[month]} {year}"

    prev_start, prev_end = get_prev_month_range(year, month)

    client = get_ga4_client()

    users, sessions = get_en_summary(client, start_date, end_date)
    users_prev, sessions_prev = get_en_summary(client, prev_start, prev_end)
    top_pages = get_en_top_pages(client, start_date, end_date)
    top_countries = get_en_top_countries(client, start_date, end_date)
    top_referrers = get_en_top_referrers(client, start_date, end_date)

    # Форматуємо топ матеріали
    pages_text = ""
    for i, (path, title, views) in enumerate(top_pages, 1):
        url = f"{BASE_URL}{path}"
        pages_text += f'  {i}. <a href="{url}">{title}</a>\n'

    # Форматуємо країни
    countries_text = ""
    for i, (country, cnt) in enumerate(top_countries, 1):
        countries_text += f"  {i}. {country} — {cnt}\n"

    # Форматуємо реферери
    referrers_text = ""
    for source, cnt in top_referrers:
        referrers_text += f"  • {source}: {cnt}\n"

    # AI-коментар
    ai_comment = await generate_english_monthly_comment(
        period_label, users, users_prev, sessions, sessions_prev,
        top_pages, top_countries, top_referrers
    )

    msg = (
        f"🇬🇧 <b>Англійська версія МикВісті — {period_label}</b>\n"
        f"<i>(без урахування ботів із Сінгапуру)</i>\n\n"
        f"👥 Користувачі: <b>{users}</b>{format_diff(users, users_prev)}\n"
        f"🔄 Сесії: <b>{sessions}</b>{format_diff(sessions, sessions_prev)}\n"
    )

    if top_pages:
        msg += f"\n🔥 <b>Топ матеріали:</b>\n{pages_text}"

    if top_countries:
        msg += f"\n🌍 <b>Топ країни:</b>\n{countries_text}"

    if top_referrers:
        msg += f"\n🔗 <b>Реферери (звідки прийшли):</b>\n{referrers_text}"

    msg += f"\n🦊 {ai_comment}\n\n"
    msg += "👆 @diiessa, тобі буде цікаво — це твоя версія 😉"

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
        await update.message.reply_text("⏳ Збираю дані GA4 для EN-версії...")
        msg = await build_english_report()
        await update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
