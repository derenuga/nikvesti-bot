"""
Блокнот — особистий список справ (webapp/app.js + handlers/team_todos.py).

Олег, 29.07: «блокнот to-do, адмінам теж його. Але щоб /todo у боті теж
записувало задачу. І почитай, які існують найкращі практики todo-лістів, чому
одні ефективні, а інші ні, щоб реалізувати за найкращим прототипом».

Тест стереже саме те, що взято з дослідження, — бо це і є прототип:

- ЗАХОПЛЕННЯ миттєве: Enter додає і ЛИШАЄ фокус (половину зроблених пунктів
  закривають у день запису, 10% — за хвилину; усе вирішує швидкість);
- два кошики «Сьогодні»/«Потім» і перенесення в ОДИН тап — це той мінімальний
  план, який знімає вантаж незавершеного (Masicampo & Baumeister), і найдешевша
  форма «коли» (Gollwitzer);
- мʼякий поріг шести справ на день (Айві Лі): не забороняє, а каже вголос;
- зроблене не зникає мовчки — «Зроблено сьогодні · N» (принцип прогресу);
- вік пункту сказано без докорів («з учора»), бо це інформація для рішення;
- пріоритетів, дедлайнів і тегів немає — кожен коштував би рішення в момент
  захоплення;
- блокнот приватний: у перегляді чужими очима він недоступний;
- він є і в журналістки (меню), і в менеджера (утиліти).

Запуск:  python test_webapp_todos.py
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

ME_JOURNALIST = {"name": "Юлія Лукʼяненко", "first_name": "Юлія",
                 "dept": "newsroom", "dept_title": "Newsroom", "manager": False}
ME_MANAGER = {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
              "dept_title": "Адміністративний", "manager": True}

PEOPLE = [{"name": "Юлія Лукʼяненко", "dept": "newsroom", "dept_title": "Newsroom",
           "photo": None, "photo_sm": None, "photo_orig": None}]


def _boot(me):
    return {"me": me, "site_db": True, "nora": True, "people": PEOPLE,
            "projects": [], "assignees": [], "managers": [], "tasks": []}


def _todo(i, text, bucket="today", done=False, age=0, stale=False):
    return {"id": i, "text": text, "bucket": bucket, "done": done,
            "done_today": done, "age": age, "stale": stale, "source": "app"}


# Сім справ на «сьогодні» — рівно на один більше за мʼякий поріг Айві Лі
TODOS = {
    "today": [_todo(1, "Передзвонити в департамент ЖКГ", age=1),
              _todo(2, "Зверстати афішу"),
              _todo(3, "Запит у поліцію"),
              _todo(4, "Розшифрувати інтервʼю"),
              _todo(5, "Погодити фото"),
              _todo(6, "Написати лід"),
              _todo(7, "Забрати техніку")],
    "later": [_todo(20, "Домовитись про екскурсію на водоканал",
                    bucket="later", age=28, stale=True)],
    "done": [_todo(30, "Відправити запит у ОВА", done=True)],
    "done_today": 1,
    "soft_max": 6,
}

STUB = """
window.__todoCalls = [];
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
  if (url === "/api/todos" && (opts.method || "GET") === "GET") {
    return json(window.TODOS);
  }
  if (url === "/api/todos") {
    const body = JSON.parse(opts.body);
    window.__todoCalls.push({ url, method: "POST", body });
    const todo = { id: 99, text: body.text, bucket: body.bucket, done: false,
                   done_today: false, age: 0, stale: false, source: "app" };
    window.TODOS.today.push(todo);
    return json({ todo });
  }
  if (url.startsWith("/api/todos/")) {
    window.__todoCalls.push({ url, method: opts.method,
                              body: opts.body ? JSON.parse(opts.body) : null });
    return json({ todo: {} });
  }
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  if (url.startsWith("/api/kpi")) return json({ norms: [], week_label: "т",
    month_label: "липень 2026", site_db: true });
  return json({ ok: true });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, me, todos=None):
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
        "window.TODOS = " + json.dumps(TODOS if todos is None else todos) + ";" + STUB)
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
            check("блокнот є пунктом меню журналістки",
                  "Блокнот" in await page.inner_text("#bottomnav"))
            await page.click('#bottomnav [data-view="todo"]')
            await page.wait_for_selector("#td-body .td-row", timeout=5000)

            check("поле запису стоїть зверху, до самого списку",
                  await page.locator("#td-form").count() == 1)
            check("і сказано, що список приватний",
                  "не бачить ніхто" in await page.inner_text(".h-sub"))

            body = await page.inner_text("#td-body")
            low = body.lower()   # заголовки груп CSS малює капсом
            check("два кошики — «Сьогодні» і «Потім»",
                  "сьогодні · 7" in low and "потім · 1" in low)
            check("зроблене лишається на очах як прогрес дня",
                  "зроблено сьогодні · 1" in low)
            check("сім справ на день — поріг сказано вголос",
                  "шести справ" in body)
            check("вік пункту сказано по-людськи, без докорів", "з учора" in body)
            check("а те, що висить місяць у «Потім», підписано",
                  "висить 4 тиж." in body)
            check("пріоритетів у рядку немає — це свідомо",
                  "!" not in body and "Високий" not in body)

            # --- захоплення: Enter додає і лишає фокус ---
            await page.fill("#td-text", "Передзвонити Луковій")
            await page.press("#td-text", "Enter")
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__todoCalls")
            post = [c for c in calls if c["method"] == "POST"]
            check("Enter записує без жодного діалогу",
                  post and post[0]["body"]["text"] == "Передзвонити Луковій")
            check("новий запис іде в «Сьогодні» — записав, отже плануєш робити",
                  post and post[0]["body"]["bucket"] == "today")
            check("поле очистилось під наступний запис",
                  await page.input_value("#td-text") == "")
            check("і фокус лишився в полі — підряд пишуть кілька справ",
                  await page.evaluate("document.activeElement.id") == "td-text")

            # --- один тап: закрити ---
            await page.click('[data-tdone="2"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__todoCalls")
            done = [c for c in calls if c["url"].endswith("/2")]
            check("галочка закриває пункт одним тапом",
                  done and done[0]["body"]["done"] is True)

            # --- один тап: перенести між кошиками ---
            await page.click('[data-tmove="20"]')
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__todoCalls")
            moved = [c for c in calls if c["url"].endswith("/20")]
            check("із «Потім» у «Сьогодні» теж один тап — це і є весь план",
                  moved and moved[0]["body"]["bucket"] == "today")
            await page.screenshot(path="/tmp/ph-shots/todo.png", full_page=True)

            await page.click("[data-back]")
            await page.wait_for_timeout(300)
            check("«Назад» із блокнота працює",
                  await page.evaluate("STATE.view") == "home")
        finally:
            await browser.close()

        # ---------- порожній стан ----------
        browser, page = await _open(pw, ME_JOURNALIST,
                                    {"today": [], "later": [], "done": [],
                                     "done_today": 0, "soft_max": 6})
        try:
            await page.evaluate("nav('todo')")
            await page.wait_for_selector("#td-body .empty-hint", timeout=5000)
            empty = await page.inner_text("#td-body")
            check("порожній стан підказує і другий вхід — /todo у чаті",
                  "/todo" in empty)
        finally:
            await browser.close()

        # ---------- менеджер ----------
        browser, page = await _open(pw, ME_MANAGER)
        try:
            await page.click("#home-tools")
            await page.wait_for_selector("[data-tool]", timeout=3000)
            check("у менеджера блокнот лежить в утилітах",
                  "Блокнот" in await page.inner_text("#sheet"))
            await page.click('[data-tool="todo"]')
            await page.wait_for_selector("#td-body .td-row", timeout=5000)
            check("і відкривається той самий екран",
                  "сьогодні" in (await page.inner_text("#td-body")).lower())

            await page.evaluate("nav('preview', 'Юлія Лукʼяненко')")
            await page.wait_for_selector(".doors", timeout=5000)
            check("у перегляді чужими очима блокнота немає — він приватний",
                  await page.locator('#bottomnav [data-view="todo"]').count() == 0)
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
