"""
Парсинг анонсів подій з календаря Миколаївської міської ради
(https://mkrada.gov.ua/calendar/) для ранкового повідомлення.

Структура сторінки (підтверджено на реальному HTML, не вгадана):

    <div class="news_line_item">
        <div class="date">18 Червня 2026, 14:00</div>
        Текст анонсу йде ПРЯМО тут, як простий текстовий вузол —
        НЕ всередині окремого тега (важливо для парсингу: не можна
        просто взяти .find_next_sibling(), бо тут немає сусіднього
        тега з текстом, є "голий" текстовий вузол поряд із div.date)
        <div class="page_split_bar"></div>
    </div>

Тому заголовок події збирається як конкатенація всіх НЕ-тегових
(NavigableString) дочірніх вузлів div.news_line_item, окрім div.date
і div.page_split_bar.

Фільтр дат на сторінці підтримує GET-параметри (підтверджено робочим
прикладом від користувача):
    ?c=0&fd=16&fm=6&fy=2026&td=25&tm=6&ty=2026&o=0&s=
де fd/fm/fy = "З" (from day/month/year), td/tm/ty = "ПО" (to day/month/year),
c = категорія (0 = всі), o = порядок сортування, s = пошуковий рядок.

Запитуємо одразу fd=td=сьогодні, fm=tm=поточний місяць, fy=ty=поточний рік —
тобто фільтруємо на стороні сервера, а не вручну в Python. Якщо з якоїсь
причини фільтр перестане працювати (сайт ігнорує параметри) — парсер
однаково підстраховується і додатково звіряє дату кожної картки з
сьогоднішньою датою перед тим як її врахувати.
"""

import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag

CALENDAR_URL = "https://mkrada.gov.ua/calendar/"

MONTHS_UA_GENITIVE = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MONTHS_UA_GENITIVE.keys()) + r")\s+(\d{4})",
    re.IGNORECASE,
)


def _parse_event_date(date_text):
    match = DATE_PATTERN.search(date_text.lower())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = MONTHS_UA_GENITIVE.get(month_name.lower())
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day)).date()
    except ValueError:
        return None


def get_today_events():
    """
    Повертає список подій на сьогодні з календаря міськради у вигляді
    списку dict {time, title}. У разі будь-якої помилки (сайт недоступний,
    змінилась структура сторінки) повертає [] — щоб ранкове повідомлення
    просто обійшлось без цього блоку, а не зламалось.
    """
    try:
        today = datetime.now()
        params = {
            "c": 0,
            "fd": today.day, "fm": today.month, "fy": today.year,
            "td": today.day, "tm": today.month, "ty": today.year,
            "o": 0,
            "s": "",
        }
        response = requests.get(CALENDAR_URL, params=params, timeout=10)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        today_date = today.date()
        events = []

        for item in soup.find_all("div", class_="news_line_item"):
            date_div = item.find("div", class_="date")
            if not date_div:
                continue

            date_text = date_div.get_text(strip=True)
            event_date = _parse_event_date(date_text)
            if event_date != today_date:
                continue  # підстраховка, навіть якщо серверний фільтр уже відфільтрував

            time_match = re.search(r"(\d{1,2}:\d{2})", date_text)
            time_text = time_match.group(1) if time_match else None

            # Заголовок — конкатенація текстових вузлів, що лежать
            # прямо в item, окрім будь-яких тегів (div.date, div.page_split_bar
            # і подібних).
            title_parts = []
            for child in item.children:
                if isinstance(child, Tag):
                    continue
                text = str(child).strip()
                if text:
                    title_parts.append(text)
            title = " ".join(title_parts)

            # Прибираємо URL з тексту — на сторінці посилання на порядок
            # денний/трансляцію часто йдуть прямо в тілі анонсу
            title = re.sub(r"https?://\S+", "", title).strip()
            title = re.sub(r"\s{2,}", " ", title)

            if len(title) < 5:
                continue

            if len(title) > 200:
                title = title[:200].rsplit(" ", 1)[0] + "..."

            events.append({"time": time_text, "title": title})

        return events
    except Exception as e:
        print("Помилка отримання подій міськради: " + str(e))
        return []


def format_events_for_prompt(events):
    """Готує короткий текстовий блок подій для вставки у промпт AI."""
    if not events:
        return None
    lines = []
    for e in events:
        if e.get("time"):
            lines.append(f"- о {e['time']}: {e['title']}")
        else:
            lines.append(f"- {e['title']}")
    return "\n".join(lines)
