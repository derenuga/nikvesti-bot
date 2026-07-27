"""
Тест обліку виконання creative tasks на ЖИВОМУ Postgres (handlers/team_matches.py).

Не мок: піднімає схему в реальній базі, пише матчі й перевіряє арифметику
прогресу і крайні випадки, заради яких усе й затівалось:

- ідемпотентність UNIQUE (source, ref): повторний прогін НЕ зараховує вдруге;
- «2/3» рахує лише auto/confirmed (pending і rejected не рахуються);
- набрав qty → таск закривається сам, з позначкою auto_done;
- підняли qty авто-закритому → повертається у відкриті;
- відкликали матч (rejected) після авто-закриття → те саме;
- закритий РУКАМИ таск авто-логіка не чіпає ніколи;
- рішення Каті: confirm у ту саму / іншу тематику і reject;
- зарахування в тематику, на яку завдання ЩЕ НЕ ставили: завдання заводиться
  на льоту і мовчки (без «тобі поставили завдання» за вчорашнє);
- зняття зарахування ПОВЕРТАЄ публікацію в чергу (а не ховає її назавжди):
  «це не сюди» ≠ «це взагалі не проєктне»;
- зарахування руками за лінком ПЕРЕНОСИТЬ публікацію між завданнями (запис на
  неї один, тож прогрес не подвоюється) — випадок задубльованої таски.

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python test_team_matches.py
"""

import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, team_matches, team_tasks   # noqa: E402

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    """Тест ганяється по живій схемі — прибираємо тільки свої рядки."""
    bot_db.execute("DELETE FROM team_task_matches WHERE ref LIKE 'test-%'")
    bot_db.execute("DELETE FROM team_task_matches WHERE person = 'Тест Тестова'")
    ids = [r["id"] for r in bot_db.query(
        "SELECT id FROM team_creative_tasks WHERE person = 'Тест Тестова'")]
    if ids:
        bot_db.execute("DELETE FROM team_task_events WHERE task_id = ANY(%s)", (ids,))
        bot_db.execute("DELETE FROM team_task_matches WHERE task_id = ANY(%s)", (ids,))
    bot_db.execute("DELETE FROM team_creative_tasks WHERE person = 'Тест Тестова'")


def new_task(qty=1, type_="article"):
    return team_tasks.create_task(
        creator="Катерина Середа", person="Тест Тестова", type_=type_,
        project_id=999, project_name="Тестовий проєкт", theme_id=None,
        theme_name="Тестова тематика", qty=qty, partner_name="Тест-донор",
    )


def counted(task_id):
    return team_matches.counted_count(task_id)


