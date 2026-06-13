import os
import requests
from datetime import datetime, timedelta

INSTAGRAM_TOKEN = os.environ.get("INSTAGRAM_TOKEN")
INSTAGRAM_USER_ID = "17841400860799899"

def get_instagram_profile():
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}"
    params = {
        "fields": "followers_count,media_count",
        "access_token": INSTAGRAM_TOKEN
    }
    return requests.get(url, params=params).json()

def get_instagram_stats():
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "reach,views,total_interactions,accounts_engaged",
        "period": "week",
        "metric_type": "total_value",
        "access_token": INSTAGRAM_TOKEN
    }
    data = requests.get(url, params=params).json()
    if "error" in data:
        raise Exception(data["error"]["message"])
    stats = {}
    for item in data.get("data", []):
        stats[item["name"]] = item.get("total_value", {}).get("value", 0)
    return stats

def get_follows_week():
    since = int((datetime.now() - timedelta(days=7)).timestamp())
    until = int(datetime.now().timestamp())
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "follows_and_unfollows",
        "period": "day",
        "metric_type": "total_value",
        "breakdown": "follow_type",
        "since": since,
        "until": until,
        "access_token": INSTAGRAM_TOKEN
    }
    data = requests.get(url, params=params).json()
    try:
        follows = 0
        unfollows = 0
        for day in data["data"]:
            for result in day["total_value"]["breakdowns"][0]["results"]:
                if result["dimension_values"][0] == "FOLLOWER":
                    follows += result["value"]
                elif result["dimension_values"][0] == "NON_FOLLOWER":
                    unfollows += result["value"]
        return follows, unfollows
    except:
        return None, None

def get_top_media():
    since = int((datetime.now() - timedelta(days=7)).timestamp())
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "id,media_type,permalink,like_count,comments_count,caption,timestamp",
        "since": since,
        "access_token": INSTAGRAM_TOKEN,
        "limit": 50
    }
    data = requests.get(url, params=params).json()
    if "error" in data:
        return []
    media = data.get("data", [])
    for m in media:
        m["engagement"] = m.get("like_count", 0) + m.get("comments_count", 0)
    media.sort(key=lambda x: x["engagement"], reverse=True)
    return media[:5]

def get_media_counts():
    since = int((datetime.now() - timedelta(days=7)).timestamp())
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "media_type",
        "since": since,
        "access_token": INSTAGRAM_TOKEN,
        "limit": 100
    }
    data = requests.get(url, params=params).json()
    if "error" in data:
        return {}
    counts = {"IMAGE": 0, "VIDEO": 0, "CAROUSEL_ALBUM": 0}
    for m in data.get("data", []):
        t = m.get("media_type", "")
        if t in counts:
            counts[t] += 1
    return counts

def short_caption(caption, words=5):
    if not caption:
        return "без підпису"
    w = caption.split()
    if len(w) <= words:
        return caption
    return " ".join(w[:words]) + "..."

async def send_weekly_instagram_report(bot, chat_id):
    from handlers.ai_messages import generate_instagram_weekly_comment
    try:
        profile = get_instagram_profile()
        stats = get_instagram_stats()
        top_media = get_top_media()
        counts = get_media_counts()

        followers_now = profile.get("followers_count", 0)
        follows, unfollows = get_follows_week()
        if follows is not None:
            net = follows - unfollows
            diff = f"+{net}" if net >= 0 else str(net)
            diff += f" (↑{follows} ↓{unfollows})"
        else:
            follows, unfollows, net, diff = 0, 0, 0, "н/д"

        photos = counts.get("IMAGE", 0)
        reels = counts.get("VIDEO", 0)
        carousels = counts.get("CAROUSEL_ALBUM", 0)
        total_posts = photos + reels + carousels

        week_end = datetime.now().strftime("%d.%m.%Y")
        week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

        top_text = ""
        for i, m in enumerate(top_media):
            media_type = {"IMAGE": "📸", "VIDEO": "🎬", "CAROUSEL_ALBUM": "🗂"}.get(m.get("media_type"), "📄")
            likes = m.get("like_count", 0)
            comments = m.get("comments_count", 0)
            link = m.get("permalink", "")
            title = short_caption(m.get("caption", ""))
            top_text += f'  {i+1}. {media_type} <a href="{link}">{title}</a> — ❤️ {likes} 💬 {comments}\n'

        ai_comment = await generate_instagram_weekly_comment(stats, follows, unfollows, total_posts, reels)

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"{ai_comment}\n\n"
                f"📱 Instagram МикВісті ({week_start} — {week_end}):\n\n"
                f"👥 Підписники: {followers_now} ({diff} за тиждень)\n\n"
                f"За тиждень опубліковано

async def instagram_handler(update, context):
    try:
        profile = get_instagram_profile()
        stats = get_instagram_stats()
        top_media = get_top_media()
        counts = get_media_counts()

        followers_now = profile.get("followers_count", 0)
        follows, unfollows = get_follows_week()
        if follows is not None:
            net = follows - unfollows
            diff = f"+{net}" if net >= 0 else str(net)
            diff += f" (↑{follows} ↓{unfollows})"
        else:
            diff = "н/д"

        week_end = datetime.now().strftime("%d.%m.%Y")
        week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

        photos = counts.get("IMAGE", 0)
        reels = counts.get("VIDEO", 0)
        carousels = counts.get("CAROUSEL_ALBUM", 0)
        total_posts = photos + reels + carousels

        top_text = ""
        for i, m in enumerate(top_media):
            media_type = {"IMAGE": "📸", "VIDEO": "🎬", "CAROUSEL_ALBUM": "🗂"}.get(m.get("media_type"), "📄")
            likes = m.get("like_count", 0)
            comments = m.get("comments_count", 0)
            link = m.get("permalink", "")
            title = short_caption(m.get("caption", ""))
            top_text += f'  {i+1}. {media_type} <a href="{link}">{title}</a> — ❤️ {likes} 💬 {comments}\n'

        await update.message.reply_text(
            f"📱 Instagram МикВісті ({week_start} — {week_end}):\n\n"
            f"👥 Підписники: {followers_now} ({diff} за тиждень)\n\n"
            f"За тиждень опубліковано {total_posts} матеріалів:\n"
            f"  📸 Постів: {photos + carousels}\n"
            f"  🎬 Рілзів: {reels}\n\n"
            f"📊 Статистика:\n"
            f"  👁 Охоплення: {stats.get('reach', 'н/д')}\n"
            f"  🤝 Взаємодії: {stats.get('total_interactions', 'н/д')}\n"
            f"  👤 Залучені акаунти: {stats.get('accounts_engaged', 'н/д')}\n\n"
            f"🔥 Топ-5 публікацій тижня:\n{top_text}",
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка Instagram: {e}")
