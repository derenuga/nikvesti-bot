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
import re
import time
from datetime import datetime
from urllib.parse import parse_qsl

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — модуль просто "не налаштований"
    web = None

from handlers import (
    bot_db, card_maker, content_report, impact_archive, team_analytics,
    team_conditions, team_contacts, team_director, team_kpi, team_matches,
    team_notifications, team_publications, team_projects, team_reviews,
    team_roster, team_tasks, team_todos,
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


def _viewed(request, person):
    """Чиїми очима дивимось. Параметр `person` поважається лише в менеджера —
    журналістці чужий id нічого не дає, вона завжди бачить свій рядок."""
    who = request.query.get("person")
    if not who or who == person:
        return person
    info = team_roster.ROSTER.get(person) or {}
    return who if info.get("manager") and who in team_roster.ROSTER else person


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
        # Кажемо, ЩО саме показати Олегу. Резолв іде через username, а він у
        # Telegram міняється в один тап — і тоді людина з редакції впирається
        # в глухий текст, а причину доводиться шукати вручну (Таміла, 04.08:
        # бот у чаті відповідає, апка не пускає). З id у повідомленні це
        # лікується одним рядком у ростері.
        who = tg_user.get("username")
        raise web.HTTPForbidden(
            text="Ця апка — для команди МикВісті. Якщо ти з редакції — "
                 f"надішли Олегу цей рядок: id {tg_user['id']}"
                 + (f", @{who}" if who else ", username не заданий")
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

    out["unread"] = 0
    try:
        out["tasks"] = team_tasks.list_tasks(None if is_manager else person)
        # Прогрес «2/3» і лінки зарахованих публікацій — одним запитом на
        # весь список (не по таску), інакше екран Каті вибив би сотню запитів
        team_matches.attach_progress(out["tasks"])
        out["unread"] = team_notifications.unread_count(person, is_manager)
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
    out["pending_count"] = 0
    if out["nora"]:
        try:
            themes = team_tasks.list_themes()
        except Exception as e:
            print(f"webapp: тематики не прочитались — {e}")
        try:
            # Лічильник черги звірки — на пункті меню «Сповіщення»
            out["pending_count"] = team_matches.pending_count()
        except Exception as e:
            print(f"webapp: черга звірки не прочиталась — {e}")

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
    if "type" in payload:
        type_ = payload.get("type") or None
        if type_ is not None and type_ not in team_tasks.TASK_TYPES:
            raise web.HTTPBadRequest(text="type: news, article, post або порожньо")
        kwargs["type_"] = type_
        # Платформа має сенс лише для поста — інакше стираємо, щоб у рядку не
        # лишалось «стаття у Telegram»
        platform = payload.get("platform") or None
        if type_ == "post":
            if platform not in team_tasks.TASK_PLATFORMS:
                raise web.HTTPBadRequest(text="platform: telegram або instagram")
            kwargs["platform"] = platform
        else:
            kwargs["platform"] = None
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


def _task_card_blocking(task_id):
    task = team_tasks.get_task(task_id)
    if task:
        team_matches.attach_progress([task])
    return task


async def api_task_get(request):
    """Один таск із прогресом — для картки зі стрічки подій. bootstrap тримає
    лише свіже (закриті за останні 30 днів), а подія «виконано» у стрічці може
    вказувати на старіший таск — картка доганяє його цим запитом.
    Журналістці віддаються тільки її власні завдання."""
    person, info, _ = await _authenticate(request)
    task = await _in_session(_task_card_blocking, int(request.match_info["task_id"]))
    if not task:
        raise web.HTTPNotFound(text="Такого завдання вже немає")
    if not info["manager"] and task["person"] != person:
        raise web.HTTPForbidden(text="Це завдання іншої людини")
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


# ---------- Контент-звіт проєкту ----------

async def api_project_report_months(request):
    """Місяці для пікера контент-звіту з к-стю публікацій у кожному —
    апка одразу підсвічує, скільки зроблено."""
    person, info, _ = await _require_manager(request)
    project_id = int(request.match_info["project_id"])
    projects = await asyncio.to_thread(team_projects.list_projects, False)
    project = next((p for p in projects if p["id"] == project_id), None)
    if not project:
        raise web.HTTPNotFound(text="Проєкту немає")
    try:
        months = await asyncio.to_thread(
            content_report.month_options, project_id, project.get("start_date"))
    except Exception as e:
        raise web.HTTPBadRequest(text=f"БД сайту не відповіла: {e}")
    return web.json_response({"months": months})


async def api_project_report(request):
    """Запустити збір контент-звіту за місяць. Збір асинхронний (FB/TG —
    хвилини), файл .html прилітає в приват тому, хто натиснув."""
    person, info, tg_user = await _require_manager(request)
    payload = await _json(request)
    ym = (payload.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", ym) or not 1 <= int(ym[5:7]) <= 12:
        raise web.HTTPBadRequest(text="month: YYYY-MM")
    project_id = int(request.match_info["project_id"])
    projects = await asyncio.to_thread(team_projects.list_projects, False)
    project = next((p for p in projects if p["id"] == project_id), None)
    if not project:
        raise web.HTTPNotFound(text="Проєкту немає")
    started = content_report.start_report(
        request.app["bot"], tg_user["id"], project, ym)
    if not started:
        raise web.HTTPConflict(text="Цей звіт уже збирається — файл прийде в приват")
    return web.json_response({"ok": True, "status": "building"})


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


# ---------- Відсутності (відпустка / лікарняна / відрядження) ----------

async def api_absences(request):
    """Список відсутностей: ?person=… або всі."""
    person, info, _ = await _require_manager(request)
    who = request.query.get("person")
    data = await _in_session(team_kpi.list_absences, who or None)
    return web.json_response({"absences": data})


async def api_absence_create(request):
    """{person, start, end, kind?, note?} — «була у відпустці з 8 по 17».
    Цілі KPI перерахуються самі, пропорційно робочим дням."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = payload.get("person")
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    dates = []
    for key in ("start", "end"):
        try:
            dates.append(time.strftime("%Y-%m-%d", time.strptime(payload.get(key), "%Y-%m-%d")))
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text=f"{key}: YYYY-MM-DD")
    kind = payload.get("kind") or "vacation"
    if kind not in team_kpi.ABSENCE_KINDS:
        raise web.HTTPBadRequest(text="kind: vacation, sick або trip")
    absence = await _in_session(
        team_kpi.add_absence, person, who, dates[0], dates[1], kind, payload.get("note"))
    team_kpi._fact_cache.clear()      # цілі змінились — зведення протухло
    return web.json_response({"absence": absence})


async def api_rate(request):
    """{person, rate} — ПОСТІЙНА ставка людини у відсотках (Олег, 29.07).

    Не плутати з правкою цілі: правка живе один період, ставка діє завжди —
    саме тому «пів ставки» тепер не треба вбивати щомісяця заново."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = payload.get("person")
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    try:
        rate = int(payload.get("rate"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="rate: число 0…100")
    saved = await _in_session(team_kpi.set_rate, who, rate, person)
    return web.json_response({"rate": saved})


async def api_absence_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await _in_session(
        team_kpi.delete_absence, int(request.match_info["absence_id"]))
    if not deleted:
        raise web.HTTPNotFound(text="Запису немає")
    return web.json_response({"ok": True})


# ---------- Запит на відпустку (від людини) ----------

async def api_absence_request_create(request):
    """{start, end, kind?, note?} — журналістка сама пропонує дати, Каті це
    приходить на погодження (Олег, 29.07). До погодження цілі KPI НЕ чіпаємо:
    інакше норму можна було б знизити самим фактом прохання."""
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    dates = []
    for key in ("start", "end"):
        try:
            dates.append(time.strftime("%Y-%m-%d",
                                       time.strptime(payload.get(key), "%Y-%m-%d")))
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text=f"{key}: YYYY-MM-DD")
    kind = payload.get("kind") or "vacation"
    if kind not in team_kpi.ABSENCE_KINDS:
        raise web.HTTPBadRequest(text="kind: vacation, sick або trip")
    req = await _in_session(team_kpi.request_absence, person,
                            dates[0], dates[1], kind, payload.get("note"))
    team_notifications.notify_safe(
        "absence_request", f"{person} просить {req['title']}",
        body=f"{_ru_range(req['start'], req['end'])}"
             + (f" · {req['note']}" if req.get("note") else ""),
        object_type="absence_request", object_id=req["id"],
        dedup_key=f"absence_request:{req['id']}",
    )
    return web.json_response({"request": req})


def _ru_range(start, end):
    """«08.08 — 17.08» одним рядком для картки погодження."""
    fmt = lambda d: f"{d[8:10]}.{d[5:7]}"
    return fmt(start) if start == end else f"{fmt(start)} — {fmt(end)}"


async def api_absence_requests(request):
    """Черга погодження — менеджерам."""
    person, info, _ = await _require_manager(request)
    data = await _in_session(team_kpi.list_absence_requests, "pending")
    return web.json_response({"requests": data})


async def api_absence_request_decide(request):
    """{approve: true|false}. Погоджений запит СТВОРЮЄ відсутність — саме з
    цієї хвилини перераховуються цілі."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    approve = bool(payload.get("approve"))
    req = await _in_session(team_kpi.decide_absence_request,
                            int(request.match_info["request_id"]), person, approve)
    if not req:
        raise web.HTTPNotFound(text="Запиту немає")
    if approve:
        team_kpi._fact_cache.clear()   # цілі змінились — зведення протухло
    team_notifications.notify_safe(
        "absence_decided",
        f"{req['title'].capitalize()} {'погоджено' if approve else 'відхилено'}",
        audience=team_notifications.AUDIENCE_PERSON, person=req["person"],
        body=f"{_ru_range(req['start'], req['end'])} · {person}",
        object_type="absence_request", object_id=req["id"],
        dedup_key=f"absence_decided:{req['id']}",
    )
    return web.json_response({"request": req})


# ---------- Телефонна база редакції ----------

async def api_contacts(request):
    """Пошук по імені, посаді, темах, телефону. Доступна всій команді:
    базу наповнюють усі, і закривати її від тих, хто наповнює, безглуздо."""
    person, info, _ = await _authenticate(request)
    q = request.query.get("q") or None
    # ?only=mine — лише лічильники для підпису на дверях: тягнути заради них
    # усю базу немає сенсу (головна відкривається частіше, ніж сама база)
    only_mine = request.query.get("only") == "mine"
    try:
        items = [] if only_mine else await _in_session(team_contacts.list_contacts, q)
        mine = await _in_session(team_contacts.contributions, person)
    except Exception as e:
        print(f"webapp: база контактів недоступна — {e}")
        return web.json_response({"contacts": [], "mine": None, "nora": False})
    return web.json_response({"contacts": items, "mine": mine, "nora": True})


async def api_contact_lookup(request):
    """«Чик-чик» — підтягнути посаду з архіву (Олег, 29.07).

    Сутнісний шар нори вже знає, ким людина була в новинах: редакція має це
    знання, воно просто лежало не там, де його питають. Нічого не зберігаємо —
    лише пропонуємо, рішення за людиною."""
    person, info, _ = await _authenticate(request)
    name = request.query.get("name") or ""
    found = await _in_session(team_contacts.lookup_entity, name)
    return web.json_response({"candidates": found})


async def api_contact_create(request):
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    try:
        def run():
            return team_contacts.add_contact(
                person, payload.get("name"), payload.get("role"),
                payload.get("phone"), payload.get("telegram"),
                payload.get("email"), payload.get("tags"), payload.get("note"),
                phones=payload.get("phones"))
        contact = await asyncio.to_thread(lambda: _with_session(run))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    return web.json_response({"contact": contact})


async def api_contact_patch(request):
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    def run():
        return team_contacts.update_contact(
            int(request.match_info["contact_id"]), person, **payload)
    contact = await asyncio.to_thread(lambda: _with_session(run))
    if not contact:
        raise web.HTTPNotFound(text="Контакту немає")
    return web.json_response({"contact": contact})


async def api_contact_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await _in_session(
        team_contacts.delete_contact, int(request.match_info["contact_id"]))
    if not deleted:
        raise web.HTTPNotFound(text="Контакту немає")
    return web.json_response({"ok": True})


def _with_session(fn):
    with bot_db.session():
        return fn()


# ---------- Черга звірки (спірні матчі) ----------

def _pending_payload():
    """Черга + кандидати на перевизначення. Кандидати рахуються тут, а не на
    фронті: у апці немає закритих тасків інших людей і немає ліміту qty."""
    pending = team_matches.pending_matches()
    if not pending:
        return {"pending": []}
    pool = team_tasks.list_open_tasks()
    counted = team_matches.counted_by_task([t["id"] for t in pool])
    for m in pending:
        # Автор відомий (публікація сайту) — кандидати лише його. Автор
        # невідомий (пост Telegram: у каналі його немає ніде) — усі відкриті
        # таски проєкту, і Катя обирає заразом людину й тематику.
        m["options"] = [
            {"id": t["id"], "theme_id": t["theme_id"], "theme_name": t["theme_name"],
             "type": t["type"], "qty": t["qty"],
             "done_count": len(counted.get(t["id"], [])),
             "person": t["person"], "project_id": t["project_id"]}
            for t in pool
            if t["project_id"] == m["project_id"]
            and (m["person"] is None or t["person"] == m["person"])
        ]
    # Тематики проєкту — щоб зарахувати можна було і в ту, на яку завдання ще
    # не ставили. Інакше Катя впиралась у глухий кут: публікація очевидно
    # закриває «критичні інформаційні потреби», а в списку лише те, що хтось
    # колись завів. Завдання під таку тематику створюється на льоту.
    themes_by_project = {}
    for t in team_tasks.list_themes():
        themes_by_project.setdefault(t["project_id"], []).append(
            {"id": t["id"], "name": t["name"], "format": t["format"]})
    for m in pending:
        m["themes"] = themes_by_project.get(m["project_id"], [])
    return {"pending": pending}


async def api_matches_pending(request):
    """Черга звірки для «Сповіщень». Тільки менеджери — це редакторське
    рішення, кому й куди зараховувати."""
    person, info, _ = await _require_manager(request)
    data = await _in_session(_pending_payload)
    return web.json_response(data)


def _task_for_theme(actor, match, theme_id, person):
    """Заводить завдання під тематику, на яку його ще не ставили, і повертає
    його. Потрібне, коли публікація закриває тематику проєкту, якої немає в
    жодному відкритому завданні людини: без цього чергу нікуди розрулити.

    Завдання створюється мовчки (notify=False) — «тобі поставили завдання»
    за вчора зроблене тільки збиває з пантелику; «виконано» вона отримає."""
    theme = next((t for t in team_tasks.list_themes()
                  if t["id"] == int(theme_id)), None)
    if not theme or theme["project_id"] != match["project_id"]:
        return None
    # Снапшоти назв — з уже записаного матчу й проєктів CMS (як у звичайній
    # постановці): проєкт живе в чужій БД і може зникнути з вибірки
    project_name = partner_name = None
    try:
        project = next((p for p in team_projects.list_projects(False)
                        if p["id"] == match["project_id"]), None)
        if project:
            project_name, partner_name = project["name"], project["partner"]
    except Exception as e:
        print(f"webapp: проєкт для нового завдання не прочитався — {e}")
    today = datetime.now(team_tasks.KYIV_TZ)
    last_day = calendar.monthrange(today.year, today.month)[1]
    # Тип беремо з формату тематики, а якщо його не задано — з САМОЇ публікації,
    # яку зараз зараховуємо. Інакше завдання, заведене з черги під новину,
    # називалось безликим «матеріал», хоча тип відомий достеменно.
    type_ = theme["format"] if theme["format"] in ("news", "article") else None
    if not type_ and match.get("node_type") in ("news", "article"):
        type_ = match["node_type"]
    return team_tasks.create_task(
        creator=actor, person=person, type_=type_,
        project_id=match["project_id"], project_name=project_name,
        theme_id=theme["id"], theme_name=theme["name"], qty=1,
        note="Заведено із черги звірки",
        deadline=f"{today.year:04d}-{today.month:02d}-{last_day:02d}",
        partner_name=partner_name, notify=False)


def _decide_blocking(match_id, actor, action, task_id, theme_id=None, person=None):
    if action == "requeue":
        match, touched = team_matches.requeue_match(match_id, actor)
        if not match:
            return None
        tasks = []
        for tid in touched:
            task, _ = team_matches.apply_progress(tid, actor)
            if task:
                tasks.append(task)
        team_matches.attach_progress(tasks)
        return {"match": match, "tasks": tasks,
                "pending_count": team_matches.pending_count()}

    """Рішення + перерахунок прогресу обох зачеплених тасків в одній сесії.

    Виконавицю беремо з обраного таска, а не з тіла запиту: для постів
    Telegram автор невідомий, і саме вибір Каті («кому зарахувати») робить
    його відомим — довіряти тут клієнту нема потреби."""
    if action == "confirm" and not task_id and theme_id:
        match = team_matches.get_match(match_id)
        who = person or (match or {}).get("person")
        if not match or who not in team_roster.ROSTER:
            return None
        task = _task_for_theme(actor, match, theme_id, who)
        if not task:
            return None
        task_id = task["id"]
    person = None
    if action == "confirm" and task_id:
        task = team_tasks.get_task(task_id)
        if not task:
            return None
        person = task["person"]
    match, touched = team_matches.decide_match(match_id, actor, action,
                                               task_id=task_id, person=person)
    if not match:
        return None
    tasks = []
    for tid in touched:
        task, _ = team_matches.apply_progress(tid, actor)
        if task:
            tasks.append(task)
    team_matches.attach_progress(tasks)
    return {"match": match, "tasks": tasks, "pending_count": team_matches.pending_count()}


async def api_matches_queue(request):
    """POST /api/matches/queue {url} — покласти публікацію в чергу руками.
    Для матеріалів, яких прогін не побачив (не той автор у CMS, поза
    проєктом, старіші за вікно) — редактор кидає лінк і вирішує по них там
    само, де й по решті."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    result = await _in_session(
        _queue_blocking, payload.get("url"), person, bool(payload.get("force")))
    if isinstance(result, str):
        raise web.HTTPBadRequest(text=result)
    return web.json_response(result)


def _queue_blocking(url, actor, force=False):
    from handlers import team_matching

    match, touched = team_matching.queue_publication(url, actor, force)
    if not match:
        # dict — не помилка, а «вже зараховано, підтвердь зняття»
        if isinstance(touched, dict):
            return touched
        return touched if isinstance(touched, str) else "Не вдалося додати"
    tasks = []
    for tid in touched:
        updated, _ = team_matches.apply_progress(tid, actor)
        if updated:
            tasks.append(updated)
    team_matches.attach_progress(tasks)
    return {"match": match, "tasks": tasks,
            "pending_count": team_matches.pending_count()}


async def api_matches_decide(request):
    """{action: confirm|reject|requeue, task_id?|theme_id?} — «зарахувати»
    (у наявне завдання чи в тематику, під яку його заведе сервер), «не те»
    або «повернути в чергу».

    Різниця між reject і requeue принципова: reject — остаточне «це не
    проєктне», публікація більше нікуди не повернеться; requeue знімає
    зарахування і кладе питання назад Каті (саме це робить зняття галочки в
    картці таска). Запис лишається в обох випадках, тож суддя ту саму
    публікацію повторно не чіпає."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    action = payload.get("action")
    if action not in ("confirm", "reject", "requeue"):
        raise web.HTTPBadRequest(text="action: confirm, reject або requeue")
    task_id = payload.get("task_id")
    theme_id = payload.get("theme_id")
    for name, value in (("task_id", task_id), ("theme_id", theme_id)):
        if action == "confirm" and value is not None:
            try:
                int(value)
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text=f"{name}: число")
    who = payload.get("person")
    if who is not None and who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    result = await _in_session(
        _decide_blocking, int(request.match_info["match_id"]), person, action,
        int(task_id) if action == "confirm" and task_id else None,
        int(theme_id) if action == "confirm" and theme_id else None,
        who,
    )
    if not result:
        raise web.HTTPNotFound(text="Матчу немає (або нема куди зараховувати)")
    return web.json_response(result)


async def api_task_attach(request):
    """POST /api/tasks/{id}/attach {url} — зарахувати публікацію в завдання
    руками. Перенос із задубльованого завдання і «оце сюди» без чекання
    прогону — одна й та сама операція."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    task_id = int(request.match_info["task_id"])
    result = await _in_session(_attach_blocking, task_id, payload.get("url"), person)
    if isinstance(result, str):
        raise web.HTTPBadRequest(text=result)
    if not result:
        raise web.HTTPNotFound(text="Завдання не знайдено")
    return web.json_response(result)


def _attach_blocking(task_id, url, actor):
    from handlers import team_matching

    task = team_tasks.get_task(task_id)
    if not task:
        return None
    match, touched = team_matching.attach_publication(task, url, actor)
    if not match:
        return touched if isinstance(touched, str) else "Не вдалося зарахувати"
    tasks = []
    for tid in touched:
        updated, _ = team_matches.apply_progress(tid, actor)
        if updated:
            tasks.append(updated)
    team_matches.attach_progress(tasks)
    return {"match": match, "tasks": tasks,
            "pending_count": team_matches.pending_count()}


# ---------- Стрічка сповіщень ----------

async def api_notifications(request):
    """Стрічка подій за роллю: свої персональні + менеджерські для керівництва.
    Доступна всім — журналістці теж (її завдання і зарахування)."""
    person, info, _ = await _authenticate(request)
    data = await _in_session(
        lambda: {"items": team_notifications.feed(person, info["manager"]),
                 "unread": team_notifications.unread_count(person, info["manager"])}
    )
    return web.json_response(data)


async def api_notifications_read(request):
    """{ids: [...]} або {all: true} — позначити прочитаним."""
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    ids = None if payload.get("all") else payload.get("ids") or []
    if ids is not None and not isinstance(ids, list):
        raise web.HTTPBadRequest(text="ids: список")
    unread = await _in_session(_mark_read_blocking, person, info["manager"], ids)
    return web.json_response({"ok": True, "unread": unread})


def _mark_read_blocking(person, is_manager, ids):
    team_notifications.mark_read(person, is_manager, ids)
    return team_notifications.unread_count(person, is_manager)


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


# ---------- Аналітика ----------

async def api_dashboard(request):
    """Аналітичний дашборд Головної: ?period=week|month&offset=0|-1…

    Дефолт offset=-1 — ПОПЕРЕДНІЙ період: у вівторок поточний тиждень має два
    дні даних і сам собою нічого не каже. Тільки менеджери: це цифри всієї
    редакції разом із виходом по авторах."""
    person, info, _ = await _require_manager(request)
    period = request.query.get("period", "week")
    if period not in team_analytics.PERIODS:
        period = "week"
    try:
        offset = int(request.query.get("offset", "-1"))
    except ValueError:
        offset = -1
    data = await _in_session(team_analytics.dashboard, period, offset)
    return web.json_response(data)


# ---------- KPI ----------

async def api_kpi(request):
    """Зведення KPI: менеджер — всі норми з людьми і прогресом; журналістка —
    норми свого відділу зі своїм рядком (для «Мої KPI»)."""
    person, info, _ = await _authenticate(request)
    # ?person= — менеджерський перегляд екрана конкретної журналістки («так
    # дебажити легше», Олег 29.07). Тільки для менеджера: журналістці чужий
    # параметр нічого не дає, вона завжди бачить свій рядок.
    who = request.query.get("person")
    if info["manager"]:
        target = who if who in team_roster.ROSTER else None
    else:
        target = person
    payload = await _in_session(team_kpi.kpi_payload, target)
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


async def api_publications(request):
    """«Мої публікації» за місяць. Та сама межа, що в історії KPI: менеджер
    може подивитись будь-кого (перегляд чужими очима), журналістка — тільки
    себе, тож підмінити person і зазирнути в чужий вихід не вийде."""
    person, info, _ = await _authenticate(request)
    who = request.query.get("person") if info["manager"] else person
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    try:
        offset = max(-24, min(0, int(request.query.get("offset", "0"))))
    except ValueError:
        offset = 0
    data = await _in_session(team_publications.my_publications, who, offset)
    return web.json_response(data)


# ---------- Кабінет директора: перегляди умов ----------
#
# Менеджерське через _require_manager, тобто це бачать Олег, Катя й Олена.
# ⚠️ Якщо кабінет має бути тільки Олегів — це один рядок (звірка person із
# конкретним іменем), але вирішувати за Олега я не став: Олена фінансова
# менеджерка, і закрити їй доступ мовчки було б не безпечніше, а незручніше.

async def api_conditions(request):
    person, info, _ = await _require_manager(request)
    return web.json_response(await _in_session(team_conditions.payload))


async def api_director_card(request):
    """Картка перегляду на людину: ?person=…

    Збірка тягне і Нору, і БД сайту, тож усе в одній сесії й одному потоці —
    інакше на кожен блок відкривалось би своє з'єднання."""
    person, info, _ = await _require_manager(request)
    who = request.query.get("person")
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    data = await _in_session(team_director.card, who)
    if not data:
        raise web.HTTPNotFound(text="Людину не знайдено")
    return web.json_response(data)


async def api_condition_create(request):
    """Зафіксувати перегляд. Тіло: person, date (опційно — засів історії
    минулою датою), note. Сум і типу рішення тут немає за задумом."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = payload.get("person")
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    saved = await _in_session(
        team_conditions.add_review, who, person,
        payload.get("date") or None, payload.get("note"))
    if not saved:
        raise web.HTTPBadRequest(text="Не вдалось зберегти — перевір дату")
    return web.json_response({"review": saved})


async def api_condition_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await _in_session(
        team_conditions.delete_review, int(request.match_info["review_id"]))
    if not deleted:
        raise web.HTTPNotFound(text="Запису немає")
    return web.json_response({"ok": True})


# ---------- Квартальна оцінка редактора ----------
#
# Менеджерське й ТІЛЬКИ менеджерське: журналістка своєї оцінки не бачить —
# і це не приховування, а умова, за якої оцінку взагалі можна ставити чесно
# (спека COMPENSATION_ADVISOR.md, §«Оцінку і гроші розводять у часі»).
# Що з оцінки казати людині — вирішує редакторка в розмові, а не екран.

async def api_reviews(request):
    """Зведення за квартал: ?offset=0 (поточний), -1 (попередній) …

    Глибина обмежена вісьмома кварталами назад: це два роки, далі оцінки
    просто немає, а від'ємний offset без межі дозволив би згенерувати запит
    за 1900 рік."""
    person, info, _ = await _require_manager(request)
    try:
        offset = max(-8, min(0, int(request.query.get("offset", "0"))))
    except ValueError:
        offset = 0
    data = await _in_session(team_reviews.review_payload, offset)
    return web.json_response(data)


async def api_review_save(request):
    """Зберегти оцінку однієї людини. Тіло: person, offset, чипи вимірів,
    note, або clear=true — зняти оцінку цілком.

    Виміри, яких у тілі немає, НЕ чіпаються: екран шле по одному чипу на тап,
    і повний перезапис стирав би сусідні відповіді."""
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    who = payload.get("person")
    if who not in team_roster.ROSTER or team_roster.ROSTER[who]["manager"]:
        raise web.HTTPBadRequest(text="Невідома людина")
    try:
        offset = max(-8, min(0, int(payload.get("offset", 0))))
    except (TypeError, ValueError):
        offset = 0

    if payload.get("clear"):
        await _in_session(team_reviews.clear_review, who, offset)
        return web.json_response({"cleared": True})

    dims = {k: payload[k] for k in team_reviews.DIMENSION_KEYS if k in payload}
    note = payload.get("note")
    saved = await _in_session(
        lambda: team_reviews.save_review(
            who, person, offset, note if isinstance(note, str) else None, **dims))
    if not saved:
        raise web.HTTPBadRequest(text="Нічого зберігати")
    return web.json_response({"review": saved})


# ---------- Блокнот (особистий список справ) ----------
#
# Приватний повністю: person береться з initData і в кожному запиті йде у
# WHERE. Менеджерського «подивитись чужий блокнот» немає й не буде — інакше це
# вже не блокнот, а звітність, і в нього перестануть писати.

async def api_todos(request):
    person, info, _ = await _authenticate(request)
    return web.json_response(await _in_session(team_todos.list_todos, person))


async def api_todo_create(request):
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    bucket = payload.get("bucket") if payload.get("bucket") in team_todos.BUCKETS else "today"
    item = await _in_session(
        team_todos.add_todo, person, payload.get("text") or "", bucket, "app")
    if not item:
        raise web.HTTPBadRequest(text="Порожній запис")
    return web.json_response({"todo": item})


async def api_todo_patch(request):
    person, info, _ = await _authenticate(request)
    payload = await _json(request)
    bucket = payload.get("bucket") if payload.get("bucket") in team_todos.BUCKETS else None
    done = payload.get("done")
    item = await _in_session(
        team_todos.update_todo, person, int(request.match_info["todo_id"]),
        bucket, done if isinstance(done, bool) else None, payload.get("text"))
    if not item:
        raise web.HTTPNotFound(text="Запису немає")
    return web.json_response({"todo": item})


async def api_todo_delete(request):
    person, info, _ = await _authenticate(request)
    deleted = await _in_session(
        team_todos.delete_todo, person, int(request.match_info["todo_id"]))
    if not deleted:
        raise web.HTTPNotFound(text="Запису немає")
    return web.json_response({"ok": True})


# ---------- Банк тем ----------
#
# Читає ВЖЕ зібране: черга, картка з ланцюгом і окремо дублі. Нора тут єдина
# БД (сайтова не потрібна), тож блок деградує сам по собі — без нори екран
# скаже «банк порожній», а не завалить решту апки.
#
# Дії розкладені за роллю рівно так, як у §6: «Перевірили» доступне обом,
# злиття дублів і «не наша тема» — менеджерські (обидві незворотні на вигляд,
# хоч і лежать у журналі), «Взяти в роботу» — журналістський шлях через
# погодження.

async def api_promises(request):
    person, _, _ = await _authenticate(request)
    from handlers import promise_app, team_kpi

    cls = request.query.get("cls") or None
    q = (request.query.get("q") or "").strip() or None
    try:
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        offset = 0
    # users.id людини — щоб порахувати «з моїх новин». Резолв кешований
    # (10 хв), а без БД сайту просто немає фасета, і решта екрана живе.
    #
    # У менеджерському перегляді чужими очима рахуємо ТОГО, на кого дивимось:
    # інакше фасет мовчки показував свої новини під чужим іменем — тобто
    # екран виглядав робочим і брехав («з моїх тем: 0», хоч там не нуль).
    #
    # Беремо ВСІ акаунти людини, а не пін: у `users` сайту та сама людина
    # буває двічі (Юлія Бойченко: 38 і 44), і матеріали розкладені між ними
    # по роках. Пін лікує НОРМУ, де роздвоєний рахунок неприпустимий, а тут
    # фільтр — сховати своє гірше, ніж показати зайве.
    viewed = _viewed(request, person)
    author_id = []
    try:
        author_id = await asyncio.to_thread(team_kpi.resolve_site_user_ids, viewed)
    except Exception as e:
        print(f"webapp: не резолвнув автора «{viewed}» — {e}")
    data = await asyncio.to_thread(promise_app.queue, cls, q, offset,
                                   promise_app.PAGE, None, author_id, viewed)
    # Чиї новини порахував фасет — щоб нуль було чим пояснити: у перегляді
    # чужими очима він має три різні причини, і вгадувати по одному фіксу за
    # раунд найдорожче.
    data["mine_of"] = {"person": viewed, "ids": author_id,
                       "preview": viewed != person}
    return web.json_response(data)


async def api_promises_mine_count(request):
    """Число для дверей журналістки. Окремий легкий ендпойнт, а не повна
    черга: головна вантажиться і без того довго, а тут потрібні два числа."""
    person, _, _ = await _authenticate(request)
    from handlers import promise_app, team_kpi

    try:
        author_id = await asyncio.to_thread(
            team_kpi.resolve_site_user_ids, _viewed(request, person))
    except Exception as e:
        print(f"webapp: не резолвнув автора «{person}» — {e}")
        return web.json_response({"total": 0, "overdue": 0})
    return web.json_response(
        await asyncio.to_thread(promise_app.mine_count, author_id))


async def api_promise_card(request):
    person, _, _ = await _authenticate(request)
    from handlers import promise_app

    data = await asyncio.to_thread(promise_app.card,
                                   int(request.match_info["cid"]),
                                   _viewed(request, person))
    if not data:
        raise web.HTTPNotFound(text="Обіцянки немає")
    return web.json_response(data)


async def api_promise_check(request):
    """«Перевірили» — і ЧИМ скінчилось. Висновок людини це продукт банку, а
    не службова позначка: обіцянка, перевірена й зірвана, лишається фактом,
    на який посилаються в наступному тексті."""
    person, _, _ = await _authenticate(request)
    from handlers import promise_app
    import promise_pipeline as pp

    payload = await _json(request) if request.can_read_body else {}
    outcome = (payload or {}).get("outcome")
    if outcome not in pp.CHECK_OUTCOMES:
        outcome = None
    # Черга шикується ТЕМАМИ, тож відповідь мусить уміти лягти на тему цілком —
    # інакше закрита людиною справа повертається в чергу наступним рядком.
    scope = "topic" if (payload or {}).get("scope") == "topic" else "one"
    n = await asyncio.to_thread(
        promise_app.check, int(request.match_info["cid"]), person, outcome,
        ((payload or {}).get("note") or "").strip() or None, scope)
    if not n:
        raise web.HTTPNotFound(text="Обіцянки немає")
    return web.json_response({"ok": True, "outcome": outcome, "count": n})


async def api_promise_star(request):
    """Зірочка «веду цю тему»: фасет «Обрані» + кожна нова ревізія прилітає
    в стрічку й у приват. Особиста, тож у перегляді чужими очима ставиться
    ТОМУ, на кого дивляться, — інакше менеджер тихо підписував би себе."""
    person, _, _ = await _authenticate(request)
    from handlers import promise_app

    payload = await _json(request) if request.can_read_body else {}
    on = bool((payload or {}).get("on", True))
    await asyncio.to_thread(promise_app.set_star,
                            int(request.match_info["cid"]),
                            _viewed(request, person), on)
    return web.json_response({"ok": True, "starred": on})


async def api_promise_take(request):
    person, _, _ = await _authenticate(request)
    from handlers import promise_app

    data = await asyncio.to_thread(
        promise_app.take, int(request.match_info["cid"]), person)
    if not data:
        raise web.HTTPNotFound(text="Обіцянки немає")
    return web.json_response(data)


async def api_promise_drop(request):
    person, _, _ = await _require_manager(request)
    from handlers import promise_app

    payload = await _json(request)
    data = await asyncio.to_thread(
        promise_app.drop, int(request.match_info["cid"]), person,
        (payload.get("reason") or "").strip() or None)
    if not data:
        raise web.HTTPNotFound(text="Обіцянки немає")
    return web.json_response({"ok": True, "purge_id": data["purge_id"]})


async def api_promise_dupes(request):
    await _authenticate(request)
    from handlers import promise_app

    return web.json_response(await asyncio.to_thread(promise_app.dupes))


async def api_promise_not_dupe(request):
    person, _, _ = await _require_manager(request)
    from handlers import promise_app

    payload = await _json(request)
    try:
        other = int(payload.get("other_id"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Немає other_id")
    await asyncio.to_thread(
        promise_app.not_dupe, int(request.match_info["cid"]), other, person)
    return web.json_response({"ok": True})


async def api_promise_merge(request):
    person, _, _ = await _require_manager(request)
    from handlers import promise_app

    payload = await _json(request)
    try:
        dup_id = int(payload.get("dup_id"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Немає dup_id")
    data = await asyncio.to_thread(
        promise_app.merge, int(request.match_info["cid"]), dup_id, person)
    if not data:
        raise web.HTTPNotFound(text="Однієї з обіцянок немає")
    return web.json_response({"ok": True, **data})


# ---------- Імпакт-архів ----------
#
# Журнал впливу редакції для донорських звітів (Олег, 29.07: «мені постійно
# важко шукати і наново описувати імпакти при кожній заявці по грант»).
# Менеджерський інструмент: кидаєш URL новини-фіксації — бот сам збирає серію,
# ключовий текст, донора ключового тексту і медальки. Збір асинхронний
# (створили → building → апка полить), бо беклінки + нора + Sonnet — це
# 15-30 секунд, і HTTP-запит апки стільки висіти не має.

async def api_impacts(request):
    person, info, _ = await _require_manager(request)
    return web.json_response({"impacts": await _in_session(impact_archive.list_impacts)})


async def api_impact_get(request):
    """Менеджер читає будь-який кейс; журналістка — лише ті, де має медальку
    (блок «Мої імпакти»). Чужі кейси їй не віддаємо — там чернетки і чужі
    оцінки внесків."""
    person, info, _ = await _authenticate(request)
    impact_id = int(request.match_info["impact_id"])
    if not info["manager"]:
        allowed = await _in_session(impact_archive.person_credited, impact_id, person)
        if not allowed:
            raise web.HTTPForbidden(text="Це не твій кейс")
    imp = await _in_session(impact_archive.get_impact, impact_id)
    if not imp:
        raise web.HTTPNotFound(text="Кейсу немає")
    return web.json_response(imp)


async def api_impacts_mine(request):
    """«Мої імпакти» — кейси з медалькою людини. Журналістка бачить тільки
    себе; менеджер може передати person (перегляд чужими очима)."""
    person, info, _ = await _authenticate(request)
    who = request.query.get("person") if info["manager"] else person
    who = who or person
    if who not in team_roster.ROSTER:
        raise web.HTTPBadRequest(text="Невідома людина")
    return web.json_response(
        {"impacts": await _in_session(impact_archive.list_impacts_for, who)})


async def api_impact_create(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    url = (payload.get("url") or "").strip()
    if "nikvesti.com" not in url:
        raise web.HTTPBadRequest(text="Потрібен лінк на матеріал nikvesti.com")
    impact_id = await _in_session(
        impact_archive.create_impact, person, url, payload.get("essence"))
    # збір — у фоні того самого event loop; свій try/except усередині
    asyncio.create_task(impact_archive.build_impact(impact_id))
    return web.json_response({"id": impact_id, "status": "building"})


async def api_impact_retry(request):
    person, info, _ = await _require_manager(request)
    impact_id = int(request.match_info["impact_id"])
    await _in_session(impact_archive.retry_impact, impact_id)
    asyncio.create_task(impact_archive.build_impact(impact_id))
    return web.json_response({"id": impact_id, "status": "building"})


async def api_impact_patch(request):
    person, info, _ = await _require_manager(request)
    payload = await _json(request)
    impact_id = int(request.match_info["impact_id"])
    action = payload.get("action")
    if action == "add_article":
        ok, err = await _in_session(
            impact_archive.add_article, impact_id, payload.get("url"))
        if not ok:
            raise web.HTTPBadRequest(text=err or "Не вдалось додати")
    elif action == "set_key":
        await _in_session(impact_archive.set_key_article, impact_id, int(payload.get("row_id")))
    elif action == "remove_article":
        await _in_session(impact_archive.remove_article, impact_id, int(payload.get("row_id")))
    elif action == "add_credit":
        await _in_session(impact_archive.add_credit, impact_id,
                          payload.get("person"), payload.get("note"))
    elif action == "remove_credit":
        await _in_session(impact_archive.remove_credit, impact_id, int(payload.get("credit_id")))
    else:
        imp = await _in_session(
            impact_archive.update_impact, impact_id, payload.get("title"),
            payload.get("essence"), payload.get("what_happened"), payload.get("significance"))
        if not imp:
            raise web.HTTPNotFound(text="Кейсу немає")
        return web.json_response(imp)
    imp = await _in_session(impact_archive.get_impact, impact_id)
    return web.json_response(imp)


async def api_impact_delete(request):
    person, info, _ = await _require_manager(request)
    deleted = await _in_session(
        impact_archive.delete_impact, int(request.match_info["impact_id"]))
    if not deleted:
        raise web.HTTPNotFound(text="Кейсу немає")
    return web.json_response({"ok": True})


async def api_impact_send(request):
    """Готовий кейс файлом .html у приват тому, хто натиснув: з файлу донор
    отримує його як є, а браузер друкує в PDF. Через бота, а не download у
    вебвʼю — там збереження файлів працює через раз."""
    person, info, tg_user = await _require_manager(request)
    impact_id = int(request.match_info["impact_id"])
    fname, html = await _in_session(impact_archive.export_html, impact_id)
    if not html:
        raise web.HTTPBadRequest(text="Кейс ще не зібрано")
    from io import BytesIO
    buf = BytesIO(html.encode("utf-8"))
    buf.name = fname
    try:
        await request.app["bot"].send_document(
            chat_id=tg_user["id"], document=buf,
            caption="🦊 Імпакт-кейс. Відкривається в браузері, звідти — у PDF.")
    except Exception as e:
        raise web.HTTPBadRequest(text=f"Не долетіло в приват: {e}")
    return web.json_response({"ok": True})


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
        web.get("/api/tasks/{task_id:\\d+}", api_task_get),
        web.patch("/api/tasks/{task_id:\\d+}", api_tasks_patch),
        web.post("/api/themes", api_themes_create),
        web.patch("/api/themes/{theme_id:\\d+}", api_themes_patch),
        web.delete("/api/themes/{theme_id:\\d+}", api_themes_delete),
        web.put("/api/projects/order", api_projects_order),
        web.put("/api/projects/{project_id:\\d+}/drive", api_project_drive),
        web.get("/api/projects/{project_id:\\d+}/report_months", api_project_report_months),
        web.post("/api/projects/{project_id:\\d+}/report", api_project_report),
        web.post("/api/project_deadlines", api_deadline_create),
        web.patch("/api/project_deadlines/{dl_id:\\d+}", api_deadline_patch),
        web.put("/api/project_deadlines/{dl_id:\\d+}/assignee", api_deadline_assignee),
        web.put("/api/project_deadlines/{dl_id:\\d+}/status", api_deadline_status),
        web.delete("/api/project_deadlines/{dl_id:\\d+}", api_deadline_delete),
        web.put("/api/people/dept", api_people_dept),
        web.get("/api/matches/pending", api_matches_pending),
        web.get("/api/notifications", api_notifications),
        web.post("/api/notifications/read", api_notifications_read),
        web.post("/api/matches/queue", api_matches_queue),
        web.post("/api/matches/{match_id:\\d+}/decide", api_matches_decide),
        web.post("/api/tasks/{task_id:\\d+}/attach", api_task_attach),
        web.get("/api/absences", api_absences),
        web.post("/api/absences", api_absence_create),
        web.delete("/api/absences/{absence_id:\\d+}", api_absence_delete),
        web.get("/api/contacts", api_contacts),
        web.get("/api/contacts/lookup", api_contact_lookup),
        web.post("/api/contacts", api_contact_create),
        web.patch("/api/contacts/{contact_id:\\d+}", api_contact_patch),
        web.delete("/api/contacts/{contact_id:\\d+}", api_contact_delete),
        web.put("/api/rate", api_rate),
        web.post("/api/absences/request", api_absence_request_create),
        web.get("/api/absences/requests", api_absence_requests),
        web.post("/api/absences/requests/{request_id:\\d+}/decide",
                 api_absence_request_decide),
        web.get("/api/dashboard", api_dashboard),
        web.get("/api/kpi", api_kpi),
        web.get("/api/kpi/dashboard", api_kpi_dashboard),
        web.get("/api/kpi/person", api_kpi_person),
        web.get("/api/publications", api_publications),
        web.get("/api/impacts", api_impacts),
        web.get("/api/impacts/mine", api_impacts_mine),
        web.post("/api/impacts", api_impact_create),
        web.get("/api/impacts/{impact_id:\\d+}", api_impact_get),
        web.patch("/api/impacts/{impact_id:\\d+}", api_impact_patch),
        web.delete("/api/impacts/{impact_id:\\d+}", api_impact_delete),
        web.post("/api/impacts/{impact_id:\\d+}/retry", api_impact_retry),
        web.post("/api/impacts/{impact_id:\\d+}/send", api_impact_send),
        web.get("/api/reviews", api_reviews),
        web.put("/api/reviews", api_review_save),
        web.get("/api/conditions", api_conditions),
        web.get("/api/conditions/card", api_director_card),
        web.post("/api/conditions", api_condition_create),
        web.delete("/api/conditions/{review_id:\\d+}", api_condition_delete),
        web.get("/api/todos", api_todos),
        web.post("/api/todos", api_todo_create),
        web.patch("/api/todos/{todo_id:\\d+}", api_todo_patch),
        web.delete("/api/todos/{todo_id:\\d+}", api_todo_delete),
        web.post("/api/kpi/norms", api_kpi_norm_create),
        web.patch("/api/kpi/norms/{norm_id:\\d+}", api_kpi_norm_patch),
        web.delete("/api/kpi/norms/{norm_id:\\d+}", api_kpi_norm_delete),
        web.put("/api/kpi/override", api_kpi_override),
        web.get("/api/promises", api_promises),
        web.get("/api/promises/dupes", api_promise_dupes),
        web.get("/api/promises/mine/count", api_promises_mine_count),
        web.get("/api/promises/{cid:\\d+}", api_promise_card),
        web.post("/api/promises/{cid:\\d+}/check", api_promise_check),
        web.post("/api/promises/{cid:\\d+}/star", api_promise_star),
        web.post("/api/promises/{cid:\\d+}/take", api_promise_take),
        web.post("/api/promises/{cid:\\d+}/drop", api_promise_drop),
        web.post("/api/promises/{cid:\\d+}/merge", api_promise_merge),
        web.post("/api/promises/{cid:\\d+}/notdupe", api_promise_not_dupe),
        web.get("/card", card_maker.page),
        web.post("/api/card/scrape", card_maker.api_scrape),
        web.get("/api/card/img", card_maker.api_image),
        web.static("/static", _STATIC_DIR),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(PORT))
    await site.start()
    print(f"webapp: Mini App слухає на :{PORT}")


async def setup_menu_button(bot):
    """Кнопка апки зліва від поля вводу в приваті (як «Кошелёк» у Wallet).

    setChatMenuButton без chat_id міняє ДЕФОЛТНУ кнопку меню всіх приватних
    чатів бота: замість «Меню» зі списком команд стоятиме «МикВісті», що
    відкриває Mini App (напис — бренд, а не назва апки: кнопка живе поруч з
    полем вводу постійно, як «Кошелёк» у Wallet). Команди при цьому нікуди
    не діваються — Telegram підказує їх, щойно людина набирає «/» (список
    реєструє set_my_commands у bot.py). Ставимо при кожному старті
    (ідемпотентно), щоб не залежати від ручного /setmenubutton у BotFather;
    без WEBAPP_URL нічого не чіпаємо — інакше зламали б звичайне меню."""
    if not WEBAPP_URL:
        return
    from telegram import MenuButtonWebApp, WebAppInfo
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="МикВісті", web_app=WebAppInfo(url=WEBAPP_URL)))
    print("webapp: menu button «МикВісті» встановлено")


# ---------- /team ----------

async def todo_handler(update, context):
    """/todo <текст> — записати в особистий блокнот прямо з чату.

    Навіщо взагалі команда, коли є екран (Олег, 29.07: «щоб /todo у боті теж
    записувало задачу»): думка приходить у чаті. За даними Todoist половину
    зроблених пунктів закривають у той самий день, 10% — за хвилину після
    запису; тобто виграє те захоплення, яке не вимагає перемикати застосунок.

    Кладемо в «Потім», а не в «Сьогодні»: бот не знає плану людини на день, а
    підкинути чуже в денний список — це зіпсувати єдиний кошик, який має
    значення. Натомість поруч кнопка «Зробити сьогодні» — один тап просто в
    чаті, і план готовий."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from handlers import team_roster, team_todos

    user = update.effective_user
    person = await asyncio.to_thread(
        team_roster.resolve_person, user.id, user.username)
    if not person:
        await update.message.reply_text("Блокнот — для команди редакції.")
        return
    text = " ".join(context.args or []).strip()
    if not text:
        try:
            today, later, done = await _in_session(team_todos.summary, person)
        except Exception as e:
            await update.message.reply_text(f"Блокнот недоступний: {e}")
            return
        lines = [f"🦊 <b>Блокнот</b>",
                 f"Сьогодні: {today} · Потім: {later} · зроблено сьогодні: {done}",
                 "",
                 "Записати: <code>/todo передзвонити в департамент</code>"]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                        reply_markup=_todo_markup())
        return
    try:
        item = await _in_session(team_todos.add_todo, person, text,
                                 "later", "bot")
    except Exception as e:
        await update.message.reply_text(f"Не записав: {e}")
        return
    if not item:
        await update.message.reply_text("Порожній запис — нічого записувати.")
        return
    await update.message.reply_text(
        f"✍️ Записав у «Потім»:\n<b>{_esc(item['text'])}</b>",
        parse_mode="HTML",
        reply_markup=_todo_markup(item["id"]),
    )


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _todo_markup(todo_id=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    if todo_id:
        rows.append([InlineKeyboardButton("Зробити сьогодні",
                                          callback_data=f"todo_today:{todo_id}")])
    if WEBAPP_URL:
        from telegram import WebAppInfo
        rows.append([InlineKeyboardButton(
            "Відкрити блокнот",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}?screen=todo"))])
    return InlineKeyboardMarkup(rows) if rows else None


async def todo_today_callback(update, context):
    """Кнопка «Зробити сьогодні» під записом у чаті — той самий один тап, що
    в апці. Плану без переносу не буває, а переносити, відкриваючи апку, —
    зайвий крок саме там, де людина вже тримає телефон у руках."""
    from handlers import team_roster, team_todos

    query = update.callback_query
    user = query.from_user
    person = await asyncio.to_thread(
        team_roster.resolve_person, user.id, user.username)
    if not person:
        await query.answer("Блокнот — для команди редакції.", show_alert=True)
        return
    todo_id = int(query.data.split(":")[1])
    try:
        # _in_session передає лише позиційні аргументи
        item = await _in_session(team_todos.update_todo, person, todo_id, "today")
    except Exception as e:
        await query.answer(f"Не вдалось: {e}", show_alert=True)
        return
    if not item:
        await query.answer("Запису вже немає.", show_alert=True)
        return
    await query.answer("Перенесено в «Сьогодні»")
    try:
        await query.edit_message_text(
            f"📌 Сьогодні:\n<b>{_esc(item['text'])}</b>",
            parse_mode="HTML", reply_markup=_todo_markup())
    except Exception:
        pass


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
    else:
        # У групі web_app-кнопка недоступна — лише URL t.me. Заданого руками
        # лінка не потребуємо: helpers збере його з username бота.
        from handlers.helpers import resolve_app_link

        link, _ = await resolve_app_link(context.bot, WEBAPP_DIRECT_LINK)
        if link:
            await update.message.reply_text(
                "🦊 Завдання і KPI команди:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Відкрити «Команду»", url=link)]]
                ),
            )
        else:
            await update.message.reply_text(
                "🦊 Відкрий мене в приваті й надішли /team — там кнопка апки."
            )
