"""
Ростер команди для Mini App «Команда» (таски + KPI, хвиля 1).

Джерело правди про людей — TEAM в ai_messages.py (імена/ролі/хендли).
Тут — НАДБУДОВА для апки: відділ (KPI рахуються по відділах — рішення Олега,
24.07.2026), прапорець менеджера (хто ставить таски: Олег і головред) і
резолв «хто прийшов у апку» за підписаними даними Telegram initData.

Проблема ідентифікації: Telegram у initData дає numeric id + username, а в
TEAM — лише @хендли (numeric id відомі тільки для двох людей без username).
Тому резолв триступеневий:
  1. кеш team_users у Норі (людина вже відкривала апку — id запам'ятовано);
  2. відомі numeric id з ростера;
  3. збіг username (без регістру) з хендлом ростера → одразу кешуємо id.
Кеш id потрібен не лише для швидкості: ПІНГИ від Лиса (нове завдання,
прийнято роботу) шлються за numeric id, якого до першого входу немає.
"""

import time

from handlers import bot_db

# Відділи (KPI-групи). Реальна структура редакції (Олег, 27.07):
# Creative — власні матеріали, Newsroom — стрічка, Діджитал та дистрибуція —
# SMM/відео/переклад; Адміністративний — Олег і Катя (менеджери; їхні KPI —
# «може колись», у виборі виконавиць і нормах відділ не бере участі).
DEPT_ADMIN = "admin"
DEPT_NEWSROOM = "newsroom"
DEPT_CREATIVE = "creative"
DEPT_DIGITAL = "digital"

# Відділи, доступні для перенесення людей і KPI-норм (без адміністративного)
MOVABLE_DEPTS = (DEPT_NEWSROOM, DEPT_CREATIVE, DEPT_DIGITAL)

DEPT_TITLES = {
    DEPT_ADMIN: "Адміністративний",
    DEPT_NEWSROOM: "Newsroom",
    DEPT_CREATIVE: "Creative",
    DEPT_DIGITAL: "Діджитал та дистрибуція",
}

