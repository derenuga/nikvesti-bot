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
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

from handlers import storage

CHANNEL = "nikvesti"
SEARCH_MAX_PAGES = 15  # ~300 постів углиб (по ~20 на сторінку)
HEADERS = {
    # t.me інколи віддає редирект-заглушку без браузерного UA
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
}

# Калібрувальні якорі (дата 1-го числа → message_id), від Олега. Дозволяють
# оцінити message_id для будь-якої дати ЛІНІЙНОЮ інтерполяцією між сусідніми
# якорями (локальна швидкість постингу точніша за середню по всій історії).
# Використовуються: (1) для прицільного пошуку старого поста по даті статті,
# (2) як орієнтир глибини для бэкфілу. Доповнювати новими місяцями за потреби.
CALIBRATION_ANCHORS = {
    "2026-07-01": 82078, "2026-06-01": 81415, "2026-05-01": 80768,
    "2026-04-01": 80044, "2026-03-01": 79397, "2026-02-01": 78753,
    "2026-01-01": 78042, "2025-12-01": 77356, "2025-11-01": 76638,
    "2025-10-01": 75963, "2025-09-01": 75331, "2025-08-01": 74569,
    "2025-07-01": 73830, "2025-06-01": 73224, "2025-05-01": 72552,
    "2025-04-01": 71816, "2025-03-01": 71130, "2025-02-01": 70344,
    "2025-01-01": 69580,
}

# Прицільний пошук по даті: старт трохи вище оцінки (пост виходить у день
# публікації або наступні — тобто id ≈ оцінка або трохи більший) і гортаємо
# вниз, щоб перекрити похибку інтерполяції в обидва боки.
TARGETED_START_OFFSET = 300
TARGETED_PAGES = 25


def _anchor_items():
    return sorted(
        (datetime.fromisoformat(d).replace(tzinfo=timezone.utc), mid)
        for d, mid in CALIBRATION_ANCHORS.items()
    )


def estimate_message_id(target_date):
    """Оцінка message_id для дати лінійною інтерполяцією між якорями
    (екстраполяція за крайніми двома, якщо дата поза діапазоном)."""
    if target_date.tzinfo is None:
        target_date = target_date.replace(tzinfo=timezone.utc)
    else:
        target_date = target_date.astimezone(timezone.utc)

    items = _anchor_items()
    if target_date <= items[0][0]:
        (d0, m0), (d1, m1) = items[0], items[1]
    elif target_date >= items[-1][0]:
        (d0, m0), (d1, m1) = items[-2], items[-1]
    else:
        (d0, m0), (d1, m1) = items[0], items[1]
        for i in range(len(items) - 1):
            if items[i][0] <= target_date <= items[i + 1][0]:
                (d0, m0), (d1, m1) = items[i], items[i + 1]
                break

    span_days = (d1 - d0).total_seconds() / 86400
    if span_days == 0:
        return m1
    rate = (m1 - m0) / span_days
    return int(m0 + rate * ((target_date - d0).total_seconds() / 86400))


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


_HREF_ARTICLE_RE = re.compile(r"nikvesti\.com/[^\s\"'<>]*?/(\d{4,})-")


def _parse_message_block(block):
    """З div.tgme_widget_message дістає (message_id, views, href'и, datetime)."""
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

    dt = None
    time_tag = block.find("time", attrs={"datetime": True})
    if time_tag:
        try:
            dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return msg_id, views, hrefs, dt


def _article_ids_in_hrefs(hrefs):
    """Усі ID статей nikvesti.com серед посилань поста."""
    ids = set()
    for href in hrefs:
        m = _HREF_ARTICLE_RE.search(href)
        if m:
            ids.add(m.group(1))
    return ids


def fetch_post_views(message_id):
    """Перегляди конкретного поста через embed-сторінку. None якщо не знайшли."""
    html = _fetch_html(f"https://t.me/{CHANNEL}/{message_id}", params={"embed": "1"})
    soup = BeautifulSoup(html, "html.parser")
    views_span = soup.find("span", class_="tgme_widget_message_views")
    if not views_span:
        return None
    return _parse_views_text(views_span.get_text(strip=True))


