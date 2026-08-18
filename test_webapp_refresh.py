"""
Тест тихого фонового оновлення Mini App (webapp/app.js).

Навіщо. Апка підтягує зміни колеги, коли людина повертається у вкладку
(visibilitychange). Робила вона це повним re-render'ом, а екрани, які тягнуть
свої дані, починають із каркаса зі скелетонами — тому заповнений екран на очах
стирався в сірі плашки і збирався наново. Олег (18.08) прочитав це рівно так,
як воно виглядає: «при возвращении во вкладку перезагрузка запускается, бесит
страшно». На екрані дублів, де запит тоді думав пів хвилини, сірі плашки
висіли всі ці пів хвилини.

Дані підтягувати треба, видовище — ні. Тому каркас малюється лише при
звичайному переході, а у фоновому проході старий вміст лишається на місці,
поки не приїдуть свіжі дані.

Що стереже цей тест:

- повернення у вкладку НЕ стирає заповнений екран у скелетони;
- свіжі дані все одно приїжджають і перемальовують екран;
- звичайний перехід на екран, даних для якого ще немає, скелетони показує —
  інакше замість «тихо» ми зробили б «мертво».

Запуск (потрібні playwright + chromium):
    python test_webapp_refresh.py
"""

import asyncio
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

ME = {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
      "dept_title": "Адміністративний", "manager": True}

BOOT = {"me": ME, "site_db": True, "nora": True, "people": [], "projects": [],
        "assignees": [], "managers": [], "tasks": []}

QUEUE = {"items": [], "total": 0, "offset": 0,
         "facets": [{"key": "all", "label": "Усе", "n": 0}],
         "bounds": {"from": 1780000000, "to": 1785000000, "articles": 900},
         "dupes": 1, "query": "", "matched": [],
         "shadow": {"pending": 0, "noise": 0, "good": 0}}


def _side(cid, title):
    return {"id": cid, "cls": "open", "state": "Строку не дали", "how": None,
            "cheap": False, "title": title, "who": {"promiser": "Віталій Луков",
            "role": None, "reported": None, "owner": None, "hidden": False},
            "quote": "Цитата " + title, "populism": None, "meta": ["1 ревізія"],
            "found": None, "fresh": False, "topic_id": None, "image": None,
            "link": {"url": "https://nikvesti.com/news/x/1-a", "title": "Новина",
                     "date": "31.07.2026"}}


DUPES_FIRST = {"total": 1, "pairs": [{
    "sim": 0.37, "keep": 2352, "why": "стадія і результат однієї справи",
    "sure": True, "a": _side(2352, "ПЕРШИЙ ПОКАЗ"), "b": _side(2351, "ПКД")}]}

DUPES_FRESH = {"total": 1, "pairs": [{
    "sim": 0.37, "keep": 2352, "why": "стадія і результат однієї справи",
    "sure": True, "a": _side(2352, "СВІЖІ ДАНІ"), "b": _side(2351, "ПКД")}]}

STUB = """
window.__delay = 0;
window.DUPES = window.DUPES_FIRST;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  // Гальмуємо ЛИШЕ запит екрана: якби гальмувався й bootstrap, перевірка
  // встигала б спрацювати ще до того, як апка взагалі почне малювати.
  if (url.startsWith("/api/promises/dupes")) {
    if (window.__delay) await new Promise((r) => setTimeout(r, window.__delay));
    return json(window.DUPES);
  }
  if (url.startsWith("/api/promises/triage")) return json(
    { items: [], offset: 0, counts: { pending: 0, noise: 0, good: 0, by_kind: {} } });
  if (url.startsWith("/api/promises")) return json(window.QUEUE);
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  if (url.startsWith("/api/kpi")) return json({ norms: [], week_label: "т",
    month_label: "серпень 2026", site_db: true });
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    # Живий telegram-web-app.js затирає заглушку window.Telegram — і апка
    # показала б екран помилки (урок 15.08, усі браузерні тести в CI).
    await page.route("https://telegram.org/**", lambda r: asyncio.ensure_future(
        r.fulfill(status=200, content_type="application/javascript", body="")))
    await page.route("**/static/*", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
    await page.route("https://app.local/", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
    await page.add_init_script(
        "window.BOOT = " + json.dumps(BOOT) + ";"
        "window.QUEUE = " + json.dumps(QUEUE) + ";"
        "window.DUPES_FIRST = " + json.dumps(DUPES_FIRST) + ";"
        "window.DUPES_FRESH = " + json.dumps(DUPES_FRESH) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _return_to_tab(page):
    """Повернення у вкладку: годинник переводимо вперед, бо фонове оновлення
    має власний мінімальний інтервал і одразу після завантаження мовчить."""
    await page.evaluate("""() => {
      const t0 = Date.now();
      Date.now = () => t0 + 60000;
      document.dispatchEvent(new Event("visibilitychange"));
    }""")


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0
    if not _chromium_path():
        print("chromium не знайдено — тест пропущено (playwright install chromium)")
        return 0

    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail else ""))

    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            print("\nЕкран дублів, повернення у вкладку:")
            await page.evaluate("nav('dupes')")
            await page.wait_for_selector(".dp-pair", timeout=5000)
            check("екран намальовано даними", "ПЕРШИЙ ПОКАЗ" in await page.inner_text("#content"))

            # Наступний запит навмисно повільний: якби каркас малювався, сірі
            # плашки висіли б ці 400 мс — саме те, що бачив Олег.
            await page.evaluate("window.__delay = 400; window.DUPES = window.DUPES_FRESH;")
            await _return_to_tab(page)
            await page.wait_for_timeout(150)
            sk = await page.evaluate("() => document.querySelectorAll('#content .sk').length")
            body = await page.inner_text("#content")
            check("скелетонів не з'явилось", sk == 0, f"знайшов {sk}")
            check("старий вміст лишився на місці, поки їдуть свіжі дані",
                  "ПЕРШИЙ ПОКАЗ" in body)

            await page.wait_for_function(
                "() => document.querySelector('#content').innerText.includes('СВІЖІ ДАНІ')",
                timeout=5000)
            check("свіжі дані все одно приїхали і перемалювали екран", True)

            print("\nЗвичайний перехід (даних ще немає):")
            await page.evaluate("nav('promises')")
            await page.wait_for_timeout(50)
            await page.evaluate("nav('dupes')")   # вхід заново — кеш скинуто
            await page.wait_for_timeout(120)
            sk2 = await page.evaluate("() => document.querySelectorAll('#content .sk').length")
            check("скелетони показуються, як і раніше", sk2 > 0, f"знайшов {sk2}")
        finally:
            await browser.close()

    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} перевірок пройдено")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
