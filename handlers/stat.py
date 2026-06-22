"""
Команда /stat <url> — збирає статистику матеріалу nikvesti.com:
- Facebook: перегляди (post_media_view), реакції, коментарі, шери
- GA4: перегляди по мовних версіях (ua/ru/en) за ID матеріалу

Використання: /stat https://nikvesti.com/news/...
"""

import os
import re
import json
import requests
from datetime import datetime, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension, FilterExpression, Filter
from google.oauth2 import service_account

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")

LANG_FLAGS = {
    "ua": "🇺🇦",
    "ru": "🇷🇺",
    "en": "🇬🇧",
}


# ---------- Утиліти ----------

def _clean_url(url):
    return url.split("?")[0].split("#")[0].rstrip("/")

def _extract_article_id(url):
    """Витягуємо числовий ID з URL: /news/justice/320102-... → '320102'"""
    match = re.search(r'/(\d{4,})-', url)
    return match.group(1) if match else None


# ---------- Facebook ----------

def _get_fb_posts(since_ts, until_ts):
    url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/posts"
    params = {
        "fields": "id,message,permalink_url,created_time,reactions.summary(true),comments.summary(true),shares",
        "since": since_ts,
        "until": until_ts,
        "limit": 100,
        "access_token": FACEBOOK_PAGE_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "error" in data:
        raise Exception(data["error"]["message"])
    return data.get("data", [])

def _get_post_views(post_id):
    url = f"https://graph.facebook.com/v25.0/{post_id}/insights"
    params = {"metric": "post_media_view", "access_token": FACEBOOK_PAGE_TOKEN}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "error" in data:
        return None
    try:
        return data["data"][0]["values"][0]["value"]
    except Exception:
        return None

def get_fb_stat(article_url):
    """Шукає пост у Facebook і повертає статистику. Шукає за 14 днів."""
    clean = _clean_url(article_url)
    until_dt = datetime.now()
    since_dt = until_dt - timedelta(days=14)
    posts = _get_fb_posts(int(since_dt.timestamp()), int(until_dt.timestamp()))

    found = None
    for post in posts:
        message = post.get("message", "") or ""
        if clean in message:
            found = post
            break

    if not found:
        return None

    reactions = found.get("reactions", {}).get("summary", {}).get("total_count", 0)
    comments = found.get("comments", {}).get("summary", {}).get("total_count", 0)
    shares = found.get("shares", {}).get("count", 0)
    permalink = found.get("permalink_url", "")

    try:
        dt = datetime.strptime(found.get("created_time", ""), "%Y-%m-%dT%H:%M:%S+0000")
        date_str = (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_str = found.get("created_time", "")

    views = _get_post_views(found["id"])

    return {
        "permalink": permalink,
        "date": date_str,
        "views": views,
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
    }


# ---------- GA4 ----------

def get_ga4_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)

def get_ga4_stat(article_id):
    """
    Запитує GA4 по всіх сторінках де pagePath містить article_id.
    Групує по мові: ua (/news/...), ru (/ru/news/...), en (/en/news/...).
    Повертає dict {"ua": N, "ru": N, "en": N} — тільки ті що є.
    """
    client = get_ga4_client()

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

    return by_lang


# ---------- Форматування ----------

def format_stat_message(article_url, fb_stat, ga4_stat):
    clean = _clean_url(article_url)
    lines = [
        f"📊 <b>Статистика матеріалу</b>",
        f'<a href="{clean}">{clean}</a>',
        "",
        "📘 <b>Facebook</b>",
    ]

    if fb_stat is None:
        lines.append("Пост не знайдено (шукали за останні 14 днів)")
    else:
        lines.append(f'<a href="{fb_stat["permalink"]}">Пост від {fb_stat["date"]}</a>')
        if fb_stat["views"] is not None:
            lines.append(f'👁 Перегляди: {fb_stat["views"]:,}'.replace(",", "\u00a0"))
        lines.append(f'❤️ Реакції: {fb_stat["reactions"]}')
        lines.append(f'💬 Коментарі: {fb_stat["comments"]}')
        lines.append(f'🔄 Шери: {fb_stat["shares"]}')

    lines.append("")
    lines.append("📈 <b>GA4</b>")

    if not ga4_stat:
        lines.append("Даних не знайдено")
    else:
        total = sum(ga4_stat.values())
        for lang in ["ua", "ru", "en"]:
            if lang in ga4_stat:
                flag = LANG_FLAGS[lang]
                lines.append(f'{flag} {ga4_stat[lang]:,}'.replace(",", "\u00a0"))
        lines.append(f'──────')
        lines.append(f'Всього: {total:,}'.replace(",", "\u00a0"))

    return "\n".join(lines)


# ---------- Handler ----------

async def stat_handler(update, context):
    if not context.args:
        await update.message.reply_text(
            "Використання: /stat https://nikvesti.com/news/...",
            parse_mode="HTML"
        )
        return

    article_url = context.args[0]
    if "nikvesti.com" not in article_url:
        await update.message.reply_text("Вкажіть посилання на матеріал nikvesti.com")
        return

    article_id = _extract_article_id(article_url)
    if not article_id:
        await update.message.reply_text("Не вдалося визначити ID матеріалу з URL")
        return

    msg = await update.message.reply_text("⏳ Збираю статистику...")

    try:
        fb_stat = get_fb_stat(article_url)
    except Exception as e:
        fb_stat = None
        print(f"stat: помилка Facebook — {e}")

    try:
        ga4_stat = get_ga4_stat(article_id)
    except Exception as e:
        ga4_stat = {}
        print(f"stat: помилка GA4 — {e}")

    text = format_stat_message(article_url, fb_stat, ga4_stat)
    await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
