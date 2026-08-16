"""
Паритет світлої й тёмної теми в Mini App «Команда» + знімки екранів.

Навіщо (28.07): тёмна тема була недороблена, і цього ніхто не бачив, бо
дивились у світлій. `--line` (світло-блакитний) захардкоджений і в `body.dark`
не перевизначався жодного разу — 21 використання, з них ЧОТИРИ як заливка,
включно з усіма скелетонами завантаження: при кожному вході на екран у темній
темі спалахували світло-блакитні блоки. А `--shadow` (7% темно-синього) на
темному фоні не видно фізично — при тому, що вся карткова система тримається
ТІЛЬКИ на ній. На симптом наступали двічі й обидва рази лікували точково
(трек кільця, бейдж впевненості), причину — жодного разу.

Тому тут не «подивитись очима», а перевірка:
- заливки (`--fill`) у темній темі ТЕМНІ, а у світлій світлі;
- розділювачі (`--line`) у двох темах різні, тобто тема таки перевизначена;
- `--fill` і `--line` — РІЗНІ змінні (одна змінна на дві роботи і була причиною:
  розділювачу потрібен низький контраст, заливці — помітність);
- скелетон завантаження в темній темі не світиться;
- тінь картки в темній темі не та сама, що у світлій.

Заразом знімає скриншоти всіх ключових екранів в обох темах — саме те, чого
бракувало, щоб помилку помітили в перший день.

Запуск:  python test_webapp_themes.py [каталог-для-знімків]
"""

import asyncio
import datetime
import json
import os
import pathlib
import sys

WEBAPP = pathlib.Path(__file__).resolve().parent / "webapp"

# Дати у фікстурах рахуються ВІД СЬОГОДНІ. Вписана числом майбутня дата — це
# міна з відомим запалом: 15.08 вона ще майбутня, 16.08 вже минула, і
# перевірка «найближчий звіт» падає без жодної правки коду (реальний випадок
# 16.08.2026, прогін на main).
def _in(days):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/theme-shots")

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

PEOPLE = [
    {"name": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
     "photo": None, "photo_sm": None, "photo_orig": None},
    {"name": "Юлія Бойченко", "dept": "creative", "dept_title": "Creative",
     "photo": None, "photo_sm": None, "photo_orig": None},
    {"name": "Таміла Ксьонжик", "dept": "newsroom", "dept_title": "Newsroom",
     "photo": None, "photo_sm": None, "photo_orig": None},
]

TASKS = [
    {"id": 1, "person": "Аліна Квітко", "creator": "Катерина Середа",
     "project_id": 1, "project_name": "Голоси Миколаєва", "partner_name": "IMS",
     "platform": None, "type": "news", "theme_id": 1, "theme_name": "Тендери",
     "qty": 2, "note": "Хто виграв тендери на укриття шкіл", "deadline": _in(-2),
     "status": "open", "auto_done": False, "created_at": "2026-08-01T10:00:00+03:00",
     "done_at": None, "done_count": 1, "matches": [
         {"id": 11, "title": "Тендер на укриття виграла фірма з Одеси",
          "url": "https://nikvesti.com/news/economics/321001-tender",
          "published": "2026-08-05T12:00:00+03:00", "source": "site"}]},
    {"id": 2, "person": "Аліна Квітко", "creator": "Катерина Середа",
     "project_id": 1, "project_name": "Голоси Миколаєва", "partner_name": "IWPR",
     "platform": None, "type": "article", "theme_id": 2,
     "theme_name": "Критичні інформаційні потреби", "qty": 1, "note": "",
     "deadline": _in(15), "status": "open", "auto_done": False,
     "created_at": "2026-08-01T10:00:00+03:00", "done_at": None,
     "done_count": 0, "matches": []},
]

