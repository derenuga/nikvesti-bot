"""
Дим-тест ВЕБ-СЕРВЕРА Mini App: піднімає справжній aiohttp-сервер із тими
самими маршрутами, middleware і статикою, що в проді, і стукає в нього
клієнтом.

**Навіщо окремий тест.** Решта webapp-тестів (test_webapp_*.py) працюють у
браузері: вони підсовують апці заглушку `fetch` і перевіряють, що малює JS.
Тобто ВЕСЬ серверний шар — aiohttp, middleware кешу, віддача .gz-сусідів,
рендер index.html із версіями — не перевіряв ніхто. Це виявилось рівно тоді,
коли знадобилось оновити aiohttp: жодного способу переконатись, що апка
підніметься, крім «задеплоїти й подивитись», не було.

Перевіряється те, що ламається при зміні версії бібліотеки, а не наша логіка:

- сервер узагалі стартує (Application + middleware + AppRunner + TCPSite);
- `/health` відповідає (він же — VIBER_WEBHOOK_URL, тобто його падіння тихо
  ламає ще й дзеркало Viber);
- index.html віддається з no-cache і з версійованим `app.js?v=<хеш>` —
  саме це робить неможливим старий JS проти нового API;
- статика віддає ГОТОВИЙ .gz-сусід (403 КБ app.js → 115 КБ на кожен холодний
  старт апки в мобільній мережі);
- Cache-Control: вічний для версійованого URL, короткий для голого;
- запит до API без initData дає 401, а не 500 — тобто HTTPException із
  text= доїжджає до клієнта так, як ми розраховуємо.

Живих БД і токенів не треба: беруться лише ті маршрути, які нічого не
питають у Нори й у БД сайту.
"""
import asyncio
import os
import sys

os.environ.setdefault("PORT", "8099")
os.environ.setdefault("WEBAPP_URL", "https://example.invalid")
os.environ.setdefault("BOT_TOKEN", "123:abc")

PORT = 8099

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("aiohttp не встановлено — тест неможливий (pip install -r requirements.txt)")
    sys.exit(1)

from handlers import webapp


async def main():
    print(f"Веб-сервер Mini App (aiohttp {aiohttp.__version__}):")
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(f"  {'✅' if cond else '❌'} {name}{(' — ' + extra) if extra else ''}")

    webapp._prepare_static()
    app = web.Application(middlewares=[webapp.cache_headers_middleware])
    app.add_routes([
        web.get("/", webapp.index),
        web.get("/health", webapp.health),
        web.get("/api/bootstrap", webapp.api_bootstrap),
        web.static("/static", webapp._STATIC_DIR),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    base = f"http://127.0.0.1:{PORT}"

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/health") as r:
                check("/health віддає ok", r.status == 200 and (await r.text()) == "ok")

            async with s.get(f"{base}/") as r:
                html = await r.text()
                check("index.html віддається", r.status == 200 and "<" in html)
                check("index не кешується",
                      r.headers.get("Cache-Control") == "no-cache",
                      r.headers.get("Cache-Control", "—"))
                check("index посилається на версійований app.js", "app.js?v=" in html)

            async with s.get(f"{base}/static/app.js",
                             headers={"Accept-Encoding": "gzip"}) as r:
                body = await r.read()
                check("статика віддається стиснутою",
                      r.headers.get("Content-Encoding") == "gzip",
                      r.headers.get("Content-Encoding", "без стиснення"))
                check("app.js цілий після розпакування", len(body) > 10000,
                      f"{len(body) // 1024} КБ")
                check("голий URL статики — короткий кеш",
                      r.headers.get("Cache-Control") == "public, max-age=300",
                      r.headers.get("Cache-Control", "—"))

            async with s.get(f"{base}/static/app.js?v=deadbeef") as r:
                check("версійований URL — вічний кеш",
                      "immutable" in (r.headers.get("Cache-Control") or ""),
                      r.headers.get("Cache-Control", "—"))

            async with s.get(f"{base}/api/bootstrap") as r:
                check("API без initData — 401 з поясненням людині",
                      r.status == 401 and "Telegram" in (await r.text()),
                      f"HTTP {r.status}")
    finally:
        await runner.cleanup()

    bad = [n for n, c in results if not c]
    print(f"\n{len(results) - len(bad)}/{len(results)} перевірок пройдено")
    if bad:
        print("Є падіння — див. ❌ вище.")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))
