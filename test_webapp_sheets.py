"""
Регресійний тест шторок Mini App «Команда» (webapp/app.js).

Ловить баг, через який шторки псували дані в проді (знайдено 27.07.2026):
taskSheet вішав click-слухач на #sheet і ніколи його не знімав — closeSheet
лише ховає бекдроп. Кожна колись відкрита шторка спрацьовувала знову, тож
«Виконано» на одній тасці мовчки закривало ще й усі таски, чиї картки
відкривали раніше в цій сесії (по одному зайвому PATCH на кожне відкриття).
Тост при цьому показував успіх — баг було видно лише як «я цього не закривала».

Заодно перевіряє пікер проєкту: він мав { once: true }, і слухач знімався від
будь-якого кліку в шторці — тап по заголовку робив пікер мертвим.

Запуск (потрібні playwright + chromium):
    pip install playwright
    python test_webapp_sheets.py

Мережі й БД не треба: Telegram.WebApp і fetch застабані фікстурами.
"""

import asyncio
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

# Перший знайдений chromium: env → шлях Railway/CI → дефолт playwright
CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True,
    "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_orig": None}],
    "projects": [{"id": 7, "name": "Голоси Миколаєва", "partner": "IMS",
                  "logo": None, "logo_orig": None, "start_date": None,
                  "end_date": None, "kpi_news": 0, "kpi_articles": 0,
                  "themes": [], "deadlines": [], "drive_url": None}],
    "tasks": [
        {"id": 101, "person": "Аліна Квітко", "creator": "Катерина Середа",
         "project_id": None, "project_name": None, "partner_name": None,
         "platform": None, "type": "news", "theme_id": None, "theme_name": "Сесії",
         "qty": 2, "note": "", "deadline": None, "status": "open",
         "created_at": "2026-07-27T10:00:00+03:00", "done_at": None},
        {"id": 202, "person": "Аліна Квітко", "creator": "Катерина Середа",
         "project_id": None, "project_name": None, "partner_name": None,
         "platform": None, "type": "article", "theme_id": None, "theme_name": "Екологія",
         "qty": 1, "note": "", "deadline": None, "status": "open",
         "created_at": "2026-07-27T11:00:00+03:00", "done_at": None},
    ],
}

STUB = """
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  HapticFeedback: { notificationOccurred(){} } } };
window.__patches = [];
window.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  if (method === "PATCH") window.__patches.push(url);
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });
  return json({ ok: true, task: {} });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None  # хай playwright шукає сам


async def _open_app(pw):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    await page.route("**/static/*", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
    await page.route("https://app.local/", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
    await page.add_init_script("window.BOOT = " + json.dumps(BOOTSTRAP) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _reopen(page):
    """Перезапустити апку так, як її бачить людина, що зайшла ЗАНОВО.

    З 11.08 апка памʼятає останній екран і після перезапуску повертає саме
    його (rememberView/restoreView у app.js): Telegram піднімає WebView сам —
    зміна теми, повернення з фону, «Reload» у меню, — і доти будь-що з цього
    викидало з картки людини на Головну. Тест же користується reload як
    скиданням стану, тому памʼять треба знести явно; інакше після reload ми
    лишаємось у трекері Аліни, Головної не бачимо, і тест падає на пошуку
    рядка людини — не полагодивши нічого й нічого не зламавши."""
    await page.evaluate("try { localStorage.removeItem('team.lastView'); } catch (e) {}")
    await page.reload()
    await page.wait_for_selector("#screen-main:not(.hidden)")


async def test_sheet_listener_leak(page, opens):
    """Відкриваємо/закриваємо картку таска 101 `opens` разів, потім у картці
    таска 202 тиснемо «Виконано». Має полетіти РІВНО один PATCH — на 202."""
    await page.click("[data-tracker]")
    await page.wait_for_selector("[data-task]")
    await page.evaluate("window.__patches = []")

    for _ in range(opens):
        await page.click('[data-task="101"]')
        await page.wait_for_selector("#sheet h2")
        await page.mouse.click(195, 60)          # тап по бекдропу — закрити
        await page.wait_for_timeout(120)

    await page.click('[data-task="202"]')
    await page.wait_for_selector('[data-status="done"]')
    await page.click('[data-status="done"]')
    await page.wait_for_timeout(700)

    patches = await page.evaluate("window.__patches")
    ok = patches == ["/api/tasks/202"]
    print(f"  {'✓' if ok else '✗'} шторок відкрито {opens}, PATCH-ів: "
          f"{len(patches)} {patches if not ok else ''}")
    return ok


async def test_project_picker_after_stray_tap(page):
    """Тап по заголовку шторки не має вбивати пікер проєкту."""
    await page.click('.bn-plus')
    await page.wait_for_selector("[data-person]")
    await page.click("[data-person]")
    await page.wait_for_selector("#f-project")
    await page.click("#f-project")
    await page.wait_for_selector('[data-proj]')
    await page.click("#sheet h2")               # «промазали» по заголовку
    await page.wait_for_timeout(120)
    await page.click('[data-proj="7"]')         # тепер обираємо проєкт
    await page.wait_for_timeout(250)
    label = await page.inner_text("#f-project")
    ok = "Голоси Миколаєва" in label
    print(f"  {'✓' if ok else '✗'} пікер проєкту після тапу по заголовку "
          f"(обрано: {label.strip()!r})")
    return ok


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    print("Регресія шторок Mini App «Команда»:")
    results = []
    async with async_playwright() as pw:
        browser, page = await _open_app(pw)
        try:
            results.append(await test_sheet_listener_leak(page, opens=1))
            await _reopen(page)
            results.append(await test_sheet_listener_leak(page, opens=4))
            await _reopen(page)
            results.append(await test_project_picker_after_stray_tap(page))
        finally:
            await browser.close()

    if all(results):
        print("Усе зелено.")
        return 0
    print("Є падіння — див. ✗ вище.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
