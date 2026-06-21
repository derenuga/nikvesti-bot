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


import hashlib


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


def _parse_oblrada(source):
    """
    Парсер для mk-oblrada.gov.ua/proekty-rishen?group_id=14.
    Структура: table.table-striped > tr > td[0] (назва + посилання на PDF)
                                          td[1] (пояснювальна записка, якщо є)
    ID документа — md5 від повної назви (PDF іноді відсутній, тому не можна
    покладатись на URL файлу як ідентифікатор).
    Підтримує пагінацію: гортає всі сторінки (?page=N) поки є посилання.
    """
    OBLRADA_BASE = "https://mk-oblrada.gov.ua"
    try:
        results = []
        page = 1
        while True:
            params = dict(source.get("params", {}))
            params["page"] = page
            response = requests.get(source["url"], params=params, timeout=15)
            if response.status_code != 200:
                print(f"Документи [{source['id']}]: HTTP {response.status_code} (сторінка {page})")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            table = soup.find("table", class_="table-striped")
            if not table:
                break

            rows = table.find_all("tr")[1:]  # пропускаємо заголовок
            if not rows:
                break

            for row in rows:
                cols = row.find_all("td")
                if not cols:
                    continue
                title_td = cols[0]

                # Посилання на PDF в першій колонці
                pdf_link = title_td.find("a", href=True)
                if pdf_link:
                    title = pdf_link.get_text(strip=True)
                    pdf_url = pdf_link["href"]
                    if not pdf_url.startswith("http"):
                        pdf_url = OBLRADA_BASE + pdf_url
                else:
                    b = title_td.find("b")
                    title = b.get_text(strip=True) if b else title_td.get_text(strip=True)
                    pdf_url = None

                if not title or len(title) < 5:
                    continue

                # Пояснювальна записка
                expl_td = cols[1] if len(cols) > 1 else None
                expl_link = expl_td.find("a", href=True) if expl_td else None
                expl_url = expl_link["href"] if expl_link else None
                if expl_url and not expl_url.startswith("http"):
                    expl_url = OBLRADA_BASE + expl_url

                doc_id = hashlib.md5(title.encode()).hexdigest()[:16]
                results.append({
                    "id": doc_id,
                    "title": title,
                    "number": None,
                    "date": None,
                    "url": pdf_url,
                    "explanation_url": expl_url,
                    "has_file": pdf_url is not None,
                })

            # Перевіряємо чи є наступна сторінка
            next_page = soup.find("a", class_="page", attrs={"data-page": str(page + 1)})
            if not next_page:
                break
            page += 1

        return results
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка — {e}")
        return []


