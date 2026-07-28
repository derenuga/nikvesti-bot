"""
Тест аналітичного дашборда Mini App («Аналітика» на Головній, handlers/team_analytics.py).

Живих БД не потрібно: підміняємо bot_db.query / db.query і перевіряємо саму
логіку, у якій легко помилитись і важко помітити помилку на очі —
арифметику періодів і порівнянь.

Що стереже:
- ЗАВЕРШЕНИЙ період порівнюється цілком, НЕПОВНИЙ — з тією самою кількістю днів
  попереднього (інакше у вівторок дашборд щоразу малював би −70%);
- дані беруться не далі ніж «вчора» (захват о 09:00 пише вчорашній день);
- тиждень, який ще не має жодного повного дня, віддає порожній payload, а не помилку;
- суми й дельти сайту рахуються по своїх відрізках, NULL-и не ламають суму;
- частки типів пошуку рахуються від суми кліків періоду;
- вихід редакції розкладається з ОДНОГО запиту з колонкою-бакетом cur;
- топ матеріалів складається з денних топ-5 (сума по шляху) і чистить «| МикВісті»;
- підписники соцмереж — останній знімок ≤ кінця періоду, приріст — до знімка
  перед періодом;
- зараховане виконання групується по донорах;
- збій будь-якого джерела гасить СВІЙ блок, а не весь екран;
- payload кешується на (period, offset).

Запуск:
    python test_team_analytics.py
"""

import sys
from datetime import date

from handlers import team_analytics as ta

FAILS = []


