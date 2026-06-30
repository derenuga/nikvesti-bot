"""
Intent Router — природномовні запити до Лиса Микити (Agentic Query Layer, GA4-контур).

Спрацьовує тільки на приватні повідомлення від користувачів з ALLOWED_USER_IDS
(перевірка вже робиться глобальним middleware в bot.py). Питання людською мовою
обробляється через Claude tool use: Claude обирає GA4-функцію і параметри,
Python її виконує, результат повертається Claude для фінальної відповіді.

Контур: тільки GA4 (без Meta, без пошуку по сайту) — docs/NATURAL_LANGUAGE_QUERIES_MODULE.md.
"""

import json
import os
from datetime import datetime, timedelta

import anthropic
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Metric, Dimension, OrderBy,
    FilterExpression, FilterExpressionList, Filter,
)
from google.oauth2 import service_account

from handlers.ai_messages import FOX_SYSTEM_PROMPT, clean_ai_text
from handlers.helpers import get_author_from_url

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")
BASE_URL = "https://nikvesti.com"

MAX_TOOL_ITERATIONS = 4

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------- GA4 ----------

def _ga4_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def _no_singapore_filter():
    """Singapore — бот-трафік, виключаємо з усіх GA4-запитів NLQ-шару."""
    return FilterExpression(
        not_expression=FilterExpression(
            filter=Filter(
                field_name="country",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value="Singapore",
                ),
            )
        )
    )


PERIOD_PRESETS = {
    "yesterday": 1,
    "last_7_days": 7,
    "last_30_days": 30,
}


def _resolve_period(period, start_date, end_date):
    """Повертає (start_date, end_date) у форматі YYYY-MM-DD."""
    today = datetime.now()

    if period == "custom":
        if not start_date or not end_date:
            raise ValueError("Для period='custom' потрібні start_date і end_date")
        return start_date, end_date

    if period == "today":
        d = today.strftime("%Y-%m-%d")
        return d, d

    if period in PERIOD_PRESETS:
        days = PERIOD_PRESETS[period]
        start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        end = (today - timedelta(days=1) if period == "yesterday" else today).strftime("%Y-%m-%d")
        return start, end

    if period == "this_month":
        return today.replace(day=1).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

    if period == "last_month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d")

    if period == "this_quarter":
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        start = today.replace(month=quarter_start_month, day=1)
        return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

    raise ValueError(f"Невідомий period: {period}. Використай 'custom' зі start_date/end_date.")


def get_ga4_metric(metric, period, start_date=None, end_date=None):
    start, end = _resolve_period(period, start_date, end_date)
    client = _ga4_client()

    if metric == "returningUsers":
        active = get_ga4_metric("activeUsers", "custom", start, end)["value"]
        new = get_ga4_metric("newUsers", "custom", start, end)["value"]
        return {"metric": metric, "start_date": start, "end_date": end, "value": active - new}

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        metrics=[Metric(name=metric)],
        dimension_filter=_no_singapore_filter(),
    )
    response = client.run_report(request)
    value = int(response.rows[0].metric_values[0].value) if response.rows else 0
    return {"metric": metric, "start_date": start, "end_date": end, "value": value}


def get_ga4_top_articles(period, limit=5, start_date=None, end_date=None):
    start, end = _resolve_period(period, start_date, end_date)
    client = _ga4_client()

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name="pagePath"), Dimension(name="pageTitle")],
        metrics=[Metric(name="screenPageViews")],
        dimension_filter=_no_singapore_filter(),
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=50,
    )
    response = client.run_report(request)

    results = []
    for row in response.rows:
        path = row.dimension_values[0].value
        title = row.dimension_values[1].value
        views = int(row.metric_values[0].value)
        if path in ("/", "", "/ru", "/en"):
            continue
        if "archive" in path:
            continue
        if not (path.startswith("/news") or path.startswith("/articles") or path.startswith("/blog")):
            continue
        author = get_author_from_url(BASE_URL + path)
        results.append({
            "url": BASE_URL + path,
            "title": title,
            "views": views,
            "author": author,
        })
        if len(results) == limit:
            break

    return {"start_date": start, "end_date": end, "articles": results}


def get_ga4_geo_breakdown(period, dimension="region", limit=10, start_date=None, end_date=None):
    start, end = _resolve_period(period, start_date, end_date)
    client = _ga4_client()

    ga4_dimension = "city" if dimension == "city" else "region"

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name=ga4_dimension)],
        metrics=[Metric(name="activeUsers")],
        dimension_filter=FilterExpression(
            and_group=FilterExpressionList(
                expressions=[
                    FilterExpression(
                        filter=Filter(
                            field_name="country",
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.EXACT,
                                value="Ukraine",
                            ),
                        )
                    ),
                    _no_singapore_filter(),
                ]
            )
        ),
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        limit=limit,
    )
    response = client.run_report(request)

    breakdown = [
        {dimension: row.dimension_values[0].value, "users": int(row.metric_values[0].value)}
        for row in response.rows
    ]
    return {"start_date": start, "end_date": end, "dimension": dimension, "breakdown": breakdown}


