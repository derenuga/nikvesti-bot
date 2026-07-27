"""
Тест нативної кнопки «Назад» Telegram у Mini App «Команда» (webapp/app.js).

Навіщо (ревізія 27.07.2026): апка малювала свої кнопки «‹ Назад», але
апаратна кнопка Android і свайп-назад просто закривали її з будь-якого
екрана. Плюс вертикальний свайп під час перетягування проєкту згортав саму
апку — лікується tg.disableVerticalSwipes().

Кнопку веде статична мапа «екран → батько», а не стек історії: nav("project")
після збереження тематики клав би у стек зайвий запис, і нативна кнопка
розійшлася б із намальованою.

Запуск (потрібні playwright + chromium):
    python test_webapp_backbutton.py
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

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_orig": None}],
    "projects": [{
        "id": 7, "name": "Голоси Миколаєва", "partner": "IMS",
        "logo": None, "logo_orig": None, "start_date": 1750000000,
        "end_date": 1800000000, "kpi_news": 60, "kpi_articles": 10,
        "themes": [{"id": 1, "name": "Репортажі з сесій", "planned": 20, "format": "news"}],
        "deadlines": [], "drive_url": None,
    }],
    "tasks": [
        {"id": 101, "person": "Аліна Квітко", "creator": "Катерина Середа",
         "project_id": 7, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
         "platform": None, "type": "news", "theme_id": 1, "theme_name": "Репортажі з сесій",
         "qty": 2, "note": "", "deadline": None, "status": "open",
         "created_at": "2026-07-27T10:00:00+03:00", "done_at": None},
    ],
}

STUB = """
window.__back = { visible: false, handler: null, calls: [] };
window.__swipesDisabled = false;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(msg, cb){ cb(true); },
  disableVerticalSwipes(){ window.__swipesDisabled = true; },
  BackButton: {
    show(){ window.__back.visible = true; window.__back.calls.push("show"); },
    hide(){ window.__back.visible = false; window.__back.calls.push("hide"); },
    onClick(fn){ window.__back.handler = fn; },
  },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });
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
    await page.route("**/static/*", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
    await page.route("https://app.local/", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
    await page.add_init_script("window.BOOT = " + json.dumps(BOOTSTRAP) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _visible(page):
    return await page.evaluate("window.__back.visible")


async def _view(page):
    return await page.evaluate("STATE.view")


async def _press_back(page):
    await page.evaluate("window.__back.handler && window.__back.handler()")
    await page.wait_for_timeout(180)


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Нативна кнопка «Назад» Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            check("вертикальні свайпи заборонено (drag не згортає апку)",
                  await page.evaluate("window.__swipesDisabled"))
            check("обробник кнопки зареєстровано",
                  await page.evaluate("!!window.__back.handler"))
            check("на Головній кнопки немає", not await _visible(page))

            # Головна → трекер людини → назад
            await page.click("[data-tracker]")
            await page.wait_for_timeout(200)
            check("у трекері людини кнопка з'явилась", await _visible(page))
            await _press_back(page)
            check("назад із трекера → Головна", await _view(page) == "home")
            check("на Головній кнопку знову сховано", not await _visible(page))

            # Проєкти → проєкт → масова постановка → назад веде у ПРОЄКТ
            await page.click('[data-view="projects"]')
            await page.wait_for_timeout(150)
            check("у корені «Проєкти» кнопки немає", not await _visible(page))
            await page.click("[data-project]")
            await page.wait_for_timeout(200)
            check("у картці проєкту кнопка є", await _visible(page))
            await page.click("[data-bulk-theme]")
            await page.wait_for_timeout(250)
            check("у масовій постановці кнопка є", await _visible(page))
            await _press_back(page)
            check("назад із масової → картка проєкту", await _view(page) == "project")
            check("і саме той проєкт",
                  await page.evaluate("STATE.currentProject") == 7)
            await _press_back(page)
            check("назад із проєкту → список проєктів", await _view(page) == "projects")

            # Шторка: back закриває її, а не йде назад
            await page.click("[data-project]")
            await page.wait_for_timeout(200)
            await page.click("#add-theme")
            await page.wait_for_selector("#t-save")
            check("при відкритій шторці кнопка є", await _visible(page))
            await _press_back(page)
            check("back закрив шторку",
                  await page.evaluate('$("sheet-backdrop").classList.contains("hidden")'))
            check("але з екрана не пішов", await _view(page) == "project")

            # KPI більше не корінь меню — заходять із Головної, тож назад є куди
            await page.click('[data-view="home"]')
            await page.wait_for_selector('[data-nav="kpi"]')
            check("на Головній кнопки немає", not await _visible(page))
            await page.click('[data-nav="kpi"]')
            await page.wait_for_timeout(300)
            check("у налаштуваннях KPI кнопка є", await _visible(page))
            await page.click("#add-norm")
            await page.wait_for_selector("#n-save")
            await _press_back(page)
            check("back закрив шторку поверх KPI",
                  await page.evaluate('$("sheet-backdrop").classList.contains("hidden")'))
            check("з KPI ще є куди назад — кнопка лишилась", await _visible(page))
            await _press_back(page)
            check("назад із KPI → Головна", await _view(page) == "home")

            # Звітність — корінь меню, кнопки бути не має
            await page.click('[data-view="reports"]')
            await page.wait_for_timeout(250)
            check("у корені «Звітність» кнопки немає", not await _visible(page))

            # Форма таска: «+» → людина → форма → назад до вибору людини
            await page.click(".bn-plus")
            await page.wait_for_timeout(200)
            check("на виборі людини кнопка є", await _visible(page))
            await page.click("[data-person]")
            await page.wait_for_timeout(200)
            await _press_back(page)
            check("назад із форми → вибір людини", await _view(page) == "people")
            await _press_back(page)
            check("назад із вибору людини → Головна", await _view(page) == "home")
        finally:
            await browser.close()

    print()
    for name, okk in results:
        print(f"  {'✓' if okk else '✗'} {name}")
    if all(okk for _, okk in results):
        print("Усе зелено.")
        return 0
    print("Є падіння — див. ✗ вище.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
