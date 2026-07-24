"""
Веб-шар Mini App «Команда»: aiohttp-сервер поруч із polling-ботом (хвиля 1).

Один процес — два входи: python-telegram-bot крутить polling, aiohttp слухає
HTTP на тому ж Railway-сервісі. Стартує з post_init (event loop уже живий) і
ТІЛЬКИ коли задано PORT — без нього модуль тихо спить, тож деплой безпечний
до моменту, коли Railway-сервісу ввімкнуть публічний домен.

Налаштування (разово):
  1. Railway → сервіс бота → Settings → Networking → Generate Domain
     (з'явиться PORT в env і https-домен).
  2. WEBAPP_URL = https://<домен> — для кнопок «Відкрити» у пінгах і /team.
  3. BotFather → /newapp для @mykvisti_bot → Web App URL = WEBAPP_URL
     (дасть прямий лінк t.me/mykvisti_bot/<shortname> — можна кидати в чат).
  4. За бажанням BotFather → Menu Button → той самий URL.

Авторизація: Telegram Mini App шле window.Telegram.WebApp.initData — підписаний
HMAC-ом рядок з user id/username. Перевіряємо підпис секретом від BOT_TOKEN
(алгоритм з docs Telegram), свіжість auth_date ≤ 24 год, далі резолвимо людину
через team_roster (чужинець = 403, навіть із валідним підписом). Це САМОСТІЙНИЙ
захист: закритість чату, звідки відкрили апку, ролі не грає.

GET /health віддає 200 — придатний і як VIBER_WEBHOOK_URL (Viber вимагає
живий endpoint перед постингом у канал).
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — модуль просто "не налаштований"
    web = None

from handlers import team_roster, team_tasks

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = os.environ.get("PORT") or os.environ.get("WEBAPP_PORT")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
# Прямий лінк апки з BotFather (t.me/mykvisti_bot/team) — для запуску з груп.
WEBAPP_DIRECT_LINK = os.environ.get("WEBAPP_DIRECT_LINK", "")

INIT_DATA_MAX_AGE = 24 * 3600

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")


def is_configured():
    return bool(web and PORT and BOT_TOKEN)


# ---------- Авторизація ----------

def _verify_init_data(init_data):
    """Перевіряє підпис initData за алгоритмом Telegram. Повертає dict user
    ({id, username, first_name, ...}) або кидає ValueError."""
    if not init_data:
        raise ValueError("initData порожній")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    received_hash = None
    fields = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            fields.append(f"{key}={value}")
    if not received_hash:
        raise ValueError("немає hash")
    check_string = "\n".join(sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        raise ValueError("підпис не збігається")
    data = dict(pairs)
    auth_date = int(data.get("auth_date", "0"))
    if time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise ValueError("initData протух")
    user = json.loads(data.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("немає user")
    return user


async def _authenticate(request):
    """Розбирає Authorization: tma <initData>, резолвить людину з ростера.
    Повертає (person, info, tg_user) або кидає web.HTTPException."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("tma "):
        raise web.HTTPUnauthorized(text="Відкрийте апку через Telegram")
    try:
        tg_user = _verify_init_data(auth[4:].strip())
    except ValueError as e:
        raise web.HTTPUnauthorized(text=f"Невалідний initData: {e}")
    person = await asyncio.to_thread(
        team_roster.resolve_person, tg_user["id"], tg_user.get("username")
    )
    if not person:
        raise web.HTTPForbidden(
            text="Ця апка — для команди МикВісті. Якщо ти з редакції — напиши Олегу."
        )
    try:
        await asyncio.to_thread(
            team_roster.remember_user, tg_user["id"], tg_user.get("username"), person
        )
    except Exception as e:
        print(f"webapp: не вдалось закешувати tg_id для «{person}» — {e}")
    return person, team_roster.person_info(person), tg_user


# ---------- API ----------

async def api_me(request):
    person, info, tg_user = await _authenticate(request)
    stats = await asyncio.to_thread(team_tasks.stats_for, person)
    return web.json_response({
        "name": person,
        "first_name": tg_user.get("first_name") or person.split()[0],
        "dept": info["dept"],
        "dept_title": team_roster.DEPT_TITLES.get(info["dept"], info["dept"]),
        "manager": info["manager"],
        "stats": stats,
    })


async def api_roster(request):
    person, info, _ = await _authenticate(request)
    people = [
        {
            "name": name,
            "dept": p["dept"],
            "dept_title": team_roster.DEPT_TITLES.get(p["dept"], p["dept"]),
            "manager": p["manager"],
        }
        for name, p in team_roster.ROSTER.items()
    ]
    return web.json_response({"people": people})


async def api_tasks_list(request):
    person, info, _ = await _authenticate(request)
    assignee = None if info["manager"] else person
    tasks = await asyncio.to_thread(team_tasks.list_tasks, assignee)
    return web.json_response({"tasks": tasks})


