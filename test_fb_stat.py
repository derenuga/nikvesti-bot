"""
Тест: знайти Facebook пост по URL статті і отримати статистику.
Запуск в консолі Railway: python test_fb_stat.py
"""

import os
import re
import requests
from datetime import datetime, timedelta

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

ARTICLE_URL = "https://nikvesti.com/news/justice/320102-sud-vidpraviv-pid-vartu-vodiya-audi-yakii-pyanym-skoiv-smertelnu-dtp-u-mykolayevi"

def clean_url(url):
    """Прибираємо fbclid та інші параметри з URL."""
    return url.split("?")[0].split("#")[0].rstrip("/")

def get_posts_range(since_ts, until_ts):
    """Тягнемо пости за діапазон дат."""
    url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/posts"
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
        print(f"❌ Помилка API: {data['error']['message']}")
        return []
    return data.get("data", [])

def get_post_reach(post_id):
    """Тягнемо охоплення конкретного поста."""
    url = f"https://graph.facebook.com/v19.0/{post_id}/insights/post_impressions_unique"
    params = {"access_token": FACEBOOK_PAGE_TOKEN}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "error" in data:
        print(f"  ⚠️  Охоплення недоступне: {data['error']['message']}")
        return None
    try:
        return data["data"][0]["values"][0]["value"]
    except Exception:
        return None

def main():
    clean = clean_url(ARTICLE_URL)
    print(f"🔍 Шукаємо пост з URL: {clean}")
    print()

    # Спробуємо за останні 7 днів (широко, для тесту)
    until_dt = datetime.now()
    since_dt = until_dt - timedelta(days=7)
    since_ts = int(since_dt.timestamp())
    until_ts = int(until_dt.timestamp())

    print(f"📅 Діапазон: {since_dt.strftime('%d.%m.%Y')} — {until_dt.strftime('%d.%m.%Y')}")
    posts = get_posts_range(since_ts, until_ts)
    print(f"📦 Отримано постів: {len(posts)}")
    print()

    found = None
    for post in posts:
        message = post.get("message", "") or ""
        if clean in message:
            found = post
            break

    if not found:
        print("❌ Пост з цим URL не знайдено в Facebook за останні 7 днів.")
        print()
        print("Перші 5 постів для перевірки:")
        for p in posts[:5]:
            print(f"  [{p.get('created_time','?')}] {(p.get('message') or '')[:80]}")
        return

    print(f"✅ Знайдено пост!")
    print(f"  ID: {found['id']}")
    print(f"  Дата: {found.get('created_time','?')}")
    print(f"  Посилання: {found.get('permalink_url','?')}")
    print()

    reactions = found.get("reactions", {}).get("summary", {}).get("total_count", 0)
    comments = found.get("comments", {}).get("summary", {}).get("total_count", 0)
    shares = found.get("shares", {}).get("count", 0)

    print(f"  ❤️  Реакції: {reactions}")
    print(f"  💬 Коментарі: {comments}")
    print(f"  🔄 Шери: {shares}")
    print()

    print("  🔍 Запитуємо охоплення (post_impressions_unique)...")
    reach = get_post_reach(found["id"])
    if reach is not None:
        print(f"  👁  Охоплення: {reach}")
    print()
    print("✅ Тест завершено.")

if __name__ == "__main__":
    main()
