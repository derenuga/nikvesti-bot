"""
Квартальна оцінка редактора — крок 2 спеки docs/COMPENSATION_ADVISOR.md.

Навіщо це окремо від KPI
------------------------
KPI рахує ОБСЯГ і рахує його сам із nodes. Обсяг — поріг, а не вісь: норма
виконана — заходиш у розмову про гроші, не виконана — розмова інша. Але
відрізнити людину, яка закрила норму рерайтами, від тієї, що закрила її
власними матеріалами й ще й у строк, жоден лічильник не може. Це знає рівно
одна людина — редакторка. Тому єдиний спосіб дістати якість — спитати її, і
зробити це так дешево, щоб вона реально відповідала.

Чому три рівні і ЖОДНОГО бала
-----------------------------
У кожного виміру ВЛАСНІ слова, а не спільна шкала 1–3. «Приносить теми сама» і
«Завжди в строк» неможливо додати одне до одного й поділити на два — навіть
якщо дуже захотіти. Якби рівні були числами, перше, що зробив би будь-хто
(зокрема майбутня сесія), — вивів би «середній бал якості», а це рівно той
єдиний бал, який спека забороняє: він одразу став би рейтингом людей, а рейтинг
руйнує співпрацю (Microsoft, stack ranking).

З тієї ж причини тут немає жодної функції, яка повертає число по людині.
Споживач (картка перегляду, крок 3) отримує три СЛОВА і рядок тексту.

Порядок варіантів усередині виміру — від «потребує менше уваги редактора» до
«потребує більше». Це НЕ шкала якості й не має нею ставати.

Чому вимірів три, а не чотири, як у спеці
------------------------------------------
Четвертим спека називала «складність (стрічка / власні / розслідування)».
Обидва його краї відпали, кожен зі своєї причини (Олег, 11.08):

1. **«Розслідування» — не наш жанр.** МикВісті — watchdog, а не інвестигативна
   редакція. Лишати варіант, якого не буває, означає змусити редакторку
   обирати між вигадкою і тим, що людина реально робить.
2. **«Власні / стрічка» бот рахує САМ.** Це `own_material` у `nodes`, і KPI
   вже показує «+22 новини в стрічку» у Creative та «4 власні 🔥» у Newsroom
   (team_kpi.feed_news_counts / own_news_counts). Питати про це редакторку —
   витрачати ті самі 30 секунд на те, що вже лежить у базі, ще й отримувати
   гірший результат: у базі точне співвідношення за квартал, а на екрані був
   би чип «переважно».

Звідси правило, суворіше за початкове: **в анкеті лишається рівно те, чого
жоден лічильник не бачить.** Три виміри — це не «скоротили заради простоти», а
рівно ті питання, на які може відповісти тільки людина. Розкрій власні/стрічка
нікуди не подівся — він приїде в картку перегляду цифрами з KPI, безкоштовно.

Побічний наслідок: Діджитал перестав бути винятком. Уся його непридатність до
анкети йшла саме від четвертого виміру, привʼязаного до `nodes` — у Сергія
(оператор) і Іміри (Instagram) рядків там немає взагалі. Точність,
самостійність і строк застосовуються до їхньої роботи так само, як до тексту,
тож окремої скороченої анкети для відділу не потрібно.

Що можна лишити порожнім
------------------------
Будь-який вимір. Якщо редакторка не має думки — вона має мати змогу не
відповісти, інакше вона натисне будь-що, аби пройти екран, і дані стануть
гіршими за відсутні. Порожнє значення живе як NULL і в картці читається
«не питали», а не «погано».

Періоди — квартали за Києвом. Оцінка ідемпотентна (PRIMARY KEY person+period):
Катя може повернутись і виправити, поки квартал не закрито, і це не плодить
рядків. Оцінка і гроші РОЗВЕДЕНІ В ЧАСІ свідомо (спека, §«Оцінку і гроші
розводять у часі»): якщо редакторка знає, що низька оцінка сьогодні коштує
людині премії завтра, оцінки надуваються.
"""

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from handlers import bot_db, team_roster

KYIV_TZ = ZoneInfo("Europe/Kiev")

