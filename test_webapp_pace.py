"""
Тест прогресу періоду на екрані журналістки (webapp/app.js).

Вихідна проблема (Олег, 28.07): «скоро початок місяця — людина відкриє, побачить
0% і засмутиться». Екран тепер говорить про ТЕМП, а не про відсоток від кінця
періоду: фраза за матрицею «фаза × темп», засічка «де ти маєш бути сьогодні»,
кроки тижнів, і салют рівно на перетин норми.

Що стереже:
- ПЕРШОГО ЧИСЛА фраза не лає, кільце не червоне і засічки немає (очікування 0);
- у ФРАЗІ НЕМАЄ ЖОДНОЇ ЦИФРИ і жодного рахунку днів (Олег, 29.07: цифри тягнуть
  хвіст умов — відпустка, лікарняна, — а лічильник днів звучить як завод і
  робить таймер зворотного відліку замість спокою). Темп говорить мовчки:
  кольором і засічкою. Кальки «позаду графіка» теж немає;
- при відставанні колір бурштиновий, НЕ червоний: на власному екрані червоне
  демотивує, а не інформує;
- випередження і закриття норми мають свої фрази, і жодна фраза не хвалить за нуль;
- кроки тижнів малюються: закритий — з галочкою, поточний — підсвічений,
  а над рядом стоїть «Тижні серпня» — інакше «1–5» читається як кількість;
- «що далі» дає конкретику (скільки лишилось + що стаття закриває три);
- САЛЮТ спрацьовує РІВНО ОДИН РАЗ на місяць і норму (памʼять у localStorage):
  інакше людина, що закрила норму 20-го, до кінця місяця відкриває апку під
  конфеті;
- смуга й кільце приїжджають анімацією з нуля, а не малюються одразу повними;
- біля фрази є довідкове «?», і воно пояснює саме нове поняття «на сьогодні»;
- робота ПОЗА нормою («+22 новини в стрічку») видима підписом, без другої
  смуги прогресу і без слова «рерайт»;
- журналістка може сама попросити відпустку: дати, тип і коментар їдуть на
  сервер, Каті це приходить на погодження;
- у менеджерському «Звіті» першого числа НІХТО не червоний.

Запуск (потрібні playwright + chromium):
    python test_webapp_pace.py
"""

import asyncio
import json
import re
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

PEOPLE = [{"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
           "photo": None, "photo_sm": None, "photo_orig": None}]

TASKS = [{
    "id": 1, "person": "Аліна Квітко", "creator": "Катерина Середа",
    "project_id": 1, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
    "platform": None, "type": "news", "theme_id": 1, "theme_name": "Тендери",
    "qty": 2, "note": "", "deadline": "2026-08-14", "status": "open",
    "auto_done": False, "created_at": "2026-08-01T10:00:00+03:00",
    "done_at": None, "done_count": 0, "matches": [],
}]

JOURNALIST = {
    "me": {"name": "Аліна Квітко", "first_name": "Аліна", "dept": "creative",
           "dept_title": "Creative", "manager": False},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": [],
    "assignees": [], "managers": [], "tasks": TASKS,
}


def norm(fact, target, expected, pace, phase, left, steps=(), done=False,
         remaining=None):
    """Норма у форматі /api/kpi з полями темпу (team_kpi.pace_row)."""
    return [{
        "id": 1, "dept": "creative", "metric": "news", "period": "month",
        "target": target, "own": True, "dept_title": "Creative",
        "period_label": "серпень 2026",
        "pace": {"elapsed": 21 - left, "total": 21, "left": left,
                 "share": (21 - left) / 21, "phase": phase, "is_current": True},
        "week_steps": list(steps),
        "rows": [{
            "person": "Аліна Квітко", "fact": fact, "target": target,
            "base_target": target, "overridden": False, "note": None,
            "excused": False, "done": done, "articles": 0, "article_weight": 3,
            "feed_news": 22,
            "remaining": target - fact if remaining is None else remaining,
            "days_left": left, "expected": expected, "pace": pace,
            "pace_pct": None if not expected else min(100, round(fact / expected * 100)),
        }],
    }]


