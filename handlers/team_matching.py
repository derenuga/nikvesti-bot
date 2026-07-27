"""
Зарахування виконання creative tasks — судья-матчер і фоновий прогін.

Проблема (Олег, 27.07): БД сайту віддає, що публікація зроблена В РАМКАХ
проєкту (nodes.partner_project), але НЕ каже, по якій тематиці. А таска
привʼязана саме до тематики. Тобто по (owner_id + partner_project + type)
коло звужується, але яка з тематик проєкту виконана — зі структурних даних
не видно. Синхронізація тематики в БД сайту буде колись (недоступний
програміст) — поки закриваємо це AI-суддею.

Пайплайн (на публікацію, що вийшла в проєкті з відкритими тасками):
  1. Дешевий пре-фільтр БЕЗ AI: кандидати — відкриті таски цієї людини
     (owner_id→person) у цьому проєкті, сумісні за типом (тип таска = тип
     ноди, або таска «будь-який»).
  2. Немає кандидатів → нічого не робимо.
  3. Суддя (Claude, tool use → строгий вердикт): читає заголовок + лід +
     рубрику статті і перелік тематик-кандидатів → обирає ОДНУ тематику, яку
     публікація виконує, або «жодна», з рівнем впевненості.
  4. high  → авто-зарахування; medium → черга на рішення Каті; low/жодна →
     тихо (лог «бачено, не збіг»), нікого не смикаємо.

Фоновий прогін (run_matching_scan, щогодини :45): ОДИН запит у БД сайту по
свіжих проєктних публікаціях → пре-фільтр у памʼяті проти пулу відкритих
тасків → суддя лише там, де кандидати справді є. Записи — через
team_matches (UNIQUE (source, ref)), тож повторний прогін нічого не дублює,
а збій AI не рухає нічого і просто повториться наступної години.

/match_test <url> лишається діагностикою: проганяє суддю на одній статті і
показує вердикт, НІЧОГО не зараховуючи.
"""

import asyncio
import os
import re
import time
from datetime import datetime

from handlers import (
    ai_usage, db, team_kpi, team_matches, team_projects, team_roster, team_tasks,
)
from handlers.ai_messages import async_client, _record_usage
from handlers.helpers import extract_article_id
from handlers.news_archive import BASE_URL, _news_url
from handlers.team_projects import _norm_name

ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

JUDGE_MODEL = "claude-sonnet-5"  # матчинг — відповідальна задача, беремо SMART

