"""
Тест пошуку по проєктах у Mini App «Команда» (webapp/app.js).

Навіщо (ревізія 27.07.2026): проєктів уже ~40, і догортати до потрібного
донора — найдовша дія в апці. Пошук є у списку проєктів і в пікері форми
таска; матчиться і донор, і назва.

Дві речі, які легко зламати таким полем і які тест стереже:
  1. ФОКУС. Список перемальовується на кожній літері. Якщо перемалювати
     разом із полем — фокус злітає і клавіатура на телефоні закривається.
  2. ПОРЯДОК ПРОЄКТІВ. Перетягування пише в Нору ВЕСЬ список. Якщо лишити
     drag активним при фільтрі, Катя перетягне щось у списку з трьох
     знайдених — і порядок сорока проєктів заміниться на порядок трьох,
     тобто решта просто зникне з таблиці порядку.

Запуск (потрібні playwright + chromium):
    python test_webapp_search.py
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

DONORS = ["IMS", "IWPR", "Fondation Hirondelle", "NED", "UNDP",
          "Internews", "Deutsche Welle", "Посольство Швеції"]

PROJECTS = [
    {"id": i, "name": f"Проєкт {i}", "partner": DONORS[i % len(DONORS)],
     "logo": None, "logo_orig": None, "start_date": 1750000000,
     "end_date": 1800000000, "kpi_news": 10, "kpi_articles": 2,
     "themes": [], "deadlines": [], "drive_url": None}
    for i in range(1, 41)
]
PROJECTS[0]["name"] = "Голоси Миколаєва"
PROJECTS[0]["partner"] = "IMS"

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_sm": None, "photo_orig": None}],
    "projects": PROJECTS,
    "tasks": [],
}

STUB = """
window.__orderPuts = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(m, c){ c(true); }, disableVerticalSwipes(){},
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/projects/order")
    window.__orderPuts.push(JSON.parse(opts.body).ids);
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


async def _long_press_drag(page, selector, dy):
    """Довгий натиск (350 мс) і протягування — як реальний жест Каті."""
    box = await page.locator(selector).first.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.wait_for_timeout(500)          # чекаємо активації drag
    for step in range(1, 6):
        await page.mouse.move(x, y + dy * step / 5)
        await page.wait_for_timeout(40)
    await page.mouse.up()
    await page.wait_for_timeout(400)


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Пошук по проєктах у Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            await page.click('[data-view="projects"]')
            await page.wait_for_selector("#proj-q")
            total = await page.locator("[data-project]").count()
            check("без фільтра видно всі 40 проєктів", total == 40)

            # --- фільтрація ---
            await page.fill("#proj-q", "IWPR")
            await page.wait_for_timeout(250)
            shown = await page.locator("[data-project]").count()
            check(f"пошук «IWPR» звузив список (лишилось {shown})", 0 < shown < 40)
            check("у видимих рядках лише IWPR",
                  "IWPR" in await page.inner_text("#proj-list")
                  and "UNDP" not in await page.inner_text("#proj-list"))

            await page.fill("#proj-q", "миколаєва")
            await page.wait_for_timeout(250)
            check("пошук за НАЗВОЮ, не лише донором, і без регістру",
                  await page.locator("[data-project]").count() == 1
                  and "Голоси Миколаєва" in await page.inner_text("#proj-list"))

            await page.fill("#proj-q", "щось чого немає")
            await page.wait_for_timeout(250)
            check("порожній результат пояснює себе",
                  "Нічого не знайшлось" in await page.inner_text("#proj-body"))

            # --- фокус не злітає при наборі ---
            await page.fill("#proj-q", "")
            await page.click("#proj-q")
            for ch in "IMS":
                await page.keyboard.type(ch)
                await page.wait_for_timeout(120)
            focused = await page.evaluate("document.activeElement && document.activeElement.id")
            check("фокус лишається в полі після набору", focused == "proj-q")
            check("і текст у полі повний",
                  await page.input_value("#proj-q") == "IMS")

            # --- ГОЛОВНЕ: перетягування при фільтрі не псує порядок ---
            await page.evaluate("window.__orderPuts = []")
            await _long_press_drag(page, "[data-drag-id]", 120)
            check("при активному фільтрі порядок НЕ зберігається",
                  await page.evaluate("window.__orderPuts.length") == 0)
            check("підказка пояснює, чому",
                  "очисти пошук" in await page.inner_text("#proj-body"))

            # --- на повному списку перетягування працює як раніше ---
            await page.fill("#proj-q", "")
            await page.wait_for_timeout(250)
            await page.evaluate("window.__orderPuts = []")
            await _long_press_drag(page, "[data-drag-id]", 150)
            puts = await page.evaluate("window.__orderPuts")
            check("без фільтра перетягування зберігається", len(puts) == 1)
            check("і шле ПОВНИЙ список із 40 id",
                  bool(puts) and len(puts[0]) == 40 and sorted(puts[0]) == list(range(1, 41)))

            # --- пошук у пікері форми таска ---
            await page.click(".bn-plus")
            await page.click("[data-person]")
            await page.wait_for_selector("#f-project")
            await page.click("#f-project")
            await page.wait_for_selector("#pick-q")
            before = await page.locator("[data-proj]").count()
            await page.fill("#pick-q", "IWPR")
            await page.wait_for_timeout(250)
            after = await page.locator("[data-proj]").count()
            check(f"пікер теж фільтрується ({before} → {after})", after < before)
            check("«Позапроєктне» лишається доступним завжди",
                  await page.locator('[data-proj="none"]').count() == 1)
            focused = await page.evaluate("document.activeElement && document.activeElement.id")
            check("фокус у пікері теж не злітає", focused == "pick-q")
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
