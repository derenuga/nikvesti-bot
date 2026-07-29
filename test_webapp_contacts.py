"""
Телефонна база редакції в Mini App «Команда» (webapp/app.js + handlers/team_contacts.py).

Олег, 29.07: «у кого є телефон віцемера Лукова?» — хтось кидає в чат, а через
пів року питають знову. Два входи, обидва РУЧНІ: форма в апці й пересланий
Лису контакт. Чат бот не сканує — свідоме рішення.

Що стереже:
- пошук іде і за іменем, і за ПОСАДОЮ, і за ТЕМОЮ, і за номером — одним полем
  (реальне питання частіше «хто в нас по енергетиці», ніж «дай Лукова»);
- картка редагується всією командою, видаляє лише менеджер;
- подяка на дверях («твоїх — N, дякуємо») є, але це САМЕ подяка: на KPI
  кількість контактів не впливає — інакше базу заб'ють сміттям заради лічильника;
- лічильник для дверей тягнеться окремим дешевим запитом (?only=mine), а не
  всією базою;
- пересланий контакт із тим самим номером не плодить другу картку;
- порожня база каже, ЩО зробити, а не просто «нічого немає»;
- у журналістки книга — дверима на головній, у менеджера — в утилітах у шапці
  (нижнє меню не чіпаємо: там пʼять пунктів робочого потоку);
- «Підтягнути з архіву» пропонує посаду з сутнісного шару, показує, кого саме
  архів має на увазі (однофамільці), підставляє в поле, але НЕ зберігає само;
  а коли не знайшов — чесно каже про межу покриття (2–3 роки).

Запуск:  python test_webapp_contacts.py
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

CONTACTS = [
    {"id": 1, "name": "Ігор Луков", "role": "віцемер Миколаєва",
     "phone": "+380501112233",
     "phones": ["+380501112233", "0512 555 000 приймальня"],
     "telegram": None, "email": None,
     "tags": "міськрада, бюджет", "note": None,
     "added_by": "Аліна Квітко", "updated_by": "Аліна Квітко",
     "updated_at": "2026-07-20T10:00:00+03:00"},
    {"id": 2, "name": "Олена Приходько", "role": "прессекретарка обленерго",
     "phone": "+380671234567", "phones": ["+380671234567"],
     "telegram": "@olena", "email": None,
     "tags": "енергетика, відключення", "note": "після 10:00",
     "added_by": "Юлія Бойченко", "updated_by": "Юлія Бойченко",
     "updated_at": "2026-07-21T10:00:00+03:00"},
]

MINE = {"added": 1, "fixed": 0, "total": 2}

PEOPLE = [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
           "photo": None, "photo_sm": None, "photo_orig": None}]

JOURNALIST = {
    "me": {"name": "Аліна Квітко", "first_name": "Аліна", "dept": "creative",
           "dept_title": "Creative", "manager": False},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": [],
    "assignees": [], "managers": [], "tasks": [],
}
MANAGER = {**JOURNALIST,
           "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
                  "dept_title": "Адміністративний", "manager": True}}

STUB = """
window.__calls = [];
window.__saved = null;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "dark",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url.startsWith("/api/contacts/lookup")) {
    window.__calls.push({ url, method: "GET" });
    const q = new URLSearchParams(url.split("?")[1] || "");
    const name = (q.get("name") || "").toLowerCase();
    return json({ candidates: name.includes("логвинов")
      ? [{ id: 9, kind: "person", name: "Микола Логвінов",
           role: "директор Миколаївобтеплоенерго", mentions: 47,
           last_year: 2026 }]
      : [] });
  }
  if (url.startsWith("/api/contacts")) {
    window.__calls.push({ url, method: opts.method || "GET" });
    if (opts.method === "POST" || opts.method === "PATCH") {
      window.__saved = { url, method: opts.method, body: JSON.parse(opts.body) };
      return json({ contact: {} });
    }
    if (opts.method === "DELETE") {
      window.__saved = { url, method: "DELETE" };
      return json({ ok: true });
    }
    const q = new URLSearchParams(url.split("?")[1] || "");
    if (q.get("only") === "mine") return json({ contacts: [], mine: window.MINE });
    const needle = (q.get("q") || "").toLowerCase();
    const list = window.CONTACTS.filter((c) => !needle ||
      ["name", "role", "tags", "phone", "note"].some((f) =>
        (c[f] || "").toLowerCase().includes(needle)) ||
      (c.phones || []).some((p) => p.toLowerCase().includes(needle)));
    return json({ contacts: list, mine: window.MINE, nora: true });
  }
  if (url === "/api/kpi") return json({ norms: [], week_label: "т",
                                        month_label: "серпень 2026" });
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  return json({ ok: true });
};
"""


async def closeSheetSafely(page):
    await page.evaluate("closeSheet()")
    await page.wait_for_timeout(200)


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
        "window.CONTACTS = " + json.dumps(CONTACTS) + ";"
        "window.MINE = " + json.dumps(MINE) + ";" + STUB)
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

    print("Телефонна база редакції:")
    async with async_playwright() as pw:
        browser, page = await _open(pw, JOURNALIST)
        try:
            # --- двері з подякою на головній ---
            await page.wait_for_selector('.door[data-nav="contacts"]', timeout=5000)
            await page.wait_for_timeout(400)
            door = await page.inner_text('.door[data-nav="contacts"]')
            check("двері бази є в журналістки", "Контакти" in door)
            check("на дверях подяка за внесок", "дякуємо" in door)
            check("і скільки саме її", "твоїх — 1" in door)

            calls = await page.evaluate("window.__calls")
            check("лічильник тягнеться дешево (only=mine), а не всією базою",
                  any("only=mine" in c["url"] for c in calls))

            # --- сам екран ---
            await page.click('.door[data-nav="contacts"]')
            await page.wait_for_selector(".cnt-row", timeout=5000)
            body = await page.inner_text("#content")
            check("видно контакти", "Ігор Луков" in body and "Олена Приходько" in body)
            check("під імʼям — посада й теми", "віцемер Миколаєва" in body)
            check("є кнопка дзвінка",
                  await page.locator(".cnt-tel").count() == 2)

            # --- пошук за ТЕМОЮ, а не лише за імʼям ---
            await page.fill("#cnt-q", "енергетика")
            await page.wait_for_timeout(600)
            body = await page.inner_text("#content")
            check("пошук за темою знаходить потрібного",
                  "Олена Приходько" in body and "Луков" not in body)

            await page.fill("#cnt-q", "віцемер")
            await page.wait_for_timeout(600)
            check("пошук за посадою теж працює",
                  "Луков" in await page.inner_text("#content"))

            await page.fill("#cnt-q", "0501112233")
            await page.wait_for_timeout(600)
            check("і за номером",
                  "Луков" in await page.inner_text("#content"))

            await page.fill("#cnt-q", "щось чого немає")
            await page.wait_for_timeout(600)
            check("порожній результат не лякає",
                  "нікого" in await page.inner_text("#content"))

            # --- кілька номерів ---
            await page.fill("#cnt-q", "")
            await page.wait_for_timeout(600)
            check("у списку видно, що номерів більше одного",
                  await page.locator(".cnt-more").count() == 1)
            check("і скільки саме ще", "+1" in await page.inner_text(".cnt-more"))

            await page.fill("#cnt-q", "0512")
            await page.wait_for_timeout(600)
            check("пошук знаходить і за ДРУГИМ номером",
                  "Луков" in await page.inner_text("#content"))
            await page.fill("#cnt-q", "")
            await page.wait_for_timeout(600)

            await page.click(".cnt-row")
            await page.wait_for_selector("#c-phones", timeout=3000)
            check("у картці всі номери окремими полями",
                  await page.locator("#c-phones .c-phone").count() == 2)
            await page.click("#c-addphone")
            check("«ще номер» додає порожнє поле",
                  await page.locator("#c-phones .c-phone").count() == 3)
            await page.locator("#c-phones .c-phone").nth(2).fill("+380931234567")
            await page.click("#c-save")
            await page.wait_for_timeout(400)
            saved = await page.evaluate("window.__saved")
            check("на сервер їде масив номерів",
                  saved and isinstance(saved["body"].get("phones"), list)
                  and len(saved["body"]["phones"]) == 3)
            check("порожні поля у масив не потрапляють",
                  saved and all(p.strip() for p in saved["body"]["phones"]))

            # --- редагування картки ---
            await page.fill("#cnt-q", "")
            await page.wait_for_timeout(600)
            await page.click(".cnt-row")
            await page.wait_for_selector("#c-name", timeout=3000)
            check("у картці підтягнулись поля",
                  await page.input_value("#c-role") == "віцемер Миколаєва")
            check("журналістці кнопки видалення немає",
                  await page.locator("#c-del").count() == 0)
            await page.fill("#c-tags", "міськрада, бюджет, тендери")
            await page.click("#c-save")
            await page.wait_for_timeout(400)
            saved = await page.evaluate("window.__saved")
            check("правка їде PATCH-ом на потрібну картку",
                  saved and saved["method"] == "PATCH"
                  and saved["url"].endswith("/api/contacts/1"))
            check("і несе оновлені теми",
                  saved and "тендери" in saved["body"]["tags"])
            # --- «чик-чик»: підтягнути посаду з архіву ---
            await page.evaluate("STATE.contactQuery = ''; renderContacts()")
            await page.wait_for_selector(".cnt-row", timeout=5000)
            await page.click("[data-cnt-add], #cnt-add")
            await page.wait_for_selector("#c-name", timeout=3000)
            await page.fill("#c-name", "Николай Логвинов")
            await page.click("#c-lookup")
            await page.wait_for_selector(".cnt-cand", timeout=3000)
            cand = await page.inner_text(".cnt-cand")
            check("архів пропонує посаду",
                  "директор Миколаївобтеплоенерго" in cand)
            check("видно, кого саме архів має на увазі",
                  "Логвінов" in cand and "47" in cand)
            await page.click(".cnt-cand")
            check("тап підставляє посаду в поле",
                  await page.input_value("#c-role") == "директор Миколаївобтеплоенерго")
            check("але не зберігає мовчки — просить перевірити",
                  "перевір" in (await page.inner_text("#c-found")).lower())

            # нікого не знайшли — кажемо чесно, з межею покриття
            await page.fill("#c-name", "Хтось Невідомий")
            await page.click("#c-lookup")
            await page.wait_for_timeout(400)
            nf = await page.inner_text("#c-found")
            check("порожній результат не бреше", "не знайшов" in nf)
            check("і пояснює межу покриття архіву", "2–3 роки" in nf)
            await closeSheetSafely(page)
        finally:
            await browser.close()

        # --- менеджер: книга живе в утилітах у шапці Головної ---
        browser, page = await _open(pw, MANAGER)
        try:
            await page.wait_for_selector("#home-tools", timeout=5000)
            check("у менеджера в шапці є кнопка утиліт",
                  await page.locator("#home-tools").count() == 1)
            await page.click("#home-tools")
            await page.wait_for_selector('[data-tool="contacts"]', timeout=3000)
            check("серед утиліт — телефонна книга",
                  "Контакти редакції" in await page.inner_text("#sheet"))
            await page.click('[data-tool="contacts"]')
            await page.wait_for_selector(".cnt-row", timeout=5000)
            check("з утиліт відкривається сама база",
                  await page.evaluate("() => STATE.view") == "contacts")

            await page.evaluate("nav('contacts')")
            await page.wait_for_selector(".cnt-row", timeout=5000)
            await page.click(".cnt-row")
            await page.wait_for_selector("#c-name", timeout=3000)
            check("менеджеру кнопка видалення доступна",
                  await page.locator("#c-del").count() == 1)
            await page.click("#c-del")
            await page.wait_for_timeout(400)
            saved = await page.evaluate("window.__saved")
            check("видалення йде DELETE-ом", saved and saved["method"] == "DELETE")
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