PROJECTS = [{"id": 1, "name": "Голоси Миколаєва", "partner": "IMS", "logo": None,
             "logo_orig": None, "start_date": 1748000000, "end_date": 1790000000,
             "kpi_news": 30, "kpi_articles": 5, "themes": [], "deadlines": [],
             "drive_url": None},
            {"id": 2, "name": "Висвітлення війни та миру", "partner": "IWPR",
             "logo": None, "logo_orig": None, "start_date": 1740000000,
             "end_date": 1795000000, "kpi_news": 60, "kpi_articles": 8,
             "themes": [], "deadlines": [], "drive_url": None}]

JOURNALIST = {
    "me": {"name": "Аліна Квітко", "first_name": "Аліна", "dept": "creative",
           "dept_title": "Creative", "manager": False},
    "site_db": True, "nora": True, "people": PEOPLE, "projects": PROJECTS,
    "assignees": [], "managers": [], "tasks": TASKS,
}
MANAGER = {**JOURNALIST,
           "me": {"name": "Олег Деренюга", "first_name": "Олег", "dept": "admin",
                  "dept_title": "Адміністративний", "manager": True}}

KPI_NORMS = [{
    "id": 1, "dept": "creative", "metric": "news", "period": "month",
    "target": 20, "own": True, "dept_title": "Creative",
    "period_label": "серпень 2026",
    "pace": {"elapsed": 9, "total": 21, "left": 12, "share": 9 / 21,
             "phase": "mid", "is_current": True},
    "week_steps": [
        {"label": "3–9", "fact": 6, "target": 5, "done": True,
         "is_current": False, "future": False, "away": False},
        {"label": "10–16", "fact": 1, "target": 5, "done": False,
         "is_current": True, "future": False, "away": False},
        {"label": "17–23", "fact": 0, "target": 5, "done": False,
         "is_current": False, "future": True, "away": False},
        {"label": "24–30", "fact": 0, "target": 5, "done": False,
         "is_current": False, "future": True, "away": False},
    ],
    "rows": [{"person": "Аліна Квітко", "fact": 7, "target": 20,
              "base_target": 20, "overridden": False, "note": None,
              "excused": False, "done": False, "articles": 1,
              "article_weight": 3, "remaining": 13, "days_left": 12,
              "expected": 9, "pace": "behind", "pace_pct": 78}],
}]

DASH = {
    "period": "month", "offset": 0, "label": "серпень 2026", "is_current": True,
    "site_db": True,
    "pace": {"elapsed": 9, "total": 21, "left": 12, "share": 9 / 21,
             "phase": "mid", "is_current": True},
    "people": [
        {"person": "Аліна Квітко", "dept": "creative", "dept_title": "Creative",
         "overall_pct": 35, "pace_pct": 78, "pace": "behind", "missed_days": 0,
         "norms": [{"label": "20 власних новин", "fact": 7, "target": 20,
                    "pct": 35, "expected": 9, "pace": "behind", "pace_pct": 78,
                    "articles": 1, "article_weight": 3, "done": False,
                    "beyond": False, "metric": "news", "own": True}]},
        {"person": "Юлія Бойченко", "dept": "creative", "dept_title": "Creative",
         "overall_pct": 60, "pace_pct": 100, "pace": "ahead", "missed_days": 0,
         "norms": [{"label": "20 власних новин", "fact": 12, "target": 20,
                    "pct": 60, "expected": 9, "pace": "ahead", "pace_pct": 100,
                    "articles": 0, "article_weight": 3, "done": False,
                    "beyond": False, "metric": "news", "own": True}]},
        {"person": "Таміла Ксьонжик", "dept": "newsroom", "dept_title": "Newsroom",
         "overall_pct": 100, "pace_pct": 100, "pace": "done", "missed_days": 0,
         "norms": [{"label": "40 новин", "fact": 41, "target": 40, "pct": 100,
                    "expected": 40, "pace": "done", "pace_pct": 100,
                    "articles": 0, "article_weight": 3, "done": True,
                    "beyond": False, "metric": "news", "own": False}]},
    ],
}