async def api_tasks_create(request):
    person, info, _ = await _authenticate(request)
    if not info["manager"]:
        raise web.HTTPForbidden(text="Таски ставлять Олег і головред")
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Очікую JSON")
    title = (payload.get("title") or "").strip()
    assignee = payload.get("assignee")
    if not title:
        raise web.HTTPBadRequest(text="Порожній заголовок")
    if assignee not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома виконавиця")
    priority = payload.get("priority", 1)
    if priority not in (0, 1, 2):
        raise web.HTTPBadRequest(text="priority: 0, 1 або 2")
    task = await asyncio.to_thread(
        team_tasks.create_task,
        person, assignee, title,
        (payload.get("project") or "").strip(),
        (payload.get("body") or "").strip(),
        payload.get("deadline") or None,
        priority,
    )
    # Пінг — після відповіді не чекаємо: створення таска не має висіти на Telegram API
    asyncio.get_running_loop().create_task(
        team_tasks.ping_assigned(request.app["bot"], task)
    )
    return web.json_response({"task": task})


async def api_tasks_patch(request):
    person, info, _ = await _authenticate(request)
    task_id = int(request.match_info["task_id"])
    current = await asyncio.to_thread(team_tasks.get_task, task_id)
    if not current:
        raise web.HTTPNotFound(text="Таска немає")
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Очікую JSON")

    if not info["manager"]:
        # Виконавиця: тільки власний таск, тільки статусні ходи + лінк на матеріал
        if current["assignee"] != person:
            raise web.HTTPForbidden(text="Це не твій таск")
        extra = set(payload) - {"status", "article_url"}
        if extra:
            raise web.HTTPForbidden(text="Можна змінювати лише статус і лінк на матеріал")
        new_status = payload.get("status")
        if new_status and (current["status"], new_status) not in team_tasks.EXECUTOR_MOVES:
            raise web.HTTPForbidden(text=f"Хід {current['status']} → {new_status} — за менеджером")
    else:
        new_status = payload.get("status")
        if new_status and new_status not in team_tasks.STATUSES:
            raise web.HTTPBadRequest(text="Невідомий статус")

    task = await asyncio.to_thread(team_tasks.update_task, task_id, person, payload)

    old, new = current["status"], task["status"]
    bot = request.app["bot"]
    loop = asyncio.get_running_loop()
    if old != new:
        if new == "review":
            loop.create_task(team_tasks.ping_review(bot, task))
        elif new == "done":
            loop.create_task(team_tasks.ping_done(bot, task))
        elif old == "review" and new == "doing" and person != task["assignee"]:
            loop.create_task(team_tasks.ping_returned(bot, task))
    return web.json_response({"task": task})


async def api_task_events(request):
    person, info, _ = await _authenticate(request)
    task_id = int(request.match_info["task_id"])
    current = await asyncio.to_thread(team_tasks.get_task, task_id)
    if not current:
        raise web.HTTPNotFound(text="Таска немає")
    if not info["manager"] and current["assignee"] != person:
        raise web.HTTPForbidden(text="Це не твій таск")
    events = await asyncio.to_thread(team_tasks.get_task_events, task_id)
    return web.json_response({"events": events})


# ---------- Статика ----------

async def index(request):
    return web.FileResponse(
        os.path.join(_STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


async def health(request):
    return web.Response(text="ok")


# ---------- Старт ----------

async def start_webapp(application):
    """Піднімає HTTP-сервер у тому ж event loop, що polling. Викликається з
    post_init. Без PORT (домен Railway ще не ввімкнено) — тихо спить."""
    if not is_configured():
        print("webapp: PORT/aiohttp не налаштовано — Mini App спить")
        return
    app = web.Application()
    app["bot"] = application.bot
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/me", api_me),
        web.get("/api/roster", api_roster),
        web.get("/api/tasks", api_tasks_list),
        web.post("/api/tasks", api_tasks_create),
        web.patch("/api/tasks/{task_id:\\d+}", api_tasks_patch),
        web.get("/api/tasks/{task_id:\\d+}/events", api_task_events),
        web.static("/static", _STATIC_DIR),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(PORT))
    await site.start()
    print(f"webapp: Mini App слухає на :{PORT}")


# ---------- /team ----------

async def team_handler(update, context):
    """/team — кнопка відкриття апки. У приваті — нативна web_app кнопка;
    у групі web_app недоступна, тож даємо прямий лінк (якщо зареєстровано
    в BotFather) або відправляємо в приват."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    if not WEBAPP_URL:
        await update.message.reply_text(
            "Mini App ще не налаштована: потрібні домен Railway (PORT), "
            "WEBAPP_URL і /newapp у BotFather — див. docs/TEAM_APP_MODULE.md."
        )
        return
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "🦊 Завдання і KPI команди:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Відкрити «Команду»", web_app=WebAppInfo(url=WEBAPP_URL))]]
            ),
        )
    elif WEBAPP_DIRECT_LINK:
        await update.message.reply_text(
            "🦊 Завдання і KPI команди:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Відкрити «Команду»", url=WEBAPP_DIRECT_LINK)]]
            ),
        )
    else:
        await update.message.reply_text(
            "🦊 Відкрий мене в приваті й надішли /team — там кнопка апки."
        )
