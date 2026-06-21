"""
Моніторинг новин конкурентів — пошук миколаївських новин на сайтах
конкурентів і відправка в канал раз на годину.

Зараз підключено: news.pn
Планується: novosti-n.org

Логіка:
1. Раз на годину парсимо головну сторінку конкурента.
2. Фільтруємо новини по словнику ключових слів (миколаївські теми).
3. Нові (не бачені раніше) → формуємо пост і надсилаємо в чат редакції.
4. Зберігаємо seen_ids в storage.

Формат поста:
  🔍 Новини конкурентів про Миколаїв

  📰 NEWS.PN
  08:02 — Заголовок новини
  09:33 — Інший заголовок

Пост без превью (disable_web_page_preview=True).
"""

import os
import re
import asyncio
import requests
from datetime import datetime
from bs4 import BeautifulSoup

from handlers import storage

CHAT_ID = os.environ.get("CHAT_ID")

# Словник ключових слів для визначення миколаївських новин
LOCAL_KEYWORDS = re.compile(
    r'Миколає|Миколаїв|миколаїв|миколаївськ|'
    r'Інгул|Намив|Парутин|Слобідськ|'
    r'Галицинів|Снігурівк|Вознесенськ|Баштанськ|Первомайськ|'
    r'Очак|Южноукраїнськ|'
    r'Кім|Сєнкевич|'
    r'Куцуруб|Новоодеськ|Мертвовод|'
    r'Корабельн',
    re.IGNORECASE
)

COMPETITORS = [
    {
        "id": "news_pn",
        "name": "NEWS.PN",
        "url": "https://news.pn/uk/",
        "parser": "parse_news_pn",
    },
    # Майбутні джерела:
    # {
    #     "id": "novosti_n",
    #     "name": "Новини N",
    #     "url": "https://novosti-n.org/ua/",
    #     "parser": "parse_novosti_n",
    # },
]


# ---------- Парсери ----------

def parse_news_pn(url):
    """
    Парсить головну сторінку news.pn.
    Структура: div.hentry > span.t (час) + a[href] > span (заголовок)
    ID новини — числовий в кінці URL: /uk/criminal/345266 → '345266'
    """
    try:
        response = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=15)
        if response.status_code != 200:
            print(f"Конкуренти [news_pn]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for item in soup.find_all("div", class_="hentry"):
            time_el = item.find("span", class_="t")
            a = item.find("a", href=True)
            if not a:
                continue
            span = a.find("span")
            if not span:
                continue

            title = span.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            href = a["href"]
            id_match = re.search(r"/(\d+)$", href)
            if not id_match:
                continue
            news_id = id_match.group(1)

            time_text = time_el.get_text(strip=True) if time_el else None
            url_full = "https://news.pn" + href if href.startswith("/") else href

            results.append({
                "id": news_id,
                "title": title,
                "time": time_text,
                "url": url_full,
            })

        return results
    except Exception as e:
        print(f"Конкуренти [news_pn]: помилка парсингу — {e}")
        return []


PARSERS = {
    "parse_news_pn": parse_news_pn,
}


# ---------- Фільтрація ----------

def is_local(title):
    return bool(LOCAL_KEYWORDS.search(title))


# ---------- Форматування ----------

def _escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_post(new_by_source):
    """
    Формує пост з новинами конкурентів.
    new_by_source: список (source_config, [news_items])
    """
    lines = ["🔍 <b>Новини інших миколаївських медіа на регіональну тематику</b>", ""]

    for source, items in new_by_source:
        lines.append(f"📰 <b>{source['name']}</b>")
        for item in items:
            time_text = item["time"] or ""
            title = _escape_html(item["title"])
            url = item["url"]
            lines.append(f'{time_text} — <a href="{url}">{title}</a>')
        lines.append("")

    return "\n".join(lines).strip()


# ---------- Основна логіка ----------

async def check_competitors(bot):
    """Перевіряє всі джерела конкурентів. Викликається з планувальника."""
    if not CHAT_ID:
        print("Конкуренти: CHAT_ID не задано")
        return

    loop = asyncio.get_event_loop()
    new_by_source = []

    for source in COMPETITORS:
        try:
            parser_fn = PARSERS.get(source["parser"])
            if not parser_fn:
                print(f"Конкуренти [{source['id']}]: парсер не знайдено")
                continue

            items = await loop.run_in_executor(None, parser_fn, source["url"])
            if not items:
                continue

            seen_ids = await loop.run_in_executor(
                None, storage.get_seen_competitor_ids, source["id"]
            )

            # Перший запуск — зберігаємо baseline без відправки
            if seen_ids is None:
                all_ids = [i["id"] for i in items]
                print(f"Конкуренти [{source['id']}]: перший запуск, baseline {len(all_ids)} новин")
                await loop.run_in_executor(
                    None, storage.save_seen_competitor_ids, source["id"], all_ids
                )
                continue

            seen_set = set(seen_ids)

            # Нові локальні новини
            new_local = [
                i for i in items
                if i["id"] not in seen_set and is_local(i["title"])
            ]

            # Зберігаємо всі нові ID (не тільки локальні) щоб не повторювати
            all_new_ids = [i["id"] for i in items if i["id"] not in seen_set]
            if all_new_ids:
                updated_ids = list(seen_set) + all_new_ids
                await loop.run_in_executor(
                    None, storage.save_seen_competitor_ids, source["id"], updated_ids
                )

            if new_local:
                print(f"Конкуренти [{source['id']}]: {len(new_local)} нових локальних новин")
                new_by_source.append((source, new_local))

        except Exception as e:
            print(f"Конкуренти [{source['id']}]: неочікувана помилка — {e}")

    if new_by_source:
        text = _format_post(new_by_source)
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            print(f"Конкуренти: помилка відправки — {e}")