def _scan_pages(article_id, start_before, max_pages):
    """Гортає t.me/s назад від start_before, шукає пост із посиланням на статтю.
    start_before=None → від найновіших. Повертає {"message_id","url","views"} або None."""
    link_re = _article_link_re(article_id)
    before = start_before

    for _ in range(max_pages):
        params = {"before": before} if before else None
        html = _fetch_html(f"https://t.me/s/{CHANNEL}", params=params)
        soup = BeautifulSoup(html, "html.parser")

        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            return None

        page_min_id = None
        for block in blocks:
            msg_id, views, hrefs, _dt = _parse_message_block(block)
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


def search_channel_post(article_id, max_pages=SEARCH_MAX_PAGES):
    """Пошук від найновіших постів назад (для свіжих матеріалів)."""
    return _scan_pages(article_id, None, max_pages)


def search_channel_post_near_date(article_id, pub_date, pages=TARGETED_PAGES):
    """Прицільний пошук: оцінюємо message_id по даті публікації (інтерполяція
    між якорями) і гортаємо вікно навколо оцінки. Так знаходимо старі
    матеріали за ~десяток запитів замість сотень сторінок від початку."""
    est = estimate_message_id(pub_date)
    return _scan_pages(article_id, est + TARGETED_START_OFFSET, pages)


def backfill_channel_index(months_back=None, max_pages=1500, pace_seconds=0.35):
    """Гортає історію каналу назад і індексує всі article_id→message_id одним
    записом у storage наприкінці. months_back — скільки місяців углиб (None =
    поки є сторінки або поки не досягнемо max_pages). Стоп також по якірному
    floor-id (страховка, якщо парсинг дати підведе). Повертає
    (проіндексовано_статей, переглянуто_постів, сторінок)."""
    cutoff = None
    floor_id = None
    if months_back:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
        floor_id = estimate_message_id(cutoff)

    mapping = {}
    posts_seen = 0
    pages = 0
    before = None

    for _ in range(max_pages):
        params = {"before": before} if before else None
        try:
            html = _fetch_html(f"https://t.me/s/{CHANNEL}", params=params)
        except Exception as e:
            print(f"tg_stats backfill: зупинка на сторінці {pages} — {e}")
            break

        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            break
        pages += 1

        page_min_id = None
        oldest_dt = None
        for block in blocks:
            msg_id, _views, hrefs, dt = _parse_message_block(block)
            posts_seen += 1
            if msg_id is not None and (page_min_id is None or msg_id < page_min_id):
                page_min_id = msg_id
            if dt is not None and (oldest_dt is None or dt < oldest_dt):
                oldest_dt = dt
            if msg_id is not None:
                for aid in _article_ids_in_hrefs(hrefs):
                    # лишаємо найменший message_id — оригінальний пост статті
                    if aid not in mapping or msg_id < mapping[aid]:
                        mapping[aid] = msg_id

        if page_min_id is None or page_min_id <= 1:
            break
        if cutoff and oldest_dt is not None and oldest_dt < cutoff:
            break
        if floor_id and page_min_id <= floor_id:
            break
        before = page_min_id
        time.sleep(pace_seconds)

    if mapping:
        storage.bulk_save_tg_posts(mapping)
    return len(mapping), posts_seen, pages


def get_tg_stat(article_id, pub_date=None):
    """
    Головна точка для /stat. Шляхи пошуку по черзі:
      1. індекс (миттєво) → перегляди з embed
      2. прицільний пошук по даті публікації (якщо pub_date відома) —
         оцінка message_id по якорях + вікно навколо неї
      3. пошук від найновіших постів назад (свіжі матеріали)
    Знайдене кладемо в індекс. Повертає {"url","views","found_via"} або None.
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

    found = None
    found_via = None
    if pub_date is not None:
        try:
            found = search_channel_post_near_date(article_id, pub_date)
            found_via = "date"
        except Exception as e:
            print(f"tg_stats: прицільний пошук не вдався — {e}")

    if not found:
        found = search_channel_post(article_id)
        found_via = "search"

    if found:
        if found["message_id"]:
            storage.save_tg_post(article_id, found["message_id"])
        return {"url": found["url"], "views": found["views"], "found_via": found_via}

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
