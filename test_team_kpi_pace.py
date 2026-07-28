"""
Тест темпу періоду в KPI (handlers/team_kpi.py: period_pace / pace_row /
month_week_steps).

Живих БД не треба: підміняємо db.query і перевіряємо саму арифметику — ту, яку
на очі не перевіриш, а помилка в ній тиха й неприємна (людині показали червоне
першого числа — і вона повірила, що завалила місяць).

Що стереже:
- 1 СЕРПНЯ дає очікування 0 і стан «start», а не «behind»: 0% на початку
  періоду — це рівно за планом, і червоного там бути не може в принципі
  (це і є вихідна проблема, з якої модуль виріс);
- ЗАВЕРШЕНИЙ період дає share = 1, тож очікування = ціль і темп збігається зі
  старим відсотком виконання ЦИФРА В ЦИФРУ — історія («Звіт» за минулі періоди,
  помісячна динаміка) від появи темпу не змінюється;
- день, що триває, у минулі не рахується (вимагати денну норму о 9:00 — те саме
  червоне, від якого йдемо);
- ВІДПУСТКА зсуває і ціль, і темп: людина, що вийшла 17-го, не має виглядати
  безнадійною через дні, коли її не було;
- поріг «в темпі» (PACE_TOLERANCE) працює саме як поріг, а не як рівність;
- кроки тижнів ріжуть місяць по понеділках, обрізані тижні отримують меншу
  частку цілі, тиждень із самих вихідних кроком не стає, а стаття важить три
  новини так само, як у самій нормі;
- один запит на всі кроки (ліміти БД сайту), і кеш його не подвоює.

Запуск:  python test_team_kpi_pace.py
"""

import sys
from datetime import date

import handlers.team_kpi as kpi

FAILED = []


def check(name, cond):
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAILED.append(name)


def eq(name, got, want):
    check(f"{name} — {got!r}", got == want)


# ---------- period_pace ----------

print("Темп періоду:")

# Серпень 2026: 1-ше — субота, 21 робочий день
p1 = kpi.period_pace("month", 0, [], date(2026, 8, 1))
eq("1 серпня: минуло робочих днів", p1["elapsed"], 0)
eq("1 серпня: усього робочих днів", p1["total"], 21)
eq("1 серпня: фаза", p1["phase"], "start")

p3 = kpi.period_pace("month", 0, [], date(2026, 8, 3))   # понеділок
eq("3 серпня (перший робочий): минуло", p3["elapsed"], 0)
check("день, що триває, не зараховано наперед", p3["share"] == 0.0)

p14 = kpi.period_pace("month", 0, [], date(2026, 8, 14))
eq("14 серпня: минуло", p14["elapsed"], 9)
eq("14 серпня: лишилось", p14["left"], 12)
eq("14 серпня: фаза", p14["phase"], "mid")

p31 = kpi.period_pace("month", 0, [], date(2026, 8, 31))
eq("31 серпня: фаза", p31["phase"], "end")

prev = kpi.period_pace("month", -1, [], date(2026, 8, 14))
eq("завершений місяць: частка", prev["share"], 1.0)
eq("завершений місяць: не поточний", prev["is_current"], False)


# ---------- pace_row ----------

print("\nСтан відносно темпу:")

r = kpi.pace_row(0, 20, p1["share"])
eq("1 серпня, факт 0: очікування", r["expected"], 0)
eq("1 серпня, факт 0: стан", r["pace"], "start")
check("1 серпня, факт 0: відсотка темпу немає (нічого фарбувати)",
      r["pace_pct"] is None)

check("1 серпня це НЕ «відстає» — уся суть зміни", r["pace"] != "behind")

r0 = kpi.pace_row(2, 20, p1["share"])
eq("1 серпня, факт 2: стан", r0["pace"], "ahead")

rm = kpi.pace_row(0, 20, p14["share"])
eq("14 серпня, факт 0: очікування", rm["expected"], 9)
eq("14 серпня, факт 0: стан", rm["pace"], "behind")

eq("14 серпня, факт 9 (рівно очікування)",
   kpi.pace_row(9, 20, p14["share"])["pace"], "ahead")
eq("14 серпня, факт 8 (89% очікуваного) — ще в темпі",
   kpi.pace_row(8, 20, p14["share"])["pace"], "on")
eq("14 серпня, факт 7 (78%) — уже відстає",
   kpi.pace_row(7, 20, p14["share"])["pace"], "behind")
check("поріг саме 0.8, а не «на око»", kpi.PACE_TOLERANCE == 0.8)

