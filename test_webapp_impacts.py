"""
Імпакт-архів (webapp/app.js + handlers/impact_archive.py).

Олег, 29.07: «мені постійно важко шукати і наново описувати імпакти при кожній
заявці по грант». Кинув лінк новини-фіксації — бот сам збирає серію (беклінки
+ нора), донорський заголовок, наратив, ключовий текст і медальки.

Що стереже:
- «+» просить лише URL і суть своїми словами — більше нічого;
- поки кейс збирається, видно стан «збирається…», і апка полить сама;
- збій показує причину і дає «спробувати ще» (це ж рятує від редеплою
  посеред білда);
- у готовому кейсі: заголовок, «що сталось», «Значення та вплив», серія з
  датами й авторами, новина-фіксація без кнопок (її не викинути);
- ключовий текст позначено, і його можна ПЕРЕПРИЗНАЧИТИ — вага в серії різна,
  останнє слово за людиною;
- донор ключового тексту підсвічений окремо — «цим кейсом можна порадувати
  донора»;
- медальки: видно, кому і за що; можна зняти і додати людину;
- зайвий матеріал прибирається з серії;
- «Надіслати файлом у приват» б'є в /send;
- вхід — з утиліт менеджера.

Запуск:  python test_webapp_impacts.py
"""

import asyncio
import datetime
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

BOOT = {
    "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True, "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative",
                "dept_title": "Creative", "photo": None, "photo_sm": None,
                "photo_orig": None},
               {"name": "Світлана Іванченко", "dept": "newsroom",
                "dept_title": "Newsroom", "photo": None, "photo_sm": None,
                "photo_orig": None}],
    "projects": [], "assignees": [], "managers": [], "tasks": [],
}

BOOT_J = {
    "me": {"name": "Аліна Квітко", "first_name": "Аліна", "dept": "creative",
           "dept_title": "Creative", "manager": False},
    "site_db": True, "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative",
                "dept_title": "Creative", "photo": None, "photo_sm": None,
                "photo_orig": None}],
    "projects": [], "assignees": [], "managers": [], "tasks": [],
}

READY = {
    "id": 7, "status": "ready", "error": None,
    "title": "Зміна підходу до відновлення зруйнованих багатоповерхівок у Миколаєві",
    "essence": "після нас реконструкція замість демонтажу",
    "source_url": "https://nikvesti.com/news/public/300001",
    "created_by": "Олег Деренюга", "created_at": "2026-07-29T18:00:00+03:00",
    "image": "https://nikvesti.com/img/impact-300001.webp",
    "what_happened": "Матеріали висвітлили проблеми відновлення трьох будинків, "
                     "акцентуючи на конфлікті між позицією влади та мешканців. "
                     "Після публікацій влада переглянула плани.",
    "significance": "Публікації сприяли ухваленню рішень на користь мешканців — "
                    "приклад впливу професійної журналістики на місцеву владу.",
    "articles": [
        {"id": 41, "article_id": 300001, "url": "https://nikvesti.com/news/public/300001",
         "title": "Замість демонтажу — реконструкція: влада переглянула плани",
         "date": "28.07.2026", "role": "fixer", "is_key": False,
         "authors": "Світлана Іванченко",
         "project_name": None, "partner_name": None},
        {"id": 42, "article_id": 279936, "url": "https://nikvesti.com/articles/279936",
         "title": "Знести не можна відновити: три будинки між владою і мешканцями",
         "date": "03.02.2026", "role": "series", "is_key": True,
         "authors": "Аліна Квітко",
         "project_name": "Голоси Миколаєва", "partner_name": "IMS"},
        {"id": 43, "article_id": 294960, "url": "https://nikvesti.com/news/public/294960",
         "title": "Будівельники почали зносити будинок на Погранічній",
         "date": "11.04.2026", "role": "series", "is_key": False,
         "authors": "Аліса Мелікадамян",
         "project_name": None, "partner_name": None},
    ],
    "credits": [
        {"id": 1, "person": "Аліна Квітко", "note": "вела серію, авторка ключового тексту"},
        {"id": 2, "person": "Світлана Іванченко", "note": "зафіксувала результат"},
    ],
}

