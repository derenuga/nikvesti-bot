"""
Тест ретроспективного прогону звірки на ЖИВОМУ Postgres
(handlers/team_matching: retro_windows / retro_nodes / retro_plan).

Вимога Олега: не робити baseline, а пройти базу і позакривати вже виконані
таски заднім числом. Вікно привʼязане до ТАСКІВ, а не до календаря.

Що стереже:
- вікно = min(створення таска, перше число місяця його дедлайну) — масова
  постановка 27-го числа не має губити публікації з початку того місяця;
- публікації ДО вікна не беруться, у вікні — беруться;
- пара «людина × проєкт» рахується окремо: чуже вікно не тягне чужі ноди;
- уже суджені публікації в план не потрапляють (повторний прогін дешевий);
- оцінка вартості рахується за прайсом судді;
- ретроспектива НЕ пінгує людей (інакше два десятки привітань разом),
  але таски закриває і прогрес пише.

Запуск:
    BOT_DATABASE_URL=postgresql://... python test_matching_retro.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, team_matches, team_matching, team_tasks   # noqa: E402

RESULTS = []
ANNA = "Тест Тестова"
BORYS = "Тест Другий"
KYIV = team_tasks.KYIV_TZ


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    for person in (ANNA, BORYS):
        ids = [r["id"] for r in bot_db.query(
            "SELECT id FROM team_creative_tasks WHERE person = %s", (person,))]
        if ids:
            bot_db.execute("DELETE FROM team_task_events WHERE task_id = ANY(%s)", (ids,))
        bot_db.execute("DELETE FROM team_task_matches WHERE person = %s", (person,))
        bot_db.execute("DELETE FROM team_creative_tasks WHERE person = %s", (person,))
    bot_db.execute("DELETE FROM team_task_matches WHERE ref LIKE '95%%'")


def ts(dt):
    return int(dt.timestamp())


def node(nid, owner, project, when):
    return {"id": nid, "type": "article", "category": "politics", "status": 1,
            "owner_id": owner, "partner_project": project, "published": ts(when),
            "slug_ua": f"{nid}-retro", "slug": None,
            "title": f"Матеріал {nid}", "content": "<p>Лід.</p>"}


class FakeSiteDB:
    """БД сайту: віддає задані ноди, ігноруючи WHERE (фільтрацію по вікнах
    робить сам retro_nodes — саме її ми й перевіряємо)."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def is_configured(self):
        return True

    def query(self, sql, params=None):
        self.queries += 1
        return list(self.rows)


class Judge:
    def __init__(self, conf="high"):
        self.conf = conf
        self.calls = []

    async def __call__(self, article, candidates):
        self.calls.append(article["title"])
        return {"matched_task_id": candidates[0]["id"], "confidence": self.conf,
                "reasoning": "ретро-вердикт", "task": candidates[0]}


