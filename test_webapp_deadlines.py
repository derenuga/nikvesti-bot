"""
Тест екрана «Звітність» Mini App «Команда» (webapp/app.js).

Запит Олега 27.07.2026: окрема сторінка про звіти по грантах — донор, строк
проєкту, дати звітів, хто пише, і кнопки «звіт подано» / «звіт прийнято».

Що стереже:
- сортування карток за НАЙБЛИЖЧИМ дедлайном (зверху те, що горить);
- дефолти відповідальних: фінанси — Олена Бондаренко, наративка — Катя;
- зміна відповідального шле правильний запит і одразу видно в рядку;
- позначки руху звіту, зокрема повернення в «очікується»;
- проєкти без заведених дедлайнів не губляться, а йдуть окремим рядком.

Запуск (потрібні playwright + chromium):
    python test_webapp_deadlines.py

Заразом стереже засічки звітності на ТАЙМЛАЙНІ (29.07): вони є, прострочене
видно окремим станом, прийняте приглушене, збіг дат склеюється в одну засічку
з цифрою і питає, який звіт відкрити, а одиночна веде одразу в дедлайн (не в
проєкт під нею).
"""

import asyncio
import datetime
import json
import os
import pathlib
import sys
import time

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

# Дати фікстури рахуються ВІД СЬОГОДНІ, а не вписані числами.
#
# Тут стояли літерали (найближчий звіт — 05.08.2026), і 15.08.2026 тест почав
# падати пʼятьма перевірками, хоч ніхто нічого не ламав: строк просто минув,
# «найближчий» став простроченим, і сортування чесно поїхало. Перевірка,
# написана під конкретний тиждень, гниє сама — а падіння без причини вчить
# не дивитись на червоне.
DAY = 86400
_today = datetime.date.today()


def _d(days):
    """Дата за N днів від сьогодні у форматі, який чекає апка."""
    return (_today + datetime.timedelta(days=days)).isoformat()


NEAR = _d(20)     # найближчий звіт — на ньому тримається сортування карток
MID = _d(75)      # наступний за ним, іншого проєкту
FAR = _d(150)     # майлстоун наприкінці
LATE = _d(-200)   # давно прострочений (засічка «late» на таймлайні)
PAST_OK = _d(-150)  # минулий, але прийнятий (засічка «ok»)

_now = int(time.time())

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


def _dl(i, pid, kind, stage, due, assignee, custom=False, status=None, title=""):
    return {"id": i, "project_id": pid, "kind": kind, "stage": stage, "title": title,
            "due": due, "assignee": assignee, "assignee_custom": custom,
            "status": status, "status_by": None, "status_at": None}


def _proj(i, partner, name, end, dls):
    return {"id": i, "name": name, "partner": partner, "logo": None, "logo_orig": None,
            "start_date": _now - 365 * DAY, "end_date": end, "kpi_news": 30,
            "kpi_articles": 5, "themes": [], "deadlines": dls, "drive_url": None}


PROJECTS = [
    # Навмисно НЕ по порядку дат: перевіряємо, що екран сортує сам
    _proj(1, "International Media Support", "Голоси Миколаєва", _now + 300 * DAY, [
        _dl(1, 1, "narrative", "interim", MID, "Катерина Середа"),
    ]),
    _proj(2, "IWPR", "Стійкість локального медіа", _now + 220 * DAY, [
        _dl(2, 2, "financial", "final", NEAR, "Олена Бондаренко"),
        _dl(3, 2, "milestone", None, FAR, "Олег Деренюга", custom=True,
            title="Публічна презентація"),
    ]),
    _proj(3, "Internews", "Fight for Facts", _now + 260 * DAY, []),
]

ASSIGNEES = [
    {"name": "Олег Деренюга", "dept_title": "Адміністративний", "admin": True},
    {"name": "Катерина Середа", "dept_title": "Адміністративний", "admin": True},
    {"name": "Олена Бондаренко", "dept_title": "Адміністративний", "admin": True},
    {"name": "Аліна Квітко", "dept_title": "Creative", "admin": False},
]

BOOTSTRAP = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_sm": None, "photo_orig": None}],
    "projects": PROJECTS, "assignees": ASSIGNEES, "tasks": [],
}

