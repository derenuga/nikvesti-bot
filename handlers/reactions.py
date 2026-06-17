"""
Обробка реакцій на повідомлення про тендери в тендерній групі.

Логіка: коли хтось ставить будь-яку реакцію на повідомлення бота про тендер —
це означає "беру в роботу". Фіксується тільки ПЕРША реакція (хто встиг раніше
за всіх), усі наступні реакції на те саме повідомлення ігноруються.
Скасування реакції не видаляє запис.

ВАЖЛИВО: storage.mark_tender_taken (яка остаточно "закриває" тендер від
повторних реакцій) викликається ТІЛЬКИ ПІСЛЯ успішного запису в Google Sheets.
Якщо запис у Sheets впаде (наприклад тимчасова помилка API чи прав доступу),
тендер залишається "відкритим" для нової спроби при наступній реакції,
замість того щоб мовчки застрягнути в стані "взято, але без запису".
"""

from datetime import datetime

from handlers import storage
from handlers.sheets import append_pickup_row


async def handle_message_reaction(update, context):
    reaction = update.message_reaction
    if reaction is None:
        print("Реакція: update без message_reaction, ігноруємо")
        return

    print(f"Реакція: отримано update на message_id={reaction.message_id} в chat_id={reaction.chat.id}")

    # Реагуємо тільки на додавання нової реакції (new_reaction непорожній)
    if not reaction.new_reaction:
        print("Реакція: new_reaction порожній (це скасування реакції) — ігноруємо")
        return

    print(f"Реакція: нова реакція виявлена, типи={[r.type for r in reaction.new_reaction]}")

    message_id = reaction.message_id
    tender = storage.get_tender_by_message_id(message_id)
    if not tender:
        print(f"Реакція: message_id={message_id} не знайдено в storage (це не повідомлення про тендер) — ігноруємо")
        return

    tender_id = tender["tender_id"]
    print(f"Реакція: знайдено тендер {tender_id} для message_id={message_id}")

    if storage.is_tender_taken(tender_id):
        print(f"Реакція: тендер {tender_id} вже позначений взятим — ігноруємо повторну реакцію")
        return

    user = reaction.user
    if user:
        taken_by = user.full_name or (f"@{user.username}" if user.username else str(user.id))
    else:
        taken_by = "Невідомо (анонімна реакція)"

    print(f"Реакція: користувач, що поставив реакцію — {taken_by}")

    taken_at = datetime.now()

    print(f"Реакція: йду записувати рядок в Google Sheets для тендера {tender_id}")
    try:
        append_pickup_row(
            date_str=taken_at.strftime("%d.%m.%Y %H:%M"),
            taken_by=taken_by,
            buyer=tender.get("buyer", "н/д"),
            amount=tender.get("amount", "н/д"),
            tender_id=tender_id,
        )
        print(f"Реакція: запис в Google Sheets для тендера {tender_id} успішний")
    except Exception as e:
        print(f"Реакція: ПОМИЛКА запису в Google Sheets для тендера {tender_id}: {type(e).__name__}: {e}")
        # НЕ позначаємо tender як "взятий" — наступна реакція спробує ще раз
        return

    # Позначаємо як взяте лише ПІСЛЯ успішного запису в Sheets
    storage.mark_tender_taken(
        tender_id=tender_id,
        taken_by=taken_by,
        taken_at=taken_at.isoformat(),
    )
    print(f"Реакція: тендер {tender_id} позначено взятим користувачем {taken_by}")