STUB = """
window.__calls = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ (window.__confirms = window.__confirms || []).push(m); c(true); },
  BackButton: { show(){}, hide(){}, onClick(f){ window.__back = f; } },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  const method = opts.method || "GET";
  window.__calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : null });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/impacts/mine")) return json({ impacts: window.MINE });
  if (url === "/api/impacts" && method === "GET") return json({ impacts: window.LIST });
  if (url === "/api/impacts" && method === "POST") {
    window.LIST = [{ id: 9, title: null, essence: JSON.parse(opts.body).essence,
      status: "building", error: null, articles: 0, partners: null,
      source_url: JSON.parse(opts.body).url }].concat(window.LIST);
    return json({ id: 9, status: "building" });
  }
  if (url.startsWith("/api/impacts/7/send")) return json({ ok: true });
  if (url.startsWith("/api/impacts/7/retry")) return json({ id: 7, status: "building" });
  if (url.startsWith("/api/impacts/8/retry")) return json({ id: 8, status: "building" });
  if (url === "/api/impacts/7" && method === "PATCH") return json(window.READY);
  if (url === "/api/impacts/7") return json(window.READY);
  if (url === "/api/impacts/8") return json({ id: 8, status: "failed",
    error: "Матеріалу 123 немає в норі", essence: "тест", title: null,
    articles: [], credits: [], what_happened: "", significance: "",
    source_url: "x", created_by: "", created_at: null });
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  if (url.startsWith("/api/kpi")) return json({ norms: [], week_label: "т",
    month_label: "липень 2026", site_db: true });
  return json({ ok: true });
};
"""

# Дата — поточний місяць: банер на головній показує імпакт САМЕ поточного
# періоду, і тест не має протухати першого числа
TODAY = datetime.datetime.now().strftime("%d.%m.%Y")

MINE = [{"id": 7, "title": READY["title"],
         "note": "вела серію, авторка ключового тексту", "articles": 3,
         "date": TODAY,
         "image": "https://nikvesti.com/img/impact-300001.webp",
         "created_at": "2026-07-29T18:00:00+03:00"}]

