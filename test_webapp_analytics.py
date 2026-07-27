"""
Тест таби «Аналітика» Mini App «Команда» (webapp/app.js).

Аналітичний дашборд редакції: сайт, пошук Google, вихід редакції, соцмережі,
гранти, топ матеріалів — за тиждень або місяць з порівнянням до попереднього
періоду. Дані — з /api/dashboard (handlers/team_analytics.py).

Що стереже:
- таба стоїть ЗЛІВА від «Завдань» і відкриває дашборд;
- геройське число читачів, дельта з правильним знаком і кольором;
- стовпчики днів: по одному на день, підсвічений максимум, тап дає цифру дня;
- частки типів пошуку — складена смуга (три сегменти) + легенда з цифрами,
  тобто ідентичність не тримається на самому кольорі;
- вихід редакції: плитки й автори, тап по автору веде в його трекер;
- соцмережі й гранти малюються з payload, підписники — з приростом;
- топ матеріалів відкривається через tg.openLink, а не всередині апки;
- перемикач тиждень/місяць і гортання періодів шлють правильні запити,
  стрілка «вперед» на поточному періоді заблокована;
- обрана таба переживає перезаход в апку (localStorage);
- недоступна Нора / порожній блок дають зрозумілу підказку, а не порожній екран.

Запуск (потрібні playwright + chromium):
    python test_webapp_analytics.py
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
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
]

PEOPLE = [
    {"name": "Юлія Бойченко", "dept": "creative", "dept_title": "Creative",
     "photo": None, "photo_sm": None, "photo_orig": None},
    {"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
     "photo": None, "photo_sm": None, "photo_orig": None},
]

PROJECTS = [{
    "id": 1, "name": "Голоси Миколаєва", "partner": "IMS", "logo": None,
    "logo_orig": None, "start_date": 1740000000, "end_date": 1798000000,
    "kpi_news": 30, "kpi_articles": 5, "themes": [], "drive_url": None,
    "deadlines": [{"id": 5, "kind": "narrative", "stage": "interim", "title": "",
                   "due": "2026-08-15", "assignee": "Катерина Середа",
                   "assignee_custom": False, "status": None}],
}]

BOOTSTRAP = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": PROJECTS,
    "assignees": [], "managers": [], "tasks": [], "pending_count": 0, "unread": 0,
}

# 7 днів: максимум у середу 22.07
SERIES = [
    {"date": "2026-07-20", "users": 15000, "pageviews": 30000},
    {"date": "2026-07-21", "users": 17000, "pageviews": 33000},
    {"date": "2026-07-22", "users": 22100, "pageviews": 41000},
    {"date": "2026-07-23", "users": 19000, "pageviews": 35000},
    {"date": "2026-07-24", "users": 18300, "pageviews": 34000},
    {"date": "2026-07-25", "users": 19000, "pageviews": 36000},
    {"date": "2026-07-26", "users": 18000, "pageviews": 33000},
]

DASH = {
    "period": "week", "offset": -1, "label": "20–26.07", "prev_label": "13–19.07",
    "is_current": False, "days": 7, "days_total": 7, "comparable_days": 7,
    "range": {"start": "2026-07-20", "end": "2026-07-26", "data_end": "2026-07-26"},
    "nora": True, "site_db": True,
    "site": {
        "users": {"value": 128400, "prev": 114000, "delta": 13},
        "sessions": {"value": 148320, "prev": 150000, "delta": -1},
        "pageviews": {"value": 242000, "prev": 230000, "delta": 5},
        "series": SERIES,
        "best_day": {"date": "2026-07-22", "users": 22100},
        "days_with_data": 7,
    },
    "search": {
        "clicks": {"value": 41500, "prev": 38000, "delta": 9},
        "impressions": {"value": 980000},
        "types": [
            {"type": "web", "label": "Пошук", "clicks": 26000, "impressions": 700000,
             "prev": 25000, "delta": 4, "share": 63},
            {"type": "discover", "label": "Discover", "clicks": 14000,
             "impressions": 260000, "prev": 11000, "delta": 27, "share": 34},
            {"type": "googleNews", "label": "Google Новини", "clicks": 1500,
             "impressions": 20000, "prev": 2000, "delta": -25, "share": 4},
        ],
        "has_data": True,
    },
    "newsroom": {
        "news": {"value": 214, "prev": 198, "delta": 8},
        "articles": {"value": 9, "prev": 11, "delta": -18},
        "own": {"value": 96, "prev": 80, "delta": 20},
        "own_share": 43, "total": 223,
        # сервер уже впорядкував за вагою (власне × 2,6) і звів автора до
        # канонічного ПІБ ростера — фронт лише малює
        "authors": [
            {"name": "Юлія Бойченко", "person": "Юлія Бойченко", "count": 34,
             "own": 30, "news": 30, "articles": 4, "score": 82.0},
            {"name": "Аліна Квітко", "person": "Аліна Квітко", "count": 22,
             "own": 18, "news": 21, "articles": 1, "score": 50.8},
            {"name": "Стрічка", "person": None, "count": 40, "own": 0,
             "news": 40, "articles": 0, "score": 40.0},
        ],
    },
    "social": {
        "facebook": {"title": "Facebook", "followers": 41500, "followers_as_of": "2026-07-26",
                     "followers_delta": 500, "snapshots": 1,
                     "views": {"value": 120400, "prev": 111000, "delta": 8},
                     "engagement": {"value": 6200, "prev": 6400, "delta": -3},
                     "posts": {"value": 84, "prev": 80, "delta": 5}},
        "instagram": {"title": "Instagram", "followers": 20100, "followers_as_of": "2026-07-26",
                      "followers_delta": -40, "snapshots": 0,
                      "views": {"value": None, "prev": None, "delta": None},
                      "engagement": {"value": None, "prev": None, "delta": None},
                      "posts": {"value": None, "prev": None, "delta": None}},
        "telegram": {"title": "Telegram", "subscribers": 41012},
    },
    "grants": {
        "projects_active": 12, "tasks_open": 25, "tasks_done": 7,
        "counted": 9, "pending": 3,
        "donors": [{"project_id": 2, "name": "IWPR", "count": 5},
                   {"project_id": 1, "name": "IMS", "count": 4}],
    },
    "top": [
        {"path": "/news/city/1-a", "title": "Сесія міськради ухвалила бюджет",
         "views": 12400, "days": 3, "author": "Аліна Квітко",
         "url": "https://nikvesti.com/news/city/1-a"},
        {"path": "/articles/2-b", "title": "Як живе Корабельний район",
         "views": 8100, "days": 1, "author": "Юлія Бойченко",
         "url": "https://nikvesti.com/articles/2-b"},
    ],
}

DASH_EMPTY = {**DASH, "nora": False, "site": None, "search": None,
              "newsroom": None, "social": None, "grants": None, "top": []}

STUB = """
window.__opened = [];
window.__reqs = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(u){ window.__opened.push(u); },
  showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  window.__reqs.push(url);
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/dashboard")) {
    if (window.__degraded) return json(window.DASH_EMPTY);
    const q = new URLSearchParams(url.split("?")[1] || "");
    const period = q.get("period"), offset = +q.get("offset");
    return json({ ...window.DASH, period, offset,
      is_current: offset === 0,
      label: offset === 0 ? (period === "month" ? "липень 2026" : "27.07–02.08")
                          : (period === "month" ? "червень 2026" : "20–26.07") });
  }
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, boot=BOOTSTRAP):
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
    await page.add_init_script(
        f"window.BOOT = {json.dumps(boot)}; window.DASH = {json.dumps(DASH)};"
        f"window.DASH_EMPTY = {json.dumps(DASH_EMPTY)};" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


def _digits(text):
    return "".join(ch for ch in text if ch.isdigit())


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Таба «Аналітика» Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            tabs = await page.locator("[data-hv]").all_inner_texts()
            check("таба «Аналітика» стоїть першою, перед «Завданнями»",
                  tabs[:3] == ["Аналітика", "Завдання", "Звіт"])
            check("на старті відкриті «Завдання» (робочий екран Каті)",
                  await page.locator('[data-hv="tasks"].on').count() == 1)

            await page.click('[data-hv="analytics"]')
            await page.wait_for_selector(".hero-v", timeout=5000)

            # --- сайт ---
            check("геройське число читачів",
                  _digits(await page.inner_text(".hero-v")) == "128400")
            check("дельта зростання зелена й зі стрілкою вгору",
                  await page.locator(".hero-d .st-d.up").count() == 1
                  and "▲" in await page.inner_text(".hero-d .st-d"))
            check("падіння сесій позначено вниз і червоним",
                  await page.locator(".stat .st-d.down").count() >= 1)
            tiles = await page.inner_text(".stat-grid")
            check("плитки сайту на місці", "Сесії" in tiles and "Перегляди" in tiles)
            check("сторінок на сесію порахувано на фронті", "1,6" in tiles)

            # --- стовпчики днів ---
            check("по стовпчику на кожен день",
                  await page.locator("#an-spark .sp-col").count() == 7)
            check("максимум підсвічений рівно один",
                  await page.locator("#an-spark .sp-col.max").count() == 1)
            check("підпис максимуму каже цифру й дату",
                  "22100" in _digits(await page.inner_text(".sp-cap")))
            await page.click('#an-spark [data-sp="3"]')
            await page.wait_for_selector("#toast:not(.hidden)", timeout=2000)
            check("тап по стовпчику дає цифру дня",
                  "19000" in _digits(await page.inner_text("#toast")))

            # --- пошук ---
            check("складена смуга з трьох сегментів",
                  await page.locator(".shr i").count() == 3)
            check("сегменти мають різні кольори (категоріальні слоти)",
                  await page.locator(".shr i.c1").count() == 1
                  and await page.locator(".shr i.c3").count() == 1)
            legend = await page.inner_text(".legend")
            check("легенда називає типи словами, не лише кольором",
                  "Пошук" in legend and "Discover" in legend and "Google Новини" in legend)
            check("у легенді є і цифри, і частки", "14 000" in legend.replace(" ", " ")
                  or "14000" in _digits(legend))
            check("падіння Google Новин показано вниз",
                  await page.locator(".lg-row .st-d.down").count() == 1)

            # --- вихід редакції ---
            nr = await page.inner_text("#an-data")
            check("вихід редакції: новини, статті, власні",
                  "Новин" in nr and "Статей" in nr and "Власних" in nr)
            check("частка власних підписана", "43% усього" in nr)
            check("автори з кількістю", await page.locator(".an-row").count() == 3)
            check("автор поза ростером не клікабельний",
                  await page.locator('.an-row[data-tracker]').count() == 2)
            names = await page.locator(".ar-n").all_inner_texts()
            check("порядок авторів — з сервера (за вагою власного матеріалу), "
                  "40 рерайтів нижче за 34 матеріали з 30 власними",
                  names == ["Юлія Бойченко", "Аліна Квітко", "Стрічка"])
            bars = await page.locator(".an-row .kbar i").all()
            w1 = await bars[0].evaluate("el => el.style.width")
            w3 = await bars[2].evaluate("el => el.style.width")
            check("смуга теж за вагою, а не за кількістю",
                  int(w1.rstrip("%")) > int(w3.rstrip("%")))
            check("вага підписана, щоб цифри не читались як суперечність",
                  "2,6" in await page.inner_text("#an-data"))

            # --- соцмережі й гранти ---
            soc = await page.inner_text("#an-data")
            check("Facebook із приростом підписників", "+500" in soc)
            check("відписки показані мінусом", "−40" in soc)
            check("Telegram підтягнуто живим знімком", "41 012" in soc.replace(" ", " ")
                  or "41012" in _digits(soc))
            check("платформа без зрізу за період не бреше нулем",
                  "зрізу за цей період ще немає" in soc)
            check("гранти: проєкти, завдання, зараховане",
                  "Активних проєктів" in soc and "Зараховано" in soc)
            check("донори перелічені", "IWPR ×5" in soc)
            check("найближчий звіт узято з bootstrap, без окремого запиту",
                  "Найближчий звіт" in soc and "IMS" in soc)

            # --- топ матеріалів ---
            check("топ матеріалів списком", await page.locator(".top-row").count() == 2)
            await page.click(".top-row")
            await page.wait_for_timeout(200)
            opened = await page.evaluate("window.__opened")
            check("матеріал відкривається через tg.openLink",
                  opened and opened[0].endswith("/news/city/1-a"))
            check("апка при цьому лишилась на місці",
                  page.url.startswith("https://app.local/"))

            await page.screenshot(path="/tmp/analytics-week.png", full_page=True)

            # --- перемикачі періоду ---
            await page.evaluate("window.__reqs = []")
            await page.click('[data-ap="month"]')
            await page.wait_for_timeout(400)
            reqs = await page.evaluate("window.__reqs")
            check("місяць запитується з offset=-1 (завершений)",
                  any("period=month&offset=-1" in r for r in reqs))
            # inner_text віддає ВІДРЕНДЕРЕНИЙ текст, а .month-nav b::first-letter
            # робить першу літеру великою — тому порівнюємо без регістру
            body = (await page.inner_text(".month-nav")).lower()
            check("підпис періоду з сервера", "червень 2026" in body)

            await page.click('[data-aoff="1"]')
            await page.wait_for_timeout(400)
            check("гортання вперед доводить до поточного періоду",
                  "липень 2026" in (await page.inner_text(".month-nav")).lower())
            check("на поточному періоді «вперед» заблоковано",
                  await page.locator('[data-aoff="1"]').is_disabled())
            await page.click('[data-aoff="-1"]')
            await page.wait_for_timeout(400)
            check("назад працює далі в історію",
                  "червень 2026" in (await page.inner_text(".month-nav")).lower())

            # --- пам'ять обраної таби ---
            await page.reload()
            await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
            await page.wait_for_selector(".hero-v", timeout=5000)
            check("перезаход відкриває ту саму табу",
                  await page.locator('[data-hv="analytics"].on').count() == 1)

            # --- тап по автору веде в трекер ---
            await page.click('.an-row[data-tracker="Юлія Бойченко"]')
            await page.wait_for_timeout(300)
            check("тап по автору відкриває його трекер",
                  "Юлія Бойченко" in await page.inner_text(".who"))
        finally:
            await browser.close()

        # --- деградація: нора недоступна ---
        browser, page = await _open(pw)
        try:
            await page.evaluate("window.__degraded = true")
            await page.click('[data-hv="analytics"]')
            await page.wait_for_selector(".empty-hint", timeout=5000)
            hint = await page.inner_text("#an-data")
            check("недоступна Нора — зрозуміла підказка, а не порожній екран",
                  "Нора" in hint)
            check("апка не впала і меню на місці",
                  await page.locator("#bottomnav").count() == 1)
            await page.click('[data-hv="tasks"]')
            await page.wait_for_timeout(200)
            check("з дашборда можна повернутись у «Завдання»",
                  await page.locator('[data-hv="tasks"].on').count() == 1)
        finally:
            await browser.close()

    ok = 0
    for name, passed in results:
        print(f"{'✅' if passed else '❌'} {name}")
        ok += 1 if passed else 0
    print(f"\n{ok}/{len(results)} перевірок пройдено")
    print("Скріншот: /tmp/analytics-week.png")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
