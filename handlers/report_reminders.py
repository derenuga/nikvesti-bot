"""
Нагадування про звітні дедлайни грантів — у чат «Фінанси МикВісті».

Запит Олега 27.07.2026: дедлайни звітності вже заводяться в Mini App
«Команда» (team_project_deadlines), але досі лише показувались. Тепер Лис
нагадує сам — двічі: за тиждень (щоб сісти писати) і за дві доби (щоб
дотиснути).

Що НЕ нагадуємо: звіти, вже позначені як подані або прийняті — рух звіту
менеджери відмічають в апці, і смикати їх після цього немає сенсу.

Ідемпотентність: кожне нагадування фіксується в team_deadline_reminders
(deadline_id × скільки днів лишалось). Тому перезапуск процесу, повторний
прогін чи ручний виклик /reports не задублюють повідомлення. Якщо бот лежав
і день пропустили — нагадування за цю позначку просто не піде (дата вже
пройшла), але наступна (за 2 доби) спрацює.

Чат задається env FINANCE_CHAT_ID; приймає і «сирий» id групи без префікса
(4738653227), і канонічний (-1004738653227) — див. _normalize_chat_id.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import bot_db, team_projects, team_tasks

KYIV_TZ = ZoneInfo("Europe/Kiev")

# За скільки днів до дедлайну нагадуємо (Олег: спочатку за тиждень, потім за 2 доби)
REMIND_DAYS = (7, 2)

_RAW_FINANCE_CHAT_ID = os.environ.get("FINANCE_CHAT_ID", "4738653227")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_deadline_reminders (
    deadline_id BIGINT NOT NULL,
    days_before SMALLINT NOT NULL,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (deadline_id, days_before)
)
"""

_schema_done = False

KIND_TITLES = {
    "narrative": "Наративний звіт",
    "financial": "Фінансовий звіт",
    "milestone": "Майлстоун",
}
STAGE_TITLES = {"interim": "проміжний", "final": "фінальний"}


def _normalize_chat_id(raw):
    """Telegram id супергрупи — відʼємний, з префіксом -100. Олег дає id у
    «сирому» вигляді (4738653227), як показують деякі клієнти, тож приймаємо
    обидві форми і не змушуємо пам'ятати про префікс."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n < 0 else -1000000000000 - n


FINANCE_CHAT_ID = _normalize_chat_id(_RAW_FINANCE_CHAT_ID)


def is_configured():
    return FINANCE_CHAT_ID is not None


def _ensure_schema():
    global _schema_done
    if _schema_done:
        return
    bot_db.execute(_SCHEMA)
    _schema_done = True


def _already_sent(deadline_id, days_before):
    _ensure_schema()
    return bool(bot_db.query(
        "SELECT 1 FROM team_deadline_reminders WHERE deadline_id = %s AND days_before = %s",
        (int(deadline_id), int(days_before)),
    ))


def _mark_sent(deadline_id, days_before):
    _ensure_schema()
    bot_db.execute(
        "INSERT INTO team_deadline_reminders (deadline_id, days_before) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (int(deadline_id), int(days_before)),
    )


def deadline_title(dl):
    if dl["kind"] == "milestone":
        return dl["title"] or "Майлстоун"
    title = KIND_TITLES.get(dl["kind"], dl["kind"])
    if dl.get("stage"):
        title += f" ({STAGE_TITLES.get(dl['stage'], dl['stage'])})"
    return title


def _mention(person):
    """Хендл людини для згадки в чаті; немає хендла — просто ім'я.
    TEAM імпортуємо ліниво: ai_messages тягне за собою anthropic, а
    нагадуванням AI-шар ні до чого — модуль має працювати й без нього."""
    if not person:
        return "нікому не призначено"
    try:
        from handlers.ai_messages import TEAM
        handle = (TEAM.get(person) or {}).get("tg", "")
    except Exception:
        handle = ""
    return f"{person} ({handle})" if handle.startswith("@") else person


def _due_human(due_iso):
    y, m, d = due_iso.split("-")
    return f"{d}.{m}.{y}"


def collect_due(today=None):
    """Що треба нагадати сьогодні: [(дедлайн, проєкт, за скільки днів)].
    Блокуюча (обидві БД) — кликати через asyncio.to_thread."""
    today = today or datetime.now(KYIV_TZ).date()
    deadlines = team_tasks.list_project_deadlines()
    if not deadlines:
        return []

    try:
        projects = {p["id"]: p for p in team_projects.list_projects(False)}
    except Exception as e:
        print(f"report_reminders: проєкти з БД сайту не прочитались — {e}")
        projects = {}

    out = []
    for dl in deadlines:
        # Подані й прийняті не смикаємо — рух звіту менеджери відмічають в апці
        if dl.get("status") in ("submitted", "accepted"):
            continue
        try:
            due = datetime.strptime(dl["due"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_left = (due - today).days
        if days_left not in REMIND_DAYS:
            continue
        if _already_sent(dl["id"], days_left):
            continue
        out.append((dl, projects.get(dl["project_id"]), days_left))
    return out


def format_reminder(dl, project, days_left):
    donor = (project or {}).get("partner") or (project or {}).get("name") or "проєкт"
    name = (project or {}).get("name")
    when = "за тиждень" if days_left >= 7 else f"через {days_left} доби"
    lines = [
        f"🦊 <b>{deadline_title(dl)}</b> — {when}",
        f"{donor}" + (f" · {name}" if name and name != donor else ""),
        f"Дедлайн: <b>{_due_human(dl['due'])}</b>",
        f"Пише: {_mention(dl.get('assignee'))}",
    ]
    return "\n".join(lines)


async def check_report_deadlines(bot, chat_id=None, force=False):
    """Щоденний прогін: нагадує про звіти за 7 і за 2 доби. Повертає
    кількість надісланих нагадувань."""
    import asyncio

    if not is_configured() and chat_id is None:
        print("report_reminders: FINANCE_CHAT_ID не задано — нагадування вимкнено")
        return 0

    target = chat_id or FINANCE_CHAT_ID
    try:
        due = await asyncio.to_thread(collect_due)
    except Exception as e:
        print(f"report_reminders: не вдалось зібрати дедлайни — {e}")
        return 0

    sent = 0
    for dl, project, days_left in due:
        try:
            await bot.send_message(
                chat_id=target,
                text=format_reminder(dl, project, days_left),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            if not force:
                await asyncio.to_thread(_mark_sent, dl["id"], days_left)
            sent += 1
        except Exception as e:
            print(f"report_reminders: нагадування по дедлайну {dl['id']} не пішло — {e}")
    if sent:
        print(f"report_reminders: надіслано {sent} нагадувань у {target}")
    return sent


# ---------- Команда ----------

async def reports_check_handler(update, context):
    """/reports — прогнати перевірку зараз. Нагадування летять у той чат, де
    викликали команду, і НЕ позначаються як надіслані, щоб не з'їсти планове."""
    await update.message.reply_text("Дивлюсь звітні дедлайни…")
    sent = await check_report_deadlines(
        context.bot, chat_id=update.effective_chat.id, force=True
    )
    if not sent:
        target = FINANCE_CHAT_ID if is_configured() else "не налаштовано"
        await update.message.reply_text(
            "Нічого нагадувати: немає звітів, до яких лишилось рівно 7 або 2 доби "
            "(подані й прийняті не рахуються).\n"
            f"Плановий чат нагадувань: <code>{target}</code>",
            parse_mode="HTML",
        )