def get_ga4_article_stats(url):
    import re
    match = re.search(r'/(\d{4,})-', url)
    article_id = match.group(1) if match else None
    if not article_id:
        return {"error": "Не вдалося визначити ID матеріалу з URL"}

    client = _ga4_client()
    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date="2020-01-01", end_date="today")],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
        dimension_filter=FilterExpression(
            and_group=FilterExpressionList(
                expressions=[
                    FilterExpression(
                        filter=Filter(
                            field_name="pagePath",
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.CONTAINS,
                                value=article_id,
                            )
                        )
                    ),
                    _no_singapore_filter(),
                ]
            )
        ),
        limit=50,
    )
    response = client.run_report(request)

    by_lang = {}
    for row in response.rows:
        path = row.dimension_values[0].value
        views = int(row.metric_values[0].value)
        if path.startswith("/ru/"):
            lang = "ru"
        elif path.startswith("/en/"):
            lang = "en"
        else:
            lang = "ua"
        by_lang[lang] = by_lang.get(lang, 0) + views

    return {"url": url, "views_by_lang": by_lang, "total_views": sum(by_lang.values())}


# ---------- Tool use ----------

TOOLS = [
    {
        "name": "get_ga4_metric",
        "description": "Отримати метрику Google Analytics за вказаний період. Використовувати для питань про відвідуваність, перегляди, нових/повторних відвідувачів сайту nikvesti.com.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["activeUsers", "screenPageViews", "screenPageViewsPerSession", "newUsers", "returningUsers"],
                    "description": "activeUsers — користувачі, screenPageViews — перегляди, screenPageViewsPerSession — перегляди на сесію, newUsers — нові, returningUsers — повторні (рахується як activeUsers - newUsers)",
                },
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                    "description": "Стандартний період, або 'custom' якщо вказані start_date/end_date",
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
            },
            "required": ["metric", "period"],
        },
    },
    {
        "name": "get_ga4_top_articles",
        "description": "Топ статей сайту nikvesti.com за переглядами за вказаний період, з автором кожної статті.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                },
                "limit": {"type": "integer", "description": "Скільки статей повернути, за замовчуванням 5"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_ga4_geo_breakdown",
        "description": "Географія аудиторії сайту nikvesti.com по Україні (кількість користувачів по областях/регіонах або містах) за вказаний період. Трафік поза Україною сюди не входить.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                },
                "dimension": {
                    "type": "string",
                    "enum": ["region", "city"],
                    "description": "region — області/регіони України, city — міста. За замовчуванням region",
                },
                "limit": {"type": "integer", "description": "Скільки регіонів/міст повернути, за замовчуванням 10"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_ga4_article_stats",
        "description": "Статистика переглядів конкретної статті nikvesti.com за всю історію, по мовних версіях (ua/ru/en).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Повний URL статті на nikvesti.com"},
            },
            "required": ["url"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_ga4_metric": get_ga4_metric,
    "get_ga4_top_articles": get_ga4_top_articles,
    "get_ga4_geo_breakdown": get_ga4_geo_breakdown,
    "get_ga4_article_stats": get_ga4_article_stats,
}

QUERY_ROUTER_SYSTEM_PROMPT = FOX_SYSTEM_PROMPT + """

Зараз ти відповідаєш на природномовне запитання про статистику сайту nikvesti.com (Google Analytics). Сьогоднішня дата: {today}.
Використовуй доступні tools щоб отримати реальні дані — не вигадуй цифр. Якщо період сформульований розмовно ("середньомісячна", "за останній тиждень", "у вересні") — сам визнач відповідний period або start_date/end_date.
Відповідай коротко, по суті, з конкретними числами, простим текстом у кілька рядків — без Markdown-таблиць. Якщо даних не вдалось отримати — чесно скажи про це."""


async def handle_natural_language_query(update, context):
    question = update.message.text
    today = datetime.now().strftime("%Y-%m-%d")
    system_prompt = QUERY_ROUTER_SYSTEM_PROMPT.format(today=today)

    messages = [{"role": "user", "content": question}]
    placeholder = await update.message.reply_text("🦊 Розбираюсь з вашим питанням, шефе...")

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1000,
                system=system_prompt,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                final_text = "".join(b.text for b in response.content if b.type == "text")
                await placeholder.edit_text(clean_ai_text(final_text) or "Не вдалося сформувати відповідь.")
                return

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                func = TOOL_FUNCTIONS.get(block.name)
                try:
                    result = func(**block.input) if func else {"error": f"Невідомий tool: {block.name}"}
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            messages.append({"role": "user", "content": tool_results})

        await placeholder.edit_text("Забагато кроків для відповіді на це питання — спробуй сформулювати простіше.")
    except Exception as e:
        await placeholder.edit_text(f"❌ Помилка: {e}")
