"""
Утиліта «Хто коли відсутній» у Mini App «Команда» (webapp/app.js).

Олег, 29.07: «утиліта — дивитись таймлайн відпусток, лікарняних і іншого, у
кого коли відпустки, + щоб на ній засічками були дедлайни тасків. Адже таски
не дивляться на відпустку і не перераховуються, і люди, йдучи у відпустку,
мають думати, що робити з їхніми тасками».

Що стереже:
- головне тут не календар, а ЗІТКНЕННЯ: відкритий дедлайн усередині
  відсутності виводиться списком зверху, ще до шкали;
- зіткнення відкриває картку завдання — щоб продовжити або зняти на місці,
  а не шукати таск руками;
- непогоджений запит на відпустку видно, але окремим виглядом: він ще не факт
  і на цілі KPI не впливає;
- закриті й зняті таски в зіткнення не йдуть — вони вже нікого не турбують;
- дедлайн ПОЗА відсутністю лишається спокійною засічкою, а не тривогою;
- порожній стан каже, що всі на місці, і де заводять відпустку;
- шкала прокручена до «сьогодні», а не в початок історії.

Запуск:  python test_webapp_away.py
"""

import asyncio
import datetime
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

# Дати у фікстурах рахуються ВІД СЬОГОДНІ. Вписана числом майбутня дата — це
# міна з відомим запалом: 15.08 вона ще майбутня, 16.08 вже минула, і
# перевірка «найближчий звіт» падає без жодної правки коду (реальний випадок
# 16.08.2026, прогін на main).
def _in(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

PEOPLE = [
    {"name": "Кристина Леонова", "dept": "newsroom", "dept_title": "Newsroom",
     "photo": None, "photo_sm": None, "photo_orig": None},
    {"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
     "photo": None, "photo_sm": None, "photo_orig": None},
]


def _task(i, person, deadline, status="open"):
    return {
        "id": i, "person": person, "creator": "Катерина Середа",
        "project_id": 1, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
        "platform": None, "type": "news", "theme_id": 1, "theme_name": "Енергетика",
        "qty": 2, "note": "", "deadline": deadline, "status": status,
        "auto_done": False, "created_at": "2026-07-01T10:00:00+03:00",
        "done_at": None, "done_count": 0, "matches": [],
    }


BOOT = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": [],
    "assignees": [], "managers": [], "tasks": [
        # дедлайн УСЕРЕДИНІ відпустки Кристини — це і є зіткнення
        _task(1, "Кристина Леонова", _in(-4)),
        # дедлайн поза будь-якою відсутністю — спокійна засічка
        _task(2, "Кристина Леонова", _in(9)),
        # закритий таск усередині відпустки — не турбує нікого
        _task(3, "Кристина Леонова", _in(-3), status="done"),
        # чужий таск усередині ЇЇ відпустки — теж не зіткнення
        _task(4, "Аліна Квітко", _in(-4)),
    ],
}

ABSENCES = [
    {"id": 11, "person": "Кристина Леонова", "start": _in(-6),
     "end": _in(1), "kind": "vacation", "title": "Відпустка",
     "note": "", "created_by": "Катерина Середа"},
    # щойно завершена лікарняна: вона лишає позаду «сьогодні» шматок історії,
    # тож видно, що шкала прокручується саме до сьогодні, а не стоїть на нулі
    {"id": 12, "person": "Аліна Квітко", "start": "2026-07-20",
     "end": "2026-07-25", "kind": "sick", "title": "Лікарняна",
     "note": "", "created_by": "Катерина Середа"},
]

REQUESTS = [
    {"id": 21, "person": "Аліна Квітко", "start": _in(16),
     "end": _in(20), "kind": "trip", "title": "Відрядження",
     "note": None, "status": "pending", "decided_by": None,
     "created_at": "2026-08-01T10:00:00+03:00"},
]