STUB = """
window.Telegram = { WebApp: {
  initData: "stub", colorScheme: window.__scheme,
  ready(){}, expand(){}, onEvent(){}, disableVerticalSwipes(){},
  openLink(){}, showConfirm(m, c){ c(true); },
  BackButton: { show(){}, hide(){}, onClick(){} },
  HapticFeedback: { notificationOccurred(){} } } };
window.fetch = async (url) => {
  const json = (o) => new Response(JSON.stringify(o),
    { headers: { "Content-Type": "application/json" } });
  if (url === "/api/bootstrap") return json(window.BOOT);
  if (url === "/api/kpi") return json({ norms: window.KPI_NORMS || [],
    week_label: "10–16.08", month_label: "серпень 2026", site_db: true });
  if (url.startsWith("/api/kpi/person")) return json({ has_norms: false, months: [] });
  if (url === "/api/notifications") return json({ items: [], unread: 0 });
  if (url.startsWith("/api/kpi/dashboard")) {
    await new Promise((r) => setTimeout(r, window.__dashDelay || 0));
    return json(window.DASH);
  }
  if (url.startsWith("/api/matches/pending")) return json({ items: [] });
  return json({ ok: true });
};
"""

# Змінні теми Telegram. У світлій — як віддає застосунок; у темній — реальні
# кольори темної теми Telegram (bg світліший за secondary_bg: саме різниця
# поверхонь, а не тінь, дає підйом картки на темному).
THEME_VARS = {
    "light": {"--tg-theme-bg-color": "#ffffff",
              "--tg-theme-secondary-bg-color": "#f3f7fc",
              "--tg-theme-text-color": "#16283c",
              "--tg-theme-hint-color": "#6a7f96",
              "--tg-theme-button-color": "#1f6fd6",
              "--tg-theme-button-text-color": "#ffffff"},
    "dark": {"--tg-theme-bg-color": "#212d3b",
             "--tg-theme-secondary-bg-color": "#151f2b",
             "--tg-theme-text-color": "#f0f3f6",
             "--tg-theme-hint-color": "#8d9aa8",
             "--tg-theme-button-color": "#4a9eea",
             "--tg-theme-button-text-color": "#ffffff"},
}


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def _open(pw, boot, scheme):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 390, "height": 844},
                                  device_scale_factor=2)
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
    css = ";".join(f"{k}:{v}" for k, v in THEME_VARS[scheme].items())
    await page.add_init_script(
        f"window.__scheme = {json.dumps(scheme)};"
        "window.BOOT = " + json.dumps(boot) + ";"
        "window.KPI_NORMS = " + json.dumps(KPI_NORMS) + ";"
        "window.DASH = " + json.dumps(DASH) + ";" + STUB +
        f"document.addEventListener('DOMContentLoaded', () => "
        f"document.documentElement.setAttribute('style', {json.dumps(css)}));")
    await page.goto("https://app.local/")
    await page.wait_for_selector("#screen-main:not(.hidden)", timeout=10000)
    return browser, page


def _parse(rgb):
    parts = [float(x) for x in rgb.replace("rgba", "").replace("rgb", "")
             .strip("() ").split(",")]
    return parts[:3], (parts[3] if len(parts) > 3 else 1.0)


