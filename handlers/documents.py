"""
Моніторинг нових документів органів влади Миколаївщини.

Підключені джерела:
- Розпорядження міського голови Миколаєва (mkrada.gov.ua, ?c=4)
- Розпорядження голови ОВА/ОВА (mk.gov.ua)

Архітектура конфіг-driven: щоб додати нове джерело — додати рядок у
DOCUMENT_SOURCES. Якщо структура HTML нового сайту відрізняється —
написати окрему функцію парсера і вказати її в полі "parser".

Логіка для кожного джерела:
1. Завантажуємо список документів (перша сторінка або пошук за датою).
2. Порівнюємо ID з тими що вже бачили (storage).
3. Нові → формуємо пост → надсилаємо в канал.
4. Зберігаємо нові ID.

Перший запуск: зберігаємо baseline БЕЗ відправки (захист від спаму).
"""

import os
import re
import asyncio
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from handlers import storage

MKRADA_BASE = "https://mkrada.gov.ua"
OVA_BASE = "https://mk.gov.ua"

DOCUMENTS_CHAT_ID = os.environ.get("DOCUMENTS_CHAT_ID") or os.environ.get("PROZORRO_CHAT_ID")

MONTHS_UA = {
    "Січня": "01", "Лютого": "02", "Березня": "03", "Квітня": "04",
    "Травня": "05", "Червня": "06", "Липня": "07", "Серпня": "08",
    "Вересня": "09", "Жовтня": "10", "Листопада": "11", "Грудня": "12",
}


# ---------- Парсери ----------