def check(name, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {name}{'' if cond else ' — ' + str(extra)}")
    if not cond:
        FAILS.append(name)


TODAY = date(2026, 7, 29)          # середа
WEEK_DONE = ta._ranges("week", -1, today=TODAY)   # 20–26.07, завершений
WEEK_NOW = ta._ranges("week", 0, today=TODAY)     # 27.07–02.08, триває
MONTH_NOW = ta._ranges("month", 0, today=TODAY)


# ---------- Періоди ----------

def test_ranges():
    check("завершений тиждень: 20–26.07 повністю",
          (WEEK_DONE["start"], WEEK_DONE["data_end"]) == (date(2026, 7, 20), date(2026, 7, 26)),
          WEEK_DONE)
    check("завершений тиждень порівнюється з повним попереднім",
          (WEEK_DONE["prev_start"], WEEK_DONE["prev_end"]) == (date(2026, 7, 13), date(2026, 7, 19)),
          WEEK_DONE)
    check("поточний тиждень: дані лише до вчора",
          WEEK_NOW["data_end"] == date(2026, 7, 28) and WEEK_NOW["days"] == 2, WEEK_NOW)
    check("поточний тиждень порівнюється з ТИМИ САМИМИ днями попереднього",
          (WEEK_NOW["prev_start"], WEEK_NOW["prev_end"]) == (date(2026, 7, 20), date(2026, 7, 21))
          and WEEK_NOW["comparable_days"] == 2, WEEK_NOW)
    check("поточний місяць: 1–28.07 проти 1–28.06",
          (MONTH_NOW["start"], MONTH_NOW["data_end"], MONTH_NOW["prev_end"])
          == (date(2026, 7, 1), date(2026, 7, 28), date(2026, 6, 28)), MONTH_NOW)
    # понеділок: тиждень почався сьогодні, повних днів ще нема
    monday = ta._ranges("week", 0, today=date(2026, 7, 27))
    check("тиждень без жодного повного дня — has_data False", not monday["has_data"], monday)


# ---------- Сайт ----------

def _daily(day, users, sessions=None, pageviews=None):
    return {"date": f"2026-07-{day:02d}", "users": users,
            "sessions": sessions, "pageviews": pageviews}


def test_site():
    rows = (
        [_daily(d, 100, 120, 200) for d in range(13, 20)]      # попередній: 700
        + [_daily(d, 110, 130, 210) for d in range(20, 27)]    # поточний: 770
    )
    rows.append(_daily(26, 110, None, 210))                     # дубль із NULL-сесіями
    ta.bot_db.query = lambda sql, params=None: rows
    s = ta._site_block(WEEK_DONE)
    check("сума читачів завершеного тижня", s["users"]["value"] == 880, s["users"])
    check("сума попереднього тижня", s["users"]["prev"] == 700, s["users"])
    check("дельта у %", s["users"]["delta"] == 26, s["users"])
    check("NULL у колонці не ламає суму сесій", s["sessions"]["value"] == 7 * 130, s["sessions"])
    check("серія — лише дні поточного відрізку", len(s["series"]) == 8, len(s["series"]))
    check("найкращий день знайдено", s["best_day"]["users"] == 110, s["best_day"])

    ta.bot_db.query = lambda sql, params=None: []
    empty = ta._site_block(WEEK_DONE)
    check("порожня пам'ять аналітики — None, а не нуль",
          empty["users"]["value"] is None and empty["users"]["delta"] is None, empty["users"])


# ---------- Пошук ----------

def test_search():
    def rows(_sql, _params=None):
        out = []
        for d in range(20, 27):                       # поточний тиждень
            out += [{"date": f"2026-07-{d:02d}", "search_type": "web",
                     "clicks": 100, "impressions": 1000},
                    {"date": f"2026-07-{d:02d}", "search_type": "discover",
                     "clicks": 40, "impressions": 900},
                    {"date": f"2026-07-{d:02d}", "search_type": "googleNews",
                     "clicks": 10, "impressions": 100}]
        for d in range(13, 20):                       # попередній
            out.append({"date": f"2026-07-{d:02d}", "search_type": "web",
                        "clicks": 80, "impressions": 900})
        return out

    ta.bot_db.query = rows
    s = ta._search_block(WEEK_DONE)
    web = next(t for t in s["types"] if t["type"] == "web")
    disc = next(t for t in s["types"] if t["type"] == "discover")
    check("усі кліки періоду", s["clicks"]["value"] == 7 * 150, s["clicks"])
    check("частка пошуку", web["share"] == 67, web)
    check("частка Discover", disc["share"] == 27, disc)
    check("дельта по типу проти попереднього тижня", web["delta"] == 25, web)
    check("тип без історії — delta None", disc["delta"] is None, disc)
    check("порядок типів фіксований (він же порядок кольорів)",
          [t["type"] for t in s["types"]] == ["web", "discover", "googleNews"], s["types"])


# ---------- Вихід редакції ----------

def test_newsroom():
    rows = [
        {"owner_id": 44, "type": "news", "own": 1, "cur": 1, "c": 10, "name": "Юлія Бойченко"},
        {"owner_id": 44, "type": "news", "own": 0, "cur": 1, "c": 5, "name": "Юлія Бойченко"},
        {"owner_id": 44, "type": "article", "own": 1, "cur": 1, "c": 2, "name": "Юлія Бойченко"},
        {"owner_id": 51, "type": "news", "own": 0, "cur": 1, "c": 8, "name": "Марія Хаміцевич"},
        {"owner_id": 99, "type": "news", "own": 0, "cur": 1, "c": 1, "name": "   "},
        {"owner_id": 44, "type": "news", "own": 1, "cur": 0, "c": 12, "name": "Юлія Бойченко"},
        # рерайтерка з 20 рядками проти авторки з 10 власними: за кількістю
        # перша, за вагою (OWN_WEIGHT) — друга
        {"owner_id": 60, "type": "news", "own": 0, "cur": 1, "c": 20, "name": "Стрічка"},
        {"owner_id": 61, "type": "news", "own": 1, "cur": 1, "c": 10, "name": "Лук'яненко"},
        # третій тип у CMS: у «новини/статті» не входить, але й зникнути не має
        {"owner_id": 44, "type": "blog", "own": 1, "cur": 1, "c": 3, "name": "Юлія Бойченко"},
    ]
    ta.db.query = lambda sql, params=None: rows
    # owner_id → людина ростера (у проді це пін team_user_link → ПІБ)
    ta._person_by_owner_id = lambda: {44: "Юлія Бойченко", 61: "Юлія Лук'яненко"}
    n = ta._newsroom_block(WEEK_DONE)
    check("новини поточного періоду", n["news"]["value"] == 54, n["news"])
    check("статті окремо від новин", n["articles"]["value"] == 2, n["articles"])
    check("власні лише з own_material=1", n["own"]["value"] == 22, n["own"])
    check("порівняльний період з того самого запиту (колонка cur)",
          n["news"]["prev"] == 12 and n["news"]["delta"] == 350, n["news"])
    by_name = {a["name"]: a for a in n["authors"]}
    # 12 власних × вага + 5 рерайтів; звіряємось із самою константою, щоб
    # тест не переписувати щоразу, коли Олег її змінить
    check(f"вага власного матеріалу — {ta.OWN_WEIGHT} звичайного",
          by_name["Юлія Бойченко"]["score"] == round(12 * ta.OWN_WEIGHT + 5, 1),
          by_name["Юлія Бойченко"])
    check("коефіцієнт їде у payload — підпис на фронті з нього ж",
          n["own_weight"] == ta.OWN_WEIGHT, n.get("own_weight"))
    check("рейтинг за вагою, а не за кількістю рядків",
          [a["name"] for a in n["authors"]][:2]
          == ["Юлія Бойченко", "Юлія Лук'яненко"], [a["name"] for a in n["authors"]])
    check("20 рерайтів стоять НИЖЧЕ за 10 власних",
          [a["name"] for a in n["authors"]].index("Стрічка")
          > [a["name"] for a in n["authors"]].index("Юлія Лук'яненко"), n["authors"])
    check("автор зведений до канонічного ПІБ ростера (фото і трекер)",
          by_name["Юлія Лук'яненко"]["person"] == "Юлія Лук'яненко", by_name.keys())
    check("автор поза ростером лишається з іменем із users",
          by_name["Стрічка"]["person"] is None, by_name["Стрічка"])
    check("автор без імені в users підписаний id",
          any(a["name"] == "id 99" for a in n["authors"]), n["authors"])
    check("власні автора рахуються окремо",
          by_name["Юлія Бойченко"]["own"] == 12
          and by_name["Юлія Бойченко"]["count"] == 17, by_name["Юлія Бойченко"])
    check("список авторів НЕ обрізаний — фронт вирішує, скільки показати",
          len(n["authors"]) == 5 and n["shown"] == ta.TOP_AUTHORS, len(n["authors"]))
    check("третій тип видно окремо, а не тихо зникає",
          n["by_type"] == [{"type": "blog", "count": 3}], n["by_type"])
    check("блог не додався ні в новини, ні в статті",
          n["news"]["value"] == 54 and n["articles"]["value"] == 2, n)
    zero_names = [z["name"] for z in n["zero"]]
    check("хто за період не опублікував нічого — окремим списком",
          "Аліса Мелікадамян" in zero_names and "Таміла Ксьонжик" in zero_names,
          zero_names[:5])
    check("менеджерів у «без публікацій» немає — у них вихід не міряють",
          "Катерина Середа" not in zero_names and "Олег Деренюга" not in zero_names,
          zero_names)
    check("хто публікував — у списку нулів не опиниться",
          "Юлія Бойченко" not in zero_names and "Юлія Лук'яненко" not in zero_names,
          zero_names)
    check("непривʼязаний до users.id — це «не знаю», а не нуль",
          all("linked" in z for z in n["zero"])
          and any(not z["linked"] for z in n["zero"]), n["zero"][:3])


# ---------- Топ матеріалів ----------

def test_top():
    ta.bot_db.query = lambda sql, params=None: [
        {"top_pages": [
            {"path": "/news/city/1-a", "title": "Сесія міськради | МикВісті",
             "views": 3000, "author": "Аліна Квітко"},
            {"path": "/articles/2-b", "title": "Лонгрід", "views": 1000, "author": None},
        ]},
        {"top_pages": [
            {"path": "/news/city/1-a", "title": "Сесія міськради | МикВісті",
             "views": 2000, "author": "Аліна Квітко"},
            {"path": None, "title": "сміття", "views": 999},
        ]},
        {"top_pages": None},
    ]
    top = ta._top_materials(WEEK_DONE)
    check("перегляди сумуються по шляху", top[0]["views"] == 5000, top[0])
    check("«днів у топі» рахується", top[0]["days"] == 2, top[0])
    check("хвіст «| МикВісті» прибрано", top[0]["title"] == "Сесія міськради", top[0])
    check("абсолютний URL добудовано",
          top[0]["url"] == "https://nikvesti.com/news/city/1-a", top[0])
    check("рядки без шляху пропускаються", len(top) == 2, top)


# ---------- Соцмережі ----------

def test_social():
    """Тижневий грейн: FB/IG зі зрізів, підписники з обох грейнів, TikTok —
    із місячної історії (тижневих зрізів по ньому ще немає)."""
    def query(sql, params=None):
        if "DISTINCT ON" in sql and "social_stats" in sql:
            day = params[0]
            if day < date(2026, 7, 20):
                return [{"platform": "facebook", "followers": 41000, "at": "2026-07-12"}]
            return [{"platform": "facebook", "followers": 41500, "at": "2026-07-26"}]
        if "DISTINCT ON" in sql and "social_monthly" in sql:
            # TikTok знаємо лише з таблиці (місячний рядок 1 липня)
            return [{"platform": "tiktok", "followers": 8200, "at": "2026-07-01"}]
        if "FROM social_monthly" in sql:
            return [{"platform": "tiktok", "date": "2026-07-01", "followers": 8200,
                     "reach": None, "views": 90000, "engagement": 4000,
                     "posts": 12, "extra": {"likes": 3000}}]
        return [
            {"platform": "facebook", "date": "2026-07-19", "followers": 41000,
             "reach": None, "views": 100, "engagement": 10, "posts": 50},
            {"platform": "facebook", "date": "2026-07-26", "followers": 41500,
             "reach": None, "views": 150, "engagement": 20, "posts": 60},
            {"platform": "instagram", "date": "2026-07-26", "followers": 20000,
             "reach": 5, "views": 7, "engagement": 3, "posts": 12},
        ]

    ta.bot_db.query = query
    ta._tg_cache.update({"at": 0.0, "value": None})
    ta._tg_subscribers = lambda: 41012
    s = ta._social_block(WEEK_DONE, "week")
    by_key = {p["key"]: p for p in s["platforms"]}
    fb = by_key["facebook"]
    check("тижневий грейн так і названий", s["grain"] == "week", s["grain"])
    check("підписники — останній знімок у межах періоду", fb["followers"] == 41500, fb)
    check("приріст підписників — до знімка перед періодом",
          fb["followers_delta"] == 500, fb)
    check("перегляди періоду — знімок 26.07", fb["views"]["value"] == 150, fb["views"])
    check("порівняння з відрізком попереднього тижня (19.07)",
          fb["views"]["prev"] == 100 and fb["views"]["delta"] == 50, fb["views"])
    check("підписники беруться і з місячної історії (TikTok — лише там)",
          by_key["tiktok"]["followers"] == 8200, by_key.get("tiktok"))
    check("Telegram без жодного сліду в норі — живий знімок t.me",
          by_key["telegram"]["followers"] == 41012 and by_key["telegram"]["live"],
          by_key.get("telegram"))
    check("мережі без даних у картках немає, вона в missing",
          "YouTube" in s["missing"] and "youtube" not in by_key, s["missing"])
    check("порядок карток фіксований",
          [p["key"] for p in s["platforms"]][:2] == ["facebook", "instagram"],
          [p["key"] for p in s["platforms"]])

    # Місячний грейн: читаються МІСЯЧНІ рядки (рядок лежить на 1-му числі)
    m = ta._social_block(ta._ranges("month", 0, today=TODAY), "month")
    tt = {p["key"]: p for p in m["platforms"]}["tiktok"]
    check("місячний грейн читає рядок місяця, а не діапазон дат",
          tt["views"]["value"] == 90000 and m["grain"] == "month", tt["views"])
    check("тижневі зрізи в місячний вид не підмішуються",
          {p["key"] for p in m["platforms"]} & {"facebook"} == set()
          or {p["key"]: p for p in m["platforms"]}["facebook"]["views"]["value"] is None,
          [p["key"] for p in m["platforms"]])


# ---------- Гранти ----------

def test_grants():
    def query(sql, params=None):
        if "team_creative_tasks" in sql and "'open'" in sql:
            return [{"c": 25}]
        if "team_creative_tasks" in sql:
            return [{"c": 7}]
        if "GROUP BY project_id" in sql:
            return [{"project_id": 1, "c": 3}, {"project_id": 2, "c": 5},
                    {"project_id": 777, "c": 1}]
        return [{"c": 4}]

    ta.bot_db.query = query
    projects = [{"id": 1, "name": "Голоси Миколаєва", "partner": "IMS"},
                {"id": 2, "name": "Стійкість", "partner": "IWPR"}]
    g = ta._grants_block(WEEK_DONE, projects)
    check("активні проєкти — з CMS", g["projects_active"] == 2, g)
    check("відкриті й закриті завдання окремо",
          (g["tasks_open"], g["tasks_done"]) == (25, 7), g)
    check("зараховане підсумовано", g["counted"] == 9, g)
    check("донори за спаданням", [d["name"] for d in g["donors"]][:2] == ["IWPR", "IMS"], g["donors"])
    check("зниклий з вибірки проєкт не ламає список",
          g["donors"][-1]["name"] == "поза проєктами", g["donors"])


# ---------- Деградація і кеш ----------

def test_degradation_and_cache():
    calls = {"n": 0}

    def boom(sql, params=None):
        calls["n"] += 1
        raise RuntimeError("нора впала")

    ta.bot_db.query = boom
    ta.db.query = lambda sql, params=None: [
        {"owner_id": 44, "type": "news", "own": 1, "cur": 1, "c": 3, "name": "Аліса Мелікадамян"}]
    ta.team_projects.list_projects = lambda active_only=True: []
    ta._cache.clear()
    d = ta.dashboard("week", -1)
    check("збій нори гасить свої блоки", d["site"] is None and d["social"] is None, d.keys())
    check("а БД сайту продовжує працювати", d["newsroom"]["news"]["value"] == 3, d["newsroom"])
    check("топ при збої — порожній список, не None", d["top"] == [], d["top"])
    check("payload завжди має підписи періоду",
          d["label"] and d["prev_label"] and d["range"]["data_end"], d)
    before = calls["n"]
    ta.dashboard("week", -1)
    check("повторний виклик іде з кешу", calls["n"] == before, calls["n"])
    ta.dashboard("month", -1)
    check("інший період кеш не підмінює", calls["n"] > before, calls["n"])


if __name__ == "__main__":
    test_ranges()
    test_site()
    test_search()
    test_newsroom()
    test_top()
    test_social()
    test_grants()
    test_degradation_and_cache()
    print()
    if FAILS:
        print(f"❌ Провалено {len(FAILS)}: {', '.join(FAILS)}")
        sys.exit(1)
    print("✅ Усе зелене")
