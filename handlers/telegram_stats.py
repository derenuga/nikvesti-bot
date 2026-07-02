"""
Перегляди постів у Telegram-каналі @nikvesti — для /stat.

Bot API принципово НЕ дає ні пошуку по історії каналу, ні лічильника
переглядів. Обхід — веб-дзеркало публічних каналів:
- https://t.me/s/nikvesti — стрічка з переглядами в HTML
  (span.tgme_widget_message_views), пагінація ?before=<message_id>
- https://t.me/nikvesti/82005?embed=1 — embed одного поста, теж з переглядами

Двошарова схема:
1. ІНДЕКС: bot.py отримує кожен пост каналу (channel_post_handler) —
   якщо в пості є посилання на nikvesti.com, зберігаємо
   article_id → message_id у storage (ключ "tg_posts"). Для всього,
   що опубліковано після деплою, пошук миттєвий.
2. ПОШУК: для старіших матеріалів гортаємо t.me/s/nikvesti назад
   (до SEARCH_MAX_PAGES сторінок ≈ 20 постів кожна), шукаємо посилання
   з ID статті. Знайдене — теж кладемо в індекс.

Перегляди завжди тягнемо свіжі з HTML (вони ростуть), індекс зберігає
тільки message_id.

УВАГА: парсер написано за відомою розміткою t.me без живого тесту
(з пісочниці розробки t.me недоступний — мережева політика). Перший
реальний тест — /stat на проді. Якщо Telegram змінить розмітку,
_parse_message_block/_parse_views_text — перші кандидати на правку.
"""

import re
import requests
from bs4 import BeautifulSoup

from handlers import storage

CHANNEL = "nikvesti"
SEARCH_MAX_PAGES = 15  # ~300 постів углиб (по ~20 на сторінку)
HEADERS = {
    # t.me інколи віддає редирект-заглушку без браузерного UA
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
}


def _parse_views_text(text):
    """'12.3K' → 12300, '1.2M' → 1200000, '456' → 456. None якщо не число."""
    if not text:
        return None
    t = text.strip().replace(" ", "").replace(",", ".")
    mult = 1
    if t[-1:].upper() == "K":
        mult, t = 1_000, t[:-1]
    elif t[-1:].upper() == "M":
        mult, t = 1_000_000, t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def _article_link_re(article_id):
    return re.compile(rf"nikvesti\.com/[^\s\"'<>]*/{article_id}-")


def _fetch_html(url, params=None):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"t.me HTTP {resp.status_code}")
    return resp.text


def _parse_message_block(block):
    """З div.tgme_widget_message дістає (message_id, views, всі href'и)."""
    data_post = block.get("data-post", "")  # "nikvesti/82005"
    msg_id = None
    if "/" in data_post:
        try:
            msg_id = int(data_post.rsplit("/", 1)[1])
        except ValueError:
            pass

    views = None
    views_span = block.find("span", class_="tgme_widget_message_views")
    if views_span:
        views = _parse_views_text(views_span.get_text(strip=True))

    hrefs = [a.get("href", "") for a in block.find_all("a", href=True)]
    return msg_id, views, hrefs


def fetch_post_views(message_id):
    """Перегляди конкретного поста через embed-сторінку. None якщо не знайшли."""
    html = _fetch_html(f"https://t.me/{CHANNEL}/{message_id}", params={"embed": "1"})
    soup = BeautifulSoup(html, "html.parser")
    views_span = soup.find("span", class_="tgme_widget_message_views")
    if not views_span:
        return None
    return _parse_views_text(views_span.get_text(strip=True))


def search_channel_post(article_id, max_pages=SEARCH_MAX_PAGES):
    """
    Гортає t.me/s/nikvesti назад і шукає пост із посиланням на статтю.
    Повертає {"message_id", "url", "views"} або None.
    """
    link_re = _article_link_re(article_id)
    before = None

    for _ in range(max_pages):
        params = {"before": before} if before else None
        html = _fetch_html(f"https://t.me/s/{CHANNEL}", params=params)
        soup = BeautifulSoup(html, "html.parser")

        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            return None

        page_min_id = None
        for block in blocks:
            msg_id, views, hrefs = _parse_message_block(block)
            if msg_id is not None and (page_min_id is None or msg_id < page_min_id):
                page_min_id = msg_id
            if any(link_re.search(h) for h in hrefs):
                return {
                    "message_id": msg_id,
                    "url": f"https://t.me/{CHANNEL}/{msg_id}",
                    "views": views,
                }

        if page_min_id is None or page_min_id <= 1:
            return None
        before = page_min_id

    return None


def get_tg_stat(article_id):
    """
    Головна точка для /stat: індекс → embed; інакше пошук по стрічці.
    Повертає {"url", "views", "found_via"} або None.
    """
    entry = storage.get_tg_post(article_id)
    if entry:
        message_id = entry["message_id"]
        views = fetch_post_views(message_id)
        return {
            "url": f"https://t.me/{CHANNEL}/{message_id}",
            "views": views,
            "found_via": "index",
        }

    found = search_channel_post(article_id)
    if found:
        if found["message_id"]:
            storage.save_tg_post(article_id, found["message_id"])
        return {"url": found["url"], "views": found["views"], "found_via": "search"}

    return None


# ---------- Індексація постів каналу (викликається з bot.py) ----------

def extract_article_ids_from_message(msg):
    """Всі ID статей nikvesti.com з поста каналу: текст/підпис, entities
    (text_link має url, url — підрядок тексту), кнопки inline-клавіатури."""
    urls = []

    text = msg.text or msg.caption or ""
    for entities in (msg.entities, msg.caption_entities):
        for e in entities or []:
            if getattr(e, "url", None):
                urls.append(e.url)
            elif getattr(e, "type", "") == "url":
                urls.append(text[e.offset:e.offset + e.length])

    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            for button in row:
                if getattr(button, "url", None):
                    urls.append(button.url)

    # посилання можуть бути і просто в тексті без entity
    urls.extend(re.findall(r"https?://nikvesti\.com/\S+", text))

    ids = set()
    for u in urls:
        if "nikvesti.com" not in u:
            continue
        m = re.search(r"/(\d{4,})-", u)
        if m:
            ids.add(m.group(1))
    return ids


def index_channel_post(msg):
    """Зберігає article_id → message_id для поста каналу. Тихо ігнорує помилки."""
    try:
        for article_id in extract_article_ids_from_message(msg):
            storage.save_tg_post(article_id, msg.message_id)
            print(f"tg_stats: проіндексовано статтю {article_id} → пост {msg.message_id}")
    except Exception as e:
        print(f"tg_stats: помилка індексації — {e}")
