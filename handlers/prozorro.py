"""
Моніторинг тендерів Прозорро.

Логіка:
1. Раз на годину запитуємо /api/2.5/tenders?offset=... — це повертає легкий
   список {id, dateModified} тендерів, що з'явились/змінились з минулого разу,
   і новий offset для наступного запиту (інкрементальне опитування).
2. Для кожного нового id робимо окремий запит /api/2.5/tenders/{id},
   щоб отримати повну інформацію (сума, замовник, регіон, назва).
3. Фільтруємо: регіон замовника = Миколаївська область, сума >= 1 млн грн.
4. Якщо тендер підходить і ще не був відісланий раніше — формуємо і шлемо
   повідомлення в групу, зберігаємо tender_id і message_id в storage.

Дедублікація: кожен tender_id обробляється і шлеться лише один раз
(перевірка через storage.is_tender_seen).
"""

import os
import requests

from handlers import storage

API_BASE = "https://public-api.openprocurement.org/api/2.5"
TARGET_REGION = "Миколаївська область"
MIN_AMOUNT = 1_000_000

PROZORRO_CHAT_ID = os.environ.get("PROZORRO_CHAT_ID")


def _fetch_tender_list(offset=None):
    """Повертає (список id-тендерів для перевірки, новий offset)."""
    params = {}
    if offset:
        params["offset"] = offset

    response = requests.get(f"{API_BASE}/tenders", params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    ids = [item["id"] for item in data.get("data", [])]
    next_offset = data.get("next_page", {}).get("offset")
    return ids, next_offset


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


async def check_prozorro_tenders(bot):
    if not PROZORRO_CHAT_ID:
        print("Помилка Прозорро: PROZORRO_CHAT_ID не задано")
        return

    try:
        offset = storage.get_offset()
        tender_ids, next_offset = _fetch_tender_list(offset)

        for tender_id in tender_ids:
            if storage.is_tender_seen(tender_id):
                continue

            tender = _fetch_tender_details(tender_id)
            if not tender:
                continue

            if not _matches_criteria(tender):
                continue

            text, real_tender_id, title, amount, buyer = _format_message(tender)

            message = await bot.send_message(
                chat_id=PROZORRO_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            from datetime import datetime
            storage.mark_tender_sent(
                tender_id=real_tender_id,
                message_id=message.message_id,
                title=title,
                amount=amount,
                buyer=buyer,
                sent_at=datetime.now().isoformat(),
            )

        if next_offset:
            storage.set_offset(next_offset)

    except Exception as e:
        print("Помилка перевірки Прозорро: " + str(e))
