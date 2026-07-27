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
import calendar
import gzip
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

from handlers import (
    bot_db, team_kpi, team_matches, team_projects, team_roster, team_tasks,
)
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


async def _in_session(fn, *args):
    """Виконує блокуючу роботу в потоці, тримаючи ОДНЕ з'єднання з Норою на
    весь виклик (bot_db.session). Без цього кожен query/execute відкривав своє:
    /api/kpi — 17 з'єднань, профіль людини — 27, при тому що сам SELECT коштує
    менше відсотка часу конекту."""
    def run():
        with bot_db.session():
            return fn(*args)

    return await asyncio.to_thread(run)


def _resolve_and_remember(tg_id, username):
    """Резолв людини + кеш tg_id — в одному потоці й одному з'єднанні."""
    person = team_roster.resolve_person(tg_id, username)
    if person:
        try:
            team_roster.remember_user(tg_id, username, person)
        except Exception as e:
            print(f"webapp: не вдалось закешувати tg_id для «{person}» — {e}")
    return person


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
    person = await _in_session(
        _resolve_and_remember, tg_user["id"], tg_user.get("username")
    )
    if not person:
        raise web.HTTPForbidden(
            text="Ця апка — для команди МикВісті. Якщо ти з редакції — напиши Олегу."
        )
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
        # Прогрес «2/3» і лінки зарахованих публікацій — одним запитом на
        # весь список (не по таску), інакше екран Каті вибив би сотню запитів
        team_matches.attach_progress(out["tasks"])
        out["nora"] = True
    except Exception as e:
        print(f"webapp: Нора недоступна — {e}")
        out["tasks"] = []

    if not is_manager:
        # Своє фото — для кільця KPI у шапці її екрана
        try:
            out["me_photo"] = (team_projects.avatar_map([person]).get(person) or {})
            out["site_db"] = team_projects.is_configured()
        except Exception as e:
            print(f"webapp: фото «{person}» не прочиталось — {e}")
            out["me_photo"] = {}
        # Журналістці — мінімум про проєкти (донор + лого) для рядків тасків
        try:
            out["projects"] = [
                {"id": p["id"], "name": p["name"], "partner": p["partner"],
                 "logo": p["logo"], "logo_orig": p["logo_orig"]}
                for p in team_projects.list_projects(False)
            ]
        except Exception as e:
            print(f"webapp: проєкти для журналістки не прочитались — {e}")
            out["projects"] = []
        return out

    names = [n for n, p in team_roster.ROSTER.items() if not p["manager"]]
    manager_names = [n for n, p in team_roster.ROSTER.items() if p["manager"]]
    depts = team_roster.dept_overrides() if out["nora"] else {}
    photos = {}
    projects = []
    try:
        # Керівництво теж із фото — воно показується в табі «Команда»
        photos = team_projects.avatar_map(
            names + manager_names, team_roster.SITE_USER_IDS
        )
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
            {"id": t["id"], "name": t["name"], "planned": t["planned"], "format": t["format"]}
        )
    deadlines_by_project = {}
    order = {}
    drive = {}
    if out["nora"]:
        try:
            for d in team_tasks.list_project_deadlines():
                deadlines_by_project.setdefault(d["project_id"], []).append(d)
            order = team_tasks.get_project_order()
            drive = team_tasks.get_drive_links()
        except Exception as e:
            print(f"webapp: порядок/дедлайни/drive проєктів не прочитались — {e}")
    for p in projects:
        p["themes"] = theme_by_project.get(p["id"], [])
        p["deadlines"] = deadlines_by_project.get(p["id"], [])
        p["drive_url"] = drive.get(p["id"])
    # Ручний порядок Каті; невпорядковані — після, у дефолтному порядку
    default_pos = {p["id"]: i for i, p in enumerate(projects)}
    projects.sort(key=lambda p: (order.get(p["id"], 10**9), default_pos[p["id"]]))

    out["people"] = [
        {
            "name": n,
            "dept": (dept := team_roster.effective_dept(n, depts)),
            "dept_title": team_roster.DEPT_TITLES.get(dept, ""),
            "photo": (photos.get(n) or {}).get("photo"),
            "photo_sm": (photos.get(n) or {}).get("photo_sm"),
            "photo_orig": (photos.get(n) or {}).get("photo_orig"),
        }
        for n in names
    ]
    # Керівництво — окремим списком: у people воно навмисно не входить (той
    # список для постановки тасків журналісткам і для KPI-норм)
    out["managers"] = [
        {
            "name": n,
            "role": team_roster.person_role(n) or "",
            "photo": (photos.get(n) or {}).get("photo"),
            "photo_sm": (photos.get(n) or {}).get("photo_sm"),
            "photo_orig": (photos.get(n) or {}).get("photo_orig"),
        }
        for n in manager_names
    ]
    out["projects"] = projects
    # Кандидати у відповідальні за звіти: ВЕСЬ ростер, включно з
    # адміністративними — Олена (фінанси), Катя (наративка), Олег. У people
    # їх немає навмисно: той список для постановки тасків журналісткам.
    out["assignees"] = [
        {"name": n,
         "dept_title": team_roster.DEPT_TITLES.get(
             team_roster.effective_dept(n, depts) or i["dept"], ""),
         "admin": bool(i["manager"])}
        for n, i in team_roster.ROSTER.items()
    ]
    return out


