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

from handlers import ai_usage, storage
from handlers.notifier import notify_error

KYIV_TZ = ZoneInfo("Europe/Kiev")

_allowed = os.environ.get("ALLOWED_USER_IDS", "").strip()
_ALLOWED_USER_IDS = {int(uid) for uid in _allowed.split(",") if uid.strip()}
# Перший у списку ALLOWED_USER_IDS — Олег (та сама конвенція, що в notifier)
ADMIN_USER_ID = int(_allowed.split(",")[0]) if _allowed else 56631818

# Скільки питань/беків розгортати в тексті звіту на людину (решта — «і ще N»)
REPORT_QUESTIONS_SHOWN = 10
REPORT_TOPIC_CLIP = 90
# У зведенні по ОДНІЙ людині (/usage <імʼя>) обрізка ширша: мета режиму —
# роздивитись, ЩО саме людина пише боту, а 90 символів з'їдає один URL
PERSON_TOPIC_CLIP = 170
# Ліміт повідомлення Telegram 4096; ріжемо з запасом на службові рядки
MESSAGE_CHUNK_LIMIT = 3800


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


def _fmt_clipped(text, full_len, limit=REPORT_TOPIC_CLIP):
    """Обрізаний текст + реальна довжина оригіналу, коли її видно не стало.
    full_len пишеться в storage з 30.07; старі записи без нього — просто текст."""
    clipped = _clip(text, limit)
    if full_len and full_len > limit:
        return f"«{clipped}» ({full_len} симв.)"
    return f"«{clipped}»"


def _fmt_counter(counter):
    """{'stat': 2, 'weekly': 1} → '/stat ×2, /weekly' (за спаданням)."""
    parts = []
    for name, cnt in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{name} ×{cnt}" if cnt > 1 else name)
    return ", ".join(parts)


def _user_activity(rec):
    # "ai" рахується теж: якщо від дня лишився тільки AI-слід (бек згенерувався,
    # а запис беку впав) — людина з витратами не має зникати зі звіту
    return (sum(rec.get("commands", {}).values())
            + rec.get("nlq", 0)
            + len(rec.get("backs", []))
            + (1 if rec.get("ai") else 0))


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
    total_ai_cost = 0.0
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
                if isinstance(q, dict):
                    lines.append(f"   • {_fmt_clipped(q.get('q'), q.get('len'))}")
                else:
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
                topic = _fmt_clipped(b.get("topic"), b.get("len"))
                lines.append(f"   📎 {topic}{suffix}")

        # Скільки коштували AI-виклики людини за день (пише record_ai_usage
        # з user_id; старі дні без ключа "ai" — просто без рядка)
        ai_models = rec.get("ai")
        if ai_models:
            cost = ai_usage.models_cost(ai_models, day)
            total_ai_cost += cost
            tokens = ai_usage.models_tokens(ai_models)
            tokens_text = f"{tokens // 1000} тис." if tokens >= 1000 else str(tokens)
            lines.append(f"   💰 AI: {ai_usage.fmt_cost(cost)} ({tokens_text} токенів)")

    people = len(data)
    lines.append("")
    ai_total_text = f", AI {ai_usage.fmt_cost(total_ai_cost)}" if total_ai_cost > 0 else ""
    lines.append(
        f"Разом: {people} " + ("людина" if people == 1 else "людей")
        + f", {total_cmds} команд, {total_nlq} питань, {total_backs} беків{ai_total_text}."
    )
    return "\n".join(lines)


def _person_matches(rec_name, needle):
    """Чи підходить запис під запит «кристина» / «леонова» / «@skxxlw».
    Ім'я в записі — «Ім'я Прізвище (@нік)», тому вистачає contains без регістру."""
    return needle.lower().lstrip("@") in (rec_name or "").lower()


