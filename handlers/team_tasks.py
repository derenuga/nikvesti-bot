"""
Таски команди — дата-шар Mini App «Команда» (хвиля 1).

Модель: таск персональний (один виконавець з ростера), належить «проєкту»
(вільний ярлик: «Бюджет-2026», «Афіша», назва спецпроєкту) — рішення Олега
24.07.2026: таски персональні по проєктах, KPI — по відділах (KPI — хвиля 2).

Життєвий цикл: todo → doing → review → done (менеджер приймає) / dropped.
Журналістка рухає СВІЙ таск todo↔doing→review і чіпляє лінк на матеріал;
менеджер (ростер manager=True) — усе: створення, редагування, done/dropped.
Права перевіряє API-шар (handlers/webapp.py), тут — чиста робота з Норою.

Люди зберігаються КАНОНІЧНИМ ІМ'ЯМ ростера (не tg_id): numeric id більшості
команди невідомі до першого входу в апку, а ім'я стабільне. Пінги резолвлять
ім'я → id через team_roster.tg_id_for у момент відправки (best-effort: немає
id або людина не стартувала бота — пінг тихо пропускається, апка лишається
джерелом правди).

Таблиці (схема — тут, НЕ в bot_db._SCHEMA_STATEMENTS: модуль опційний і
самодостатній, як budget_*): team_users (кеш tg_id ↔ людина, пише
team_roster), team_tasks, team_task_events (журнал змін — історія в картці
таска і матеріал для геймифікації хвилі 2).
"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from handlers import bot_db, team_roster

KYIV_TZ = ZoneInfo("Europe/Kiev")

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

STATUSES = ("todo", "doing", "review", "done", "dropped")
# Дозволені ходи виконавиці по ВЛАСНОМУ таску; решта переходів — менеджерські.
EXECUTOR_MOVES = {("todo", "doing"), ("doing", "todo"), ("doing", "review"), ("review", "doing")}

PRIORITY_TITLES = {0: "не горить", 1: "звичайний", 2: "терміново"}

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_users (
        tg_id      BIGINT PRIMARY KEY,
        username   TEXT,
        person     TEXT,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_tasks (
        id          BIGSERIAL PRIMARY KEY,
        project     TEXT,
        title       TEXT NOT NULL,
        body        TEXT,
        assignee    TEXT NOT NULL,
        creator     TEXT NOT NULL,
        priority    SMALLINT NOT NULL DEFAULT 1,
        status      TEXT NOT NULL DEFAULT 'todo',
        deadline    DATE,
        article_url TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        done_at     TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_tasks_assignee ON team_tasks (assignee, status)",
    "CREATE INDEX IF NOT EXISTS idx_team_tasks_status ON team_tasks (status)",
    """
    CREATE TABLE IF NOT EXISTS team_task_events (
        id      BIGSERIAL PRIMARY KEY,
        task_id BIGINT NOT NULL,
        actor   TEXT,
        event   TEXT NOT NULL,
        detail  TEXT,
        at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_task_events_task ON team_task_events (task_id, at)",
]

_schema_done = False


def ensure_team_schema():
    """Ідемпотентно створює таблиці team_* (раз на процес)."""
    global _schema_done
    if _schema_done:
        return
    for sql in _SCHEMA_STATEMENTS:
        bot_db.execute(sql)
    _schema_done = True


def _row_to_task(r):
    return {
        "id": r["id"],
        "project": r["project"] or "",
        "title": r["title"],
        "body": r["body"] or "",
        "assignee": r["assignee"],
        "creator": r["creator"],
        "priority": r["priority"],
        "status": r["status"],
        "deadline": r["deadline"].isoformat() if r["deadline"] else None,
        "article_url": r["article_url"] or "",
        "created_at": r["created_at"].astimezone(KYIV_TZ).isoformat(),
        "updated_at": r["updated_at"].astimezone(KYIV_TZ).isoformat(),
        "done_at": r["done_at"].astimezone(KYIV_TZ).isoformat() if r["done_at"] else None,
    }


def list_tasks(assignee=None, done_days=30):
    """Активні таски (+ закриті/зняті за останні done_days — для стрічки
    перемог і недавньої історії). assignee=None — всі (менеджерський вид)."""
    ensure_team_schema()
    sql = (
        "SELECT * FROM team_tasks WHERE "
        "(status NOT IN ('done','dropped') OR updated_at > now() - %s * INTERVAL '1 day')"
    )
    params = [done_days]
    if assignee:
        sql += " AND assignee = %s"
        params.append(assignee)
    sql += " ORDER BY (status = 'review') DESC, priority DESC, deadline ASC NULLS LAST, id DESC"
    return [_row_to_task(r) for r in bot_db.query(sql, params)]


def get_task(task_id):
    ensure_team_schema()
    rows = bot_db.query("SELECT * FROM team_tasks WHERE id = %s", (int(task_id),))
    return _row_to_task(rows[0]) if rows else None