def _parse_mkrada_decisions(source):
    """
    Парсер для mkrada.gov.ua/content/proekti-rishen-miskradi.html
    Структура: div.p-content > p (заголовок блоку) + ol (список документів)
    Блоки: "Перелік...", "Поточні питання", "ТИМЧАСОВІ СПОРУДИ",
           "Проєкти рішень, зареєстровані...", "Земельні питання" тощо.
    Логіка: беремо ВСІ p що мають наступний ol — це і є блоки документів.
    Нові документи завжди в кінці кожного блоку.
    ID: md5 від URL файлу. Пояснювальні записки ігноруємо.
    ВАЖЛИВО: response.encoding = 'utf-8' — сервер не вказує кодування,
    requests читає як latin-1 що ламає кирилицю.
    """
    SKIP_TEXTS = {'розпорядження', 'скликання'}
    try:
        response = requests.get(source["url"], timeout=15)
        response.encoding = 'utf-8'
        if response.status_code != 200:
            print(f"Документи [{source['id']}]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.find("div", class_="p-content")
        if not content:
            print(f"Документи [{source['id']}]: div.p-content не знайдено")
            return []

        results = []
        for p in content.find_all("p"):
            block_title = p.get_text(strip=True)
            if not block_title or len(block_title) < 5:
                continue
            if any(s in block_title.lower() for s in SKIP_TEXTS):
                continue
            ol = p.find_next_sibling("ol")
            if not ol:
                continue

            for li in ol.find_all("li", recursive=False):
                a = li.find("a", href=lambda h: h and any(
                    ext in h.lower() for ext in [".pdf", ".doc", ".docx", ".rtf"]
                ))
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title or "пояснювальна" in title.lower():
                    continue
                href = a["href"]
                if "пояснювальна" in href.lower():
                    continue

                li_text = li.get_text()
                date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", li_text)
                date_text = date_match.group(1) if date_match else None

                doc_id = hashlib.md5(href.encode()).hexdigest()[:16]
                url = MKRADA_BASE + href if href.startswith("/") else href
                results.append({
                    "id": doc_id,
                    "title": title,
                    "number": None,
                    "date": date_text,
                    "url": url,
                    "block_title": block_title,
                })

        return results
    except Exception as e:
        print(f"Документи [{source['id']}]: помилка — {e}")
        return []



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
    {
        "id": "oblrada_decisions",
        "name": "Проєкти рішень Миколаївської обласної ради",
        "emoji": "📜",
        "url": "https://mk-oblrada.gov.ua/proekty-rishen",
        "params": {"group_id": 14},
        "index_url": "https://mk-oblrada.gov.ua/proekty-rishen",
        "parser": _parse_oblrada,
    },
    {
        "id": "mkrada_decisions",
        "name": "Проєкти рішень Миколаївської міської ради",
        "emoji": "🏙",
        "url": f"{MKRADA_BASE}/content/proekti-rishen-miskradi.html",
        "index_url": f"{MKRADA_BASE}/content/proekti-rishen-miskradi.html",
        "baseline_per_block": 3,  # кількість останніх документів з кожного блоку для тестової відправки
        "parser": _parse_mkrada_decisions,
    },
    # Майбутні джерела:
    # {
    #     "id": "council_decisions",
    #     "name": "Проєкти рішень міської ради Миколаєва",
    #     "emoji": "🏙",
    #     "url": f"{MKRADA_BASE}/documents/",
    #     "params": {"c": 1, "o": "DESC"},
    #     "parser": _parse_mkrada,
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

    # Якщо є block_title — групуємо по блоках
    has_blocks = any(d.get("block_title") for d in new_docs)
    if has_blocks:
        current_block = None
        for doc in new_docs:
            block = doc.get("block_title", "")
            if block != current_block:
                current_block = block
                lines.append(f"<i>{_escape_html(block)}</i>")
                lines.append("")

            date_text = f"{doc['date']} — " if doc.get("date") else ""
            title_escaped = _escape_html(doc["title"])
            if doc.get("url"):
                lines.append(f'📃 {date_text}<a href="{doc["url"]}">{title_escaped}</a>')
            else:
                lines.append(f"📃 {date_text}{title_escaped} <i>(файл відсутній)</i>")
            lines.append("")
    else:
        for doc in new_docs:
            title_escaped = _escape_html(doc["title"])

            # Джерела з номером і датою (міськрада розпорядження, ОВА)
            if doc.get("number") or doc.get("date"):
                num = doc.get("number") or ""
                num_text = num if num.startswith("№") else (f"№ {num}" if num else "Документ")
                date_text = f" від {doc['date']}" if doc.get("date") else ""
                if doc.get("url"):
                    link = f'<a href="{doc["url"]}">{_escape_html(num_text)}</a>{date_text}'
                else:
                    link = f"{_escape_html(num_text)}{date_text}"
                lines.append(link)
                lines.append(title_escaped)

            # Облрада — перші два слова як лінк, решта звичайним текстом
            else:
                words = doc["title"].split()
                if doc.get("url") and len(words) >= 2:
                    link_words = _escape_html(" ".join(words[:2]))
                    rest_words = _escape_html(" ".join(words[2:])) if len(words) > 2 else ""
                    line = f'📃 <a href="{doc["url"]}">{link_words}</a>'
                    if rest_words:
                        line += f" {rest_words}"
                elif doc.get("url"):
                    line = f'📃 <a href="{doc["url"]}">{title_escaped}</a>'
                else:
                    line = f"📃 {title_escaped} <i>(файл відсутній)</i>"
                lines.append(line)

            lines.append("")

    # Для джерел з index_url додаємо посилання на повний перелік
    if source.get("index_url"):
        lines.append(f'Перелік: <a href="{source["index_url"]}">{source["index_url"]}</a>')

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
        # ВАЖЛИВО: baseline НЕ "зберегти все без відправки".
        # Правило: зберегти як бачені всі крім N останніх, N відправити одразу
        # щоб перевірити що парсинг і відправка працюють після деплою.
        # N = baseline_per_block (по блоках) або 10 (для звичайних джерел).

        baseline_per_block = source.get("baseline_per_block")

        if baseline_per_block:
            # Для джерел з блоками (mkrada_decisions):
            # беремо по N останніх з кожного блоку для відправки.
            # N = baseline_per_block = 3 (задано в конфігу джерела)
            blocks = {}
            for d in docs:
                bt = d.get("block_title", "")
                blocks.setdefault(bt, []).append(d)

            to_send = []
            for bt, block_docs in blocks.items():
                to_send.extend(block_docs[-baseline_per_block:])
            to_send_ids = {d["id"] for d in to_send}
            baseline_ids = [d["id"] for d in docs if d["id"] not in to_send_ids]
            new_docs = to_send
        else:
            # Звичайні джерела — 10 найновіших
            if len(fetched_ids) <= 10:
                baseline_ids = []
                new_docs = docs
            else:
                baseline_ids = fetched_ids[10:]
                new_docs = docs[:10]

        print(f"Документи [{source['id']}]: перший запуск, baseline {len(baseline_ids)}, відправляємо {len(new_docs)}")

        if new_docs and DOCUMENTS_CHAT_ID:
            text = _format_post(source, new_docs)
            try:
                await bot.send_message(
                    chat_id=DOCUMENTS_CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print(f"Документи [{source['id']}]: помилка відправки при першому запуску — {e}")

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