async def main():
    team_matches.ensure_match_schema()
    cleanup()
    team_matching.owner_person_map = lambda: {42: ANNA, 43: BORYS}

    now = datetime.now(KYIV)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Дедлайн — кінець поточного місяця (як у масовій постановці Каті)
    nxt = (month_start + timedelta(days=32)).replace(day=1)
    deadline = (nxt - timedelta(days=1)).date().isoformat()

    anna = team_tasks.create_task(
        creator="Катерина Середа", person=ANNA, type_="article", project_id=901,
        project_name="Проєкт А", theme_id=None, theme_name="Тематика А", qty=2,
        deadline=deadline, partner_name="Донор А")
    team_tasks.create_task(
        creator="Катерина Середа", person=BORYS, type_="article", project_id=902,
        project_name="Проєкт Б", theme_id=None, theme_name="Тематика Б", qty=1,
        deadline=deadline, partner_name="Донор Б")

    # ---------- 1. Вікно тягнеться до початку місяця дедлайну ----------
    pool = team_tasks.list_open_tasks()
    windows = team_matching.retro_windows(pool)
    check("вікно рахується по кожній парі «людина × проєкт»",
          set(windows) == {(42, 901), (43, 902)})
    check("вікно відкотилось до першого числа місяця дедлайну, а не до "
          "створення таска", windows[(42, 901)] == ts(month_start))

    # Межа глибини (Олег: «достатньо за липень прогнати») — дефолт поточний
    # місяць, глибше тільки явно
    older = team_tasks.create_task(
        creator="Катерина Середа", person=ANNA, type_="article", project_id=903,
        project_name="Старий проєкт", theme_id=None, theme_name="Стара тематика",
        qty=1, deadline=(month_start - timedelta(days=40)).date().isoformat(),
        partner_name="Донор В")
    pool_with_old = team_tasks.list_open_tasks()
    deep = team_matching.retro_windows(pool_with_old)
    clamped = team_matching.retro_windows(
        pool_with_old, team_matching.current_month_start())
    check("без межі старий таск тягне вікно в минулий місяць",
          deep[(42, 903)] < ts(month_start))
    check("межа «поточний місяць» підрізає його до першого числа",
          clamped[(42, 903)] == ts(month_start))
    check("свіжі таски від межі не страждають",
          clamped[(42, 901)] == deep[(42, 901)] == ts(month_start))
    team_tasks.set_status(older["id"], "Катерина Середа", "dropped")

    # ---------- 2. Фільтрація по вікну ----------
    inside = now - timedelta(hours=6)
    before = month_start - timedelta(days=5)
    rows = [
        node(9501, 42, 901, inside),          # у вікні
        node(9502, 42, 901, before),          # до вікна — не беремо
        node(9503, 43, 902, inside),          # інша пара, у її вікні
        node(9504, 42, 902, inside),          # автор без таска в цьому проєкті
    ]
    fake = FakeSiteDB(rows)
    team_matching.db = fake
    picked = {r["id"] for r in team_matching.retro_nodes(windows)}
    check("публікація у вікні взята", 9501 in picked)
    check("публікація ДО вікна відкинута", 9502 not in picked)
    check("чужа пара «людина × проєкт» не тягнеться", 9504 not in picked)
    check("кожна пара зі своїм вікном", 9503 in picked)
    check("БД сайту смикнули один раз на весь прогін", fake.queries == 1)

    # ---------- 3. План і оцінка ----------
    fresh, total, pairs, earliest = await asyncio.to_thread(team_matching.retro_plan)
    check("план бачить обидві публікації у вікнах", len(fresh) == 2)
    check("пар для ретроспективи — дві", pairs == 2)
    cost, tin, tout = team_matching.estimate_cost(100)
    check("оцінка вартості в розумних межах (100 публікацій ≈ $0.2–0.6)",
          0.2 < cost < 0.6 and tin == 95000 and tout == 5000)

    # ---------- 4. Прогін: пінгів немає, таски закриваються ----------
    pings = []
    orig_ping = team_tasks._ping

    async def spy_ping(bot, person, text):
        pings.append(person)
        return True

    team_tasks._ping = spy_ping
    judge = Judge("high")
    report = await team_matching.judge_nodes(fresh, ping=None, judge=judge)
    team_tasks._ping = orig_ping

    check("ретроспектива нікого не пінгувала", pings == [])
    check("зараховано обидві публікації", report.auto == 2)
    check("таск Бориса (1 матеріал) закрився заднім числом",
          any(p == BORYS for p, _ in report.closed))
    check("таск Анни (2 матеріали) лишився відкритим — набрано 1 із 2",
          team_matches.counted_count(anna["id"]) == 1
          and team_tasks.get_task(anna["id"])["status"] == "open")

    # ---------- 5. Повторний прогін безпечний і дешевий ----------
    fresh2, _, _, _ = await asyncio.to_thread(team_matching.retro_plan)
    check("повторна ретроспектива не має що судити", fresh2 == [])
    judge2 = Judge("high")
    rep2 = await team_matching.judge_nodes(fresh, ping=None, judge=judge2)
    check("і навіть на тих самих нодах AI не смикається", judge2.calls == [])
    check("нічого не подвоїлось", rep2.auto == 0
          and team_matches.counted_count(anna["id"]) == 1)

    cleanup()

    print("Ретроспективний прогін звірки (живий Postgres):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
