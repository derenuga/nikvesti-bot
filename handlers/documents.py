"""
Моніторинг нових документів на сайті Миколаївської міської ради.

Зараз підключено одне джерело: розпорядження міського голови (?c=4).
Щоб додати нове джерело (рішення сесії, виконкому тощо) — достатньо
додати рядок у DOCUMENT_SOURCES нижче, код більше не чіпати.

Логіка:
1. Щогодини для кожного джерела завантажуємо першу сторінку списку
   (сортування DESC, тобто найновіші зверху).
2. Порівнюємо внутрішні ID документів (числові, з URL /documents/51982.html)
   з тими, що вже бачили (зберігаються в storage).
3. Нові ID → формуємо пост і надсилаємо в канал.
4. Зберігаємо нові ID в storage.

Формат поста (parse_mode=HTML):
  📋 <b>Розпорядження міського голови</b> — 3 нові документи

  <a href="...">№ 275р</a> від 17.06.2026
  Про затвердження внутрішньої організаційної структури...

  <a href="...">№ 274р</a> від 17.06.2026
  ...

Якщо документ один — без заголовку "N нові документи", просто один запис.

ВАЖЛИВО: перший запуск після деплою завантажить поточну першу сторінку
і збереже всі ID як "вже бачені" — БЕЗ відправки в канал (режим
"ініціалізація"). Це запобігає спаму при першому старті. Наступні
запуски порівнюють з цим baseline і надсилають лише реально нові.
"""

import os
import re
import asyncio
import requests
from datetime import datetime
from bs4 import BeautifulSoup

from handlers import storage

BASE_URL = "https://mkrada.gov.ua"

DOCUMENT_SOURCES = [
    {
        "id": "mayor_orders",
        "name": "Розпорядження міського голови",
        "emoji": "📋",
        "url": f"{BASE_URL}/documents/",
        "params": {"c": 4, "o": "DESC"},
    },
    # Майбутні джерела — просто додати рядок:
    # {
    #     "id": "council_decisions",
    #     "name": "Рішення міської ради",
    #     "emoji": "🏛",
    #     "url": f"{BASE_URL}/documents/",
    #     "params": {"c": 1, "o": "DESC"},
    # },
    # {
    #     "id": "executive_decisions",
    #     "name": "Рішення виконкому",
    #     "emoji": "⚙️",
    #     "url": f"{BASE_URL}/documents/",
    #     "params": {"c": 5, "o": "DESC"},
    # },
]

DOCUMENTS_CHAT_ID = os.environ.get("DOCUMENTS_CHAT_ID") or os.environ.get("PROZORRO_CHAT_ID")


# ---------- Парсинг ----------

def _parse_doc_id(href):
    """Витягує числовий ID з href виду /documents/51982.html → '51982'."""
    match = re.search(r"/documents/(\d+)\.html", href)
    return match.group(1) if match else None


def _parse_doc_number(date_div):
    """Витягує номер документа зі структури div.date → '275р'."""
    strong = date_div.find("strong") if date_div else None
    return strong.get_text(strip=True) if strong else None


def _parse_doc_date(date_div):
    """Витягує дату зі структури div.date → '17.06.2026'."""
    if not date_div:
        return None
    raw = date_div.get_text(strip=True)
    match = re.search(r"\((\d+)\s+(\S+)\s+(\d{4})\)", raw)
    if not match:
        return None
    months = {
        "Січня": "01", "Лютого": "02", "Березня": "03", "Квітня": "04",
        "Травня": "05", "Червня": "06", "Липня": "07", "Серпня": "08",
        "Вересня": "09", "Жовтня": "10", "Листопада": "11", "Грудня": "12",
    }
    day, month_name, year = match.groups()
    month = months.get(month_name, "??")
    return f"{int(day):02d}.{month}.{year}"


def _fetch_documents_sync(source):
    """
    Завантажує першу сторінку списку документів і повертає список:
    [{"id": "51982", "title": "...", "number": "275р", "date": "17.06.2026", "url": "..."}]
    """
    try:
        response = requests.get(source["url"], params=source["params"], timeout=15)
        if response.status_code != 200:
            print(f"Документи [{source['id']}]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for item in soup.find_all("div", class_="news_line_item"):
            a = item.find("a")
            if not a or not a.get("href"):
                continue

            doc_id = _parse_doc_id(a["href"])
            if not doc_id:
                continue

            date_div = item.find("div", class_="date")
            title = a.get_text(strip=True)
            number = _parse_doc_number(date_div)
            date = _parse_doc_date(date_div)
            url = BASE_URL + a["href"] if a["href"].startswith("/") else a["href"]

            results.append({
                "id": doc_id,
                "title": title,
                "number": number,
                "date": date,
                "url": url,
            })

        return results
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка завантаження — {e}")
        return []


# ---------- Форматування ----------

def _escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_post(source, new_docs):
    """Формує HTML-текст поста для Telegram."""
    count = len(new_docs)
    header = f"{source['emoji']} <b>{_escape_html(source['name'])}</b>"
    if count > 1:
        header += f" — {count} нові документи"

    lines = [header, ""]
    for doc in new_docs:
        num_text = f"№ {doc['number']}" if doc.get("number") else "Документ"
        date_text = f" від {doc['date']}" if doc.get("date") else ""
        link = f'<a href="{doc["url"]}">{_escape_html(num_text)}</a>{date_text}'
        lines.append(link)
        lines.append(_escape_html(doc["title"]))
        lines.append("")  # порожній рядок між документами

    return "\n".join(lines).strip()


# ---------- Основна логіка ----------

async def _check_source(bot, source):
    """Перевіряє одне джерело і надсилає пост якщо є нові документи."""
    loop = asyncio.get_event_loop()

    docs = await loop.run_in_executor(None, _fetch_documents_sync, source)
    if not docs:
        return

    seen_ids = await loop.run_in_executor(None, storage.get_seen_document_ids, source["id"])
    fetched_ids = [d["id"] for d in docs]

    # Перший запуск — зберігаємо baseline без відправки в канал
    if seen_ids is None:
        print(f"Документи [{source['id']}]: перший запуск, зберігаємо baseline ({len(fetched_ids)} документів)")
        await loop.run_in_executor(None, storage.save_seen_document_ids, source["id"], fetched_ids)
        return

    # Знаходимо нові — ті, яких ще не бачили, зберігаємо порядок (найновіші першими)
    seen_set = set(seen_ids)
    new_docs = [d for d in docs if d["id"] not in seen_set]

    if not new_docs:
        return

    print(f"Документи [{source['id']}]: знайдено {len(new_docs)} нових")

    if not DOCUMENTS_CHAT_ID:
        print(f"Документи [{source['id']}]: DOCUMENTS_CHAT_ID не задано, пропускаємо відправку")
        return

    text = _format_post(source, new_docs)
    try:
        await bot.send_message(
            chat_id=DOCUMENTS_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка відправки в Telegram — {e}")
        return

    # Зберігаємо нові ID (додаємо до існуючих)
    all_ids = list(seen_set) + [d["id"] for d in new_docs]
    await loop.run_in_executor(None, storage.save_seen_document_ids, source["id"], all_ids)


async def check_documents(bot):
    """Перевіряє всі джерела документів. Викликається з планувальника."""
    for source in DOCUMENT_SOURCES:
        try:
            await _check_source(bot, source)
        except Exception as e:
            print(f"Документи [{source['id']}]: неочікувана помилка — {e}")
