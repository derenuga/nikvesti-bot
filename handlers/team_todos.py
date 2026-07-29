"""
Блокнот — особистий список справ (Mini App «Команда» + команда /todo у боті).

Олег, 29.07: «блокнот to-do, адмінам теж його. Але щоб /todo у боті теж
записувало задачу. І почитай, які ще існують найкращі практики todo-лістів,
чому одні ефективні, а інші ні, щоб реалізувати за найкращим прототипом».

Що каже дослідження і що з цього випливає для КОДУ
---------------------------------------------------

1. **41% пунктів не роблять ніколи; 50% зроблених закривають у той самий день,
   18% — за годину, 10% — за хвилину** (дані Todoist/iDoneThis). Висновок не
   «люди ліниві», а «якщо річ не зробили сьогодні, найімовірніше не зроблять
   узагалі». Тому:
   - ЗАХОПЛЕННЯ має бути миттєвим — поле зверху, Enter додає і ЛИШАЄ фокус;
     плюс /todo у чаті, бо думка приходить у чаті, а не в апці;
   - вік пункту видно («з учора», «3 дні»), без червоного і без докорів: це
     інформація для рішення «роблю чи викидаю», а не оцінка людини.

2. **Ефект Зейгарнік і Masicampo & Baumeister (2011), «Consider it done!»:**
   незавершене вантажить память НЕ доки не зроблене, а доки не СПЛАНОВАНЕ.
   Достатньо конкретного плану — і нав'язливість зникає. Тому в блокноті два
   кошики, «Сьогодні» і «Потім», і перенесення в один тап: це і є той
   мінімальний план. Список без кошиків не знімає вантаж, а додає.

3. **Implementation intentions (Gollwitzer & Sheeran, d = 0.65 на 94 тестах;
   642 тести у 2024-му):** намір «коли/де» приблизно подвоює шанс, що дію
   виконають. Повний if-then-план — це поле, яке ніхто не заповнить; «сьогодні
   чи потім» — найдешевша його форма, яка ще працює.

4. **Метод Айві Лі (шість справ на день, у порядку) і правило 1-3-5:** сила не
   в порядку, а в ЛІМІТІ. Тому на «Сьогодні» стоїть мʼякий поріг TODAY_SOFT_MAX:
   не забороняємо, але кажемо вголос. Довгий денний список — головна причина,
   чому в нього перестають заглядати.

5. **Принцип прогресу (Amabile & Kramer, 12 000 щоденників):** найсильніший
   драйвер — видимий рух уперед, навіть маленький. Тому зроблене не зникає
   мовчки: «Зроблено сьогодні · 4» лишається на екрані до кінця дня.

Чого свідомо НЕМАЄ (і це рішення, а не недоробка)
--------------------------------------------------
- **Пріоритетів.** Три рівні важливості перетворюються на «все важливе» за
  тиждень (priority inflation), а кошик «Сьогодні» вже і є пріоритетом.
- **Дедлайнів і нагадувань.** Для зобовʼязань перед редакцією є creative tasks
  зі строком і пінгом. Блокнот — це те, що людина записала СОБІ; дедлайн
  перетворив би його на другу систему обліку, і в неї перестали б писати.
- **Підзадач, проєктів, тегів.** Кожен рівень вкладеності — це рішення, яке
  треба ухвалити на захопленні, тобто плата саме там, де має бути безкоштовно.
- **Спільних списків.** Блокнот приватний: його не бачить ні редактор, ні
  менеджер, і на KPI він не впливає. Інакше це вже не блокнот, а звітність.
"""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from handlers import bot_db

KYIV_TZ = ZoneInfo("Europe/Kiev")

