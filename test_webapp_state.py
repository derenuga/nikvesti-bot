"""
Тест локальних патчів STATE у Mini App «Команда» (webapp/app.js).

Навіщо (ревізія 27.07.2026): після КОЖНОЇ дії апка робила повний
/api/bootstrap — створив таску, а перечитуються люди, фото, всі проєкти,
тематики, дедлайни й Drive. Це і затримка перед тим, як дія стане видимою,
і зайва робота обом БД (проєкти й фото — з БД сайту з жорсткими лімітами).
Роути мутацій і так повертають змінений об'єкт, тож апка вкладає його в
STATE сама.

Тест перевіряє дві речі, які легко зламати таким рефактором:
  1. bootstrap справді перестав ходити після дій (рахуємо запити);
  2. екран після дії показує ПРАВДУ — те саме, що показав би повний
     перечит із сервера (порівнюємо з еталоном, зібраним із фікстури).

Плюс перевіряє, що зміни колеги підтягуються при поверненні в апку.

Запуск (потрібні playwright + chromium):
    python test_webapp_state.py
"""

import asyncio
import copy
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

PROJECT = {
    "id": 7, "name": "Голоси Миколаєва", "partner": "IMS",
    "logo": None, "logo_orig": None,
    "start_date": 1750000000, "end_date": 1800000000,
    "kpi_news": 60, "kpi_articles": 10,
    "themes": [{"id": 1, "name": "Репортажі з сесій", "planned": 20, "format": "news"}],
    "deadlines": [{"id": 1, "project_id": 7, "kind": "narrative", "stage": "interim",
                   "title": "", "due": "2026-09-30"}],
    "drive_url": None,
}

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [
        {"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
         "photo": None, "photo_orig": None},
        {"name": "Таміла Ксьонжик", "dept": "newsroom", "dept_title": "Newsroom",
         "photo": None, "photo_orig": None},
    ],
    "projects": [PROJECT],
    "tasks": [
        {"id": 101, "person": "Аліна Квітко", "creator": "Катерина Середа",
         "project_id": 7, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
         "platform": None, "type": "news", "theme_id": 1, "theme_name": "Репортажі з сесій",
         "qty": 2, "note": "", "deadline": None, "status": "open",
         "created_at": "2026-07-27T10:00:00+03:00", "done_at": None},
    ],
}