def get_task_events(task_id):
    ensure_team_schema()
    return [
        {
            "actor": r["actor"],
            "event": r["event"],
            "detail": r["detail"],
            "at": r["at"].astimezone(KYIV_TZ).isoformat(),
        }
        for r in bot_db.query(
            "SELECT actor, event, detail, at FROM team_task_events "
            "WHERE task_id = %s ORDER BY at",
            (int(task_id),),
        )
    ]


def _add_event(task_id, actor, event, detail=None):
    bot_db.execute(
        "INSERT INTO team_task_events (task_id, actor, event, detail) VALUES (%s, %s, %s, %s)",
        (int(task_id), actor, event, detail),
    )


def create_task(creator, assignee, title, project=None, body=None, deadline=None, priority=1):
    """Створює таск, повертає його dict. deadline — 'YYYY-MM-DD' або None."""
    ensure_team_schema()
    rows = bot_db.query(
        """
        INSERT INTO team_tasks (project, title, body, assignee, creator, priority, deadline)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (project or None, title, body or None, assignee, creator,
         int(priority), deadline or None),
    )
    task = _row_to_task(rows[0])
    _add_event(task["id"], creator, "created", f"→ {assignee}")
    return task


def update_task(task_id, actor, fields):
    """Оновлює поля таска (без перевірки прав — це робить API-шар).
    fields — dict з підмножини: title, body, project, assignee, deadline,
    priority, status, article_url. Повертає оновлений таск або None."""
    ensure_team_schema()
    current = get_task(task_id)
    if not current:
        return None
    allowed = ("title", "body", "project", "assignee", "deadline", "priority", "status", "article_url")
    sets, params = [], []
    for key in allowed:
        if key not in fields:
            continue
        value = fields[key]
        if key == "priority":
            value = int(value)
        if key in ("body", "project", "deadline", "article_url") and not value:
            value = None
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return current
    sets.append("updated_at = now()")
    new_status = fields.get("status")
    if new_status == "done" and current["status"] != "done":
        sets.append("done_at = now()")
    params.append(int(task_id))
    bot_db.execute(f"UPDATE team_tasks SET {', '.join(sets)} WHERE id = %s", params)
    if new_status and new_status != current["status"]:
        _add_event(task_id, actor, "status", f"{current['status']} → {new_status}")
    edited = [k for k in fields if k != "status" and fields.get(k) != current.get(k)]
    if edited:
        _add_event(task_id, actor, "edited", ", ".join(edited))
    return get_task(task_id)


def stats_for(person):
    """Лічильники для шапки екрана журналістки: активні / на перевірці /
    закриті цього тижня (тиждень від понеділка, Київ) — паливо геймифікації."""
    ensure_team_schema()
    now = datetime.now(KYIV_TZ)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = bot_db.query(
        """
        SELECT
            count(*) FILTER (WHERE status IN ('todo','doing')) AS active,
            count(*) FILTER (WHERE status = 'review') AS review,
            count(*) FILTER (WHERE status = 'done' AND done_at >= %s) AS done_week
        FROM team_tasks WHERE assignee = %s
        """,
        (week_start, person),
    )
    r = rows[0]
    return {"active": r["active"], "review": r["review"], "done_week": r["done_week"]}


# ---------- Пінги від Лиса ----------

def _open_app_markup():
    """Кнопка «Відкрити» — web_app працює в приваті, куди й шлються пінги."""
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🦊 Відкрити завдання", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def _ping(bot, person, text):
    """Best-effort приват від Лиса: немає id у кеші або людина не стартувала
    бота — тихо пропускаємо (апка — джерело правди, пінг лише зручність)."""
    tg_id = await asyncio.to_thread(team_roster.tg_id_for, person)
    if not tg_id:
        print(f"team_tasks: пінг для «{person}» пропущено — tg_id ще невідомий")
        return False
    try:
        await bot.send_message(
            chat_id=tg_id, text=text, reply_markup=_open_app_markup(),
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        print(f"team_tasks: пінг «{person}» не пішов — {e}")
        return False


def _deadline_phrase(task):
    if not task["deadline"]:
        return ""
    d = datetime.fromisoformat(task["deadline"]).strftime("%d.%m")
    return f"\nДедлайн: {d}"


async def ping_assigned(bot, task):
    urgency = " 🔥 Терміново!" if task["priority"] == 2 else ""
    project = f" (проєкт «{task['project']}»)" if task["project"] else ""
    await _ping(
        bot, task["assignee"],
        f"🦊 {task['creator']} має для тебе завдання{project}:\n"
        f"«{task['title']}»{urgency}{_deadline_phrase(task)}",
    )


async def ping_review(bot, task):
    await _ping(
        bot, task["creator"],
        f"🦊 {task['assignee']} здає роботу на перевірку:\n«{task['title']}»"
        + (f"\n{task['article_url']}" if task["article_url"] else ""),
    )


async def ping_done(bot, task):
    await _ping(
        bot, task["assignee"],
        f"🎉 «{task['title']}» — прийнято! Микита пишається. 🦊",
    )


async def ping_returned(bot, task):
    await _ping(
        bot, task["assignee"],
        f"🦊 «{task['title']}» повернуто на доопрацювання — зазирни в картку.",
    )
