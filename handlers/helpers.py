import re
from datetime import datetime
from calendar import monthrange

MONTHS_UK = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4,
    "травень": 5, "червень": 6, "липень": 7, "серпень": 8,
    "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
}

MONTHS_UA = {
    1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
    5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
    9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
}

def parse_month_arg(args):
    if not args:
        return None, None, None
    month_str = args[0].lower()
    month_num = MONTHS_UK.get(month_str)
    if not month_num:
        return None, None, None
    now = datetime.now()
    year = now.year if month_num <= now.month else now.year - 1
    last_day = monthrange(year, month_num)[1]
    start = datetime(year, month_num, 1)
    end = datetime(year, month_num, last_day, 23, 59, 59)
    label = f"{MONTHS_UA[month_num]} {year}"
    return start, end, label

def extract_article_id(url):
    """Числовий ID матеріалу (nodes.id) з URL або шляху nikvesti.com. Два формати:
    - зі слагом: /news/justice/320102-slug → '320102' (ID перед дефісом);
    - без слага (старі матеріали, зокрема /ru/…): /news/politics/111079 →
      '111079' (ID — останній сегмент шляху)."""
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    match = re.search(r'/(\d{4,})-', clean)
    if match:
        return match.group(1)
    match = re.search(r'/(\d{4,})$', clean)
    return match.group(1) if match else None


def normalize_https_url(url):
    """Нормалізує URL з env: обрізає пробіли і хвостовий слеш, додає https://
    якщо схему не вказали (реальний кейс 27.07: WEBAPP_URL без схеми →
    Telegram відхиляє web_app кнопку «only https links are allowed»)."""
    url = (url or "").strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


_app_link_cache = {}


async def resolve_app_link(bot, explicit=None):
    """Базовий лінк Mini App для кнопок у ГРУПОВИХ чатах.

    У приваті кнопку апки дає web_app — але Telegram дозволяє web_app-кнопки
    ЛИШЕ в приваті, у групі їх не приймає зовсім. Тому в групу йде звичайний
    URL t.me, який Telegram сам відкриває як апку.

    Лінк не потребує ручного налаштування: https://t.me/<bot> веде в Main Mini
    App, і достатньо username бота. WEBAPP_DIRECT_LINK лишається як override —
    коли апку зареєстрували окремо через /newapp і потрібна форма з short name
    (https://t.me/<bot>/<app>).

    Повертає (url, джерело) — джерело потрібне діагностиці. get_me смикаємо
    раз на процес; збій не має валити те, заради чого кнопку показують."""
    key = explicit or ""
    if key in _app_link_cache:
        return _app_link_cache[key]
    if explicit:
        result = (explicit, "env")
    else:
        try:
            me = await bot.get_me()
            username = (me.username or "").lstrip("@")
        except Exception as e:
            print(f"resolve_app_link: username бота не дістався — {e}")
            return None, None
        if not username:
            return None, None
        result = (f"https://t.me/{username}", "get_me")
    _app_link_cache[key] = result
    return result


def app_link_with_param(base_url, param):
    """Дочіпляє startapp=… до лінка апки, поважаючи вже наявні параметри."""
    if not base_url:
        return None
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}startapp={param}"


def escape_html(text):
    """Екранування для Telegram parse_mode=HTML. Єдина копія для всіх модулів."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


import requests
from bs4 import BeautifulSoup

def get_author_from_url(url):
    try:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        author_tag = soup.find("meta", attrs={"name": "author"})
        if author_tag:
            return author_tag.get("content")
        author_tag = soup.find("meta", property="article:author")
        if author_tag:
            return author_tag.get("content")
        return None
    except:
        return None