# Вимір = (ключ, заголовок, підказка, [(значення, підпис), …]).
# Підписи — це і є «три рівні»: у кожного виміру свої, спільної шкали немає.
DIMENSIONS = (
    {
        "key": "accuracy",
        "title": "Точність",
        "hint": "правки після здачі",
        "levels": (
            ("clean", "Майже без правок"),
            ("normal", "Звичайні правки"),
            ("heavy", "Багато правок"),
        ),
    },
    {
        "key": "initiative",
        "title": "Самостійність",
        "hint": "хто знаходить тему",
        "levels": (
            ("brings", "Приносить теми"),
            ("picks", "Підхоплює"),
            ("needs", "Треба ставити"),
        ),
    },
    {
        "key": "reliability",
        "title": "Надійність у строк",
        "hint": "як тримає дедлайни",
        "levels": (
            ("always", "Завжди в строк"),
            ("slips", "Іноді зсуває"),
            ("watch", "Треба контролювати"),
        ),
    },
)

DIMENSION_KEYS = tuple(d["key"] for d in DIMENSIONS)
_ALLOWED = {d["key"]: {v for v, _ in d["levels"]} for d in DIMENSIONS}
_LABELS = {d["key"]: dict(d["levels"]) for d in DIMENSIONS}

NOTE_MAX = 400
ROMAN = ("I", "II", "III", "IV")

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_quality_reviews (
        person      TEXT NOT NULL,
        period      TEXT NOT NULL,
        accuracy    TEXT,
        initiative  TEXT,
        reliability TEXT,
        note        TEXT,
        created_by  TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (person, period)
    )
    """,
    # Картка перегляду (крок 3) читає «усі квартали однієї людини»
    "CREATE INDEX IF NOT EXISTS idx_team_reviews_person "
    "ON team_quality_reviews (person, period DESC)",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_reviews_schema():
    """Під блокуванням і в одній сесії — як team_tasks/team_kpi: гонка перших
    паралельних запитів плюс з'єднання на кожен statement."""
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


# ---------- Квартали (Київ) ----------

def current_quarter(offset=0, today=None):
    """(рік, квартал 1–4) із зсувом на `offset` кварталів назад/вперед.
    Зсуваємо по наскрізному індексу, щоб не ловити переходи року."""
    today = today or datetime.now(KYIV_TZ).date()
    idx = today.year * 4 + (today.month - 1) // 3 + offset
    return idx // 4, idx % 4 + 1


def quarter_key(year, quarter):
    """Ключ періоду в базі: '2026-Q3'. Сортується лексикографічно як хронологічно."""
    return f"{year}-Q{quarter}"


def quarter_label(year, quarter):
    return f"{ROMAN[quarter - 1]} квартал {year}"


def quarter_bounds(year, quarter):
    """(перший день, перший день наступного) — для підпису «за що оцінюємо»."""
    start = date(year, (quarter - 1) * 3 + 1, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, quarter * 3 + 1, 1)
    return start, end


def _period_meta(offset=0):
    year, quarter = current_quarter(offset)
    start, end = quarter_bounds(year, quarter)
    return {
        "period": quarter_key(year, quarter),
        "label": quarter_label(year, quarter),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "is_current": offset == 0,
    }


# ---------- Читання ----------

def _row_to_review(r):
    """Рядок бази → оцінка з ГОТОВИМИ підписами. Підписи резолвляться тут, а не
    у фронті: інакше словник рівнів жив би двома копіями і розійшовся б при
    першій же зміні формулювання."""
    values = {k: r.get(k) for k in DIMENSION_KEYS}
    return {
        "person": r["person"],
        "period": r["period"],
        "values": values,
        "labels": {k: _LABELS[k].get(v) for k, v in values.items()},
        "note": r.get("note") or "",
        "filled": sum(1 for v in values.values() if v),
        "created_by": r.get("created_by"),
        "updated_at": r["updated_at"].astimezone(KYIV_TZ).isoformat()
                      if r.get("updated_at") else None,
    }


def list_reviews(period):
    """{людина: оцінка} за квартал."""
    ensure_reviews_schema()
    cols = ", ".join(DIMENSION_KEYS)
    rows = bot_db.query(
        f"SELECT person, period, {cols}, note, created_by, updated_at "
        f"FROM team_quality_reviews WHERE period = %s",
        (period,),
    )
    return {r["person"]: _row_to_review(r) for r in rows}


