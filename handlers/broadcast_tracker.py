"""Реєстр message_id повідомлень, які бот надіслав як автоматичну розсилку
(ранкове привітання, привітання з ДН, нагадування про пошту, нагадування
про мовчання каналу) — а не як відповідь на конкретне запитання.

Reply на такий пост у чаті редакції — коментар до контенту розсилки,
а не нове питання до Intent Router (query_router.py: там немає tool,
який стосувався б цих повідомлень). group_reply_to_bot в bot.py звіряється
з цим реєстром і мовчки ігнорує такі reply, замість того щоб Claude
намагався вигадати відповідь через GA4/Search Console tools.

Звіти зі статистикою (send_daily_report, тижневі IG/FB звіти, EN-звіт)
сюди свідомо НЕ потрапляють — reply на них цілком може бути реальним
аналітичним питанням ("чому впали перегляди?") і має йти в Intent Router.

In-memory, без персистентності — після рестарту бот "забуває" старі
розсилки, і reply на них знову підуть в Intent Router (прийнятна
деградація: рестарти рідкісні, а Intent Router просто спробує
відповісти, як і раніше)."""

from collections import deque

_MAX_TRACKED = 500
_broadcast_ids = deque(maxlen=_MAX_TRACKED)


def mark_broadcast(message):
    """Зберегти (chat_id, message_id) надісланого розсилкового повідомлення.
    Приймає об'єкт Message (те, що повертає bot.send_message/reply_text) або None."""
    if message is None:
        return
    _broadcast_ids.append((message.chat_id, message.message_id))


def is_broadcast(chat_id, message_id):
    return (chat_id, message_id) in _broadcast_ids
