"""
Менеджерський перегляд екрана журналістки + ввід цілі KPI з клавіатури.

Обидва — запити Олега 29.07:
- «зроби мені можливість дивитись перший екран обраного журналіста, так
  дебажити легше» — кнопка там, де він і так дивиться її звіт по KPI;
- «Кристина на пів ставки, хочу замінити їй KPI з 200 на 100, а вручну клікати
  на мінус довго» — у правці цілі зʼявилось текстове поле.

Що стереже:
- кнопка «Подивитись її екран» є на профілі людини і веде на ЇЇ головну;
- смуга «Перегляд очима …» на місці — інакше за секунду забуваєш, чий екран;
- у перегляді видно завдання ОБРАНОЇ людини, а не свої;
- KPI тягнуться з ?person=<вона>, а не власні;
- ЧУЖА стрічка подій у перегляді не показується (двері «Події» немає) і
  прочитаною не робиться;
- «Назад» повертає на профіль;
- ціль правки можна ввести з клавіатури, і на сервер їде саме введене число;
- межі поля ті самі, що у кнопок: відʼємне і понад стелю не зберігаються;
- СТАВКА людини (пів ставки) — окремо від правки цілі: діє назавжди, видна на
  екрані людини і зберігається одним тапом.

Запуск (потрібні playwright + chromium):
    python test_webapp_peek.py
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


def _task(i, person, theme):
    return {
        "id": i, "person": person, "creator": "Катерина Середа",
        "project_id": 1, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
        "platform": None, "type": "news", "theme_id": 1, "theme_name": theme,
        "qty": 2, "note": "", "deadline": _in(15), "status": "open",
        "auto_done": False, "created_at": "2026-07-01T10:00:00+03:00",
        "done_at": None, "done_count": 0, "matches": [],
    }


BOOT = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": [],
    "assignees": [], "managers": [], "tasks": [
        _task(1, "Кристина Леонова", "Стрічка новин"),
        _task(2, "Аліна Квітко", "Тендери"),
    ],
}

# Норма відділу 200, у Кристини правкою знижена до 170 (як на знімку Олега)
NORM = {
    "id": 7, "dept": "newsroom", "metric": "news", "period": "month",
    "target": 200, "own": False, "dept_title": "Newsroom",
    "period_label": "липень 2026",
    "pace": {"elapsed": 20, "total": 23, "left": 3, "share": 20 / 23,
             "phase": "end", "is_current": True},
    "week_steps": [],
    "rows": [{"person": "Кристина Леонова", "fact": 131, "target": 170,
              "base_target": 200, "overridden": True, "note": "пів ставки",
              "excused": False, "done": False, "articles": 0,
              "article_weight": 3, "remaining": 39, "days_left": 3,
              "expected": 148, "pace": "behind", "pace_pct": 89,
              "absence": None}],
}

STUB = """
window.__kpiCalls = [];
window.__reads = 0;
window.__saved = null;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "dark",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/rate") {
    window.__rate = JSON.parse(opts.body);
    return json({ rate: window.__rate });
  }
  if (url === "/api/kpi/override") {
    window.__saved = JSON.parse(opts.body);
    return json({ ok: true });
  }
  if (url.startsWith("/api/kpi/person")) return json(window.HISTORY);
  if (url.startsWith("/api/kpi/dashboard")) return json(window.DASH);
  if (url.startsWith("/api/kpi")) {
    window.__kpiCalls.push(url);
    return json({ norms: [window.NORM], week_label: "27–02.08",
                  month_label: "липень 2026", site_db: true });
  }
  if (url === "/api/notifications") return json({ items: [], unread: 3 });
  if (url === "/api/notifications/read") { window.__reads++; return json({ ok: true }); }
  return json({ ok: true });
};
"""

HISTORY = {
    "person": "Кристина Леонова", "dept_title": "Newsroom", "has_norms": True,
    "site_db": True,
    "months": [{"label": "чер 26", "offset": -1, "is_current": False,
                "overall_pct": 40, "norms": []},
               {"label": "лип 26", "offset": 0, "is_current": True,
                "overall_pct": 66, "norms": []}],
}

DASH = {
    "period": "month", "offset": 0, "label": "липень 2026", "is_current": True,
    "site_db": True,
    "people": [{"person": "Кристина Леонова", "dept": "newsroom",
                "dept_title": "Newsroom", "overall_pct": 66, "pace_pct": 89,
                "pace": "behind", "missed_days": 0, "norms": []}],
}


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
        "window.NORM = " + json.dumps(NORM) + ";"
        "window.HISTORY = " + json.dumps(HISTORY) + ";"
        "window.DASH = " + json.dumps(DASH) + ";" + STUB)
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

    print("Перегляд екрана журналістки і ввід цілі:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            # профіль людини — там, де Олег і дивиться звіт по KPI
            await page.evaluate("nav('personhist', 'Кристина Леонова')")
            await page.wait_for_selector("[data-peek]", timeout=5000)
            check("на профілі є кнопка перегляду",
                  "Подивитись її екран" in await page.inner_text("[data-peek]"))

            await page.click("[data-peek]")
            await page.wait_for_selector(".peek", timeout=5000)
            body = await page.inner_text("#content")
            check("смуга каже, чий це екран", "Перегляд очима Кристина" in body)
            check("привітання — до неї", "Привіт, Кристина" in body)
            check("видно ЇЇ завдання", "Стрічка новин" in body)
            check("чужих завдань немає", "Тендери" not in body)

            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__kpiCalls")
            check("KPI тягнуться саме про неї",
                  any("person=" in c and "%D0%9B%D0%B5%D0%BE%D0%BD%D0%BE%D0%B2%D0%B0" in c
                      for c in calls))
            check("у перегляді видно її цифри", "131/170" in
                  await page.inner_text("#my-kpi"))

            check("чужа стрічка подій за дверима не світиться",
                  await page.locator('.door[data-nav="myfeed"]').count() == 0)
            check("і прочитаною не стає",
                  await page.evaluate("window.__reads") == 0)
            check("салют на чужу норму не стріляє",
                  await page.locator(".confetti").count() == 0)

            await page.screenshot(path="/tmp/peek.png", full_page=True)

            await page.click(".back[data-back]")
            await page.wait_for_selector("[data-peek]", timeout=5000)
            view = await page.evaluate("() => STATE.view")
            check(f"«Назад» повертає на профіль (view={view})", view == "personhist")

            # --- ввід цілі з клавіатури ---
            await page.evaluate("nav('home'); setHomeView('report')")
            await page.evaluate("STATE.kpi = { norms: [window.NORM], "
                                "week_label: 'т', month_label: 'липень 2026' }")
            await page.evaluate("nav('person', 'Кристина Леонова')")
            await page.wait_for_selector("[data-kpn]", timeout=5000)
            await page.click("[data-kpn]")
            await page.wait_for_selector("#o-val", timeout=3000)
            check("ціль тепер поле вводу, а не тільки степер",
                  await page.locator("input#o-val").count() == 1)
            check("у полі стоїть поточна ціль",
                  await page.input_value("#o-val") == "170")

            await page.fill("#o-val", "100")
            await page.click("#o-save")
            await page.wait_for_timeout(300)
            saved = await page.evaluate("window.__saved")
            check("на сервер поїхало введене число", saved and saved["target"] == 100)
            check("і потрібна норма", saved and saved["norm_id"] == 7)

            # межі ті самі, що в кнопок
            await page.evaluate("STATE.kpi = { norms: [window.NORM], "
                                "week_label: 'т', month_label: 'липень 2026' }")
            await page.evaluate("nav('person', 'Кристина Леонова')")
            await page.wait_for_selector("[data-kpn]", timeout=5000)
            await page.click("[data-kpn]")
            await page.wait_for_selector("#o-val", timeout=3000)
            await page.fill("#o-val", "9000")
            await page.click("#o-save")
            await page.wait_for_timeout(300)
            saved = await page.evaluate("window.__saved")
            check("понад стелю не зберігається", saved and saved["target"] == 500)

            # --- ставка людини (постійна, на відміну від правки періоду) ---
            await page.evaluate("closeSheet(); nav('person', 'Кристина Леонова')")
            await page.wait_for_selector("#person-rate", timeout=5000)
            check("на екрані людини видно її ставку",
                  "Ставка · 100%" in await page.inner_text("#person-rate"))
            await page.click("#person-rate")
            await page.wait_for_selector("#rt-val", timeout=3000)
            sheet_txt = await page.inner_text("#sheet")
            check("шторка пояснює різницю з правкою цілі",
                  "назавжди" in sheet_txt and "один період" in sheet_txt)
            await page.click('[data-rt="50"]')
            check("швидкий вибір ставить 50%",
                  await page.input_value("#rt-val") == "50")
            await page.click("#rt-save")
            await page.wait_for_timeout(400)
            rate = await page.evaluate("window.__rate")
            check("ставка їде на сервер саме для цієї людини",
                  rate and rate["person"] == "Кристина Леонова" and rate["rate"] == 50)

            # --- правка з'їдає ставку, і це має бути СКАЗАНО ---
            # Олег, 29.07: «поставил Кристине ставку 60%, а 200 KPI не
            # пересчиталось». Порядок множників такий і задуманий, але коли
            # правка збігається з відділовою нормою, на екрані від неї не
            # лишалось ані сліду — виглядало як зламана ставка.
            await page.evaluate("""
              window.__saved = null;
              STATE.kpi = { norms: [Object.assign({}, window.NORM, { rows: [
                Object.assign({}, window.NORM.rows[0],
                  { target: 200, base_target: 200, rate: 60, overridden: true,
                    note: null }),
              ] })], week_label: 'т', month_label: 'липень 2026' };
            """)
            await page.evaluate("closeSheet(); nav('person', 'Кристина Леонова')")
            await page.wait_for_selector("[data-kpn]", timeout=5000)
            row_txt = await page.inner_text("[data-kpn]")
            check("правку видно, навіть коли її число дорівнює відділовому",
                  "правка" in row_txt)
            check("і сказано, що ставка через неї не діє",
                  "60%" in row_txt and "не діє" in row_txt)

            await page.click("#person-rate")
            await page.wait_for_selector("#rt-val", timeout=3000)
            sheet_txt = await page.inner_text("#sheet")
            check("шторка ставки застерігає про ручну правку",
                  "сильніша за ставку" in sheet_txt)
            check("і показує, яке саме число тримає правка", "200" in sheet_txt)
            await page.click("#rt-clear")
            await page.wait_for_timeout(400)
            saved = await page.evaluate("window.__saved")
            check("кнопка знімає саме правку цієї норми",
                  saved and saved.get("clear") is True and saved["norm_id"] == 7
                  and saved["person"] == "Кристина Леонова")
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
