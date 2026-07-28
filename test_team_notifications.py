"""
Тест стрічки сповіщень Mini App «Команда» на ЖИВОМУ Postgres
(handlers/team_notifications.py + емітери).

Олег окреслив «Сповіщення» як стрічку подій за роллю. Тут — спільний каркас
і перші типи: поставили завдання · завдання виконано · звітний дедлайн.

Що стереже:
- адресація: журналістка бачить своє й НЕ бачить менеджерського;
- дедуп (dedup_key): та сама подія двічі в стрічку не лягає;
- прочитане — per людина: Катя прочитала, в Олега лишилось непрочитаним;
- емітер постановки таска пише подію виконавиці;
- емітер авто-закриття пише подію і виконавиці, і керівництву, з лінком на
  зараховану публікацію;
- збій сповіщення не ламає дію, що його породила (notify_safe).

Запуск:
    BOT_DATABASE_URL=postgresql://... python test_team_notifications.py
"""

import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import (   # noqa: E402
    bot_db, team_matches, team_notifications as tn, team_tasks,
)

RESULTS = []
ANNA = "Тест Тестова"
KATYA = "Тест Редакторка"
OLEH = "Тест Керівник"


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    ids = [r["id"] for r in bot_db.query(
        "SELECT id FROM team_creative_tasks WHERE person = %s", (ANNA,))]
    if ids:
        bot_db.execute("DELETE FROM team_task_events WHERE task_id = ANY(%s)", (ids,))
        bot_db.execute("DELETE FROM team_task_matches WHERE task_id = ANY(%s)", (ids,))
    bot_db.execute("DELETE FROM team_creative_tasks WHERE person = %s", (ANNA,))
    rows = bot_db.query(
        "SELECT id FROM team_notifications WHERE person = ANY(%s) "
        "OR dedup_key LIKE 'test-%%' OR title LIKE '%%Тест%%'",
        ([ANNA, KATYA, OLEH],))
    if rows:
        nids = [r["id"] for r in rows]
        bot_db.execute(
            "DELETE FROM team_notification_reads WHERE notification_id = ANY(%s)", (nids,))
        bot_db.execute("DELETE FROM team_notifications WHERE id = ANY(%s)", (nids,))


