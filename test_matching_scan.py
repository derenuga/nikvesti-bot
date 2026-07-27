"""
Тест фонового прогону звірки виконання на ЖИВОМУ Postgres
(handlers/team_matching.judge_nodes).

БД сайту і суддя підмінені (публікації — фікстури, вердикти — задані), а от
Нора справжня: пишуться реальні матчі, реально закриваються таски.

Що стереже:
- high → авто-зарахування, не-high → черга Каті;
- пінг виконавиці йде РІВНО раз і тільки на закриття таска;
- повторний прогін по тих самих публікаціях не зараховує вдруге і не смикає
  AI (ідемпотентність UNIQUE (source, ref));
- вичерпаний таск (набрав qty) не збирає нові публікації;
- автора немає в ростері → skipped без виклику AI;
- skipped перерозглядається, коли таск на цю тематику з'явився пізніше;
- збій судді нічого не пише — наступний прогін спробує ще.

Запуск:
    BOT_DATABASE_URL=postgresql://... python test_matching_scan.py
"""

import asyncio
import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, team_matches, team_matching, team_tasks   # noqa: E402

RESULTS = []
PERSON = "Тест Тестова"
PROJECT = 999


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    ids = [r["id"] for r in bot_db.query(
        "SELECT id FROM team_creative_tasks WHERE person = %s", (PERSON,))]
    if ids:
        bot_db.execute("DELETE FROM team_task_events WHERE task_id = ANY(%s)", (ids,))
    bot_db.execute("DELETE FROM team_task_matches WHERE person = %s OR ref LIKE '90%%'",
                   (PERSON,))
    bot_db.execute("DELETE FROM team_creative_tasks WHERE person = %s", (PERSON,))


def node(nid, title, owner=42, type_="article", project=PROJECT):
    return {"id": nid, "type": type_, "category": "politics", "status": 1,
            "owner_id": owner, "partner_project": project, "published": 1785000000,
            "slug_ua": f"{nid}-test", "slug": None, "title": title,
            "content": f"<p>Лід матеріалу {nid}.</p>"}


def task(theme, qty=1, type_="article"):
    return team_tasks.create_task(
        creator="Катерина Середа", person=PERSON, type_=type_, project_id=PROJECT,
        project_name="Тестовий проєкт", theme_id=None, theme_name=theme, qty=qty,
        partner_name="Тест-донор")


class Judge:
    """Керований суддя: вердикт за заголовком публікації."""

    def __init__(self, plan):
        self.plan = plan          # {заголовок: (confidence, тематика|None)}
        self.calls = []
        self.offered = []         # які тематики пропонували судді

    async def __call__(self, article, candidates):
        self.calls.append(article["title"])
        self.offered.append([c["theme_name"] for c in candidates])
        conf, theme = self.plan.get(article["title"], ("low", None))
        if conf == "boom":
            raise RuntimeError("суддя недоступний")
        chosen = next((c for c in candidates if c["theme_name"] == theme), None)
        return {"matched_task_id": chosen["id"] if chosen else None,
                "confidence": conf, "reasoning": "тестовий вердикт", "task": chosen}


