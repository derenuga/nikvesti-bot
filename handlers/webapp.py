"""
Веб-шар Mini App «Команда»: aiohttp-сервер поруч із polling-ботом.

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

API прототипу (концепція v2 — редакторський інтерфейс):
  GET  /api/bootstrap        — усе для старту апки одним запитом: я, люди з фото
                               (БД сайту), проєкти з лого і квотами (БД сайту),
                               тематики (Нора), останні creative tasks
  POST /api/tasks            — створити creative task (менеджер) + пінг
  PATCH /api/tasks/{id}      — статус open/done/dropped (менеджер)
  POST/PATCH/DELETE /api/themes… — тематики проєкту (менеджер)
Обидві БД опційні: без БД сайту люди без фото і проєкти порожні, без Нори
таски/тематики порожні — апка деградує, а не падає (site_db/nora у відповіді).

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

from handlers import team_kpi, team_projects, team_roster, team_tasks
from handlers.helpers import normalize_https_url

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = os.environ.get("PORT") or os.environ.get("WEBAPP_PORT")
WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))
# Прямий лінк апки з BotFather (t.me/mykvisti_bot/team) — для запуску з груп.
WEBAPP_DIRECT_LINK = normalize_https_url(os.environ.get("WEBAPP_DIRECT_LINK"))

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


async def _require_manager(request):
    person, info, tg_user = await _authenticate(request)
    if not info["manager"]:
        raise web.HTTPForbidden(text="Це редакторська дія")
    return person, info, tg_user


async def _json(request):
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Очікую JSON")


# ---------- API ----------

def _me_payload(person, info, tg_user):
    return {
        "name": person,
        "first_name": tg_user.get("first_name") or person.split()[0],
        "dept": info["dept"],
        "dept_title": team_roster.DEPT_TITLES.get(info["dept"], info["dept"]),
        "manager": info["manager"],
    }


def _bootstrap_blocking(person, is_manager):
    """Уся стартова вибірка одним потоком: БД сайту (люди/проєкти) + Нора
    (тематики/таски). Кожне джерело деградує окремо."""
    out = {"site_db": False, "nora": False}

    try:
        out["tasks"] = team_tasks.list_tasks(None if is_manager else person)
        out["nora"] = True
    except Exception as e:
        print(f"webapp: Нора недоступна — {e}")
        out["tasks"] = []

    if not is_manager:
        return out

    names = [n for n, p in team_roster.ROSTER.items() if not p["manager"]]
    depts = team_roster.dept_overrides() if out["nora"] else {}
    photos = {}
    projects = []
    try:
        photos = team_projects.avatar_map(names)
        projects = team_projects.list_projects()
        out["site_db"] = team_projects.is_configured()
    except Exception as e:
        print(f"webapp: БД сайту недоступна — {e}")
        photos = {n: None for n in names}

    themes = []
    if out["nora"]:
        try:
            themes = team_tasks.list_themes()
        except Exception as e:
            print(f"webapp: тематики не прочитались — {e}")

    theme_by_project = {}
    for t in themes:
        theme_by_project.setdefault(t["project_id"], []).append(
            {"id": t["id"], "name": t["name"], "planned": t["planned"]}
        )
    for p in projects:
        p["themes"] = theme_by_project.get(p["id"], [])

    out["people"] = [
        {
            "name": n,
            "dept": (dept := team_roster.effective_dept(n, depts)),
            "dept_title": team_roster.DEPT_TITLES.get(dept, ""),
            "photo": (photos.get(n) or {}).get("photo"),
            "photo_orig": (photos.get(n) or {}).get("photo_orig"),
        }
        for n in names
    ]
    out["projects"] = projects
    return out


async def api_bootstrap(request):
    person, info, tg_user = await _authenticate(request)
    data = await asyncio.to_thread(_bootstrap_blocking, person, info["manager"])
    data["me"] = _me_payload(person, info, tg_user)
    return web.json_response(data)


async def api_tasks_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)

    assignee = payload.get("person")
    if assignee not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    type_ = payload.get("type")
    if type_ not in team_tasks.TASK_TYPES:
        raise web.HTTPBadRequest(text="type: news або article")
    qty = payload.get("qty", 1)
    try:
        qty = max(1, min(99, int(qty)))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="qty: число")

    project_id = payload.get("project_id") or None
    project_name = None
    if project_id:
        projects = await asyncio.to_thread(team_projects.list_projects, False)
        match = next((p for p in projects if p["id"] == int(project_id)), None)
        if not match:
            raise web.HTTPBadRequest(text="Невідомий проєкт")
        project_name = match["name"]

    theme_id = payload.get("theme_id") or None
    theme_name = None
    if theme_id:
        themes = await asyncio.to_thread(team_tasks.list_themes)
        theme = next((t for t in themes if t["id"] == int(theme_id)), None)
        if not theme or (project_id and theme["project_id"] != int(project_id)):
            raise web.HTTPBadRequest(text="Тематика не з цього проєкту")
        theme_name = theme["name"]

    deadline = payload.get("deadline") or None
    if deadline:
        try:
            time.strptime(deadline, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="deadline: YYYY-MM-DD")

    task = await asyncio.to_thread(
        team_tasks.create_task,
        person, assignee, type_, project_id, project_name,
        theme_id, theme_name, qty, payload.get("note"), deadline,
    )
    # Пінг після відповіді — створення таска не має висіти на Telegram API
    asyncio.get_running_loop().create_task(
        team_tasks.ping_assigned(request.app["bot"], task)
    )
    return web.json_response({"task": task})


async def api_tasks_patch(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    status = payload.get("status")
    if status not in team_tasks.TASK_STATUSES:
        raise web.HTTPBadRequest(text="status: open, done або dropped")
    task = await asyncio.to_thread(
        team_tasks.set_status, int(request.match_info["task_id"]), person, status
    )
    if not task:
        raise web.HTTPNotFound(text="Таска немає")
    return web.json_response({"task": task})


def _validate_theme_format(payload):
    """format: news/article/post/video/hybrid або None (без формату)."""
    fmt = payload.get("format") or None
    if fmt is not None and fmt not in team_tasks.THEME_FORMATS:
        raise web.HTTPBadRequest(text="format: news, article, post, video, hybrid або порожньо")
    return fmt


async def api_themes_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    name = (payload.get("name") or "").strip()
    project_id = payload.get("project_id")
    if not name or not project_id:
        raise web.HTTPBadRequest(text="Потрібні project_id і назва")
    theme = await asyncio.to_thread(
        team_tasks.add_theme, int(project_id), name, payload.get("planned"),
        _validate_theme_format(payload),
    )
    return web.json_response({"theme": theme})


async def api_themes_patch(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    kwargs = {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="Порожня назва")
        kwargs["name"] = name
    if "planned" in payload:
        kwargs["planned"] = payload.get("planned")
    if "format" in payload:
        kwargs["format"] = _validate_theme_format(payload)
    theme = await asyncio.to_thread(
        team_tasks.update_theme, int(request.match_info["theme_id"]), **kwargs
    )
    if not theme:
        raise web.HTTPNotFound(text="Тематики немає")
    return web.json_response({"theme": theme})


async def api_themes_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await asyncio.to_thread(
        team_tasks.delete_theme, int(request.match_info["theme_id"])
    )
    if not deleted:
        raise web.HTTPNotFound(text="Тематики немає")
    return web.json_response({"ok": True})


# ---------- Відділ людини (Катя переносить в апці) ----------

async def api_people_dept(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = payload.get("person")
    dept = payload.get("dept")
    target_info = team_roster.ROSTER.get(who)
    if not target_info or target_info["manager"]:
        raise web.HTTPBadRequest(text="Невідома людина")
    if dept not in team_roster.MOVABLE_DEPTS:
        raise web.HTTPBadRequest(text="dept: newsroom, creative або digital")
    await asyncio.to_thread(team_roster.set_dept, who, dept, person)
    return web.json_response({"ok": True})


# ---------- KPI ----------

async def api_kpi(request):
    """Зведення KPI: менеджер — всі норми з людьми і прогресом; журналістка —
    норми свого відділу зі своїм рядком (для «Мої KPI»)."""
    person, info, _ = await _authenticate(request)
    payload = await asyncio.to_thread(
        team_kpi.kpi_payload, None if info["manager"] else person
    )
    return web.json_response(payload)


async def api_kpi_norm_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    dept = payload.get("dept")
    if dept not in team_roster.MOVABLE_DEPTS:
        raise web.HTTPBadRequest(text="Невідомий відділ")
    if payload.get("metric") not in team_kpi.KPI_METRICS:
        raise web.HTTPBadRequest(text="metric: news або article")
    if payload.get("period") not in team_kpi.KPI_PERIODS:
        raise web.HTTPBadRequest(text="period: week або month")
    try:
        target = int(payload.get("target"))
        assert 1 <= target <= 500
    except (TypeError, ValueError, AssertionError):
        raise web.HTTPBadRequest(text="target: число від 1 до 500")
    norm = await asyncio.to_thread(
        team_kpi.add_norm, person, dept, payload["metric"], payload["period"],
        target, bool(payload.get("own")),
    )
    return web.json_response({"norm": norm})


async def api_kpi_norm_patch(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    try:
        target = int(payload.get("target"))
        assert 1 <= target <= 500
    except (TypeError, ValueError, AssertionError):
        raise web.HTTPBadRequest(text="target: число від 1 до 500")
    norm = await asyncio.to_thread(
        team_kpi.update_norm, int(request.match_info["norm_id"]), target
    )
    if not norm:
        raise web.HTTPNotFound(text="Норми немає")
    return web.json_response({"norm": norm})


async def api_kpi_norm_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await asyncio.to_thread(
        team_kpi.delete_norm, int(request.match_info["norm_id"])
    )
    if not deleted:
        raise web.HTTPNotFound(text="Норми немає")
    return web.json_response({"ok": True})


async def api_kpi_override(request):
    """Правка людини на поточний період: target (0 = звільнена) + нотатка;
    {clear: true} — прибрати правку (повернути дефолт відділу)."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    norm_id = payload.get("norm_id")
    who = payload.get("person")
    if not norm_id or who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Потрібні norm_id і людина")
    if payload.get("clear"):
        await asyncio.to_thread(team_kpi.clear_override, int(norm_id), who)
        return web.json_response({"ok": True})
    try:
        target = int(payload.get("target"))
        assert 0 <= target <= 500
    except (TypeError, ValueError, AssertionError):
        raise web.HTTPBadRequest(text="target: число від 0 до 500 (0 — звільнена)")
    ok = await asyncio.to_thread(
        team_kpi.set_override, person, int(norm_id), who, target, payload.get("note")
    )
    if not ok:
        raise web.HTTPNotFound(text="Норми немає")
    return web.json_response({"ok": True})


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
        web.get("/api/bootstrap", api_bootstrap),
        web.post("/api/tasks", api_tasks_create),
        web.patch("/api/tasks/{task_id:\\d+}", api_tasks_patch),
        web.post("/api/themes", api_themes_create),
        web.patch("/api/themes/{theme_id:\\d+}", api_themes_patch),
        web.delete("/api/themes/{theme_id:\\d+}", api_themes_delete),
        web.put("/api/people/dept", api_people_dept),
        web.get("/api/kpi", api_kpi),
        web.post("/api/kpi/norms", api_kpi_norm_create),
        web.patch("/api/kpi/norms/{norm_id:\\d+}", api_kpi_norm_patch),
        web.delete("/api/kpi/norms/{norm_id:\\d+}", api_kpi_norm_delete),
        web.put("/api/kpi/override", api_kpi_override),
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
