"""
Тест звітних екранів Mini App «Команда» (webapp/app.js + handlers/webapp.py).

Що стереже (правки Олега 27.07):
- ЗВІТ ВІДКРИВАЄТЬСЯ НА МИНУЛОМУ ТИЖНІ. У понеділок-четвер поточний ще
  напівпорожній: усі кружечки червоні просто тому, що тиждень не скінчився.
  Місяць лишається поточним — там видно прогрес до цілі.
- ЖУРНАЛІСТКА БАЧИТЬ СВОЮ ПОМІСЯЧНУ ДИНАМІКУ — ті самі стовпчики, що
  редакція бачить у Звіті.
- І НЕ БАЧИТЬ ЧУЖУ: /api/kpi/person їй віддає лише її власні цифри, хоч би
  який person вона підставила в запит.

Запуск (потрібні playwright + chromium):
    python test_webapp_report.py
"""

import asyncio
import json
import os
import pathlib
import sys
import types

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

MONTHS = [
    {"label": f"міс {i}", "offset": -(11 - i), "is_current": i == 11,
     "overall_pct": 40 + i * 5,
     "norms": [{"label": "60 власних новин", "fact": 30 + i, "target": 60,
                "pct": 40 + i * 5, "done": False}]}
    for i in range(12)
]

BOOT_JOURNALIST = {
    "me": {"name": "Юлія Бойченко", "first_name": "Юлія", "dept": "creative",
           "dept_title": "Creative", "manager": False,
           "photo": None, "photo_sm": None, "photo_orig": None},
    "site_db": True, "nora": True, "people": [], "projects": [],
    "tasks": [{"id": 1, "person": "Юлія Бойченко", "creator": "Катерина Середа",
               "project_id": None, "project_name": None, "partner_name": "IMS",
               "platform": None, "type": "news", "theme_id": None,
               "theme_name": "Стійкість", "qty": 3, "note": "",
               "deadline": "2026-07-31", "status": "open",
               "created_at": "2026-07-01T10:00:00+03:00", "done_at": None}],
}

# Норми журналістки: місячна (для кільця у шапці) + тижнева (у кільце не йде)
MY_NORMS = [
    {"id": 1, "dept": "creative", "metric": "news", "period": "month", "target": 60,
     "own": True, "dept_title": "Creative", "period_label": "липень 2026",
     "rows": [{"person": "Юлія Бойченко", "fact": 30, "target": 60, "base_target": 60,
               "overridden": False, "note": None, "excused": False, "done": False}]},
    {"id": 2, "dept": "creative", "metric": "news", "period": "week", "target": 15,
     "own": True, "dept_title": "Creative", "period_label": "27–02.08",
     "rows": [{"person": "Юлія Бойченко", "fact": 1, "target": 15, "base_target": 15,
               "overridden": False, "note": None, "excused": False, "done": False}]},
]

BOOT_MANAGER = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [{"name": "Юлія Бойченко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_sm": None, "photo_orig": None}],
    "projects": [], "tasks": [],
}