def format_person_report(person_query, days=30):
    """Зведення запитів ОДНІЄЇ людини за останні N днів — «ресьорч патернів»:
    усі збережені питання по днях (storage тримає перші 200 симв. кожного),
    беки, tools, команди і вартість AI. Повертає готовий текст або None,
    якщо людини в історії немає. Кілька збігів імені — текст із проханням
    уточнити (щоб «юлія» мовчки не склеїла Бойченко з Лук'яненко)."""
    all_days = storage.get_usage_all()
    since = (datetime.now(KYIV_TZ) - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    matched = {}   # day -> rec (лише дні з активністю цієї людини)
    names_seen = {}  # найсвіжіше імʼя за кожним uid — ловимо збіг кількох людей
    for day in sorted(all_days.keys(), reverse=True):
        if day < since:
            continue
        for uid, rec in all_days[day].items():
            if not _person_matches(rec.get("name"), person_query) or not _user_activity(rec):
                continue
            names_seen.setdefault(uid, rec.get("name") or f"id {uid}")
            matched[day] = rec

    if not matched:
        return None
    if len(names_seen) > 1:
        listed = "\n".join(f"  • {name}" for name in names_seen.values())
        return (f"Під «{person_query}» підпадає кілька людей:\n{listed}\n"
                f"Уточни — прізвищем або @ніком.")

    name = next(iter(names_seen.values()))
    lines = [f"🦊 Запити: {name} — останні {days} днів", ""]

    total_nlq = total_backs = 0
    total_cmds, total_tools = {}, {}
    total_ai = {}
    total_ai_cost = 0.0
    for day in sorted(matched.keys(), reverse=True):
        rec = matched[day]
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
        parts = []
        nlq = rec.get("nlq", 0)
        if nlq:
            parts.append(f"{nlq} пит.")
        backs = rec.get("backs", [])
        if backs:
            parts.append(f"{len(backs)} бек.")
        commands = rec.get("commands", {})
        if commands:
            parts.append(f"{sum(commands.values())} команд")
        ai_models = rec.get("ai")
        if ai_models:
            # Ціна рахується ЗА ДЕНЬ, а не за зведеним набором: вступний прайс
            # має дату кінця, тож вікно у 30 днів може лежати по обидва її боки.
            day_cost = ai_usage.models_cost(ai_models, day)
            total_ai_cost += day_cost
            parts.append(f"AI {ai_usage.fmt_cost(day_cost)}")
        lines.append(f"📅 {label} — " + ", ".join(parts))

        for q in rec.get("questions", []):
            if isinstance(q, dict):
                lines.append(f"   • {_fmt_clipped(q.get('q'), q.get('len'), PERSON_TOPIC_CLIP)}")
            else:
                lines.append(f"   • «{_clip(q, PERSON_TOPIC_CLIP)}»")
        for b in backs:
            items = b.get("items")
            suffix = f" ({items} новин)" if items else ""
            lines.append(f"   📎 бек: {_fmt_clipped(b.get('topic'), b.get('len'), PERSON_TOPIC_CLIP)}{suffix}")
        if commands:
            lines.append(f"   Команди: {_fmt_counter({f'/{c}': n for c, n in commands.items()})}")
        tools = rec.get("tools", {})
        if tools:
            lines.append(f"   Tools: {_fmt_counter(tools)}")
        lines.append("")

        total_nlq += nlq
        total_backs += len(backs)
        for c, n in commands.items():
            total_cmds[c] = total_cmds.get(c, 0) + n
        for t, n in tools.items():
            total_tools[t] = total_tools.get(t, 0) + n
        for model, mrec in (ai_models or {}).items():
            acc = total_ai.setdefault(model, {})
            for k, v in mrec.items():
                acc[k] = acc.get(k, 0) + v

    summary = [f"Разом за {len(matched)} акт. днів: {total_nlq} питань, {total_backs} беків"]
    if total_cmds:
        summary.append(f"{sum(total_cmds.values())} команд")
    if total_ai:
        cost = total_ai_cost          # сума денних, а не перерахунок зведеного
        tokens = ai_usage.models_tokens(total_ai)
        tokens_text = f"{tokens // 1000} тис." if tokens >= 1000 else str(tokens)
        summary.append(f"AI {ai_usage.fmt_cost(cost)} ({tokens_text} токенів)")
    lines.append(", ".join(summary) + ".")
    if total_tools:
        lines.append(f"Найчастіші tools: {_fmt_counter(total_tools)}")
    lines.append("(питання збережені першими 200 симв.; історія живе 30 днів, "
                 "вартість AI по людях пишеться з 14.08.2026)")
    return "\n".join(lines)


def _chunk_text(text, limit=MESSAGE_CHUNK_LIMIT):
    """Порізати довгий звіт на повідомлення по межах рядків (ліміт Telegram
    4096; зведення по людині за місяць в одне повідомлення не влазить)."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


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
    зручно перевірити, що облік працює.

    /usage <імʼя|прізвище|@нік> [днів] — зведення запитів ОДНІЄЇ людини за
    останні N днів (дефолт 30): усі питання по днях, беки, tools, вартість AI.
    «Ресьорч патернів» — що і як людина пише боту."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    args = [a.strip() for a in (context.args or []) if a.strip()]

    day = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
    person_query = None
    days = 30
    if args:
        first = args[0].lower()
        if first in ("вчора", "yesterday"):
            day = (datetime.now(KYIV_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            try:
                day = datetime.strptime(args[0], "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                # Не дата — режим людини: /usage кристина [днів], /usage @skxxlw
                query_parts = list(args)
                if len(query_parts) > 1 and query_parts[-1].isdigit():
                    days = max(1, min(30, int(query_parts.pop())))
                person_query = " ".join(query_parts)
                if not person_query or person_query.isdigit():
                    await update.message.reply_text(
                        "Використання: /usage [вчора|YYYY-MM-DD] — день, або "
                        "/usage <імʼя|@нік> [днів] — зведення по людині.")
                    return

    if person_query:
        text = await asyncio.to_thread(format_person_report, person_query, days)
        if not text:
            await update.message.reply_text(
                f"🦊 «{person_query}» серед тих, хто користувався ботом за "
                f"останні {days} днів, не знайшов. Історія живе 30 днів.")
            return
        for chunk in _chunk_text(text):
            await update.message.reply_text(chunk)
        return

    text = await asyncio.to_thread(format_usage_report, day)
    if not text:
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
        text = f"🦊 За {label} записаної активності немає."
    await update.message.reply_text(text)
