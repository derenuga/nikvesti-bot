"""
Обробка реакцій на повідомлення про тендери в тендерній групі.

Логіка: коли хтось ставить будь-яку реакцію на повідомлення бота про тендер —
це означає "беру в роботу". Фіксується тільки ПЕРША реакція (хто встиг раніше
за всіх), усі наступні реакції на те саме повідомлення ігноруються.
Скасування реакції не видаляє запис.
"""

from datetime import datetime

from handlers import storage
from handlers.sheets import append_pickup_row

PROZORRO_CHAT_ID_STR = None  # порівнюємо chat.id напряму, без жорсткої прив'язки рядка


async def handle_message_reaction(update, context):
    reaction = update.message_reaction
    if reaction is None:
        return

    # Реагуємо тільки на додавання нової реакції (new_reaction непорожній)
    if not reaction.new_reaction:
        return

    message_id = reaction.message_id
    tender = storage.get_tender_by_message_id(message_id)
    if not tender:
        return

    tender_id = tender["tender_id"]

    if storage.is_tender_taken(tender_id):
        return

    user = reaction.user
    if user:
        taken_by = user.full_name or (f"@{user.username}" if user.username else str(user.id))
    else:
        taken_by = "Невідомо (анонімна реакція)"

    taken_at = datetime.now()

    marked = storage.mark_tender_taken(
        tender_id=tender_id,
        taken_by=taken_by,
        taken_at=taken_at.isoformat(),
    )

    if not marked:
        # Хтось встиг раніше між перевіркою is_tender_taken і цим моментом
        return

    try:
        append_pickup_row(
            date_str=taken_at.strftime("%d.%m.%Y %H:%M"),
            taken_by=taken_by,
            buyer=tender.get("buyer", "н/д"),
            amount=tender.get("amount", "н/д"),
            tender_id=tender_id,
        )
    except Exception as e:
        print("Помилка запису в Google Sheets: " + str(e))