STUB = """
window.__puts = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(m, c){ c(true); }, disableVerticalSwipes(){},
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  const body = opts.body ? JSON.parse(opts.body) : {};
  let m;
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });

  // Сервер зберігає й віддає оновлений дедлайн — як справжні роути
  const find = (id) => {
    for (const p of window.BOOT.projects) {
      const d = (p.deadlines || []).find((x) => x.id === +id);
      if (d) return d;
    }
    return null;
  };
  if ((m = url.match(/^\\/api\\/project_deadlines\\/(\\d+)\\/assignee$/))) {
    window.__puts.push({ url, body });
    const d = find(m[1]);
    d.assignee = body.clear ? (d.kind === "financial" ? "Олена Бондаренко"
      : d.kind === "narrative" ? "Катерина Середа" : null) : body.person;
    d.assignee_custom = !body.clear;
    return json({ deadline: JSON.parse(JSON.stringify(d)) });
  }
  if ((m = url.match(/^\\/api\\/project_deadlines\\/(\\d+)\\/status$/))) {
    window.__puts.push({ url, body });
    const d = find(m[1]);
    d.status = body.clear ? null : body.status;
    d.status_by = d.status ? "Олег Деренюга" : null;
    d.status_at = d.status ? "2026-07-27" : null;
    return json({ deadline: JSON.parse(JSON.stringify(d)) });
  }
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
    await page.click('[data-view="reports"]')
    await page.wait_for_selector(".rep-card")
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

    print("Екран «Звітність» Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            donors = await page.locator(".rh-donor").all_inner_texts()
            check("зверху проєкт із найближчим дедлайном "
                  "(IWPR за 20 днів, не IMS за 75)",
                  donors and donors[0] == "IWPR")
            check("проєкт без дедлайнів не загубився, а внизу окремо",
                  "Без заведених дедлайнів" in await page.inner_text("#content")
                  and "Internews" in await page.inner_text("#content"))
            check("видно строк проєкту з CMS",
                  "проєкт до" in await page.inner_text(".rep-card"))

            body = await page.inner_text("#content")
            check("фінансовий звіт за замовчуванням на фінменеджерці",
                  "Олена Бондаренко" in body)
            check("наративний — на головредакторці", "Катерина Середа" in body)

            # --- зміна відповідального ---
            await page.click('[data-dl-assign="2"]')
            await page.wait_for_selector('[data-who]')
            check("у шторці є всі троє адміністративних",
                  await page.locator('[data-who]').count() == len(ASSIGNEES))
            await page.click('[data-who="Олег Деренюга"]')
            await page.wait_for_timeout(400)
            puts = await page.evaluate("window.__puts")
            check("полетів PUT на /assignee з правильною людиною",
                  puts and puts[-1]["url"].endswith("/2/assignee")
                  and puts[-1]["body"] == {"person": "Олег Деренюга"})
            check("рядок одразу показує нового відповідального",
                  "Олег Деренюга" in await page.inner_text(".rep-card"))

            # --- рух звіту ---
            await page.evaluate("window.__puts = []")
            await page.click('[data-dl-assign="2"]')
            await page.wait_for_selector('[data-st="submitted"]')
            await page.click('[data-st="submitted"]')
            await page.wait_for_timeout(400)
            check("«Звіт подано» шле status=submitted",
                  (await page.evaluate("window.__puts"))[-1]["body"] == {"status": "submitted"})
            check("бейдж «подано» з'явився у рядку",
                  "подано" in await page.inner_text(".rep-card"))

            await page.click('[data-dl-assign="2"]')
            await page.wait_for_selector('[data-st="accepted"]')
            await page.click('[data-st="accepted"]')
            await page.wait_for_timeout(400)
            check("«Звіт прийнято» шле status=accepted",
                  (await page.evaluate("window.__puts"))[-1]["body"] == {"status": "accepted"})
            check("рядок прийнятого звіту притлумлено",
                  await page.locator(".rep-row.done").count() == 1)

            # --- помилились кнопкою: повернення в «очікується» ---
            await page.click('[data-dl-assign="2"]')
            await page.wait_for_selector('[data-st=""]')
            await page.click('[data-st=""]')
            await page.wait_for_timeout(400)
            check("«Скасувати позначку» шле clear",
                  (await page.evaluate("window.__puts"))[-1]["body"] == {"clear": True})
            check("бейджів статусу не лишилось",
                  await page.locator(".rr-st").count() == 0)
            # --- засічки звітності на таймлайні (Олег, 29.07) ---
            # Стани доливаємо просто тут, а не у фікстуру: фікстура несуча —
            # на ній тримаються перевірки сортування й статусів «Звітності»
            await page.evaluate("""(d) => {
              const p2 = STATE.projects.find((x) => x.id === 2);
              p2.deadlines.push(
                { id: 4, project_id: 2, kind: 'narrative', stage: 'interim',
                  title: '', due: d.late, assignee: 'Катерина Середа',
                  assignee_custom: false, status: null },
                { id: 5, project_id: 2, kind: 'financial', stage: 'interim',
                  title: '', due: d.pastOk, assignee: 'Олена Бондаренко',
                  assignee_custom: false, status: 'accepted' },
                { id: 6, project_id: 2, kind: 'narrative', stage: 'final',
                  title: '', due: d.near, assignee: 'Катерина Середа',
                  assignee_custom: false, status: null });
              nav('projects'); STATE.projView = 'timeline'; render();
            }""", {"late": LATE, "pastOk": PAST_OK, "near": NEAR})
            await page.wait_for_selector(".tl-mark", timeout=5000)
            check("засічки зʼявились на смугах",
                  await page.locator(".tl-mark:not(.static)").count() >= 3)
            check("прострочений звіт помічено окремо",
                  await page.locator(".tl-mark.late:not(.static)").count() == 1)
            check("прийнятий — приглушено",
                  await page.locator(".tl-mark.ok:not(.static)").count() == 1)
            check("два звіти на одну дату склеєні в одну засічку з цифрою",
                  "2" in await page.locator(
                      f'[data-dlmark="2:{NEAR}"] i').inner_text())
            check("під діаграмою є легенда",
                  await page.locator(".tl-legend .tl-lg").count() == 5)

            # тап по склеєній засічці має спитати, який саме звіт
            await page.click(f'[data-dlmark="2:{NEAR}"]')
            await page.wait_for_selector("[data-dlpick]", timeout=3000)
            check("склеєна засічка питає, який звіт відкрити",
                  await page.locator("[data-dlpick]").count() == 2)
            await page.click('[data-dlpick="0"]')
            await page.wait_for_timeout(300)
            check("і відкриває саме дедлайн, а не проєкт під ним",
                  "Звіт" in await page.inner_text("#sheet")
                  or "звіт" in await page.inner_text("#sheet"))
            await page.evaluate("closeSheet()")

            # одиночна засічка відкриває дедлайн одразу
            await page.click(f'[data-dlmark="1:{MID}"]')
            await page.wait_for_timeout(300)
            check("одиночна засічка відкриває дедлайн без зайвого кроку",
                  await page.locator("[data-dlpick]").count() == 0
                  and await page.locator("#sheet-backdrop:not(.hidden)").count() == 1)
            await page.evaluate("closeSheet()")

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
