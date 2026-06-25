"""
Тест: дивимось що повертає API для конкретного поста з фото/галереєю.
Запуск: python test_fb_post.py
"""
import os
import requests

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")

# Пост який не знайшовся — з URL nikvesti/posts/pfbid...
# Спробуємо через пошук по сторінці за датою публікації матеріалу (19 червня)
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

from datetime import datetime, timedelta

# Матеріал від ~19-20 червня, шукаємо ширше
until_dt = datetime(2026, 6, 22)
since_dt = datetime(2026, 6, 18)

print("Запит постів 18-22 червня включно з полем 'story' і 'attachments'...")
url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/posts"
params = {
    "fields": "id,message,story,permalink_url,created_time,attachments{description,url,media_type}",
    "since": int(since_dt.timestamp()),
    "until": int(until_dt.timestamp()),
    "limit": 100,
    "access_token": FACEBOOK_PAGE_TOKEN,
}
resp = requests.get(url, params=params, timeout=15)
data = resp.json()

if "error" in data:
    print(f"ПОМИЛКА: {data['error']['message']}")
    exit(1)

posts = data.get("data", [])
print(f"Отримано постів: {len(posts)}")
print()

SEARCH = "319968"

for p in posts:
    message = p.get("message", "") or ""
    story = p.get("story", "") or ""
    attachments = p.get("attachments", {}).get("data", [])
    
    # Шукаємо по message, story і attachment url/description
    found = SEARCH in message or SEARCH in story
    if not found:
        for att in attachments:
            if SEARCH in (att.get("url", "") or "") or SEARCH in (att.get("description", "") or ""):
                found = True
                break
    
    if found:
        print(f"✅ ЗНАЙДЕНО! [{p.get('created_time')}]")
        print(f"  ID: {p['id']}")
        print(f"  message: {message[:200]}")
        print(f"  story: {story[:200]}")
        for att in attachments:
            print(f"  attachment: type={att.get('media_type')} url={att.get('url','')[:100]} desc={att.get('description','')[:100]}")
    else:
        print(f"  [{p.get('created_time')}] {message[:80] or story[:80]}")

print()
print("=== КІНЕЦЬ ===")
