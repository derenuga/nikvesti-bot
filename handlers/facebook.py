import os
import re
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
FACEBOOK_PAGE_SLUG = "nikvesti"

def get_page_followers():
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}"
    params = {
        "fields": "fan_count,followers_count,name",
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
        "metric": "page_impressions_unique,page_post_engagements,page_follows",
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

def fix_permalink(url):
    if not url:
        return url
    url = re.sub(
        r'https://www\.facebook\.com/\d+/posts/(\d+)',
        lambda m: f"https://www.facebook.com/{FACEBOOK_PAGE_SLUG}/posts/{m.group(1)}",
        url
    )
    if url.startswith("/reel/"):
        url = "https://www.facebook.com" + url
    return url

def get_top_posts():
    since = int((datetime.now() - timedelta(days=7)).timestamp())
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/posts"
    params = {
        "fields": "id,message,permalink_url,reactions.summary(true),comments.summary(true),shares,created_time",
        "since": since,
        "access_token": FACEBOOK_PAGE_TOKEN,
        "limit": 100
    }
    response = requests.get(url, params=params)
    data = response.json()
    if "error" in data:
        return [], 0

    all_posts = data.get("data", [])
    posts = [p for p in all_posts if "nikvesti.com" in (p.get("message") or "")]
    total = len(posts)

    for p in posts:
        reactions = p.get("reactions", {}).get("summary", {}).get("total_count", 0)
        comments = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        shares = p.get("shares", {}).get("count", 0)
        p["engagement"] = reactions + comments + shares
        p["permalink_url"] = fix_permalink(p.get("permalink_url", ""))

    posts.sort(key=lambda x: x["engagement"], reverse=True)
    return posts[:5], total

def get_top_reels():
    since_dt = (datetime.now(tz=timezone.utc) - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/video_reels"
    params = {
        "fields": "id,description,permalink_url,likes.summary(true),comments.summary(true),created_time",
        "access_token": FACEBOOK_PAGE_TOKEN,
        "limit": 25
    }
    response = requests.get(url, params=params)
    data = response.json()
    if "error" in data:
        return [], 0

    all_reels = data.get("data", [])

    week_reels = []
    for r in all_reels:
        created = r.get("created_time", "")
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt >= since_dt:
                week_reels.append(r)
        except:
            pass

    total = len(week_reels)
    for r in week_reels:
        likes = r.get("likes", {}).get("summary", {}).get("total_count", 0)
        comments = r.get("comments", {}).get("summary", {}).get("total_count", 0)
        r["engagement"] = likes + comments
        r["permalink_url"] = fix_permalink(r.get("permalink_url", ""))

    week_reels.sort(key=lambda x: x["engagement"], reverse=True)
    return week_reels[:5], total

def extract_url_from_message(message):
    if not message:
        return None
    urls = re.findall(r'https?://nikvesti\.com/\S+', message)
    return urls[0] if urls else None

def get_author_from_url(url):
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag:
            return author_tag.get("content")
        author_tag = soup.find("meta", property="article:author")
        if author_tag:
            return author_tag.get("content")
        return None
    except:
        return None

def short_message(message, words=5):
    if not message:
        return "без тексту"
    w = message.split()
    if len(w) <= words:
        return message
    return " ".join(w[:words]) + "..."

def build_facebook_report(page, stats, top_posts, total_posts, top_reels, total_reels):
    week_end = datetime.now().strftime("%d.%m.%Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

    followers = stats.get("page_follows", page.get("followers_count", "н/д"))
    fans = page.get("fan_count", "н/д")

    posts_text = ""
    top_authors = []
    for i, p in enumerate(top_posts):
        reactions = p.get("reactions", {}).get("summary", {}).get("total_count", 0)
        comments = p.get("comments", {}).get("summary", {}).get("total_count", 0)
        shares = p.get("shares", {}).get("count", 0)
        link = p.get("permalink_url", "")
        title = short_message(p.get("message", ""))
        article_url = extract_url_from_message(p.get("message", ""))
        author = get_author_from_url(article_url) if article_url else None
        if author and author not in top_authors:
            top_authors.append(author)
        author_text = f"\n      👤 {author}" if author else ""
        posts_text += f'  {i+1}. <a href="{link}">{title}</a>\n      ❤️{reactions} 💬{comments} 🔄{shares}{author_text}\n'

    reels_text = ""
    for i, r in enumerate(top_reels):
        likes = r.get("likes", {}).get("summary", {}).get("total_count", 0)
        comments = r.get("comments", {}).get("summary", {}).get("total_count", 0)
        link = r.get("permalink_url", "")
        title = short_message(r.get("description", ""))
        reels_text += f'  {i+1}. <a href="{link}">{title}</a>\n      ❤️{likes} 💬{comments}\n'

    text = (
        f"📘 Facebook МикВісті ({week_start} — {week_end}):\n\n"
        f"👥 Підписників: {followers}\n"
        f"❤️ Фанів: {fans}\n\n"
        f"📊 Статистика за тиждень:\n"
        f"  👁 Охоплення: {stats.get('page_impressions_unique', 'н/д')}\n"
        f"  🤝 Взаємодії: {stats.get('page_post_engagements', 'н/д')}\n\n"
    )

    if posts_text:
        text += f"🔥 Топ-5 публікацій тижня (з {total_posts}):\n{posts_text}\n"
    if reels_text:
        text += f"🎬 Топ-5 рілзів тижня (з {total_reels}):\n{reels_text}"

    return text, top_authors

async def facebook_handler(update, context):
    try:
        page = get_page_followers()
        stats = get_page_stats()
        top_posts, total_posts = get_top_posts()
        top_reels, total_reels = get_top_reels()
        text, _ = build_facebook_report(page, stats, top_posts, total_posts, top_reels, total_reels)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text("Помилка Facebook: " + str(e))

async def send_weekly_facebook_report(bot, chat_id):
    from handlers.ai_messages import generate_facebook_weekly_comment
    try:
        page = get_page_followers()
        stats = get_page_stats()
        top_posts, total_posts = get_top_posts()
        top_reels, total_reels = get_top_reels()
        report_text, top_authors = build_facebook_report(page, stats, top_posts, total_posts, top_reels, total_reels)

        ai_comment = await generate_facebook_weekly_comment(stats, top_authors, total_posts, total_reels)

        await bot.send_message(
            chat_id=chat_id,
            text=ai_comment + "\n\n" + report_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        print("Помилка тижневого Facebook звіту: " + str(e))