# Ключі — канонічні імена, ті самі, що в TEAM (ai_messages.py). Тримати синхронно.
# username — БЕЗ @, у нижньому регістрі (так порівнюємо з initData).
# tg_id — заповнено лише де відоме заздалегідь; решта докешується при
# першому вході в апку (див. remember_user).
ROSTER = {
    "Олег Деренюга": {"username": "derenuga", "tg_id": 56631818, "dept": DEPT_ADMIN, "manager": True},
    "Катерина Середа": {"username": "sereda_ka", "tg_id": 56424866, "dept": DEPT_ADMIN, "manager": True},
    # Фінансова менеджерка (Олег, 27.07): відповідає за фінансові звіти
    # грантів. Менеджерка — щоб бачити «Звітність»; у KPI-нормах і виборі
    # виконавиць адміністративний відділ участі не бере.
    "Олена Бондаренко": {"username": None, "tg_id": 191642941, "dept": DEPT_ADMIN, "manager": True},
    # Розклад по відділах — списки Олега 27.07 (Creative — власні матеріали,
    # digital — діджитал та дистрибуція). Дефолт тут, фактичний відділ —
    # team_dept у Норі (Катя переносить в апці).
    "Юлія Бойченко": {"username": "boichenko13", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Аліса Мелікадамян": {"username": "lislislisalisa", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Світлана Іванченко": {"username": "lana_prpolka", "tg_id": None, "dept": DEPT_NEWSROOM, "manager": False},
    "Альона Коханчук": {"username": "aliona_banu", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Аліна Квітко": {"username": "aliniskv", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Марія Хаміцевич": {"username": None, "tg_id": 846178524, "dept": DEPT_NEWSROOM, "manager": False},
    "Сергій Овчаришин": {"username": None, "tg_id": 891685789, "dept": DEPT_DIGITAL, "manager": False},
    "Єлизавета Москвіна": {"username": "mskvn1", "tg_id": 386403807, "dept": DEPT_DIGITAL, "manager": False},
    "Іміра Борухова": {"username": "imira_91", "tg_id": None, "dept": DEPT_DIGITAL, "manager": False},
    "Даріна Мельничук": {"username": "dariimlk", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Юлія Лук'яненко": {"username": "yuliia_lukianenko", "tg_id": None, "dept": DEPT_CREATIVE, "manager": False},
    "Таміла Ксьонжик": {"username": "tamilissssa", "tg_id": None, "dept": DEPT_NEWSROOM, "manager": False},
    "Кристина Леонова": {"username": "skxxlw", "tg_id": None, "dept": DEPT_NEWSROOM, "manager": False},
    "Кірілл Витвицький": {"username": "simada24", "tg_id": None, "dept": DEPT_DIGITAL, "manager": False},
    "Ірина Федорович": {"username": "diiessa", "tg_id": None, "dept": DEPT_DIGITAL, "manager": False},
}

# Троттл запису team_users: (tg_id, людина) -> monotonic останнього запису.
# Росте максимум до розміру ростера, чиститись нема чого.
REMEMBER_TTL = 3600
_remembered = {}

_BY_USERNAME = {info["username"]: name for name, info in ROSTER.items() if info["username"]}
_BY_KNOWN_ID = {info["tg_id"]: name for name, info in ROSTER.items() if info["tg_id"]}


def person_info(name):
    """Картка людини з ростера або None."""
    return ROSTER.get(name)


def dept_overrides():
    """{людина: відділ} з Нори (team_dept) — перекриття, які Катя ставить в
    апці. Нора недоступна → порожньо, діють дефолти ростера. Блокуючий."""
    try:
        from handlers import team_tasks
        team_tasks.ensure_team_schema()
        return {
            r["person"]: r["dept"]
            for r in bot_db.query("SELECT person, dept FROM team_dept")
            if r["person"] in ROSTER
        }
    except Exception as e:
        print(f"team_roster: team_dept недоступна — дефолти ростера ({e})")
        return {}


def effective_dept(person, overrides=None):
    """Фактичний відділ людини: перекриття з Нори або дефолт ростера.
    overrides — вже прочитаний dept_overrides(), щоб не смикати БД у циклі."""
    info = ROSTER.get(person)
    if not info:
        return None
    if info["manager"]:
        return info["dept"]  # менеджерів між відділами не носимо
    o = overrides if overrides is not None else dept_overrides()
    return o.get(person, info["dept"])


def set_dept(person, dept, updated_by):
    """Переносить людину у відділ (upsert перекриття). Блокуючий."""
    from handlers import team_tasks
    team_tasks.ensure_team_schema()
    bot_db.execute(
        """
        INSERT INTO team_dept (person, dept, updated_by, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (person) DO UPDATE SET
            dept = EXCLUDED.dept, updated_by = EXCLUDED.updated_by, updated_at = now()
        """,
        (person, dept, updated_by),
    )


def is_manager(name):
    info = ROSTER.get(name)
    return bool(info and info["manager"])


def resolve_person(tg_id, username):
    """Хто це з команди: канонічне ім'я з ростера або None (чужинець).
    Порядок: кеш Нори → відомі id → username. Блокуючий (БД) — кликати
    через asyncio.to_thread з async-коду."""
    if tg_id in _BY_KNOWN_ID:
        return _BY_KNOWN_ID[tg_id]
    try:
        rows = bot_db.query(
            "SELECT person FROM team_users WHERE tg_id = %s", (int(tg_id),)
        )
        if rows and rows[0]["person"] in ROSTER:
            return rows[0]["person"]
    except Exception as e:
        print(f"team_roster: кеш team_users недоступний — {e}")
    if username:
        return _BY_USERNAME.get(username.lower().lstrip("@"))
    return None


def remember_user(tg_id, username, person, force=False):
    """Кешує tg_id ↔ людина при вході в апку — далі пінги від Лиса
    знаходять її без повторного матчингу по username.

    Викликається з авторизації, тобто на КОЖЕН запит апки — а один екран це
    кілька запитів. Писати щоразу нема сенсу: рядок той самий, міняється
    хіба last_seen. Тому не частіше ніж раз на REMEMBER_TTL на людину;
    кеш у пам'яті процесу, після рестарту перший запит знову пише."""
    key = (int(tg_id), person)
    now = time.monotonic()
    if not force and now - _remembered.get(key, float("-inf")) < REMEMBER_TTL:
        return

    from handlers import team_tasks  # ensure схеми team_* живе там

    team_tasks.ensure_team_schema()
    bot_db.execute(
        """
        INSERT INTO team_users (tg_id, username, person, first_seen, last_seen)
        VALUES (%s, %s, %s, now(), now())
        ON CONFLICT (tg_id) DO UPDATE SET
            username = EXCLUDED.username,
            person = EXCLUDED.person,
            last_seen = now()
        """,
        (int(tg_id), (username or "").lower() or None, person),
    )
    _remembered[key] = now


def tg_id_for(person):
    """Numeric id людини для пінгів: відомий з ростера або з кешу першого
    входу. None — людина ще не відкривала апку і id невідомий."""
    info = ROSTER.get(person)
    if info and info["tg_id"]:
        return info["tg_id"]
    try:
        rows = bot_db.query(
            "SELECT tg_id FROM team_users WHERE person = %s ORDER BY last_seen DESC LIMIT 1",
            (person,),
        )
        return int(rows[0]["tg_id"]) if rows else None
    except Exception as e:
        print(f"team_roster: tg_id_for({person}) — {e}")
        return None