async def api_bootstrap(request):
    person, info, tg_user = await _authenticate(request)
    data = await _in_session(_bootstrap_blocking, person, info["manager"])
    data["me"] = _me_payload(person, info, tg_user)
    # Фото журналістки кладемо прямо в me — далі воно потрібне лише їй самій
    data["me"].update(data.pop("me_photo", None) or {})
    return web.json_response(data)


async def api_tasks_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)

    assignee = payload.get("person")
    if assignee not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    type_ = payload.get("type") or None
    if type_ is not None and type_ not in team_tasks.TASK_TYPES:
        raise web.HTTPBadRequest(text="type: news, article, post або порожньо (будь-який)")
    platform = payload.get("platform") or None
    if type_ == "post":
        if platform not in team_tasks.TASK_PLATFORMS:
            raise web.HTTPBadRequest(text="platform: telegram або instagram (для поста)")
    else:
        platform = None
    qty = payload.get("qty", 1)
    try:
        qty = max(1, min(99, int(qty)))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="qty: число")

    project_id = payload.get("project_id") or None
    project_name = None
    partner_name = None
    if project_id:
        projects = await asyncio.to_thread(team_projects.list_projects, False)
        match = next((p for p in projects if p["id"] == int(project_id)), None)
        if not match:
            raise web.HTTPBadRequest(text="Невідомий проєкт")
        project_name = match["name"]
        partner_name = match["partner"]

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
        theme_id, theme_name, qty, payload.get("note"), deadline, partner_name,
        platform,
    )
    # Пінг після відповіді — створення таска не має висіти на Telegram API
    asyncio.get_running_loop().create_task(
        team_tasks.ping_assigned(request.app["bot"], task)
    )
    return web.json_response({"task": task})


