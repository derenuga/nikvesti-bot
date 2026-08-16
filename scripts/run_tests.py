#!/usr/bin/env python3
"""
Прогін усіх перевірок бота однією командою.

    python scripts/run_tests.py            # усе, що можна прогнати без живих БД
    python scripts/run_tests.py pace kpi   # лише тести, чиї імена містять слова
    python scripts/run_tests.py --list     # що є і що де вимагається

**Навіщо це існує.** Тестів у репо чотири десятки, вони не pytest-сумісні
(кожен — самостійний скрипт із `__main__`), і запустити їх можна було лише
по одному, знаючи ім'я. Тому їх не ганяв ніхто, і за пів місяця шість
перевірок Mini App розійшлись із самою апкою мовчки: одна вимагала кольору,
який Олег скасував, друга протухла по календарю, три впирались у екрани, що
переїхали. Червоне, на яке ніхто не дивиться, гірше за відсутність тестів —
воно створює відчуття, що перевірка є.

**Головне правило цього файлу: мовчазних пропусків не буває.** Тест, який не
запускався, друкується окремим списком і з причиною. Саме на цьому місці
дірка й зяяла: 18 браузерних тестів апки при відсутньому playwright виходили
з кодом 0 і словами «тест пропущено», тобто будь-який CI був би вічнозеленим,
не перевіривши жодного екрана. Тому playwright тут не «як пощастить»: якщо
його немає, а тести апки просили прогнати — це ПОМИЛКА, а не пропуск.

Що не запускається тут і чому — нижче в NEEDS_NORA / NEEDS_LIVE_API. Це не
«зламані» тести: їм потрібні живий Postgres нори або токен Facebook, тобто
доступи, яких у CI немає й не має бути.
"""
import concurrent.futures
import os
import pathlib
import signal
import subprocess
import sys
import time

# `python scripts/run_tests.py | head` не має закінчуватись простирадлом
# трейсбека замість виводу
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Тести, яким потрібна жива нора (Postgres бота). Ганяти локально з
# BOT_DATABASE_URL у середовищі — вони справжні, просто не для CI.
NEEDS_NORA = {
    "test_archive_excerpt": "витяг цитат рахує сам Postgres (ts_headline)",
    "test_entity_backfill_filter": "відбір статей для батчів — запит у нору",
    "test_entity_increment": "самозаліковність інкремента перевіряється на даних",
    "test_entity_junk": "масштаб сміття рахується в норі",
    "test_entity_roles": "нормалізація ролі звіряється SQL-функцією і Python",
    "test_matching_retro": "ретро-прогін матчера ходить у team_task_matches",
    "test_matching_scan": "щогодинна звірка ходить у team_task_matches",
    "test_promise_eval": "умова приймання банку тем жене живий витяг",
    "test_promises": "банк тем лежить у норі",
    "test_team_absences": "відсутності знижують цілі — рахунок у норі",
    "test_team_matches": "памʼять зарахованого виконання — таблиця нори",
    "test_team_notifications": "стрічка сповіщень — таблиці нори",
    "test_team_tg_match": "матчі за дисклеймером пишуться в нору",
}

# Тести, яким потрібні живі токени зовнішніх сервісів.
NEEDS_LIVE_API = {
    "test_fb_post": "потрібен живий FACEBOOK_PAGE_TOKEN",
    "test_fb_stat": "потрібен живий FACEBOOK_PAGE_TOKEN",
}

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "300"))
WORKERS = int(os.environ.get("TEST_WORKERS", "4"))


def needs_browser(name):
    """Браузерні тести — усі webapp-ні. Ознака за іменем, а не за списком:
    новий екран апки має потрапляти в прогін сам, без правки цього файлу."""
    return name.startswith("test_webapp_")


def have_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


