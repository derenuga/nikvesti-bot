"""
Моніторинг новин конкурентів — пошук миколаївських новин на сайтах
конкурентів і відправка в чат редакції раз на годину.

Підключено: news.pn, novosti-n.org

Логіка:
1. Раз на годину парсимо сторінку конкурента.
2. Фільтруємо по словнику ключових слів (миколаївські теми).
3. Нові (не бачені раніше по ID) → формуємо пост → надсилаємо в чат.
4. Зберігаємо seen_ids всіх новин (не тільки локальних).

news.pn: фільтр по часу 3 години — захист від підняття старих новин.
novosti-n.org: без фільтру по часу — беремо все нове по ID.
"""

import os
import re
import asyncio
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from handlers import storage
from handlers.ai_messages import generate_competitors_intro

CHAT_ID = os.environ.get("CHAT_ID")

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
    {
        "id": "novosti_n",
        "name": "Новини N",
        "url": "https://novosti-n.org/ua/",
        "parser": "parse_novosti_n",
    },
]


# ---------- Парсери ----------

def _kyiv_now_str():
    """Повертає поточний час Києва у форматі HH:MM для відображення в постах.
    Railway працює на UTC, Київ = UTC+3."""
    now_kyiv = datetime.utcnow() + timedelta(hours=3)
    return now_kyiv.strftime("%H:%M")


def parse_news_pn(url):
    """
    Парсить головну сторінку news.pn.
    Структура: div.hentry > span.t (час) + a[href] > span (заголовок)
    ID: числовий в кінці URL /uk/criminal/345266 → '345266'
    Фільтр по часу: новини старші за 3 год відкидаються.
    """
    try:
        response = requests.get(url, headers={"User-Agent": "NikVesti-Bot/1.0"}, timeout=15)
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
            # news.pn повертає час в UTC для серверних запитів (Railway = UTC).
            # Конвертуємо в київський час (+3). При переїзді на KEY4 (UA сервер) — прибрати.
            if time_text:
                try:
                    t = datetime.strptime(time_text, "%H:%M")
                    t_kyiv = (t + timedelta(hours=3)).strftime("%H:%M")
                    time_text = t_kyiv
                except Exception:
                    pass

            url_full = "https://news.pn" + href if href.startswith("/") else href
            results.append({"id": news_id, "title": title, "time": time_text, "url": url_full})

        return results
    except Exception as e:
        print(f"Конкуренти [news_pn]: помилка — {e}")
        return []


def parse_novosti_n(url):
    """
    Парсить лівий фід головної сторінки novosti-n.org.
    Контейнер: div.mewsCity > div.newsList__item.ddd.hentry
    Час: div.newsList__time > span (може бути "Вчора о 23:18" або "08:00")
    ID: числовий в кінці URL /ua/news/Nazva-340345 → '340345'
    Без фільтру по часу — беремо все нове по ID (в тому числі вчорашнє).
    """
    try:
        response = requests.get(url, headers={"User-Agent": "NikVesti-Bot/1.0"}, timeout=15)
        if response.status_code != 200:
            print(f"Конкуренти [novosti_n]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        ID_RE = re.compile(r'-(\d+)$')
        results = []

        feed = soup.find('div', class_='mewsCity')
        if not feed:
            print("Конкуренти [novosti_n]: div.mewsCity не знайдено")
            return []

        for item in feed.find_all('div', class_='newsList__item'):
            time_el = item.find('div', class_='newsList__time')
            time_span = time_el.find('span') if time_el else None
            time_text = time_span.get_text(strip=True) if time_span else None

            a = item.find('a', href=re.compile(r'/news/'))
            if not a:
                continue
            href = a.get('href', '')
            id_match = ID_RE.search(href)
            if not id_match:
                continue
            news_id = id_match.group(1)

            title = a.get_text(strip=True)
            if not title or len(title) < 10:
                continue

            url_full = 'https://novosti-n.org' + href if href.startswith('/') else href
            results.append({"id": news_id, "title": title, "time": time_text, "url": url_full})

        return results
    except Exception as e:
        print(f"Конкуренти [novosti_n]: помилка — {e}")
        return []


PARSERS = {
    "parse_news_pn": parse_news_pn,
    "parse_novosti_n": parse_novosti_n,
}


# ---------- Фільтрація ----------

def is_local(title):
    return bool(LOCAL_KEYWORDS.search(title))


# ---------- Форматування ----------

from handlers.helpers import escape_html as _escape_html


def _format_post(new_by_source):
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

            if seen_ids is None:
                all_ids = [i["id"] for i in items]
                # Baseline: зберігаємо всі крім 3 останніх локальних,
                # 3 останніх локальних відправляємо одразу — щоб перевірити
                # що парсинг і відправка працюють після деплою.
                local_items = [i for i in items if is_local(i["title"])]
                to_send = local_items[:3]
                print(f"Конкуренти [{source['id']}]: перший запуск, baseline {len(all_ids)}, відправляємо {len(to_send)}")
                await loop.run_in_executor(
                    None, storage.save_seen_competitor_ids, source["id"], all_ids
                )
                if to_send:
                    new_by_source.append((source, to_send))
                continue

            seen_set = set(seen_ids)

            new_local = [
                i for i in items
                if i["id"] not in seen_set and is_local(i["title"])
            ]

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
        # AI-підводка Лиса перед списком — якщо генерація впаде,
        # пост все одно йде без неї
        try:
            intro = await generate_competitors_intro(
                [(source["name"], items) for source, items in new_by_source]
            )
            if intro and intro.strip():
                text = f"🦊 {_escape_html(intro.strip())}\n\n{text}"
        except Exception as e:
            print(f"Конкуренти: помилка AI-підводки — {e}")
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            print(f"Конкуренти: помилка відправки — {e}")