def main():
    tn.ensure_notifications_schema()
    team_matches.ensure_match_schema()
    cleanup()

    # ---------- 1. Адресація ----------
    tn.notify("task_assigned", "Тест: особисте", audience="person", person=ANNA,
              dedup_key="test-personal")
    tn.notify("report_deadline", "Тест: керівництву", audience="managers",
              dedup_key="test-managers")
    anna_feed = [n["title"] for n in tn.feed(ANNA, False)]
    katya_feed = [n["title"] for n in tn.feed(KATYA, True)]
    check("журналістка бачить своє", "Тест: особисте" in anna_feed)
    check("і не бачить менеджерського", "Тест: керівництву" not in anna_feed)
    check("менеджер бачить менеджерське", "Тест: керівництву" in katya_feed)
    check("чуже особисте менеджер теж не читає", "Тест: особисте" not in katya_feed)

    # ---------- 2. Дедуп ----------
    again = tn.notify("task_assigned", "Тест: особисте (дубль)", audience="person",
                      person=ANNA, dedup_key="test-personal")
    check("та сама подія двічі не лягає", again is None)
    check("у стрічці лишився один запис",
          [n["title"] for n in tn.feed(ANNA, False)].count("Тест: особисте") == 1)

    # ---------- 3. Прочитане — по людині ----------
    check("спершу непрочитане в обох", tn.unread_count(KATYA, True) >= 1
          and tn.unread_count(OLEH, True) >= 1)
    katya_before = tn.unread_count(KATYA, True)
    tn.mark_read(KATYA, True)
    check("Катя прочитала — у неї нуль", tn.unread_count(KATYA, True) == 0)
    check("в Олега лишилось непрочитаним", tn.unread_count(OLEH, True) == katya_before)
    check("прочитане не зникає зі стрічки, а глушиться",
          any(not n["unread"] and n["title"] == "Тест: керівництву"
              for n in tn.feed(KATYA, True)))

    # ---------- 4. Емітер постановки таска ----------
    task = team_tasks.create_task(
        creator="Катерина Середа", person=ANNA, type_="article", project_id=999,
        project_name="Тестовий проєкт", theme_id=None, theme_name="Тестова тематика",
        qty=1, deadline="2026-08-31", partner_name="Тест-донор")
    assigned = [n for n in tn.feed(ANNA, False) if n["kind"] == "task_assigned"
                and n["object_id"] == str(task["id"])]
    check("постановка таска створила подію виконавиці", len(assigned) == 1)
    check("у події видно, хто поставив і до якого числа",
          "Катерина" in assigned[0]["body"] and "31.08" in assigned[0]["body"])
    # Заголовок події = task_summary: тематика і донор, БЕЗ назви проєкту
    check("у заголовку події тематика, а не назва проєкту",
          "Тестова тематика" in assigned[0]["title"]
          and "Тестовий проєкт" not in assigned[0]["title"])
    check("донор у заголовку лишився", "Тест-донор" in assigned[0]["title"])

    # ---------- 5. Емітер авто-закриття ----------
    team_matches.record_match(
        "site", "test-nt-1", "auto", task_id=task["id"], person=ANNA,
        project_id=999, confidence="high", title="Зарахована стаття",
        url="https://nikvesti.com/news/politics/1-a")
    team_matches.apply_progress(task["id"])
    done_anna = [n for n in tn.feed(ANNA, False) if n["kind"] == "task_done"]
    done_mgr = [n for n in tn.feed(KATYA, True)
                if n["kind"] == "task_done" and n["audience"] == "managers"]
    check("виконавиця отримала подію про виконання", len(done_anna) == 1)
    check("керівництво теж", len(done_mgr) == 1)
    check("у події є лінк на зараховану публікацію",
          done_anna[0]["url"].endswith("/1-a"))
    check("керівництву видно, чиє це виконання", ANNA in done_mgr[0]["title"])

    # Повторний перерахунок не має сипати дублями
    team_matches.apply_progress(task["id"])
    check("повторний перерахунок дублів не створює",
          len([n for n in tn.feed(ANNA, False) if n["kind"] == "task_done"]) == 1)

    # ---------- 6. Збій сповіщення не ламає дію ----------
    broken = tn.notify_safe("task_done", None, audience="person", person=ANNA)
    check("збій запису події проковтнутий", broken is None)
    check("і таск на місці", team_tasks.get_task(task["id"]) is not None)

    # ...і не валить ТРАНЗАКЦІЮ масової постановки: у Postgres збійний
    # statement робить непридатною всю транзакцію, тож самого try/except мало
    orig_notify = tn.notify
    # Падає САМ SQL (title NOT NULL), а не обгортка — інакше перевірка була б
    # ні про що: транзакцію псує саме збійний statement
    tn.notify = lambda *a, **kw: orig_notify(
        "task_assigned", None, audience="person", person=ANNA)
    try:
        specs = [
            {"creator": "Катерина Середа", "person": ANNA, "type_": "news",
             "project_id": 999, "project_name": "Тестовий проєкт",
             "theme_name": f"Масова {i}", "qty": 1}
            for i in range(3)
        ]
        bulk = team_tasks.create_tasks_bulk(specs)
    except Exception as e:
        bulk = f"впало: {e}"
    finally:
        tn.notify = orig_notify
    check("збій сповіщення не завалив пачку з трьох тасків",
          isinstance(bulk, list) and len(bulk) == 3)
    check("і таски справді в базі",
          isinstance(bulk, list) and all(team_tasks.get_task(t["id"]) for t in bulk))

    cleanup()

    print("Стрічка сповіщень (живий Postgres):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
