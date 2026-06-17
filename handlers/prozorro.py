"""
Моніторинг тендерів Прозорро.

Логіка:
1. Раз на годину запитуємо /api/2.5/tenders?offset=... — це повертає легкий
   список {id, dateModified} тендерів, що з'явились/змінились з минулого разу,
   і новий offset для наступного запиту (інкрементальне опитування).
   Стрічка йде сторінками по MAX_LIMIT записів; ми гортаємо сторінки одна
   за одною (з невеликою паузою між запитами, як радить документація API),
   поки не "наздоганяємо" поточний момент (порожня відповідь) або поки не
   досягнемо MAX_PAGES_PER_RUN за один запуск — щоб не зависнути назавжди
   при першому холодному старті, коли offset ще не збережений.
2. Для кожного нового id робимо окремий запит /api/2.5/tenders/{id},
   щоб отримати повну інформацію (сума, замовник, регіон, назва).
3. Фільтруємо: регіон замовника = Миколаївська область, сума >= 1 млн грн.
4. Якщо тендер підходить і ще не був відісланий раніше — формуємо і шлемо
   повідомлення в групу, зберігаємо tender_id і message_id в storage.

Дедублікація: кожен tender_id обробляється і шлеться лише один раз
(перевірка через storage.is_tender_seen).

Примітка про холодний старт: при першому запуску offset порожній, і стрічка
може містити дуже старі записи зміни на самому початку. MAX_PAGES_PER_RUN
дозволяє за кілька годинних прогонів (або кілька ручних /prozorro)
поступово "наздогнати" поточний момент.
"""

import os
import asyncio
from datetime import datetime

import requests

from handlers import storage

API_BASE = "https://public-api.prozorro.gov.ua/api/2.5"
TARGET_REGION = "Миколаївська область"
MIN_AMOUNT = 1_000_000

MAX_LIMIT = 1000          # максимальний розмір сторінки, який підтримує API
MAX_PAGES_PER_RUN = 20     # запобіжник, щоб один прогон не тривав вічно
PAGE_DELAY_SECONDS = 1     # пауза між сторінками, щоб не спамити API

PROZORRO_CHAT_ID = os.environ.get("PROZORRO_CHAT_ID")


def _fetch_tender_page(offset=None):
    """Повертає (список id-тендерів на цій сторінці, новий offset, чи сторінка непорожня)."""
    params = {"limit": MAX_LIMIT}
    if offset:
        params["offset"] = offset

    response = requests.get(f"{API_BASE}/tenders", params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    items = data.get("data", [])
    ids = [item["id"] for item in items]
    next_offset = data.get("next_page", {}).get("offset")
    return ids, next_offset, len(items) > 0


def _fetch_tender_details(tender_id):
    """Повертає повний об'єкт тендера, або None при помилці/відсутності."""
    try:
        response = requests.get(f"{API_BASE}/tenders/{tender_id}", timeout=30)
        if response.status_code != 200:
            return None
        return response.json().get("data")
    except requests.RequestException:
        return None


def _matches_criteria(tender):
    procuring_entity = tender.get("procuringEntity") or {}
    address = procuring_entity.get("address") or {}
    region = address.get("region")

    value = tender.get("value") or {}
    amount = value.get("amount")

    if region != TARGET_REGION:
        return False
    if amount is None or amount < MIN_AMOUNT:
        return False
    return True


def _format_message(tender):
    tender_id = tender.get("tenderID") or tender.get("id")
    title = (tender.get("title") or "Без назви").strip()
    buyer = (tender.get("procuringEntity", {}).get("name") or "Невідомий замовник").strip()
    amount = tender.get("value", {}).get("amount")
    currency = tender.get("value", {}).get("currency", "UAH")

    amount_text = f"{amount:,.0f}".replace(",", " ") if amount is not None else "н/д"
    url = f"https://prozorro.gov.ua/tender/{tender_id}"

    text = (
        f"💰 {amount_text} {currency}\n"
        f"🏛 <b>{_escape_html(buyer)}</b>\n"
        f'📋 <a href="{url}">{tender_id}</a>\n\n'
        f"<blockquote>{_escape_html(title)}</blockquote>"
    )
    return text, tender_id, title, amount, buyer


def _escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def _process_tender_id(bot, tender_id):
    """Перевіряє один тендер і шле повідомлення, якщо підходить. Повертає True, якщо відіслано."""
    if storage.is_tender_seen(tender_id):
        return False

    tender = _fetch_tender_details(tender_id)
    if not tender:
        return False

    if not _matches_criteria(tender):
        return False

    text, real_tender_id, title, amount, buyer = _format_message(tender)

    message = await bot.send_message(
        chat_id=PROZORRO_CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    storage.mark_tender_sent(
        tender_id=real_tender_id,
        message_id=message.message_id,
        title=title,
        amount=amount,
        buyer=buyer,
        sent_at=datetime.now().isoformat(),
    )
    return True


async def check_prozorro_tenders(bot):
    if not PROZORRO_CHAT_ID:
        print("Помилка Прозорро: PROZORRO_CHAT_ID не задано")
        return

    try:
        offset = storage.get_offset()
        total_checked = 0
        total_sent = 0
        pages_fetched = 0

        for page_num in range(MAX_PAGES_PER_RUN):
            tender_ids, next_offset, has_data = _fetch_tender_page(offset)
            pages_fetched += 1

            for tender_id in tender_ids:
                total_checked += 1
                sent = await _process_tender_id(bot, tender_id)
                if sent:
                    total_sent += 1

            if next_offset:
                offset = next_offset
                storage.set_offset(offset)

            if not has_data:
                # Дійшли до кінця стрічки — наздогнали поточний момент
                break

            await asyncio.sleep(PAGE_DELAY_SECONDS)

        print(
            f"Прозорро: опрацьовано сторінок={pages_fetched}, "
            f"перевірено тендерів={total_checked}, відіслано={total_sent}"
        )

    except Exception as e:
        print("Помилка перевірки Прозорро: " + str(e))

