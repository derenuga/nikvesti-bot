"""
Сповіщення Mini App «Команда» — стрічка подій за роллю (Нора).

Олег окреслив «Сповіщення» не як один екран черги, а як СТРІЧКУ ПОДІЙ: що
сталося з тим, за що ти відповідаєш. Черга звірки (team_matches) — лише один
тип, до того ж особливий: він потребує дії, а не прочитання. Тут — спільна
модель для решти.

Модель:
  kind      — тип події (див. KINDS);
  audience  — 'managers' (Олег, Катя, фінменеджерка) або 'person';
  person    — адресат, коли audience='person';
  object    — на що вказує подія (task/deadline/project/match) + id;
  dedup_key — UNIQUE: «не смикати одним і тим самим двічі». Емітер може
              спокійно викликати notify() у циклі щогодини — другий раз
              нічого не створиться.

Прочитане — окремою таблицею team_notification_reads (подія × людина), бо
менеджерські сповіщення бачать двоє-троє: read_at у самому рядку означав би,
що Катя прочитала за Олега.

Емітери навмисно тонкі й тихі: збій запису сповіщення НЕ має ламати дію, яка
його породила (поставити таску важливіше, ніж повідомити про неї).
"""

import json
import threading

from handlers import bot_db

# Типи подій. Беклог Олега ширший (проєкт добігає кінця, KPI під загрозою,
# матеріал передано в SMM) — тут те, що вже має джерело істини в боті.
KINDS = {
    "task_assigned": "Нове завдання",
    "task_done": "Завдання виконано",
    "report_deadline": "Звітний дедлайн",
    "task_overdue": "Дедлайн завдання минув",
    "absence_request": "Запит на відпустку",
    "absence_decided": "Відповідь на запит",
    "impact_credit": "Імпакт за твоєї участі",
}

AUDIENCE_MANAGERS = "managers"
AUDIENCE_PERSON = "person"

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_notifications (
        id          BIGSERIAL PRIMARY KEY,
        kind        TEXT NOT NULL,
        audience    TEXT NOT NULL,
        person      TEXT,
        object_type TEXT,
        object_id   TEXT,
        title       TEXT NOT NULL,
        body        TEXT,
        url         TEXT,
        dedup_key   TEXT UNIQUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_notif_feed ON team_notifications (audience, person, id DESC)",
    # Структуровані шматки події (тематика/завдання/автор/донор) — щоб екран
    # міг розкласти рядок, а не тулити все в один обрізаний title (Олег,
    # 30.07: «незрозуміло, що виконано; тематику догори, автора вниз»)
    "ALTER TABLE team_notifications ADD COLUMN IF NOT EXISTS meta JSONB",
    """
    CREATE TABLE IF NOT EXISTS team_notification_reads (
        notification_id BIGINT NOT NULL,
        person          TEXT NOT NULL,
        read_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (notification_id, person)
    )
    """,
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_notifications_schema():
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


def _row(r, unread=True):
    return {
        "id": r["id"],
        "kind": r["kind"],
        "meta": r.get("meta") or None,
        "kind_title": KINDS.get(r["kind"], r["kind"]),
        "audience": r["audience"],
        "person": r["person"],
        "object_type": r["object_type"],
        "object_id": r["object_id"],
        "title": r["title"],
        "body": r["body"] or "",
        "url": r["url"] or "",
        "created_at": r["created_at"].isoformat(),
        "unread": unread,
    }


def notify(kind, title, *, audience=AUDIENCE_MANAGERS, person=None, body=None,
           url=None, object_type=None, object_id=None, dedup_key=None, meta=None):
    """Кладе подію в стрічку. Повертає рядок або None, якщо така подія вже
    була (dedup_key). Тихо: помилку ковтає викликач через notify_safe.
    meta — структуровані шматки для екрана (тематика/завдання/автор/донор)."""
    ensure_notifications_schema()
    rows = bot_db.query(
        """
        INSERT INTO team_notifications
            (kind, audience, person, object_type, object_id, title, body, url,
             dedup_key, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dedup_key) DO NOTHING
        RETURNING id, kind, audience, person, object_type, object_id, title,
                  body, url, created_at, meta
        """,
        (kind, audience, person, object_type,
         str(object_id) if object_id is not None else None,
         title, body, url, dedup_key,
         json.dumps(meta, ensure_ascii=False) if meta else None),
    )
    return _row(rows[0]) if rows else None


def notify_safe(*args, **kwargs):
    """notify(), який не має права зламати дію, що його породила.

    Просто try/except тут МАЛО. Масова постановка тасків іде однією
    транзакцією, а в Postgres будь-який збійний statement робить усю
    транзакцію непридатною: проковтнутий виняток однаково завалив би решту
    пачки з «current transaction is aborted». Тому всередині транзакції
    загортаємо запис у SAVEPOINT — тоді відкочується рівно сповіщення, а
    таски лягають."""
    in_tx = getattr(bot_db._local, "in_tx", False)
    try:
        if in_tx:
            bot_db.execute("SAVEPOINT notif")
        result = notify(*args, **kwargs)
        if in_tx:
            bot_db.execute("RELEASE SAVEPOINT notif")
        return result
    except Exception as e:
        if in_tx:
            try:
                bot_db.execute("ROLLBACK TO SAVEPOINT notif")
            except Exception:
                pass
        print(f"team_notifications: подію не записано — {e}")
        return None


def _visible_where(is_manager):
    """Умова видимості: свої персональні + менеджерські, якщо ти менеджер."""
    where = "(n.audience = 'person' AND n.person = %s)"
    if is_manager:
        where += " OR n.audience = 'managers'"
    return f"({where})"


def feed(person, is_manager, limit=40):
    """Стрічка людини: її персональні події + (для менеджера) менеджерські.
    Прочитане не зникає, а приглушується — щоб можна було перечитати."""
    ensure_notifications_schema()
    rows = bot_db.query(
        f"""
        SELECT n.id, n.kind, n.audience, n.person, n.object_type, n.object_id, n.meta,
               n.title, n.body, n.url, n.created_at,
               (r.person IS NULL) AS unread
        FROM team_notifications n
        LEFT JOIN team_notification_reads r
               ON r.notification_id = n.id AND r.person = %s
        WHERE {_visible_where(is_manager)}
        ORDER BY n.id DESC LIMIT %s
        """,
        (person, person, limit),
    )
    return [_row(r, unread=bool(r["unread"])) for r in rows]


def unread_count(person, is_manager):
    ensure_notifications_schema()
    rows = bot_db.query(
        f"""
        SELECT COUNT(*) AS c FROM team_notifications n
        LEFT JOIN team_notification_reads r
               ON r.notification_id = n.id AND r.person = %s
        WHERE r.person IS NULL AND {_visible_where(is_manager)}
        """,
        (person, person),
    )
    return rows[0]["c"] if rows else 0


def mark_read(person, is_manager, ids=None):
    """Позначає прочитаним. ids=None — усе, що людина бачить (вона щойно
    відкрила екран). Ідемпотентно."""
    ensure_notifications_schema()
    if ids is not None:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        return bot_db.execute(
            "INSERT INTO team_notification_reads (notification_id, person) "
            "SELECT unnest(%s::bigint[]), %s ON CONFLICT DO NOTHING",
            (ids, person),
        )
    return bot_db.execute(
        f"""
        INSERT INTO team_notification_reads (notification_id, person)
        SELECT n.id, %s FROM team_notifications n
        WHERE {_visible_where(is_manager)}
        ON CONFLICT DO NOTHING
        """,
        (person, person),
    )
