"""
Матчі «публікація → creative task»: пам'ять зарахованого виконання (Нора).

Дата-шар авто-обліку (docs/TEAM_APP_MATCHING_BRIEF.md). Тут — тільки зберігання
й арифметика прогресу; хто саме вирішує, яку тематику виконує публікація —
у team_matching.py (пре-фільтр + AI-суддя).

Ключова властивість — ІДЕМПОТЕНТНІСТЬ через UNIQUE (source, ref): одна
публікація судиться РІВНО раз, скільки б разів не прогнали фоновий скан чи
ретроспективу. Без цього повторний прогін зараховував би той самий матеріал
удруге і «2/3» перетворювалось би на «6/3».

Статуси матчу:
  auto      — суддя дав high, зараховано без людини;
  pending   — суддя вагався (або автор невідомий, як у TG-постів) → черга Каті;
  confirmed — Катя підтвердила (або перевизначила тематику);
  rejected  — Катя сказала «не те» (запис лишається — щоб не судити повторно);
  skipped   — кандидатів не було (теж запис: інакше суддя ганявся б щогодини
              по тих самих нодах).
Зараховуються тільки auto і confirmed (COUNTED).

Авто-закриття таска (apply_progress) продумане на крайні випадки:
  • таск закрили руками, потім прийшов матч → НЕ чіпаємо (людське рішення
    сильніше за арифметику; прогрес просто буде «4/3»);
  • qty підняли після зарахувань → авто-закритий таск сам вертається у відкриті;
  • матч відкликали (rejected) після авто-закриття → те саме.
Розрізняє їх колонка team_creative_tasks.auto_done: «закрив Лис» проти
«закрила Катя». Руками закритий таск авто-логіка не перевідкриває ніколи.
"""

import threading

from handlers import bot_db, team_notifications, team_tasks

MATCH_STATUSES = ("auto", "pending", "confirmed", "rejected", "skipped")

# Які статуси йдуть у прогрес таска
COUNTED = ("auto", "confirmed")

# «Питання закрите»: цю публікацію повторно не судимо НІКОЛИ. skipped сюди
# навмисно не входить — тоді кандидатів не було, а таск на цю тематику могли
# поставити пізніше (Катя часто ставить заднім числом). Пере-розгляд skipped
# дешевий: пре-фільтр без AI, і суддя вмикається, лише якщо кандидати таки
# з'явились.
DECIDED = ("auto", "pending", "confirmed", "rejected")

