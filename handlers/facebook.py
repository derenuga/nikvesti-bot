import os
import requests
from datetime import datetime, timedelta

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

def get_page_followers():
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}"
    params = {
        "fields": "fan_count,followers_count",
        "access_token": FACEBOOK_PAGE_TOKEN
    }
    response = requests.get(url, params=params)
    data = response.json()
    if "error" in data:
        raise Exception(data["error"]["message"])
    return data
    
def get_page_stats():
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/insights"
    params = {
        "metric": "page_impressions_unique,page_post_engagements,page_fan_adds_unique",
        "period": "week",
        "access_token": FACEBOOK_PAGE_TOKEN
    }
    response = requests.get(url, params=params)
    data = response.json()
    if "error" in data:
        raise Exception(data["error"]["message"])
    stats = {}
    for item in data.get("data", []):
        values = item.get("values", [])
        if values:
            stats[item["name"]] = values[-1]["value"]
    return stats

def get_top_posts():
    since = int((datetime.now() - timedelta(days=7)).timestamp())
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/posts"
    params = {
        "fields": "id,message,permalink_url,likes.summary(true),comments.summary(true),shares,created_time",
        "since": since,
        "access_token": FACEBOOK_PAGE_TOKEN,
        "limit": 50
    }
    response = requests.get(url, params=params)
    data = response.json()
    if "error" in data:
        return []

    posts = data.get("data", [])
    for p in posts:
        likes = p.get("likes", {}).get("summary", {}).get("total_count", 0)
        comments = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        shares = p.get("shares", {}).get("count", 0)
        p["engagement"] = likes + comments + shares

    posts.sort(key=lambda x: x["engagement"], reverse=True)
    return posts[:5]

def short_message(message, words=5):
    if not message:
        return "без тексту"
    w = message.split()
    if len(w) <= words:
        return message
    return " ".join(w[:words]) + "..."

async def facebook_handler(update, context):
    try:
        page = get_page_followers()
        stats = get_page_stats()
        top_posts = get_top_posts()

        week_end = datetime.now().strftime("%d.%m.%Y")
        week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

        fans = page.get("fan_count", "н/д")
        followers = page.get("followers_count", "н/д")

        top_text = ""
        for i, p in enumerate(top_posts):
            likes = p.get("likes", {}).get("summary", {}).get("total_count", 0)
            comments = p.get("comments", {}).get("summary", {}).get("total_count", 0)
            shares = p.get("shares", {}).get("count", 0)
            link = p.get("permalink_url", "")
            title = short_message(p.get("message", ""))
            top_text += f'  {i+1}. <a href="{link}">{title}</a>\n      ❤️{likes} 💬{comments} 🔄{shares}\n'

        await update.message.reply_text(
            f"📘 Facebook МикВісті ({week_start} — {week_end}):\n\n"
            f"👥 Підписників: {followers}\n"
            f"❤️ Фанів: {fans}\n\n"
            f"📊 Статистика за тиждень:\n"
            f"  👁 Покази: {stats.get('page_impressions', 'н/д')}\n"
            f"  🌍 Охоплення: {stats.get('page_reach', 'н/д')}\n"
            f"  🤝 Залучені: {stats.get('page_engaged_users', 'н/д')}\n\n"
            f"🔥 Топ-5 публікацій тижня:\n{top_text}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text("Помилка Facebook: " + str(e))