def _parse_mkrada(source):
    """
    Парсер для mkrada.gov.ua/documents/.
    Структура: div.news_line_item > a[href] + div.date > strong(номер)
    ID документа — числовий з URL /documents/51982.html
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

            match = re.search(r"/documents/(\d+)\.html", a["href"])
            if not match:
                continue
            doc_id = match.group(1)

            date_div = item.find("div", class_="date")
            title = a.get_text(strip=True)

            strong = date_div.find("strong") if date_div else None
            number = strong.get_text(strip=True) if strong else None

            date = None
            if date_div:
                dm = re.search(r"\((\d+)\s+(\S+)\s+(\d{4})\)", date_div.get_text(strip=True))
                if dm:
                    day, month_name, year = dm.groups()
                    month = MONTHS_UA.get(month_name, "??")
                    date = f"{int(day):02d}.{month}.{year}"

            url = MKRADA_BASE + a["href"]
            results.append({"id": doc_id, "title": title, "number": number, "date": date, "url": url})

        return results
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка — {e}")
        return []


def _parse_ova(source):
    """
    Парсер для mk.gov.ua/ua/oda/order/.
    Структура: a.list-group-item[href="?doc_id=18165"] >
                 h4.list-group-item-heading (номер і дата)
                 p.list-group-item-text (назва)
    ID документа — числовий з query-параметра ?doc_id=18165.
    Запит з фільтром по даті: s_date і e_date (формат YYYY-MM-DD).
    Беремо діапазон "останні 3 дні" — страховка якщо прогін пропустив
    день (вихідні, перезапуск бота тощо).
    """
    try:
        today = datetime.now()
        s_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
        e_date = today.strftime("%Y-%m-%d")

        params = {
            "action": "search",
            "uin": "",
            "s_date": s_date,
            "e_date": e_date,
            "type": source.get("type", 1),
            "publisher": source.get("publisher", 1),
            "memo": "",
        }
        response = requests.get(source["url"], params=params, timeout=15)
        if response.status_code != 200:
            print(f"Документи [{source['id']}]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        for item in soup.find_all("a", href=True):
            if "doc_id=" not in item.get("href", ""):
                continue

            match = re.search(r"doc_id=(\d+)", item["href"])
            if not match:
                continue
            doc_id = match.group(1)

            h4 = item.find("h4")
            h4_text = h4.get_text(strip=True) if h4 else ""

            strong = item.find("strong")
            number = strong.get_text(strip=True) if strong else None

            date = None
            dm = re.search(r"вiд\s+(\d+)\s+(\S+)\s+(\d{4})", h4_text, re.IGNORECASE)
            if dm:
                day, month_name, year = dm.groups()
                month = MONTHS_UA.get(month_name.capitalize(), "??")
                date = f"{int(day):02d}.{month}.{year}"

            p = item.find("p", class_="list-group-item-text")
            title = p.get_text(strip=True) if p else h4_text
            # Прибираємо стрілку → яка іноді з'являється в кінці
            title = title.rstrip(" →")

            url = OVA_BASE + "/ua/oda/order/" + item["href"]
            results.append({"id": doc_id, "title": title, "number": number, "date": date, "url": url})

        return results
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка — {e}")
        return []


# ---------- Конфігурація джерел ----------

DOCUMENT_SOURCES = [
    {
        "id": "mayor_orders",
        "name": "Розпорядження міського голови Миколаєва",
        "emoji": "📋",
        "url": f"{MKRADA_BASE}/documents/",
        "params": {"c": 4, "o": "DESC"},
        "parser": _parse_mkrada,
    },
    {
        "id": "ova_orders",
        "name": "Розпорядження голови ОВА",
        "emoji": "🏛",
        "url": f"{OVA_BASE}/ua/oda/order/",
        "type": 1,
        "publisher": 1,
        "parser": _parse_ova,
    },
    # Майбутні джерела — просто додати рядок:
    # {
    #     "id": "council_decisions",
    #     "name": "Рішення міської ради Миколаєва",
    #     "emoji": "🏙",
    #     "url": f"{MKRADA_BASE}/documents/",
    #     "params": {"c": 1, "o": "DESC"},
    #     "parser": _parse_mkrada,
    # },
    # {
    #     "id": "oblrada_orders",
    #     "name": "Розпорядження голови облради",
    #     "emoji": "📜",
    #     "url": "...",
    #     "parser": _parse_???,
    # },
]


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
        num = doc.get("number") or ""
        num_text = num if num.startswith("№") else (f"№ {num}" if num else "Документ")
        date_text = f" від {doc['date']}" if doc.get("date") else ""
        link = f'<a href="{doc["url"]}">{_escape_html(num_text)}</a>{date_text}'
        lines.append(link)
        lines.append(_escape_html(doc["title"]))
        lines.append("")

    return "\n".join(lines).strip()


# ---------- Основна логіка ----------

async def _check_source(bot, source):
    loop = asyncio.get_event_loop()
    parser = source["parser"]

    docs = await loop.run_in_executor(None, parser, source)
    if not docs:
        return

    seen_ids = await loop.run_in_executor(None, storage.get_seen_document_ids, source["id"])
    fetched_ids = [d["id"] for d in docs]

    if seen_ids is None:
        print(f"Документи [{source['id']}]: перший запуск, зберігаємо baseline ({len(fetched_ids)} документів)")
        await loop.run_in_executor(None, storage.save_seen_document_ids, source["id"], fetched_ids)
        return

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
        print(f"Документи [{source['id']}]: помилка відправки — {e}")
        return

    all_ids = list(seen_set) + [d["id"] for d in new_docs]
    await loop.run_in_executor(None, storage.save_seen_document_ids, source["id"], all_ids)


async def check_documents(bot):
    """Перевіряє всі джерела. Викликається з планувальника і /documents."""
    for source in DOCUMENT_SOURCES:
        try:
            await _check_source(bot, source)
        except Exception as e:
            print(f"Документи [{source['id']}]: неочікувана помилка — {e}")


async def test_documents(bot):
    """
    Надсилає перший документ з кожного джерела в канал як тестовий пост.
    НЕ змінює baseline і НЕ оновлює seen_ids — лише перевіряє що
    парсинг і відправка працюють. Викликається через /documents_test.
    """
    if not DOCUMENTS_CHAT_ID:
        print("test_documents: DOCUMENTS_CHAT_ID не задано")
        return

    loop = asyncio.get_event_loop()
    sent = 0

    for source in DOCUMENT_SOURCES:
        try:
            docs = await loop.run_in_executor(None, source["parser"], source)
            if not docs:
                print(f"test_documents [{source['id']}]: список порожній")
                continue

            # Беремо тільки перший документ
            test_doc = docs[0]
            text = _format_post(source, [test_doc])
            text = f"🧪 <i>Тестовий пост</i>\n\n{text}"

            await bot.send_message(
                chat_id=DOCUMENTS_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent += 1
            print(f"test_documents [{source['id']}]: надіслано тестовий пост (id={test_doc['id']})")
        except Exception as e:
            print(f"test_documents [{source['id']}]: помилка — {e}")

    return sent