async def api_tasks_patch(request):
    """PATCH таска: {status} — зміна статусу; інакше редагування полів
    (qty, deadline, note, theme_id)."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    task_id = int(request.match_info["task_id"])

    if "status" in payload:
        status = payload.get("status")
        if status not in team_tasks.TASK_STATUSES:
            raise web.HTTPBadRequest(text="status: open, done або dropped")
        task = await _in_session(_set_status_blocking, task_id, person, status)
        if not task:
            raise web.HTTPNotFound(text="Таска немає")
        return web.json_response({"task": task})

    current = await asyncio.to_thread(team_tasks.get_task, task_id)
    if not current:
        raise web.HTTPNotFound(text="Таска немає")
    kwargs = {}
    if "qty" in payload:
        try:
            kwargs["qty"] = int(payload["qty"])
            assert 1 <= kwargs["qty"] <= 99
        except (TypeError, ValueError, AssertionError):
            raise web.HTTPBadRequest(text="qty: 1–99")
    if "deadline" in payload:
        deadline = payload.get("deadline") or None
        if deadline:
            try:
                time.strptime(deadline, "%Y-%m-%d")
            except (ValueError, TypeError):
                raise web.HTTPBadRequest(text="deadline: YYYY-MM-DD")
        kwargs["deadline"] = deadline
    if "note" in payload:
        kwargs["note"] = payload.get("note")
    if "theme_id" in payload:
        theme_id = payload.get("theme_id") or None
        theme_name = None
        if theme_id:
            themes = await asyncio.to_thread(team_tasks.list_themes)
            theme = next((t for t in themes if t["id"] == int(theme_id)), None)
            if not theme or theme["project_id"] != current["project_id"]:
                raise web.HTTPBadRequest(text="Тематика не з проєкту цього таска")
            theme_name = theme["name"]
        kwargs["theme_id"] = theme_id
        kwargs["theme_name"] = theme_name
    task = await _in_session(_update_task_blocking, task_id, person, kwargs)
    return web.json_response({"task": task})


def _set_status_blocking(task_id, person, status):
    """Статус руками + свіжий прогрес у відповідь (щоб картка одразу показала
    зараховані публікації, а не чекала наступного bootstrap)."""
    task = team_tasks.set_status(task_id, person, status)
    if task:
        team_matches.attach_progress([task])
    return task


def _update_task_blocking(task_id, person, kwargs):
    """Редагування полів. Після зміни qty статус має наздогнати прогрес в
    обидва боки: підняли кількість авто-закритому — він вертається у відкриті,
    знизили до вже зарахованого — закривається."""
    task = team_tasks.update_task_fields(task_id, person, **kwargs)
    if task and "qty" in kwargs:
        updated, _ = team_matches.recount_after_qty_change(task_id)
        task = updated or task
    if task:
        team_matches.attach_progress([task])
    return task


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


async def api_tasks_bulk(request):
    """Масова постановка з проєкту (Олег, 27.07): тематика (опційно) + місяць +
    {людина: скільки}. Кожній створюється таска з дедлайном на кінець місяця,
    тип — з формату тематики (news/article; інші формати → «будь-який»),
    кожній летить пінг."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)

    project_id = payload.get("project_id")
    if not project_id:
        raise web.HTTPBadRequest(text="Потрібен project_id")
    projects = await asyncio.to_thread(team_projects.list_projects, False)
    project = next((p for p in projects if p["id"] == int(project_id)), None)
    if not project:
        raise web.HTTPBadRequest(text="Невідомий проєкт")

    theme = None
    if payload.get("theme_id"):
        themes = await asyncio.to_thread(team_tasks.list_themes)
        theme = next((t for t in themes if t["id"] == int(payload["theme_id"])), None)
        if not theme or theme["project_id"] != int(project_id):
            raise web.HTTPBadRequest(text="Тематика не з цього проєкту")

    month = payload.get("month") or ""
    try:
        y, m = int(month[:4]), int(month[5:7])
        assert month[4] == "-" and 1 <= m <= 12 and 2020 <= y <= 2100
    except (ValueError, AssertionError, IndexError):
        raise web.HTTPBadRequest(text="month: YYYY-MM")
    # Дедлайн — останній день місяця
    deadline = f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise web.HTTPBadRequest(text="items: [{person, qty}]")
    for item in items:
        if item.get("person") not in team_roster.ROSTER:
            raise web.HTTPBadRequest(text=f"Невідома людина: {item.get('person')}")
        try:
            assert 1 <= int(item.get("qty")) <= 99
        except (TypeError, ValueError, AssertionError):
            raise web.HTTPBadRequest(text="qty: 1–99")

    type_ = theme["format"] if theme and theme["format"] in team_tasks.TASK_TYPES else None
    if type_ == "post":
        type_ = None  # платформа в масовій постановці не задається

    specs = [
        {
            "creator": person, "person": item["person"], "type_": type_,
            "project_id": project["id"], "project_name": project["name"],
            "theme_id": theme["id"] if theme else None,
            "theme_name": theme["name"] if theme else None,
            "qty": int(item["qty"]), "deadline": deadline,
            "partner_name": project["partner"],
        }
        for item in items
    ]
    # Уся пачка однією транзакцією й одним з'єднанням: це один намір Каті,
    # тож половина поставлених тасків гірша за жодного.
    created = await asyncio.to_thread(team_tasks.create_tasks_bulk, specs)
    # Пінги — тільки після коміту: інакше людина могла отримати «маєш завдання»
    # від Лиса за таску, яку відкотило.
    loop = asyncio.get_running_loop()
    for task in created:
        loop.create_task(team_tasks.ping_assigned(request.app["bot"], task))
    # Віддаємо самі таски, а не лише лічильник: апка домальовує їх у себе
    # локально, замість перечитувати весь /api/bootstrap.
    return web.json_response({"created": len(created), "tasks": created})


