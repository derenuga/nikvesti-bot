"""
Creative tasks і тематики проєктів — дата-шар Mini App «Команда» (концепція v2).

Модель (Олег, 27.07.2026, docs/TEAM_APP_MODULE.md):
- **Creative task** — завдання від редактора: людина + тип (news/article) +
  проєкт з CMS АБО позапроєктне + тематика проєкту + кількість + нотатка.
  Деталізація вільна: від «стаття на тему відбудова» до «стаття про Снігурівку».
  Статуси прості: open → done / dropped (цикл doing/review з хвилі 1 прибрано —
  переусложнення; виконання з часом звірятиметься з nodes автоматично).
- **Тематика проєкту** — шар апки, якого немає в БД сайту: Катя розкроює квоту
  з CMS («IWPR: 60 публікацій») на тематики з цифрами («20 репортажів з сесій,
  15 інфозапитів…»). Тематики підказуються у формі creative task.
- project_id/theme_id зберігаємо РАЗОМ зі снапшотом назв (project_name,
  theme_name): проєкт живе в чужій БД (може зникнути з вибірки), тематику
  можуть перейменувати/видалити — а історія тасків має лишатись читабельною.

Люди — канонічним ім'ям ростера (team_roster), як у хвилі 1: numeric id
більшості невідомі до першого входу в апку. Пінги — best-effort через
team_roster.tg_id_for.

Таблиці (Нора, ensure ліниво): team_users (кеш tg_id, пише team_roster),
team_creative_tasks, team_project_themes, team_task_events (журнал).
Таблиця team_tasks хвилі 1 в проді не створювалась (сервер ще не стартував) —
код її більше не згадує.
"""

import asyncio
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from handlers import bot_db, team_notifications, team_roster
from handlers.helpers import normalize_https_url

KYIV_TZ = ZoneInfo("Europe/Kiev")

WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))

TASK_TYPES = ("news", "article", "post")
TASK_STATUSES = ("open", "done", "dropped")
TYPE_TITLES = {"news": "новина", "article": "стаття", "post": "пост"}

# Платформи для типу «пост» (Олег, 27.07): поки Telegram та Instagram
TASK_PLATFORMS = ("telegram", "instagram")
PLATFORM_PHRASES = {"telegram": "у Telegram", "instagram": "в Instagram"}

# Формат тематики проєкту (рішення Олега 27.07): конкретний формат,
# «гібридний» (мікс форматів) або None — тематика без формату.
THEME_FORMATS = ("news", "article", "post", "video", "hybrid")

# Дедлайни звітності проєкту (Олег, 27.07): наративні й фінансові звіти
# (проміжні/фінальні) або майлстоуни з датами. Поки — довідник у картці
# проєкту; нагадування Олегу/Каті/фінменеджерці — майбутній шар (беклог).
DEADLINE_KINDS = ("narrative", "financial", "milestone")
DEADLINE_STAGES = ("interim", "final")

# Хто за замовчуванням пише звіт (Олег, 27.07): фінанси — фінменеджерка,
# наративку — головредакторка. Майлстоуни дефолту не мають: це не звіт, а
# подія проєкту, відповідальний у кожного свій. Дефолт НЕ записується в БД —
# застосовується при читанні, тож правка тут діє на всі непризначені руками.
DEADLINE_DEFAULT_ASSIGNEE = {
    "financial": "Олена Бондаренко",
    "narrative": "Катерина Середа",
}

