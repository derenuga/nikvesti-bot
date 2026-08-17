"""
Повний цикл генератора каруселей у справжньому браузері й проти справжнього
aiohttp-сервера: токен → сторінка → план → правка → експорт.

Чому не заглушка сервера, як у решті webapp-тестів: тут половина сенсу саме
в бекенді — токен, кеш плану, підстановка цитат. Тому піднімається реальний
сервер із реальними маршрутами, і живим лишається все, крім двох речей, яких
у прогоні бути не може: походу в мережу за статтею (замість нього фікстура з
data/) і виклику Sonnet (CAROUSEL_FAKE_PLAN=1). Правило CLAUDE.md «тест не
має залежати від того, чи є мережа» тут виконується буквально: і фото, і
шрифти, і лого перехоплюються маршрутами Playwright.

Що стережеться:
  • без токена — підказка «візьми лінк у бота», а не білий екран і не редактор;
  • експорт РІВНО 1080×1440 — формат Instagram з травня 2025, і саме його
    легко втратити, бо прев'ю малюється в 1620×2160;
  • лічильники й стрілки рахує КОД: додала людина слайд — «3/5» стало «3/6»,
    стрілка зникла з нового останнього. З увімкненим закликом і без;
  • ZIP відкривається справжнім zipfile і всередині справжні JPEG — архів
    пишеться руками, тож «наче качається» тут недостатньо;
  • Safari: ctx.filter приймається, але не діє — обробка фото має все одно
    спрацювати через ручний LUT, інакше ч/б лишиться кольоровим;
  • стаття без blockquote (322389) не отримує слайдів-цитат узагалі.

Запуск: python test_webapp_carousel.py
"""

import asyncio
import base64
import binascii
import io
import os
import pathlib
import struct
import sys
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent
WEBAPP = ROOT / "webapp"

os.environ.setdefault("STATE_PATH", "/tmp/carousel_webapp_test_state.json")
if os.path.exists(os.environ["STATE_PATH"]):
    os.remove(os.environ["STATE_PATH"])   # прогін має стартувати з чистого стану
os.environ.setdefault("WEBAPP_URL", "https://bot.example")
os.environ["CAROUSEL_FAKE_PLAN"] = "1"

PORT = int(os.environ.get("CAROUSEL_TEST_PORT", "8123"))

CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]

try:
    from aiohttp import web
except ImportError:
    print("aiohttp не встановлено — тест неможливий (pip install -r requirements.txt)")
    sys.exit(1)

sys.path.insert(0, str(ROOT))
from handlers import card_maker, carousel, storage  # noqa: E402


# ---------- Фікстури замість мережі ----------

def _fixture_article(url):
    """carousel.scrape_article без мережі: та сама збірка, але з файлу."""
    import gzip
    import re
    m = re.search(r"(32\d{4})", url or "")
    article_id = m.group(1) if m else "322371"
    path = ROOT / "data" / f"article_{article_id}.html.gz"
    if not path.exists():
        raise ValueError(f"немає фікстури {article_id}")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        html = f.read()
    data = card_maker.parse_article(html)
    body = card_maker.parse_article_body(html)
    data["quotes"] = body["quotes"]
    data["paragraphs"] = body["paragraphs"]
    data["author"] = body["author"]
    data["final_url"] = f"https://nikvesti.com/news/public/{article_id}-test"
    data["article_id"] = article_id
    if data["author"] and data["author"].get("photo"):
        # Пробу content-type перевіряє test_carousel.py; тут мережі немає
        data["author"]["photo_big"] = card_maker.author_photo_url(
            data["author"]["photo"])
    return data


