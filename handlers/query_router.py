"""
Intent Router — природномовні запити до Лиса Микити (Agentic Query Layer, GA4-контур).

Спрацьовує тільки на приватні повідомлення від користувачів з ALLOWED_USER_IDS
(перевірка вже робиться глобальним middleware в bot.py). Питання людською мовою
обробляється через Claude tool use: Claude обирає GA4-функцію і параметри,
Python її виконує, результат повертається Claude для фінальної відповіді.

Контур: тільки GA4 (без Meta, без пошуку по сайту) — docs/NATURAL_LANGUAGE_QUERIES_MODULE.md.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta

import anthropic
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Metric, Dimension, OrderBy,
    FilterExpression, FilterExpressionList, Filter,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build as gapi_build

from handlers.ai_messages import FOX_SYSTEM_PROMPT, clean_ai_text
from handlers.helpers import get_author_from_url

CHARTS_DIR = "/tmp/nlq_charts"
os.makedirs(CHARTS_DIR, exist_ok=True)

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")
BASE_URL = "https://nikvesti.com"
SC_SITE_URL = "sc-domain:nikvesti.com"

# 8 ітерацій: складні питання ("порівняй два місяці по джерелах і намалюй графік")
# потребують 4+ викликів tools; з prompt caching додаткові ітерації майже безкоштовні.
MAX_TOOL_ITERATIONS = 8

# Sonnet 5: помітно розумніший вибір tools та інтерпретація даних, ніж у 4.6.
# thinking не передаємо — у Sonnet 5 це вмикає adaptive thinking (модель сама
# вирішує, коли подумати перед вибором tool), тому max_tokens із запасом.
ROUTER_MODEL = "claude-sonnet-5"
ROUTER_MAX_TOKENS = 2000

client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------- GA4 ----------

def _ga4_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)


def _sc_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    return gapi_build("searchconsole", "v1", credentials=credentials, cache_discovery=False)


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
            filter=Filter(
                field_name="country",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value="Ukraine",
                ),
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


def get_ga4_hourly_breakdown(period, start_date=None, end_date=None):
    """Активність аудиторії по годинах доби (0-23, за київським часом сайту) — для пошуку найкращого часу публікації."""
    start, end = _resolve_period(period, start_date, end_date)
    client = _ga4_client()

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name="hour")],
        metrics=[Metric(name="activeUsers"), Metric(name="screenPageViews")],
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="hour"))],
        limit=24,
    )
    response = client.run_report(request)

    breakdown = [
        {
            "hour": int(row.dimension_values[0].value),
            "users": int(row.metric_values[0].value),
            "pageviews": int(row.metric_values[1].value),
        }
        for row in response.rows
    ]
    breakdown.sort(key=lambda r: r["hour"])
    return {"start_date": start, "end_date": end, "breakdown": breakdown}


def get_ga4_custom_report(dimensions, metrics, period, limit=20, start_date=None, end_date=None,
                           page_path_contains=None, filter_dimension=None, filter_value_contains=None):
    """Запасний вихід: довільний GA4 звіт для питань, які не покриті іншими tools.
    dimensions/metrics — точні назви з GA4 Data API (наприклад deviceCategory, browser,
    sessionDefaultChannelGroup, operatingSystem, dayOfWeek, sessionSource, pageReferrer).
    page_path_contains — опційно звузити звіт до конкретної статті/розділу (наприклад ID статті з URL).
    filter_dimension/filter_value_contains — опційно звузити звіт по будь-якій іншій dimension
    (наприклад filter_dimension='sessionSource', filter_value_contains='derstandard.de'),
    щоб подивитись детальний розклад трафіку з конкретного джерела/реферера."""
    start, end = _resolve_period(period, start_date, end_date)
    client = _ga4_client()

    filters = []
    if page_path_contains:
        filters.append(
            FilterExpression(
                filter=Filter(
                    field_name="pagePath",
                    string_filter=Filter.StringFilter(
                        match_type=Filter.StringFilter.MatchType.CONTAINS,
                        value=page_path_contains,
                    )
                )
            )
        )
    if filter_dimension and filter_value_contains:
        filters.append(
            FilterExpression(
                filter=Filter(
                    field_name=filter_dimension,
                    string_filter=Filter.StringFilter(
                        match_type=Filter.StringFilter.MatchType.CONTAINS,
                        value=filter_value_contains,
                    )
                )
            )
        )

    dimension_filter = None
    if len(filters) == 1:
        dimension_filter = filters[0]
    elif len(filters) > 1:
        dimension_filter = FilterExpression(and_group=FilterExpressionList(expressions=filters))

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name=d) for d in dimensions],
        metrics=[Metric(name=m) for m in metrics],
        dimension_filter=dimension_filter,
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metrics[0]), desc=True)],
        limit=limit,
    )
    response = client.run_report(request)

    rows = []
    for row in response.rows:
        entry = {}
        for i, d in enumerate(dimensions):
            entry[d] = row.dimension_values[i].value
        for i, m in enumerate(metrics):
            entry[m] = row.metric_values[i].value
        rows.append(entry)

    return {"start_date": start, "end_date": end, "dimensions": dimensions, "metrics": metrics, "rows": rows}


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
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value=article_id,
                )
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


def get_search_console_report(period, dimensions=None, page_url=None, search_type="web", limit=10, start_date=None, end_date=None):
    """Дані Google Search Console: пошукові запити, сторінки, країни, дати тощо.
    search_type='discover' — трафік з Google Discover (стрічка рекомендацій), 'web' — звичайний пошук Google,
    'googleNews' — Google News. page_url — звузити до конкретної статті (повний URL nikvesti.com)."""
    start, end = _resolve_period(period, start_date, end_date)
    dims = dimensions or ["query"]
    sc = _sc_client()

    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": dims,
        "type": search_type,
        "rowLimit": limit,
    }
    if page_url:
        # equals вимагає точного співпадіння URL (протокол, www, трейлінг слеш) — крихко.
        # Натомість шукаємо по ID статті (числовий префікс у шляху), як і в GA4-tools.
        id_match = re.search(r'/(\d{4,})-', page_url)
        expression = id_match.group(1) if id_match else page_url
        body["dimensionFilterGroups"] = [{
            "filters": [{"dimension": "page", "operator": "contains", "expression": expression}]
        }]

    try:
        response = sc.searchanalytics().query(siteUrl=SC_SITE_URL, body=body).execute()
    except Exception as e:
        return {"error": str(e)}

    rows = response.get("rows", [])
    results = [
        {
            **{dims[i]: r["keys"][i] for i in range(len(dims))},
            "clicks": int(r.get("clicks", 0)),
            "impressions": int(r.get("impressions", 0)),
            "ctr": round(r.get("ctr", 0) * 100, 2),
            "position": round(r.get("position", 0), 1),
        }
        for r in rows
    ]
    return {"start_date": start, "end_date": end, "search_type": search_type, "rows": results}


# ---------- Графіки ----------

def render_chart(labels, values, chart_type="bar", title="", ylabel=""):
    """Малює простий графік (bar/line) з даних, які Claude вже отримав з інших tools,
    і зберігає PNG. Викликати тільки коли дані — це розподіл/часовий ряд, а не одне число."""
    fig, ax = plt.subplots(figsize=(8, 4.5))

    if chart_type == "line":
        ax.plot(labels, values, marker="o", color="#e8772e")
    else:
        ax.bar(labels, values, color="#e8772e")

    ax.set_title(title)
    ax.set_ylabel(ylabel)
    plt.xticks(rotation=45, ha="right")
    fig.tight_layout()

    filename = f"{uuid.uuid4().hex}.png"
    path = os.path.join(CHARTS_DIR, filename)
    fig.savefig(path, dpi=150)
    plt.close(fig)

    return {"chart_rendered": True, "path": path}


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
        "name": "get_ga4_hourly_breakdown",
        "description": "Активність аудиторії сайту nikvesti.com по годинах доби (0-23) за вказаний період — кількість користувачів і переглядів на кожну годину. Використовувати для питань про найкращий/оптимальний час публікації новин.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "get_ga4_custom_report",
        "description": (
            "Запасний інструмент для GA4-питань, які не покриваються іншими tools — наприклад "
            "по пристроях, браузерах, джерелах трафіку, дні тижня тощо. Приймає точні назви "
            "dimensions і metrics з Google Analytics 4 Data API (GA4 dimension/metric reference). "
            "Приклади dimensions: deviceCategory, browser, operatingSystem, sessionDefaultChannelGroup, "
            "dayOfWeek, sessionSource, landingPage, pageReferrer (повний URL сторінки-реферера). "
            "Приклади metrics: activeUsers, sessions, "
            "screenPageViews, engagementRate, averageSessionDuration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Точні назви GA4 dimensions, наприклад ['deviceCategory']",
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Точні назви GA4 metrics, наприклад ['activeUsers']",
                },
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                },
                "limit": {"type": "integer", "description": "Скільки рядків повернути, за замовчуванням 20"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "page_path_contains": {
                    "type": "string",
                    "description": "Опційно: звузити звіт до конкретної статті — ID статті з URL або фрагмент шляху. Використовуй разом з dimensions типу sessionDefaultChannelGroup/sessionSource, щоб дізнатись звідки прийшов трафік на конкретний матеріал.",
                },
                "filter_dimension": {
                    "type": "string",
                    "description": "Опційно: назва GA4 dimension для додаткового фільтра (наприклад 'sessionSource'). Працює разом з filter_value_contains. Можна комбінувати з page_path_contains.",
                },
                "filter_value_contains": {
                    "type": "string",
                    "description": "Опційно: значення для фільтра по filter_dimension (CONTAINS-збіг), наприклад 'derstandard.de'. Використовуй щоб деталізувати трафік з конкретного реферера/джерела — наприклад dimensions=['pageReferrer','pagePath'], filter_dimension='sessionSource', filter_value_contains='derstandard.de', щоб побачити з яких сторінок-реферерів і на які наші сторінки прийшли користувачі.",
                },
            },
            "required": ["dimensions", "metrics", "period"],
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
    {
        "name": "get_search_console_report",
        "description": (
            "Дані Google Search Console для nikvesti.com: пошукові запити, по яких показується сайт, "
            "сторінки, країни, кліки/покази/CTR/позиція. search_type='discover' — трафік з Google Discover "
            "(стрічка рекомендацій в додатку Google, типово саме звідти приходять вірусні сплески), "
            "'web' — звичайний органічний пошук, 'googleNews' — Google News. Використовуй, коли питають "
            "звідки з Google прийшов трафік, чи стаття 'попала в Discover', по яких запитах знаходять сайт."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month", "this_quarter", "custom"],
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["query", "page", "country", "device", "date"]},
                    "description": "За замовчуванням ['query']. Можна комбінувати, наприклад ['date'] для динаміки по днях.",
                },
                "page_url": {"type": "string", "description": "Опційно: повний URL конкретної статті nikvesti.com, щоб звузити звіт до неї"},
                "search_type": {
                    "type": "string",
                    "enum": ["web", "discover", "googleNews", "image", "video"],
                    "description": "За замовчуванням 'web'. 'discover' — для питань про Google Discover",
                },
                "limit": {"type": "integer", "description": "Скільки рядків повернути, за замовчуванням 10"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, тільки якщо period='custom'"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Малює графік (стовпчиковий або лінійний) з даних, які ти вже отримав з інших GA4-tools, "
            "і додає його до відповіді як зображення. Використовуй, коли дані — це розподіл або часовий "
            "ряд (по годинах, по регіонах, по днях, топ статей) і графік допоможе наочніше за текст. "
            "Не використовуй для одного числа."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Підписи по осі X, наприклад години ['08:00', '09:00', ...] або назви регіонів",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Числові значення, по одному на кожен label",
                },
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line"],
                    "description": "bar — для категорій/розподілу, line — для часового ряду",
                },
                "title": {"type": "string"},
                "ylabel": {"type": "string", "description": "Підпис осі Y, наприклад 'Користувачі'"},
            },
            "required": ["labels", "values"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_ga4_metric": get_ga4_metric,
    "get_ga4_top_articles": get_ga4_top_articles,
    "get_ga4_geo_breakdown": get_ga4_geo_breakdown,
    "get_ga4_hourly_breakdown": get_ga4_hourly_breakdown,
    "get_ga4_custom_report": get_ga4_custom_report,
    "get_ga4_article_stats": get_ga4_article_stats,
    "get_search_console_report": get_search_console_report,
    "render_chart": render_chart,
}

# Живий прогрес у плейсхолдері: людський опис кожного tool,
# щоб під час довгого запиту було видно, що Лис зараз робить.
TOOL_PROGRESS = {
    "get_ga4_metric": "🦊 Дивлюсь метрики GA4...",
    "get_ga4_top_articles": "🦊 Збираю топ статей...",
    "get_ga4_geo_breakdown": "🦊 Дивлюсь географію аудиторії...",
    "get_ga4_hourly_breakdown": "🦊 Розкладаю трафік по годинах...",
    "get_ga4_custom_report": "🦊 Копаю глибше в GA4...",
    "get_ga4_article_stats": "🦊 Рахую перегляди статті...",
    "get_search_console_report": "🦊 Звіряю з Google Search Console...",
    "render_chart": "🦊 Малюю графік...",
}

QUERY_ROUTER_SYSTEM_PROMPT = FOX_SYSTEM_PROMPT + """

