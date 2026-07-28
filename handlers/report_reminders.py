"""
Нагадування про звітні дедлайни грантів — у чат «Фінанси МикВісті».

Кілька звітів одного проєкту з однією датою йдуть ОДНИМ повідомленням
(Олег: «часто наративку і фінансовий разом») — див. group_due.

Запит Олега 27.07.2026: дедлайни звітності вже заводяться в Mini App
«Команда» (team_project_deadlines), але досі лише показувались. Тепер Лис
нагадує сам — двічі: за тиждень (щоб сісти писати) і за дві доби (щоб
дотиснути).

Що НЕ нагадуємо: звіти, вже позначені як подані або прийняті — рух звіту
менеджери відмічають в апці, і смикати їх після цього немає сенсу.

Ідемпотентність: кожне нагадування фіксується в team_deadline_reminders
(deadline_id × поріг). Тому перезапуск процесу, повторний прогін чи ручний
виклик /reports не задублюють повідомлення. Позначка ставиться ЛИШЕ після
успішної відправки — якщо бот ще не має прав у чаті, наступного дня спроба
повториться.

Чат задається env FINANCE_CHAT_ID — без неї модуль тихо спить. Приймає і
«сирий» id групи без префікса (4738653227), і канонічний (-1004738653227) —
див. _normalize_chat_id.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import bot_db, team_projects, team_tasks
from handlers.helpers import normalize_https_url

KYIV_TZ = ZoneInfo("Europe/Kiev")

# Пороги нагадувань (Олег: спочатку за тиждень, потім за 2 доби). Це саме
# ПОРОГИ, а не точні дні: спрацьовує перший непройдений поріг, під який
# підпадає залишок часу. Інакше нагадування «за 7 днів» мало б рівно одну
# спробу — і якщо того дня бот лежав, не мав прав у чаті або дедлайн завели
# пізніше, ніж за тиждень, воно губилось би назавжди.
REMIND_DAYS = (7, 2)

_RAW_FINANCE_CHAT_ID = os.environ.get("FINANCE_CHAT_ID")

# Прямий лінк апки з BotFather. У ГРУПІ Telegram не дозволяє web_app-кнопки —
# лише URL, тож ведемо саме прямим лінком; startapp=reports відкриває апку
# одразу на «Звітності», а не на Головній.
WEBAPP_DIRECT_LINK = normalize_https_url(os.environ.get("WEBAPP_DIRECT_LINK"))

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

# Порядок звітів усередині одного повідомлення: спершу наративка (її пишуть
# довше), потім фінансовий, потім майлстоуни.
_KIND_ORDER = {"narrative": 0, "financial": 1, "milestone": 2}


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

    try:
        drive = team_tasks.get_drive_links()
    except Exception as e:
        print(f"report_reminders: папки Drive не прочитались — {e}")
        drive = {}

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
        if days_left < 0:
            continue  # дедлайн минув — нагадувати вже пізно, це не спам-бот
        # Найтісніший поріг, під який підпадає залишок: 5 днів → поріг 7,
        # 1 день → поріг 2. Так пропущений день не губить нагадування, і
        # водночас за один дедлайн не летить два повідомлення поспіль.
        fitting = [t for t in REMIND_DAYS if days_left <= t]
        if not fitting:
            continue
        threshold = min(fitting)
        if _already_sent(dl["id"], threshold):
            continue
        # Копія, а не сам обʼєкт зі списку: list_projects віддає кешовані
        # словники, і дописування в них отруїло б кеш на 10 хв
        project = dict(projects.get(dl["project_id"]) or {})
        project["drive_url"] = drive.get(dl["project_id"])
        out.append((dl, project, threshold, days_left))
    return out


def _when_human(days_left):
    if days_left == 0:
        return "сьогодні"
    if days_left == 1:
        return "завтра"
    if days_left < 5:
        return f"через {days_left} доби"
    return f"через {days_left} днів"


def group_due(items):
    """Звіти одного проєкту з ОДНІЄЮ датою — в одну групу (Олег: «часто
    наративку і фінансовий разом»). Інакше в чат летіли два майже однакові
    повідомлення поспіль, які око зчитує як дубль.

    Групуємо саме за парою «проєкт + дата»: різні дати — це різні події,
    і зливати їх в одне повідомлення означало б ховати одну з них."""
    groups = {}
    for dl, project, threshold, days_left in items:
        key = (dl["project_id"], dl["due"], threshold)
        group = groups.setdefault(key, {
            "project": project, "threshold": threshold,
            "days_left": days_left, "deadlines": [],
        })
        group["deadlines"].append(dl)
    for group in groups.values():
        group["deadlines"].sort(key=lambda d: (_KIND_ORDER.get(d["kind"], 9), d["id"]))
    return list(groups.values())


def _project_line(project):
    donor = (project or {}).get("partner") or (project or {}).get("name") or "проєкт"
    name = (project or {}).get("name")
    return donor + (f" · {name}" if name and name != donor else "")


def format_group(group):
    """Повідомлення на групу. Один звіт — компактний вигляд «що і кому»;
    кілька — шапка проєкту з датою і список звітів під нею."""
    project, dls = group["project"], group["deadlines"]
    days_left = group["days_left"]
    if len(dls) == 1:
        return format_reminder(dls[0], project, days_left)

    lines = [
        f"🦊 <b>{_project_line(project)}</b> — {_when_human(days_left)}",
        f"Дедлайн: <b>{_due_human(dls[0]['due'])}</b>",
        "",
    ]
    for dl in dls:
        lines.append(f"• {deadline_title(dl)} — {_mention(dl.get('assignee'))}")
    if (project or {}).get("drive_url"):
        lines.append("")
        lines.append(f"<a href=\"{project['drive_url']}\">Папка проєкту на Drive</a>")
    return "\n".join(lines)


def format_reminder(dl, project, days_left):
    donor = (project or {}).get("partner") or (project or {}).get("name") or "проєкт"
    name = (project or {}).get("name")
    when = _when_human(days_left)
    lines = [
        f"🦊 <b>{deadline_title(dl)}</b> — {when}",
        f"{donor}" + (f" · {name}" if name and name != donor else ""),
        f"Дедлайн: <b>{_due_human(dl['due'])}</b>",
        f"Пише: {_mention(dl.get('assignee'))}",
    ]
    # Папка проєкту на Drive — щоб не шукати документи гранту по чатах
    if (project or {}).get("drive_url"):
        lines.append(f"<a href=\"{project['drive_url']}\">Папка проєкту на Drive</a>")
    return "\n".join(lines)


def _app_markup():
    """Кнопка «Дедлайни в апці». Без WEBAPP_DIRECT_LINK кнопки просто не буде —
    повідомлення лишиться корисним і без неї."""
    if not WEBAPP_DIRECT_LINK:
        return None
    sep = "&" if "?" in WEBAPP_DIRECT_LINK else "?"
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📅 Дедлайни в апці", url=f"{WEBAPP_DIRECT_LINK}{sep}startapp=reports")]])


def _notify_app(dl, project, threshold, days_left):
    """Дублює нагадування у стрічку сповіщень апки (керівництву). Дедуп по
    парі «дедлайн × поріг» — тим самим ключем, що й позначка надсилання."""
    from handlers import team_notifications

    donor = (project or {}).get("partner") or (project or {}).get("name") or "проєкт"
    team_notifications.notify_safe(
        "report_deadline", deadline_title(dl), audience="managers",
        body=f"{donor} · {_when_human(days_left)} · пише "
             f"{dl.get('assignee') or 'ніхто (не призначено)'}",
        object_type="deadline", object_id=dl["id"],
        dedup_key=f"report_deadline:{dl['id']}:{threshold}")


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
    for group in group_due(due):
        project, threshold = group["project"], group["threshold"]
        days_left, dls = group["days_left"], group["deadlines"]
        try:
            await bot.send_message(
                chat_id=target,
                text=format_group(group),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_app_markup(),
            )
            if not force:
                # Позначку і подію в апці ставимо на КОЖЕН звіт групи: у чат
                # пішло одне повідомлення, але закрито ним кілька дедлайнів.
                for dl in dls:
                    await asyncio.to_thread(_mark_sent, dl["id"], threshold)
                    # Те саме нагадування — у стрічку «Сповіщень» апки: у чаті
                    # фінансів воно тоне, а тут лежить, доки не прочитають.
                    await asyncio.to_thread(_notify_app, dl, project, threshold, days_left)
            sent += 1
        except Exception as e:
            ids = ", ".join(str(d["id"]) for d in dls)
            print(f"report_reminders: нагадування по дедлайнах {ids} не пішло — {e}")
    if sent:
        print(f"report_reminders: надіслано {sent} нагадувань у {target}")
    return sent


def diagnose(today=None):
    """Чому нагадувань немає: розкладка всіх дедлайнів по причинах. Потрібна
    саме тому, що «нічого не прийшло» має щонайменше пʼять різних причин, і
    без цифр вони не розрізняються."""
    today = today or datetime.now(KYIV_TZ).date()
    out = {"total": 0, "closed": 0, "past": 0, "far": 0,
           "already": 0, "ready": 0, "error": None}
    try:
        deadlines = team_tasks.list_project_deadlines()
    except Exception as e:
        out["error"] = str(e)
        return out
    out["total"] = len(deadlines)
    for dl in deadlines:
        if dl.get("status") in ("submitted", "accepted"):
            out["closed"] += 1
            continue
        try:
            due = datetime.strptime(dl["due"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_left = (due - today).days
        if days_left < 0:
            out["past"] += 1
            continue
        fitting = [t for t in REMIND_DAYS if days_left <= t]
        if not fitting:
            out["far"] += 1
            continue
        if _already_sent(dl["id"], min(fitting)):
            out["already"] += 1
        else:
            out["ready"] += 1
    return out


# ---------- Команда ----------

async def reports_check_handler(update, context):
    """/reports — прогнати перевірку зараз І показати, чому нагадувань немає.
    Прев'ю летить у той чат, де викликали команду, і НЕ позначається як
    надіслане, щоб не з'їсти планове."""
    import asyncio

    d = await asyncio.to_thread(diagnose)

    lines = ["🦊 <b>Звітні дедлайни — діагностика</b>"]
    if is_configured():
        lines.append(f"Чат нагадувань: <code>{FINANCE_CHAT_ID}</code>")
        # Найважливіша перевірка: чи бот справді бачить той чат. Помилковий id
        # і відсутність прав виглядають однаково — «нічого не прийшло».
        try:
            chat = await context.bot.get_chat(FINANCE_CHAT_ID)
            lines.append(f"Доступ до чату: ✅ «{chat.title or chat.id}»")
        except Exception as e:
            lines.append(f"Доступ до чату: ❌ {e}")
    else:
        lines.append("Чат нагадувань: ❌ <b>змінна FINANCE_CHAT_ID не задана</b> "
                     "(Railway → Variables)")

    if d["error"]:
        lines.append(f"Дедлайни не прочитались: {d['error']}")
    else:
        lines.append(
            f"\nДедлайнів усього: <b>{d['total']}</b>\n"
            f"• подані/прийняті (не нагадуємо): {d['closed']}\n"
            f"• дата вже минула: {d['past']}\n"
            f"• ще далеко (понад 7 діб): {d['far']}\n"
            f"• у вікні, але вже нагадано: {d['already']}\n"
            f"• <b>чекають нагадування: {d['ready']}</b>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    sent = await check_report_deadlines(
        context.bot, chat_id=update.effective_chat.id, force=True
    )
    if not sent and not d["error"]:
        await update.message.reply_text(
            "Прев'ю порожнє — сьогодні нагадувати нема про що.",
        )
