"""
Моніторинг новин конкурентів — пошук миколаївських новин на сайтах
конкурентів і відправка в чат редакції раз на годину.

Підключено: news.pn, novosti-n.org, Суспільне Миколаїв

Логіка:
1. Раз на годину парсимо сторінку/RSS конкурента.
2. Фільтруємо по словнику ключових слів (миколаївські теми).
   Виняток: Суспільне Миколаїв — всі новини вже миколаївські, фільтр не потрібен.
3. Нові (не бачені раніше по ID) → формуємо пост → надсилаємо в чат.
4. Зберігаємо seen_ids всіх новин (не тільки локальних).

news.pn: фільтр по часу 3 години — захист від підняття старих новин.
novosti-n.org: без фільтру по часу — беремо все нове по ID (в тому числі вчорашнє).
Суспільне: RSS all.rss → фільтр по /mykolaiv/ в URL, без фільтру по словнику.
           Baseline: N=3 — зберігаємо всі крім 3 останніх, 3 останніх відправляємо
           одразу щоб перевірити що RSS читається і відправка працює.
"""

import os
import re
import asyncio
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

from handlers import storage

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
        "filter_local": True,
    },
    {
        "id": "novosti_n",
        "name": "Новини N",
        "url": "https://novosti-n.org/ua/",
        "parser": "parse_novosti_n",
        "filter_local": True,
    },
    {
        "id": "suspilne_mk",
        "name": "Суспільне Миколаїв",
        "url": "https://suspilne.media/rss/all.rss",
        "parser": "parse_suspilne_mk",
        "filter_local": False,  # всі новини вже миколаївські
    },
]


# ---------- Парсери ----------

def _is_fresh_pn(time_str):
    """Для news.pn — відкидаємо новини старші за 3 години (захист від підняття)."""
    if not time_str:
        return True
    try:
        t = datetime.strptime(time_str.strip(), "%H:%M")
        now = datetime.now()
        news_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return news_time >= now - timedelta(hours=3)
    except ValueError:
        return True


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

            if not _is_fresh_pn(time_text):
                continue

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


def parse_suspilne_mk(url):
    """
    Парсить загальний RSS Суспільного, фільтрує тільки миколаївські новини.
    URL: https://suspilne.media/rss/all.rss
    Фільтр: link містить /mykolaiv/
    ID: числовий префікс в URL /mykolaiv/1336382-назва/ → '1336382'
    Час: pubDate в форматі RFC 2822 (Sun, 21 Jun 2026 22:08:45 +0300)
    Без фільтру по ключових словах — всі новини вже миколаївські.
    """
    try:
        response = requests.get(url, headers={"User-Agent": "NikVesti-Bot/1.0"}, timeout=15)
        if response.status_code != 200:
            print(f"Конкуренти [suspilne_mk]: HTTP {response.status_code}")
            return []

        root = ET.fromstring(response.content)
        channel = root.find("channel")
        if channel is None:
            print("Конкуренти [suspilne_mk]: channel не знайдено в RSS")
            return []

        ID_RE = re.compile(r'/mykolaiv/(\d+)-')
        results = []

        for item in channel.findall("item"):
            link = item.findtext("link") or ""
            if "/mykolaiv/" not in link:
                continue

            id_match = ID_RE.search(link)
            if not id_match:
                continue
            news_id = id_match.group(1)

            title = item.findtext("title") or ""
            title = title.strip()
            if not title:
                continue

            pub_date = item.findtext("pubDate") or ""
            # Форматуємо час як "HH:MM" для однорідності з іншими джерелами
            time_text = None
            if pub_date:
                try:
                    dt = parsedate_to_datetime(pub_date)
                    time_text = dt.strftime("%H:%M")
                except Exception:
                    pass

            results.append({
                "id": news_id,
                "title": title,
                "time": time_text,
                "url": link,
            })

        return results
    except Exception as e:
        print(f"Конкуренти [suspilne_mk]: помилка — {e}")
        return []


PARSERS = {
    "parse_news_pn": parse_news_pn,
    "parse_novosti_n": parse_novosti_n,
    "parse_suspilne_mk": parse_suspilne_mk,
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
                # Перший запуск (baseline).
                # Для джерел з filter_local=True (news.pn, novosti-n): зберігаємо
                # всі ID без відправки — нові з'являться при наступній перевірці.
                # Для suspilne_mk (filter_local=False): зберігаємо всі крім
                # N=3 останніх, 3 останніх відправляємо одразу — щоб одразу
                # перевірити що RSS читається і відправка в Telegram працює.
                if source.get("filter_local", True):
                    all_ids = [i["id"] for i in items]
                    print(f"Конкуренти [{source['id']}]: перший запуск, baseline {len(all_ids)} новин")
                    await loop.run_in_executor(
                        None, storage.save_seen_competitor_ids, source["id"], all_ids
                    )
                else:
                    # N=3: перші 3 (найновіші в RSS) відправляємо, решту — в baseline
                    BASELINE_SEND_COUNT = 3
                    to_send = items[:BASELINE_SEND_COUNT]
                    to_skip = items[BASELINE_SEND_COUNT:]
                    baseline_ids = [i["id"] for i in to_skip]
                    print(
                        f"Конкуренти [{source['id']}]: перший запуск, "
                        f"baseline {len(baseline_ids)} новин, відправляємо {len(to_send)}"
                    )
                    await loop.run_in_executor(
                        None, storage.save_seen_competitor_ids, source["id"], baseline_ids
                    )
                    if to_send:
                        new_by_source.append((source, to_send))
                continue

            seen_set = set(seen_ids)

            # Для джерел з filter_local=True — фільтруємо по ключових словах
            # Для suspilne_mk — беремо всі нові без фільтру
            if source.get("filter_local", True):
                new_items = [
                    i for i in items
                    if i["id"] not in seen_set and is_local(i["title"])
                ]
            else:
                new_items = [i for i in items if i["id"] not in seen_set]

            # Зберігаємо всі нові ID (не тільки відфільтровані)
            all_new_ids = [i["id"] for i in items if i["id"] not in seen_set]
            if all_new_ids:
                updated_ids = list(seen_set) + all_new_ids
                await loop.run_in_executor(
                    None, storage.save_seen_competitor_ids, source["id"], updated_ids
                )

            if new_items:
                print(f"Конкуренти [{source['id']}]: {len(new_items)} нових новин")
                new_by_source.append((source, new_items))

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