def main():
    team_matches.ensure_match_schema()
    cleanup()

    # ---------- 1. Ідемпотентність ----------
    task = new_task(qty=3)
    first = team_matches.record_match(
        "site", "test-1001", "auto", task_id=task["id"], person="Тест Тестова",
        project_id=999, confidence="high", reasoning="збіг", title="Стаття 1",
        url="https://nikvesti.com/news/politics/1001-a", published=1785000000,
        node_type="article")
    again = team_matches.record_match(
        "site", "test-1001", "auto", task_id=task["id"], person="Тест Тестова",
        project_id=999, confidence="high", reasoning="збіг", title="Стаття 1",
        url="https://nikvesti.com/news/politics/1001-a", published=1785000000,
        node_type="article")
    check("перший запис матчу проходить", first and first["status"] == "auto")
    check("повторний запис тієї самої публікації відкидається (UNIQUE)", again is None)
    check("прогрес після дубля лишається 1", counted(task["id"]) == 1)
    check("known_refs бачить уже суджену публікацію",
          "test-1001" in team_matches.known_refs("site", ["test-1001", "test-9999"]))

    # ---------- 2. У прогрес ідуть лише auto/confirmed ----------
    team_matches.record_match("site", "test-1002", "pending", task_id=task["id"],
                              person="Тест Тестова", project_id=999,
                              confidence="medium", title="Стаття 2", url="u2")
    team_matches.record_match("site", "test-1003", "rejected", task_id=task["id"],
                              person="Тест Тестова", project_id=999, title="Стаття 3",
                              url="u3")
    check("pending і rejected у прогрес не йдуть", counted(task["id"]) == 1)

    tasks = team_matches.attach_progress([team_tasks.get_task(task["id"])])
    check("attach_progress віддає done_count", tasks[0]["done_count"] == 1)
    check("до зарахування прикріплений лінк публікації",
          tasks[0]["matches"][0]["url"].endswith("1001-a")
          and tasks[0]["matches"][0]["title"] == "Стаття 1")

    # ---------- 3. Авто-закриття ----------
    _, action = team_matches.apply_progress(task["id"])
    check("на 1 із 3 таск не закривається", action is None
          and team_tasks.get_task(task["id"])["status"] == "open")
    for i in (1004, 1005):
        team_matches.record_match("site", f"test-{i}", "auto", task_id=task["id"],
                                  person="Тест Тестова", project_id=999,
                                  confidence="high", title=f"Стаття {i}", url=f"u{i}")
    updated, action = team_matches.apply_progress(task["id"])
    check("набрав qty → закрився сам", action == "done" and updated["status"] == "done")
    check("позначка «закрив Лис» стоїть", updated["auto_done"] is True)

    # ---------- 4. Підняли qty авто-закритому ----------
    team_tasks.update_task_fields(task["id"], "Катерина Середа", qty=5)
    reopened, action = team_matches.recount_after_qty_change(task["id"])
    check("qty підняли → авто-закритий повернувся у відкриті",
          action == "reopen" and reopened["status"] == "open"
          and reopened["auto_done"] is False)

    # ---------- 5. Відкликали матч після авто-закриття ----------
    team_tasks.update_task_fields(task["id"], "Катерина Середа", qty=3)
    team_matches.apply_progress(task["id"])
    check("знизили qty назад → закрився знову",
          team_tasks.get_task(task["id"])["status"] == "done")
    m = team_matches.get_match(first["id"])
    team_matches.decide_match(m["id"], "Катерина Середа", "reject")
    reopened, action = team_matches.apply_progress(task["id"])
    check("матч відкликали → таск знову відкритий",
          action == "reopen" and reopened["status"] == "open")
    check("прогрес упав до 2", counted(task["id"]) == 2)

    # ---------- 6. Закритий руками авто-логіка не чіпає ----------
    manual = new_task(qty=2)
    team_matches.record_match("site", "test-2001", "auto", task_id=manual["id"],
                              person="Тест Тестова", project_id=999, title="М1", url="m1")
    team_tasks.set_status(manual["id"], "Катерина Середа", "done")   # руками
    after, action = team_matches.apply_progress(manual["id"])
    check("закритий руками (1 із 2) не перевідкривається",
          action is None and after["status"] == "done" and after["auto_done"] is False)

    # ---------- 7. Черга Каті: confirm у іншу тематику ----------
    other = new_task(qty=1)
    pend = team_matches.record_match(
        "site", "test-3001", "pending", task_id=manual["id"], person="Тест Тестова",
        project_id=999, confidence="medium", reasoning="сумнів", title="Спірна",
        url="https://nikvesti.com/news/3001")
    check("спірний матч потрапив у чергу",
          any(x["id"] == pend["id"] for x in team_matches.pending_matches()))
    decided, touched = team_matches.decide_match(
        pend["id"], "Катерина Середа", "confirm", task_id=other["id"])
    check("перевизначення тематики зараховує в інший таск",
          decided["status"] == "confirmed" and decided["task_id"] == other["id"]
          and manual["id"] in touched and other["id"] in touched)
    check("зарахований матч виходить із черги",
          all(x["id"] != pend["id"] for x in team_matches.pending_matches()))
    upd, action = team_matches.apply_progress(other["id"])
    check("новий таск закрився зарахованим матчем",
          action == "done" and upd["status"] == "done")
    check("хто вирішив — записано", team_matches.get_match(pend["id"])["decided_by"]
          == "Катерина Середа")

    # ---------- 7а. Зняття зарахування повертає в чергу ----------
    # Випадок Олега: зарахував не тому, зняв, таск видалив — і публікація
    # зависла б у rejected: ні в черзі, ні у судді.
    back_task = new_task(qty=1)
    back = team_matches.record_match(
        "site", "test-3500", "auto", task_id=back_task["id"],
        person="Тест Тестова", project_id=999, confidence="high",
        title="Помилково зарахована", url="https://nikvesti.com/news/3500")
    team_matches.apply_progress(back_task["id"])
    check("спершу зараховано і таск закрито",
          counted(back_task["id"]) == 1
          and team_tasks.get_task(back_task["id"])["status"] == "done")
    requeued, touched = team_matches.requeue_match(back["id"], "Олег Деренюга")
    for tid in touched:
        team_matches.apply_progress(tid)
    check("зняття повертає публікацію в чергу", requeued["status"] == "pending")
    check("і прибирає помилкову прив'язку до таска", requeued["task_id"] is None)
    check("вона знову видима в «Сповіщеннях»",
          any(x["ref"] == "test-3500" for x in team_matches.pending_matches()))
    check("прогрес таска впав, таск повернувся у відкриті",
          counted(back_task["id"]) == 0
          and team_tasks.get_task(back_task["id"])["status"] == "open")
    check("а reject, навпаки, ховає назавжди",
          "test-1003" not in {x["ref"] for x in team_matches.pending_matches()})

    # ---------- 8. Тематика, на яку завдання ще не ставили ----------
    # Випадок Олега: публікація закриває «критичні інформаційні потреби», а
    # завдання на цю тематику ніхто не заводив — і зарахувати нікуди.
    from handlers import team_notifications, webapp
    real_person = "Аліна Квітко"          # має бути з ростера: сервер це перевіряє
    theme = team_tasks.add_theme(999, "Критичні інформаційні потреби")
    m2 = team_matches.record_match(
        "site", "test-4001", "pending", person=real_person, project_id=999,
        confidence="low", title="Публікація без відповідного завдання",
        url="https://nikvesti.com/news/4001", node_type="news")
    result = webapp._decide_blocking(m2["id"], "Катерина Середа", "confirm",
                                     None, theme["id"], real_person)
    # Тип завдання має братись із самої публікації, коли формат тематики не
    # заданий: інакше воно зветься безликим «матеріал»
    check("зарахування в тематику без завдання спрацювало", bool(result))
    created = [t for t in (result or {}).get("tasks", [])
               if t["theme_id"] == theme["id"]]
    check("завдання під тематику заведено", bool(created))
    check("і публікація одразу зарахована в нього",
          created and team_matches.counted_count(created[0]["id"]) == 1)
    check("тип завдання взявся з публікації, а не «будь-який»",
          created and created[0]["type"] == "news")
    check("завдання на 1 матеріал одразу закрилось",
          created and team_tasks.get_task(created[0]["id"])["status"] == "done")
    assigned = [n for n in team_notifications.feed(real_person, False)
                if n["kind"] == "task_assigned"
                and n["object_id"] == str(created[0]["id"])]
    check("«тобі поставили завдання» за вчорашнє НЕ надсилалось", assigned == [])

    # прибирання за цим блоком
    for t in created:
        bot_db.execute("DELETE FROM team_task_events WHERE task_id = %s", (t["id"],))
        bot_db.execute("DELETE FROM team_task_matches WHERE task_id = %s", (t["id"],))
        bot_db.execute("DELETE FROM team_creative_tasks WHERE id = %s", (t["id"],))
        bot_db.execute(
            "DELETE FROM team_notifications WHERE object_type = 'task' AND object_id = %s",
            (str(t["id"]),))
    bot_db.execute("DELETE FROM team_project_themes WHERE id = %s", (theme["id"],))
    bot_db.execute("DELETE FROM team_task_matches WHERE ref = 'test-4001'")

    # ---------- 9. Перенос публікації руками, за лінком ----------
    # Випадок Олега: одну людину випадково нагородили двома однаковими
    # тасками, і в дубль уже встигли зарахувати одну новину.
    from handlers import team_matching
    dup = new_task(qty=10)          # помилковий дубль
    main_task = new_task(qty=10)    # правильне завдання
    # ref = id ноди, як його запише прогін — саме цей запис має переїхати
    team_matches.record_match(
        "site", "5001", "auto", task_id=dup["id"], person="Тест Тестова",
        project_id=999, confidence="high", title="Новина в дублі",
        url="https://nikvesti.com/news/politics/5001-a")
    check("зараховано в дубль", counted(dup["id"]) == 1)

    # Підміна БД сайту: лінк має розрішитись у справжню ноду
    class FakeDB:  # noqa: E306
        def is_configured(self):
            return True

        def query(self, sql, params=None):
            return [{"id": 5001, "type": "news", "category": "politics",
                     "status": 1, "owner_id": 42, "partner_project": 999,
                     "published": 1785000000, "slug_ua": "5001-a", "slug": None,
                     "title": "Новина в дублі", "content": "<p>Лід.</p>"}]

    orig_db = team_matching.db
    team_matching.db = FakeDB()
    try:
        match, touched = team_matching.attach_publication(
            main_task, "https://nikvesti.com/news/politics/5001-a", "Олег Деренюга")
        for tid in touched:
            team_matches.apply_progress(tid)
        check("публікація переїхала в правильне завдання",
              match and match["task_id"] == main_task["id"])
        check("у дублі її більше немає", counted(dup["id"]) == 0)
        check("а в правильному — рівно одна (не подвоїлась)",
              counted(main_task["id"]) == 1)
        check("запис на публікацію лишився ОДИН",
              len(bot_db.query(
                  "SELECT 1 FROM team_task_matches WHERE ref = '5001'")) == 1)

        again2, err = team_matching.attach_publication(
            main_task, "https://nikvesti.com/news/politics/5001-a", "Олег Деренюга")
        check("повторне зарахування того самого — зрозуміла відмова",
              again2 is None and "вже зарахована" in err)

        bad, err2 = team_matching.attach_publication(
            main_task, "https://example.com/щось", "Олег Деренюга")
        check("чужий лінк не зараховується", bad is None and "nikvesti" in err2)

        # Пост каналу теж можна зарахувати руками
        tg_match, tg_touched = team_matching.attach_publication(
            main_task, "https://t.me/nikvesti/82296", "Олег Деренюга")
        for tid in tg_touched:
            team_matches.apply_progress(tid)
        check("пост каналу теж зараховується за лінком",
              tg_match and tg_match["source"] == "telegram"
              and counted(main_task["id"]) == 2)
    finally:
        team_matching.db = orig_db
        bot_db.execute("DELETE FROM team_task_matches WHERE ref IN ('5001', '82296')")

    # ---------- 10. «Додати в чергу» вже зарахованої публікації ----------
    # Питання Олега: що буде, якщо кинути в чергу лінк, який уже десь
    # зарахований? Мовчки знімати не можна — у людини впав би прогрес без
    # пояснення. Тому спершу конфлікт, і лише з force — зняття.
    keeper = new_task(qty=2)
    team_matches.record_match(
        "site", "6001", "auto", task_id=keeper["id"], person="Тест Тестова",
        project_id=999, confidence="high", title="Уже зарахована",
        url="https://nikvesti.com/news/politics/6001-a")
    team_matching.db = FakeDB()   # той самий підмінений сайт, id не важливий
    try:
        m3, conflict = team_matching.queue_publication(
            "https://nikvesti.com/news/politics/6001-a", "Олег Деренюга")
        check("сама по собі публікація з черги не зникає",
              m3 is None and isinstance(conflict, dict)
              and conflict["conflict"] == "counted")
        check("конфлікт каже, кому і куди вона зарахована",
              conflict["person"] == "Тест Тестова"
              and conflict["theme_name"] == "Тестова тематика")
        check("і нічого не змінилось", counted(keeper["id"]) == 1)
        m4, touched4 = team_matching.queue_publication(
            "https://nikvesti.com/news/politics/6001-a", "Олег Деренюга", force=True)
        for tid in touched4:
            team_matches.apply_progress(tid)
        check("з підтвердженням — знялась і стала в чергу",
              m4 and m4["status"] == "pending" and counted(keeper["id"]) == 0)
    finally:
        team_matching.db = orig_db
        bot_db.execute("DELETE FROM team_task_matches WHERE ref = '6001'")

    cleanup()

    print("Облік виконання creative tasks (живий Postgres):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