def solid_png(w, h, rgb):
    """Мінімальний PNG без залежностей — фото для перехоплених запитів."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", binascii.crc32(tag + payload) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


PHOTO_PNG = solid_png(900, 600, (220, 40, 40))     # яскраво-червоний кадр
LOGO_PNG = solid_png(400, 180, (255, 255, 255))


def _chromium_path():
    for p in CHROMIUM_CANDIDATES:
        if p and pathlib.Path(p).exists():
            return p
    return None


async def start_server():
    app = web.Application()
    app.add_routes([
        web.get("/carousel", carousel.page),
        web.get("/api/carousel/state", carousel.api_state),
        web.post("/api/carousel/plan", carousel.api_plan),
        web.static("/static", str(WEBAPP)),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    return runner


async def open_page(pw, token, url_param=""):
    launch = {}
    path = _chromium_path()
    if path:
        launch["executable_path"] = path
    browser = await pw.chromium.launch(**launch)
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
    await route_network(page)
    link = f"http://127.0.0.1:{PORT}/carousel"
    params = []
    if token:
        params.append("k=" + token)
    if url_param:
        params.append("url=" + url_param)
    if params:
        link += "?" + "&".join(params)
    await page.goto(link)
    return browser, page


async def route_network(page):
    """Усе, що сторінка тягла б із інтернету, віддаємо самі."""
    async def img(route):
        await route.fulfill(status=200, content_type="image/png", body=PHOTO_PNG)

    async def logo(route):
        await route.fulfill(status=200, content_type="image/png", body=LOGO_PNG)

    async def empty_css(route):
        await route.fulfill(status=200, content_type="text/css", body="")

    await page.route("**/api/card/img*", lambda r: asyncio.ensure_future(
        logo(r) if "logo" in r.request.url else img(r)))
    await page.route("https://fonts.googleapis.com/**",
                     lambda r: asyncio.ensure_future(empty_css(r)))
    await page.route("https://fonts.gstatic.com/**",
                     lambda r: asyncio.ensure_future(empty_css(r)))
    await page.route("https://nikvesti.com/**", lambda r: asyncio.ensure_future(
        img(r)))


SAFARI_STUB = """
// Safari приймає ctx.filter і читає назад те саме, але при малюванні НЕ
// застосовує. Відтворюємо саме це — тоді детекція «за результатом» має
// повернути false, а обробка піти ручним LUT.
Object.defineProperty(CanvasRenderingContext2D.prototype, "filter", {
  get() { return this.__fakeFilter || "none"; },
  set(v) { this.__fakeFilter = v; },
  configurable: true,
});
"""


async def load_plan(page, url="322371"):
    await page.fill("#url", url)
    await page.click("#go")
    await page.wait_for_selector("#strip:not([hidden])", timeout=15000)
    await page.wait_for_function(
        "window.__carousel && window.__carousel.S.slides.length > 0", timeout=15000)
    # дочекатись, поки фото й лого доїдуть у canvas
    await page.wait_for_timeout(700)


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright не встановлено — тест пропущено (pip install playwright)")
        return 0

    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        if not cond and extra:
            results[-1] = (f"{name} — {extra}", False)

    storage.save_carousel_tokens({})
    token = carousel.token_for("Єлизавета Москвіна", 386403807)
    carousel.scrape_article = _fixture_article   # мережі в прогоні немає

    runner = await start_server()
    try:
        async with async_playwright() as pw:
            # ---------- 1. Без токена ----------
            browser, page = await open_page(pw, token="")
            try:
                await page.wait_for_selector("#gate:not([hidden])", timeout=10000)
                check("без токена показано підказку, а не білий екран",
                      await page.is_visible("#gate"))
                check("без токена редактор не відкривається",
                      not await page.is_visible("#app"))
                text = await page.inner_text("#gate")
                check("підказка називає команду бота", "/carousel" in text, text[:80])
            finally:
                await browser.close()

            # Чужий токен — теж ворота
            browser, page = await open_page(pw, token="сміття")
            try:
                await page.wait_for_selector("#gate:not([hidden])", timeout=10000)
                check("чужий токен не пускає", await page.is_visible("#gate"))
            finally:
                await browser.close()

            # ---------- 2. Робочий цикл ----------
            browser, page = await open_page(pw, token)
            try:
                await page.wait_for_selector("#app:not([hidden])", timeout=10000)
                check("з токеном відкривається редактор", await page.is_visible("#app"))
                check("видно, хто працює",
                      "Єлизавета" in await page.inner_text("#who"),
                      await page.inner_text("#who"))

                await load_plan(page, "322371")
                slides = await page.evaluate("__carousel.S.slides.map(s => s.type)")
                check("план прийшов слайдами", len(slides) >= 4, str(slides))
                check("перший слайд — обкладинка", slides[0] == "cover", str(slides))
                check("у стрічці стільки карток, скільки слайдів",
                      await page.eval_on_selector_all(
                          "#stripInner .slidecard", "els => els.length") == len(slides))

                # Лічильники і стрілки БЕЗ заклику
                chrome = await page.evaluate(
                    "__carousel.S.slides.map((_, i) => __carousel.chromeOf(i))")
                n = len(slides)
                check("лічильник рахується сам",
                      [c["counter"] for c in chrome] == [f"{i+1}/{n}" for i in range(n)],
                      str([c["counter"] for c in chrome]))
                check("стрілка на всіх, крім останнього",
                      [c["chevron"] for c in chrome] == [True] * (n - 1) + [False],
                      str([c["chevron"] for c in chrome]))

                # Додали слайд — арифметика перерахувалась сама
                await page.evaluate("__carousel.addSlide()")
                await page.wait_for_timeout(200)
                chrome2 = await page.evaluate(
                    "__carousel.S.slides.map((_, i) => __carousel.chromeOf(i))")
                check("новий слайд перерахував лічильники",
                      chrome2[0]["counter"] == f"1/{n + 1}", chrome2[0]["counter"])
                check("стрілка переїхала на новий останній",
                      chrome2[n - 1]["chevron"] is True and chrome2[n]["chevron"] is False,
                      str([c["chevron"] for c in chrome2]))

                # ---------- 3. Фінальний слайд-заклик ----------
                await page.check("#ctaOn")
                await page.wait_for_timeout(300)
                types = await page.evaluate("__carousel.S.slides.map(s => s.type)")
                check("заклик додається в кінець", types[-1] == "cta", str(types))
                chrome3 = await page.evaluate(
                    "__carousel.S.slides.map((_, i) => __carousel.chromeOf(i))")
                check("заклик не нумерується", chrome3[-1]["counter"] is None,
                      str(chrome3[-1]))
                check("знаменник не рахує заклик",
                      chrome3[0]["counter"] == f"1/{n + 1}", chrome3[0]["counter"])
                check("на закликові стрілки немає", chrome3[-1]["chevron"] is False)
                check("передостанній слайд отримав стрілку",
                      chrome3[-2]["chevron"] is True)

                # Заклик лишається останнім, хоч би куди його тягнули
                await page.evaluate(
                    "__carousel.moveSlide(__carousel.S.slides.length - 1, 0)")
                await page.wait_for_timeout(200)
                types = await page.evaluate("__carousel.S.slides.map(s => s.type)")
                check("заклик неможливо перетягнути в середину",
                      types[-1] == "cta" and types.count("cta") == 1, str(types))

                await page.uncheck("#ctaOn")
                await page.wait_for_timeout(300)
                types = await page.evaluate("__carousel.S.slides.map(s => s.type)")
                check("заклик знімається чекбоксом", "cta" not in types, str(types))

                # ---------- 4. Експорт ----------
                size = await page.evaluate("__carousel.slideSize(0)")
                check("експорт рівно 1080×1440",
                      size["w"] == 1080 and size["h"] == 1440,
                      f"{size['w']}×{size['h']}")
                check("слайд не порожній", size["bytes"] > 5000,
                      f"{size['bytes']} байт")

                b64 = await page.evaluate("__carousel.zipBase64()")
                raw = base64.b64decode(b64)
                zf = zipfile.ZipFile(io.BytesIO(raw))
                names = zf.namelist()
                count = await page.evaluate("__carousel.S.slides.length")
                check("ZIP відкривається справжнім розпакувальником",
                      zf.testzip() is None)
                check("імена файлів по порядку",
                      names == [f"{i+1:02d}.jpg" for i in range(count)], str(names))
                check("архів БЕЗ стиснення (JPEG уже стиснутий)",
                      all(i.compress_type == zipfile.ZIP_STORED
                          for i in zf.infolist()),
                      str([i.compress_type for i in zf.infolist()]))
                first = zf.read(names[0])
                check("усередині справжній JPEG",
                      first[:2] == b"\xff\xd8" and first[-2:] == b"\xff\xd9",
                      str(first[:4]))

                # ---------- 4a. Кегль, плашки, добудова кадру ----------
                # Заголовок із двома сусідніми виділеними словами: саме на
                # такому Олег побачив, що плашки злипаються, а текст у них
                # стоїть низько.
                # Далі все міряємо на ПЕРШОМУ слайді — попередні перевірки
                # рухали активний, і тап по мініатюрі поставив би фото не туди
                await page.evaluate("__carousel.selectSlide(0)")
                await page.wait_for_timeout(200)
                await page.evaluate("""() => {
                  const S = __carousel.S, s = S.slides[0];
                  s.type = 'cover';
                  s.title = 'Депутати вимагають прибрати мішки з піску від Дюка';
                  s.kicker = '';
                  s.words = window.CardKit.tokenize(s.title, []);
                  ['вимагають', 'прибрати', 'мішки'].forEach((w) => {
                    const t = s.words.find((x) => x.text === w);
                    if (t) t.hl = true;
                  });
                  s.slot.photoIdx = null; s.slot.photo = null;
                  s.scheme = 0;
                  s.fontScale = 1;
                }""")
                await page.wait_for_timeout(300)

                plates = await page.evaluate("""() => {
                  const CK = window.CardKit;
                  const c = document.createElement('canvas').getContext('2d');
                  CK.setFont(c, 100, 700);
                  const box = CK.plateBox(c, 100, 700);
                  return { height: box.height, top: box.top,
                           minLh: CK.minLineH(c, 100, 700) };
                }""")
                check("плашка рахується з метрик шрифту, а не з констант",
                      plates["height"] > 80 and plates["height"] < 160,
                      str(plates))
                check("міжрядок більший за плашку — рядки не злипаються",
                      plates["minLh"] > plates["height"], str(plates))
                check("плашка сидить навколо літер, а не під ними",
                      abs(abs(plates["top"]) - plates["height"] / 2) < plates["height"] * 0.22,
                      f"top={plates['top']:.1f} height={plates['height']:.1f}")

                # Кегль: 60% має дати менше тексту на екрані, 150% — більше
                async def ink(scale):
                    await page.evaluate(
                        "(s) => { __carousel.S.slides[0].fontScale = s; }", scale)
                    await page.wait_for_timeout(150)
                    return await page.evaluate("__carousel.sample(0, 0, 0, 1080, 1440)")

                small = await ink(0.6)
                big = await ink(1.5)
                check("повзунок кегля справді міняє розмір тексту",
                      big["r"] > small["r"] + 4,
                      f"60%={small['r']:.1f} 150%={big['r']:.1f} білого")
                await ink(1.0)

                # Фото, витягнуте вище краю: низ добудовується кольором самого
                # кадру, а не кольором схеми. Фото ставимо тапом по мініатюрі,
                # а не присвоєнням у стан: саме тап вантажить картинку.
                await page.click("#thumbs .thumb")
                await page.wait_for_timeout(800)
                check("тап по мініатюрі справді ставить фото на слайд",
                      await page.evaluate(
                          "!!__carousel.S.slides[__carousel.S.active].slot.photo"))
                pulled = await page.evaluate("""() => {
                  const s = __carousel.S.slides[0];
                  s.slot.offY = -400;
                  window.CardKit.clampSlot(s.slot, {x:0,y:0,w:1080,h:1440}, 0.45);
                  return s.slot.offY;
                }""")
                check("кадр можна витягнути за власну висоту",
                      pulled <= -300, f"offY={pulled}")
                await page.wait_for_timeout(300)
                bottom = await page.evaluate("__carousel.sample(0, 0, 1410, 1080, 25)")
                # Кадр у прогоні червоний, фон схеми синій. Порівнюємо ВІДТІНОК,
                # а не яскравість: градієнт добудови ще й притемнений, тож
                # абсолютні числа малі («r=20 b=4»), а от синій фон дав би
                # навпаки — b утричі більше за r.
                check("порожнеча знизу добудована кольором кадру, не фоном схеми",
                      bottom["r"] > bottom["b"] * 2,
                      f"r={bottom['r']:.0f} g={bottom['g']:.0f} b={bottom['b']:.0f}")
                await page.evaluate("""() => {
                  const s = __carousel.S.slides[0];
                  s.slot.offY = 0; s.slot.photoIdx = null; s.slot.photo = null;
                }""")
                await page.wait_for_timeout(200)

                # Лого на світлому фоні мусить бути темним
                await page.evaluate("() => { __carousel.S.slides[0].scheme = 2; }")
                await page.wait_for_timeout(250)
                logo_light = await page.evaluate("__carousel.sample(0, 84, 64, 210, 90)")
                await page.evaluate("() => { __carousel.S.slides[0].scheme = 0; }")
                await page.wait_for_timeout(250)
                logo_dark = await page.evaluate("__carousel.sample(0, 84, 64, 210, 90)")
                check("на білому фоні лого темне, а не біле на білому",
                      logo_light["r"] < 200, f"яскравість {logo_light['r']:.0f}")
                check("на синьому фоні лого світле",
                      logo_dark["r"] > logo_light["r"],
                      f"синій {logo_dark['r']:.0f} проти білого {logo_light['r']:.0f}")

                # ---------- 5. Скриншоти документів ----------
                docs = await page.evaluate("__carousel.S.images.map(i => !!i.doc_like)")
                check("скриншоти акта позначені в бібліотеці фото",
                      docs == [False, True, True], str(docs))
                check("сторінка попереджає про це людину",
                      await page.is_visible("#docHint"))

                # ---------- 6. Своє фото ----------
                tmp = pathlib.Path("/tmp/carousel_own_photo.png")
                tmp.write_bytes(solid_png(1200, 900, (20, 120, 60)))
                before = await page.evaluate("__carousel.S.images.length")
                await page.set_input_files("#photoFile", str(tmp))
                await page.wait_for_timeout(500)
                after = await page.evaluate("__carousel.S.images.length")
                check("своє фото додається в набір", after == before + 1,
                      f"{before} → {after}")
                check("своє фото одразу стає на поточний слайд",
                      await page.evaluate(
                          "__carousel.S.slides[__carousel.S.active].slot.photoIdx")
                      == after - 1)

                # ---------- 7. Прев'ю каруселі ----------
                await page.click("#openViewer")
                await page.wait_for_timeout(300)
                check("прев'ю каруселі відкривається",
                      await page.is_visible("#viewer"))
                check("на першому слайді «назад» недоступне",
                      await page.is_disabled("#viewerPrev"))
                await page.click("#viewerNext")
                await page.wait_for_timeout(200)
                check("гортання вперед працює",
                      not await page.is_disabled("#viewerPrev"))
                await page.click("#viewerClose")
                await page.wait_for_timeout(200)
                check("прев'ю закривається", not await page.is_visible("#viewer"))
            finally:
                await browser.close()

            # ---------- 8. Стаття без цитат ----------
            browser, page = await open_page(pw, token)
            try:
                await page.wait_for_selector("#app:not([hidden])", timeout=10000)
                await load_plan(page, "322389")
                types = await page.evaluate("__carousel.S.slides.map(s => s.type)")
                check("у статті без blockquote немає слайдів-цитат",
                      "quote" not in types, str(types))
                check("панель цитат схована, коли цитат немає",
                      not await page.is_visible("#quotesPanel"))
                size = await page.evaluate("__carousel.slideSize(0)")
                check("стаття без цитат теж експортується 1080×1440",
                      size["w"] == 1080 and size["h"] == 1440,
                      f"{size['w']}×{size['h']}")
            finally:
                await browser.close()

            # ---------- 9. Цитати з тексту статті ----------
            browser, page = await open_page(pw, token)
            try:
                await page.wait_for_selector("#app:not([hidden])", timeout=10000)
                await load_plan(page, "322377")
                check("панель цитат показує цитати статті",
                      await page.is_visible("#quotesPanel"))
                quotes = await page.evaluate("__carousel.S.article.quotes")
                slides_q = await page.evaluate(
                    "__carousel.S.slides.filter(s => s.type === 'quote')"
                    ".map(s => s.quote)")
                check("кожна цитата слайда дослівно є в тексті статті",
                      all(any(q.rstrip(".") in orig for orig in quotes)
                          for q in slides_q),
                      str([q[:40] for q in slides_q]))
                check("хвіст атрибуції на слайд не поїхав",
                      not any("сказав" in q or "запитав" in q or "заявив" in q
                              for q in slides_q),
                      str([q[-40:] for q in slides_q]))
                parts = await page.evaluate("__carousel.S.article.quote_parts")
                check("сервер віддав розібрані цитати сторінці",
                      len(parts) == len(quotes) and all("speech" in p for p in parts),
                      str(len(parts)))
            finally:
                await browser.close()

            # ---------- 10. Safari ----------
            for safari in (False, True):
                browser, page = await open_page(pw, token) if not safari else (None, None)
                if safari:
                    launch = {}
                    path = _chromium_path()
                    if path:
                        launch["executable_path"] = path
                    browser = await pw.chromium.launch(**launch)
                    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
                    await route_network(page)
                    await page.add_init_script(SAFARI_STUB)
                    await page.goto(f"http://127.0.0.1:{PORT}/carousel?k={token}")
                try:
                    await page.wait_for_selector("#app:not([hidden])", timeout=10000)
                    await load_plan(page, "322371")
                    label = "Safari (ctx.filter не діє)" if safari else "Chrome"
                    detected = await page.evaluate("__carousel.supportsFilter()")
                    check(f"{label}: детекція фільтра за результатом",
                          detected is not safari, f"SUPPORTS_FILTER={detected}")

                    # Ставимо фото і робимо його чорно-білим — кольоровий
                    # результат означав би, що обробка не спрацювала
                    await page.evaluate("""() => {
                      const s = __carousel.S.slides[0];
                      s.slot.photoIdx = 0;
                      s.type = 'photo';
                    }""")
                    await page.wait_for_timeout(400)
                    colour = await page.evaluate("__carousel.sample(0, 120, 700, 200, 200)")
                    await page.evaluate("""() => {
                      const s = __carousel.S.slides[0];
                      s.slot.adj = { bright: 102, contrast: 112, sat: 0, warm: 0, vivid: 0 };
                    }""")
                    await page.wait_for_timeout(200)
                    grey = await page.evaluate("__carousel.sample(0, 120, 700, 200, 200)")
                    check(f"{label}: кольоровий кадр справді кольоровий",
                          colour["r"] - colour["g"] > 60,
                          f"r={colour['r']:.0f} g={colour['g']:.0f}")
                    check(f"{label}: чорно-біле справді знебарвлює",
                          abs(grey["r"] - grey["g"]) < 14 and abs(grey["g"] - grey["b"]) < 14,
                          f"r={grey['r']:.0f} g={grey['g']:.0f} b={grey['b']:.0f}")
                finally:
                    await browser.close()

            # ---------- 11. Кеш плану ----------
            browser, page = await open_page(pw, token)
            try:
                await page.wait_for_selector("#app:not([hidden])", timeout=10000)
                await load_plan(page, "322371")
                source = await page.inner_text("#planSource")
                check("повторне відкриття новини бере план із памʼяті",
                      "памʼяті" in source, source)
            finally:
                await browser.close()
    finally:
        await runner.cleanup()

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