# Рух звіту: подали донору → донор прийняв. NULL — ще очікується.
DEADLINE_STATUSES = ("submitted", "accepted")

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
    # Перекриття відділу людини (Катя переносить людей між Newsroom/Creative
    # прямо в апці — відділ це дані, не код; дефолт лишається в team_roster)
    """
    CREATE TABLE IF NOT EXISTS team_dept (
        person     TEXT PRIMARY KEY,
        dept       TEXT NOT NULL,
        updated_by TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_project_themes (
        id         BIGSERIAL PRIMARY KEY,
        project_id BIGINT NOT NULL,
        name       TEXT NOT NULL,
        planned    INTEGER,
        format     TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_themes_project ON team_project_themes (project_id)",
    # Ідемпотентна міграція для таблиці, створеної до появи формату (27.07)
    "ALTER TABLE team_project_themes ADD COLUMN IF NOT EXISTS format TEXT",
    # Ручний порядок проєктів у списку (drag-n-drop Каті). БД сайту read-only,
    # тож порядок живе тут; проєкти поза таблицею йдуть після впорядкованих.
    """
    CREATE TABLE IF NOT EXISTS team_project_order (
        project_id BIGINT PRIMARY KEY,
        position   INTEGER NOT NULL
    )
    """,
    # Дедлайни звітності/майлстоуни проєктів
    """
    CREATE TABLE IF NOT EXISTS team_project_deadlines (
        id         BIGSERIAL PRIMARY KEY,
        project_id BIGINT NOT NULL,
        kind       TEXT NOT NULL,
        stage      TEXT,
        title      TEXT,
        due        DATE NOT NULL,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_pdl_project ON team_project_deadlines (project_id, due)",
    # Хто пише цей звіт (Олег, 27.07). NULL — діє дефолт за типом звіту
    # (див. DEADLINE_DEFAULT_ASSIGNEE): так зміна дефолту застосується заднім
    # числом до всіх, кому нікого не призначали руками.
    "ALTER TABLE team_project_deadlines ADD COLUMN IF NOT EXISTS assignee TEXT",
    # Рух звіту: NULL — ще очікується, submitted — подано донору,
    # accepted — донор прийняв. Хто і коли позначив — щоб було видно, чиє це
    # слово (Олег, Катя й Олена працюють з тими самими звітами).
    "ALTER TABLE team_project_deadlines ADD COLUMN IF NOT EXISTS status TEXT",
    "ALTER TABLE team_project_deadlines ADD COLUMN IF NOT EXISTS status_by TEXT",
    "ALTER TABLE team_project_deadlines ADD COLUMN IF NOT EXISTS status_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS team_creative_tasks (
        id           BIGSERIAL PRIMARY KEY,
        person       TEXT NOT NULL,
        creator      TEXT NOT NULL,
        project_id   BIGINT,
        project_name TEXT,
        type         TEXT NOT NULL,
        theme_id     BIGINT,
        theme_name   TEXT,
        qty          SMALLINT NOT NULL DEFAULT 1,
        note         TEXT,
        deadline     DATE,
        status       TEXT NOT NULL DEFAULT 'open',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        done_at      TIMESTAMPTZ
    )
    """,
    # Ідемпотентна міграція для таблиці, створеної до появи дедлайну (27.07)
    "ALTER TABLE team_creative_tasks ADD COLUMN IF NOT EXISTS deadline DATE",
    # Тип матеріалу став опційним (27.07): «будь-який» — зарахування виконання
    # йде через зв'язок із проєктом, незалежно від типу
    "ALTER TABLE team_creative_tasks ALTER COLUMN type DROP NOT NULL",
    # Снапшот донора (партнера) проєкту: у рядку таска донор іде першим,
    # і пінг каже «2 матеріали · IMS · Голоси Миколаєва» без походу в БД сайту
    "ALTER TABLE team_creative_tasks ADD COLUMN IF NOT EXISTS partner_name TEXT",
    # Платформа для тасків типу «пост» (telegram/instagram)
    "ALTER TABLE team_creative_tasks ADD COLUMN IF NOT EXISTS platform TEXT",
    # Хто закрив таск: Лис за зарахованими публікаціями (TRUE) чи людина
    # (FALSE). Різниця не косметична — авто-закритий таск авто-логіка має
    # право повернути у відкриті (підняли qty, відкликали матч), а закритий
    # руками рішенням Каті — ніколи. Див. team_matches.apply_progress.
    "ALTER TABLE team_creative_tasks ADD COLUMN IF NOT EXISTS auto_done BOOLEAN NOT NULL DEFAULT FALSE",
    # Папка проєкту на Google Drive (лінк; документи гранту — там)
    """
    CREATE TABLE IF NOT EXISTS team_project_drive (
        project_id BIGINT PRIMARY KEY,
        url        TEXT NOT NULL,
        updated_by TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_ctasks_person ON team_creative_tasks (person, status)",
    "CREATE INDEX IF NOT EXISTS idx_team_ctasks_status ON team_creative_tasks (status)",
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

_schema_lock = threading.Lock()
_schema_done = False


def ensure_team_schema():
    """Ідемпотентно створює таблиці team_* (раз на процес).

    Під блокуванням і в одній сесії: прапорця мало, бо перші паралельні
    запити входили сюди одночасно і ганяли CREATE/ALTER навперейми. А сесія
    робить із 18 окремих з'єднань одне — саме ці міграції були найдовшою
    частиною першого відкриття апки після деплою."""
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        with bot_db.session():
            for sql in _SCHEMA_STATEMENTS:
                bot_db.execute(sql)
        _schema_done = True


# ---------- Тематики проєктів ----------

def _row_to_theme(r):
    return {"id": r["id"], "project_id": r["project_id"], "name": r["name"],
            "planned": r["planned"], "format": r["format"]}


def list_themes():
    """Усі тематики всіх проєктів (їх десятки, не тисячі) — фронт групує сам."""
    ensure_team_schema()
    return [
        _row_to_theme(r)
        for r in bot_db.query(
            "SELECT id, project_id, name, planned, format FROM team_project_themes "
            "ORDER BY project_id, id"
        )
    ]


def add_theme(project_id, name, planned=None, format=None):
    ensure_team_schema()
    rows = bot_db.query(
        "INSERT INTO team_project_themes (project_id, name, planned, format) "
        "VALUES (%s, %s, %s, %s) RETURNING id, project_id, name, planned, format",
        (int(project_id), name.strip(), int(planned) if planned else None, format or None),
    )
    return _row_to_theme(rows[0])


def update_theme(theme_id, name=None, planned=..., format=...):
    """Сентинел ... — «не чіпати»; None — явно стерти (цифру або формат)."""
    ensure_team_schema()
    sets, params = [], []
    if name is not None:
        sets.append("name = %s")
        params.append(name.strip())
    if planned is not ...:
        sets.append("planned = %s")
        params.append(int(planned) if planned else None)
    if format is not ...:
        sets.append("format = %s")
        params.append(format or None)
    if not sets:
        return None
    params.append(int(theme_id))
    rows = bot_db.query(
        f"UPDATE team_project_themes SET {', '.join(sets)} WHERE id = %s "
        "RETURNING id, project_id, name, planned, format",
        params,
    )
    return _row_to_theme(rows[0]) if rows else None


def delete_theme(theme_id):
    ensure_team_schema()
    return bot_db.execute(
        "DELETE FROM team_project_themes WHERE id = %s", (int(theme_id),)
    )


# ---------- Порядок проєктів ----------

def get_project_order():
    """{project_id: position} — ручний порядок списку проєктів."""
    ensure_team_schema()
    return {
        r["project_id"]: r["position"]
        for r in bot_db.query("SELECT project_id, position FROM team_project_order")
    }


def set_project_order(project_ids):
    """Перезаписує порядок повністю (список у апки завжди цілий, ~десятки).

    Одна транзакція і два statement замість DELETE + N окремих INSERT. Було
    41 з'єднання і ~384 мс на 40 проєктів (на Railway ще втричі більше), а
    головне — без транзакції: обрив посеред циклу лишав список наполовину
    впорядкованим, тобто гірше, ніж якби Катя нічого не перетягувала.

    Дублі id відкидаємо, зберігаючи перше входження: PRIMARY KEY на
    project_id, і повторюваний id завалив би весь запис."""
    ensure_team_schema()
    seen, ids = set(), []
    for pid in project_ids:
        pid = int(pid)
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
    with bot_db.transaction():
        bot_db.execute("DELETE FROM team_project_order")
        if ids:
            bot_db.execute(
                "INSERT INTO team_project_order (project_id, position) "
                "SELECT unnest(%s::bigint[]), generate_subscripts(%s::bigint[], 1) - 1",
                (ids, ids),
            )


# ---------- Папки проєктів на Google Drive ----------

def get_drive_links():
    """{project_id: url} — прикріплені папки Drive."""
    ensure_team_schema()
    return {
        r["project_id"]: r["url"]
        for r in bot_db.query("SELECT project_id, url FROM team_project_drive")
    }


def set_drive_link(project_id, url, updated_by):
    """Прикріпляє папку (upsert); порожній url — відкріпляє."""
    ensure_team_schema()
    if not url:
        return bot_db.execute(
            "DELETE FROM team_project_drive WHERE project_id = %s", (int(project_id),)
        )
    return bot_db.execute(
        """
        INSERT INTO team_project_drive (project_id, url, updated_by, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (project_id) DO UPDATE SET
            url = EXCLUDED.url, updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (int(project_id), url, updated_by),
    )


# ---------- Дедлайни звітності проєктів ----------

def _row_to_deadline(r):
    assignee = r.get("assignee")
    return {"id": r["id"], "project_id": r["project_id"], "kind": r["kind"],
            "stage": r["stage"], "title": r["title"] or "",
            "due": r["due"].isoformat(),
            # assignee — ефективний (з дефолтом), assignee_custom — чи його
            # призначали руками (щоб UI показав «за замовчуванням»)
            "assignee": assignee or DEADLINE_DEFAULT_ASSIGNEE.get(r["kind"]),
            "assignee_custom": bool(assignee),
            "status": r.get("status"),
            "status_by": r.get("status_by"),
            "status_at": r["status_at"].astimezone(KYIV_TZ).date().isoformat()
                         if r.get("status_at") else None}


def list_project_deadlines():
    """Усі дедлайни всіх проєктів — фронт групує сам."""
    ensure_team_schema()
    return [
        _row_to_deadline(r)
        for r in bot_db.query(
            "SELECT id, project_id, kind, stage, title, due, assignee, status, status_by, status_at "
            "FROM team_project_deadlines ORDER BY due, id"
        )
    ]


def add_project_deadline(creator, project_id, kind, due, stage=None, title=None):
    ensure_team_schema()
    rows = bot_db.query(
        "INSERT INTO team_project_deadlines (project_id, kind, stage, title, due, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, project_id, kind, stage, title, due, assignee, status, status_by, status_at",
        (int(project_id), kind, stage or None, (title or "").strip() or None, due, creator),
    )
    return _row_to_deadline(rows[0])


def update_project_deadline(deadline_id, kind=None, due=None, stage=..., title=...):
    ensure_team_schema()
    sets, params = [], []
    if kind:
        sets.append("kind = %s")
        params.append(kind)
    if due:
        sets.append("due = %s")
        params.append(due)
    if stage is not ...:
        sets.append("stage = %s")
        params.append(stage or None)
    if title is not ...:
        sets.append("title = %s")
        params.append((title or "").strip() or None)
    if not sets:
        return None
    params.append(int(deadline_id))
    rows = bot_db.query(
        f"UPDATE team_project_deadlines SET {', '.join(sets)} WHERE id = %s "
        "RETURNING id, project_id, kind, stage, title, due, assignee, status, status_by, status_at",
        params,
    )
    return _row_to_deadline(rows[0]) if rows else None


def set_deadline_assignee(deadline_id, person):
    """Призначає відповідального за звіт. person=None — повернути дефолт
    за типом звіту."""
    ensure_team_schema()
    rows = bot_db.query(
        "UPDATE team_project_deadlines SET assignee = %s WHERE id = %s "
        "RETURNING id, project_id, kind, stage, title, due, assignee, status, status_by, status_at",
        (person or None, int(deadline_id)),
    )
    return _row_to_deadline(rows[0]) if rows else None


def set_deadline_status(deadline_id, status, actor):
    """Позначає рух звіту: submitted / accepted, або None — повернути в
    «очікується» (помилились кнопкою)."""
    ensure_team_schema()
    rows = bot_db.query(
        "UPDATE team_project_deadlines SET status = %s, status_by = %s, "
        "status_at = CASE WHEN %s IS NULL THEN NULL ELSE now() END WHERE id = %s "
        "RETURNING id, project_id, kind, stage, title, due, assignee, status, status_by, status_at",
        (status, actor if status else None, status, int(deadline_id)),
    )
    return _row_to_deadline(rows[0]) if rows else None


def delete_project_deadline(deadline_id):
    ensure_team_schema()
    return bot_db.execute(
        "DELETE FROM team_project_deadlines WHERE id = %s", (int(deadline_id),)
    )


# ---------- Creative tasks ----------

def _row_to_task(r):
    return {
        "id": r["id"],
        "person": r["person"],
        "creator": r["creator"],
        "project_id": r["project_id"],
        "project_name": r["project_name"],
        "partner_name": r["partner_name"],
        "platform": r["platform"],
        "type": r["type"],
        "theme_id": r["theme_id"],
        "theme_name": r["theme_name"],
        "qty": r["qty"],
        "note": r["note"] or "",
        "deadline": r["deadline"].isoformat() if r["deadline"] else None,
        "status": r["status"],
        "auto_done": bool(r.get("auto_done")),
        "created_at": r["created_at"].astimezone(KYIV_TZ).isoformat(),
        "done_at": r["done_at"].astimezone(KYIV_TZ).isoformat() if r["done_at"] else None,
    }


def list_tasks(person=None, done_days=30, limit=200):
    """Відкриті таски + закриті/зняті за останні done_days.
    person=None — усі (редакторський вид)."""
    ensure_team_schema()
    sql = (
        "SELECT * FROM team_creative_tasks WHERE "
        "(status = 'open' OR updated_at > now() - %s * INTERVAL '1 day')"
    )
    params = [done_days]
    if person:
        sql += " AND person = %s"
        params.append(person)
    sql += " ORDER BY (status = 'open') DESC, id DESC LIMIT %s"
    params.append(limit)
    return [_row_to_task(r) for r in bot_db.query(sql, params)]


def list_open_tasks():
    """ВСІ відкриті таски (без ліміту й без закритих) — пул кандидатів для
    авто-обліку. Прогін бере його ОДИН раз і фільтрує в памʼяті: інакше на
    кожну публікацію йшов би окремий запит у Нору."""
    ensure_team_schema()
    return [_row_to_task(r) for r in bot_db.query(
        "SELECT * FROM team_creative_tasks WHERE status = 'open' ORDER BY id"
    )]


def get_task(task_id):
    ensure_team_schema()
    rows = bot_db.query("SELECT * FROM team_creative_tasks WHERE id = %s", (int(task_id),))
    return _row_to_task(rows[0]) if rows else None


def _add_event(task_id, actor, event, detail=None):
    bot_db.execute(
        "INSERT INTO team_task_events (task_id, actor, event, detail) VALUES (%s, %s, %s, %s)",
        (int(task_id), actor, event, detail),
    )


def create_task(creator, person, type_, project_id=None, project_name=None,
                theme_id=None, theme_name=None, qty=1, note=None, deadline=None,
                partner_name=None, platform=None, notify=True):
    ensure_team_schema()
    rows = bot_db.query(
        """
        INSERT INTO team_creative_tasks
            (person, creator, project_id, project_name, type, theme_id, theme_name,
             qty, note, deadline, partner_name, platform)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (person, creator,
         int(project_id) if project_id else None, project_name or None,
         type_ or None,
         int(theme_id) if theme_id else None, theme_name or None,
         max(1, int(qty)), (note or "").strip() or None, deadline or None,
         partner_name or None, platform or None),
    )
    task = _row_to_task(rows[0])
    _add_event(task["id"], creator, "created", f"→ {person}")
    # Сповіщення в апці дублює пінг у приват: пінг не дійде, доки людина хоч
    # раз не відкрила апку, а стрічка чекає її там завжди.
    # notify=False — коли таск заводиться під уже виконану публікацію (Катя
    # зараховує з черги звірки): «тобі поставили завдання» за зроблене вчора
    # тільки збиває з пантелику, а «завдання виконано» вона однаково отримає.
    if notify:
        team_notifications.notify_safe(
            "task_assigned", task_summary(task), audience="person", person=person,
            body=f"поставив(ла) {creator.split()[0]}"
                 + (f" · до {task['deadline'][8:10]}.{task['deadline'][5:7]}"
                    if task["deadline"] else ""),
            object_type="task", object_id=task["id"],
            dedup_key=f"task_assigned:{task['id']}")
    return task


def create_tasks_bulk(specs):
    """Створює пачку тасків ОДНІЄЮ транзакцією: масова постановка з проєкту —
    це один намір Каті («поставити тему п'ятьом»), тож або лягають усі, або
    жодного. Раніше кожен create_task ішов окремим з'єднанням і окремою
    транзакцією, і збій на третій людині з п'яти лишав дві поставлені й три ні,
    без жодного сліду про те, що сталось.

    specs — список kwargs для create_task. Повертає створені таски."""
    ensure_team_schema()
    with bot_db.transaction():
        return [create_task(**spec) for spec in specs]


def update_task_fields(task_id, actor, qty=None, deadline=..., note=...,
                       theme_id=..., theme_name=..., type_=..., platform=...):
    """Редагування таска (кількість, дедлайн, нотатка, тематика, ТИП).
    Сентинел ... — «не чіпати»; None/порожнє — явно стерти.

    Тип редагується з тієї ж причини, з якої зʼявився безликий «матеріал»:
    таск, заведений під тематику без формату, лишався без типу, і виконавиця
    бачила «матеріал» замість «новина»."""
    ensure_team_schema()
    sets, params = [], []
    if type_ is not ...:
        sets.append("type = %s")
        params.append(type_ or None)
    if platform is not ...:
        sets.append("platform = %s")
        params.append(platform or None)
    if qty is not None:
        sets.append("qty = %s")
        params.append(max(1, min(99, int(qty))))
    if deadline is not ...:
        sets.append("deadline = %s")
        params.append(deadline or None)
    if note is not ...:
        sets.append("note = %s")
        params.append((note or "").strip() or None)
    if theme_id is not ...:
        sets.append("theme_id = %s")
        params.append(int(theme_id) if theme_id else None)
        sets.append("theme_name = %s")
        params.append(theme_name if theme_id else None)
    if not sets:
        return get_task(task_id)
    sets.append("updated_at = now()")
    params.append(int(task_id))
    rows = bot_db.query(
        f"UPDATE team_creative_tasks SET {', '.join(sets)} WHERE id = %s RETURNING *",
        params,
    )
    if not rows:
        return None
    _add_event(task_id, actor, "edited", None)
    return _row_to_task(rows[0])


def set_status(task_id, actor, status, auto=False):
    """open → done/dropped (і назад open, якщо закрили помилково).

    auto=True — це зробив Лис за зарахованими публікаціями. Позначка лягає в
    auto_done, і саме вона дозволяє потім повернути таск у відкриті, коли
    прогрес перестав покривати qty. Дія людини завжди знімає позначку:
    закрила Катя — Лис уже не перевідкриє."""
    ensure_team_schema()
    current = get_task(task_id)
    if not current:
        return None
    done_at_sql = "now()" if status == "done" else "NULL"
    rows = bot_db.query(
        f"UPDATE team_creative_tasks SET status = %s, updated_at = now(), "
        f"auto_done = %s, done_at = {done_at_sql} WHERE id = %s RETURNING *",
        (status, bool(auto) and status == "done", int(task_id)),
    )
    task = _row_to_task(rows[0])
    if status != current["status"]:
        _add_event(task_id, actor, "status",
                   f"{current['status']} → {status}" + (" (авто)" if auto else ""))
    return task


# ---------- Пінги від Лиса ----------

def _open_app_markup():
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Відкрити завдання", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def _ping(bot, person, text):
    """Best-effort приват: немає tg_id (людина ще не відкривала апку) або
    людина не стартувала бота — тихо пропускаємо, апка лишається джерелом правди."""
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


_QTY_WORDS = {
    "news": ("новина", "новини", "новин"),
    "article": ("стаття", "статті", "статей"),
    "post": ("пост", "пости", "постів"),
    None: ("матеріал", "матеріали", "матеріалів"),
}


def task_summary(task):
    """Людський рядок таска: «3 новини · Критичні потреби · IMS».

    Порядок той самий, що в апці (taskLine): скільки й чого → ТЕМАТИКА →
    донор. Назва проєкту сюди не йде взагалі — крім випадку, коли тематики
    немає й донора немає, і без неї рядок став би безадресним. Олег про це
    просив не раз: людині треба знати, ЩО писати, а назва проєкту («Fight
    for Facts – Stage 9-11») цього не каже і лише з'їдає рядок."""
    qty = task["qty"]
    one, few, many = _QTY_WORDS.get(task["type"], _QTY_WORDS[None])
    type_word = one if qty == 1 else (few if qty < 5 else many)
    if task["type"] == "post" and task.get("platform") in PLATFORM_PHRASES:
        type_word += f" {PLATFORM_PHRASES[task['platform']]}"
    parts = [f"{qty} {type_word}" if qty > 1 else type_word]
    if task.get("theme_name"):
        parts.append(task["theme_name"])
    if task.get("partner_name"):
        parts.append(task["partner_name"])
    if not task.get("theme_name") and not task.get("partner_name") \
            and task.get("project_name"):
        parts.append(task["project_name"])
    return " · ".join(parts)


async def ping_assigned(bot, task):
    text = f"🦊 {task['creator']} має для тебе завдання:\n{task_summary(task)}"
    if task["deadline"]:
        d = datetime.fromisoformat(task["deadline"])
        text += f"\nДедлайн: {d.strftime('%d.%m')}"
    if task["note"]:
        text += f"\n\n{task['note']}"
    await _ping(bot, task["person"], text)