CONFIDENCE_ACTION = {
    "high": "авто-зарахування",
    "medium": "на рішення Каті",
    "low": "не зараховувати",
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html, limit=1600):
    text = _TAG_RE.sub(" ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def load_node_signal(node_id):
    """Сигнатура публікації з БД сайту для судді: заголовок, лід (початок
    тексту), рубрика, автор (owner_id), проєкт (partner_project), тип.
    Повертає dict або None (немає ноди / БД недоступна)."""
    if not db.is_configured():
        return None
    rows = db.query(
        "SELECT id, type, category, status, owner_id, partner_project, "
        "COALESCE(title_ua, title) AS title, "
        "COALESCE(content_ua, content) AS content "
        "FROM nodes WHERE id = %s",
        (int(node_id),),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "node_id": r["id"],
        "type": r["type"],
        "category": r["category"],
        "status": r["status"],
        "owner_id": r["owner_id"],
        "partner_project": r["partner_project"],
        "title": (r["title"] or "").strip(),
        "lead": _strip_html(r["content"]),
    }


def resolve_owner_person(owner_id):
    """owner_id (users.id) → канонічне імʼя ростера, або None."""
    if not owner_id or not db.is_configured():
        return None
    rows = db.query(
        "SELECT first_name, last_name FROM users WHERE id = %s", (int(owner_id),)
    )
    if not rows:
        return None
    full = _norm_name(f"{rows[0]['first_name'] or ''} {rows[0]['last_name'] or ''}")
    for name in team_roster.ROSTER:
        if _norm_name(name) == full:
            return name
    return None


def candidate_tasks(person, project_id, node_type, pool=None):
    """Пре-фільтр БЕЗ AI: відкриті таски людини в цьому проєкті, сумісні за
    типом (тип таска збігається з типом ноди, або таска «будь-який»).
    Пости в облік не беремо (їх у nodes немає — окрема історія).

    pool — уже прочитані відкриті таски (прогін бере їх раз на весь скан);
    без нього читаємо таски людини самі (шлях /match_test)."""
    if not person or not project_id:
        return []
    out = []
    for t in (pool if pool is not None else team_tasks.list_tasks(person)):
        if t["status"] != "open" or t["person"] != person:
            continue
        if t["project_id"] != int(project_id):
            continue
        if t["type"] == "post":
            continue
        if t["type"] and t["type"] != node_type:
            continue
        out.append(t)
    return out


# Обґрунтування суддя НЕ пише (Олег, 27.07): у картці й так видно заголовок,
# опис і зображення — переказ змісту своїми словами нічого не додає, а платимо
# ми за кожен його токен. Лишились тільки id і впевненість.
_JUDGE_TOOL = {
    "name": "verdict",
    "description": "Вердикт: яку тематику-таску виконує ця публікація (або жодну).",
    "input_schema": {
        "type": "object",
        "properties": {
            "matched_task_id": {
                "type": ["integer", "null"],
                "description": "id таски, яку публікація виконує; null — жодна з наведених",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high — публікація чітко і конкретно виконує саме цю тематику; "
                               "medium — схоже, але є сумнів; low — слабкий звʼязок",
            },
        },
        "required": ["matched_task_id", "confidence"],
    },
}

# Критичні інформаційні потреби (CIN) — методологія Annenberg School (USC),
# адаптована для України Media Development Foundation у «News Deserts in
# Ukraine 2.0». Донори (Internews, IWPR, IMS) формулюють тематики саме цією
# мовою, а суддя без переліку восьми потреб гадає, що це таке.
#
# Блок додається в промпт ЛИШЕ коли серед кандидатів є CIN-тематика: інакше це
# були б зайві ~150 вхідних токенів на кожну публікацію.
CIN_KEYWORDS = ("критичн", "інформаційн потреб", "інформаційних потреб", "cin")

CIN_RUBRIC = (
    "Довідка: «критичні інформаційні потреби» (CIN, методологія Annenberg "
    "School USC, адаптована MDF у «News Deserts in Ukraine 2.0») — це вісім "
    "напрямів, без яких громада не може ухвалювати рішення:\n"
    "1) надзвичайні ситуації і безпека (обстріли, евакуація, лінія фронту, "
    "правоохоронці, стихія, техногенні загрози);\n"
    "2) охорона здоровʼя (доступність, якість і вартість медпослуг, епідемії, "
    "вакцинація);\n"
    "3) освіта (якість і управління школами, доступ до навчання, перекваліфікація);\n"
    "4) транспорт (сполучення, вартість, стан доріг, ремонти, ДТП і безпека руху);\n"
    "5) економічний розвиток (робота і зайнятість, підтримка бізнесу, великі "
    "економічні ініціативи);\n"
    "6) довкілля (якість повітря і води, екологічні загрози, відходи, відновлення);\n"
    "7) громадські ініціативи і публічні послуги (ОСББ, ГО, соцпослуги, культура, "
    "бібліотеки);\n"
    "8) політика і врядування (зміни влади, рішення й тендери місцевої влади, "
    "нові правила).\n"
    "Публікація закриває цю тематику, якщо дає жителям практично корисну "
    "інформацію хоча б з одного напряму (не просто згадує подію)."
)


def _cin_hint(candidates):
    """Рубрика CIN — тільки якщо серед кандидатів є така тематика."""
    for t in candidates:
        name = (t.get("theme_name") or "").lower()
        if any(k in name for k in CIN_KEYWORDS):
            return "\n\n" + CIN_RUBRIC
    return ""


async def judge_publication(article, candidates):
    """AI-суддя: обирає тематику-кандидата, яку виконує публікація, або жодну.
    article — {title, lead, category, type}; candidates — list таск-dict.
    Повертає {matched_task_id, confidence, reasoning, task} (task — обрана або None)."""
    if not candidates:
        return {"matched_task_id": None, "confidence": "low", "task": None}

    lines = []
    for t in candidates:
        theme = t["theme_name"] or "(без тематики)"
        type_word = {"news": "новина", "article": "стаття"}.get(t["type"], "будь-який тип")
        lines.append(f"- id={t['id']}: тематика «{theme}» ({type_word})"
                     + (f", нотатка: {t['note']}" if t["note"] else ""))
    tasks_block = "\n".join(lines)

    prompt = (
        "Ти — прискіпливий редактор, що звіряє вихід публікації із планом завдань.\n\n"
        f"ПУБЛІКАЦІЯ (тип: {article['type']}, рубрика: {article.get('category') or '—'}):\n"
        f"Заголовок: {article['title']}\n"
        f"Початок тексту: {article['lead']}\n\n"
        f"ВІДКРИТІ ЗАВДАННЯ АВТОРА в цьому проєкті (обери ОДНЕ, яке ця публікація виконує):\n"
        f"{tasks_block}"
        f"{_cin_hint(candidates)}\n\n"
        "Правила:\n"
        "• Обирай тематику, ЗМІСТ якої публікація конкретно виконує (не просто «той самий проєкт»).\n"
        "• confidence=high лише коли публікація явно і однозначно про цю тематику.\n"
        "• Якщо жодна тематика не підходить за змістом — matched_task_id=null.\n"
        "• Якщо публікація рівною мірою пасує двом — обери найточнішу і став medium.\n"
        "Виклич інструмент verdict."
    )

    msg = await async_client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=400,
        thinking={"type": "disabled"},
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "verdict"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        _record_usage(JUDGE_MODEL, msg.usage)
    except Exception:
        pass
    verdict = next((b.input for b in msg.content if b.type == "tool_use"), None) or {
        "matched_task_id": None, "confidence": "low"}
    tid = verdict.get("matched_task_id")
    verdict["task"] = next((t for t in candidates if t["id"] == tid), None)
    if tid is not None and verdict["task"] is None:
        # Суддя назвав id поза списком — вважаємо «жодна»
        verdict["matched_task_id"] = None
    return verdict


# ---------- Прогін: свіжі публікації → зарахування ----------
#
# Ліміти БД сайту (10000 запитів/год на весь бот) диктують форму: ОДИН запит
# на весь прогін, а не по ноді. Далі все — в памʼяті, і AI вмикається лише
# там, де пре-фільтр знайшов кандидатів.

SCAN_DAYS = 7          # вікно свіжості для щогодинного прогону
SCAN_LIMIT = 500       # стеля вибірки за прогін (запобіжник, не робочий режим)
JUDGE_CONCURRENCY = 4  # скільки суддів паралельно

_NODE_COLS = (
    "id, type, category, status, owner_id, partner_project, published, "
    "slug_ua, slug, COALESCE(title_ua, title) AS title, "
    "COALESCE(content_ua, content) AS content"
)


def fetch_project_nodes(since_ts, until_ts=None, limit=SCAN_LIMIT):
    """Проєктні публікації з БД сайту за вікном — ОДНИМ запитом.

    Беремо тільки те, що взагалі може щось закрити: опубліковане (status=1),
    у рамках проєкту (partner_project) і типу, який є в тасках (news/article).
    published <= UNIX_TIMESTAMP() — гейт відкладених в адмінці (nodes.published
    у них бреше майбутнім/минулим, як і в fb_missing)."""
    if not db.is_configured():
        return []
    sql = (
        f"SELECT {_NODE_COLS} FROM nodes "
        "WHERE status = 1 AND partner_project IS NOT NULL AND partner_project > 0 "
        "AND type IN ('news', 'article') "
        "AND published >= %s AND published <= UNIX_TIMESTAMP() "
    )
    params = [int(since_ts)]
    if until_ts:
        sql += "AND published < %s "
        params.append(int(until_ts))
    sql += "ORDER BY published DESC LIMIT %s"
    params.append(int(limit))
    return db.query(sql, params)


def owner_person_map():
    """{users.id: людина ростера} — з піном team_user_link, як у KPI.

    Через пін, а не через ПІБ: у users бувають дублі акаунтів (звільнена +
    нинішня), і пошук за ПІБ брав старший id — виконання зараховувалось би
    порожньому акаунту."""
    links = team_kpi.get_user_links()
    names = team_kpi._user_id_map()
    out = {}
    for person in team_roster.ROSTER:
        if team_roster.ROSTER[person]["manager"]:
            continue          # у керівництва тасків немає
        uid = team_kpi.resolve_site_user_id(person, links, names)
        if uid:
            out[int(uid)] = person
    return out


def node_url(row):
    """Канонічний URL матеріалу. Статті живуть за іншим шляхом, ніж новини:
    /articles/{slug} проти /news/{рубрика}/{slug}. Двіжок редиректить із
    новинного шляху на статейний, тож картка відкрилась би й так — але лінк
    у черзі має бути тим самим, що в GA4 і в репостах, а не редиректом."""
    if row.get("type") == "article":
        slug = (row.get("slug_ua") or row.get("slug") or "").strip()
        return f"{BASE_URL}/articles/{slug or row['id']}"
    return _news_url(row)


def node_signal(row):
    """Рядок nodes → сигнатура для судді й снапшот для картки в черзі."""
    return {
        "node_id": row["id"],
        "type": row["type"],
        "category": row["category"],
        "owner_id": row["owner_id"],
        "project_id": row["partner_project"],
        "published": row["published"],
        "title": (row["title"] or "").strip(),
        "lead": _strip_html(row["content"]),
        "url": node_url(row),
    }


def fetch_card_meta(url):
    """(опис, зображення) зі сторінки матеріалу — щоб картка в черзі виглядала
    як новина, а не як рядок бази.

    Беремо og:description і og:image: перше — редакторський лід (а не перші
    речення тексту), друге — вже ресайзнутий сайтом .webp 600×315, тобто
    легкий і готовий до показу. Один запит, і ТІЛЬКИ для спірних: у
    авто-зарахованих картки немає, платити за них трафіком нема сенсу.
    Будь-який збій — просто без картинки."""
    if not url:
        return None, None
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(
            url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NikVesti-Bot/1.0)"})
        if resp.status_code != 200:
            return None, None
        soup = BeautifulSoup(resp.text, "html.parser")

        def meta(prop, attr="property"):
            tag = soup.find("meta", attrs={attr: prop})
            return (tag.get("content") or "").strip() if tag else ""

        description = meta("og:description") or meta("description", "name")
        image = meta("og:image")
        return (description[:600] or None), (image or None)
    except Exception as e:
        print(f"team_matching: картку {url} не вдалось одягнути — {e}")
        return None, None


class ScanReport:
    """Підсумок прогону — те, що потім летить у чат зведенням."""

    def __init__(self):
        self.seen = 0
        self.auto = 0
        self.pending = 0
        self.skipped = 0
        self.errors = 0
        self.closed = []        # [(person, task)] — таски, що закрились самі
        self.judged = 0         # скільки разів реально смикали AI
        self.tg_seen = 0        # постів каналу переглянуто (за дисклеймером)
        self.tg_queued = 0      # з них проєктних — у чергу Каті

    def as_text(self):
        parts = [f"переглянуто {self.seen}"]
        if self.tg_seen:
            parts[0] += f" (+{self.tg_seen} постів каналу)"
        if self.auto:
            parts.append(f"зараховано {self.auto}")
        if self.pending:
            parts.append(f"у чергу {self.pending}")
        if self.closed:
            parts.append(f"закрито тасків {len(self.closed)}")
        if self.skipped:
            parts.append(f"без кандидатів {self.skipped}")
        if self.errors:
            parts.append(f"збоїв {self.errors}")
        return " · ".join(parts)


async def judge_nodes(rows, *, ping=None, report=None, judge=None):
    """Ядро авто-обліку: судить публікації і пише вердикти.

    ping — async-колбек (person, task) для привітання виконавиці; None —
    пінги приглушені (саме так ретроспектива не завалює людей двадцятьма
    привітаннями за раз).
    judge — підміна судді (тести); за замовчуванням справжній AI.
    """
    report = report or ScanReport()
    judge = judge or judge_publication
    if not rows:
        return report

    person_by_uid, pool, counts = await asyncio.to_thread(_load_pool)
    fresh = await asyncio.to_thread(
        team_matches.known_refs, "site", [r["id"] for r in rows]
    )
    sem = asyncio.Semaphore(JUDGE_CONCURRENCY)
    # Замок на пул: рішення по одній публікації змінює залишок таска, а судді
    # працюють паралельно — без нього дві статті могли б зайняти одне й те
    # саме останнє місце в тасці.
    lock = asyncio.Lock()

    async def handle(row):
        sig = node_signal(row)
        report.seen += 1
        person = person_by_uid.get(sig["owner_id"])
        project_id = sig["project_id"]
        base = {
            "person": person, "project_id": project_id, "title": sig["title"],
            "url": sig["url"], "published": sig["published"], "node_type": sig["type"],
        }
        if not person:
            await asyncio.to_thread(
                team_matches.record_match, "site", sig["node_id"], "skipped",
                reasoning="Автора немає в ростері (owner_id "
                          f"{sig['owner_id']})", **base)
            report.skipped += 1
            return
        async with lock:
            cands = [t for t in candidate_tasks(person, project_id, sig["type"], pool)
                     if counts.get(t["id"], 0) < t["qty"]]
        if not cands:
            await asyncio.to_thread(
                team_matches.record_match, "site", sig["node_id"], "skipped",
                reasoning="Відкритих тасків у цьому проєкті немає", **base)
            report.skipped += 1
            return

        async with sem:
            try:
                verdict = await judge(sig, cands)
                report.judged += 1
            except Exception as e:
                # Нічого не пишемо: наступний прогін спробує ще (курсора,
                # який можна зіпсувати, у нас немає — рухає лише сам запис)
                print(f"team_matching: суддя впав на ноді {sig['node_id']} — {e}")
                report.errors += 1
                return

        task = verdict.get("task")
        high = verdict.get("confidence") == "high" and task
        status = "auto" if high else "pending"
        # Опис і зображення — лише спірним: їм малювати картку в черзі
        if not high:
            base["description"], base["image"] = await asyncio.to_thread(
                fetch_card_meta, sig["url"])
        match = await asyncio.to_thread(
            team_matches.record_match, "site", sig["node_id"], status,
            task_id=task["id"] if task else None,
            confidence=verdict.get("confidence"), **base)
        if not match:
            return          # хтось уже вирішив цю публікацію (повторний прогін)
        if not high:
            report.pending += 1
            return
        report.auto += 1
        async with lock:
            counts[task["id"]] = counts.get(task["id"], 0) + 1
        updated, action = await asyncio.to_thread(
            team_matches.apply_progress, task["id"])
        if action == "done":
            async with lock:
                pool[:] = [t for t in pool if t["id"] != task["id"]]
            report.closed.append((person, updated))
            if ping:
                try:
                    await ping(person, updated)
                except Exception as e:
                    print(f"team_matching: пінг про виконання не пішов — {e}")

    await asyncio.gather(*[handle(r) for r in rows if str(r["id"]) not in fresh])
    return report


def _load_pool():
    """Одним з'єднанням: мапа авторів, пул відкритих тасків і поточні
    зарахування по них."""
    from handlers import bot_db
    with bot_db.session():
        pool = team_tasks.list_open_tasks()
        counted = team_matches.counted_by_task([t["id"] for t in pool])
    person_by_uid = owner_person_map()
    counts = {tid: len(v) for tid, v in counted.items()}
    return person_by_uid, pool, counts


async def _ping_done(bot, person, task):
    """Привітання виконавиці: таск закрився сам, і видно, чим саме."""
    links = ""
    matches = await asyncio.to_thread(team_matches.counted_by_task, [task["id"]])
    for m in matches.get(task["id"], []):
        links += f"\n• {m['title']} — {m['url']}"
    await team_tasks._ping(
        bot, person,
        f"🦊 Завдання виконано — зарахував сам:\n{team_tasks.task_summary(task)}"
        f"{links}",
    )


def resolve_publication(url, fallback_project=None):
    """Лінк → сигнатура публікації для ручних операцій: (dict, None) або
    (None, помилка-рядком). Приймає і матеріал сайту, і пост каналу."""
    url = (url or "").strip()
    if not url:
        return None, "Порожній лінк"

    # Пост каналу — окреме джерело: у nodes його немає
    if "t.me/" in url:
        ref = url.rstrip("/").split("/")[-1].split("?")[0]
        if not ref.isdigit():
            return None, "З лінка не видно номер поста"
        info = {"source": "telegram", "ref": ref, "node_type": "post",
                "title": f"Пост {ref}", "published": None, "person": None,
                "project_id": fallback_project, "description": None, "image": None,
                "url": f"https://t.me/nikvesti/{ref}"}
        try:
            from handlers import team_tg_match
            posts = team_tg_match.fetch_recent_posts(1)
            found = next((p for p in posts if str(p["message_id"]) == ref), None)
            if found:
                info["title"] = team_tg_match.post_title(found)
                info["description"] = team_tg_match.post_description(found)
                info["image"] = found.get("image")
                info["published"] = int(found["dt"].timestamp()) if found["dt"] else None
        except Exception as e:
            print(f"team_matching: заголовок поста {ref} не прочитався — {e}")
        return info, None

    node_id = extract_article_id(url)
    if not node_id:
        return None, "Це не схоже на лінк матеріалу nikvesti.com"
    rows = db.query(
        f"SELECT {_NODE_COLS} FROM nodes WHERE id = %s", (int(node_id),)
    ) if db.is_configured() else []
    if not rows:
        return None, f"Матеріалу {node_id} немає в БД сайту"
    row = rows[0]
    return {
        "source": "site", "ref": str(node_id), "node_type": row["type"],
        "title": (row["title"] or "").strip(), "published": row["published"],
        "person": owner_person_map().get(row["owner_id"]),
        "project_id": row["partner_project"] or fallback_project,
        "url": node_url(row), "description": None, "image": None,
    }, None


def queue_publication(url, actor):
    """Покласти публікацію в чергу «Сповіщень» РУКАМИ, за лінком.

    Потрібно, коли редактор сам натрапив на матеріал, якого прогін не побачив
    (не той автор у CMS, публікація поза проєктом, старіша за вікно), і хоче
    вирішити по ньому окремо. Уже зараховане повертається в чергу тим самим
    записом — див. requeue_match. Повертає (матч, [зачеплені таски]) або
    (None, помилка)."""
    info, error = resolve_publication(url)
    if error:
        return None, error
    existing = team_matches.find_match(info["source"], info["ref"])
    if existing:
        if existing["status"] == "pending":
            return None, "Ця публікація вже в черзі"
        return team_matches.requeue_match(existing["id"], actor)
    if info["source"] == "site":
        info["description"], info["image"] = fetch_card_meta(info["url"])
    match = team_matches.record_match(
        info["source"], info["ref"], "pending", person=info["person"],
        project_id=info["project_id"], title=info["title"], url=info["url"],
        published=info["published"], node_type=info["node_type"],
        description=info["description"], image=info["image"])
    return match, []


def attach_publication(task, url, actor):
    """Зарахувати конкретну публікацію в завдання РУКАМИ, за лінком.

    Потрібно у двох життєвих випадках (Олег, 27.07): перенести зараховане з
    помилково задубльованого завдання в правильне, і просто сказати «оце
    сюди», не чекаючи прогону й не шукаючи публікацію в черзі.

    Якщо цю публікацію вже судили — переносимо ТОЙ САМИЙ запис (UNIQUE
    (source, ref) один на публікацію), а не заводимо другий: інакше прогрес
    порахував би її двічі. Повертає (матч, [id зачеплених тасків]) або
    (None, помилка-рядком)."""
    info, error = resolve_publication(url, fallback_project=task["project_id"])
    if error:
        return None, error
    source, ref = info["source"], info["ref"]
    title, published, project_id = info["title"], info["published"], info["project_id"]
    node_type, clean_url = info["node_type"], info["url"]

    existing = team_matches.find_match(source, ref)
    if existing:
        if existing["task_id"] == task["id"] and existing["status"] in team_matches.COUNTED:
            return None, "Ця публікація вже зарахована в це завдання"
        match, touched = team_matches.decide_match(
            existing["id"], actor, "confirm", task_id=task["id"],
            person=task["person"])
        return match, touched
    match = team_matches.record_match(
        source, ref, "confirmed", task_id=task["id"], person=task["person"],
        project_id=project_id, title=title, url=clean_url, published=published,
        node_type=node_type)
    return match, [task["id"]]


def _enrich_pending_cards(limit=50):
    """Самолікування черги: добирає опис і зображення тим спірним, у кого їх
    ще немає. Пости Telegram сюди не йдуть — у них картинка вже зі стрічки."""
    from handlers import bot_db
    with bot_db.session():
        stale = [m for m in team_matches.pending_without_meta(limit)
                 if m["source"] == "site" and m["url"]]
    filled = 0
    for m in stale:
        description, image = fetch_card_meta(m["url"])
        if description or image:
            team_matches.set_meta(m["id"], description, image)
            filled += 1
    return filled


async def run_matching_scan(bot, days=SCAN_DAYS, ping=True, chat_id=None):
    """Щогодинний прогін (scheduler). Тихий: у чат пише, лише коли є що
    сказати, і лише на явний виклик (/match_scan)."""
    if not db.is_configured():
        return None
    since = int(time.time()) - days * 86400
    rows = await asyncio.to_thread(fetch_project_nodes, since)
    ping_cb = (lambda person, task: _ping_done(bot, person, task)) if ping else None
    report = await judge_nodes(rows, ping=ping_cb)
    # Картки, що лишились без опису/зображення (сторінка тоді не відповіла або
    # рядок записано до появи цих колонок) — доганяємо тихо, по кілька за раз
    try:
        await asyncio.to_thread(_enrich_pending_cards)
    except Exception as e:
        print(f"team_matching: картки черги не оновились — {e}")
    # Telegram-пости за дисклеймером — той самий прогін, інше джерело і без AI.
    # Окремим try: збій t.me не має чіпати зарахування з сайту.
    try:
        from handlers import team_tg_match
        seen_posts, queued = await asyncio.to_thread(team_tg_match.scan_posts)
        report.tg_seen, report.tg_queued = seen_posts, queued
        report.pending += queued
    except Exception as e:
        print(f"team_matching: пости каналу не переглянулись — {e}")
    if report.auto or report.pending or report.errors:
        print(f"team_matching: прогін — {report.as_text()}")
    if chat_id:
        await bot.send_message(chat_id=chat_id, text=f"🦊 Звірка: {report.as_text()}")
    return report


# ---------- Ретроспектива: догнати те, що вже вийшло ----------
#
# Вимога Олега: НЕ робити baseline («позначити старе побаченим і почати з
# нуля»). Таски вже поставлені, публікації по них уже вийшли — бот має пройти
# базу і позакривати їх заднім числом.
#
# Вікно привʼязане до ТАСКІВ, а не до календаря: для кожного відкритого таска
# беремо публікації його автора в його проєкті від початку періоду таска.
# Практично — min(created_at таска, перше число місяця його дедлайну): масова
# постановка ставить таск на місяць із дедлайном у кінці, і публікації з
# початку того місяця мають зарахуватись, навіть якщо таск створили 27-го.
#
# Спершу оцінка (безкоштовно), потім прогін — патерн /entity_estimate +
# /entity_backfill. Пінги на час ретроспективи ПРИГЛУШЕНІ: інакше при закритті
# двох десятків тасків заднім числом людям прилетіло б двадцять привітань.

# Оцінка токенів на одну публікацію: промпт судді — заголовок + ~1600 символів
# ліда + перелік кандидатів (+ рубрика CIN там, де тематика про критичні
# інформаційні потреби); вихід — сам вердикт, лише id і впевненість, бо
# обґрунтування словами суддя більше не пише.
EST_IN_PER_NODE = 950
EST_OUT_PER_NODE = 50

_retro_running = {"flag": False}


def _month_start(date_iso):
    return f"{date_iso[:7]}-01"


def _day_ts(date_iso):
    return int(datetime.strptime(date_iso, "%Y-%m-%d")
               .replace(tzinfo=team_tasks.KYIV_TZ).timestamp())


def current_month_start():
    """Перше число поточного місяця за Києвом — дефолтна межа ретроспективи
    (Олег, 27.07: «достатньо за липень прогнати»). Глибше — тільки явно."""
    return datetime.now(team_tasks.KYIV_TZ).strftime("%Y-%m-01")


def retro_windows(pool, since=None):
    """{(users.id автора, project_id): найраніший unix-час вікна} — з чого
    починати ретроспективу для кожної пари «людина × проєкт».

    since ('YYYY-MM-DD') — жорстка межа: глибше не заглядаємо, навіть якщо
    таск старіший. Так «прогнати за липень» коштує рівно липень."""
    person_by_uid = owner_person_map()
    uid_by_person = {p: uid for uid, p in person_by_uid.items()}
    floor = _day_ts(since) if since else None
    windows = {}
    for t in pool:
        uid = uid_by_person.get(t["person"])
        if not uid or not t["project_id"]:
            continue
        start = t["created_at"][:10]
        if t["deadline"]:
            start = min(start, _month_start(t["deadline"]))
        ts = _day_ts(start)
        if floor is not None:
            ts = max(ts, floor)
        key = (uid, int(t["project_id"]))
        windows[key] = min(windows.get(key, ts), ts)
    return windows


def retro_nodes(windows, limit=SCAN_LIMIT * 4):
    """Публікації, що підпадають під вікна — ОДНИМ запитом (широка вибірка за
    найранішим вікном) із доводкою в памʼяті по кожній парі окремо."""
    if not windows or not db.is_configured():
        return []
    uids = sorted({uid for uid, _ in windows})
    projects = sorted({pid for _, pid in windows})
    rows = db.query(
        f"SELECT {_NODE_COLS} FROM nodes "
        "WHERE status = 1 AND type IN ('news', 'article') "
        "AND published >= %s AND published <= UNIX_TIMESTAMP() "
        "AND owner_id IN ({}) AND partner_project IN ({}) "
        "ORDER BY published".format(
            ",".join(["%s"] * len(uids)), ",".join(["%s"] * len(projects)))
        + " LIMIT %s",
        [min(windows.values()), *uids, *projects, int(limit)],
    )
    out = []
    for r in rows:
        start = windows.get((r["owner_id"], r["partner_project"]))
        if start is not None and r["published"] >= start:
            out.append(r)
    return out


def retro_plan(since=None):
    """Що саме підпадає під ретроспективу: (ноди на суд, усього знайдено,
    к-сть пар, найраніша дата). Read-only і безкоштовно.

    since=None — поточний місяць; 'all' — уся глибина тасків."""
    since = None if since == "all" else (since or current_month_start())
    from handlers import bot_db
    with bot_db.session():
        pool = team_tasks.list_open_tasks()
        windows = retro_windows([t for t in pool if t["project_id"]], since)
    rows = retro_nodes(windows)
    with bot_db.session():
        seen = team_matches.known_refs("site", [r["id"] for r in rows])
    fresh = [r for r in rows if str(r["id"]) not in seen]
    earliest = min(windows.values()) if windows else None
    return fresh, len(rows), len(windows), earliest


def estimate_cost(n):
    """($ за прогін, скільки токенів) за прайсом судді."""
    price_in, price_out = ai_usage.PRICING.get(JUDGE_MODEL, ai_usage.DEFAULT_PRICE)
    tin, tout = n * EST_IN_PER_NODE, n * EST_OUT_PER_NODE
    return tin / 1e6 * price_in + tout / 1e6 * price_out, tin, tout


def parse_since(args):
    """Аргумент межі: YYYY-MM (місяць) / YYYY-MM-DD (день) / all (уся глибина).
    Порожньо — поточний місяць. Повертає (since, людський підпис)."""
    raw = (args[0] if args else "").strip().lower()
    if raw in ("all", "усе", "все"):
        return "all", "уся глибина тасків"
    if len(raw) == 7 and raw[4] == "-":
        return f"{raw}-01", f"з {raw}"
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return raw, f"з {raw}"
    month = current_month_start()
    return month, f"поточний місяць (з {month})"


async def match_estimate_handler(update, context):
    """/match_estimate [YYYY-MM|all] — скільки публікацій підпаде під
    ретроспективу і скільки це коштуватиме. Нічого не витрачає і не пише.
    Без аргументу — поточний місяць."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    since, label = parse_since(context.args)
    msg = await update.message.reply_text(
        f"🦊 Рахую ретроспективу ({label}, безкоштовно)…")
    try:
        fresh, total, pairs, earliest = await asyncio.to_thread(retro_plan, since)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалося порахувати: {e}")
        return
    if not pairs:
        await msg.edit_text("Відкритих проєктних тасків немає — доганяти нічого.")
        return
    cost, tin, tout = estimate_cost(len(fresh))
    since_label = (datetime.fromtimestamp(earliest, team_tasks.KYIV_TZ)
                   .strftime("%d.%m.%Y") if earliest else "—")
    # Місячний прогноз: скільки б коштував той самий обсяг щомісяця
    days = max(1, (int(time.time()) - earliest) // 86400) if earliest else 30
    monthly = cost / days * 30
    await msg.edit_text(
        f"🦊 Ретроспектива звірки виконання — {label}\n\n"
        f"Пар «людина × проєкт»: {pairs}\n"
        f"Найраніше вікно: з {since_label}\n"
        f"Публікацій у вікнах: {total}\n"
        f"З них ще не суджених: <b>{len(fresh)}</b>\n\n"
        f"Токени: ~{tin/1000:.0f}k вх + ~{tout/1000:.0f}k вих ({JUDGE_MODEL})\n"
        f"Вартість прогону: <b>≈ ${cost:.2f}</b>\n"
        f"Для орієнтиру, той самий обсяг щомісяця: ≈ ${monthly:.2f}/міс\n\n"
        f"Запуск (платно): /match_backfill yes"
        + (f" {context.args[0]}" if context.args else "") + "\n"
        f"<i>Пінги виконавицям на час ретроспективи приглушені — інакше "
        f"прилетіло б двадцять привітань разом.</i>",
        parse_mode="HTML",
    )


async def match_backfill_handler(update, context):
    """/match_backfill yes [YYYY-MM|all] — ретроспективний прогін бази. Пінги
    приглушені, зведення — в кінці. Без межі — поточний місяць.
    Ідемпотентно: ганяти можна скільки завгодно."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if _retro_running["flag"]:
        await update.message.reply_text("Ретроспектива вже йде — зачекай зведення.")
        return
    if not context.args or context.args[0].lower() not in ("yes", "так", "-y"):
        await update.message.reply_text(
            "Спершу оцінка (безкоштовно): /match_estimate\n"
            "Далі запуск: /match_backfill yes [YYYY-MM|all]\n"
            "Без межі — поточний місяць."
        )
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        await update.message.reply_text("ANTHROPIC_API_KEY не заданий.")
        return
    since, label = parse_since(context.args[1:])

    msg = await update.message.reply_text(f"🦊 Збираю, що доганяти ({label})…")
    _retro_running["flag"] = True
    try:
        fresh, total, pairs, earliest = await asyncio.to_thread(retro_plan, since)
        if not fresh:
            await msg.edit_text(
                f"Доганяти нема чого: у вікнах {total} публікацій, і всі вже "
                f"розібрані.")
            return
        cost, _, _ = estimate_cost(len(fresh))
        await msg.edit_text(
            f"🦊 Доганяю {len(fresh)} публікацій, {label} (≈ ${cost:.2f}). Пінги "
            f"приглушені, зведення пришлю в кінці.")
        report = await judge_nodes(fresh, ping=None)   # ← пінги вимкнені навмисно
        lines = [f"🦊 Ретроспектива ({label}) готова: {report.as_text()}"]
        if report.closed:
            lines.append("\nЗакрились самі:")
            for person, task in report.closed:
                lines.append(f"✅ {person} — {team_tasks.task_summary(task)}")
        if report.pending:
            lines.append(f"\n🤔 {report.pending} спірних чекають у «Сповіщеннях» апки.")
        if report.errors:
            lines.append(f"\n⚠️ {report.errors} публікацій не додивились "
                         f"(збій AI) — повтори /match_backfill yes, воно "
                         f"безпечне: вже зараховане не подвоїться.")
        lines.append("\nЛюдям нічого не прилетіло — побачать в апці.")
        await msg.edit_text("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Ретроспектива впала: {e}\n"
                            f"Уже зараховане збереглось — повторний запуск "
                            f"продовжить з того ж місця.")
    finally:
        _retro_running["flag"] = False


# ---------- Тестова команда (нічого не зараховує) ----------

async def match_scan_handler(update, context):
    """/match_scan [днів] — прогнати звірку виконання зараз (за замовчуванням
    вікно щогодинного прогону). Зараховує по-справжньому; ідемпотентно —
    повторний виклик нічого не подвоїть."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    try:
        days = max(1, min(400, int(context.args[0]))) if context.args else SCAN_DAYS
    except ValueError:
        days = SCAN_DAYS
    msg = await update.message.reply_text(f"🦊 Звіряю публікації за {days} дн…")
    try:
        report = await run_matching_scan(context.bot, days=days)
    except Exception as e:
        await msg.edit_text(f"❌ Прогін упав: {e}")
        return
    if report is None:
        await msg.edit_text("БД сайту не налаштована — звіряти нема з чим.")
        return
    lines = [f"🦊 Звірка за {days} дн: {report.as_text()}"]
    for person, task in report.closed:
        lines.append(f"✅ {person}: {team_tasks.task_summary(task)}")
    if report.pending:
        lines.append(f"\n🤔 {report.pending} спірних — у «Сповіщеннях» апки.")
    await msg.edit_text("\n".join(lines), disable_web_page_preview=True)


async def match_requeue_handler(update, context):
    """/match_requeue <url або id> — повернути публікацію в чергу «Сповіщень».

    Потрібно, коли рішення виявилось помилковим уже після того, як його
    ухвалили: зарахували не тому, зняли, а таск потім видалили — і публікація
    зависла зі статусом rejected, тобто ні в черзі, ні у судді (rejected
    входить у DECIDED, тож прогін її більше не чіпає)."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not context.args:
        await update.message.reply_text(
            "Використання: /match_requeue <url матеріалу або id ноди>")
        return
    arg = context.args[0]
    ref = str(extract_article_id(arg) or arg).strip()

    def run():
        from handlers import bot_db
        with bot_db.session():
            rows = bot_db.query(
                "SELECT id, status, title FROM team_task_matches "
                "WHERE ref = %s ORDER BY id DESC LIMIT 1", (ref,))
            if not rows:
                return None
            match, touched = team_matches.requeue_match(rows[0]["id"], "Олег")
            for tid in touched:
                team_matches.apply_progress(tid)
            return {"before": rows[0]["status"], "match": match}

    result = await asyncio.to_thread(run)
    if not result:
        await update.message.reply_text(
            f"Матеріал {ref} у звірці не значиться — його ще не судили. "
            f"Прогнати: /match_scan")
        return
    await update.message.reply_text(
        f"🦊 Повернув у чергу: {result['match']['title'][:80]}\n"
        f"Було: {result['before']} → стало: pending.\n"
        f"Розібрати — у «Сповіщеннях» апки.")


async def match_cards_handler(update, context):
    """/match_cards [скільки] — доодягнути картки черги зараз: опис і
    зображення зі сторінок матеріалів. Те саме робить щогодинний прогін, але
    після великої ретроспективи чекати годину нема сенсу."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    try:
        limit = max(1, min(300, int(context.args[0]))) if context.args else 100
    except ValueError:
        limit = 100
    msg = await update.message.reply_text("🦊 Одягаю картки черги…")
    try:
        filled = await asyncio.to_thread(_enrich_pending_cards, limit)
        left = await asyncio.to_thread(
            lambda: len(team_matches.pending_without_meta(limit)))
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        return
    await msg.edit_text(
        f"🦊 Оновлено карток: {filled}"
        + (f"\nЩе без опису/картинки: {left} — повтори /match_cards."
           if left else "\nУсі картки черги з описом і зображенням."))


async def match_tg_handler(update, context):
    """/match_tg [сторінок] — переглянути пости каналу за дисклеймерами зараз
    (авто — разом зі щогодинним прогоном). AI тут не задіяний."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    from handlers import team_tg_match

    try:
        pages = max(1, min(20, int(context.args[0]))) if context.args else \
            team_tg_match.SCAN_PAGES
    except ValueError:
        pages = team_tg_match.SCAN_PAGES
    msg = await update.message.reply_text(f"🦊 Дивлюсь {pages} стор. каналу…")
    try:
        seen, queued = await asyncio.to_thread(team_tg_match.scan_posts, pages)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось переглянути канал: {e}")
        return
    await msg.edit_text(
        f"🦊 Постів переглянуто: {seen}\n"
        + (f"Проєктних (за дисклеймером) у чергу: {queued} — розібрати в "
           f"«Сповіщеннях» апки."
           if queued else "Нових проєктних постів немає."))


async def match_test_handler(update, context):
    """/match_test <url> — прогнати суддю на реальній статті проти реальних
    відкритих тасків її автора. Показує вердикт і що БУЛО Б зроблено —
    але НІЧОГО не зараховує. Діагностика якості матчингу перед авто-обліком."""
    if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /match_test <url статті nikvesti.com>")
        return
    url = context.args[0]
    node_id = extract_article_id(url)
    if not node_id:
        await update.message.reply_text("Не змогла дістати id матеріалу з URL.")
        return

    msg = await update.message.reply_text("🦊 Дивлюсь публікацію і звіряю з планом…")

    import asyncio
    signal = await asyncio.to_thread(load_node_signal, node_id)
    if not signal:
        await msg.edit_text(f"Ноду {node_id} не знайдено в БД сайту (або БД недоступна).")
        return

    person = await asyncio.to_thread(resolve_owner_person, signal["owner_id"])
    project_id = signal["partner_project"]
    project = None
    if project_id:
        projects = await asyncio.to_thread(team_projects.list_projects, False)
        project = next((p for p in projects if p["id"] == int(project_id)), None)

    header = (
        f"📄 <b>{signal['title'][:120]}</b>\n"
        f"Тип: {signal['type']} · рубрика: {signal.get('category') or '—'}\n"
        f"Автор (owner_id {signal['owner_id']}): {person or '❓ не в ростері'}\n"
        f"Проєкт (partner_project {project_id or '—'}): "
        f"{(project['partner'] + ' · ' + project['name']) if project else '❓ поза проєктом'}\n"
    )
    if not person:
        await msg.edit_text(header + "\n⚠️ Автора не зіставлено з ростером — матчинг неможливий.", parse_mode="HTML")
        return
    if not project_id:
        await msg.edit_text(header + "\n⚠️ Публікація не в рамках проєкту — таски не звіряються.", parse_mode="HTML")
        return

    candidates = await asyncio.to_thread(candidate_tasks, person, project_id, signal["type"])
    if not candidates:
        await msg.edit_text(
            header + f"\n📋 Відкритих тасків {person} у цьому проєкті (сумісних за типом) — немає.\n"
            "Нічого зараховувати.", parse_mode="HTML")
        return

    cand_lines = "\n".join(
        f"  • id={t['id']}: «{t['theme_name'] or '(без тематики)'}» ({t['qty']} × {t['type'] or 'будь-який'})"
        for t in candidates)

    verdict = await judge_publication(
        {"title": signal["title"], "lead": signal["lead"],
         "category": signal["category"], "type": signal["type"]},
        candidates,
    )

    conf = verdict["confidence"]
    action = CONFIDENCE_ACTION.get(conf, "?")
    icon = {"high": "✅", "medium": "🤔", "low": "🚫"}.get(conf, "•")
    if verdict["task"]:
        result = (f"{icon} <b>Збіг:</b> «{verdict['task']['theme_name'] or '(без тематики)'}» "
                  f"(id={verdict['task']['id']})\n"
                  f"Впевненість: <b>{conf}</b> → {action}")
    else:
        result = f"{icon} <b>Жодна тематика не підходить</b>\nВпевненість: {conf} → нічого не зараховувати"

    await msg.edit_text(
        f"{header}\n📋 Кандидати ({len(candidates)}):\n{cand_lines}\n\n"
        f"{result}\n💬 {verdict['reasoning']}\n\n"
        f"<i>Тест — нічого не зараховано.</i>",
        parse_mode="HTML", disable_web_page_preview=True,
    )
