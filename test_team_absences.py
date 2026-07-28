"""
Тест відсутностей (відпустка/лікарняна/відрядження) і перерахунку цілей KPI
на ЖИВОМУ Postgres (handlers/team_kpi.py).

Питання Олега: «як поставити, що Аліна була у відпустці з 8 по 17 липня, щоб
їй перерахувалось?». Раніше було лише «звільнити від норми на весь період» —
для десяти днів усередині місяця це неправда: обнуляло б увесь місяць.

Модель: відсутність — факт про ЛЮДИНУ (а не про окрему норму), і вона
пропорційно знижує цілі в КОЖНОМУ періоді, якого торкається. Рахуємо по
робочих днях (пн–пт): тиждень відпустки має занулити тижневу ціль, а не
лишити «дві сьомих» за вихідні.

Що стереже:
- арифметику робочих днів і частку періоду;
- реальний випадок: 8–17 липня 2026 у місячній нормі 20 → ціль 13;
- тиждень, повністю накритий відпусткою → ціль 0 (людина зникає зі «Звіту»);
- ручна правка сильніша за автоперерахунок;
- відсутність поза періодом нічого не чіпає;
- видалення запису повертає ціль на місце.

Запуск:
    BOT_DATABASE_URL=postgresql://... python test_team_absences.py
"""

import os
import sys
from datetime import date

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, team_kpi   # noqa: E402

RESULTS = []
ALINA = "Аліна Квітко"


def check(name, cond):
    RESULTS.append((name, bool(cond)))


def cleanup():
    bot_db.execute("DELETE FROM team_absences WHERE person = %s", (ALINA,))


def main():
    team_kpi.ensure_kpi_schema()
    cleanup()

    # ---------- 1. Робочі дні ----------
    july = (date(2026, 7, 1), date(2026, 8, 1))
    check("робочих днів у липні 2026 — 23", team_kpi._workdays(*july) == 23)
    check("вихідні не рахуються",
          team_kpi._workdays(date(2026, 7, 4), date(2026, 7, 6)) == 0)

    # ---------- 2. Випадок Олега: 8–17 липня ----------
    team_kpi.add_absence("Олег Деренюга", ALINA, "2026-07-08", "2026-07-17")
    absences = team_kpi.list_absences(ALINA)
    check("відсутність записана", len(absences) == 1
          and absences[0]["title"] == "відпустка")
    factor, missed = team_kpi.absence_factor(ALINA, *july, absences)
    check("пропущено 8 робочих днів (8–17 липня)", missed == 8)
    check("частка місяця — 15 із 23", abs(factor - 15 / 23) < 1e-9)
    check("місячна ціль 20 стала 13", team_kpi.scaled_target(20, factor) == 13)
    check("а ціль 60 стала 39", team_kpi.scaled_target(60, factor) == 39)

    # ---------- 3. Тиждень цілком у відпустці ----------
    week = (date(2026, 7, 13), date(2026, 7, 20))   # пн 13 — нд 19
    f_week, missed_week = team_kpi.absence_factor(ALINA, *week, absences)
    check("тиждень 13–19 пропущено повністю (5 робочих днів)", missed_week == 5)
    check("частка тижня — нуль", f_week == 0)
    check("тижнева ціль 5 стала 0 — людина зникає зі «Звіту»",
          team_kpi.scaled_target(5, f_week) == 0)

    # Частковий тиждень: відпустка по 17-те, тож 17-те ще пропущене, а
    # наступний тиждень (20–26) — уже робочий
    next_week = (date(2026, 7, 20), date(2026, 7, 27))
    f_next, missed_next = team_kpi.absence_factor(ALINA, *next_week, absences)
    check("наступний тиждень не зачеплений", missed_next == 0 and f_next == 1.0)

    # ---------- 4. Відсутність в іншому місяці ----------
    june = (date(2026, 6, 1), date(2026, 7, 1))
    f_june, missed_june = team_kpi.absence_factor(ALINA, *june, absences)
    check("червня відпустка не торкається", missed_june == 0 and f_june == 1.0)

    # ---------- 5. Один робочий день, що лишився ----------
    # Ціль не має падати в нуль, поки людина хоч трохи працювала: «0 із 0»
    # виглядало б як виконана норма
    check("залишок часу дає щонайменше 1", team_kpi.scaled_target(20, 0.02) == 1)

    # ---------- 6. Видалення повертає все як було ----------
    team_kpi.delete_absence(absences[0]["id"])
    f_after, missed_after = team_kpi.absence_factor(ALINA, *july)
    check("після видалення ціль знову повна",
          missed_after == 0 and team_kpi.scaled_target(20, f_after) == 20)

    cleanup()

    print("Відсутності і перерахунок цілей (живий Postgres):")
    ok = 0
    for name, passed in RESULTS:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(RESULTS)} перевірок пройдено")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