Зараз ти відповідаєш на природномовне запитання про статистику сайту nikvesti.com (Google Analytics). Сьогоднішня дата: {today}.
Використовуй доступні tools щоб отримати реальні дані — не вигадуй цифр. Якщо період сформульований розмовно ("середньомісячна", "за останній тиждень", "у вересні") — сам визнач відповідний period або start_date/end_date.
Якщо питання не покривається жодним із спеціалізованих tools (наприклад про пристрої, браузери, джерела трафіку, дні тижня) — використай get_ga4_custom_report з точними назвами GA4 dimensions/metrics. Якщо він поверне помилку через невірну назву — спробуй іншу назву ще раз, не здавайся одразу.
Якщо питають звідки прийшов трафік на конкретну статтю (соцмережі, реферали тощо) — використай get_ga4_custom_report з dimensions ['sessionDefaultChannelGroup'] або ['sessionSource', 'sessionMedium'] і page_path_contains (ID статті з URL, наприклад "35814" з "/news/35814-..."). Не питай дату публікації — для джерел трафіку конкретної статті дата не потрібна, бери period='last_30_days' або ширше якщо невпевнений.
Якщо питають конкретно про Google Discover, Google News чи пошукові запити Google — використай get_search_console_report (search_type='discover' для Discover). Для конкретної статті передай page_url повним URL (https://nikvesti.com/...). Це окреме джерело даних від GA4 — не плутай.
Якщо питають деталі про конкретний реферер/джерело трафіку з невеликою кількістю сесій (наприклад "звідки саме прийшли заходи з derstandard.de" або "на які наші сторінки попав трафік з X") — використай get_ga4_custom_report з filter_dimension='sessionSource', filter_value_contains=<домен>, dimensions=['pageReferrer', 'pagePath'] (або додай 'sessionSourceMedium'). Це дозволяє звузити звіт до конкретного джерела навіть якщо воно дало лише кілька сесій і не потрапляє в загальний топ. pageReferrer дає повний URL сторінки-донора, pagePath — куди саме на нашому сайті потрапив користувач.
Відповідай коротко, по суті, з конкретними числами, простим текстом у кілька рядків — без Markdown-таблиць. Якщо викликав render_chart — графік буде надіслано окремим повідомленням автоматично, НЕ згадуй шлях до файлу, НЕ вставляй markdown-посилання чи ![]() на зображення в тексті відповіді. Якщо даних не вдалось отримати — чесно скажи про це."""


async def _update_placeholder(placeholder, text, last_text):
    """Редагує плейсхолдер, ігноруючи помилки Telegram (той самий текст, rate limit)."""
    if text == last_text[0]:
        return
    try:
        await placeholder.edit_text(text)
        last_text[0] = text
    except Exception:
        pass


async def handle_natural_language_query(update, context):
    question = update.message.text
    today = datetime.now().strftime("%Y-%m-%d")
    system_prompt = QUERY_ROUTER_SYSTEM_PROMPT.format(today=today)

    messages = [{"role": "user", "content": question}]
    placeholder = await update.message.reply_text("🦊 Розбираюсь з вашим питанням, шефе...")
    last_placeholder_text = ["🦊 Розбираюсь з вашим питанням, шефе..."]
    chart_path = None
    used_tools = set()

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            # Prompt caching: tools рендеряться перед system, тому один
            # cache_control на system-блоці кешує tools+system разом (~4-5 тис.
            # токенів). Кожна наступна ітерація tool use і кожен запит протягом
            # дня читають цей префікс з кешу (~10% ціни). system містить дату —
            # кеш природно оновлюється раз на добу.
            response = await client.messages.create(
                model=ROUTER_MODEL,
                max_tokens=ROUTER_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                final_text = "".join(b.text for b in response.content if b.type == "text")
                final_text = clean_ai_text(final_text) or "Не вдалося сформувати відповідь."
                final_text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', final_text).strip()
                if used_tools:
                    sources = []
                    if used_tools - {"get_search_console_report", "render_chart"}:
                        sources.append("Google Analytics 4")
                    if "get_search_console_report" in used_tools:
                        sources.append("Google Search Console")
                    if sources:
                        final_text += f"\n\n📊 Джерело даних: {' + '.join(sources)} (nikvesti.com)"
                await placeholder.edit_text(final_text)
                if chart_path:
                    try:
                        with open(chart_path, "rb") as f:
                            await update.message.reply_photo(photo=f)
                    finally:
                        os.remove(chart_path)
                return

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                func = TOOL_FUNCTIONS.get(block.name)
                used_tools.add(block.name)
                progress = TOOL_PROGRESS.get(block.name)
                if progress:
                    await _update_placeholder(placeholder, progress, last_placeholder_text)
                try:
                    if func:
                        # GA4/Search Console/HTTP — синхронні; виконуємо в окремому
                        # потоці, щоб не заморожувати бота на час запиту (REVIEW б.1)
                        result = await asyncio.to_thread(func, **block.input)
                    else:
                        result = {"error": f"Невідомий tool: {block.name}"}
                except Exception as e:
                    result = {"error": str(e)}
                if block.name == "render_chart" and isinstance(result, dict) and result.get("path"):
                    chart_path = result["path"]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })

            messages.append({"role": "user", "content": tool_results})

        await placeholder.edit_text("Забагато кроків для відповіді на це питання — спробуй сформулювати простіше.")
    except Exception as e:
        await placeholder.edit_text(f"❌ Помилка: {e}")
