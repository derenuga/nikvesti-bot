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

print("=== СТАРТ ===")
print(f"PAGE_ID: {FACEBOOK_PAGE_ID}")
print(f"TOKEN: {'є' if FACEBOOK_PAGE_TOKEN else 'НЕМАЄ'}")
print(f"URL: {ARTICLE_URL}")
print()

clean = ARTICLE_URL.split("?")[0].rstrip("/")
print(f"Очищений URL: {clean}")

until_dt = datetime.now()
since_dt = until_dt - timedelta(days=7)
since_ts = int(since_dt.timestamp())
until_ts = int(until_dt.timestamp())
print(f"Діапазон: {since_dt.strftime('%d.%m.%Y')} — {until_dt.strftime('%d.%m.%Y')}")
print()

print("Запит до Facebook API...")
url = f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/posts"
params = {
    "fields": "id,message,permalink_url,created_time,reactions.summary(true),comments.summary(true),shares",
    "since": since_ts,
    "until": until_ts,
    "limit": 100,
    "access_token": FACEBOOK_PAGE_TOKEN,
}
try:
    resp = requests.get(url, params=params, timeout=15)
    print(f"HTTP статус: {resp.status_code}")
    data = resp.json()
except Exception as e:
    print(f"ПОМИЛКА запиту: {e}")
    exit(1)

if "error" in data:
    print(f"ПОМИЛКА API: {data['error']['message']}")
    exit(1)

posts = data.get("data", [])
print(f"Отримано постів: {len(posts)}")
print()

found = None
for post in posts:
    message = post.get("message", "") or ""
    if clean in message:
        found = post
        print(f"✅ Знайдено пост! ID: {found['id']}")
        break

if not found:
    print("❌ Пост не знайдено. Перші 5 постів:")
    for p in posts[:5]:
        print(f"  [{p.get('created_time','?')}] {(p.get('message') or '')[:100]}")
    exit(0)

reactions = found.get("reactions", {}).get("summary", {}).get("total_count", 0)
comments = found.get("comments", {}).get("summary", {}).get("total_count", 0)
shares = found.get("shares", {}).get("count", 0)
print(f"Дата: {found.get('created_time','?')}")
print(f"Посилання: {found.get('permalink_url','?')}")
print(f"❤️  Реакції: {reactions}")
print(f"💬 Коментарі: {comments}")
print(f"🔄 Шери: {shares}")
print()

print("Запитуємо охоплення...")
reach_url = f"https://graph.facebook.com/v19.0/{found['id']}/insights/post_impressions_unique"
try:
    r2 = requests.get(reach_url, params={"access_token": FACEBOOK_PAGE_TOKEN}, timeout=15)
    print(f"HTTP статус: {r2.status_code}")
    r2data = r2.json()
    if "error" in r2data:
        print(f"Охоплення недоступне: {r2data['error']['message']}")
    else:
        reach = r2data["data"][0]["values"][0]["value"]
        print(f"👁  Охоплення: {reach}")
except Exception as e:
    print(f"ПОМИЛКА охоплення: {e}")

print()
print("=== КІНЕЦЬ ===")
