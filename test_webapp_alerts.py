"""
Тест черги звірки у «Сповіщеннях» Mini App «Команда» (webapp/app.js).

Крок 2 авто-обліку: спірні публікації (суддя не дав high) приїжджають Каті
картками — «бачимо, що Юля додала в IWPR новину: це тендери чи історія з
інфозапиту?». Окремого екрана черги немає — це тип сповіщення.

Що стереже:
- лічильник непрочитаного на пункті меню «Сповіщення»;
- картка читається як новина: фото автора зверху, заголовок, дата, опис із
  мініатюрою праворуч, рядок проєкту з лого донора і підказка тематики;
- «Зарахувати» → вибір тематики, пропозиція Лиса зверху й позначена;
- рішення шле правильний запит і одразу прибирає картку зі списку;
- «Не те» питає підтвердження (скасування нічого не шле);
- порожня черга — спокійний стан, а не помилка;
- стрічка подій під чергою: непрочитане з крапкою, лічильник меню = спірні +
  непрочитані, вхід на екран позначає прочитаним;
- пост Telegram (автор невідомий) питає КОМУ і КУДИ зараховувати — рядки з
  людьми, а не лише з тематиками;
- кнопка в шапці додає публікацію в чергу за лінком (прогін бачить не все);
- прогрес у РЕШТІ карток черги оновлюється одразу після зарахування (у проєкті
  часто десяток спірних публікацій на одне завдання), а набране завдання
  зникає з вибору.

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
     "description": "Миколаївські школи закупили укриття на 40 мільйонів — "
                    "з'ясовуємо, хто виграв ці тендери.",
     "image": "https://nikvesti.com/600x315/images/imageeditor/321844.webp",
     "url": "https://nikvesti.com/news/economics/321844-tendery",
     "published": "2026-07-25T11:00:00+03:00", "decided_by": None,
     "options": [
         {"id": 71, "theme_id": 1, "theme_name": "Тендери", "type": "news",
          "qty": 3, "done_count": 1, "person": "Юлія Бойченко", "project_id": 43},
         {"id": 72, "theme_id": 2, "theme_name": "Історії з інфозапитів",
          "type": None, "qty": 2, "done_count": 0, "person": "Юлія Бойченко",
          "project_id": 43},
     ],
     "themes": [{"id": 1, "name": "Тендери", "format": "news"},
                {"id": 2, "name": "Історії з інфозапитів", "format": None},
                {"id": 3, "name": "Критичні інформаційні потреби", "format": None}]},
    {"id": 6, "source": "site", "ref": "321901", "task_id": None,
     "person": "Аліна Квітко", "project_id": 43, "confidence": "low",
     "reasoning": "Жодна тематика за змістом не підходить.",
     "status": "pending", "title": "Сесія міськради не відбулась",
     "description": "Депутати не зібрались вдруге поспіль.", "image": "",
     "url": "https://nikvesti.com/news/politics/321901-sesiia",
     "published": "2026-07-26T09:00:00+03:00", "decided_by": None,
     "options": [
         {"id": 80, "theme_id": 4, "theme_name": "Підзвітність влади", "type": None,
          "qty": 4, "done_count": 2, "person": "Аліна Квітко", "project_id": 43},
     ],
     "themes": [{"id": 4, "name": "Підзвітність влади", "format": None},
                {"id": 3, "name": "Критичні інформаційні потреби", "format": None}]},
    {"id": 7, "source": "telegram", "ref": "82296", "task_id": None,
     "person": None, "project_id": 43, "confidence": None,
     "reasoning": "Пост каналу з дисклеймером цього проєкту — автора Telegram "
                  "не показує, тож кому зарахувати, вирішує редакторка.",
     "status": "pending",
     "title": "Як пройшла позачергова сесія Баштанської міської ради 3 липня",
     "description": "Депутати розглянули бюджет, водоканал, освіту.",
     "image": "https://cdn4.telegram-cdn.org/file/preview.jpg",
     "url": "https://t.me/nikvesti/82296",
     "published": "2026-07-03T16:10:00+03:00", "decided_by": None,
     "options": [
         {"id": 71, "theme_id": 1, "theme_name": "Тендери", "type": "news",
          "qty": 3, "done_count": 1, "person": "Юлія Бойченко", "project_id": 43},
         {"id": 80, "theme_id": 4, "theme_name": "Підзвітність влади", "type": None,
          "qty": 4, "done_count": 2, "person": "Аліна Квітко", "project_id": 43},
     ],
     "themes": [{"id": 1, "name": "Тендери", "format": "news"},
                {"id": 4, "name": "Підзвітність влади", "format": None}]},
]

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True, "pending_count": 3, "unread": 2,
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
  if (url === "/api/matches/queue") {
    window.__posts.push({ queue: true, body });
    // Лінк, який уже комусь зараховано: сервер каже про конфлікт, а не мовчки
    // знімає — зняття лише з force
    if (body.url.includes("321844") && !body.force) {
      return json({ conflict: "counted", title: "Хто виграв тендери на укриття",
                    person: "Юлія Бойченко", theme_name: "Тендери",
                    project_name: "Стійкість локального медіа",
                    partner_name: "IWPR" });
    }
    return json({ match: { id: 99, status: "pending" },
                  tasks: body.force ? [{ id: 71, done_count: 0, qty: 3,
                                         status: "open", matches: [] }] : [],
                  pending_count: window.PENDING.length + 1 });
  }
  if (url === "/api/notifications/read") {
    window.__reads.push(body);
    window.NOTIFS = window.NOTIFS.map((n) => ({ ...n, unread: false }));
    return json({ ok: true, unread: 0 });
  }
  let m;
  if ((m = url.match(/^\\/api\\/tasks\\/(\\d+)$/)) && opts.method === "PATCH") {
    window.__posts.push({ patch: +m[1], body });
    const t = (STATE.tasks || []).find((x) => x.id === +m[1]) || { id: +m[1] };
    return json({ task: { ...t, ...body } });
  }
  if ((m = url.match(/^\\/api\\/matches\\/(\\d+)\\/decide$/))) {
    window.__posts.push({ id: +m[1], body });
    // Сервер віддає ОНОВЛЕНІ таски (як справжній роут після attach_progress)
    const decided = window.PENDING.find((x) => x.id === +m[1]);
    const opt = (decided && decided.options || []).find((o) => o.id === body.task_id);
    const tasks = opt ? [{ id: opt.id, qty: opt.qty, status: "open",
                           done_count: opt.done_count + 1 }] : [];
    window.PENDING = window.PENDING.filter((x) => x.id !== +m[1]);
    return json({ match: { id: +m[1] }, tasks,
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
                  await page.inner_text('#bottomnav [data-view="alerts"] .bn-badge') == "5")

            await page.click('[data-view="alerts"]')
            await page.wait_for_selector(".al-card")
            check("усі спірні випадки показані",
                  await page.locator(".al-card").count() == 3)
            body = await page.inner_text("#content")
            check("видно автора", "Юлія Бойченко" in body)
            check("видно проєкт", "Стійкість локального медіа" in body)
            check("видно заголовок публікації", "Хто виграв тендери на укриття" in body)
            check("видно опис матеріалу, а не переказ судді",
                  "хто виграв ці тендери" in body and "Публікація стосується" not in body)
            check("видно дату виходу", "25 липня 2026" in body)
            check("мініатюра новини показана",
                  await page.locator(".al-thumb img").count() == 2)
            check("рядок проєкту з донором",
                  "додано до проєкту" in body and "IWPR" in body)
            check("видно підказку тематики з прогресом",
                  "Схоже тематика" in body and "1/3" in body)
            check("коли суддя не обрав — так і сказано", "обери вручну" in body)
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
                  await page.inner_text('#bottomnav [data-view="alerts"] .bn-badge') == "3")

            # --- пост Telegram: автора немає, питаємо кому і куди ---
            check("пост каналу підписаний як пост, а не як автор",
                  "Пост каналу" in await page.inner_text("#content"))
            await page.click('[data-mconfirm="7"]')
            await page.wait_for_selector("[data-mperson]")
            sheet = await page.inner_text("#sheet")
            check("для поста спершу питає, кому зарахувати", "Кому зарахувати" in sheet)
            check("у списку люди з завданнями проєкту",
                  "Юлія Бойченко" in sheet and "Аліна Квітко" in sheet)
            await page.screenshot(path="/tmp/alerts-tg-pick.png")
            await page.click('[data-mperson="Аліна Квітко"]')
            await page.wait_for_selector("[data-mtask]")
            check("далі — її завдання в цьому проєкті",
                  "Підзвітність влади" in await page.inner_text("#sheet"))
            await page.click('[data-mtask="80"]')
            await page.wait_for_timeout(300)
            posts = await page.evaluate("window.__posts")
            check("рішення по посту шле обраний таск (людину бере сервер)",
                  posts[-1]["id"] == 7
                  and posts[-1]["body"] == {"action": "confirm", "task_id": 80})

            # --- додати публікацію в чергу за лінком ---
            await page.click("#queue-add")
            await page.wait_for_selector("#q-url")
            await page.fill("#q-url", "https://nikvesti.com/news/politics/5001-a")
            await page.click("#q-save")
            await page.wait_for_timeout(400)
            queued = [p for p in await page.evaluate("window.__posts") if p.get("queue")]
            check("кнопка в шапці шле лінк у чергу",
                  queued and queued[-1]["body"]["url"].endswith("5001-a"))

            # --- лінк, який уже зараховано: питаємо, а не знімаємо мовчки ---
            await page.evaluate("window.__posts = []; window.__confirmAnswer = false")
            await page.click("#queue-add")
            await page.wait_for_selector("#q-url")
            await page.fill("#q-url", "https://nikvesti.com/news/economics/321844-tendery")
            await page.click("#q-save")
            await page.wait_for_timeout(400)
            posts = await page.evaluate("window.__posts")
            check("на вже зараховану публікацію сервер віддає конфлікт",
                  len(posts) == 1 and not posts[0]["body"].get("force"))
            check("скасування діалогу нічого не змінює — force не пішов",
                  all(not p["body"].get("force") for p in posts))
            await page.evaluate("window.__confirmAnswer = true")
            await page.click("#q-save")
            await page.wait_for_timeout(400)
            posts = await page.evaluate("window.__posts")
            check("після підтвердження летить force — знімаємо і в чергу",
                  posts[-1]["body"].get("force") is True)
            await page.evaluate("window.__posts = []")
            body = await page.inner_text("#content")

            # --- лінк на публікацію ---
            await page.click(".al-card .al-title")
            await page.wait_for_timeout(150)
            check("тап по заголовку відкриває матеріал через tg.openLink",
                  (await page.evaluate("window.__opened"))[0].endswith("321844-tendery"))

            # --- «Не те» зі скасуванням і з підтвердженням ---
            await page.evaluate("window.__posts = []; window.__confirmAnswer = false")
            await page.click('[data-mreject="5"]')
            await page.wait_for_timeout(250)
            check("скасоване «Не те» нічого не шле",
                  await page.evaluate("window.__posts.length") == 0)
            await page.evaluate("window.__confirmAnswer = true")

            # --- тематика, на яку завдання ЩЕ НЕ ставили (випадок Олега:
            #     «а де кнопка зарахувати в критичні інформаційні потреби?») ---
            await page.click('[data-mconfirm="6"]')
            await page.wait_for_selector("[data-mtheme]")
            sheet = await page.inner_text("#sheet")
            check("тематика без завдання теж пропонується",
                  "Критичні інформаційні потреби" in sheet)
            check("і чесно каже, що заведе завдання",
                  "заведу і зарахую" in sheet)
            check("тематика, на яку завдання вже є, у другий список не дублюється",
                  sheet.count("Підзвітність влади") == 1)
            await page.screenshot(path="/tmp/alerts-theme-pick.png")
            await page.click('[data-mtheme="3"]')
            await page.wait_for_timeout(300)
            posts = await page.evaluate("window.__posts")
            check("рішення шле тематику і людину, а не таск",
                  posts[-1]["body"] == {"action": "confirm", "theme_id": 3,
                                        "person": "Аліна Квітко"})

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

            # Прогрес у сусідніх картках: у проєкті буває десяток спірних
            # публікацій на одне завдання, і «14/15» висіло старим до перезаходу
            check("сусідні картки бачать новий прогрес одразу",
                  await page.evaluate("""(() => {
                    STATE.pending = [{ id: 91, options: [
                      { id: 71, theme_name: 'Тендери', qty: 15, done_count: 14 }] }];
                    patchPendingOptions([{ id: 71, qty: 15, done_count: 15,
                                           status: 'open' }]);
                    return STATE.pending[0].options[0].done_count;
                  })()""") == 15)
            check("набране й закрите завдання зникає з вибору",
                  await page.evaluate("""(() => {
                    STATE.pending = [{ id: 92, options: [
                      { id: 71, theme_name: 'Тендери', qty: 3, done_count: 2 }] }];
                    patchPendingOptions([{ id: 71, qty: 3, done_count: 3,
                                           status: 'done' }]);
                    return STATE.pending[0].options.length;
                  })()""") == 0)
            check("картка одразу зникла зі списку",
                  await page.locator(".al-card").count() == 0)
            check("лічильник меню зменшився",
                  await page.locator('#bottomnav [data-view="alerts"] .bn-badge').count() == 0)

            check("черга спорожніла спокійним станом",
                  "Спірних публікацій немає" in await page.inner_text("#content"))
            await page.screenshot(path="/tmp/alerts-empty.png", full_page=True)

            # --- прострочені завдання (Олег, 29.07) ---
            await page.evaluate("""() => {
              STATE.tasks = [{ id: 901, person: 'Аліна Квітко',
                creator: 'Катерина Середа', project_id: 1,
                project_name: 'Голоси Миколаєва', partner_name: 'IMS',
                platform: null, type: 'news', theme_id: 1,
                theme_name: 'Тендери', qty: 2, note: '', deadline: '2020-01-31',
                status: 'open', auto_done: false, done_count: 0, matches: [] }];
              nav('alerts');
            }""")
            await page.wait_for_selector("[data-oext]", timeout=5000)
            card = await page.inner_text("#alerts-body")
            # .dept-title малює великими літерами — порівнюємо в одному регістрі
            check("прострочене завдання підняте окремим блоком",
                  "СТРОК МИНУВ" in card.upper())
            check("сказано, чий строк і коли був",
                  "31.01" in card and "Аліна" in card)
            check("є дві дії — продовжити і зняти",
                  await page.locator("[data-oext]").count() == 1
                  and await page.locator("[data-odrop]").count() == 1)
            check("прострочене потрапило в лічильник меню",
                  await page.locator(
                      '#bottomnav [data-view="alerts"] .bn-badge').count() == 1)

            await page.click("[data-oext]")
            await page.wait_for_selector("#od-date", timeout=3000)
            check("у шторці є швидкі варіанти строку",
                  await page.locator("[data-od]").count() == 3)
            await page.locator("[data-od]").nth(1).click()
            await page.wait_for_timeout(400)
            sent = await page.evaluate("window.__posts.slice(-1)[0]")
            check("продовження їде як PATCH дедлайну",
                  bool(sent) and sent.get("patch") == 901
                  and "deadline" in (sent.get("body") or {}))
            check("новий строк у майбутньому",
                  bool(sent) and sent["body"]["deadline"] > "2020-01-31")
            check("картка зникла зі «Строк минув»",
                  await page.locator("[data-oext]").count() == 0)
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
