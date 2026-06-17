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
   повідомлення в групу. Усі нові тендери за прогон накопичуються в пам'яті
   і записуються в storage ОДНИМ файловим записом наприкінці прогону —
   це критично, бо повторні файлові читання/записи на Railway Volume на
   кожен окремий тендер (сотні разів) самі по собі додавали суттєву
   затримку і блокували event loop бота навіть через run_in_executor.

Дедублікація: список уже відісланих tender_id завантажується ОДНИМ читанням
на старті прогону (storage.get_seen_tender_ids), звірка йде в пам'яті.

КРИТИЧНО ВАЖЛИВО: bot.py працює на asyncio event loop. Усі мережеві запити
через requests (синхронна бібліотека) і всі файлові операції зі storage
виконуються в окремому потоці через run_in_executor, інакше довгий цикл
повністю блокує бота — він не відповідає НІ на одну команду, поки цикл
не завершиться. Це сталось на практиці двічі (мережеві запити, потім
файлові операції storage) і виправлено цією версією файлу.
"""

import os
import asyncio
import time
from datetime import datetime

import requests

from handlers import storage

API_BASE = "https://public-api.prozorro.gov.ua/api/2.5"
TARGET_REGION = "Миколаївська область"
MIN_AMOUNT = 1_000_000

MAX_LIMIT = 50             # розмір сторінки для регулярних прогонів
MAX_PAGES_PER_RUN = 3       # запобіжник на один виклик /prozorro чи cron-тик
MAX_DETAIL_REQUESTS_PER_RUN = 100  # запобіжник на кількість детальних запитів (найдорожча частина)
PAGE_DELAY_SECONDS = 1      # пауза між сторінками, щоб не спамити API

PROZORRO_CHAT_ID = os.environ.get("PROZORRO_CHAT_ID")


# ---------- Синхронні функції (виконуються в окремому потоці через run_in_executor) ----------

def _fetch_tender_page_sync(offset=None, limit=MAX_LIMIT):
    """Повертає (список id-тендерів на цій сторінці, новий offset, чи сторінка непорожня)."""
    params = {"limit": limit}
    if offset:
        params["offset"] = offset

    response = requests.get(f"{API_BASE}/tenders", params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    items = data.get("data", [])
    ids = [item["id"] for item in items]
    next_offset = data.get("next_page", {}).get("offset")
    return ids, next_offset, len(items) > 0


def _fetch_tender_details_sync(tender_id):
    """Повертає повний об'єкт тендера, або None при помилці/відсутності."""
    try:
        response = requests.get(f"{API_BASE}/tenders/{tender_id}", timeout=30)
        if response.status_code != 200:
            return None
        return response.json().get("data")
    except requests.RequestException:
        return None


# ---------- Асинхронні обгортки над мережевими запитами ----------

async def _fetch_tender_page(offset=None, limit=MAX_LIMIT):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_tender_page_sync, offset, limit)


async def _fetch_tender_details(tender_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_tender_details_sync, tender_id)


# ---------- Фільтрація і форматування (швидкі, без I/O — лишаються синхронними) ----------

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


# ---------- Основна логіка ----------

async def _process_tender_id(bot, tender_id, seen_ids, newly_sent):
    """
    Перевіряє один тендер і шле повідомлення, якщо підходить.
    seen_ids: set вже відісланих id (звірка в пам'яті, без файлового I/O).
    newly_sent: список, куди додається запис про новий тендер (накопичення
    для одного фінального bulk-запису в storage наприкінці прогону).
    """
    if tender_id in seen_ids:
        return False

    tender = await _fetch_tender_details(tender_id)
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

    seen_ids.add(real_tender_id)
    newly_sent.append({
        "tender_id": real_tender_id,
        "message_id": message.message_id,
        "title": title,
        "amount": amount,
        "buyer": buyer,
        "sent_at": datetime.now().isoformat(),
    })
    return True


async def check_prozorro_tenders(bot):
    if not PROZORRO_CHAT_ID:
        print("Помилка Прозорро: PROZORRO_CHAT_ID не задано")
        return

    try:
        await asyncio.wait_for(_run_check_cycle(bot), timeout=120)
    except asyncio.TimeoutError:
        print("Прозорро: прогон перевищив 120 секунд, перервано (запобіжник від зависання)")
    except Exception as e:
        print("Помилка перевірки Прозорро: " + str(e))