# Накопичувальні кроки: перший тиждень позаду графіка, на другому наздогнала
STEPS = [
    {"label": "3–9", "fact": 2, "fact_cum": 2, "expected_cum": 5, "done": False,
     "is_current": False, "future": False, "away": False},
    {"label": "10–16", "fact": 5, "fact_cum": 7, "expected_cum": 7, "done": True,
     "is_current": True, "future": False, "away": False},
    {"label": "17–23", "fact": 0, "fact_cum": 7, "expected_cum": 14, "done": False,
     "is_current": False, "future": True, "away": False},
    {"label": "24–30", "fact": 0, "fact_cum": 7, "expected_cum": 20, "done": False,
     "is_current": False, "future": True, "away": False},
]

# Три стани, які має пережити екран
CASES = {
    # 1 серпня: нічого не зроблено і нічого не чекають
    "start": norm(0, 20, 0, "start", "start", 21),
    # 14 серпня: 7 із очікуваних 9 — відстає
    "behind": norm(7, 20, 9, "behind", "mid", 12, steps=STEPS),
    # норму закрито
    "done": norm(20, 20, 20, "done", "mid", 12, steps=STEPS, done=True),
}

STUB = """
window.__haptics = [];
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: "light",
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(t){ window.__haptics.push(t); } } } };
window.fetch = async (url, opts = {}) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: window.KPI_NORMS || [],
    week_label: "10–16.08", month_label: "серпень 2026", site_db: true });
  if (url.startsWith("/api/kpi/person")) return json({ has_norms: false, months: [] });
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  if (url === "/api/absences/request") {
    window.__askedAbsence = JSON.parse(opts.body);
    return json({ ok: true });
  }
  if (url.startsWith("/api/kpi/dashboard")) return json(window.DASH || { people: [] });
  return json({ ok: true });
};
"""

# Дашборд менеджера першого числа: факт 0, очікування 0 — темпу немає
DASH_FIRST_DAY = {
    "period": "month", "offset": 0, "label": "серпень 2026", "is_current": True,
    "site_db": True,
    "pace": {"elapsed": 0, "total": 21, "left": 21, "share": 0.0,
             "phase": "start", "is_current": True},
    "people": [{
        "person": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
        "overall_pct": 0, "pace_pct": None, "pace": "start", "missed_days": 0,
        "norms": [{"label": "20 власних новин", "metric": "news", "own": True,
                   "fact": 0, "target": 20, "pct": 0, "expected": 0,
                   "pace": "start", "pace_pct": None, "articles": 0,
                   "article_weight": 3, "done": False, "beyond": False}],
    }],
}

