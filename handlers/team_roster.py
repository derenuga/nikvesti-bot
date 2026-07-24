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

from handlers import bot_db

# Відділи (KPI-групи). Слаг = ключ для kpi_defs у хвилі 2.
DEPT_LEADERSHIP = "керівництво"
DEPT_JOURNALISTS = "журналістика"
DEPT_NEWSFEED = "стрічка"
DEPT_SOCIAL = "соцмережі"
DEPT_VIDEO = "відео"
DEPT_TRANSLATION = "переклад"

DEPT_TITLES = {
    DEPT_LEADERSHIP: "Керівництво",
    DEPT_JOURNALISTS: "Журналістика",
    DEPT_NEWSFEED: "Стрічка новин",
    DEPT_SOCIAL: "Соцмережі",
    DEPT_VIDEO: "Відео і фото",
    DEPT_TRANSLATION: "Англійська версія",
}

# Ключі — канонічні імена, ті самі, що в TEAM (ai_messages.py). Тримати синхронно.
# username — БЕЗ @, у нижньому регістрі (так порівнюємо з initData).
# tg_id — заповнено лише де відоме заздалегідь; решта докешується при
# першому вході в апку (див. remember_user).
ROSTER = {
    "Олег Деренюга": {"username": "derenuga", "tg_id": 56631818, "dept": DEPT_LEADERSHIP, "manager": True},
    "Катерина Середа": {"username": "sereda_ka", "tg_id": 56424866, "dept": DEPT_LEADERSHIP, "manager": True},
    "Юлія Бойченко": {"username": "boichenko13", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Аліса Мелікадамян": {"username": "lislislisalisa", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Світлана Іванченко": {"username": "lana_prpolka", "tg_id": None, "dept": DEPT_NEWSFEED, "manager": False},
    "Альона Коханчук": {"username": "aliona_banu", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Аліна Квітко": {"username": "aliniskv", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Марія Хаміцевич": {"username": None, "tg_id": 846178524, "dept": DEPT_NEWSFEED, "manager": False},
    "Сергій Овчаришин": {"username": None, "tg_id": 891685789, "dept": DEPT_VIDEO, "manager": False},
    "Єлизавета Москвіна": {"username": "mskvn1", "tg_id": 386403807, "dept": DEPT_SOCIAL, "manager": False},
    "Іміра Борухова": {"username": "imira_91", "tg_id": None, "dept": DEPT_SOCIAL, "manager": False},
    "Даріна Мельничук": {"username": "dariimlk", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Юлія Лук'яненко": {"username": "yuliia_lukianenko", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Таміла Ксьонжик": {"username": "tamilissssa", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Кристина Леонова": {"username": "skxxlw", "tg_id": None, "dept": DEPT_JOURNALISTS, "manager": False},
    "Кирил Витвицький": {"username": "simada24", "tg_id": None, "dept": DEPT_VIDEO, "manager": False},
    "Ірина Федорович": {"username": "diiessa", "tg_id": None, "dept": DEPT_TRANSLATION, "manager": False},
}

_BY_USERNAME = {info["username"]: name for name, info in ROSTER.items() if info["username"]}
_BY_KNOWN_ID = {info["tg_id"]: name for name, info in ROSTER.items() if info["tg_id"]}


def person_info(name):
    """Картка людини з ростера або None."""
    return ROSTER.get(name)


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


def remember_user(tg_id, username, person):
    """Кешує tg_id ↔ людина при вході в апку — далі пінги від Лиса
    знаходять її без повторного матчингу по username."""
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