# ---------- Порядок проєктів (drag-n-drop) ----------

async def api_projects_order(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        raise web.HTTPBadRequest(text="ids: список id проєктів")
    await asyncio.to_thread(team_tasks.set_project_order, ids)
    return web.json_response({"ok": True})


async def api_project_drive(request):
    """Прикріпити/змінити/відкріпити папку Google Drive проєкту."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    url = normalize_https_url(payload.get("url"))
    if url and "://" in url and not url.startswith("https://"):
        raise web.HTTPBadRequest(text="Потрібен https-лінк")
    await asyncio.to_thread(
        team_tasks.set_drive_link, int(request.match_info["project_id"]), url, person
    )
    return web.json_response({"ok": True, "url": url or None})


# ---------- Дедлайни звітності проєктів ----------

def _validate_deadline(payload, require_all=True):
    kind = payload.get("kind")
    if require_all or kind is not None:
        if kind not in team_tasks.DEADLINE_KINDS:
            raise web.HTTPBadRequest(text="kind: narrative, financial або milestone")
    stage = payload.get("stage") or None
    if kind in ("narrative", "financial"):
        if stage not in team_tasks.DEADLINE_STAGES:
            raise web.HTTPBadRequest(text="stage: interim або final (для звітів)")
    else:
        stage = None
    title = (payload.get("title") or "").strip()
    if kind == "milestone" and not title:
        raise web.HTTPBadRequest(text="Майлстоуну потрібна назва")
    due = payload.get("due")
    if require_all or due is not None:
        try:
            time.strptime(due, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="due: YYYY-MM-DD")
    return kind, stage, title, due


async def api_deadline_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    project_id = payload.get("project_id")
    if not project_id:
        raise web.HTTPBadRequest(text="Потрібен project_id")
    kind, stage, title, due = _validate_deadline(payload)
    dl = await asyncio.to_thread(
        team_tasks.add_project_deadline, person, int(project_id), kind, due, stage, title
    )
    return web.json_response({"deadline": dl})


async def api_deadline_patch(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    kind, stage, title, due = _validate_deadline(payload)
    dl = await asyncio.to_thread(
        team_tasks.update_project_deadline, int(request.match_info["dl_id"]),
        kind, due, stage, title,
    )
    if not dl:
        raise web.HTTPNotFound(text="Дедлайну немає")
    return web.json_response({"deadline": dl})


async def api_deadline_assignee(request):
    """Хто пише цей звіт. {person: "ПІБ"} або {clear: true} — повернути дефолт
    за типом звіту (фінансовий — фінменеджерка, наративний — головредакторка)."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = None if payload.get("clear") else payload.get("person")
    if who is not None and who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    dl = await _in_session(
        team_tasks.set_deadline_assignee, int(request.match_info["dl_id"]), who
    )
    if not dl:
        raise web.HTTPNotFound(text="Дедлайну немає")
    return web.json_response({"deadline": dl})


async def api_deadline_status(request):
    """Рух звіту: {status: "submitted"|"accepted"} або {clear: true} —
    повернути в «очікується». Доступно менеджерам (Олег, Катя, Олена)."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    status = None if payload.get("clear") else payload.get("status")
    if status is not None and status not in team_tasks.DEADLINE_STATUSES:
        raise web.HTTPBadRequest(text="status: submitted, accepted або clear")
    dl = await _in_session(
        team_tasks.set_deadline_status, int(request.match_info["dl_id"]), status, person
    )
    if not dl:
        raise web.HTTPNotFound(text="Дедлайну немає")
    return web.json_response({"deadline": dl})


async def api_deadline_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await asyncio.to_thread(
        team_tasks.delete_project_deadline, int(request.match_info["dl_id"])
    )
    if not deleted:
        raise web.HTTPNotFound(text="Дедлайну немає")
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
    payload = await _in_session(
        team_kpi.kpi_payload, None if info["manager"] else person
    )
    return web.json_response(payload)


async def api_kpi_dashboard(request):
    """Звітний дашборд KPI за період з історією: ?period=week|month&offset=0.
    offset ≤ 0 (у майбутнє не листаємо). Тільки менеджери."""
    person, info, _ = await _require_manager(request)
    period = request.query.get("period", "week")
    if period not in team_kpi.KPI_PERIODS:
        period = "week"
    try:
        offset = min(0, int(request.query.get("offset", "0")))
        offset = max(-60, offset)  # розумна межа углиб історії
    except ValueError:
        offset = 0
    data = await _in_session(team_kpi.kpi_dashboard, period, offset)
    return web.json_response(data)


async def api_kpi_person(request):
    """Помісячна динаміка виконання KPI. Менеджер бачить будь-кого (профіль зі
    Звіту), журналістка — ТІЛЬКИ себе: параметр person у неї ігнорується, тож
    підмінити його і зазирнути в чужі цифри не вийде."""
    person, info, _ = await _authenticate(request)
    who = request.query.get("person") if info["manager"] else person
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    try:
        months = min(24, max(3, int(request.query.get("months", "12"))))
    except ValueError:
        months = 12
    data = await _in_session(team_kpi.kpi_person_history, who, months)
    if data is None:
        raise web.HTTPNotFound(text="Людину не знайдено")
    return web.json_response(data)


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
#
# Дві речі, яких бракувало (ревізія 27.07):
#
# 1. СТИСНЕННЯ. aiohttp сам НЕ гзіпить відповіді, але web.static віддає
#    ГОТОВИЙ файл-сусід «<ім'я>.gz», якщо той лежить поруч і клієнт прислав
#    Accept-Encoding: gzip. app.js + style.css — це ~101 КБ сирими і ~26 КБ
#    стиснутими, тобто вчетверо менше на кожен холодний старт апки в мобільній
#    мережі. Тому .gz генеруємо на старті процесу, без жодної нової залежності.
#
#    Пастка: aiohttp віддає .gz, НЕ звіряючи його свіжість з оригіналом. Стале
#    .gz гірше за його відсутність — користувач дістане старий JS проти нового
#    API. Тому спершу зносимо попередній .gz і лишаємо новий ТІЛЬКИ якщо запис
#    удався; не вдалось (ФС лише на читання) — працюємо без стиснення.
#
# 2. ВЕРСІОНУВАННЯ. index.html віддається з no-cache (перевалідовується
#    завжди), а на /static/ не було Cache-Control узагалі — тобто app.js і
#    style.css жили за евристичним кешем браузера і WebView Telegram, а
#    примусово оновитись у редакції способу не було. Тепер index.html посилає
#    на /static/app.js?v=<хеш вмісту>: змінився файл — змінився URL, і старий
#    JS проти нового API стає неможливим. Заразом версійований URL можна
#    кешувати вічно (immutable), тож повторні відкриття апки не тягнуть нічого.

_STATIC_ASSETS = ("app.js", "style.css")

_asset_versions = {}   # "app.js" -> "3f2a1c0b9d" (хеш вмісту, короткий)
_index_html = None     # відрендерений index.html із ?v=… (None — віддамо файл як є)


def _gz_matches(gz_path, raw):
    """Чи .gz поруч справді відповідає поточному файлу. Відсутній — теж «так»
    (просто віддамо сирим). Саме цю перевірку aiohttp не робить сам."""
    if not os.path.exists(gz_path):
        return True
    try:
        with open(gz_path, "rb") as f:
            return gzip.decompress(f.read()) == raw
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def _prepare_static():
    """Рахує версії ассетів, генерує .gz-сусідів і рендерить index.html.
    Викликається раз на старті процесу. Будь-який збій — не фатальний:
    апка просто працює як раніше (без стиснення / без версій)."""
    global _index_html

    for name in _STATIC_ASSETS:
        path = os.path.join(_STATIC_DIR, name)
        gz_path = path + ".gz"
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            print(f"webapp: не прочитав {name} — {e}")
            continue

        # Спершу знести старий .gz (стале стиснення небезпечніше за його брак)
        try:
            if os.path.exists(gz_path):
                os.remove(gz_path)
            with open(gz_path, "wb") as f:
                f.write(gzip.compress(raw, 9))
        except OSError as e:
            print(f"webapp: {name}.gz не оновився — {e}")

        # Версію (а з нею й вічний кеш) даємо ЛИШЕ якщо впевнені в тому, що
        # реально поїде до браузера. Інакше — без ?v=, тобто max-age=300:
        # тоді навіть у найгіршому разі розбіжність живе 5 хв, а не вічно.
        if _gz_matches(gz_path, raw):
            _asset_versions[name] = hashlib.sha256(raw).hexdigest()[:10]
            size = os.path.getsize(gz_path) if os.path.exists(gz_path) else len(raw)
            print(f"webapp: {name} {len(raw) // 1024} КБ → {size // 1024} КБ, "
                  f"версія {_asset_versions[name]}")
        else:
            print(f"webapp: ⚠️ {name}.gz не відповідає {name} і не оновлюється — "
                  f"віддаю без версії (короткий кеш), перевір права на теку webapp/")

    try:
        with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        for name, version in _asset_versions.items():
            html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
        _index_html = html
    except OSError as e:
        print(f"webapp: index.html не відрендерився — {e}; віддаю файл як є")


async def cache_headers_middleware(request, handler):
    """Cache-Control для статики. Версійований URL (?v=<хеш>) можна тримати
    вічно: зміна файла змінює URL. Без версії — коротко, щоб випадковий
    прямий запит не залипав."""
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable" if request.query.get("v")
            else "public, max-age=300",
        )
    return response


if web:  # без aiohttp модуль просто спить — декорувати нічого
    cache_headers_middleware = web.middleware(cache_headers_middleware)


async def index(request):
    if _index_html is None:  # не відрендерився — стара поведінка
        return web.FileResponse(
            os.path.join(_STATIC_DIR, "index.html"),
            headers={"Cache-Control": "no-cache"},
        )
    response = web.Response(
        text=_index_html, content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )
    response.enable_compression()
    return response


async def health(request):
    return web.Response(text="ok")


# ---------- Старт ----------

async def start_webapp(application):
    """Піднімає HTTP-сервер у тому ж event loop, що polling. Викликається з
    post_init. Без PORT (домен Railway ще не ввімкнено) — тихо спить."""
    if not is_configured():
        print("webapp: PORT/aiohttp не налаштовано — Mini App спить")
        return
    _prepare_static()
    app = web.Application(middlewares=[cache_headers_middleware])
    app["bot"] = application.bot
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/api/bootstrap", api_bootstrap),
        web.post("/api/tasks", api_tasks_create),
        web.post("/api/tasks/bulk", api_tasks_bulk),
        web.patch("/api/tasks/{task_id:\\d+}", api_tasks_patch),
        web.post("/api/themes", api_themes_create),
        web.patch("/api/themes/{theme_id:\\d+}", api_themes_patch),
        web.delete("/api/themes/{theme_id:\\d+}", api_themes_delete),
        web.put("/api/projects/order", api_projects_order),
        web.put("/api/projects/{project_id:\\d+}/drive", api_project_drive),
        web.post("/api/project_deadlines", api_deadline_create),
        web.patch("/api/project_deadlines/{dl_id:\\d+}", api_deadline_patch),
        web.put("/api/project_deadlines/{dl_id:\\d+}/assignee", api_deadline_assignee),
        web.put("/api/project_deadlines/{dl_id:\\d+}/status", api_deadline_status),
        web.delete("/api/project_deadlines/{dl_id:\\d+}", api_deadline_delete),
        web.put("/api/people/dept", api_people_dept),
        web.get("/api/kpi", api_kpi),
        web.get("/api/kpi/dashboard", api_kpi_dashboard),
        web.get("/api/kpi/person", api_kpi_person),
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