MANAGER = {**JOURNALIST,
           "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
                  "dept_title": "Адміністративний", "manager": True}}


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, boot, norms, dash=None):
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
        "window.KPI_NORMS = " + json.dumps(norms) + ";"
        "window.DASH = " + json.dumps(dash or {"people": []}) + ";" + STUB)
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

    print("Прогрес періоду на екрані журналістки:")
    async with async_playwright() as pw:

        # ---------- 1 серпня: нуль — це нормально ----------
        browser, page = await _open(pw, JOURNALIST, CASES["start"])
        try:
            await page.wait_for_selector(".pace-say", timeout=5000)
            say = await page.inner_text(".pace-say")
            check("першого числа фраза про початок місяця", "почався" in say)
            check("названо сам місяць, а не «місяць»", "Серпень" in say)
            check("у фразі НЕМАЄ жодної цифри",
                  not re.search(r"\d", say))
            check("засічки немає — очікування нульове",
                  await page.locator(".avaring .mark").count() == 0)
            color = await page.locator(".me-cap b").count()
            check("відсотка в шапці немає, поки нічого не зроблено", color == 0)
            check("червоного на екрані немає",
                  "var(--red)" not in await page.content())
            await page.screenshot(path="/tmp/pace-start.png", full_page=True)
        finally:
            await browser.close()

        # ---------- 14 серпня: відстає ----------
        browser, page = await _open(pw, JOURNALIST, CASES["behind"])
        try:
            await page.wait_for_selector(".pace-say", timeout=5000)
            say = await page.inner_text(".pace-say")
            # Олег, 29.07: цифри у фразі зайві (тягнуть хвіст умов: відпустка,
            # лікарняна), а рахунок днів звучить як завод і робить таймер
            # зворотного відліку замість спокою
            check("у фразі НЕМАЄ жодної цифри", not re.search(r"\d", say))
            check("немає рахунку робочих днів", "робочих днів" not in say)
            check("немає кальки «позаду графіка»", "графік" not in say)
            check("фраза підказує додати темпу", "темп" in say)
            check("фраза коротка (в один рядок)", len(say) < 60)

            ring = await page.locator(".me-cap b").get_attribute("style")
            check("відставання бурштинове, а не червоне",
                  "--warn" in (ring or ""))

            check("засічка «де маєш бути» намальована",
                  await page.locator(".avaring .mark").count() == 1)

            # кроки тижнів
            check("кроки тижнів намальовані",
                  await page.locator(".step").count() == 4)
            check("тиждень «була в графіку» — із галочкою",
                  await page.locator(".step.done .sdot svg").count() == 1)
            check("поточний тиждень підсвічено",
                  await page.locator(".step.cur").count() == 1)
            check("майбутні тижні не закриті",
                  await page.locator(".step.fut").count() == 2)
            # Олег, 29.07: «1–5, 6–12 незрозуміло — хтось подумає, що це
            # кількість новин». Підпис має сказати, що це дні місяця.
            cap = await page.inner_text(".steps-cap")
            check("над рядом тижнів сказано, що це за числа", "Тижні" in cap)
            check("і саме якого місяця, у родовому відмінку", "серпня" in cap)
            check("незакритий минулий тиждень позначено бурштином",
                  await page.locator(".step.miss").count() == 1)

            kpi_txt = await page.inner_text("#my-kpi")
            check("новини в стрічку видно окремим підписом",
                  "+22 новини в стрічку" in kpi_txt)
            check("число відмінюється правильно (22 новинИ, не новин)",
                  "22 новин в" not in kpi_txt)
            check("слова «рерайт» в інтерфейсі немає",
                  "рерайт" not in kpi_txt.lower())
            check("смуги прогресу для них немає — це не норма",
                  await page.locator("#my-kpi .kbar").count() == 1)

            # Дзеркало для Newsroom (Олег, 29.07): їхня норма рахує всі новини
            # гуртом, тож власний матеріал у ній не видно взагалі
            await page.evaluate("""
              window.KPI_NORMS[0].rows[0].feed_news = 0;
              window.KPI_NORMS[0].rows[0].own_news = 4;
              renderMyKpi();
            """)
            await page.wait_for_timeout(250)
            own_txt = await page.inner_text("#my-kpi")
            check("власні матеріали видно окремим підписом",
                  "4 власні матеріали" in own_txt)
            check("і другої смуги для них теж немає",
                  await page.locator("#my-kpi .kbar").count() == 1)
            check("тон теплий, а не нейтральний, як у стрічки",
                  await page.locator("#my-kpi .mk-own").count() == 1)

            nxt = await page.inner_text(".pace-next")
            check("«що далі» каже, скільки лишилось", "13" in nxt)
            check("і що стаття закриває три", "стаття закриє 3" in nxt)

            check("салюту без закритої норми немає",
                  await page.locator(".confetti").count() == 0)

            # --- запит на відпустку від самої журналістки ---
            check("є кнопка запиту відсутності",
                  await page.locator("[data-ask]").count() == 1)
            # Підпис має перелічувати всі три типи: спершу там стояло
            # «Відпустка чи лікарняна», і відрядження виглядало відсутнім
            ask_label = await page.inner_text("[data-ask]")
            check("на кнопці перелічені всі три типи",
                  all(w in ask_label.lower()
                      for w in ("відпустка", "лікарняна", "відрядження")))
            await page.click("[data-ask]")
            await page.wait_for_selector("#ar-start", timeout=3000)
            kinds = await page.locator("[data-ak]").all_inner_texts()
            check(f"у шторці всі три типи ({', '.join(kinds)})",
                  set(kinds) == {"Відпустка", "Лікарняна", "Відрядження"})
            await page.fill("#ar-start", "2026-08-10")
            await page.fill("#ar-end", "2026-08-17")
            await page.fill("#ar-note", "давно планувала")
            await page.click("#ar-send")
            await page.wait_for_timeout(400)
            req = await page.evaluate("window.__askedAbsence")
            check("запит пішов із датами і типом",
                  bool(req) and req["start"] == "2026-08-10"
                  and req["end"] == "2026-08-17" and req["kind"] == "vacation")
            check("і з коментарем", req and req["note"] == "давно планувала")

            # --- довідка «?»: пояснює саме те, що щойно ввели ---
            check("біля фрази стоїть «?»",
                  await page.locator('.qmark[data-help="pace"]').count() == 1)
            check("вопросиків на екрані не більше двох",
                  await page.locator(".qmark").count() <= 2)
            await page.click('.qmark[data-help="pace"]')
            await page.wait_for_selector("#sheet h2", timeout=2000)
            hs = await page.inner_text("#sheet")
            check("шторка пояснює кільце", "заповнення" in hs.lower())
            check("і що означають кружечки", "тижні місяця" in hs)
            check("і що порожнє кільце на початку — нормально", "нормально" in hs)
            check("і що відпустка зменшує норму", "Відпустка" in hs)
            await page.click("#help-ok")
            await page.wait_for_timeout(200)
            check("шторка закрилась",
                  await page.locator("#sheet-backdrop.hidden").count() == 1)
            await page.screenshot(path="/tmp/pace-behind.png", full_page=True)
        finally:
            await browser.close()

        # ---------- норму закрито: салют рівно раз ----------
        browser, page = await _open(pw, JOURNALIST, CASES["done"])
        try:
            await page.wait_for_selector(".pace-say", timeout=5000)
            say = await page.inner_text(".pace-say")
            check("закриту норму видно у фразі", "закрито" in say)
            await page.wait_for_timeout(150)
            check("салют вистрелив", await page.locator(".confetti").count() == 1)
            check("і дав вібрацію", await page.evaluate(
                "window.__haptics.includes('success')") is True)

            # перемальовуємо екран ще раз — святкувати вдруге не можна
            await page.evaluate("window.__haptics = []")
            await page.evaluate("document.querySelectorAll('.confetti')"
                                ".forEach(e => e.remove())")
            await page.evaluate("renderMyKpi()")
            await page.wait_for_timeout(250)
            check("повторний вхід салюту НЕ повторює",
                  await page.locator(".confetti").count() == 0)
            check("і вібрації теж",
                  await page.evaluate("window.__haptics.length") == 0)

            key = await page.evaluate(
                "Object.keys(localStorage).filter(k => k.startsWith('kpiCheers'))")
            check("памʼять святкування прив'язана до місяця і норми",
                  key and "серпень 2026" in key[0])
            await page.screenshot(path="/tmp/pace-done.png", full_page=True)
        finally:
            await browser.close()

        # ---------- анімація заливки ----------
        browser, page = await _open(pw, JOURNALIST, CASES["behind"])
        try:
            # Початковий нуль зловити гонкою не можна (він живе один кадр),
            # тому перевіряємо сам механізм: перехід оголошено, разова
            # позначка data-fill спожита, значення доїхало.
            await page.wait_for_selector(".kbar i", timeout=5000)
            trans = await page.locator(".kbar i").first.evaluate(
                "el => getComputedStyle(el).transitionProperty")
            check("смузі оголошено перехід ширини", "width" in trans)
            await page.wait_for_timeout(900)
            after = await page.locator(".kbar i").first.evaluate(
                "el => el.style.width")
            check("смуга доїхала до свого значення", after == "35%")
            check("разову позначку анімації спожито (вдруге не поповзе)",
                  await page.locator(".kbar i[data-fill]").count() == 0)
            off = await page.locator(".avaring .prog").first.evaluate(
                "el => el.style.strokeDashoffset")
            check("кільце теж доїхало", off not in ("", "276.5"))
        finally:
            await browser.close()

        # ---------- менеджерський «Звіт» першого числа ----------
        browser, page = await _open(pw, MANAGER, [], DASH_FIRST_DAY)
        try:
            await page.evaluate("setHomeView('report'); renderHome()")
            await page.wait_for_selector(".ring-cell", timeout=5000)
            pct = await page.locator(".rc-pct").first.get_attribute("style")
            check("першого числа кільце не червоне (темпу ще немає)",
                  "hsl(0," not in (pct or "").replace(" ", ""))
            check("замість кольору — нейтральний сірий",
                  "--muted" in (pct or ""))
            await page.screenshot(path="/tmp/pace-dash-first-day.png",
                                  full_page=True)
        finally:
            await browser.close()

    print()
    bad = [n for n, ok in results if not ok]
    for name, ok in results:
        print(("  ✅ " if ok else "  ❌ ") + name)
    print()
    if bad:
        print(f"❌ Провалено: {len(bad)} із {len(results)}")
        return 1
    print(f"{len(results)}/{len(results)} перевірок пройдено")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