STUB = """
window.__dashCalls = [];
window.__personCalls = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(m, c){ c(true); }, disableVerticalSwipes(){},
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/kpi/dashboard")) {
    const q = new URL(url, "https://x").searchParams;
    const period = q.get("period"), offset = +q.get("offset");
    window.__dashCalls.push({ period, offset });
    return json({ period, offset, label: period === "week" ? "20–26.07" : "червень 2026",
      is_current: offset === 0, site_db: true, people: [] });
  }
  if (url.startsWith("/api/kpi/person")) {
    window.__personCalls.push(url);
    return json({ person: "Юлія Бойченко", dept_title: "Creative",
      has_norms: true, site_db: true, months: window.MONTHS });
  }
  if (url === "/api/kpi") return json({ norms: window.MY_NORMS || [],
    week_label: "27–02.08", month_label: "липень 2026", site_db: true });
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, boot):
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
        f"window.BOOT = {json.dumps(boot)}; window.MONTHS = {json.dumps(MONTHS)};"
        f"window.MY_NORMS = {json.dumps(MY_NORMS)};" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _test_journalist_cannot_read_others():
    """Серверна перевірка: журналістці /api/kpi/person віддає ТІЛЬКИ її дані,
    хоч би кого вона вписала в параметр person."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    _pm = types.ModuleType("pymysql")
    _pc = types.ModuleType("pymysql.cursors")
    _pc.DictCursor = object
    _pm.cursors = _pc
    _pm.connect = lambda *a, **k: None
    sys.modules.setdefault("pymysql", _pm)
    sys.modules.setdefault("pymysql.cursors", _pc)
    os.environ.setdefault("BOT_TOKEN", "1:test")

    from handlers import team_kpi, webapp

    asked = {}

    def fake_history(who, months):
        asked["who"] = who
        return {"person": who, "months": [], "has_norms": False, "site_db": True,
                "dept_title": ""}

    real_history = team_kpi.kpi_person_history
    real_auth = webapp._authenticate
    team_kpi.kpi_person_history = fake_history

    class Req:
        def __init__(self, query):
            self.query = query

    results = []
    try:
        # журналістка просить чужі дані
        async def auth_journalist(request):
            return "Юлія Бойченко", {"manager": False, "dept": "creative"}, {"id": 1}

        webapp._authenticate = auth_journalist
        await webapp.api_kpi_person(Req({"person": "Аліна Квітко"}))
        results.append(("журналістці підставляється ВОНА, а не запитаний person",
                        asked.get("who") == "Юлія Бойченко"))

        # менеджер просить чужі дані — і має їх отримати
        async def auth_manager(request):
            return "Олег Деренюга", {"manager": True, "dept": "admin"}, {"id": 2}

        webapp._authenticate = auth_manager
        await webapp.api_kpi_person(Req({"person": "Аліна Квітко"}))
        results.append(("менеджер бачить будь-кого", asked.get("who") == "Аліна Квітко"))
    finally:
        team_kpi.kpi_person_history = real_history
        webapp._authenticate = real_auth
    return results


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Звітні екрани Mini App «Команда»:")
    async with async_playwright() as pw:
        # --- редактор: Звіт відкривається на минулому тижні ---
        browser, page = await _open(pw, BOOT_MANAGER)
        try:
            await page.click('[data-hv="report"]')
            await page.wait_for_timeout(500)
            calls = await page.evaluate("window.__dashCalls")
            check("Звіт відкривається на МИНУЛОМУ тижні",
                  calls and calls[0] == {"period": "week", "offset": -1})
            check("у підписі періоду видно, що він минулий",
                  "минулий" in (await page.inner_text("#dash-body")).lower())

            await page.click('[data-dp="month"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__dashCalls")
            check("а місяць — поточний (там видно прогрес до цілі)",
                  calls[-1] == {"period": "month", "offset": 0})

            await page.click('[data-dp="week"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__dashCalls")
            check("повернення на тиждень знову дає минулий",
                  calls[-1] == {"period": "week", "offset": -1})

            # гортання вперед доступне — «зараз» за один тап
            await page.click('[data-doff="1"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__dashCalls")
            check("стрілка вперед веде на поточний тиждень",
                  calls[-1] == {"period": "week", "offset": 0})
        finally:
            await browser.close()

        # --- журналістка: власна помісячна динаміка ---
        browser, page = await _open(pw, BOOT_JOURNALIST)
        try:
            await page.wait_for_timeout(700)
            body = await page.inner_text("#content")
            check("журналістка бачить блок динаміки", "Як іде місяць до місяця" in body)
            check("стовпчиків — 12", await page.locator("#my-hist-chart .hb-col").count() == 12)
            check("і вони підписані відсотками", "%" in body)
            calls = await page.evaluate("window.__personCalls")
            check("апка не підставляє чуже ім'я в запит",
                  calls and "person=" not in calls[0])

            # кільце KPI у шапці: завжди зелене і за ПОТОЧНИЙ МІСЯЦЬ
            ring = await page.inner_html("#me-ring")
            check("у шапці є кільце з фото", 'class="avaring"' in ring)
            check("смуга кільця зелена, а не за рівнем виконання",
                  "var(--good)" in ring and "hsl(" not in ring)
            cap = await page.inner_text("#me-ring")
            check("під кільцем видно, що це за поточний місяць",
                  "липень 2026" in cap)
            check("і сам відсоток — за МІСЯЧНОЮ нормою (30/60), не за тижневою",
                  "50%" in cap)
        finally:
            await browser.close()

    results.extend(await _test_journalist_cannot_read_others())

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
