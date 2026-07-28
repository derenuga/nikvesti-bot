"""
Тест прогресу виконання «2/3» у Mini App «Команда» (webapp/app.js).

Крок 1 авто-обліку: таски отримали done_count і matches (зараховані публікації
з лінками — вимога Олега: до «виконано» має бути прикріплений матеріал).

Що стереже:
- «2/3» видно в трекері людини і в списку журналістки;
- набраний прогрес зелений, недобраний — синій;
- «0/1» не малюється (шум на кожному односкладовому таску);
- картка таска показує зараховані публікації з лінками;
- тап по лінку відкриває матеріал через tg.openLink, а не всередині апки;
- ОДНУ зараховану публікацію можна зняти тапом по галочці (з підтвердженням),
  не відкидаючи решту — з пʼятнадцяти зарахованих не підходити може одна;
- у списку завдань людини другим рядком донор, а назви проєкту немає;
- у картці є «Зарахувати публікацію за лінком»: вставив URL — пішов запит на
  attach (перенос із задубльованого завдання робиться цим же шляхом);
- екран людини малюється ОДРАЗУ, не чекаючи на KPI (той іде в MySQL сайту і
  думає секунди — раніше тап виглядав як зависання);
- у журналістки зверху ТЕМАТИКА (що писати), під нею «скільки і чого» + донор,
  а назви проєкту немає взагалі — вона займала перший рядок і не відповідала
  на жодне питання;
- тип матеріалу редагується в картці завдання (звідси і брався «матеріал»);
- у журналістки ДВА ПОВЕРХИ: зверху живцем те, заради чого відкривають
  (прогрес і відкриті завдання), знизу двері з числами — «Виконані», «Події»,
  «KPI по місяцях»; за кожними дверима свій екран із поверненням «Назад»;
- стаття важить три новини, і в рядку KPI видно, звідки взялась цифра.

Запуск (потрібні playwright + chromium):
    python test_webapp_progress.py
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


def _task(i, qty, done, status="open", matches=()):
    return {
        "id": i, "person": "Аліна Квітко", "creator": "Катерина Середа",
        "project_id": 1, "project_name": "Голоси Миколаєва",
        "partner_name": "IMS", "platform": None, "type": "article",
        "theme_id": 1, "theme_name": "Репортажі з сесій", "qty": qty,
        "note": "", "deadline": "2026-08-31", "status": status,
        "auto_done": status == "done", "created_at": "2026-07-01T10:00:00+03:00",
        "done_at": None, "done_count": done, "matches": list(matches),
    }


MATCH_1 = {"id": 11, "title": "Сесія міськради ухвалила бюджет",
           "url": "https://nikvesti.com/news/politics/321001-sesiia",
           "published": "2026-07-10T12:00:00+03:00", "source": "site"}
MATCH_2 = {"id": 12, "title": "Депутати не зібрались вдруге",
           "url": "https://nikvesti.com/news/politics/321044-deputaty",
           "published": "2026-07-18T09:30:00+03:00", "source": "site"}

TASKS = [
    _task(1, 3, 2, matches=[MATCH_1, MATCH_2]),          # 2/3 — синій
    _task(2, 2, 2, status="done", matches=[MATCH_1, MATCH_2]),  # 2/2 — виконаний
    _task(3, 1, 0),                                      # 0/1 — не малюємо
    {**_task(4, 2, 0), "type": None, "theme_name": "Тендери",
     "note": "Хто виграв тендери на укриття шкіл"},   # без типу — «матеріали»
]

PEOPLE = [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
           "photo": None, "photo_sm": None, "photo_orig": None}]

PROJECTS = [{"id": 1, "name": "Голоси Миколаєва", "partner": "IMS", "logo": None,
             "logo_orig": None, "start_date": 1740000000, "end_date": 1798000000,
             "kpi_news": 30, "kpi_articles": 5, "themes": [], "deadlines": [],
             "drive_url": None}]

BOOTSTRAP = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": PROJECTS,
    "assignees": [], "managers": [], "tasks": TASKS,
}

# Норма на новини з фактом, у якому 1 новина + 2 статті ×3 = 7
KPI_NORMS = [{
    "id": 1, "dept": "creative", "metric": "news", "period": "month",
    "target": 20, "own": True, "dept_title": "Creative", "period_label": "липень",
    "rows": [{"person": "Аліна Квітко", "fact": 7, "target": 20, "base_target": 20,
              "overridden": False, "note": None, "excused": False, "done": False,
              "articles": 2, "article_weight": 3}],
}]

KPI_HISTORY = {
    "person": "Аліна Квітко", "dept_title": "Creative", "has_norms": True,
    "site_db": True,
    "months": [{"label": "чер 26", "offset": -1, "is_current": False,
                "overall_pct": 60, "norms": []},
               {"label": "лип 26", "offset": 0, "is_current": True,
                "overall_pct": 35, "norms": []}],
}

# Стрічка журналістки: подія зарахування і подія постановки — різні блоки
NOTIFS = [
    {"id": 1, "kind": "task_done", "title": "матеріал · IMS · Голоси Миколаєва",
     "body": "зарахувала сама: 1 із 1", "url": None, "unread": True,
     "created_at": "2026-07-27T10:00:00+03:00"},
    {"id": 2, "kind": "task_assigned", "title": "2 новини · IWPR · Тендери",
     "body": "поставила Катерина Середа", "url": None, "unread": False,
     "created_at": "2026-07-26T10:00:00+03:00"},
]

JOURNALIST = {
    **BOOTSTRAP,
    "me": {"name": "Аліна Квітко", "first_name": "Аліна", "dept": "creative",
           "dept_title": "Creative", "manager": False},
}

STUB = """
window.__opened = [];
window.__posts = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(u){ window.__opened.push(u); },
  showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") {
    // KPI навмисно повільні — як реальна БД сайту
    await new Promise((r) => setTimeout(r, window.__kpiDelay || 0));
    return json({ norms: window.KPI_NORMS || [], week_label: "тиж",
                  month_label: "липень", site_db: true });
  }
  if (url.startsWith("/api/kpi/person")) return json(window.KPI_HISTORY
    || { has_norms: false, months: [] });
  if (url === "/api/notifications") return json({ items: window.NOTIFS || [],
                                                  unread: 1 });
  if (url.startsWith("/api/kpi/dashboard")) {
    // Дашборд навмисно повільний — факт іде в БД сайту
    await new Promise((r) => setTimeout(r, window.__dashDelay || 0));
    const q = new URLSearchParams(url.split("?")[1] || "");
    const period = q.get("period");
    return json({ period, offset: +q.get("offset"), is_current: true,
      site_db: true,
      label: period === "month" ? "липень 2026" : "27.07–02.08",
      people: [{ person: "Аліна Квітко", dept: "creative",
                 dept_title: "Creative", overall_pct: 87, norms: [],
                 missed_days: 8 }] });
  }
  let m;
  if ((m = url.match(/^\\/api\\/tasks\\/(\\d+)$/)) && opts.method === "PATCH") {
    window.__posts.push({ patch: +m[1], body: JSON.parse(opts.body) });
    const t = window.BOOT.tasks.find((x) => x.id === +m[1]);
    return json({ task: { ...t, ...JSON.parse(opts.body) } });
  }
  if ((m = url.match(/^\\/api\\/tasks\\/(\\d+)\\/attach$/))) {
    window.__posts.push({ attach: +m[1], body: JSON.parse(opts.body) });
    const t = window.BOOT.tasks.find((x) => x.id === +m[1]);
    const added = { id: 99, title: "Перенесена публікація",
                    url: "https://nikvesti.com/news/politics/5001-a",
                    published: "2026-07-20T10:00:00+03:00", source: "site" };
    const list = (t.matches || []).concat([added]);
    return json({ tasks: [{ ...t, matches: list, done_count: list.length }],
                  pending_count: 0 });
  }
  if ((m = url.match(/^\\/api\\/matches\\/(\\d+)\\/decide$/))) {
    window.__posts.push({ id: +m[1], body: JSON.parse(opts.body) });
    // Сервер віддає оновлений таск: знята публікація зменшила прогрес
    const t = window.BOOT.tasks.find((x) => (x.matches || [])
      .some((y) => y.id === +m[1]));
    const left = (t.matches || []).filter((y) => y.id !== +m[1]);
    return json({ tasks: [{ ...t, matches: left, done_count: left.length,
                            status: "open", auto_done: false }],
                  pending_count: 0 });
  }
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
        "window.BOOT = " + json.dumps(boot) + ";"
        "window.KPI_NORMS = " + json.dumps(KPI_NORMS) + ";"
        "window.KPI_HISTORY = " + json.dumps(KPI_HISTORY) + ";"
        "window.NOTIFS = " + json.dumps(NOTIFS) + ";" + STUB)
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

    print("Прогрес виконання «2/3» у Mini App «Команда»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw, BOOTSTRAP)
        try:
            # --- трекер людини: малюється до відповіді KPI ---
            await page.evaluate("window.__kpiDelay = 1500")
            await page.click('[data-tracker="Аліна Квітко"]')
            # 600 мс — свідомо менше, ніж «думає» KPI: завдання мають бути вже тут
            await page.wait_for_selector(".task-row", timeout=600)
            check("завдання видно, поки KPI ще вантажаться",
                  await page.locator(".task-row").count() > 0)
            check("на місці KPI — скелетон, а не порожнеча",
                  await page.locator("#person-kpi .sk").count() > 0)
            await page.wait_for_timeout(1600)
            check("KPI приїхали і скелетон зник",
                  await page.locator("#person-kpi .sk").count() == 0)
            await page.evaluate("window.__kpiDelay = 0")
            body0 = await page.inner_text("#content")
            # Назва проєкту не несе інформації — у рядку лишається донор
            check("у трекері другим рядком донор", "IMS" in body0)
            check("назви проєкту в трекері немає", "Голоси Миколаєва" not in body0)
            badges = await page.locator(".prog").all_inner_texts()
            check("у трекері видно «2/3» недобраного таска", "2/3" in badges)
            check("видно «2/2» закритого", "2/2" in badges)
            check("«0/1» не малюється", "0/1" not in badges)
            check("недобраний прогрес не зелений",
                  not await page.locator('.prog:not(.ok)').first.evaluate(
                      "el => el.classList.contains('ok')"))
            check("набраний прогрес зелений",
                  await page.locator(".prog.ok").count() == 1)

            await page.screenshot(path="/tmp/progress-tracker.png", full_page=True)

            # --- картка таска: лінки зарахованого ---
            await page.click('[data-task="1"]')
            await page.wait_for_selector("#sheet .mrow")
            sheet = await page.inner_text("#sheet")
            check("картка каже, скільки зараховано", "Зараховано 2 із 3" in sheet)
            check("видно заголовки зарахованих публікацій",
                  "Сесія міськради ухвалила бюджет" in sheet
                  and "Депутати не зібрались вдруге" in sheet)
            check("видно дату публікації", "10.07" in sheet)
            await page.screenshot(path="/tmp/progress-sheet.png")

            await page.click("#sheet .mr-t")
            await page.wait_for_timeout(200)
            opened = await page.evaluate("window.__opened")
            check("тап по публікації відкриває її через tg.openLink",
                  opened and opened[0].endswith("321001-sesiia"))
            check("апка при цьому нікуди не пішла",
                  page.url.startswith("https://app.local/"))

            # --- зняти ОДНУ публікацію із зарахованих ---
            check("галочка в картці клікабельна",
                  await page.locator("#sheet [data-unmatch]").count() == 2)
            await page.click('#sheet [data-unmatch="11"]')
            await page.wait_for_timeout(400)
            posts = await page.evaluate("window.__posts")
            check("зняття ПОВЕРТАЄ публікацію в чергу, а не ховає назавжди",
                  posts and posts[-1]["id"] == 11
                  and posts[-1]["body"] == {"action": "requeue"})
            sheet2 = await page.inner_text("#sheet")
            check("картка одразу показує «1 із 3», а не 2",
                  "Зараховано 1 із 3" in sheet2)
            check("решта зарахованих лишилась на місці",
                  "Депутати не зібрались вдруге" in sheet2
                  and "Сесія міськради ухвалила бюджет" not in sheet2)

            # --- зарахувати публікацію руками, за лінком ---
            await page.click("#task-attach")
            await page.wait_for_selector("#at-url")
            await page.fill("#at-url", "https://nikvesti.com/news/politics/5001-a")
            await page.click("#at-save")
            await page.wait_for_timeout(400)
            posts = await page.evaluate("window.__posts")
            check("лінк пішов на attach потрібного завдання",
                  posts[-1].get("attach") == 1
                  and posts[-1]["body"]["url"].endswith("5001-a"))
            check("картка одразу показує зараховану публікацію",
                  "Перенесена публікація" in await page.inner_text("#sheet"))
        finally:
            await browser.close()

        # --- журналістка ---
        browser, page = await _open(pw, JOURNALIST)
        try:
            await page.wait_for_selector(".task-row")
            body = await page.inner_text("#content")
            check("журналістка теж бачить свій прогрес", "2/3" in body)
            check("і бачить, що саме зараховано",
                  "Сесія міськради ухвалила бюджет" in body)
            # Головне питання Аліни: «що мені писати?»
            titles = await page.locator(".task-row .tr-who").all_inner_texts()
            check("зверху рядка — ТЕМАТИКА, а не донор", "Тендери" in titles)
            rows_txt = " ".join(await page.locator(
                ".task-row:not(.nt-row)").all_inner_texts())
            check("назви проєкту в списку немає взагалі",
                  "Голоси Миколаєва" not in rows_txt)
            check("донор лишився дрібним рядком", "IMS" in body)
            check("нотатка редактора видима",
                  "Хто виграв тендери на укриття шкіл" in body)

            # --- два поверхи: зверху робота, знизу двері з числами ---
            check("виконаних НА ГОЛОВНІЙ більше немає (вони за дверима)",
                  "2/2" not in body)
            doors = await page.locator(".door").all_inner_texts()
            check("двері «Виконані» з числом",
                  any("Виконані" in d and "1" in d for d in doors))
            check("двері «Події»", any("Події" in d for d in doors))
            check("двері «KPI по місяцях»",
                  any("KPI по місяцях" in d for d in doors))
            check("графік на головній більше не займає екран",
                  await page.locator("#my-hist-chart").count() == 0)

            # --- за дверима «Виконані» ---
            await page.click('.door[data-nav="mydone"]')
            await page.wait_for_selector(".task-row.is-done", timeout=3000)
            check("виконані знайшлись за своїми дверима",
                  "2/2" in await page.inner_text("#content"))
            await page.click('.back[data-nav="home"]')
            await page.wait_for_selector(".door", timeout=3000)

            # --- за дверима «Події»: стрічка двома названими блоками ---
            await page.click('.door[data-nav="myfeed"]')
            await page.wait_for_selector(".nt-row", timeout=3000)
            feed = await page.inner_text("#content")
            check("замість «Що нового» — «Нещодавно зараховані»",
                  "Нещодавно зараховані" in feed and "Що нового" not in feed)
            check("постановка завдань — окремим блоком",
                  "Нові завдання" in feed)
            check("подія зарахування пішла у свій блок",
                  feed.index("Нещодавно зараховані") < feed.index("Голоси Миколаєва")
                  < feed.index("Нові завдання"))
            await page.click('.back[data-nav="home"]')
            await page.wait_for_selector(".door", timeout=3000)

            # --- за дверима «KPI по місяцях»: графік цілим екраном ---
            await page.click('.door[data-nav="myhist"]')
            await page.wait_for_selector("#my-hist-chart", timeout=3000)
            check("графік відкрився окремим екраном",
                  await page.locator("#my-hist-chart .hb-col").count() > 0)
            check("тумблера згортання більше немає",
                  await page.locator("#hist-toggle").count() == 0)
            await page.click('.back[data-nav="home"]')
            await page.wait_for_selector(".door", timeout=3000)

            # --- вага статей у «Моїх KPI» ---
            check("у рядку KPI пояснено, звідки цифра",
                  "+2 статті ×3" in await page.inner_text("#my-kpi"))
            check("сам факт зважений", "7/20" in await page.inner_text("#my-kpi"))
            await page.screenshot(path="/tmp/progress-journalist.png", full_page=True)
        finally:
            await browser.close()

        # --- «Звіт»: перемикання тиждень/місяць не виглядає зависанням ---
        browser, page = await _open(pw, BOOTSTRAP)
        try:
            await page.evaluate("window.__dashDelay = 900")
            await page.click('[data-hv="report"]')
            await page.wait_for_selector(".ring-cell", timeout=10000)
            check("на «Звіті» спершу тиждень", await page.locator(
                '[data-dp="week"].on').count() == 1)

            await page.click('[data-dp="month"]')
            await page.wait_for_timeout(150)      # відповідь ще їде
            check("перемикач ОДРАЗУ переїхав на «Місяць»",
                  await page.locator('[data-dp="month"].on').count() == 1)
            check("замість старих цифр — скелетон",
                  await page.locator(".sk-ring").count() > 0
                  and await page.locator(".ring-cell").count() == 0)
            check("підпис періоду теж шимерить, а не бреше старим",
                  await page.locator(".month-nav .sk-lbl").count() == 1)
            await page.screenshot(path="/tmp/progress-dash-loading.png")

            await page.wait_for_selector(".ring-cell", timeout=10000)
            check("дані приїхали і скелетон зник",
                  await page.locator(".sk-ring").count() == 0
                  and "липень 2026" in (await page.inner_text(".month-nav")).lower())

            # Швидкі тики: перемогти має ОСТАННІЙ вибір, а не перша відповідь
            await page.click('[data-dp="week"]')
            await page.wait_for_timeout(50)
            await page.click('[data-dp="month"]')
            await page.wait_for_selector(".ring-cell", timeout=10000)
            await page.wait_for_timeout(1200)
            check("повільна відповідь тижня не перебила свіжий місяць",
                  await page.locator('[data-dp="month"].on').count() == 1
                  and "липень 2026" in (await page.inner_text(".month-nav")).lower())
        finally:
            await browser.close()

    ok = 0
    for name, passed in results:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok += passed
    print(f"{ok}/{len(results)} перевірок пройдено")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
