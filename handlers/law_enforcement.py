"""
Моніторинг новин правоохоронних органів Миколаївщини.

Підключені джерела:
- Миколаївська обласна прокуратура (myk.gp.gov.ua/ua/news.html)

Архітектура конфіг-driven: щоб додати нове джерело — додати рядок у
LAW_ENFORCEMENT_SOURCES. Якщо структура HTML нового сайту відрізняється —
написати окрему функцію парсера і вказати її в полі "parser".

Розклад: 10:00, 13:00, 16:00 — три рази на день.
Всі нові новини за період між запусками відправляються одним повідомленням.

Перший запуск (baseline):
  ПРАВИЛО N=1 — зберігаємо всі новини крім 1 найновішої як бачені,
  1 найновішу відправляємо одразу — щоб перевірити що парсинг і
  відправка працюють після деплою.
"""

import os
import re
import asyncio
import requests
from bs4 import BeautifulSoup

from handlers import storage

BASE_URL_PROKURATURA = "https://myk.gp.gov.ua"

# Канал для постів — той самий що й документи та тендери
DOCUMENTS_CHAT_ID = os.environ.get("DOCUMENTS_CHAT_ID") or os.environ.get("PROZORRO_CHAT_ID")


# ---------- Парсери ----------

def _parse_prokuratura(source):
    """
    Парсер для myk.gp.gov.ua/ua/news.html
    Структура: section.default > ul > li > a.blue_bold[href="?...&id=424328"] >
                 p > span.grey_bold (дата) + текст (заголовок)
    ID: числовий з query-параметра id= у href.
    На сторінці ~6 новин, пагінація через ?fp=0, ?fp=10 тощо.
    """
    try:
        response = requests.get(
            source["url"],
            headers={"User-Agent": "NikVesti-Bot/1.0"},
            timeout=15,
        )
        if response.status_code != 200:
            print(f"Правоохоронці [{source['id']}]: HTTP {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        default = soup.find("section", class_="default")
        if not default:
            print(f"Правоохоронці [{source['id']}]: section.default не знайдено")
            return []

        ul = default.find("ul")
        if not ul:
            print(f"Правоохоронці [{source['id']}]: ul не знайдено")
            return []

        results = []
        for li in ul.find_all("li"):
            a = li.find("a", class_="blue_bold")
            if not a or not a.get("href"):
                continue

            m = re.search(r"id=(\d+)", a["href"])
            if not m:
                continue
            doc_id = m.group(1)

            # Дата зі span.grey_bold
            span = a.find("span", class_="grey_bold")
            date = span.get_text(strip=True) if span else ""

            # Заголовок — текст <p> без span і br
            p = a.find("p")
            title = ""
            if p:
                p_copy = BeautifulSoup(str(p), "html.parser").find("p")
                for tag in p_copy.find_all(["span", "br"]):
                    tag.decompose()
                title = p_copy.get_text(strip=True)

            if not title:
                continue

            url = BASE_URL_PROKURATURA + "/ua/news.html" + a["href"].lstrip("/ua/news.html")
            # href вже містить повний відносний шлях типу /ua/news.html?...
            url = BASE_URL_PROKURATURA + a["href"]

            results.append({
                "id": doc_id,
                "title": title,
                "date": date,
                "url": url,
            })

        return results

    except Exception as e:
        print(f"Правоохоронці [{source['id']}]: помилка — {e}")
        return []


# ---------- Конфіг джерел ----------

LAW_ENFORCEMENT_SOURCES = [
    {
        "id": "prokuratura",
        "name": "Прокуратура Миколаївщини",
        "emoji": "⚖️",
        "url": "https://myk.gp.gov.ua/ua/news.html",
        "parser": _parse_prokuratura,
    },
]


# ---------- Форматування ----------

def _escape_html(text):
    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_post(source, news_items):
    """Формує один пост з усіх нових новин джерела."""
    count = len(news_items)
    header = f"{source['emoji']} <b>{_escape_html(source['name'])}</b>"
    if count > 1:
        header += f" — {count} нові новини"

    lines = [header, ""]
    for item in news_items:
        title_esc = _escape_html(item["title"])
        date_esc = _escape_html(item.get("date", ""))
        url = item.get("url", "")

        if url:
            line = f'<a href="{url}">{title_esc}</a>'
        else:
            line = title_esc

        if date_esc:
            line += f" <i>({date_esc})</i>"

        lines.append(line)
        lines.append("")

    return "\n".join(lines).strip()


# ---------- Основна логіка ----------

async def _check_source(bot, source):
    loop = asyncio.get_event_loop()
    parser = source["parser"]

    docs = await loop.run_in_executor(None, parser, source)
    if not docs:
        return

    seen_ids = await loop.run_in_executor(
        None, storage.get_seen_document_ids, source["id"]
    )
    fetched_ids = [d["id"] for d in docs]

    if seen_ids is None:
        # Перший запуск.
        # BASELINE N=1: зберігаємо всі новини крім 1 найновішої як бачені,
        # 1 найновішу відправляємо одразу — щоб перевірити що парсинг
        # і відправка працюють після деплою. N=1 обрано бо прес-релізи
        # прокуратури виходять рідше ніж документи міськради, і одного
        # достатньо для перевірки формату.
        if len(fetched_ids) <= 1:
            baseline_ids = []
            new_docs = docs
        else:
            baseline_ids = fetched_ids[1:]   # всі крім найновішої — в baseline
            new_docs = docs[:1]              # 1 найновіша — відправляємо

        print(
            f"Правоохоронці [{source['id']}]: перший запуск, "
            f"baseline {len(baseline_ids)}, відправляємо {len(new_docs)}"
        )

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
                print(f"Правоохоронці [{source['id']}]: помилка відправки при першому запуску — {e}")

        # Зберігаємо всі ID як бачені
        await loop.run_in_executor(
            None, storage.save_seen_document_ids, source["id"], fetched_ids
        )
        return

    seen_set = set(seen_ids)
    new_docs = [d for d in docs if d["id"] not in seen_set]

    if not new_docs:
        return

    print(f"Правоохоронці [{source['id']}]: знайдено {len(new_docs)} нових")

    if not DOCUMENTS_CHAT_ID:
        print(f"Правоохоронці [{source['id']}]: DOCUMENTS_CHAT_ID не задано, пропускаємо")
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
        print(f"Правоохоронці [{source['id']}]: помилка відправки — {e}")
        return

    all_ids = list(seen_set) + [d["id"] for d in new_docs]
    await loop.run_in_executor(
        None, storage.save_seen_document_ids, source["id"], all_ids
    )


async def check_law_enforcement(bot):
    """Перевіряє всі джерела. Викликається з планувальника і /law."""
    for source in LAW_ENFORCEMENT_SOURCES:
        try:
            await _check_source(bot, source)
        except Exception as e:
            print(f"Правоохоронці [{source['id']}]: неочікувана помилка — {e}")