def person_history(person, quarters=4):
    """Останні `quarters` кварталів однієї людини, свіжі зверху.

    Це і є вхід для картки перегляду (крок 3) — вона питає «що змінилось за
    пів року», тобто їй потрібні щонайменше два квартали поспіль."""
    ensure_reviews_schema()
    keys = [quarter_key(*current_quarter(-i)) for i in range(quarters)]
    cols = ", ".join(DIMENSION_KEYS)
    rows = bot_db.query(
        f"SELECT person, period, {cols}, note, created_by, updated_at "
        f"FROM team_quality_reviews WHERE person = %s AND period = ANY(%s) "
        f"ORDER BY period DESC",
        (person, keys),
    )
    return [_row_to_review(r) for r in rows]


def review_payload(offset=0):
    """Усе, що треба екрану оцінки, одним запитом.

    Люди — НЕ менеджери (Олега й Катю не оцінюють) у порядку ростера, з
    відділом. Поруч із поточним кварталом їде ПОПЕРЕДНІЙ: спека просить рядок
    «що змінилось», а сказати, що змінилось, неможливо, не бачачи попереднього.

    `done`/`total` — не заради прогрес-бара: без них редакторка не знає, чи
    вона вже пройшла всіх, а екран на 15 людей гортається довше, ніж памʼять
    про те, на кому зупинилась."""
    ensure_reviews_schema()
    now = _period_meta(offset)
    prev = _period_meta(offset - 1)
    current = list_reviews(now["period"])
    previous = list_reviews(prev["period"])
    depts = team_roster.dept_overrides()

    people = []
    for name, info in team_roster.ROSTER.items():
        if info["manager"]:
            continue
        dept = team_roster.effective_dept(name, depts)
        people.append({
            "person": name,
            "dept": dept,
            "dept_title": team_roster.DEPT_TITLES.get(dept, dept),
            "review": current.get(name),
            "previous": previous.get(name),
        })

    return {
        "dimensions": [
            {"key": d["key"], "title": d["title"], "hint": d["hint"],
             "levels": [{"value": v, "label": t} for v, t in d["levels"]]}
            for d in DIMENSIONS
        ],
        "period": now,
        "previous_period": prev,
        "people": people,
        "done": sum(1 for p in people if p["review"] and p["review"]["filled"]),
        "total": len(people),
        "note_max": NOTE_MAX,
    }


# ---------- Запис ----------

def save_review(person, actor, offset=0, note=None, **dimensions):
    """Зберігає/оновлює оцінку людини за квартал.

    Значення поза словником виміру ІГНОРУЄТЬСЯ мовчки, а не падає: єдине
    джерело таких значень — застарілий фронт після зміни формулювань, і
    ронити збереження всієї картки через один невпізнаний чип не варто.
    Порожній рядок — це усвідомлене «зняти відповідь», тому він чистить
    значення в NULL, а не пропускається.

    `note=None` не чіпає нотатку взагалі (екран міг надіслати лише чипи)."""
    ensure_reviews_schema()
    if person not in team_roster.ROSTER or team_roster.ROSTER[person]["manager"]:
        return None
    period = _period_meta(offset)["period"]

    cols, values = [], []
    for key in DIMENSION_KEYS:
        if key not in dimensions:
            continue
        raw = dimensions[key]
        if raw is None or raw == "":
            cols.append(key)
            values.append(None)
        elif raw in _ALLOWED[key]:
            cols.append(key)
            values.append(raw)
    if note is not None:
        cols.append("note")
        values.append(note.strip()[:NOTE_MAX] or None)
    if not cols:
        return None

    insert_cols = ", ".join(["person", "period", "created_by"] + cols)
    placeholders = ", ".join(["%s"] * (3 + len(cols)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    all_cols = ", ".join(DIMENSION_KEYS)
    rows = bot_db.query(
        f"INSERT INTO team_quality_reviews ({insert_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (person, period) DO UPDATE SET {updates}, "
        f"created_by = EXCLUDED.created_by, updated_at = now() "
        f"RETURNING person, period, {all_cols}, note, created_by, updated_at",
        (person, period, actor, *values),
    )
    return _row_to_review(rows[0]) if rows else None


def clear_review(person, offset=0):
    """Прибрати оцінку цілком — «я поставила не тій людині»."""
    ensure_reviews_schema()
    return bot_db.execute(
        "DELETE FROM team_quality_reviews WHERE person = %s AND period = %s",
        (person, _period_meta(offset)["period"]),
    )
