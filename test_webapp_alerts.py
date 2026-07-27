"""
Тест черги звірки у «Сповіщеннях» Mini App «Команда» (webapp/app.js).

Крок 2 авто-обліку: спірні публікації (суддя не дав high) приїжджають Каті
картками — «бачимо, що Юля додала в IWPR новину: це тендери чи історія з
інфозапиту?». Окремого екрана черги немає — це тип сповіщення.

Що стереже:
- лічильник непрочитаного на пункті меню «Сповіщення»;
- картка показує автора, донора, заголовок із лінком і обґрунтування судді;
- «Зарахувати» → вибір тематики, пропозиція Лиса зверху й позначена;
- рішення шле правильний запит і одразу прибирає картку зі списку;
- «Не те» питає підтвердження (скасування нічого не шле);
- порожня черга — спокійний стан, а не помилка;
- стрічка подій під чергою: непрочитане з крапкою, лічильник меню = спірні +
  непрочитані, вхід на екран позначає прочитаним.

Запуск (потрібні playwright + chromium):
    python test_webapp_alerts.py
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

PROJECTS = [{"id": 43, "name": "Стійкість локального медіа", "partner": "IWPR",
             "logo": None, "logo_orig": None, "start_date": 1740000000,
             "end_date": 1798000000, "kpi_news": 60, "kpi_articles": 10,
             "themes": [], "deadlines": [], "drive_url": None}]

PENDING = [
    {"id": 5, "source": "site", "ref": "321844", "task_id": 71,
     "person": "Юлія Бойченко", "project_id": 43, "confidence": "medium",
     "reasoning": "Матеріал про тендери на укриття — схоже на «Тендери», але "
                  "є ознаки історії з інфозапиту.",
     "status": "pending", "title": "Хто виграв тендери на укриття",
     "url": "https://nikvesti.com/news/economics/321844-tendery",
     "published": "2026-07-25T11:00:00+03:00", "decided_by": None,
     "options": [
         {"id": 71, "theme_name": "Тендери", "type": "news", "qty": 3,
          "done_count": 1, "person": "Юлія Бойченко", "project_id": 43},
         {"id": 72, "theme_name": "Історії з інфозапитів", "type": None, "qty": 2,
          "done_count": 0, "person": "Юлія Бойченко", "project_id": 43},
     ]},
    {"id": 6, "source": "site", "ref": "321901", "task_id": None,
     "person": "Аліна Квітко", "project_id": 43, "confidence": "low",
     "reasoning": "Жодна тематика за змістом не підходить.",
     "status": "pending", "title": "Сесія міськради не відбулась",
     "url": "https://nikvesti.com/news/politics/321901-sesiia",
     "published": "2026-07-26T09:00:00+03:00", "decided_by": None,
     "options": [
         {"id": 80, "theme_name": "Підзвітність влади", "type": None, "qty": 4,
          "done_count": 2, "person": "Аліна Квітко", "project_id": 43},
     ]},
]

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "pending_count": 2, "unread": 2,
    "people": [
        {"name": "Юлія Бойченко", "dept": "creative", "dept_title": "Creative",
         "photo": None, "photo_sm": None, "photo_orig": None},
        {"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
         "photo": None, "photo_sm": None, "photo_orig": None},
    ],
    "projects": PROJECTS, "assignees": [], "managers": [], "tasks": [],
}

NOTIFS = [
    {"id": 31, "kind": "task_done", "kind_title": "Завдання виконано",
     "audience": "managers", "person": None, "object_type": "task",
     "object_id": "71", "title": "Юлія Бойченко: 3 новини · IWPR",
     "body": "зарахувала сама: 3 із 3",
     "url": "https://nikvesti.com/news/economics/321844-tendery",
     "created_at": "2026-07-26T18:20:00+03:00", "unread": True},
    {"id": 30, "kind": "report_deadline", "kind_title": "Звітний дедлайн",
     "audience": "managers", "person": None, "object_type": "deadline",
     "object_id": "3", "title": "Фінансовий звіт · фінальний",
     "body": "IWPR · за 2 дні · пише Олена Бондаренко", "url": "",
     "created_at": "2026-07-25T09:40:00+03:00", "unread": False},
]

STUB = """
window.__posts = [];
window.__reads = [];
window.__opened = [];
window.__confirmAnswer = true;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(u){ window.__opened.push(u); },
  showConfirm(m, c){ c(window.__confirmAnswer); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  const body = opts.body ? JSON.parse(opts.body) : {};
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/matches/pending") return json({ pending: window.PENDING });
  if (url === "/api/notifications") return json({ items: window.NOTIFS, unread: 2 });
  if (url === "/api/notifications/read") {
    window.__reads.push(body);
    window.NOTIFS = window.NOTIFS.map((n) => ({ ...n, unread: false }));
    return json({ ok: true, unread: 0 });
  }
  let m;
  if ((m = url.match(/^\\/api\\/matches\\/(\\d+)\\/decide$/))) {
    window.__posts.push({ id: +m[1], body });
    window.PENDING = window.PENDING.filter((x) => x.id !== +m[1]);
    return json({ match: { id: +m[1] }, tasks: [],
                  pending_count: window.PENDING.length });
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
    await page.add_init_script(
        "window.BOOT = " + json.dumps(BOOTSTRAP) + ";"
        "window.PENDING = " + json.dumps(PENDING) + ";"
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

    print("Черга звірки у «Сповіщеннях»:")
    async with async_playwright() as pw:
        browser, page = await _open(pw)
        try:
            check("лічильник меню = спірні + непрочитані події",
                  await page.inner_text('#bottomnav [data-view="alerts"] .bn-badge') == "4")

            await page.click('[data-view="alerts"]')
            await page.wait_for_selector(".al-card")
            check("обидві спірні публікації показані",
                  await page.locator(".al-card").count() == 2)
            body = await page.inner_text("#content")
            check("видно автора", "Юлія Бойченко" in body)
            check("видно донора і проєкт",
                  "IWPR" in body and "Стійкість локального медіа" in body)
            check("видно заголовок публікації", "Хто виграв тендери на укриття" in body)
            check("видно обґрунтування судді", "історії з інфозапиту" in body)
            check("видно пропозицію судді з прогресом", "Тендери" in body and "1/3" in body)
            check("коли суддя не обрав — так і сказано", "обери сама" in body)
            # --- стрічка подій під чергою ---
            check("стрічка подій показана під чергою",
                  await page.locator(".nt-row").count() == 2)
            check("видно, що завдання виконано і чиє",
                  "Юлія Бойченко: 3 новини" in body and "зарахувала сама" in body)
            check("видно нагадування про звітний дедлайн",
                  "Фінансовий звіт" in body and "Олена Бондаренко" in body)
            check("непрочитане позначене, прочитане — ні",
                  await page.locator(".nt-row.unread").count() == 1)
            await page.screenshot(path="/tmp/alerts-queue.png", full_page=True)

            await page.wait_for_timeout(300)
            check("вхід на екран позначив події прочитаними",
                  await page.evaluate("window.__reads") == [{"all": True}])
            check("лічильник меню лишився тільки за спірними",
                  await page.inner_text('#bottomnav [data-view="alerts"] .bn-badge') == "2")

            # --- лінк на публікацію ---
            await page.click(".al-card .al-title")
            await page.wait_for_timeout(150)
            check("тап по заголовку відкриває матеріал через tg.openLink",
                  (await page.evaluate("window.__opened"))[0].endswith("321844-tendery"))

            # --- «Не те» зі скасуванням ---
            await page.evaluate("window.__confirmAnswer = false")
            await page.click('[data-mreject="5"]')
            await page.wait_for_timeout(250)
            check("скасоване «Не те» нічого не шле",
                  await page.evaluate("window.__posts.length") == 0)

            # --- «Зарахувати» в іншу тематику ---
            await page.click('[data-mconfirm="5"]')
            await page.wait_for_selector("[data-mtask]")
            names = await page.locator(".pk-name").all_inner_texts()
            check("пропозиція Лиса зверху списку", names[0] == "Тендери")
            metas = await page.locator(".pk-meta").all_inner_texts()
            check("пропозиція підписана", "пропозиція Лиса" in metas[0])
            await page.click('[data-mtask="72"]')
            await page.wait_for_timeout(300)
            posts = await page.evaluate("window.__posts")
            check("рішення пішло з обраною тематикою",
                  posts and posts[-1]["id"] == 5
                  and posts[-1]["body"] == {"action": "confirm", "task_id": 72})
            check("картка одразу зникла зі списку",
                  await page.locator(".al-card").count() == 1)
            check("лічильник меню зменшився",
                  await page.inner_text('#bottomnav [data-view="alerts"] .bn-badge') == "1")

            # --- «Не те» з підтвердженням ---
            await page.evaluate("window.__confirmAnswer = true")
            await page.click('[data-mreject="6"]')
            await page.wait_for_timeout(300)
            posts = await page.evaluate("window.__posts")
            check("«Не те» шле reject", posts[-1]["body"] == {"action": "reject"})
            check("черга спорожніла спокійним станом",
                  "Спірних публікацій немає" in await page.inner_text("#content"))
            check("лічильник меню зник",
                  await page.locator('#bottomnav [data-view="alerts"] .bn-badge').count() == 0)
            await page.screenshot(path="/tmp/alerts-empty.png", full_page=True)
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
