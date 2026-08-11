"""
Журнал переглядів умов — «Кабінет директора», крок 3.1 docs/COMPENSATION_ADVISOR.md.

Що це і чого тут свідомо немає
------------------------------
Картці перегляду потрібна рівно одна річ, якої бот не знає і знати не може:
**коли людині востаннє переглядали умови**. Зарплат немає ні в Норі, ні в базі
сайту, і класти їх туди ми домовились не класти — Нора відкривається з
телеграма.

Тому в таблиці немає і не буде колонок під суму, тип рішення («підвищення» /
«премія») чи відсоток. Це не забули додати — це рішення Олега 11.08: зберігаючи,
ЩО саме вирішили, Нора наполовину стає зарплатним реєстром, а «Не забудь про»
від цього не виграє нічого, бо там працює виключно ЧАС. Лишається один факт:
перегляд відбувся, ось дата, ось рядок контексту, який менеджер написав сам.

Якщо колись знадобиться сума — то саме діапазон/band, а не число, і окремим
рішенням, а не тихим ALTER TABLE.

Чому історія, а не одна дата на людину
--------------------------------------
Один рядок «останній перегляд» коштував би стільки ж, але «скільки разів
переглядали за два роки» тоді неможливо відповісти ніколи. Історія
безкоштовна: та сама таблиця, просто без UNIQUE. Заразом вона рятує від
помилкового тапу — запис видаляється, і попередній знову стає останнім.

Два різні «нічого немає»
------------------------
`last=None` означає **не засіяно** (історію ще не заводили), а не «давно не
переглядали». Різниця принципова для «Не забудь про»: людина без жодного
запису — це діра в даних, яку треба закрити руками один раз, а людина з
переглядом дворічної давнини — це вже сигнал. Плутати їх означало б показувати
всю редакцію як забуту в перший же день і привчити гортати повз список.
"""

import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from handlers import bot_db, team_roster

KYIV_TZ = ZoneInfo("Europe/Kiev")

# Від скількох місяців без перегляду людина потрапляє в «Не забудь про».
# Рік — не медичний норматив, а точка, після якої «ми ж нещодавно дивились»
# перестає бути правдою.
FORGOTTEN_MONTHS = 12

NOTE_MAX = 500

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_condition_reviews (
        id          BIGSERIAL PRIMARY KEY,
        person      TEXT NOT NULL,
        reviewed_on DATE NOT NULL,
        note        TEXT,
        created_by  TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_team_cond_person "
    "ON team_condition_reviews (person, reviewed_on DESC)",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_conditions_schema():
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


def months_between(then, now=None):
    """Скільки ПОВНИХ місяців минуло. Рахуємо по календарю, а не діленням днів
    на 30: «19 місяців» має збігатися з тим, що людина порахує пальцем по
    календарю, інакше цифра в картці виглядає як помилка."""
    now = now or _today()
    months = (now.year - then.year) * 12 + (now.month - then.month)
    if now.day < then.day:
        months -= 1
    return max(0, months)


def human_gap(months):
    """«19 місяців» / «2 роки 1 місяць» — довгі строки словами, бо «25 місяців»
    читається гірше, ніж «2 роки»."""
    if months < 1:
        return "цього місяця"
    if months < 12:
        return f"{months} {_plural(months, 'місяць', 'місяці', 'місяців')}"
    years, rest = divmod(months, 12)
    out = f"{years} {_plural(years, 'рік', 'роки', 'років')}"
    if rest:
        out += f" {rest} {_plural(rest, 'місяць', 'місяці', 'місяців')}"
    return out


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 100 in range(11, 15):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


def _row(r):
    return {
        "id": r["id"],
        "person": r["person"],
        "date": r["reviewed_on"].isoformat(),
        "note": r["note"] or "",
        "created_by": r["created_by"],
    }


def history(person):
    """Усі перегляди людини, свіжі зверху."""
    ensure_conditions_schema()
    return [_row(r) for r in bot_db.query(
        "SELECT id, person, reviewed_on, note, created_by FROM team_condition_reviews "
        "WHERE person = %s ORDER BY reviewed_on DESC, id DESC",
        (person,),
    )]


def _all_by_person():
    """{людина: [перегляди]} — одним запитом на весь екран."""
    ensure_conditions_schema()
    out = {}
    for r in bot_db.query(
        "SELECT id, person, reviewed_on, note, created_by FROM team_condition_reviews "
        "ORDER BY reviewed_on DESC, id DESC"
    ):
        out.setdefault(r["person"], []).append(_row(r))
    return out


def add_review(person, actor, reviewed_on=None, note=None):
    """Фіксує перегляд. `reviewed_on` у минулому — це засів історії, тому
    майбутні дати відсікаємо, а минулі приймаємо будь-які."""
    ensure_conditions_schema()
    if person not in team_roster.ROSTER:
        return None
    day = reviewed_on or _today()
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return None
    if day > _today():
        day = _today()
    rows = bot_db.query(
        "INSERT INTO team_condition_reviews (person, reviewed_on, note, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "RETURNING id, person, reviewed_on, note, created_by",
        (person, day, (note or "").strip()[:NOTE_MAX] or None, actor),
    )
    return _row(rows[0]) if rows else None


def delete_review(review_id):
    """Прибрати помилковий запис — попередній знову стає останнім."""
    ensure_conditions_schema()
    return bot_db.execute(
        "DELETE FROM team_condition_reviews WHERE id = %s", (int(review_id),))


def payload():
    """Усе для кабінету одним запитом.

    `people` — вся редакція в порядку ростера з останнім переглядом; менеджери
    теж (Олег переглядає умови й Каті, і Олені — вони не поза редакцією).

    `attention` — «Не забудь про», два РІЗНІ приводи в одному списку і кожен
    підписаний своїм: `never` (жодного запису — діру треба закрити руками) і
    `stale` (перегляд давніший за поріг). Спершу stale і найдавніші згори:
    ненасіяні — робота на пів години, а забута людина — це те, заради чого
    список існує."""
    ensure_conditions_schema()
    by_person = _all_by_person()
    today = _today()

    people, attention = [], []
    for name, info in team_roster.ROSTER.items():
        items = by_person.get(name) or []
        last = items[0] if items else None
        months = months_between(date.fromisoformat(last["date"]), today) if last else None
        row = {
            "person": name,
            "dept": info["dept"],
            "dept_title": team_roster.DEPT_TITLES.get(info["dept"], info["dept"]),
            "manager": info["manager"],
            "last": last,
            "months": months,
            "gap": human_gap(months) if months is not None else None,
            "count": len(items),
        }
        people.append(row)
        if last is None:
            attention.append({**row, "reason": "never"})
        elif months >= FORGOTTEN_MONTHS:
            attention.append({**row, "reason": "stale"})

    # stale першими, всередині — найдавніші згори; ненасіяні в кінці
    attention.sort(key=lambda r: (r["reason"] == "never", -(r["months"] or 0), r["person"]))
    return {
        "people": people,
        "attention": attention,
        "threshold": FORGOTTEN_MONTHS,
        "seeded": sum(1 for p in people if p["last"]),
        "total": len(people),
        "note_max": NOTE_MAX,
        "today": today.isoformat(),
    }