async def main():
    team_matches.ensure_match_schema()
    cleanup()
    pings = []

    async def ping(person, t):
        pings.append((person, t["id"]))

    team_matching.owner_person_map = lambda: {42: PERSON}

    sessions = task("Репортажі з сесій", qty=1)
    requests = task("Історії з інфозапитів", qty=2)

    # ---------- 1. high → авто, medium → черга ----------
    judge = Judge({
        "Сесія ухвалила бюджет": ("high", "Репортажі з сесій"),
        "Історія одного запиту": ("medium", "Історії з інфозапитів"),
    })
    rows = [node(901, "Сесія ухвалила бюджет"), node(902, "Історія одного запиту")]
    rep = await team_matching.judge_nodes(rows, ping=ping, judge=judge)
    check("переглянуто обидві публікації", rep.seen == 2)
    check("high зараховано автоматично", rep.auto == 1)
    check("не-high пішло в чергу Каті", rep.pending == 1)
    check("таск на 1 матеріал закрився сам", len(rep.closed) == 1
          and team_tasks.get_task(sessions["id"])["status"] == "done")
    check("виконавиця отримала рівно один пінг",
          pings == [(PERSON, sessions["id"])])
    check("спірна публікація видима в черзі",
          any(m["ref"] == "902" for m in team_matches.pending_matches()))
    check("до спірного матчу прикріплений лінк на матеріал",
          next(m for m in team_matches.pending_matches()
               if m["ref"] == "902")["url"].endswith("902-test"))

    # ---------- 2. Повторний прогін нічого не подвоює ----------
    judge2 = Judge({"Сесія ухвалила бюджет": ("high", "Репортажі з сесій")})
    rep2 = await team_matching.judge_nodes(rows, ping=ping, judge=judge2)
    check("повторний прогін не смикає AI", judge2.calls == [])
    check("і нічого не зараховує вдруге", rep2.auto == 0
          and team_matches.counted_count(sessions["id"]) == 1)
    check("зайвого пінга теж немає", len(pings) == 1)

    # ---------- 3. Вичерпаний таск нових публікацій не збирає ----------
    judge3 = Judge({"Ще одна сесія": ("high", "Репортажі з сесій")})
    rep3 = await team_matching.judge_nodes([node(903, "Ще одна сесія")], judge=judge3)
    check("вичерпаний таск судді не пропонується",
          judge3.offered == [["Історії з інфозапитів"]])
    check("зарахувати в нього нікуди — публікація пішла в чергу",
          rep3.auto == 0 and rep3.pending == 1
          and team_matches.counted_count(sessions["id"]) == 1)

    # ---------- 4. Автор поза ростером ----------
    rep4 = await team_matching.judge_nodes([node(904, "Чужий матеріал", owner=777)])
    check("автора немає в ростері → skipped без AI", rep4.skipped == 1)
    stored = bot_db.query(
        "SELECT status, reasoning FROM team_task_matches WHERE ref = '904'")
    check("причина записана, щоб було видно чому",
          stored and "ростер" in stored[0]["reasoning"])

    # ---------- 5. skipped перерозглядається, коли таск з'явився ----------
    # Публікація в проєкті, де тасків ще не ставили (Катя часто ставить їх
    # заднім числом — і тоді вже побачене має зарахуватись).
    other_node = node(906, "Матеріал іншого проєкту", project=998)
    rep5a = await team_matching.judge_nodes([other_node])
    check("немає тасків у проєкті → skipped без AI", rep5a.skipped == 1)
    later = team_tasks.create_task(
        creator="Катерина Середа", person=PERSON, type_="article", project_id=998,
        project_name="Проєкт-пізніше", theme_id=None, theme_name="Пізня тематика",
        qty=1, partner_name="Тест-донор")
    judge5 = Judge({"Матеріал іншого проєкту": ("high", "Пізня тематика")})
    rep5 = await team_matching.judge_nodes([other_node], judge=judge5)
    check("skipped перерозглянуто після появи таска",
          judge5.calls == ["Матеріал іншого проєкту"])
    check("і публікація тепер зарахована", rep5.auto == 1
          and team_matches.counted_count(later["id"]) == 1)

    # ---------- 6. Збій судді нічого не псує ----------
    judge6 = Judge({"Проблемна": ("boom", None)})
    rep6 = await team_matching.judge_nodes([node(905, "Проблемна")], judge=judge6)
    check("збій AI порахований як збій", rep6.errors == 1)
    check("і не залишив запису — наступний прогін спробує ще",
          not bot_db.query("SELECT 1 FROM team_task_matches WHERE ref = '905'"))

    # ---------- 7. Рішення Каті зараховує в тематику ----------
    m = next(x for x in team_matches.pending_matches() if x["ref"] == "902")
    team_matches.decide_match(m["id"], "Катерина Середа", "confirm",
                              task_id=requests["id"])
    team_matches.apply_progress(requests["id"])
    check("підтверджене з черги пішло в прогрес (1 із 2)",
          team_matches.counted_count(requests["id"]) == 1
          and team_tasks.get_task(requests["id"])["status"] == "open")

    cleanup()

    print("Фоновий прогін звірки (живий Postgres, підмінені БД сайту й суддя):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