def _lum(rgb, over="rgb(255,255,255)"):
    """Яскравість КОЛЬОРУ НА ФОНІ: напівпрозору заливку не можна міряти окремо
    від поверхні — саме тому rgba(150,175,200,.16) у темній темі темна, хоча
    сам колір світлий. Через цю різницю помилку й не помічали."""
    (r, g, b), a = _parse(rgb)
    (br, bg_, bb), _ = _parse(over)
    mix = [a * c + (1 - a) * d for c, d in ((r, br), (g, bg_), (b, bb))]
    return (0.2126 * mix[0] + 0.7152 * mix[1] + 0.0722 * mix[2]) / 255


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    results, tokens = [], {}

    def check(name, cond):
        results.append((name, bool(cond)))

    print("Паритет тем і знімки екранів:")
    async with async_playwright() as pw:
        for scheme in ("light", "dark"):
            browser, page = await _open(pw, JOURNALIST, scheme)
            try:
                # Терпимо до старого коду: на ньому картки темпу ще немає, і
                # знімок «як було» має зніматись тим самим скриптом
                try:
                    await page.wait_for_selector(".pace-say", timeout=2500)
                except Exception:
                    pass
                await page.wait_for_timeout(900)
                tokens[scheme] = await page.evaluate("""() => {
                  const cs = getComputedStyle(document.body);
                  const sk = document.createElement('span');
                  sk.className = 'sk'; document.body.appendChild(sk);
                  const skBg = getComputedStyle(sk).backgroundColor;
                  sk.remove();
                  const card = document.querySelector('.soft-card');
                  return {
                    line: cs.getPropertyValue('--line').trim(),
                    fill: cs.getPropertyValue('--fill').trim(),
                    shadow: cs.getPropertyValue('--shadow').trim(),
                    skBg,
                    bg: getComputedStyle(document.body).backgroundColor,
                    cardShadow: card ? getComputedStyle(card).boxShadow : '',
                    hasDark: document.body.classList.contains('dark'),
                  };
                }""")
                await page.screenshot(path=str(OUT / f"01-journalist-{scheme}.png"),
                                      full_page=True)
            finally:
                await browser.close()

            browser, page = await _open(pw, MANAGER, scheme)
            try:
                # скелетон завантаження — саме там світились блакитні блоки
                await page.evaluate("window.__dashDelay = 4000")
                await page.evaluate("setHomeView('report'); renderHome()")
                await page.wait_for_selector(".sk", timeout=5000)
                await page.screenshot(path=str(OUT / f"02-skeleton-{scheme}.png"),
                                      full_page=True)
                await page.evaluate("window.__dashDelay = 0; renderDashboard()")
                await page.wait_for_selector(".ring-cell", timeout=6000)
                await page.wait_for_timeout(900)
                await page.screenshot(path=str(OUT / f"03-report-{scheme}.png"),
                                      full_page=True)
                await page.evaluate("setHomeView('tasks'); renderHome()")
                await page.wait_for_timeout(200)
                await page.screenshot(path=str(OUT / f"04-tasks-{scheme}.png"),
                                      full_page=True)
                await page.evaluate("nav('projects')")
                await page.wait_for_timeout(300)
                await page.screenshot(path=str(OUT / f"05-projects-{scheme}.png"),
                                      full_page=True)
            finally:
                await browser.close()

    light, dark = tokens["light"], tokens["dark"]
    check("тёмну тему взагалі увімкнено (body.dark)", dark["hasDark"])
    check("у світлій body.dark не стоїть", not light["hasDark"])
    check("--line і --fill — різні змінні, а не одна на дві роботи",
          bool(light["fill"]) and light["line"] != light["fill"])
    check("--line у темній перевизначено", light["line"] != dark["line"])
    check("--fill у темній перевизначено", light["fill"] != dark["fill"])
    check("--shadow у темній перевизначено", light["shadow"] != dark["shadow"])
    check(f"скелетон у світлій світлий ({light['skBg']})",
          _lum(light["skBg"], light["bg"]) > 0.75)
    check(f"скелетон у тёмній НЕ світиться ({dark['skBg']} на {dark['bg']})",
          _lum(dark["skBg"], dark["bg"]) < 0.35)
    check("картка в тёмній має власну тінь", "rgb" in (dark["cardShadow"] or ""))

    print()
    for name, ok in results:
        print(("  ✅ " if ok else "  ❌ ") + name)
    shots = sorted(p.name for p in OUT.glob("*.png"))
    print(f"\nЗнімків: {len(shots)} у {OUT}")
    for s in shots:
        print("   ", s)
    bad = [n for n, ok in results if not ok]
    if bad:
        print(f"\n❌ Провалено: {len(bad)} із {len(results)}")
        return 1
    print(f"\n{len(results)}/{len(results)} перевірок пройдено")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