SOURCES = ("site", "telegram")

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_task_matches (
        id          BIGSERIAL PRIMARY KEY,
        source      TEXT NOT NULL,
        ref         TEXT NOT NULL,
        task_id     BIGINT,
        person      TEXT,
        project_id  BIGINT,
        confidence  TEXT,
        reasoning   TEXT,
        status      TEXT NOT NULL,
        title       TEXT,
        url         TEXT,
        published   TIMESTAMPTZ,
        node_type   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        decided_by  TEXT,
        decided_at  TIMESTAMPTZ,
        UNIQUE (source, ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ttm_task ON team_task_matches (task_id)",
    "CREATE INDEX IF NOT EXISTS idx_ttm_status ON team_task_matches (status)",
    # Картка в черзі має виглядати як новина, а не як рядок бази: опис і
    # зображення (og:description / og:image сторінки або фото поста каналу).
    # Тягнемо їх ЛИШЕ для спірних — у авто-зарахованих картки немає.
    "ALTER TABLE team_task_matches ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE team_task_matches ADD COLUMN IF NOT EXISTS image TEXT",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_match_schema():
    """Ідемпотентно створює team_task_matches (раз на процес).
    Модуль опційний, як team_kpi — у bot_db._SCHEMA_STATEMENTS не лізе."""
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        team_tasks.ensure_team_schema()   # auto_done живе в таблиці тасків
        with bot_db.session():
            for sql in _SCHEMA_STATEMENTS:
                bot_db.execute(sql)
        _schema_done = True


_COLS = ("id, source, ref, task_id, person, project_id, confidence, reasoning, "
         "status, title, url, description, image, published, node_type, "
         "created_at, decided_by, decided_at")


def _row_to_match(r):
    return {
        "id": r["id"],
        "source": r["source"],
        "ref": r["ref"],
        "task_id": r["task_id"],
        "person": r["person"],
        "project_id": r["project_id"],
        "confidence": r["confidence"],
        "reasoning": r["reasoning"] or "",
        "status": r["status"],
        "title": r["title"] or "",
        "url": r["url"] or "",
        "description": r["description"] or "",
        "image": r["image"] or "",
        "node_type": r["node_type"],
        "published": r["published"].astimezone(team_tasks.KYIV_TZ).isoformat()
                     if r["published"] else None,
        "created_at": r["created_at"].astimezone(team_tasks.KYIV_TZ).isoformat(),
        "decided_by": r["decided_by"],
    }


# ---------- Запис ----------

def record_match(source, ref, status, task_id=None, person=None, project_id=None,
                 confidence=None, reasoning=None, title=None, url=None,
                 published=None, node_type=None, description=None, image=None):
    """Записує вердикт по публікації. Повертає матч або None, якщо питання по
    ній уже вирішене (UNIQUE (source, ref) — саме тут спрацьовує
    ідемпотентність повторних прогонів).

    Виняток — рядок зі статусом skipped: тоді кандидатів не було, і якщо таск
    на цю тематику з'явився пізніше, вердикт перезаписується (DO UPDATE …
    WHERE status = 'skipped'). Уже вирішені (auto/pending/confirmed/rejected)
    не чіпаються ніколи."""
    ensure_match_schema()
    rows = bot_db.query(
        f"""
        INSERT INTO team_task_matches
            (source, ref, task_id, person, project_id, confidence, reasoning,
             status, title, url, published, node_type, description, image)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s)
        ON CONFLICT (source, ref) DO UPDATE SET
            task_id = EXCLUDED.task_id, person = EXCLUDED.person,
            project_id = EXCLUDED.project_id, confidence = EXCLUDED.confidence,
            reasoning = EXCLUDED.reasoning, status = EXCLUDED.status,
            title = EXCLUDED.title, url = EXCLUDED.url,
            description = EXCLUDED.description, image = EXCLUDED.image,
            published = EXCLUDED.published, node_type = EXCLUDED.node_type,
            created_at = now()
        WHERE team_task_matches.status = 'skipped'
        RETURNING {_COLS}
        """,
        (source, str(ref), int(task_id) if task_id else None, person,
         int(project_id) if project_id else None, confidence, reasoning,
         status, (title or "")[:500], url, published, node_type,
         (description or None), image),
    )
    return _row_to_match(rows[0]) if rows else None


def known_refs(source, refs=None, statuses=DECIDED):
    """Множина ref із вирішеним питанням — пре-фільтр перед скануванням, щоб
    не ганяти суддю (і БД сайту) по тому самому вдруге. За замовчуванням
    skipped не рахується вирішеним (див. DECIDED)."""
    ensure_match_schema()
    params = [source, list(statuses)]
    sql = "SELECT ref FROM team_task_matches WHERE source = %s AND status = ANY(%s)"
    if refs is not None:
        refs = [str(r) for r in refs]
        if not refs:
            return set()
        sql += " AND ref = ANY(%s)"
        params.append(refs)
    return {r["ref"] for r in bot_db.query(sql, params)}


def find_match(source, ref):
    """Матч за публікацією (source, ref) — щоб не плодити другий запис, коли
    ту саму публікацію зараховують руками поверх авто-вердикту."""
    ensure_match_schema()
    rows = bot_db.query(
        f"SELECT {_COLS} FROM team_task_matches WHERE source = %s AND ref = %s",
        (source, str(ref)),
    )
    return _row_to_match(rows[0]) if rows else None


def get_match(match_id):
    ensure_match_schema()
    rows = bot_db.query(
        f"SELECT {_COLS} FROM team_task_matches WHERE id = %s", (int(match_id),)
    )
    return _row_to_match(rows[0]) if rows else None


# ---------- Прогрес тасків ----------

def counted_by_task(task_ids=None):
    """{task_id: [матч, …]} — зараховані публікації по тасках.

    Прикріплений лінк — вимога Олега: у картці має бути видно не лише «2/3»,
    а й ЯКІ саме публікації зараховані, з переходом на матеріал."""
    ensure_match_schema()
    sql = (f"SELECT {_COLS} FROM team_task_matches "
           f"WHERE task_id IS NOT NULL AND status = ANY(%s)")
    params = [list(COUNTED)]
    if task_ids is not None:
        task_ids = [int(t) for t in task_ids]
        if not task_ids:
            return {}
        sql += " AND task_id = ANY(%s)"
        params.append(task_ids)
    sql += " ORDER BY published NULLS LAST, id"
    out = {}
    for r in bot_db.query(sql, params):
        m = _row_to_match(r)
        out.setdefault(m["task_id"], []).append(m)
    return out


def attach_progress(tasks):
    """Домальовує таскам прогрес: matches (лінки зарахованого) + done_count.
    Один запит на весь список — апці це приїжджає в bootstrap."""
    if not tasks:
        return tasks
    by_task = counted_by_task([t["id"] for t in tasks])
    for t in tasks:
        items = by_task.get(t["id"], [])
        t["matches"] = [
            {"id": m["id"], "title": m["title"], "url": m["url"],
             "published": m["published"], "source": m["source"]}
            for m in items
        ]
        t["done_count"] = len(items)
    return tasks


def counted_count(task_id):
    ensure_match_schema()
    rows = bot_db.query(
        "SELECT COUNT(*) AS c FROM team_task_matches "
        "WHERE task_id = %s AND status = ANY(%s)",
        (int(task_id), list(COUNTED)),
    )
    return rows[0]["c"] if rows else 0


def apply_progress(task_id, actor="🦊 Лис"):
    """Зводить статус таска з прогресом. Повертає (таск, дія):
    дія — "done" (щойно авто-закрили), "reopen" (повернули у відкриті) або None.

    Пінг НЕ шлеться — це рішення викликача (ретроспектива пінги глушить)."""
    if not task_id:
        return None, None
    task = team_tasks.get_task(task_id)
    if not task:
        return None, None
    count = counted_count(task_id)
    if task["status"] == "open" and count >= task["qty"]:
        closed = team_tasks.set_status(task_id, actor, "done", auto=True)
        _notify_done(closed)
        return closed, "done"
    # Авто-закритий таск вертаємо у відкриті, коли прогрес перестав покривати
    # qty: підняли кількість або відкликали матч. Закритий РУКАМИ не чіпаємо.
    if task["status"] == "done" and task.get("auto_done") and count < task["qty"]:
        return team_tasks.set_status(task_id, actor, "open", auto=True), "reopen"
    return task, None


def _notify_done(task):
    """Подія «завдання виконано» — і виконавиці, і керівництву. Обидві
    сповіщення дедуплікуються по id таска: повторний прогін чи перевідкриття
    й нове закриття не сиплють дублями."""
    if not task:
        return
    links = [m["url"] for m in counted_by_task([task["id"]]).get(task["id"], [])]
    body = f"Лис зарахував сам: {len(links)} із {task['qty']}"
    team_notifications.notify_safe(
        "task_done", team_tasks.task_summary(task), audience="person",
        person=task["person"], body=body, url=links[0] if links else None,
        object_type="task", object_id=task["id"],
        dedup_key=f"task_done:{task['id']}")
    team_notifications.notify_safe(
        "task_done", f"{task['person']}: {team_tasks.task_summary(task)}",
        audience="managers", body=body, url=links[0] if links else None,
        object_type="task", object_id=task["id"],
        dedup_key=f"task_done_mgr:{task['id']}")


def recount_after_qty_change(task_id, actor="🦊 Лис"):
    """Те саме, що apply_progress, але для редагування таска в апці:
    змінили qty — статус має наздогнати прогрес в обидва боки."""
    return apply_progress(task_id, actor)


# ---------- Черга звірки ----------

def pending_matches(limit=100):
    """Спірні випадки для «Сповіщень» Каті — найсвіжіші зверху."""
    ensure_match_schema()
    rows = bot_db.query(
        f"SELECT {_COLS} FROM team_task_matches WHERE status = 'pending' "
        f"ORDER BY COALESCE(published, created_at) DESC, id DESC LIMIT %s",
        (limit,),
    )
    return [_row_to_match(r) for r in rows]


def pending_without_meta(limit=20):
    """Спірні матчі без опису/зображення — картки, що ще не «одягнені».
    Потрібно для самолікування: рядки, записані до появи цих колонок (або
    коли сторінка тоді не відповіла), доганяються наступним прогоном."""
    ensure_match_schema()
    rows = bot_db.query(
        f"SELECT {_COLS} FROM team_task_matches WHERE status = 'pending' "
        f"AND (description IS NULL OR image IS NULL) ORDER BY id DESC LIMIT %s",
        (limit,),
    )
    return [_row_to_match(r) for r in rows]


def set_meta(match_id, description=None, image=None):
    """Дописує опис/зображення до вже записаного матчу."""
    ensure_match_schema()
    return bot_db.execute(
        "UPDATE team_task_matches SET description = COALESCE(%s, description), "
        "image = COALESCE(%s, image) WHERE id = %s",
        (description or None, image or None, int(match_id)),
    )


def pending_count():
    ensure_match_schema()
    rows = bot_db.query(
        "SELECT COUNT(*) AS c FROM team_task_matches WHERE status = 'pending'"
    )
    return rows[0]["c"] if rows else 0


def requeue_match(match_id, actor):
    """Повертає публікацію в чергу (pending), знімаючи зарахування.

    Саме це, а не reject, має відбуватись, коли зарахування знімають із
    картки таска: «це не сюди» ≠ «це взагалі не проєктне». Rejected —
    остаточне «ні», і публікація вже не поверталась би нікуди: ні в чергу
    (там лише pending), ні до судді (rejected входить у DECIDED). Пропозицію
    таска теж стираємо: таск міг бути помилковим і вже знятим.

    Повертає (матч, [id тасків, яких торкнулись])."""
    ensure_match_schema()
    current = get_match(match_id)
    if not current:
        return None, []
    rows = bot_db.query(
        f"UPDATE team_task_matches SET status = 'pending', task_id = NULL, "
        f"decided_by = %s, decided_at = now() WHERE id = %s RETURNING {_COLS}",
        (actor, int(match_id)),
    )
    if not rows:
        return None, []
    return _row_to_match(rows[0]), ([current["task_id"]] if current["task_id"] else [])


def decide_match(match_id, actor, action, task_id=None, person=None):
    """Рішення Каті по спірному матчу.

    action: confirm (зарахувати, можливо в іншу тематику — task_id),
            reject (не те — запис лишається, щоб не судити повторно).
    Повертає (матч, [id тасків, яких торкнулись]) — прогрес по них
    перераховує викликач."""
    ensure_match_schema()
    current = get_match(match_id)
    if not current:
        return None, []
    if action == "reject":
        status, new_task = "rejected", current["task_id"]
    else:
        status = "confirmed"
        new_task = int(task_id) if task_id else current["task_id"]
        if not new_task:
            return None, []
    rows = bot_db.query(
        f"UPDATE team_task_matches SET status = %s, task_id = %s, person = %s, "
        f"decided_by = %s, decided_at = now() WHERE id = %s RETURNING {_COLS}",
        (status, new_task, person or current["person"], actor, int(match_id)),
    )
    if not rows:
        return None, []
    touched = {t for t in (current["task_id"], new_task) if t}
    return _row_to_match(rows[0]), sorted(touched)


def stats():
    """Зведення для звітів прогонів: скільки чого."""
    ensure_match_schema()
    rows = bot_db.query(
        "SELECT status, COUNT(*) AS c FROM team_task_matches GROUP BY status"
    )
    return {r["status"]: r["c"] for r in rows}