eq("норму закрито достроково", kpi.pace_row(20, 20, p14["share"])["pace"], "done")
eq("понад норму — теж done", kpi.pace_row(25, 20, p14["share"])["pace"], "done")
check("без факту (немає БД сайту) стану немає",
      kpi.pace_row(None, 20, 0.5)["pace"] is None)
eq("ціль 0 (вся відпустка) з фактом — це робота понад вимоги",
   kpi.pace_row(3, 0, 1.0)["pace"], "done")

# --- регресія: історія не має поїхати ---
print("\nІсторія не змінюється (завершений період):")
for fact, target in ((15, 20), (7, 20), (20, 20), (1, 3)):
    old_pct = min(100, round(fact / target * 100))
    got = kpi.pace_row(fact, target, 1.0)
    eq(f"{fact}/{target}: темп = старому відсотку", got["pace_pct"], old_pct)
    eq(f"{fact}/{target}: очікування = ціль", got["expected"], target)


# ---------- відпустка ----------

print("\nВідпустка зсуває і темп:")
away = [{"start": "2026-08-03", "end": "2026-08-14", "title": "відпустка"}]
pa = kpi.period_pace("month", 0, away, date(2026, 8, 17))
eq("відпустка 3–14, сьогодні 17-те: доступних днів", pa["total"], 11)
eq("минуло доступних днів", pa["elapsed"], 0)
check("щойно з відпустки — очікування нульове, а не «пропустила пів місяця»",
      kpi.pace_row(0, 10, pa["share"])["pace"] == "start")

pa2 = kpi.period_pace("month", 0, away, date(2026, 8, 25))
eq("до 25-го відпрацьовано доступних днів", pa2["elapsed"], 6)


# ---------- кроки тижнів ----------

print("\nКроки тижнів місяця:")

QUERIES = []


def fake_query(sql, params=None):
    QUERIES.append((sql, params))
    # 2 новини 5 серпня, 1 стаття 12 серпня (важить 3 новини)
    def ts(d):
        from datetime import datetime
        return int(datetime(2026, 8, d, 12, tzinfo=kpi.KYIV_TZ).timestamp())
    return [
        {"published": ts(5), "type": "news", "own_material": 1},
        {"published": ts(5), "type": "news", "own_material": 1},
        {"published": ts(12), "type": "article", "own_material": 1},
    ]


kpi.db.query = fake_query
kpi._week_cache.clear()

norm = {"metric": "news", "own": True}
steps = kpi.month_week_steps(norm, 20, [], uid=42, today=date(2026, 8, 14))

eq("серпень 2026 ріжеться на тижнів", len(steps), 5)
check("тиждень із самих вихідних (1–2 серпня) кроком не стає",
      all(s["label"] != "1–2" for s in steps))
eq("перший крок — перший робочий тиждень", steps[0]["label"], "3–9")
eq("останній обрізаний місяцем", steps[-1]["label"], "31–31")
check("обрізаний тиждень отримує меншу частку цілі",
      steps[-1]["target"] < steps[0]["target"])
check("ціль тижня не нульова, поки в ньому є робочий день",
      all(s["target"] >= 1 for s in steps if not s["away"]))
eq("тиждень 3–9: факт 2 новини", steps[0]["fact"], 2)
eq("тиждень 10–16: стаття важить три новини", steps[1]["fact"], 3)
check("поточний тиждень позначено", steps[1]["is_current"])
check("майбутні тижні позначено як майбутні",
      steps[2]["future"] and steps[3]["future"])
check("минулі — не майбутні", not steps[0]["future"])

eq("на всі кроки — ОДИН запит у БД сайту", len(QUERIES), 1)
kpi.month_week_steps(norm, 20, [], uid=42, today=date(2026, 8, 14))
eq("повторний виклик іде з кешу", len(QUERIES), 1)

# норма на статті не множить статті на три (інакше потрійне зарахування)
kpi._week_cache.clear()
art = kpi.month_week_steps({"metric": "article", "own": True}, 3, [],
                           uid=42, today=date(2026, 8, 14))
eq("норма на статті: тиждень 10–16 — рівно одна стаття", art[1]["fact"], 1)

check("без users.id кроків немає (факт нізвідки взяти)",
      kpi.month_week_steps(norm, 20, [], uid=None) == [])


print()
if FAILED:
    print(f"❌ Провалено перевірок: {len(FAILED)}")
    for f in FAILED:
        print("   -", f)
    sys.exit(1)
print("Усе зелено.")
