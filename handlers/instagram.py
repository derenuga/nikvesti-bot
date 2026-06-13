import os
import requests

INSTAGRAM_TOKEN = os.environ.get("INSTAGRAM_TOKEN")
INSTAGRAM_USER_ID = "17841400860799899"

def get_instagram_stats():
    url = f"https://graph.instagram.com/v19.0/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "reach,profile_views,follower_count,total_interactions",
        "period": "day",
        "access_token": INSTAGRAM_TOKEN
    }
    response = requests.get(url, params=params)
    data = response.json()

    if "error" in data:
        raise Exception(data["error"]["message"])

    stats = {}
    for item in data.get("data", []):
        stats[item["name"]] = item["values"][-1]["value"]

    return stats

def get_instagram_followers():
    url = f"https://graph.instagram.com/v19.0/{INSTAGRAM_USER_ID}"
    params = {
        "fields": "followers_count,media_count",
        "access_token": INSTAGRAM_TOKEN
    }
    response = requests.get(url, params=params)
    data = response.json()

    if "error" in data:
        raise Exception(data["error"]["message"])

    return data

async def instagram_handler(update, context):
    try:
        followers = get_instagram_followers()
        stats = get_instagram_stats()

        await update.message.reply_text(
            f"📱 Instagram МикВісті:\n\n"
            f"👥 Підписники: {followers.get('followers_count', 'н/д')}\n"
            f"📸 Публікацій: {followers.get('media_count', 'н/д')}\n"
            f"👁 Взаємодії (день): {stats.get('reach', 'н/д')}\n"
            f"📊 Покази (день): {stats.get('impressions', 'н/д')}\n"
            f"🔍 Перегляди профілю: {stats.get('profile_views', 'н/д')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка Instagram: {e}")
