"""
Тест екрана «Розбір» — свайп-колоди банку тем (webapp/app.js).

Навіщо екран. Ревізія 17.08 показала, що значуще в банку тонуло у процедурних
кроках і рутині приблизно 1:4. Модель тепер розкладає записи за типом акту й
ховає тінь із черги сама, але межа «значущий процес / шум» людська, і
розбирають її двоє — Олег і Катя. Тому екран мусить бути приємним: у нудний
не зайдуть жодного разу, і вісь kind назавжди лишиться модельною.

Що стереже цей тест:

- двері «Розбору» бачить ЛИШЕ менеджер і лише коли в тіні щось є (двері без
  числа не відкривають — правило апки);
- колода тримає три картки одна на одній, верхня падає зверху (анімація
  tdrop) — саме це просив Олег;
- перетягування ловиться пальцем: картка їде за ним із поворотом, кольорова
  печатка проявляється за напрямком;
- відпускання ЗА порогом ухвалює рішення, до порогу — повертає картку;
- свайп ліворуч шле noise, праворуч — good, і саме на ту картку, що зверху;
- рішення застосовується ОПТИМІСТИЧНО: наступна картка стає верхньою, не
  чекаючи відповіді сервера;
- збій мережі повертає картку назад у колоду (нічого не зникає мовчки);
- кнопки внизу дублюють свайп повністю — у десктопному Telegram свайпів
  немає, і без них екран був би там непридатний;
- «скасувати останній» шле verdict:null, тобто відкат є завжди;
- порожня колода — спокійний стан, а не помилка.

Запуск (потрібні playwright + chromium):
    python test_webapp_triage.py
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

ME_MANAGER = {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
              "dept_title": "Адміністративний", "manager": True}
ME_JOURNALIST = {"name": "Юлія Лукʼяненко", "first_name": "Юлія",
                 "dept": "newsroom", "dept_title": "Newsroom", "manager": False}

PEOPLE = [{"name": "Юлія Лукʼяненко", "dept": "newsroom", "dept_title": "Newsroom",
           "photo": None, "photo_sm": None, "photo_orig": None}]


def _boot(me):
    return {"me": me, "site_db": True, "nora": True, "people": PEOPLE,
            "projects": [], "assignees": [], "managers": [], "tasks": []}


def _card(i, title, kind, kind_word, why, quote):
    return {
        "id": i, "cls": "open", "state": "Строку не дали", "how": None,
        "cheap": False, "title": title, "kind": kind, "kind_word": kind_word,
        "why": why, "micro": False,
        "who": {"promiser": "Віталій Луков", "role": "заступник міського голови",
                "reported": None, "owner": None, "hidden": False},
        "quote": quote, "populism": None, "meta": ["1 ревізія"],
        "found": None, "fresh": False, "topic_id": None, "image": None,
        "link": {"url": "https://nikvesti.com/news/politics/321833-strategiya",
                 "title": "У Миколаєві планують розробити житлову стратегію",
                 "date": "31.07.2026"},
    }


DECK = {
    "items": [
        _card(101, "Винести питання землі на розгляд комісії", "process",
              "процедурний крок", "рух папера, а не результат у місті",
              "Питання знову мають розглянути на земельній комісії"),
        _card(102, "Прибирати територію парку за графіком", "routine",
              "планова рутина", "робилося б і без заяви",
              "Прибирання відбуватиметься за затвердженим графіком"),
        _card(103, "Ферма планує розширити виробництво", "offtopic",
              "не наш актор", "редакція не перевіряє цього обіцяльника",
              "Підприємство планує наростити потужності вдвічі"),
        _card(104, "Замінити одну лавку в сквері", "commitment",
              "дрібний предмет", "предмет дрібніший за об'єкт",
              "Пошкоджену лавку обіцяють замінити найближчим часом"),
    ],
    "offset": 4,
    "counts": {"pending": 12, "noise": 3, "good": 2,
               "by_kind": {"commitment": 40, "process": 9, "routine": 2}},
}

QUEUE = {
    "items": [], "total": 0, "offset": 0,
    "facets": [{"key": "all", "label": "Усе", "n": 0}],
    "bounds": {"from": 1780000000, "to": 1785000000, "articles": 900},
    "dupes": 0, "query": "", "matched": [],
    "shadow": {"pending": 12, "noise": 3, "good": 2},
}

STUB = """
window.__triageCalls = [];
window.__fail = false;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(k){ (window.__haptics ||= []).push(k); } } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/promises/triage")) return json(window.DECK);
  if (/\\/api\\/promises\\/\\d+\\/triage$/.test(url)) {
    window.__triageCalls.push({ url, body: JSON.parse(opts.body) });
    if (window.__fail) return new Response("мережа впала", { status: 500 });
    return json({ ok: true });
  }
  if (url.startsWith("/api/promises")) return json(window.QUEUE);
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


