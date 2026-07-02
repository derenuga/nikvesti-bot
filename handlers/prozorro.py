"""
Моніторинг тендерів Прозорро.

Логіка:
1. Раз на годину запитуємо /api/2.5/tenders?offset=... — це повертає легкий
   список {id, dateModified} тендерів, що з'явились/змінились з минулого разу,
   і новий offset для наступного запиту (інкрементальне опитування).
   Стрічка йде сторінками по MAX_LIMIT записів; ми гортаємо сторінки одна
   за одною, поки не "наздоганяємо" поточний момент (порожня відповідь) або
   поки не досягнемо MAX_PAGES_PER_RUN за один запуск.
2. Для кожного нового id робимо окремий запит /api/2.5/tenders/{id},
   щоб отримати повну інформацію (сума, замовник, регіон, назва). Ці
   детальні запити виконуються ПАРАЛЕЛЬНО, обмежено через Semaphore
   (MAX_CONCURRENT_DETAIL_REQUESTS одночасних запитів), а не по черзі —
   це критично для швидкості, бо офіційний фід Прозорро не підтримує
   фільтрацію за регіоном/сумою на рівні запиту (лише повний перелік
   змін за датою), і при загальнодержавному обсязі ~7500+ записів змін
   на добу послідовна обробка по 100-150 за годину НІКОЛИ не наздоганяє
   поточний момент — приріст системи завжди більший за швидкість читання.
3. Фільтруємо: регіон замовника = Миколаївська область, сума >= 1 млн грн.
4. Якщо тендер підходить і ще не був відісланий раніше — формуємо і шлемо
   повідомлення в групу. Усі нові тендери за прогон накопичуються в пам'яті
   і записуються в storage ОДНИМ файловим записом наприкінці прогону.

Дедублікація: список уже відісланих tender_id завантажується ОДНИМ читанням
на старті прогону (storage.get_seen_tender_ids), звірка йде в пам'яті.

ВАЖЛИВО ПРО ШВИДКІСТЬ НАЗДОГАНЯННЯ: після кожного прогону в лог пишеться
фактична дата dateModified останнього обробленого запису. Це дозволяє
бачити РЕАЛЬНУ швидкість прогресу (а не орієнтовну з екстраполяції) і
підкручувати MAX_DETAIL_REQUESTS_PER_RUN/MAX_CONCURRENT_DETAIL_REQUESTS
на основі фактів.

КРИТИЧНО ВАЖЛИВО: bot.py працює на asyncio event loop. Усі мережеві запити
через requests (синхронна бібліотека) і всі файлові операції зі storage
виконуються в окремому потоці через run_in_executor, інакше довгий цикл
повністю блокує бота.
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

MAX_LIMIT = 1000                       # розмір сторінки списку id (максимум, що підтримує API)
MAX_PAGES_PER_RUN = 5                  # скільки сторінок списку обробляємо за один прогон
MAX_DETAIL_REQUESTS_PER_RUN = 1500     # скільки детальних запитів робимо за один прогон (головний обсяг)
MAX_CONCURRENT_DETAIL_REQUESTS = 12    # скільки детальних запитів виконуємо ОДНОЧАСНО (паралельно)
PAGE_DELAY_SECONDS = 0.5               # пауза між сторінками списку

RUN_TIMEOUT_SECONDS = 600              # запобіжник від зависання: максимум 10 хв на весь прогон

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

    # \u00A0 — нерозривний пробіл, щоб сума не переносилась на новий рядок
    amount_text = f"{amount:,.0f}".replace(",", "\u00A0") if amount is not None else "н/д"
    url = f"https://prozorro.gov.ua/tender/{tender_id}"

    text = (
        f"💰 {amount_text} {currency}\n"
        f"🏛 <b>{_escape_html(buyer)}</b>\n"
        f'📋 <a href="{url}">{tender_id}</a>\n\n'
        f"<blockquote>{_escape_html(title)}</blockquote>"
    )
    return text, tender_id, title, amount, buyer


from handlers.helpers import escape_html as _escape_html


# ---------- Основна логіка ----------

async def _fetch_and_filter_tender(tender_id, seen_ids, semaphore):
    """
    Завантажує деталі тендера (з обмеженням паралельності через semaphore)
    і повертає tender dict, якщо він новий і підходить під критерії,
    інакше None. НЕ виконує побічних дій (відправку повідомлень, запис
    у storage) — це робиться окремо, послідовно, після збору всіх
    результатів, щоб уникнути конкурентних записів у Telegram/storage.
    """
    if tender_id in seen_ids:
        return None

    async with semaphore:
        tender = await _fetch_tender_details(tender_id)

    if not tender:
        return None

    if not _matches_criteria(tender):
        return None

    return tender


async def check_prozorro_tenders(bot):
    if not PROZORRO_CHAT_ID:
        print("Помилка Прозорро: PROZORRO_CHAT_ID не задано")
        return

    try:
        await asyncio.wait_for(_run_check_cycle(bot), timeout=RUN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        print(f"Прозорро: прогон перевищив {RUN_TIMEOUT_SECONDS} секунд, перервано (запобіжник від зависання)")
    except Exception as e:
        print("Помилка перевірки Прозорро: " + str(e))
        from handlers.notifier import notify_error
        await notify_error(bot, "тендери Prozorro", e)


async def _run_check_cycle(bot):
    loop = asyncio.get_event_loop()
    start_time = time.time()

    # Одне читання стану на старті прогону
    offset = await loop.run_in_executor(None, storage.get_offset)
    seen_ids = await loop.run_in_executor(None, storage.get_seen_tender_ids)
    newly_sent = []

    total_checked = 0
    total_sent = 0
    pages_fetched = 0
    final_offset = offset
    last_date_modified = None

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DETAIL_REQUESTS)

    for page_num in range(MAX_PAGES_PER_RUN):
        if total_checked >= MAX_DETAIL_REQUESTS_PER_RUN:
            break

        tender_ids, next_offset, has_data = await _fetch_tender_page(final_offset)
        pages_fetched += 1

        if not tender_ids:
            if not has_data:
                break
            continue

        # Обрізаємо список, якщо ця сторінка вивела б нас за загальний ліміт
        remaining_budget = MAX_DETAIL_REQUESTS_PER_RUN - total_checked
        ids_to_process = tender_ids[:remaining_budget]
        total_checked += len(ids_to_process)

        # Паралельна перевірка деталей усіх id на цій сторінці одночасно
        # (обмежено semaphore, щоб не перевантажити API)
        results = await asyncio.gather(
            *[_fetch_and_filter_tender(tid, seen_ids, semaphore) for tid in ids_to_process],
            return_exceptions=True,
        )

        # Послідовна обробка результатів: відправка повідомлень і оновлення
        # seen_ids/newly_sent робиться по черзі, щоб уникнути дублікатів
        # повідомлень при паралельному виконанні.
        for tender, tender_id in zip(results, ids_to_process):
            if isinstance(tender, Exception) or tender is None:
                continue

            real_tender_id = tender.get("tenderID") or tender.get("id")
            if real_tender_id in seen_ids:
                continue  # про всяк випадок, якщо id повторився на двох сторінках

            text, _, title, amount, buyer = _format_message(tender)

            try:
                message = await bot.send_message(
                    chat_id=PROZORRO_CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print(f"Прозорро: не вдалось надіслати повідомлення про {real_tender_id}: {e}")
                continue

            seen_ids.add(real_tender_id)
            newly_sent.append({
                "tender_id": real_tender_id,
                "message_id": message.message_id,
                "title": title,
                "amount": amount,
                "buyer": buyer,
                "sent_at": datetime.now().isoformat(),
            })
            total_sent += 1

        # Фіксуємо dateModified останнього елемента сторінки для діагностики
        for tender in reversed(results):
            if not isinstance(tender, Exception) and tender is not None:
                last_date_modified = tender.get("dateModified")
                break

        if next_offset:
            final_offset = next_offset

        if not has_data or total_checked >= MAX_DETAIL_REQUESTS_PER_RUN:
            break

        await asyncio.sleep(PAGE_DELAY_SECONDS)

    # Один фінальний запис на диск за весь прогон
    await loop.run_in_executor(None, storage.bulk_save, newly_sent, final_offset)

    elapsed = time.time() - start_time
    progress_note = f", остання обр. dateModified≈{last_date_modified}" if last_date_modified else ""
    print(
        f"Прозорро: опрацьовано сторінок={pages_fetched}, "
        f"перевірено тендерів={total_checked}, відіслано={total_sent}, "
        f"час={elapsed:.1f}с{progress_note}"
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
