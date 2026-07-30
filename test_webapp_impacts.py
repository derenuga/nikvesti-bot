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
    "site_db": True, "nora": True, "people": [], "projects": [],
    "assignees": [], "managers": [], "tasks": [],
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
    "what_happened": "Матеріали висвітлили проблеми відновлення трьох будинків, "
                     "акцентуючи на конфлікті між позицією влади та мешканців. "
                     "Після публікацій влада переглянула плани.",
    "significance": "Публікації сприяли ухваленню рішень на користь мешканців — "
                    "приклад впливу професійної журналістики на місцеву владу.",
    "articles": [
        {"id": 41, "article_id": 300001, "url": "https://nikvesti.com/news/public/300001",
         "title": "Замість демонтажу — реконструкція: влада переглянула плани",
         "date": "28.07.2026", "role": "fixer", "authors": "Світлана Іванченко",
         "project_name": None, "partner_name": None},
        {"id": 42, "article_id": 279936, "url": "https://nikvesti.com/articles/279936",
         "title": "Знести не можна відновити: три будинки між владою і мешканцями",
         "date": "03.02.2026", "role": "key", "authors": "Аліна Квітко",
         "project_name": "Голоси Миколаєва", "partner_name": "IMS"},
        {"id": 43, "article_id": 294960, "url": "https://nikvesti.com/news/public/294960",
         "title": "Будівельники почали зносити будинок на Погранічній",
         "date": "11.04.2026", "role": "series", "authors": "Аліса Мелікадамян",
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
  openLink(){}, showConfirm(m, c){ c(true); },
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

MINE = [{"id": 7, "title": READY["title"],
         "note": "вела серію, авторка ключового тексту", "articles": 3,
         "date": "28.07.2026",
         "created_at": "2026-07-29T18:00:00+03:00"}]

LIST = [
    {"id": 7, "title": READY["title"], "essence": READY["essence"],
     "status": "ready", "error": None, "articles": 3, "partners": "IMS",
     "date": "28.07.2026",
     "source_url": READY["source_url"]},
    {"id": 8, "title": None, "essence": "тест", "status": "failed",
     "error": "Матеріалу 123 немає в норі", "articles": 0, "partners": None,
     "source_url": "x"},
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
            check("у списку видно готовий кейс із донором",
                  "багатоповерхівок" in lst and "IMS" in lst)
            # Дата імпакту = дата новини-фіксації, не дата заведення в архів:
            # старі кейси заливаються заднім числом і мають стати в історію
            check("кейс підписано датою фіксації", "28.07.2026" in lst)
            check("і збитий кейс чесно підписано",
                  "не зібрався" in lst)

            # --- нова чернетка: лише URL і суть ---
            await page.click("#im-add")
            await page.wait_for_selector("#im-url", timeout=3000)
            sheet = await page.inner_text("#sheet")
            check("форма просить лише лінк і суть — полів-анкет немає",
                  "фіксац" in sheet and await page.locator("#sheet input").count() == 2)
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
            check("новина-фіксація підписана і без кнопок",
                  "новина-фіксація" in det
                  and await page.locator("[data-imdrop]").count() == 2)
            check("ключовий позначено", "ключовий" in det)
            check("медальки: видно кому і за що",
                  "вела серію" in det and "зафіксувала результат" in det)

            # --- перепризначити ключовий ---
            await page.click('[data-imkey="43"]')
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            patch = [c for c in calls if c["method"] == "PATCH"]
            check("зірочка перепризначає ключовий — останнє слово за людиною",
                  patch and patch[-1]["body"]["action"] == "set_key"
                  and patch[-1]["body"]["row_id"] == 43)

            # --- прибрати зайвий матеріал ---
            await page.click('[data-imdrop="43"]')
            await page.wait_for_timeout(300)
            calls = await page.evaluate("window.__calls")
            check("хрестик прибирає матеріал із серії",
                  any(c["method"] == "PATCH" and c["body"]
                      and c["body"].get("action") == "remove_article"
                      for c in calls))

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

        # ---------- журналістка: двері, список, read-only ----------
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
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
                  await page.locator("[data-imkey], [data-imdrop], #imd-edit, #imd-del").count() == 0)
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