async def _open(pw, me=ME_MANAGER, deck=None):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    # Справжній telegram-web-app.js не вантажимо: він затирає заглушку
    # window.Telegram, і апка показала б екран помилки (урок 15.08 — усі
    # браузерні тести падали в CI саме на цьому).
    await page.route("https://telegram.org/**", lambda r: asyncio.ensure_future(
        r.fulfill(status=200, content_type="application/javascript", body="")))
    await page.route("**/static/*", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
    await page.route("https://app.local/", lambda r: asyncio.ensure_future(
        r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
    await page.add_init_script(
        "window.BOOT = " + json.dumps(_boot(me)) + ";"
        "window.DECK = " + json.dumps(deck if deck is not None else DECK) + ";"
        "window.QUEUE = " + json.dumps(QUEUE) + ";" + STUB)
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


async def _settle(page):
    """Дочекатись, поки верхня картка ДОПАДЕ.

    Не педантизм: під час анімації `tdrop` картка ще висить вище свого місця,
    і координати, зняті в цей момент, вказують на підзаголовок екрана — палець
    у тесті просто промахувався повз колоду.
    """
    await page.wait_for_function(
        "() => { const c = document.querySelector('.tr-card:last-child');"
        "  return c && c.getAnimations().every(a => a.playState !== 'running'); }",
        timeout=3000)


async def _drag(page, dx, release=True):
    """Перетягнути верхню картку на dx пікселів. Pointer Events, як пальцем."""
    await _settle(page)
    box = await page.evaluate(
        "() => document.querySelector('.tr-card:last-child')"
        ".getBoundingClientRect().toJSON()")
    x, y = box["x"] + box["width"] / 2, box["y"] + 40
    await page.mouse.move(x, y)
    await page.mouse.down()
    # Кількома кроками: один стрибок браузер віддає як єдиний pointermove, і
    # проміжний стан (поворот, печатка) перевірити було б нічим.
    for i in range(1, 5):
        await page.mouse.move(x + dx * i / 4, y)
        await page.wait_for_timeout(30)
    if release:
        await page.mouse.up()
        await page.wait_for_timeout(420)


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    async with async_playwright() as pw:
        # ---------- двері з банку тем ----------
        browser, page = await _open(pw)
        try:
            await page.evaluate("nav('promises')")
            await page.wait_for_selector("#pr-body", timeout=5000)
            door = page.locator('[data-nav="triage"]')
            check("менеджер бачить двері «Розбору» з числом",
                  await door.count() == 1
                  and "12" in await door.inner_text())
            check("двері пояснюють, що це і куди свайпати",
                  "свайп" in (await door.inner_text()).lower())
        finally:
            await browser.close()

        browser, page = await _open(pw, ME_JOURNALIST)
        try:
            await page.evaluate("nav('promises')")
            await page.wait_for_selector("#pr-body", timeout=5000)
            check("журналістці розбору не показуємо — у неї вже чиста черга",
                  await page.locator('[data-nav="triage"]').count() == 0)
        finally:
            await browser.close()

        # ---------- колода ----------
        browser, page = await _open(pw)
        try:
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-card", timeout=5000)

            # Малюється ОДНА картка плюс дві плашки глибини: у справжніх
            # карток різна висота, і сусідні визирали з-під верхньої разом зі
            # своїми заголовками — три тексти одночасно на екрані.
            check("колода має глибину, але текст показує лише верхня картка",
                  await page.locator(".tr-card").count() == 1
                  and await page.locator(".tr-slab").count() == 2)
            top = page.locator(".tr-card:last-child")
            check("зверху лежить перша картка колоди",
                  await top.get_attribute("data-tid") == "101")
            # Рядок типу CSS малює капсом, тому звіряємо в нижньому регістрі —
            # інакше тест ловив би оформлення, а не наявність підстави.
            text = (await top.inner_text()).lower()
            check("картка каже, ЧОМУ вона в тіні",
                  "процедурний крок" in text and "рух папера" in text, text[:60])
            check("і показує цитату — без неї свайп це вгадування",
                  "земельній комісії" in text)
            check("верхня картка падає згори (анімація)",
                  "tdrop" in await page.evaluate(
                      "getComputedStyle(document.querySelector"
                      "('.tr-card:last-child')).animationName"))
            check("лишок у тіні названо числом",
                  "12" in await page.inner_text(".tr-counts"))
            check("сказано, що нічого не видаляється",
                  "не видаляється" in await page.inner_text(".tr-note"))

            # --- перетягування: картка їде за пальцем ---
            await _drag(page, -60, release=False)
            style = await page.evaluate(
                "document.querySelector('.tr-card:last-child').style.transform")
            check("картка їде за пальцем із поворотом",
                  "translate" in style and "rotate" in style, style)
            check("печатка «Шум» проявляється за напрямком",
                  await page.evaluate(
                      "getComputedStyle(document.querySelector"
                      "('.tr-card:last-child .tr-stamp.noise')).opacity") != "0")
            # відпускаємо ДО порогу — рішення не ухвалюється
            await page.mouse.up()
            await page.wait_for_timeout(350)
            check("відпускання до порогу нічого не вирішує",
                  await page.evaluate("window.__triageCalls.length") == 0)
            check("…і картка повертається на місце",
                  await page.evaluate(
                      "document.querySelector('.tr-card:last-child').style.transform")
                  == "")

            # --- свайп ліворуч = шум ---
            await _drag(page, -160)
            calls = await page.evaluate("window.__triageCalls")
            check("свайп ліворуч шле noise на верхню картку",
                  len(calls) == 1 and calls[0]["url"].endswith("/101/triage")
                  and calls[0]["body"]["verdict"] == "noise", str(calls))
            top = page.locator(".tr-card:last-child")
            check("наступна картка стала верхньою одразу",
                  await top.get_attribute("data-tid") == "102")
            check("лічильник заходу рахує розібране",
                  "1" in await page.inner_text(".tr-counts"))

            # --- свайп праворуч = лишити ---
            await _drag(page, 160)
            calls = await page.evaluate("window.__triageCalls")
            check("свайп праворуч шле good",
                  len(calls) == 2 and calls[1]["url"].endswith("/102/triage")
                  and calls[1]["body"]["verdict"] == "good", str(calls))

            # --- кнопки дублюють свайп (десктоп) ---
            await page.click('[data-swipe="noise"]')
            await page.wait_for_timeout(420)
            calls = await page.evaluate("window.__triageCalls")
            check("кнопка «Шум» робить те саме, що свайп",
                  len(calls) == 3 and calls[2]["body"]["verdict"] == "noise")

            # --- відкат ---
            await page.click("#tr-undo")
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__triageCalls")
            check("«скасувати останній» шле порожній вердикт",
                  calls[-1]["body"]["verdict"] is None, str(calls[-1]))
            check("скасована картка повертається зверху колоди",
                  await page.locator(".tr-card:last-child")
                  .get_attribute("data-tid") == "103")

            # --- слід рішення: труха ліворуч, галочки праворуч ---
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-card", timeout=5000)
            await _settle(page)
            await page.click('[data-swipe="noise"]')
            await page.wait_for_timeout(60)
            fx = await page.evaluate("""() => {
              const f = document.querySelector('.tr-fx');
              return f ? { dust: f.querySelectorAll('.dust').length,
                           puff: f.querySelectorAll('.puff').length,
                           tick: f.querySelectorAll('.tick').length } : null; }""")
            check("свайп «шум» лишає хмару пилу з хлопком",
                  fx and fx["dust"] > 10 and fx["puff"] == 1, str(fx))
            check("…і жодної галочки — це інше рішення", fx and fx["tick"] == 0)
            # Шар живе НАД колодою, а не в ній: колода перемальовується через
            # 190 мс, і все, покладене всередину, зникло б посеред анімації.
            check("слід не всередині колоди — інакше його стерло б перемальовування",
                  await page.evaluate(
                      "() => !document.querySelector('#tr-deck .tr-fx')"))
            await page.wait_for_timeout(900)
            check("слід прибирає себе сам",
                  await page.evaluate("() => !document.querySelector('.tr-fx')"))

            await _settle(page)
            await page.click('[data-swipe="good"]')
            await page.wait_for_timeout(60)
            fx = await page.evaluate("""() => {
              const f = document.querySelector('.tr-fx');
              return f ? { tick: f.querySelectorAll('.tick svg').length,
                           glow: f.querySelectorAll('.glow').length,
                           dust: f.querySelectorAll('.dust').length } : null; }""")
            check("свайп «лишити» малює галочки, а не емодзі",
                  fx and fx["tick"] >= 4 and fx["glow"] == 1, str(fx))
            check("…і жодної пилинки", fx and fx["dust"] == 0)
            await page.wait_for_timeout(900)

            # --- «Відкрити» веде в картку, а не вирішує ---
            before = await page.evaluate("window.__triageCalls.length")
            await page.click('[data-swipe="open"]')
            await page.wait_for_timeout(400)
            check("«Відкрити» веде в картку обіцянки і нічого не вирішує",
                  await page.evaluate("STATE.view") == "promise"
                  and await page.evaluate("window.__triageCalls.length") == before)
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-card", timeout=5000)
            await _settle(page)     # знімок після падіння, а не в польоті
            await page.screenshot(path="/tmp/ph-shots/triage.png", full_page=True)
        finally:
            await browser.close()

        # ---------- збій мережі ----------
        browser, page = await _open(pw)
        try:
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-card", timeout=5000)
            await page.evaluate("window.__fail = true")
            await _drag(page, -160)
            await page.wait_for_timeout(400)
            check("збій мережі повертає картку в колоду, а не ковтає її",
                  await page.locator(".tr-card:last-child")
                  .get_attribute("data-tid") == "101")
            check("і про збій сказано вголос",
                  not await page.locator("#toast.hidden").count())
        finally:
            await browser.close()

        # ---------- розкладка: заголовок цілий, під колодою не порожньо ----------
        browser, page = await _open(pw, ME_MANAGER, {
            "items": [_card(301, "Встановити транспортні огородження та дорожні "
                            "знаки на чотирьох вулицях Миколаєва через велогонку",
                            "process", "процедурний крок",
                            "рух папера, а не результат у місті",
                            "Комунальному підприємству доручать встановити "
                            "огородження та знаки")] + DECK["items"],
            "offset": 5,
            "counts": {"pending": 489, "noise": 3, "good": 6, "by_kind": {}}})
        try:
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-card", timeout=5000)
            await _settle(page)
            box = await page.evaluate("""() => {
              const t = document.querySelector('.tr-title');
              const s = document.querySelector('.tr-screen');
              const n = document.getElementById('bottomnav');
              return { cut: t.scrollHeight - t.clientHeight,
                       gap: n.getBoundingClientRect().top
                            - s.getBoundingClientRect().bottom }; }""")
            # Заголовок різало НЕ обрізанням рядків, а флексом: у картки
            # фіксована висота, і текст стискався разом із нею (Олег, 18.08).
            check("довгий заголовок видно повністю, а не обрізаним флексом",
                  box["cut"] == 0, f"сховано {box['cut']}px")
            check("під колодою не лишається порожньої смуги",
                  0 <= box["gap"] <= 24, f"{round(box['gap'])}px до меню")
        finally:
            await browser.close()

        # ---------- порожня колода ----------
        browser, page = await _open(pw, ME_MANAGER, {
            "items": [], "offset": 0,
            "counts": {"pending": 0, "noise": 8, "good": 3, "by_kind": {}}})
        try:
            await page.evaluate("nav('triage')")
            await page.wait_for_selector(".tr-empty", timeout=5000)
            text = await page.inner_text(".tr-empty")
            check("порожня колода — спокійний стан, а не помилка",
                  "розібрана" in text.lower() and "з'явиться саме" in text)
            check("колоди немає — немає й кнопок рішення",
                  await page.locator(".tr-acts").count() == 0)
        finally:
            await browser.close()

    print()
    for name, ok, detail in results:
        print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    good = sum(1 for _, ok, _ in results if ok)
    print(f"\n{good}/{len(results)} перевірок пройдено")
    return 0 if good == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
