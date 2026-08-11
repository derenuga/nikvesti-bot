"""
Картка перегляду умов — крок 3.2 docs/COMPENSATION_ADVISOR.md.

Цей модуль нічого не рахує заново. Він ЗВОДИТЬ те, що вже накопичили інші:
стабільність норми з team_kpi, вихід із nodes, зараховані проєктні завдання з
team_matches, відсутності з team_absences, квартальну оцінку з team_reviews,
медальки з impact_archive і дату ревізії з team_conditions. Розділення таке
саме, як team_matches (память) ↔ team_matching (логіка): team_conditions —
журнал, team_director — збірка.

Що на картці НЕ з'явиться
-------------------------
**Підсумкового бала.** Ні числа, ні відсотка, ні зірочок. Порахувати його з
цих даних неможливо технічно: у вимірах Каті немає спільної шкали, а обсяг і
перегляди свідомо не входять у формулу. Щойно число починає розподіляти гроші,
воно псується і псує те, що мало вимірювати.

**Рекомендації.** Чотири кнопки (підвищити / премія / лишити / поговорити)
скасовано Олегом 11.08: зберігаючи, ЩО саме вирішили, Нора наполовину стає
зарплатним реєстром. Лишається один факт — перегляд відбувся, ось дата.

**Рейтингу.** Картка завжди про ОДНУ людину; функції, яка шикує редакцію за
спаданням чогось, тут немає й не має з'явитись.

Головне правило показу: «не ставлять», а не нуль
------------------------------------------------
Newsroom проєктних завдань не отримує. Показати їм «0 із 0» означало б
похвалити за те, чого не було («жодного прострочення!»), а показати порожній
прочерк — докорити за те, чого не просили. Обидва варіанти брешуть, і другий
ще й робить картку Світлани біднішою за картку людини з Creative, яка працює
не краще (зауваження Олега 11.08).

Тому кожен блок несе `applies`, і рахується воно **ЗА ДАНИМИ, а не за списком
відділів у коді**: завдання «ставлять», якщо хоч комусь із цього відділу їх
хоч раз ставили; норма «є», якщо у відділу вона заведена. Якщо Катя завтра
почне ставити завдання Newsroom — блок з'явиться сам, без правки коду. Хардкод
`dept == "creative"` розійшовся б із реальністю першого ж дня.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from handlers import (bot_db, db, impact_archive, team_conditions, team_kpi,
                      team_matches, team_reviews, team_roster, team_tasks)

KYIV_TZ = ZoneInfo("Europe/Kiev")

# Скільки місяців дивимось назад. Пів року — горизонт спеки: менше не показує
# стабільності (один провал стає половиною картини), більше згадують гірше.
WINDOW_MONTHS = 6

# Скільки кварталів оцінки показуємо. Два — бо «що змінилось» потребує з чим
# порівняти, а третій квартал на екран уже не додає нічого.
QUARTERS = 2


def _window():
    """(перший день вікна, сьогодні) — вікно рівно WINDOW_MONTHS місяців назад,
    включно з поточним."""
    start, _ = team_kpi.period_bounds("month", -(WINDOW_MONTHS - 1))
    return start, datetime.now(KYIV_TZ).date()


def _depts_with_tasks():
    """Відділи, яким завдання ХОЧ РАЗ ставили. Саме так визначається «ставлять
    / не ставлять» — за фактом у даних, а не за списком відділів у коді."""
    people = {r["person"] for r in bot_db.query(
        "SELECT DISTINCT person FROM team_creative_tasks")}
    overrides = team_roster.dept_overrides()
    return {team_roster.effective_dept(p, overrides) for p in people
            if p in team_roster.ROSTER}


def _stability(person, months):
    """Скільки місяців із вікна норма закрита — це і є «стабільність».

    Беремо готову помісячну історію team_kpi: там уже враховані ставка,
    відсутності й вага статті ×3, тож картка не може розійтися з кружечком у
    «Звіті». Місяць вважається закритим, якщо ВСІ норми того місяця виконані."""
    hist = team_kpi.kpi_person_history(person, months=months)
    if not hist or not hist.get("has_norms"):
        return {"applies": False, "reason": "норм не ставили"}
    out = []
    for m in hist["months"]:
        norms = m.get("norms") or []
        out.append({
            "label": m["label"],
            "pct": m.get("overall_pct"),
            "done": bool(norms) and all(n.get("done") for n in norms),
            "empty": not norms,          # звільнена того місяця / вся у відпустці
        })
    counted = [m for m in out if not m["empty"]]
    return {
        "applies": True,
        "closed": sum(1 for m in counted if m["done"]),
        "of": len(counted),
        "months": out,
        "site_db": hist.get("site_db", True),
    }


def _output(person, start, today):
    """Вихід за вікно: усього матеріалів і скільки з них власних.

    Один запит у nodes через уже наявний _person_month_buckets — той самий, що
    малює помісячну динаміку, тож зайвого походу в БД сайту немає."""
    if not db.is_configured():
        return {"applies": False, "reason": "БД сайту недоступна"}
    uid = team_kpi.resolve_site_user_id(person)
    if not uid:
        return {"applies": False, "reason": "не знайшли акаунт на сайті"}
    buckets = team_kpi._person_month_buckets(uid, WINDOW_MONTHS)
    total = own = articles = 0
    for b in buckets.values():
        for (node_type, is_own), count in b.items():
            total += count
            if is_own:
                own += count
            if node_type == "article":
                articles += count
    return {"applies": True, "total": total, "own": own, "articles": articles}


def _tasks(person, start, dept_has_tasks):
    """Проєктні завдання за вікно: поставлено, зараховано, скільки прострочено.

    `applies=False` — це «завдань відділу не ставлять», і саме так воно й
    підписується. Нуль прострочень там, де завдань не буває, не заслуга."""
    if not dept_has_tasks:
        return {"applies": False, "reason": "завдань відділу не ставлять"}
    tasks = [t for t in team_tasks.list_tasks(person, done_days=400, limit=400)
             if (t.get("created_at") or "")[:10] >= start.isoformat()]
    if not tasks:
        return {"applies": True, "given": 0, "counted": 0, "late": 0, "open": 0}
    by_task = team_matches.counted_by_task([t["id"] for t in tasks])
    today = datetime.now(KYIV_TZ).date().isoformat()
    late = sum(1 for t in tasks
               if t.get("deadline") and t["status"] == "open" and t["deadline"] < today)
    return {
        "applies": True,
        "given": len(tasks),
        "counted": sum(1 for t in tasks if t["status"] == "done"),
        "open": sum(1 for t in tasks if t["status"] == "open"),
        "late": late,
        "publications": sum(len(v) for v in by_task.values()),
    }


def _absences(person, start, today):
    """Пропущені робочі дні за вікно — щоб «6 із 6» читалось чесно: цілі вже
    були знижені, і людина закривала знижену норму, а не повну."""
    items = [a for a in team_kpi.list_absences(person) if a["end"] >= start.isoformat()]
    days = 0
    for a in items:
        lo = max(start, date.fromisoformat(a["start"]))
        hi = min(today, date.fromisoformat(a["end"]))
        if lo <= hi:
            # _workdays бере ВЕРХНЮ МЕЖУ ВИКЛЮЧНО, а в team_absences end —
            # останній день відсутності включно, звідси +1 день
            days += team_kpi._workdays(lo, hi + timedelta(days=1))
    return {"days": days, "items": items}


def _quality(person):
    """Оцінка Каті у ГОТОВОМУ до показу вигляді: назва виміру замість ключа,
    людський підпис кварталу замість «2026-Q3». Мапити це у фронті означало б
    тримати там другу копію словника вимірів, яка розійдеться при першій же
    зміні формулювання."""
    titles = {d["key"]: d["title"] for d in team_reviews.DIMENSIONS}
    out = []
    for r in team_reviews.person_history(person, quarters=QUARTERS):
        year, quarter = r["period"].split("-Q")
        out.append({
            "period": team_reviews.quarter_label(int(year), int(quarter)),
            "rows": [{"title": titles.get(k, k), "value": v}
                     for k, v in r["labels"].items() if v],
            "note": r["note"],
        })
    return out


def card(person):
    """Повна картка людини. Нічого не пише."""
    if person not in team_roster.ROSTER:
        return None
    team_conditions.ensure_conditions_schema()
    team_tasks.ensure_team_schema()
    info = team_roster.ROSTER[person]
    dept = team_roster.effective_dept(person)
    start, today = _window()
    dept_has_tasks = dept in _depts_with_tasks()

    cond = team_conditions.history(person)
    last = cond[0] if cond else None
    gap_months = (team_conditions.months_between(date.fromisoformat(last["date"]), today)
                  if last else None)

    return {
        "person": person,
        "dept": dept,
        "dept_title": team_roster.DEPT_TITLES.get(dept, dept),
        "manager": info["manager"],
        "period": {
            "start": start.isoformat(),
            "end": today.isoformat(),
            "months": WINDOW_MONTHS,
            "label": f"{team_kpi.MONTHS_UA[start.month - 1]} — "
                     f"{team_kpi.MONTHS_UA[today.month - 1]} {today.year}",
        },
        "stability": _stability(person, WINDOW_MONTHS),
        "output": _output(person, start, today),
        "tasks": _tasks(person, start, dept_has_tasks),
        "absences": _absences(person, start, today),
        "quality": _quality(person),
        "impacts": impact_archive.list_impacts_for(person),
        "conditions": {
            "last": last,
            "months": gap_months,
            "gap": team_conditions.human_gap(gap_months) if gap_months is not None else None,
            "count": len(cond),
            "history": cond[:6],
        },
    }