LIST = [
    {"id": 7, "title": READY["title"], "essence": READY["essence"],
     # partners — СПИСОК, як його віддає сервер (ARRAY_AGG у impact_archive):
     # серія збирається роками, і за цей час проєкт міг змінитись, тож донорів
     # у кейсі буває кілька. Тут лежав рядок "IMS" з часів одного донора, і
     # картка мовчки лишалась без кружечка — Array.isArray(рядок) це false
     "status": "ready", "error": None, "articles": 3, "partners": ["IMS"],
     "date": "28.07.2026",
     "image": "https://nikvesti.com/img/impact-300001.webp",
     "people": ["Аліна Квітко", "Світлана Іванченко"],
     "source_url": READY["source_url"]},
    {"id": 6, "title": "Ремонт дороги до Матвіївки після серії публікацій",
     "essence": None, "status": "ready", "error": None, "articles": 4,
     "partners": ["IMS"], "date": "16.07.2024", "image": None,
     "people": ["Альона Коханчук"], "source_url": "y"},
    {"id": 8, "title": None, "essence": "тест", "status": "failed",
     "error": "Матеріалу 123 немає в норі", "articles": 0, "partners": None,
     "image": None, "people": [], "source_url": "x"},
]


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


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
            "window.LIST = " + json.dumps(LIST) + ";"
            "window.MINE = [];"
            "window.READY = " + json.dumps(READY) + ";" + STUB)
        await page.goto("https://app.local/")
        await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
        try:
            # --- вхід з утиліт ---
            await page.click("#home-tools")
            await page.wait_for_selector("[data-tool]", timeout=3000)
            check("імпакт-архів лежить в утилітах менеджера",
                  "Імпакт-архів" in await page.inner_text("#sheet"))
            await page.click('[data-tool="impacts"]')
            await page.wait_for_selector("[data-impact]", timeout=5000)
            lst = await page.inner_text("#im-body")
            check("у списку видно готовий кейс", "багатоповерхівок" in lst)
            check("донор на картці — кружечком, підписаним іменем",
                  await page.locator("[data-impact='7'] .imp-donor[title='IMS']").count() == 1)
            # Дата імпакту = дата новини-фіксації, не дата заведення в архів:
            # старі кейси заливаються заднім числом і мають стати в історію
            check("кейс підписано датою фіксації", "28.07.2026" in lst)
            # Олег, 29.07: «давай фотку основної новини, кружечки авторів,
            # кружечок донора» — картка замість рядка тексту
            card = page.locator("[data-impact='7']")
            check("на картці — фото новини-фіксації",
                  await card.locator(".imp-img img").count() == 1)
            check("кружечки дотичних авторів",
                  await card.locator(".imp-av").count() == 2)
            check("і кружечок донора",
                  await card.locator(".imp-donor").count() == 1)
            check("заголовок імпакту — окремим рядком, не в одну кашу з метою",
                  await card.locator(".imp-title").count() == 1
                  and await card.locator(".imp-meta").count() == 1)

            # --- фільтр по роках (Олег, 30.07: «поставил 2024 — видишь
            # импакты за 2024») ---
            check("над списком — річні чипи, включно з «Всі»",
                  await page.locator(".im-years .chip").count() == 3)
            await page.click('[data-imyear="2024"]')
            await page.wait_for_timeout(200)
            check("2024: видно кейс 2024-го",
                  await page.locator("[data-impact='6']").count() == 1)
            check("а кейс 2026-го схований",
                  await page.locator("[data-impact='7']").count() == 0)
            check("збитий кейс без дати видно завжди — він чекає дії",
                  await page.locator("[data-impact='8']").count() == 1)
            await page.click('[data-imyear=""]')
            await page.wait_for_timeout(200)
            check("«Всі» повертає повний список",
                  await page.locator("[data-impact]").count() == 3)
            check("і збитий кейс чесно підписано",
                  "не зібрався" in lst)

            # --- нова чернетка: лише URL і суть ---
            await page.click("#im-add")
            await page.wait_for_selector("#im-url", timeout=3000)
            sheet = await page.inner_text("#sheet")
            # Полів рівно три і всі три — лінки й суть: сама фіксація, наш
            # матеріал підказкою (з 14.08, коли фіксацією стала чужа
            # публікація — на сторінці ІМІ лінка на нас може не бути взагалі)
            # і суть своїми словами. Анкети як не було, так і немає.
            check("форма просить лише лінки й суть — полів-анкет немає",
                  "фіксац" in sheet and await page.locator("#sheet input").count() == 3)
            check("є поле «наш матеріал» — підказка для збору",
                  await page.locator("#sheet .im-our").count() == 1)
            await page.fill("#im-url", "https://nikvesti.com/news/public/321200-remont")
            await page.fill("#im-essence", "після нас відремонтували")
            await page.click("#im-save")
            await page.wait_for_timeout(400)
            calls = await page.evaluate("window.__calls")
            post = [c for c in calls if c["method"] == "POST" and c["url"] == "/api/impacts"]
            check("створення шле URL і суть", post
                  and "321200" in post[0]["body"]["url"]
                  and post[0]["body"]["essence"] == "після нас відремонтували")
            await page.wait_for_selector("[data-impact='9']", timeout=3000)
            check("нова чернетка одразу в списку зі станом «збирається…»",
                  "збирається" in await page.inner_text("[data-impact='9']"))

            # --- готовий кейс ---
            await page.click("[data-impact='7']")
            await page.wait_for_selector("#imd-body .im-p", timeout=5000)
            det = await page.inner_text("#imd-body")
            check("є наратив «що сталось»", "переглянула плани" in det)
            check("і блок «Значення та вплив»", "Значення та вплив" in det)
            check("донор ключового тексту підсвічений — його можна порадувати",
                  "IMS" in det and "порадувати донора" in det)
            check("серія з датами й авторами",
                  "03.02.2026" in det and "Аліна Квітко" in det)
            check("ключовий позначено", "ключовий" in det)
            check("медальки: видно кому і за що",
                  "вела серію" in det and "зафіксувала результат" in det)
            check("підпис каже, що робить тап — без іконок-загадок",
                  "тап по матеріалу" in det)

            # --- дії над матеріалом: шторка зі словами (Олег, 29.07:
            # «нажал крестик — материал удалился без вопроса; как поставить
            # звездочку — непонятно») ---
            await page.click('[data-imart="43"]')
            await page.wait_for_selector("#ia-key", timeout=3000)
            sheet = await page.inner_text("#sheet")
            check("дії підписані словами",
                  "Зробити ключовим" in sheet and "Прибрати з серії" in sheet
                  and "Відкрити матеріал" in sheet)
            await page.click("#ia-key")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            patch = [c for c in calls if c["method"] == "PATCH"]
            check("«Зробити ключовим» перепризначає — останнє слово за людиною",
                  patch and patch[-1]["body"]["action"] == "set_key"
                  and patch[-1]["body"]["row_id"] == 43)

            # фіксацію не викинути, але КЛЮЧОВОЮ вона бути може: це той
            # лінк, що кинули в «+», і він же буває головним текстом серії
            # (Олег, 30.07: «в чем прикол?» — прикол був у моделі, виправлено)
            await page.click('[data-imart="41"]')
            await page.wait_for_selector("#ia-open", timeout=3000)
            check("фіксацію не можна прибрати з серії",
                  await page.locator("#ia-drop").count() == 0)
            check("але зробити ключовою — можна",
                  await page.locator("#ia-key").count() == 1)
            await page.click("#ia-cancel")

            # --- додати матеріал руками (кейс 30.07: стаття про автошколу
            # без беклінка і поза норою — і з нею донор Sigrid Rausing) ---
            check("під серією є «Додати матеріал за лінком»",
                  await page.locator("#imd-add-art").count() == 1)
            await page.click("#imd-add-art")
            await page.wait_for_selector("#ia-url", timeout=3000)
            await page.fill("#ia-url", "https://nikvesti.com/articles/313776-avtoshkola")
            await page.click("#ia-add-save")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("лінк їде PATCH-ем add_article",
                  any(c["method"] == "PATCH" and c["body"]
                      and c["body"].get("action") == "add_article"
                      and "313776" in (c["body"].get("url") or "")
                      for c in calls))

            # --- прибрати зайвий матеріал: тепер із підтвердженням ---
            await page.click('[data-imart="43"]')
            await page.wait_for_selector("#ia-drop", timeout=3000)
            await page.click("#ia-drop")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("«Прибрати з серії» справді прибирає",
                  any(c["method"] == "PATCH" and c["body"]
                      and c["body"].get("action") == "remove_article"
                      for c in calls))
            confirms = await page.evaluate("window.__confirms || []")
            check("але спершу питає, чи впевнений",
                  any("Прибрати" in m for m in confirms))

            # --- медальки ---
            await page.fill("#imd-credit-name", "Юлія Бойченко")
            await page.press("#imd-credit-name", "Enter")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("людину можна дописати в медальки",
                  any(c["method"] == "PATCH" and c["body"]
                      and c["body"].get("action") == "add_credit"
                      and c["body"].get("person") == "Юлія Бойченко"
                      for c in calls))

            # --- виправити слово в наративі: тап по самому абзацу ---
            # Олег, 29.07: «нехай можна буде редагувати зміст — AI може
            # згалюцинувати, і треба буде якесь слово виправити». Олівець був,
            # але для «одного слова» він захований: тап по тексту чесніший.
            await page.click('[data-imedit="ime-what"]')
            await page.wait_for_selector("#ime-what", timeout=3000)
            check("тап по абзацу відкриває правку і фокус у ньому",
                  await page.evaluate("document.activeElement.id") == "ime-what")
            check("текст уже в полі — правиться слово, а не пишеться заново",
                  "переглянула плани" in await page.input_value("#ime-what"))
            await page.fill("#ime-what", "Після публікацій влада ЗМІНИЛА плани.")
            await page.click("#ime-save")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("виправлення їде PATCH-ем разом з рештою полів",
                  any(c["method"] == "PATCH" and c["body"]
                      and "ЗМІНИЛА" in (c["body"].get("what_happened") or "")
                      for c in calls))

            # --- перезбір готового кейсу ---
            # Олег, 29.07: «удалил импакт, нигде не было кнопки "спробувати
            # ще"» — вона жила лише на збитих кейсах, і шукаючи її на
            # готовому, він видалив кейс. Тепер перезбір є і тут, з прямим
            # попередженням, що ручні правки перезапишуться.
            check("на готовому кейсі є «Перезібрати заново»",
                  await page.locator("#imd-rebuild").count() == 1)
            await page.click("#imd-rebuild")
            await page.wait_for_timeout(300)
            confirms = await page.evaluate("window.__confirms || []")
            check("перед перезбором чесно попереджає про втрату правок",
                  any("перезаписано" in m for m in confirms))
            calls = await page.evaluate("window.__calls")
            check("і перезапускає збір тим самим retry",
                  any("/api/impacts/7/retry" in c["url"] for c in calls))
            # стаб на retry/7 віддає building — повертаємось на готовий кейс
            await page.evaluate("STATE.currentImpact = 7; nav('impact')")
            await page.wait_for_selector("#imd-send", timeout=5000)

            # --- відправка файлом ---
            await page.click("#imd-send")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("кнопка шле кейс файлом у приват",
                  any("/api/impacts/7/send" in c["url"] for c in calls))
            await page.screenshot(path="/tmp/ph-shots/impact.png", full_page=True)

            # --- збитий кейс ---
            await page.evaluate("STATE.currentImpact = 8; nav('impact')")
            await page.wait_for_selector("#im-retry", timeout=5000)
            fail = await page.inner_text("#imd-body")
            check("збій показує причину", "немає в норі" in fail)
            await page.click("#im-retry")
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("«спробувати ще» перезапускає збір",
                  any("/api/impacts/8/retry" in c["url"] for c in calls))
        finally:
            await browser.close()

        # ---------- перегляд чужими очима: імпакти видно і там ----------
        # Олег, 30.07: «где на этом экране импакты журналиста?» — прев'ю їх
        # не вантажило взагалі, хоча правило «я хочу бачити все, що у неї»
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        # telegram.org глушимо і тут: у цьому файлі три різні входи в апку
        # (менеджер, журналістка, окремий сценарій), і заглушка потрібна кожному
        await page.route("https://telegram.org/**", lambda r: asyncio.ensure_future(
            r.fulfill(status=200, content_type="application/javascript", body="")))
        await page.route("**/static/*", lambda r: asyncio.ensure_future(
            r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
        await page.route("https://app.local/", lambda r: asyncio.ensure_future(
            r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
        await page.add_init_script(
            "window.BOOT = " + json.dumps(BOOT) + ";"
            "window.LIST = [];"
            "window.MINE = " + json.dumps(MINE) + ";"
            "window.READY = " + json.dumps(READY) + ";" + STUB)
        await page.goto("https://app.local/")
        await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
        try:
            await page.evaluate("nav('preview', 'Аліна Квітко')")
            await page.wait_for_selector(".imp-banner", timeout=5000)
            check("банер імпакту видно і в перегляді чужими очима",
                  "багатоповерхівок" in await page.inner_text(".imp-banner"))
            calls = await page.evaluate("window.__calls")
            check("імпакти тягнуться для ОБРАНОЇ людини",
                  any("/api/impacts/mine" in c["url"] and "person=" in c["url"]
                      for c in calls))
            check("і двері «Мої імпакти» на місці",
                  await page.locator('.door[data-nav="myimpacts"]').count() == 1)
        finally:
            await browser.close()

        # ---------- журналістка: двері, список, read-only ----------
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        # telegram.org глушимо і тут: у цьому файлі три різні входи в апку
        # (менеджер, журналістка, окремий сценарій), і заглушка потрібна кожному
        await page.route("https://telegram.org/**", lambda r: asyncio.ensure_future(
            r.fulfill(status=200, content_type="application/javascript", body="")))
        await page.route("**/static/*", lambda r: asyncio.ensure_future(
            r.fulfill(path=str(WEBAPP / r.request.url.split("/")[-1].split("?")[0]))))
        await page.route("https://app.local/", lambda r: asyncio.ensure_future(
            r.fulfill(path=str(WEBAPP / "index.html"), content_type="text/html")))
        await page.add_init_script(
            "window.BOOT = " + json.dumps(BOOT_J) + ";"
            "window.LIST = [];"
            "window.MINE = " + json.dumps(MINE) + ";"
            "window.READY = " + json.dumps(READY) + ";" + STUB)
        await page.goto("https://app.local/")
        await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
        try:
            # --- банер імпакту поточного місяця (Олег, 30.07: «нехай і у
            # верхній панелі буде, якщо в поточному періоді відбувся») ---
            await page.wait_for_selector(".imp-banner", timeout=5000)
            ban = await page.inner_text(".imp-banner")
            check("на головній — банер «Імпакт за твоєї участі»",
                  "імпакт за твоєї участі" in ban.lower())   # CSS малює капсом
            check("з назвою кейсу", "багатоповерхівок" in ban)
            await page.click(".imp-banner")
            await page.wait_for_selector("#imd-body .im-p", timeout=5000)
            check("тап по банеру відкриває кейс на читання",
                  await page.locator("[data-imart], #imd-edit").count() == 0)
            await page.click("[data-back]")
            await page.wait_for_timeout(300)
            check("«Назад» з кейсу банера повертає на головну",
                  await page.evaluate("STATE.view") == "home")

            await page.wait_for_selector('.door[data-nav="myimpacts"]', timeout=5000)
            door = await page.inner_text('.door[data-nav="myimpacts"]')
            check("двері «Мої імпакти» зʼявились — і з числом", "1" in door)
            await page.click('.door[data-nav="myimpacts"]')
            await page.wait_for_selector("#mi-body [data-impact]", timeout=5000)
            mine = await page.inner_text("#mi-body")
            check("у списку видно кейс і нотатку медальки",
                  "багатоповерхівок" in mine and "вела серію" in mine)
            await page.click("[data-impact]")
            await page.wait_for_selector("#imd-body .im-p", timeout=5000)
            det = await page.inner_text("#imd-body")
            check("кейс відкривається на читання — наратив і серія на місці",
                  "Значення та вплив" in det and "★ ключовий" in det)
            check("кнопок правки немає — це читання",
                  await page.locator("[data-imart], #imd-edit, #imd-del").count() == 0)
            check("і абзаци не редагуються",
                  await page.locator("[data-imedit]").count() == 0)
            check("медальки підписані як команда кейсу",
                  "команда кейсу" in det.lower())
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
