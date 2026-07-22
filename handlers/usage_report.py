"""
Щоденний звіт адміну про використання бота співробітниками.

Що збирається (storage.record_usage_*):
- команди — TypeHandler-middleware у bot.py (усі /команди від людей);
- NLQ-питання + використані tools — handle_natural_language_query (query_router);
- беки — кнопка «Написати бек» (news_archive) і текстові запити беку через NLQ
  (get_news_leads / get_leads_from_urls); тема — пошуковий запит або питання.

Звіт: щодня о 09:25 за вчора (Київ) у приват Олегу, БЕЗ його власної
активності («крім мене»). Тихий день теж репортиться одним рядком — нуль
користування також інформація. Ручна команда /usage показує будь-який день
і ВКЛЮЧАЄ адміна (зручно перевірити, що облік узагалі пише).
"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import storage
from handlers.notifier import notify_error

KYIV_TZ = ZoneInfo("Europe/Kiev")

_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
_ALLOWED_USER_IDS = {int(uid) for uid in _allowed.split(",") if uid.strip()}
# Перший у списку ALLOWED_USER_IDS — Олег (та сама конвенція, що в notifier)
ADMIN_USER_ID = int(_allowed.split(",")[0]) if _allowed else 56631818

# Скільки питань/беків розгортати в тексті звіту на людину (решта — «і ще N»)
REPORT_QUESTIONS_SHOWN = 10
REPORT_TOPIC_CLIP = 90


def display_name(user):
    """Людське ім'я користувача Telegram для звіту: «Ім'я Прізвище (@нік)»."""
    if user is None:
        return "невідомо хто"
    full = (user.full_name or "").strip()
    if user.username:
        return f"{full} (@{user.username})" if full else f"@{user.username}"
    return full or str(user.id)


def _clip(text, limit=REPORT_TOPIC_CLIP):
    text = " ".join((text or "").split())
    return text[:limit] + "…" if len(text) > limit else text


def _fmt_counter(counter):
    """{'stat': 2, 'weekly': 1} → '/stat ×2, /weekly' (за спаданням)."""
    parts = []
    for name, cnt in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{name} ×{cnt}" if cnt > 1 else name)
    return ", ".join(parts)


def _user_activity(rec):
    return (sum(rec.get("commands", {}).values())
            + rec.get("nlq", 0)
            + len(rec.get("backs", [])))


def format_usage_report(day, exclude_user_id=None):
    """Текст звіту за день 'YYYY-MM-DD' або None, якщо активності не було.
    exclude_user_id — кого не показувати (адмін у щоденному авто-звіті)."""
    data = storage.get_usage_day(day)
    if exclude_user_id is not None:
        data = {uid: rec for uid, rec in data.items() if uid != str(exclude_user_id)}
    data = {uid: rec for uid, rec in data.items() if _user_activity(rec)}
    if not data:
        return None

    label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
    lines = [f"🦊 Хто і як смикав Лиса за {label}:"]

    total_cmds = total_nlq = total_backs = 0
    for uid, rec in sorted(data.items(), key=lambda kv: -_user_activity(kv[1])):
        name = rec.get("name") or f"id {uid}"
        marker = " — це ти" if uid == str(ADMIN_USER_ID) else ""
        lines.append("")
        lines.append(f"👤 {name}{marker}")

        commands = rec.get("commands", {})
        if commands:
            total_cmds += sum(commands.values())
            cmd_text = _fmt_counter({f"/{c}": n for c, n in commands.items()})
            lines.append(f"   Команди: {cmd_text}")

        nlq = rec.get("nlq", 0)
        if nlq:
            total_nlq += nlq
            lines.append(f"   Питання до Лиса: {nlq}")
            questions = rec.get("questions", [])
            for q in questions[:REPORT_QUESTIONS_SHOWN]:
                lines.append(f"   • «{_clip(q)}»")
            if len(questions) > REPORT_QUESTIONS_SHOWN:
                lines.append(f"   …і ще {len(questions) - REPORT_QUESTIONS_SHOWN}")

        tools = rec.get("tools", {})
        if tools:
            lines.append(f"   Tools: {_fmt_counter(tools)}")

        backs = rec.get("backs", [])
        if backs:
            total_backs += len(backs)
            lines.append(f"   Беки: {len(backs)}")
            for b in backs[:REPORT_QUESTIONS_SHOWN]:
                items = b.get("items")
                suffix = f" ({items} новин)" if items else ""
                lines.append(f"   📎 «{_clip(b.get('topic'))}»{suffix}")

    people = len(data)
    lines.append("")
    lines.append(
        f"Разом: {people} " + ("людина" if people == 1 else "людей")
        + f", {total_cmds} команд, {total_nlq} питань, {total_backs} беків."
    )
    return "\n".join(lines)


async def send_daily_usage_report(bot):
    """Авто-звіт за вчора (Київ) у приват адміну, без його власної активності.
    Тихий день — короткий рядок, а не мовчання: нуль теж інформація."""
    try:
        day = (datetime.now(KYIV_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
        text = await asyncio.to_thread(format_usage_report, day, ADMIN_USER_ID)
        if not text:
            text = f"🦊 За {label} ботом ніхто, крім тебе, не користувався. Тиша в норі."
        await bot.send_message(chat_id=ADMIN_USER_ID, text=text)
    except Exception as e:
        print(f"usage_report: не вдалось надіслати щоденний звіт — {e}")
        await notify_error(bot, "щоденний звіт користування ботом", e)


async def usage_handler(update, context):
    """/usage [вчора|YYYY-MM-DD] — зріз користування ботом за день (дефолт —
    сьогодні). На відміну від авто-звіту, показує і адміна (позначкою) —
    зручно перевірити, що облік працює."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    day = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    if context.args:
        arg = context.args[0].strip().lower()
        if arg in ("вчора", "yesterday"):
            day = (datetime.now(KYIV_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            try:
                day = datetime.strptime(arg, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await update.message.reply_text(
                    "Використання: /usage [вчора|YYYY-MM-DD] (без аргументів — сьогодні).")
                return
    text = await asyncio.to_thread(format_usage_report, day)
    if not text:
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
        text = f"🦊 За {label} записаної активності немає."
    await update.message.reply_text(text)
