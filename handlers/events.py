"""
Парсинг анонсів подій з календаря Миколаївської міської ради
(https://mkrada.gov.ua/calendar/) для ранкового повідомлення.

Сторінка — звичайний серверний HTML (не JSON API), з формою фільтра
за датою (День/Місяць/Рік "З" і "ПО"). Замість того, щоб жорстко
прописувати точні назви GET-параметрів форми (вони не задокументовані
і можуть змінитись при оновленні сайту), парсер:
  1. Завантажує сторінку календаря БЕЗ параметрів фільтра.
  2. З HTML дістає всі картки подій (дата + опис).
  3. Сам фільтрує по сьогоднішній даті на стороні бота (порівнює
     дату, видобуту з тексту картки, з datetime.now()), а не покладається
     на серверний фільтр.

Це трохи менш ефективно (завантажуємо весь дефолтний список, не лише
сьогоднішній день), але набагато стійкіше до змін на сайті — навіть
якщо точний формат query-параметрів фільтра зміниться, парсинг і
порівняння дат на нашій стороні продовжить працювати, доки сторінка
взагалі показує події з найближчого майбутнього в дефолтній видачі.

ВАЖЛИВО (для майбутньої підтримки): якщо одного дня цей парсер почне
повертати порожній список, хоча на сайті точно є події — найімовірніша
причина: дефолтна видача без фільтра більше не показує сьогоднішній
день (наприклад показує лише наступний тиждень). Тоді доведеться
розібрати реальну HTML-форму (атрибути name= у <select>) і явно
підставляти GET-параметри з сьогоднішньою датою.
"""

import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://mkrada.gov.ua/calendar/"

MONTHS_UA_GENITIVE = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

# Шаблон дати у форматі "18 Червня 2026" (день, місяць у родовому
# відмінку чи з великої літери, рік), як вона зустрічається на сторінці.
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
    списку dict {time, title}. Якщо подій немає або сталась помилка
    (сайт недоступний, змінилась структура сторінки) — повертає [],
    щоб ранкове повідомлення просто обійшлось без цього блоку, а не
    зламалось.
    """
    try:
        response = requests.get(CALENDAR_URL, timeout=10)
        if response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        today = datetime.now().date()
        events = []

        # Картки подій на сторінці зазвичай йдуть як заголовок з датою/часом
        # ("18 Червня 2026, 14:00") одразу за яким слідує текст опису.
        # Шукаємо всі текстові вузли, що відповідають шаблону дати, і
        # витягуємо найближчий сусідній текст як заголовок події.
        candidates = soup.find_all(string=DATE_PATTERN)

        for date_node in candidates:
            event_date = _parse_event_date(str(date_node))
            if event_date != today:
                continue

            # Час, якщо є, в тому ж текстовому вузлі через кому
            time_match = re.search(r"(\d{1,2}:\d{2})", str(date_node))
            time_text = time_match.group(1) if time_match else None

            # Заголовок події шукаємо як наступний сусідній тег ПІСЛЯ
            # батьківського елемента, що містить дату (а не наступний
            # текстовий вузол взагалі — це могло б знову захопити саму
            # дату чи службовий текст всередині того ж тега).
            title = None
            parent_tag = date_node.find_parent()
            if parent_tag:
                sibling = parent_tag.find_next_sibling()
                # Пропускаємо порожні сиблінги (порожні рядки, роздільники)
                while sibling is not None and not sibling.get_text(strip=True):
                    sibling = sibling.find_next_sibling()
                if sibling is not None:
                    title = sibling.get_text(strip=True)

            if not title:
                continue

            # Прибираємо URL з тексту (на сторінці часто йдуть посилання
            # на порядок денний/трансляцію прямо в тілі опису) — для
            # короткого ранкового анонсу вони не потрібні.
            title = re.sub(r"https?://\S+", "", title).strip()
            title = re.sub(r"\s{2,}", " ", title)

            # Відсікаємо зайве технічне сміття, якщо випадково захопили
            # навігацію чи футер замість опису події
            if len(title) < 5:
                continue

            # Для ранкового повідомлення довгі офіційні формулювання
            # скорочуємо, щоб не розтягувати текст
            if len(title) > 200:
                title = title[:200].rsplit(" ", 1)[0] + "..."

            events.append({"time": time_text, "title": title})

        return events
    except Exception:
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