# Стаб сервера: веде власний стан і відповідає як справжні роути.
STUB = """
window.__bootstrapCalls = 0;
window.__server = JSON.parse(JSON.stringify(window.BOOT));
window.__nextId = 500;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(msg, cb){ cb(true); },
  HapticFeedback: { notificationOccurred(){} } } };

window.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : {};
  const S = window.__server;
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  const proj = (id) => S.projects.find((p) => p.id === +id);

  if (url === "/api/bootstrap") { window.__bootstrapCalls++; return json(S); }
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });

  let m;
  if (url === "/api/tasks" && method === "POST") {
    const p = proj(body.project_id);
    const theme = p && (p.themes.find((t) => t.id === body.theme_id));
    const task = { id: ++window.__nextId, person: body.person, creator: "Катерина Середа",
      project_id: body.project_id || null, project_name: p ? p.name : null,
      partner_name: p ? p.partner : null, platform: body.platform || null,
      type: body.type || null, theme_id: body.theme_id || null,
      theme_name: theme ? theme.name : null, qty: body.qty, note: body.note || "",
      deadline: body.deadline || null, status: "open",
      created_at: "2026-07-27T12:00:00+03:00", done_at: null };
    S.tasks.unshift(task);
    return json({ task });
  }
  if ((m = url.match(/^\\/api\\/tasks\\/(\\d+)$/)) && method === "PATCH") {
    const t = S.tasks.find((x) => x.id === +m[1]);
    Object.assign(t, body);
    if (body.theme_id !== undefined) {
      const p = proj(t.project_id);
      const th = p && p.themes.find((x) => x.id === body.theme_id);
      t.theme_name = th ? th.name : null;
    }
    return json({ task: JSON.parse(JSON.stringify(t)) });
  }
  if (url === "/api/tasks/bulk" && method === "POST") {
    const p = proj(body.project_id);
    const theme = p && p.themes.find((t) => t.id === body.theme_id);
    const tasks = body.items.map((it) => ({
      id: ++window.__nextId, person: it.person, creator: "Катерина Середа",
      project_id: p.id, project_name: p.name, partner_name: p.partner,
      platform: null, type: theme && theme.format === "news" ? "news" : null,
      theme_id: theme ? theme.id : null, theme_name: theme ? theme.name : null,
      qty: it.qty, note: "", deadline: body.month + "-30", status: "open",
      created_at: "2026-07-27T12:00:00+03:00", done_at: null }));
    tasks.forEach((t) => S.tasks.unshift(t));
    return json({ created: tasks.length, tasks });
  }
  if (url === "/api/themes" && method === "POST") {
    const theme = { id: ++window.__nextId, project_id: body.project_id,
      name: body.name, planned: body.planned, format: body.format };
    proj(body.project_id).themes.push(theme);
    return json({ theme });
  }
  if ((m = url.match(/^\\/api\\/themes\\/(\\d+)$/)) && method === "PATCH") {
    for (const p of S.projects) {
      const th = p.themes.find((t) => t.id === +m[1]);
      if (th) { Object.assign(th, body); return json({ theme: JSON.parse(JSON.stringify(th)) }); }
    }
  }
  if ((m = url.match(/^\\/api\\/themes\\/(\\d+)$/)) && method === "DELETE") {
    for (const p of S.projects) p.themes = p.themes.filter((t) => t.id !== +m[1]);
    return json({ ok: true });
  }
  if (url === "/api/project_deadlines" && method === "POST") {
    const dl = { id: ++window.__nextId, project_id: body.project_id, kind: body.kind,
      stage: body.stage, title: body.title || "", due: body.due };
    const p = proj(body.project_id);
    p.deadlines.push(dl);
    p.deadlines.sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : a.id - b.id));
    return json({ deadline: dl });
  }
  if ((m = url.match(/^\\/api\\/project_deadlines\\/(\\d+)$/)) && method === "DELETE") {
    for (const p of S.projects) p.deadlines = p.deadlines.filter((d) => d.id !== +m[1]);
    return json({ ok: true });
  }
  if ((m = url.match(/^\\/api\\/projects\\/(\\d+)\\/drive$/)) && method === "PUT") {
    proj(m[1]).drive_url = body.url || null;
    return json({ ok: true, url: body.url || null });
  }
  if (url === "/api/people/dept" && method === "PUT") {
    const titles = { newsroom: "Newsroom", creative: "Creative", digital: "Діджитал та дистрибуція" };
    const person = S.people.find((x) => x.name === body.person);
    person.dept = body.dept; person.dept_title = titles[body.dept];
    return json({ ok: true });
  }
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _new_page(pw):
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
        "window.BOOT = " + json.dumps(BOOTSTRAP) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _state_vs_server(page):
    """Чи збігається локальний STATE з тим, що віддав би повний bootstrap."""
    return await page.evaluate("""() => {
      const norm = (d) => JSON.stringify({
        tasks: (d.tasks || []).map(t => [t.id, t.status, t.qty, t.theme_name, t.person]).sort(),
        projects: (d.projects || []).map(p => [p.id, p.drive_url,
          (p.themes || []).map(t => [t.id, t.name, t.planned, t.format]).sort(),
          (p.deadlines || []).map(x => [x.id, x.due, x.kind]).sort()]).sort(),
        people: (d.people || []).map(p => [p.name, p.dept, p.dept_title]).sort(),
      });
      return { app: norm(window.STATE_FOR_TEST()), server: norm(window.__server) };
    }""")


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Локальні патчі STATE Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _new_page(pw)
        try:
            # app.js тримає STATE у замиканні модуля — дістаємо через глобалку
            await page.evaluate("window.STATE_FOR_TEST = () => STATE")
            await page.evaluate("window.__bootstrapCalls = 0")

            # --- сценарій Каті: сім дій поспіль ---
            # 1. поставити таску через «+»
            await page.click(".bn-plus")
            await page.click("[data-person]")
            await page.wait_for_selector("#f-create")
            await page.click("#f-project")
            await page.click('[data-proj="7"]')
            await page.wait_for_selector("#f-create:not([disabled])")
            await page.click("#f-create")
            await page.wait_for_timeout(350)
            check("нова таска з'явилась на Головній",
                  "Аліна" in await page.inner_text("#content"))

            # 2. закрити таску
            await page.click("[data-tracker]")
            await page.wait_for_selector('[data-task="101"]')
            await page.click('[data-task="101"]')
            await page.wait_for_selector('[data-status="done"]')
            await page.click('[data-status="done"]')
            await page.wait_for_timeout(350)

            # 3-5. тематика: додати, змінити, потім дедлайн
            await page.click('[data-view="projects"]')
            await page.wait_for_selector("[data-project]")
            await page.click("[data-project]")
            await page.wait_for_selector("#add-theme")
            await page.click("#add-theme")
            await page.fill("#t-name", "Інфозапити")
            await page.fill("#t-planned", "15")
            await page.click("#t-save")
            await page.wait_for_timeout(300)
            check("нова тематика видима у проєкті",
                  "Інфозапити" in await page.inner_text("#content"))

            await page.click("#add-dl")
            await page.fill("#dl-due", "2026-08-15")
            await page.click("#dl-save")
            await page.wait_for_timeout(300)
            check("новий дедлайн видимий",
                  "15.08.26" in await page.inner_text("#content"))

            # 6. папка Drive
            await page.click("#drive-attach")
            await page.fill("#dr-url", "https://drive.google.com/drive/folders/abc")
            await page.click("#dr-save")
            await page.wait_for_timeout(300)
            check("папка Drive прикріпилась",
                  "Папка проєкту" in await page.inner_text("#content"))

            # 7. масова постановка з тематики
            await page.click("[data-bulk-theme]")
            await page.wait_for_selector("[data-bq]")
            await page.click('[data-bq][data-d="1"]')
            await page.click("#bulk-save")
            await page.wait_for_timeout(400)

            # 8. перенести людину між відділами (Команда — таб усередині KPI)
            await page.click('[data-view="home"]')
            await page.wait_for_selector('[data-nav="kpi"]')
            await page.click('[data-nav="kpi"]')
            await page.wait_for_selector('[data-kt="team"]')
            await page.click('[data-kt="team"]')
            await page.wait_for_selector("[data-move]")
            await page.click("[data-move]")
            await page.click('[data-d="digital"]')
            await page.click("#d-save")
            await page.wait_for_timeout(300)
            check("людина переїхала у «Діджитал»",
                  "Діджитал" in await page.inner_text("#content"))

            calls = await page.evaluate("window.__bootstrapCalls")
            print(f"\n  /api/bootstrap за 8 дій: {calls} (було б 8)")
            check("повний bootstrap після дій не ходить", calls == 0)

            # --- головне: STATE не розійшовся з сервером ---
            cmp = await _state_vs_server(page)
            check("STATE збігається з тим, що віддав би повний перечит",
                  cmp["app"] == cmp["server"])
            if cmp["app"] != cmp["server"]:
                print("    APP:   ", cmp["app"][:400])
                print("    SERVER:", cmp["server"][:400])

            # --- зміни колеги підтягуються при поверненні в апку ---
            await page.evaluate("""() => {
              window.__server.tasks.unshift({ id: 999, person: "Таміла Ксьонжик",
                creator: "Олег Деренюга", project_id: null, project_name: null,
                partner_name: null, platform: null, type: "news", theme_id: null,
                theme_name: "Правка Олега", qty: 1, note: "", deadline: null,
                status: "open", created_at: "2026-07-27T13:00:00+03:00", done_at: null });
              lastLoadedAt = 0;   // ніби минуло більше 30 с
            }""")
            await page.evaluate("""() => {
              Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
              document.dispatchEvent(new Event("visibilitychange"));
            }""")
            await page.wait_for_timeout(500)
            await page.click('[data-view="home"]')
            await page.wait_for_timeout(200)
            check("зміна колеги підтяглась при поверненні в апку",
                  "Таміла" in await page.inner_text("#content"))
            check("для цього bootstrap викликався рівно раз",
                  await page.evaluate("window.__bootstrapCalls") == 1)
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