BUCKETS = ("today", "later")
# Мʼякий поріг денного списку (Айві Лі — шість справ). Не забороняє, а каже.
TODAY_SOFT_MAX = 6
# Скільки днів пункт може висіти в «Потім», перш ніж ми тихо спитаємо про нього
STALE_DAYS = 21
# Скільки закритих показуємо (решта просто історія, її ніхто не гортає)
DONE_LIMIT = 20

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_todos (
        id         BIGSERIAL PRIMARY KEY,
        person     TEXT NOT NULL,
        text       TEXT NOT NULL,
        bucket     TEXT NOT NULL DEFAULT 'later',
        done_at    TIMESTAMPTZ,
        planned_at DATE,
        source     TEXT NOT NULL DEFAULT 'app',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Читаємо завжди «усе моє відкрите» — індекс по людині й стану
    "CREATE INDEX IF NOT EXISTS idx_team_todos_person "
    "ON team_todos (person, done_at)",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_todos_schema():
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


def _today():
    return datetime.now(KYIV_TZ).date()


def _row(r):
    """Рядок для апки. age — скільки днів пункт уже чекає; для «Сьогодні» це
    вік ПЛАНУ (скільки разів його переносили з дня в день), для «Потім» — вік
    самого запису."""
    today = _today()
    created = r["created_at"].astimezone(KYIV_TZ).date() if r["created_at"] else today
    planned = r["planned_at"] or created
    base = planned if r["bucket"] == "today" else created
    return {
        "id": r["id"],
        "text": r["text"],
        "bucket": r["bucket"],
        "done": r["done_at"] is not None,
        "done_today": bool(r["done_at"]
                           and r["done_at"].astimezone(KYIV_TZ).date() == today),
        "age": max(0, (today - base).days),
        "stale": r["bucket"] == "later" and (today - created).days >= STALE_DAYS,
        "source": r["source"],
    }


def list_todos(person):
    """{today: [...], later: [...], done: [...], done_today: N}."""
    ensure_todos_schema()
    rows = [_row(r) for r in bot_db.query(
        "SELECT id, text, bucket, done_at, planned_at, source, created_at "
        "FROM team_todos WHERE person = %s "
        # відкриті — старіші зверху (те, що висить довше, має бути на очах),
        # закриті — свіжіші зверху
        "ORDER BY done_at DESC NULLS FIRST, id ASC",
        (person,),
    )]
    open_rows = [r for r in rows if not r["done"]]
    done_rows = [r for r in rows if r["done"]]
    return {
        "today": [r for r in open_rows if r["bucket"] == "today"],
        "later": [r for r in open_rows if r["bucket"] == "later"],
        "done": done_rows[:DONE_LIMIT],
        "done_today": sum(1 for r in done_rows if r["done_today"]),
        "soft_max": TODAY_SOFT_MAX,
    }


def add_todo(person, text, bucket="later", source="app"):
    """Додає пункт. Порожній текст ігноруємо мовчки — це промах по Enter."""
    ensure_todos_schema()
    text = (text or "").strip()[:300]
    if not text:
        return None
    if bucket not in BUCKETS:
        bucket = "later"
    rows = bot_db.query(
        "INSERT INTO team_todos (person, text, bucket, planned_at, source) "
        "VALUES (%s, %s, %s, %s, %s) "
        "RETURNING id, text, bucket, done_at, planned_at, source, created_at",
        (person, text, bucket, _today() if bucket == "today" else None, source),
    )
    return _row(rows[0]) if rows else None


def update_todo(person, todo_id, bucket=None, done=None, text=None):
    """Перенести між кошиками / закрити / повернути / перейменувати.

    person у WHERE не для краси: без нього чужий id у запиті редагував би
    чужий блокнот, а блокнот тут приватний."""
    ensure_todos_schema()
    sets, params = [], []
    if bucket in BUCKETS:
        sets.append("bucket = %s")
        params.append(bucket)
        # Перенесення в «Сьогодні» перезапускає вік плану: людина щойно
        # вирішила робити це саме сьогодні, і «з учора» тут було б брехнею
        sets.append("planned_at = %s")
        params.append(_today() if bucket == "today" else None)
    if done is not None:
        sets.append("done_at = now()" if done else "done_at = NULL")
    if text is not None and text.strip():
        sets.append("text = %s")
        params.append(text.strip()[:300])
    if not sets:
        return None
    params += [person, int(todo_id)]
    rows = bot_db.query(
        f"UPDATE team_todos SET {', '.join(sets)} WHERE person = %s AND id = %s "
        "RETURNING id, text, bucket, done_at, planned_at, source, created_at",
        tuple(params),
    )
    return _row(rows[0]) if rows else None


def delete_todo(person, todo_id):
    ensure_todos_schema()
    return bot_db.execute(
        "DELETE FROM team_todos WHERE person = %s AND id = %s",
        (person, int(todo_id)),
    )


def summary(person):
    """Короткий підсумок для відповіді бота: (сьогодні, потім, зроблено сьогодні)."""
    data = list_todos(person)
    return len(data["today"]), len(data["later"]), data["done_today"]
