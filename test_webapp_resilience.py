"""
Тест живучості Mini App «Команда»: поведінка при збоях і підтвердження
деструктивних дій (webapp/app.js).

Навіщо (ревізія 27.07.2026):
- РЕТРАЙ. Будь-який збій на старті кидав у глухий екран «Не пустили» без
  виходу: у мобільному Telegram обрив зв'язку — найчастіший сценарій, а
  єдиним способом повторити було закрити й відкрити апку. Тепер мережева
  помилка й 5xx дають кнопку «Спробувати ще», а 401/403 — ні (від повторного
  тику відмова доступу не мине).
- ПІДТВЕРДЖЕННЯ. «Зняти таск», «Видалити тематику», «Видалити норму»,
  «Відкріпити папку» виконувались одразу, undo немає. Тепер — showConfirm
  Telegram (з фолбеком на window.confirm у старих клієнтах).

Запуск (потрібні playwright + chromium):
    python test_webapp_resilience.py
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

BOOTSTRAP = {
    "me": {"name": "Катерина Середа", "first_name": "Катя", "dept": "admin",
           "dept_title": "Адміністративний", "manager": True},
    "site_db": True,
    "nora": True,
    "people": [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
                "photo": None, "photo_orig": None}],
    "projects": [],
    "tasks": [
        {"id": 101, "person": "Аліна Квітко", "creator": "Катерина Середа",
         "project_id": None, "project_name": None, "partner_name": None,
         "platform": None, "type": "news", "theme_id": None, "theme_name": "Сесії",
         "qty": 2, "note": "", "deadline": None, "status": "open",
         "created_at": "2026-07-27T10:00:00+03:00", "done_at": None},
    ],
}

# window.__mode керує стабом: ok | offline | http500 | http403
STUB = """
window.__mode = "ok";
window.__patches = [];
window.__confirms = [];
window.__confirmAnswer = false;
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, openLink(){},
  showConfirm(msg, cb){ window.__confirms.push(msg); cb(window.__confirmAnswer); },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  if (method === "PATCH" || method === "DELETE") window.__patches.push(method + " " + url);
  if (window.__mode === "offline") throw new TypeError("Failed to fetch");
  if (window.__mode === "http500") return new Response("Внутрішня помилка", { status: 500 });
  if (window.__mode === "http403") return new Response("Ця апка — для команди МикВісті.", { status: 403 });
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: [], week_label: "", month_label: "", site_db: true });
  return json({ ok: true, task: {} });
};
"""


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _new_page(pw, mode="ok"):
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
        "window.BOOT = " + json.dumps(BOOTSTRAP) + ";" + STUB
        + f'\nwindow.__mode = "{mode}";')
    await page.goto("https://app.local/")
    return browser, page


async def _visible(page, sel):
    return await page.evaluate(
        f"!document.querySelector({sel!r}).classList.contains('hidden')")


async def test_offline_boot_then_recover(pw, results):
    """Обрив мережі на старті → кнопка «Спробувати ще» → зв'язок повернувся →
    апка піднялась."""
    browser, page = await _new_page(pw, mode="offline")
    try:
        await page.wait_for_selector("#screen-error:not(.hidden)", timeout=8000)
        title = await page.inner_text("#error-title")
        has_retry = await _visible(page, "#error-retry")
        results.append(("офлайн-старт показує «Немає зв'язку»", "зв'язку" in title))
        results.append(("є кнопка «Спробувати ще»", has_retry))

        await page.evaluate('window.__mode = "ok"')   # зв'язок повернувся
        await page.click("#error-retry")
        await page.wait_for_selector("#screen-main:not(.hidden)", timeout=8000)
        gone = not await _visible(page, "#screen-error")
        greeted = "Привіт" in await page.inner_text("#content")
        results.append(("після ретраю апка піднялась", greeted))
        results.append(("екран помилки прибрано", gone))
    finally:
        await browser.close()


async def test_http500_offers_retry(pw, results):
    browser, page = await _new_page(pw, mode="http500")
    try:
        await page.wait_for_selector("#screen-error:not(.hidden)", timeout=8000)
        results.append(("5xx теж дає ретрай", await _visible(page, "#error-retry")))
    finally:
        await browser.close()


async def test_403_has_no_retry(pw, results):
    """Відмова доступу — не той випадок, де тик щось змінить."""
    browser, page = await _new_page(pw, mode="http403")
    try:
        await page.wait_for_selector("#screen-error:not(.hidden)", timeout=8000)
        results.append(("403 БЕЗ кнопки ретраю", not await _visible(page, "#error-retry")))
        results.append(("403 пояснює причину",
                        "команди" in await page.inner_text("#error-text")))
    finally:
        await browser.close()


async def test_destructive_needs_confirm(pw, results):
    """«Зняти» питає підтвердження; «Скасувати» не шле нічого, «Так» — шле.
    «Виконано» (не деструктивне) не питає."""
    browser, page = await _new_page(pw, mode="ok")
    try:
        await page.wait_for_selector("#screen-main:not(.hidden)", timeout=8000)
        await page.click("[data-tracker]")
        await page.wait_for_selector("[data-task]")

        # 1. відмова у діалозі — запит не летить
        await page.evaluate("window.__confirmAnswer = false; window.__patches = []; window.__confirms = []")
        await page.click('[data-task="101"]')
        await page.wait_for_selector('[data-status="dropped"]')
        await page.click('[data-status="dropped"]')
        await page.wait_for_timeout(400)
        asked = await page.evaluate("window.__confirms.length")
        sent = await page.evaluate("window.__patches")
        results.append(("«Зняти» питає підтвердження", asked == 1))
        results.append(("після «Скасувати» запит НЕ полетів", sent == []))

        # 2. згода — запит летить
        await page.evaluate("window.__confirmAnswer = true")
        await page.click('[data-status="dropped"]')
        await page.wait_for_timeout(500)
        sent = await page.evaluate("window.__patches")
        results.append(("після згоди запит полетів", sent == ["PATCH /api/tasks/101"]))

        # 3. «Виконано» — не деструктивне, без діалогу
        await page.reload()
        await page.wait_for_selector("#screen-main:not(.hidden)")
        await page.click("[data-tracker]")
        await page.wait_for_selector("[data-task]")
        await page.evaluate("window.__patches = []; window.__confirms = []")
        await page.click('[data-task="101"]')
        await page.wait_for_selector('[data-status="done"]')
        await page.click('[data-status="done"]')
        await page.wait_for_timeout(500)
        results.append(("«Виконано» не питає зайвого",
                        await page.evaluate("window.__confirms.length") == 0))
        results.append(("«Виконано» спрацювало",
                        await page.evaluate("window.__patches") == ["PATCH /api/tasks/101"]))
    finally:
        await browser.close()


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    print("Живучість Mini App «Команда»:")
    results = []
    async with async_playwright() as pw:
        await test_offline_boot_then_recover(pw, results)
        await test_http500_offers_retry(pw, results)
        await test_403_has_no_retry(pw, results)
        await test_destructive_needs_confirm(pw, results)

    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    if all(ok for _, ok in results):
        print("Усе зелено.")
        return 0
    print("Є падіння — див. ✗ вище.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
