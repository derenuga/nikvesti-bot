"""
«Мої публікації» і нижнє меню журналістки (webapp/app.js, handlers/team_publications.py).

Олег, 29.07: «може, треба таки меню журналісту? мої публ, блокнот, телефон,
оповіщення?» — і окремо: «дати можливість журналістці в своєму інтерфейсі
бачити кнопку "Мої публікації"».

Що стереже:
- у журналістки Є нижнє меню, і в ньому «Публікації» та «Контакти» — раніше
  меню було лише в менеджера, і телефонна книга ховалась дверима;
- на головній лишились ТІЛЬКИ двері з числом («Виконані», «KPI по місяцях»,
  «Не буду на роботі») — регулярне переїхало в меню, інакше екран став би
  списком із шести посилань;
- перегляд чужими очима показує ТЕ САМЕ (Олег: «я хочу бачити все, що у неї»),
  але «Події» вимкнено: /api/notifications віддає стрічку того, хто дивиться;
- лічильник переглядів підписаний «на сайті» і окремо сказано, що він за весь
  час, а не за місяць — інакше це тихе перебільшення;
- місяці гортаються назад, у майбутнє — ні;
- у переглядi публікації тягнуться для ОБРАНОЇ людини, а не для менеджера.

Запуск:  python test_webapp_pubs.py
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

PEOPLE = [
    {"name": "Юлія Лукʼяненко", "dept": "newsroom", "dept_title": "Newsroom",
     "photo": None, "photo_sm": None, "photo_orig": None},
]

ME_JOURNALIST = {"name": "Юлія Лукʼяненко", "first_name": "Юлія",
                 "dept": "newsroom", "dept_title": "Newsroom", "manager": False}
ME_MANAGER = {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
              "dept_title": "Адміністративний", "manager": True}


def _boot(me):
    return {
        "me": me, "site_db": True, "nora": True, "people": PEOPLE,
        "projects": [], "assignees": [], "managers": [], "tasks": [],
    }


PUBS = {
    "items": [
        {"id": 321, "title": "Миколаїв отримав нову підстанцію",
         "url": "https://nikvesti.com/news/city/321", "date": "28.07",
         "time": "10:15", "ts": 1785000000, "own": True, "kind": "news",
         "views": 4312},
        {"id": 322, "title": "Як місто пережило зиму без тепла",
         "url": "https://nikvesti.com/news/city/322", "date": "21.07",
         "time": "09:00", "ts": 1784400000, "own": True, "kind": "article",
         "views": 15870},
    ],
    "total": 2, "own": 2, "articles": 1, "views": 20182, "capped": False,
    "label": "липень 2026",
}

STUB = """
window.__pubCalls = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(u){ window.__opened = u; }, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/publications")) {
    window.__pubCalls.push(url);
    return json(window.PUBS);
  }
  if (url === "/api/notifications") return json({ items: [], unread: 2 });
  if (url.startsWith("/api/kpi")) return json({ norms: [], week_label: "т",
    month_label: "липень 2026", site_db: true });
  if (url.startsWith("/api/contacts")) return json({ contacts: [], mine: null });
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, me):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    # Справжній telegram-web-app.js із telegram.org НЕ вантажимо: він затирає
    # нашу заглушку window.Telegram, апка бачить, що вона не в Телеграмі, і
    # показує екран помилки замість головного. Локально це не спливало (у
    # пісочниці немає мережі, скрипт просто не діставався) — і всі 18 тестів
    # апки чесно падали в CI, де мережа є. Тест не має залежати від того,
    # дотягнувся браузер до чужого сайту чи ні.
    await page.route("https://telegram.org/**", lambda r: asyncio.ensure_future(
        r.fulfill(status=200, content_type="application/javascript", body="")))
    await page.route("**/static/*", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
    await page.route("https://app.local/", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
    await page.add_init_script(
        "window.BOOT = " + json.dumps(_boot(me)) + ";"
        "window.PUBS = " + json.dumps(PUBS) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    async with async_playwright() as pw:
        # ---------- журналістка ----------
        browser, page = await _open(pw, ME_JOURNALIST)
        try:
            check("у журналістки зʼявилось нижнє меню",
                  await page.locator("#bottomnav:not(.hidden) .bn").count() == 5)
            nav_txt = await page.inner_text("#bottomnav")
            check("у меню є «Публікації»", "Публікації" in nav_txt)
            check("і телефонна книга — та сама, про яку питав Олег",
                  "Контакти" in nav_txt)
            check("менеджерських пунктів там немає",
                  "Проєкти" not in nav_txt and "Звітність" not in nav_txt)
            check("непрочитані події видно числом на меню",
                  (await page.inner_text("#bottomnav .bn-badge")).strip() == "2")

            doors = await page.inner_text(".doors")
            check("на головній лишились двері з числом", "KPI по місяцях" in doors)
            check("а регулярне переїхало в меню — дверей «Контакти» вже немає",
                  "Контакти редакції" not in doors)
            check("запит на відсутність лишився дверима", "Не буду на роботі" in doors)

            # --- сам екран публікацій ---
            await page.click('#bottomnav [data-view="mypubs"]')
            await page.wait_for_selector(".pub-hero", timeout=5000)
            hero = await page.inner_text(".pub-hero")
            check("геройське число — перегляди",
                  "20" in hero and "182" in hero)
            check("і сказано, що це перегляди саме на сайті", "на сайті" in hero)
            check("під ним розклад: скільки матеріалів, власних, статей",
                  "2 матеріали" in hero and "власні" in hero and "1 стаття" in hero)
            body = await page.inner_text("#pubs-body")
            check("видно сам перелік", "нову підстанцію" in body)
            check("зі статтею, позначеною окремо", "стаття" in body)
            check("дата й час стоять окремим боксом, а не всередині заголовка",
                  await page.locator(".pub-row .pub-when").count() == 2)
            check("чесно сказано, що лічильник за весь час, а не за місяць",
                  "за весь час" in body)

            check("місяць підписано", "липень 2026" in await page.inner_text(".per-strip"))
            check("уперед гортати нікуди — стрілка вимкнена",
                  await page.locator(".per-arrow.off").count() == 1)
            await page.click('.per-arrow[data-poff="-1"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__pubCalls")
            check("гортання назад питає попередній місяць",
                  any("offset=-1" in c for c in calls))
            check("і не підмішує чужу людину — журналістка бачить лише себе",
                  all("person=" not in c for c in calls))

            await page.click(".pub-row")
            await page.wait_for_timeout(200)
            check("тап по матеріалу відкриває його на сайті",
                  (await page.evaluate("window.__opened") or "").endswith("/321"))
            # Олег, 29.07: «у мене не працює кнопка назад у списку публікацій».
            # backTarget() не знав ні про mypubs, ні про away — кнопка просто
            # нічого не робила, і в журналістки так само.
            await page.click("[data-back]")
            await page.wait_for_timeout(300)
            check("«Назад» зі списку публікацій веде на головну",
                  await page.evaluate("STATE.view") == "home")
            await page.click('#bottomnav [data-view="mypubs"]')
            await page.wait_for_selector(".pub-hero", timeout=5000)
            await page.screenshot(path="/tmp/ph-shots/pubs.png", full_page=True)
        finally:
            await browser.close()

        # ---------- менеджер у перегляді чужими очима ----------
        browser, page = await _open(pw, ME_MANAGER)
        try:
            await page.evaluate("nav('preview', 'Юлія Лукʼяненко')")
            await page.wait_for_selector(".doors", timeout=5000)
            nav_txt = await page.inner_text("#bottomnav")
            check("у перегляді меню теж журналістське", "Публікації" in nav_txt)
            # «Події» — чужа стрічка, «Блокнот» — чужі особисті записи
            check("а «Події» і «Блокнот» не натискаються — це чуже",
                  await page.locator('#bottomnav .bn.off').count() == 2)
            doors = await page.inner_text(".doors")
            check("двері видно всі, як у неї", "Не буду на роботі" in doors)
            check("і саме вони позначені як тільки перегляд",
                  await page.locator(".door.off .door-off").count() == 1)

            await page.click('#bottomnav [data-view="mypubs"]')
            await page.wait_for_selector(".pub-hero", timeout=5000)
            calls = await page.evaluate("window.__pubCalls")
            check("публікації тягнуться для ОБРАНОЇ людини, а не для менеджера",
                  any("person=" in c for c in calls))
            check("у заголовку видно, чиї це публікації",
                  "Юлія" in await page.inner_text(".h-big"))
            check("і підпис не бреше про «твоє імʼя» — це чужий екран",
                  "твоїм" not in await page.inner_text(".h-sub"))
            await page.click("[data-back]")
            await page.wait_for_timeout(300)
            check("«Назад» у перегляді повертає в перегляд, а не викидає з нього",
                  await page.evaluate("STATE.view") == "preview")

            await page.evaluate("nav('home')")
            await page.wait_for_timeout(300)
            check("вийшов із перегляду — меню знову менеджерське",
                  "Звітність" in await page.inner_text("#bottomnav"))
        finally:
            await browser.close()

    print()
    for name, ok in results:
        print(("  ✅ " if ok else "  ❌ ") + name)
    bad = [n for n, ok in results if not ok]
    print()
    if bad:
        print(f"❌ Провалено: {len(bad)} із {len(results)}")
        return 1
    print(f"{len(results)}/{len(results)} перевірок пройдено")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
