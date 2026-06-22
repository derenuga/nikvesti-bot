"""
Команда /stat <url> — збирає статистику матеріалу nikvesti.com:
- Facebook: перегляди (post_media_view), реакції, коментарі, шери
- GA4: TODO після підключення до БД сайту

Використання: /stat https://nikvesti.com/news/...
"""

import os
import re
import requests
from datetime import datetime, timedelta

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")


def _clean_url(url):
    """Прибираємо fbclid та інші параметри."""
    return url.split("?")[0].split("#")[0].rstrip("/")


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


def get_article_stat(article_url):
    """
    Шукає пост у Facebook з цим URL і повертає статистику.
    Шукає за останні 14 днів.
    Повертає dict або None якщо не знайдено.
    """
    clean = _clean_url(article_url)

    until_dt = datetime.now()
    since_dt = until_dt - timedelta(days=14)
    since_ts = int(since_dt.timestamp())
    until_ts = int(until_dt.timestamp())

    posts = _get_fb_posts(since_ts, until_ts)

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
    created_time = found.get("created_time", "")

    # Конвертуємо дату з UTC в київський час для відображення
    try:
        dt = datetime.strptime(created_time, "%Y-%m-%dT%H:%M:%S+0000")
        dt_kyiv = dt + timedelta(hours=3)
        date_str = dt_kyiv.strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_str = created_time

    views = _get_post_views(found["id"])

    return {
        "post_id": found["id"],
        "permalink": permalink,
        "date": date_str,
        "views": views,
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
    }


def format_stat_message(article_url, stat):
    clean = _clean_url(article_url)
    lines = [
        f"📊 <b>Статистика матеріалу</b>",
        f'<a href="{clean}">{clean}</a>',
        "",
        "📘 <b>Facebook</b>",
    ]
    if stat is None:
        lines.append("Пост не знайдено (шукали за останні 14 днів)")
    else:
        lines.append(f'<a href="{stat["permalink"]}">Пост від {stat["date"]}</a>')
        if stat["views"] is not None:
            lines.append(f'👁 Перегляди: {stat["views"]:,}'.replace(",", "\u00a0"))
        lines.append(f'❤️ Реакції: {stat["reactions"]}')
        lines.append(f'💬 Коментарі: {stat["comments"]}')
        lines.append(f'🔄 Шери: {stat["shares"]}')

    lines.append("")
    lines.append("📈 <b>GA4</b>: буде після підключення до БД сайту")

    return "\n".join(lines)


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

    msg = await update.message.reply_text("⏳ Збираю статистику...")

    try:
        stat = get_article_stat(article_url)
        text = format_stat_message(article_url, stat)
        await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"Помилка: {e}")