STUB = """
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/absences/requests") return json({ requests: window.REQUESTS });
  if (url.startsWith("/api/absences")) return json({ absences: window.ABSENCES });
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


async def _open(pw, absences=None, requests=None):
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
        "window.BOOT = " + json.dumps(BOOT) + ";"
        "window.ABSENCES = " + json.dumps(ABSENCES if absences is None else absences) + ";"
        "window.REQUESTS = " + json.dumps(REQUESTS if requests is None else requests) + ";"
        + STUB)
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
        browser, page = await _open(pw)
        try:
            # «Сьогодні» фіксуємо в серпні 2026, інакше вікно шкали залежало б
            # від дня прогону і відпустка з фікстури то потрапляла б у нього,
            # то ні
            await page.evaluate("window.todayISO = (o = 0) => {"
                                "  const d = new Date('2026-08-05T12:00:00');"
                                "  d.setDate(d.getDate() + o);"
                                "  return d.toISOString().slice(0, 10); };")

            # --- вхід через утиліти менеджера ---
            await page.click("#home-tools")
            await page.wait_for_selector("[data-tool]", timeout=3000)
            tools = await page.inner_text("#sheet")
            check("серед утиліт є таймлайн відсутностей", "відсутній" in tools)
            await page.click('[data-tool="away"]')
            await page.wait_for_selector("#aw-body .aw-warn, #aw-body .aw-ok",
                                         timeout=5000)

            body = await page.inner_text("#aw-body")
            check("зіткнення показані першими, ще до шкали",
                  "припада" in body.lower())
            check("і сказано, чому це важливо: KPI перерахується, таск — ні",
                  "KPI" in body and "завдання" in body)
            check("зіткнення рівно одне — чужі й закриті таски не рахуються",
                  await page.locator("[data-awclash]").count() == 1)
            clash = await page.inner_text("[data-awclash]")
            # Хто і коли — це властивість ВІДПУСТКИ, тож воно в заголовку
            # групи, а не повторюється в кожному рядку (Олег, 29.07: девʼять
            # зіткнень поспіль повторювали «Даріна … відпустка»)
            head = (await page.inner_text("#aw-body .dept-title")).lower()
            check("хто і коли — у заголовку групи, один раз",
                  "кристина" in head and "відпустка" in head and "10.08" in head)
            check("а в рядку — дата дедлайну і донор", "12.08" in clash)
            check("і про яке завдання йдеться", "Енергетика" in clash)
            box = await page.locator("[data-awclash] .tl-mark").bounding_box()
            # Олег, 29.07: «скукожилась іконка» — кружечок стискався флексом
            check("кружечок лишається круглим, а не еліпсом",
                  box and abs(box["width"] - box["height"]) < 2)

            # --- шкала ---
            check("рядок на кожну людину з відсутністю",
                  await page.locator("#aw-body .tl-row").count() == 2)
            check("імена стоять окремою нерухомою колонкою",
                  await page.locator("#aw-body .aw-name").count() == 2)
            check("погоджена відпустка — заливкою",
                  await page.locator("#aw-body .aw-bar.k-vac:not(.pending)").count() == 1)
            check("непогоджений запит — окремим виглядом, а не фактом",
                  await page.locator("#aw-body .aw-bar.pending").count() == 1)
            check("дедлайн усередині відсутності світиться тривогою",
                  await page.locator("#aw-body .tl-row .tl-mark.late").count() == 1)
            check("а дедлайн поза нею лишається спокійним",
                  await page.locator("#aw-body .tl-row .tl-mark.todo").count() == 2)
            check("закритий таск засічки не отримує (2 відкриті у вікні)",
                  await page.locator("#aw-body .tl-row .tl-mark").count() == 3)

            nowx = await page.evaluate(
                "(() => { const s = document.getElementById('aw-scroll');"
                " return [s.scrollLeft, +s.dataset.nowx]; })()")
            check("шкалу прокручено до «сьогодні», а не в початок історії",
                  nowx[0] > 0 and abs(nowx[0] - (nowx[1] - 90)) < 2)

            # --- дія: зіткнення відкриває саме завдання ---
            await page.click("[data-awclash]")
            await page.wait_for_selector("#sheet h2", timeout=3000)
            sheet = await page.inner_text("#sheet")
            check("тап по зіткненню відкриває картку завдання",
                  "Кристина Леонова" in sheet and "Енергетика" in sheet)
            check("у ній є чим зарадити — продовжити або зняти",
                  "Редагувати" in sheet and "Зняти" in sheet)

            # --- тап по засічці на шкалі веде туди ж ---
            await page.evaluate("closeSheet()")
            await page.click("#aw-body .tl-row .tl-mark.late")
            await page.wait_for_selector("#sheet h2", timeout=3000)
            check("тап по засічці теж відкриває завдання",
                  "Енергетика" in await page.inner_text("#sheet"))
        finally:
            await browser.close()

        # --- порожній стан ---
        browser, page = await _open(pw, absences=[], requests=[])
        try:
            await page.evaluate("nav('away')")
            await page.wait_for_selector("#aw-body .empty-hint", timeout=5000)
            empty = await page.inner_text("#aw-body")
            check("порожній стан каже, що всі на місці", "на місці" in empty)
            check("і де заводять відпустку", "Сповіщення" in empty
                  or "екрані людини" in empty)
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