def future_dates(horizon=120):
    """Майбутні дати, вписані у фікстури ЧИСЛОМ, — з відліком до спрацювання.

    Це не педантизм. 16.08.2026 прогін на main упав однією перевіркою: у
    фікстурі стояв строк звіту «2026-08-15», і за ніч він з майбутнього став
    минулим. Код при цьому ніхто не чіпав. Такі дати видно ЗАЗДАЛЕГІДЬ — тож
    хай runner каже про них до вибуху, а не після.
    """
    import datetime
    import re

    today = datetime.date.today()
    found = []
    for path in sorted(ROOT.glob("test_*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in re.finditer(r'"(20\d\d)-(\d\d)-(\d\d)"', line):
                try:
                    d = datetime.date(*map(int, m.groups()))
                except ValueError:
                    continue
                days = (d - today).days
                if 0 <= days <= horizon:
                    found.append((path.name, n, d.isoformat(), days))
    return sorted(found, key=lambda x: x[3])


def run_one(path):
    started = time.monotonic()
    try:
        p = subprocess.run([sys.executable, path.name], cwd=ROOT, timeout=TIMEOUT,
                           capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        return path.stem, p.returncode, out, time.monotonic() - started
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return path.stem, 124, out + f"\n⏱ не вклався у {TIMEOUT} с", time.monotonic() - started


def main(argv):
    if "--list" in argv:
        for path in sorted(ROOT.glob("test_*.py")):
            why = NEEDS_NORA.get(path.stem) or NEEDS_LIVE_API.get(path.stem)
            mark = "браузер" if needs_browser(path.stem) else ("—" if not why else why)
            print(f"  {path.stem:32} {mark}")
        return 0

    words = [a for a in argv if not a.startswith("-")]
    all_tests = sorted(ROOT.glob("test_*.py"))
    if words:
        all_tests = [p for p in all_tests if any(w in p.stem for w in words)]

    skipped, to_run = [], []
    for path in all_tests:
        why = NEEDS_NORA.get(path.stem)
        if why and not os.environ.get("BOT_DATABASE_URL"):
            skipped.append((path.stem, f"потрібна нора: {why}"))
            continue
        why = NEEDS_LIVE_API.get(path.stem)
        if why and not os.environ.get("FACEBOOK_PAGE_TOKEN"):
            skipped.append((path.stem, why))
            continue
        to_run.append(path)

    # Браузер: не «як пощастить». Якщо тести апки в наборі, а playwright немає —
    # це помилка прогону, інакше зелений результат нічого не означає.
    browser_tests = [p for p in to_run if needs_browser(p.stem)]
    if browser_tests and not have_playwright():
        print("❌ У наборі є браузерні тести апки, а playwright не встановлено.")
        print("   Це не привід їх пропустити — без них апку не перевіряє ніхто.")
        print("   Постав: pip install playwright && playwright install chromium")
        print(f"   (або прибери їх із набору: {len(browser_tests)} шт.)")
        return 1
    if browser_tests:
        found = chromium_path()
        if found:
            os.environ.setdefault("CHROMIUM_PATH", found)
        print(f"Браузер: {found or 'типовий playwright-івський'}")

    print(f"Прогін {len(to_run)} тестів, по {WORKERS} паралельно "
          f"(стеля {TIMEOUT} с на тест)\n")

    failed, passed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, code, out, secs in pool.map(run_one, to_run):
            last = next((ln for ln in reversed(out.strip().splitlines()) if ln.strip()), "")
            if code == 0:
                passed.append(name)
                print(f"  ✅ {name:32} {secs:5.1f}с  {last[:60]}")
            else:
                failed.append((name, out))
                print(f"  ❌ {name:32} {secs:5.1f}с  {last[:60]}")

    if skipped:
        print(f"\nНе запускались ({len(skipped)}) — доступів немає, це не падіння:")
        for name, why in skipped:
            print(f"  ∅ {name:32} {why}")

    mines = future_dates()
    if mines:
        print(f"\nМіни ({len(mines)}) — майбутні дати, вписані числом. Спрацюють, "
              f"коли настануть:")
        for name, line, when, days in mines[:10]:
            print(f"  ⏳ {name}:{line} {when} — через {days} дн.")
        print("  Лікується так само: рахувати від сьогодні, а не вписувати число.")

    if failed:
        print(f"\n{'=' * 70}\nВивід тих, що впали:\n")
        for name, out in failed:
            print(f"----- {name} " + "-" * (64 - len(name)))
            lines = out.strip().splitlines()
            # Спершу САМІ червоні рядки, і тільки потім хвіст. Інакше тест на
            # 85 перевірок, де впала одна, показує в CI лише останні тридцять
            # зелених і підсумок «84/85» — тобто вимагає окремого заходу, щоб
            # дізнатись, ЩО не так (перший же прогін у GitHub, 15.08).
            bad = [ln for ln in lines if "❌" in ln or ln.strip().startswith("✗")]
            if bad:
                print("Червоні перевірки:")
                for ln in bad[:20]:
                    print(f"  {ln.strip()}")
                print()
            print("\n".join(lines[-30:]))
            print()

    print(f"\nПройшло {len(passed)}, впало {len(failed)}, пропущено {len(skipped)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
