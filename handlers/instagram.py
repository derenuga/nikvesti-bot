import os
import requests
from datetime import datetime, timedelta

INSTAGRAM_TOKEN = os.environ.get("INSTAGRAM_TOKEN")
INSTAGRAM_USER_ID = "17841400860799899"

def get_instagram_stats():
    url = f"https://graph.instagram.com/v19.0/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "reach,total_interactions,accounts_engaged",
        "period": "week",
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

def get_instagram_profile():
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
        profile = get_instagram_profile()
        stats = get_instagram_stats()

        week_end = datetime.now().strftime("%d.%m.%Y")
        week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

        await update.message.reply_text(
            f"📱 Instagram МикВісті ({week_start} — {week_end}):\n\n"
            f"👥 Підписники:
