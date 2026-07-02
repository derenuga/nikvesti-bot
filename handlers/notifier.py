"""
Сповіщення про збої scheduled-задач в особистий чат адміну (REVIEW п. б.5).

Раніше всі помилки йшли тільки в print → Railway logs: якщо вночі відвалиться
парсер міськради чи API, ніхто не дізнається, доки не помітять тишу. Тепер
виняток у автозадачі шле коротке повідомлення Олегу в приват.

Два шляхи потрапляння сюди:
1. Слухач EVENT_JOB_ERROR у scheduler.py — ловить усе, що вилетіло з задачі
   назовні (непередбачені винятки).
2. Прямий виклик notify_error у тих модулях, що глушать виняток на верхньому
   рівні власним try/except (check_email, prozorro, morning, traffic_spikes) —
   інакше слухач їх не побачить.

Rate-limit: не частіше ніж раз на COOLDOWN_MINUTES по кожному source, щоб
масове падіння (наприклад, ліг весь інтернет) не перетворилось на шторм алертів.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KYIV_TZ = ZoneInfo("Europe/Kiev")

_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
# Перший у списку ALLOWED_USER_IDS — Олег (той самий чат, що для outage-дебагу)
ADMIN_CHAT_ID = int(_allowed.split(",")[0]) if _allowed else 56631818

COOLDOWN_MINUTES = 30
_last_sent = {}  # source -> datetime останнього алерту


async def notify_error(bot, source, exc):
    """Шле алерт про збій. Тихо ковтає власні помилки — сповіщення не має
    ламати задачу, яка й так уже впала."""
    try:
        now = datetime.now()
        last = _last_sent.get(source)
        if last and (now - last) < timedelta(minutes=COOLDOWN_MINUTES):
            return
        _last_sent[source] = now
        ts = datetime.now(KYIV_TZ).strftime("%H:%M")
        text = (
            f"⚠️ Збій автозадачі: {source}\n"
            f"{type(exc).__name__}: {str(exc)[:300]}\n"
            f"({ts}, наступний алерт по цьому джерелу — не раніше ніж за {COOLDOWN_MINUTES} хв)"
        )
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print(f"notifier: не вдалось надіслати алерт про '{source}' — {e}")