async def _run_check_cycle(bot):
    loop = asyncio.get_event_loop()

    # Одне читання стану на старті прогону
    offset = await loop.run_in_executor(None, storage.get_offset)
    seen_ids = await loop.run_in_executor(None, storage.get_seen_tender_ids)
    newly_sent = []

    total_checked = 0
    total_sent = 0
    pages_fetched = 0
    final_offset = offset

    for page_num in range(MAX_PAGES_PER_RUN):
        tender_ids, next_offset, has_data = await _fetch_tender_page(final_offset)
        pages_fetched += 1

        for tender_id in tender_ids:
            if total_checked >= MAX_DETAIL_REQUESTS_PER_RUN:
                break
            total_checked += 1
            sent = await _process_tender_id(bot, tender_id, seen_ids, newly_sent)
            if sent:
                total_sent += 1

        if next_offset:
            final_offset = next_offset

        if not has_data or total_checked >= MAX_DETAIL_REQUESTS_PER_RUN:
            break

        await asyncio.sleep(PAGE_DELAY_SECONDS)

    # Один фінальний запис на диск за весь прогон (навіть якщо timeout — це не дійде,
    # тому при таймауті прогрес поточного прогону втрачається, але це безпечніше,
    # ніж дозволити боту висіти безкінечно)
    await loop.run_in_executor(None, storage.bulk_save, newly_sent, final_offset)

    print(
        f"Прозорро: опрацьовано сторінок={pages_fetched}, "
        f"перевірено тендерів={total_checked}, відіслано={total_sent}"
    )


# ---------- Діагностика штучного offset ----------

def build_artificial_offset(days_ago):
    """
    Конструює штучний offset на основі timestamp "N днів тому".
    Формат offset у Prozorro: {unix_timestamp}.{лот}.{хеш}.
    Хеш-частину підставляємо нульовою — це експериментально.
    """
    target_ts = time.time() - (days_ago * 86400)
    return f"{target_ts:.6f}.0.0000000000000000000000000000000"


async def diagnose_offset_jump(bot, chat_id, days_ago=14):
    """
    Діагностична перевірка: пробує штучний offset з МАЛЕНЬКИМ лімітом (5),
    щоб гарантовано не зависнути. НЕ зберігає offset в storage.
    """
    artificial_offset = build_artificial_offset(days_ago)

    report_lines = [f"🔬 Тест штучного offset (~{days_ago} днів тому):"]
    report_lines.append(f"Offset: <code>{artificial_offset}</code>")

    try:
        ids, next_offset, has_data = await _fetch_tender_page(artificial_offset, limit=5)
        report_lines.append("Статус: успіх (HTTP 200)")
        report_lines.append(f"Записів повернуто: {len(ids)}")

        if ids:
            sample_dates = []
            for tender_id in ids[:3]:
                tender = await _fetch_tender_details(tender_id)
                if tender:
                    sample_dates.append(tender.get("dateModified", "н/д"))
            report_lines.append("Приклади dateModified: " + ", ".join(sample_dates))
        else:
            report_lines.append("Сторінка порожня — можливо, offset вказує за межі стрічки.")

        report_lines.append(f"\nНаступний offset: <code>{next_offset}</code>")
        report_lines.append(
            "\nЯкщо дати вище виглядають правильно — підтвердіть командою "
            "/prozorro_confirm_jump, і ми збережемо offset для регулярних прогонів."
        )

    except requests.exceptions.HTTPError as e:
        report_lines.append(f"Статус: ПОМИЛКА HTTP — {e}")
        report_lines.append("Штучний offset не прийнято API. Доведеться йти природним шляхом.")
    except Exception as e:
        report_lines.append(f"Статус: ПОМИЛКА — {str(e)}")

    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(report_lines),
        parse_mode="HTML",
    )


async def confirm_offset_jump(days_ago=14):
    """Зберігає штучний offset як основний — викликати тільки після успішної діагностики."""
    artificial_offset = build_artificial_offset(days_ago)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, storage.set_offset, artificial_offset)
    return artificial_offset
