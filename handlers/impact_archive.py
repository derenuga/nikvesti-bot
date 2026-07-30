"""
Імпакт-архів — журнал впливу редакції (Mini App «Команда», Нора).

Олег, 29.07: «мені постійно важко шукати і наново описувати імпакти при кожній
заявці по грант». Кейс завжди один і той самий: у чат кидають «після нас
відремонтували дорогу», через пів року донор просить impact case — і хтось
годину відновлює, з чого все почалось, які тексти були в серії і хто їх писав.

Механіка (як просив Олег):

1. Менеджер відкриває Імпакт-архів, тисне «+», кидає URL новини-фіксації
   («Дорогу до Матвіївки відремонтували після скарг») і, за бажанням, суть
   своїми словами («після нас відремонтували, реакція на новину»).
2. Далі бот САМ:
   - читає живу сторінку матеріалу і збирає беклінки з тексту — передісторія
     зазвичай уже злінкована самими журналістами;
   - шукає в норі споріднені матеріали (той самий FTS, що бек і /dossier);
   - для кожного кандидата дивиться в БД сайту авторів і ПРОЄКТ
     (nodes.partner_project → partner) — щоб знати, якому донору цей імпакт
     можна потім показати: «ви нас фінансували — ось що змінив текст за вашої
     підтримки»;
   - Claude складає донорський заголовок, наратив («що сталось» + «значення та
     вплив» — за зразком реального кейсу про відбудову трьох будинків),
     відбирає СЕРІЮ текстів, називає КЛЮЧОВИЙ (той один, що все змінив, — його
     вага найбільша, і саме його проєктність цікавить донора) і пропонує,
     кому записати медальку.
3. Все це — ЧЕРНЕТКА: серію можна підрізати, ключовий перепризначити, імена
   поправити. Автор новини-фіксації часто НЕ автор розслідування — тому
   медальки пропонує AI, а підтверджує людина.
4. Готовий кейс віддається файлом .html у приват (стилізований, друкується в
   PDF з браузера) — щоб класти в заявку або слати донору.

Побудова асинхронна: POST створює запис зі status='building' і повертає
одразу, апка полить. Збій будь-якого кроку → status='failed' з причиною і
кнопкою «спробувати ще» (редеплой посеред білда лишає 'building' — лікується
тією ж кнопкою).
"""

import asyncio
import json
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from handlers import bot_db, db, storage

KYIV_TZ = ZoneInfo("Europe/Kiev")
BASE_URL = "https://nikvesti.com"

# Скільки кандидатів максимум віддаємо судді (беклінки + FTS разом): більше —
# це вже не серія, а тема, і суддя починає тягти в кейс сусідні сюжети
MAX_CANDIDATES = 24
# Скільки матеріалів максимум у збереженій серії
MAX_SERIES = 12
EXCERPT_CHARS = 600
STORY_MAX_TOKENS = 2500

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS impacts (
        id         BIGSERIAL PRIMARY KEY,
        title      TEXT,
        essence    TEXT,
        story      TEXT,
        status     TEXT NOT NULL DEFAULT 'building',
        error      TEXT,
        source_url TEXT,
        created_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Серія текстів кейсу. role: fixer — новина-фіксація результату (з неї
    # кейс почали), key — той один текст, що все змінив, series — решта.
    # Снапшоти (назва, автори, проєкт, донор) свідомо ДЕНОРМАЛІЗОВАНІ: кейс —
    # це документ для донора, він не має тихо мінятись, коли в CMS перейменують
    # проєкт чи переприв'яжуть автора.
    """
    CREATE TABLE IF NOT EXISTS impact_articles (
        id           BIGSERIAL PRIMARY KEY,
        impact_id    BIGINT NOT NULL REFERENCES impacts(id) ON DELETE CASCADE,
        article_id   BIGINT,
        url          TEXT NOT NULL,
        title        TEXT,
        published    BIGINT,
        role         TEXT NOT NULL DEFAULT 'series',
        authors      TEXT,
        project_id   BIGINT,
        project_name TEXT,
        partner_name TEXT,
        UNIQUE (impact_id, url)
    )
    """,
    # Медальки: кому зараховано кейс. Пропонує AI, підтверджує людина — бо
    # автор новини-фіксації часто просто зафіксував чужу піврічну роботу.
    """
    CREATE TABLE IF NOT EXISTS impact_credits (
        id        BIGSERIAL PRIMARY KEY,
        impact_id BIGINT NOT NULL REFERENCES impacts(id) ON DELETE CASCADE,
        person    TEXT NOT NULL,
        note      TEXT,
        UNIQUE (impact_id, person)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_impact_articles_impact "
    "ON impact_articles (impact_id, published)",
    # Фото кейсу — og:image новини-фіксації. Тримаємо URL, а не байти: картинки
    # роздає сайт (як і в картках черги матчингу), а Нора — не файлосховище.
    # Зникне картинка на сайті — картка деградує в текстову, нічого не ламається.
    "ALTER TABLE impacts ADD COLUMN IF NOT EXISTS image TEXT",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_impact_schema():
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        with bot_db.session():
            for sql in _SCHEMA_STATEMENTS:
                bot_db.execute(sql)
            # Самолікування: підчистити події-медальки кейсів, яких уже немає.
            # Каскад подій зʼявився пізніше за перші видалення (реальний кейс
            # 29.07: Олег видалив кейс у вікні деплою каскаду), тож привиди
            # могли лишитись. Ідемпотентно і дешево — раз на старт процесу.
            try:
                from handlers import team_notifications
                team_notifications.ensure_notifications_schema()
                bot_db.execute(
                    "DELETE FROM team_notification_reads WHERE notification_id IN "
                    "(SELECT id FROM team_notifications WHERE kind = 'impact_credit' "
                    " AND object_id NOT IN (SELECT id::text FROM impacts))")
                bot_db.execute(
                    "DELETE FROM team_notifications WHERE kind = 'impact_credit' "
                    "AND object_id NOT IN (SELECT id::text FROM impacts)")
            except Exception as e:
                print(f"impact: підчистка подій-сиріт не вдалась — {e}")
        _schema_done = True


# ---------- Збір кандидатів ----------

def _page_scrape(url):
    """(беклінки, og:image) з ЖИВОЇ сторінки матеріалу. Нора тримає чистий
    текст без розмітки, тож і передісторія-беклінки, і фото беруться лише зі
    сторінки. Збій → ([], None), не виняток: FTS-кандидати все одно будуть."""
    try:
        import requests
        from bs4 import BeautifulSoup

        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (nikvesti-bot)"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.find("meta", property="og:image")
        image = (og.get("content") or "").strip() if og else None
        # лише лінки з тіла статті; якщо контейнер не знайшли — з усієї
        # сторінки, зайве відсіється відсутністю id матеріалу
        body = soup.find("article") or soup.find(class_=re.compile("article|content|news-text")) or soup
        out = []
        for a in body.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = BASE_URL + href
            if "nikvesti.com" not in href:
                continue
            out.append(href.split("?")[0].split("#")[0])
        # порядок збережено, дублі геть
        seen, uniq = set(), []
        for h in out:
            if h not in seen:
                seen.add(h)
                uniq.append(h)
        return uniq, image or None
    except Exception as e:
        print(f"impact: сторінка не зчиталась — {e}")
        return [], None


def _nora_article(article_id):
    rows = bot_db.query(
        "SELECT id, published, own_material, owner_id, kind, title_ua, title_ru, "
        "slug, category, text_ua, text_ru FROM articles WHERE id = %s",
        (int(article_id),),
    )
    return rows[0] if rows else None


def _site_article(article_id):
    """Фолбек: матеріал прямо з БД сайту, у формі рядка нори. Потрібен, коли
    в норі матеріалу (ще) немає: статті історично не дзеркалились (виправлено
    30.07, але старі доїдуть лише бекфілом), а свіжа новина чекає годинний
    синк. Кейс через це падати не має — БД сайту знає все."""
    if not db.is_configured():
        return None
    try:
        rows = db.query(
            "SELECT id, published, own_material, owner_id, type, title_ua, title, "
            "slug_ua, slug, category, content_ua, content FROM nodes "
            "WHERE id = %s AND status = 1", (int(article_id),))
    except Exception as e:
        print(f"impact: фолбек у БД сайту не вдався — {e}")
        return None
    if not rows:
        return None
    from handlers.archive_mirror import html_to_text

    r = rows[0]
    return {
        "id": r["id"], "published": r.get("published"),
        "own_material": r.get("own_material"), "owner_id": r.get("owner_id"),
        "kind": (r.get("type") or "news"),
        "title_ua": r.get("title_ua"), "title_ru": r.get("title"),
        "slug": (r.get("slug_ua") or r.get("slug") or "").strip() or None,
        "category": r.get("category"),
        "text_ua": html_to_text(r.get("content_ua")),
        "text_ru": html_to_text(r.get("content")),
    }


def _nora_url(row):
    tail = (row.get("slug") or "").strip() or str(row["id"])
    if (row.get("kind") or "news") == "article":
        return f"{BASE_URL}/articles/{tail}"
    cat = (row.get("category") or "").strip()
    return f"{BASE_URL}/news/{cat}/{tail}" if cat else f"{BASE_URL}/news/{tail}"


def _site_meta(article_ids):
    """{node_id: {authors, project_id, project_name, partner_name}} одним
    заходом у MySQL. Проєкт тут — головна причина всієї таблиці: донора, який
    профінансував ключовий текст, потім можна порадувати кейсом."""
    ids = [int(i) for i in article_ids if i]
    if not ids or not db.is_configured():
        return {}
    placeholders = ",".join(["%s"] * len(ids))
    try:
        rows = db.query(
            f"""
            SELECT n.id, n.partner_project,
                   TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS author,
                   pp.name_ua AS project_name, p.name_ua AS partner_name
            FROM nodes n
            LEFT JOIN users u ON u.id = n.owner_id
            LEFT JOIN partner_project pp ON pp.id = n.partner_project
            LEFT JOIN partner p ON p.id = pp.partner_id
            WHERE n.id IN ({placeholders})
            """,
            tuple(ids),
        )
    except Exception as e:
        print(f"impact: метадані з БД сайту не приїхали — {e}")
        return {}
    return {
        int(r["id"]): {
            "authors": (r["author"] or "").strip() or None,
            "project_id": r["partner_project"] or None,
            "project_name": (r.get("project_name") or "").strip() or None,
            "partner_name": (r.get("partner_name") or "").strip() or None,
        }
        for r in rows
    }


def _collect_candidates(source_url):
    """(тригерна стаття, кандидати серії). Кандидати: беклінки зі сторінки
    (найцінніші — їх поставили самі журналісти) + FTS нори за заголовком."""
    from handlers.archive_search import get_excerpts, search_items
    from handlers.helpers import extract_article_id

    src_id = extract_article_id(source_url)
    if not src_id:
        raise ValueError("Не впізнав URL — треба лінк на матеріал nikvesti.com")
    src = _nora_article(src_id) or _site_article(src_id)
    if not src:
        raise ValueError(f"Матеріалу {src_id} не знайшов ні в норі, ні в БД "
                         "сайту — перевір лінк")

    links, image = _page_scrape(source_url)
    ordered_ids, from_backlink = [], set()

    def _take(href):
        aid = extract_article_id(href)
        if aid and int(aid) != int(src_id) and int(aid) not in from_backlink:
            from_backlink.add(int(aid))
            ordered_ids.append(int(aid))

    for href in links:
        _take(href)
    # Другий рівень: сторінки перших беклінків. Реальний кейс 30.07 — ключова
    # стаття про автошколу була злінкована не з новини-фіксації, а з середини
    # ланцюжка, і серія її не побачила (FTS теж ні: статті ще без бекфілу).
    # Обмежено п'ятьма сторінками — це передісторія, а не павук.
    first_level = list(ordered_ids)
    for aid in first_level[:5]:
        row = _nora_article(aid) or _site_article(aid)
        if not row:
            continue
        deeper, _img = _page_scrape(_nora_url(row))
        for href in deeper:
            _take(href)

    title = (src.get("title_ua") or src.get("title_ru") or "").strip()
    for it in search_items(title, limit=MAX_CANDIDATES):
        if int(it["id"]) != int(src_id) and int(it["id"]) not in from_backlink:
            ordered_ids.append(int(it["id"]))

    ordered_ids = ordered_ids[:MAX_CANDIDATES]
    # get_excerpts віддає список — перекладаємо в мапу за id
    excerpts = {int(e["id"]): e.get("excerpt") or ""
                for e in get_excerpts(ordered_ids, max_chars=EXCERPT_CHARS)}
    meta = _site_meta(ordered_ids + [int(src_id)])

    candidates = []
    for aid in ordered_ids:
        # беклінки — ручна праця журналістів, їх не кидаємо через дірку в
        # норі: добираємо з БД сайту (їх мало, ліміти MySQL не страждають)
        row = _nora_article(aid) or (
            _site_article(aid) if aid in from_backlink else None)
        if not row:
            continue
        published = int(row.get("published") or 0)
        candidates.append({
            "id": aid,
            "title": (row.get("title_ua") or row.get("title_ru") or "").strip(),
            "url": _nora_url(row),
            "published": published,
            "date": datetime.fromtimestamp(published, KYIV_TZ).strftime("%d.%m.%Y") if published else "—",
            "own": bool(row.get("own_material")),
            "backlink": aid in from_backlink,
            "excerpt": excerpts.get(aid)
                or ((row.get("text_ua") or row.get("text_ru") or "")[:EXCERPT_CHARS]),
            **(meta.get(aid) or {"authors": None, "project_id": None,
                                 "project_name": None, "partner_name": None}),
        })

    src_published = int(src.get("published") or 0)
    trigger = {
        "id": int(src_id),
        "title": title,
        "url": _nora_url(src),
        "published": src_published,
        "date": datetime.fromtimestamp(src_published, KYIV_TZ).strftime("%d.%m.%Y") if src_published else "—",
        "text": ((src.get("text_ua") or src.get("text_ru") or "")[:4000]),
        "image": image,
        **(meta.get(int(src_id)) or {"authors": None, "project_id": None,
                                     "project_name": None, "partner_name": None}),
    }
    return trigger, candidates


# ---------- Суддя-упорядник (Claude) ----------

_IMPACT_TOOL = {
    "name": "impact_case",
    "description": "Зібраний імпакт-кейс для донорського звіту",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description":
                      "Заголовок кейсу в донорському стилі, українською, без лапок навколо"},
            "what_happened": {"type": "string", "description":
                              "1 абзац: що висвітлили і що змінилось. Лише факти з наданих текстів"},
            "significance": {"type": "string", "description":
                             "1 абзац «Значення та вплив»: чому це приклад впливу журналістики"},
            "series_ids": {"type": "array", "items": {"type": "integer"}, "description":
                           "id матеріалів серії у хронологічному порядку — ЛИШЕ ті, що "
                           "справді про цей сюжет; сусідні теми не тягнути"},
            "key_article_id": {"type": "integer", "description":
                               "id ОДНОГО тексту з series_ids, який зробив найбільше — зазвичай "
                               "велике розслідування/стаття, а не новина-фіксація"},
            "credits": {"type": "array", "items": {"type": "object", "properties": {
                "person": {"type": "string"},
                "note": {"type": "string", "description":
                         "за що: «вела серію, авторка ключового тексту» / «зафіксувала реакцію»"},
            }, "required": ["person"]}, "description":
                "Кому записати кейс. Вага різна: автор розслідування — не те саме, "
                "що автор новини-фіксації; але згадати варто всіх дотичних"},
        },
        "required": ["title", "what_happened", "significance", "series_ids",
                     "key_article_id", "credits"],
    },
}


def _judge_prompt(trigger, candidates, essence):
    lines = []
    for c in candidates:
        marks = []
        if c["backlink"]:
            marks.append("беклінк із тригерної новини")
        if c["own"]:
            marks.append("власний матеріал")
        if c["partner_name"]:
            marks.append(f"проєкт: {c['project_name']} ({c['partner_name']})")
        lines.append(
            f"- id={c['id']} · {c['date']} · {c['title']}\n"
            f"  автор: {c['authors'] or '—'}{(' · ' + ' · '.join(marks)) if marks else ''}\n"
            f"  {c['excerpt'][:EXCERPT_CHARS]}"
        )
    corpus = "\n".join(lines) or "(кандидатів не знайшлось)"
    essence_line = f"\nСуть імпакту словами редактора: {essence}" if essence else ""
    return f"""Редакція МикВісті збирає імпакт-кейс для донорського звіту.

ТРИГЕР — новина, що зафіксувала результат ({trigger['date']}, автор: {trigger['authors'] or '—'}):
{trigger['title']}
{trigger['text']}
{essence_line}

КАНДИДАТИ в серію (наші матеріали, що можуть бути передісторією):
{corpus}

Збери кейс через impact_case:
- у серію бери ЛИШЕ матеріали цього сюжету; тригер у серію не включай — він додасться сам;
- key_article_id — той ОДИН текст, що зробив найбільше (розслідування, велика стаття, перший викривальний матеріал), не новина-фіксація;
- what_happened і significance — стримано і фактично, як у звіті донору: без пафосу, без «унікальний», лише те, що є в текстах; хронологія і причинність мають читатись;
- credits: усі дотичні журналісти з поміткою ЩО саме зробив кожен; авторів ключового тексту назви першими. Автор тригера потрапляє в credits лише як «зафіксував(ла) результат», якщо не робив більшого."""


def _split_leak(text):
    """(чистий текст, хвіст-витік). Sonnet зрідка «прошиває» межу поля і пише
    псевдо-XML виклику інструмента прямо у значення: реальний кейс 29.07 —
    «…підзвітності.</significance> <parameter name="series_ids">[321487,
    321494]» у полі significance, а сам параметр series_ids порожній. Ріжемо
    по першому тегу; хвіст повертаємо окремо — з нього ще можна врятувати
    загублені id серії."""
    m = re.search(r"</?[a-zA-Z_][^>]*>", text or "")
    if not m:
        return (text or "").strip(), ""
    return text[:m.start()].strip(), text[m.start():]


def _delouse_verdict(verdict, known_ids):
    """Чистить поля вердикту від витоку розмітки і повертає (вердикт,
    рятівні series_ids з хвостів). Рятуємо лише числа, які є серед відомих
    кандидатів — суми з наративу за id не зійдуть."""
    rescued = []
    for field in ("title", "what_happened", "significance"):
        clean, leak = _split_leak(verdict.get(field))
        verdict[field] = clean
        for num in re.findall(r"\d{4,9}", leak):
            if int(num) in known_ids and int(num) not in rescued:
                rescued.append(int(num))
    return verdict, rescued


async def _run_judge(trigger, candidates, essence):
    from handlers.ai_messages import FOX_MODEL_SMART, async_client

    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=STORY_MAX_TOKENS,
        tools=[_IMPACT_TOOL],
        tool_choice={"type": "tool", "name": "impact_case"},
        messages=[{"role": "user", "content": _judge_prompt(trigger, candidates, essence)}],
    )
    try:
        u = message.usage
        storage.record_ai_usage(
            FOX_MODEL_SMART,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception as e:
        print(f"ai_usage: не записався impact — {e}")
    for block in message.content:
        if block.type == "tool_use" and block.name == "impact_case":
            return block.input
    raise RuntimeError("Суддя не віддав impact_case")


def _notify_credit(impact_id, impact_title, person, note):
    """Медалька в стрічку «Події» людини (Олег, 29.07: «я зарахував імпакт
    Аліні — їй має прийти це у події»). dedup_key тримає «раз на кейс на
    людину»: перезбір серії чи повторне додавання не смикає вдруге.

    Тихо пропускає людей поза ростером: суддя пише імена як у users сайту,
    і колишні чи позаштатні автори в апку не заходять — сповіщення в нікуди
    не потрібне, а медалька в кейсі однаково лишається."""
    from handlers import team_notifications, team_roster

    if person not in team_roster.ROSTER:
        return
    team_notifications.notify_safe(
        "impact_credit",
        impact_title or "Імпакт-кейс",
        audience=team_notifications.AUDIENCE_PERSON,
        person=person,
        body=note,
        object_type="impact",
        object_id=str(impact_id),
        dedup_key=f"impact_credit:{impact_id}:{person}",
    )


# ---------- Публічне API модуля ----------

def create_impact(actor, source_url, essence):
    """Створює чернетку 'building' і повертає її id — сам збір іде окремо
    (build_impact), щоб HTTP-запит апки не висів пів хвилини."""
    ensure_impact_schema()
    rows = bot_db.query(
        "INSERT INTO impacts (essence, source_url, created_by) "
        "VALUES (%s, %s, %s) RETURNING id",
        ((essence or "").strip() or None, source_url.strip(), actor),
    )
    return rows[0]["id"]


async def build_impact(impact_id):
    """Повний збір кейсу. Помилки не летять нагору — лягають у status/error,
    апка покаже і дасть «спробувати ще»."""
    ensure_impact_schema()

    def _load():
        with bot_db.session():
            rows = bot_db.query(
                "SELECT id, source_url, essence FROM impacts WHERE id = %s",
                (int(impact_id),))
            return rows[0] if rows else None

    imp = await asyncio.to_thread(_load)
    if not imp:
        return

    try:
        trigger, candidates = await asyncio.to_thread(
            _collect_candidates, imp["source_url"])
        verdict = await _run_judge(trigger, candidates, imp["essence"])

        by_id = {c["id"]: c for c in candidates}
        verdict, rescued = _delouse_verdict(verdict, set(by_id))
        series_ids = list(verdict.get("series_ids") or [])
        if not series_ids and rescued:
            # серія втекла в текст разом із розміткою — беремо врятовані id
            series_ids = rescued
        series = [by_id[i] for i in series_ids if i in by_id]
        series = series[:MAX_SERIES]
        key_id = verdict.get("key_article_id")
        if key_id not in {s["id"] for s in series}:
            # суддя назвав ключовим щось поза серією (буває) — тоді ключовим
            # стає найстарший матеріал серії, а без серії — сам тригер
            key_id = series[0]["id"] if series else trigger["id"]

        story = json.dumps({
            "what_happened": (verdict.get("what_happened") or "").strip(),
            "significance": (verdict.get("significance") or "").strip(),
        }, ensure_ascii=False)

        def _save():
            with bot_db.transaction():
                bot_db.execute(
                    "UPDATE impacts SET title = %s, story = %s, image = %s, "
                    "status = 'ready', error = NULL, updated_at = now() WHERE id = %s",
                    ((verdict.get("title") or trigger["title"]).strip(),
                     story, trigger.get("image"), int(impact_id)),
                )
                # перезбір починає серію з нуля — інакше «спробувати ще»
                # подвоювало б рядки
                bot_db.execute(
                    "DELETE FROM impact_articles WHERE impact_id = %s", (int(impact_id),))
                bot_db.execute(
                    "DELETE FROM impact_credits WHERE impact_id = %s", (int(impact_id),))
                rows = [(trigger, "fixer")] + [
                    (s, "key" if s["id"] == key_id else "series") for s in series]
                for art, role in rows:
                    bot_db.execute(
                        "INSERT INTO impact_articles (impact_id, article_id, url, "
                        "title, published, role, authors, project_id, project_name, partner_name) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (impact_id, url) DO NOTHING",
                        (int(impact_id), art["id"], art["url"], art["title"],
                         art["published"], role, art.get("authors"),
                         art.get("project_id"), art.get("project_name"),
                         art.get("partner_name")),
                    )
                impact_title = (verdict.get("title") or trigger["title"]).strip()
                for cr in (verdict.get("credits") or [])[:10]:
                    person = (cr.get("person") or "").strip()
                    if not person:
                        continue
                    note = (cr.get("note") or "").strip() or None
                    bot_db.execute(
                        "INSERT INTO impact_credits (impact_id, person, note) "
                        "VALUES (%s, %s, %s) ON CONFLICT (impact_id, person) DO NOTHING",
                        (int(impact_id), person, note),
                    )
                    _notify_credit(impact_id, impact_title, person, note)

        await asyncio.to_thread(_save)
    except Exception as e:
        msg = str(e)[:400]
        print(f"impact: збір кейсу {impact_id} впав — {msg}")

        def _fail():
            with bot_db.session():
                bot_db.execute(
                    "UPDATE impacts SET status = 'failed', error = %s, "
                    "updated_at = now() WHERE id = %s",
                    (msg, int(impact_id)))

        await asyncio.to_thread(_fail)


def retry_impact(impact_id):
    """Повертає кейс у 'building' перед повторним збором (кнопка в апці —
    вона ж рятує і від редеплою посеред білда)."""
    ensure_impact_schema()
    return bot_db.execute(
        "UPDATE impacts SET status = 'building', error = NULL, "
        "updated_at = now() WHERE id = %s", (int(impact_id),))


def _story(row):
    try:
        return json.loads(row["story"]) if row.get("story") else {}
    except (ValueError, TypeError):
        return {}


def list_impacts():
    """Сортування — за ДАТОЮ ІМПАКТУ (published новини-фіксації), не за
    порядком заведення: архів наповнюється старими кейсами впереміш, і
    «створено вчора» для імпакту дворічної давності перемішувало б історію.
    Кейси, що ще збираються (фіксації немає), — згори: вони чекають дії."""
    ensure_impact_schema()
    rows = bot_db.query(
        """
        SELECT i.id, i.title, i.essence, i.status, i.error, i.source_url,
               i.created_by, i.created_at, i.image,
               COUNT(DISTINCT a.id) AS articles,
               STRING_AGG(DISTINCT a.partner_name, ' · ') AS partners,
               STRING_AGG(DISTINCT c.person, '|') AS people,
               MAX(a.published) FILTER (WHERE a.role = 'fixer') AS fixed_ts
        FROM impacts i
        LEFT JOIN impact_articles a ON a.impact_id = i.id
        LEFT JOIN impact_credits c ON c.impact_id = i.id
        GROUP BY i.id
        ORDER BY fixed_ts DESC NULLS FIRST, i.id DESC
        """)
    return [{
        "id": r["id"],
        "title": r["title"],
        "essence": r["essence"],
        "status": r["status"],
        "error": r["error"],
        "source_url": r["source_url"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "date": (datetime.fromtimestamp(int(r["fixed_ts"]), KYIV_TZ)
                 .strftime("%d.%m.%Y") if r["fixed_ts"] else None),
        "articles": int(r["articles"] or 0),
        "partners": r["partners"] or None,
        "image": r["image"] or None,
        "people": [p for p in (r["people"] or "").split("|") if p],
    } for r in rows]


def list_impacts_for(person):
    """Кейси, у яких людина має медальку (лише готові) — блок «Мої імпакти»
    в її інтерфейсі. Віддає і нотатку «за що» — це і є текст медальки."""
    ensure_impact_schema()
    rows = bot_db.query(
        """
        SELECT i.id, i.title, i.created_at, i.image, c.note,
               (SELECT COUNT(*) FROM impact_articles a WHERE a.impact_id = i.id) AS articles,
               (SELECT MAX(a.published) FROM impact_articles a
                 WHERE a.impact_id = i.id AND a.role = 'fixer') AS fixed_ts
        FROM impacts i
        JOIN impact_credits c ON c.impact_id = i.id AND c.person = %s
        WHERE i.status = 'ready'
        ORDER BY fixed_ts DESC NULLS LAST, i.id DESC
        """, (person,))
    return [{
        "id": r["id"], "title": r["title"], "note": r["note"],
        "image": r["image"] or None,
        "articles": int(r["articles"] or 0),
        "date": (datetime.fromtimestamp(int(r["fixed_ts"]), KYIV_TZ)
                 .strftime("%d.%m.%Y") if r["fixed_ts"] else None),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]


def person_credited(impact_id, person):
    """Чи має людина медальку в кейсі — пропуск журналістки до читання."""
    ensure_impact_schema()
    return bool(bot_db.query(
        "SELECT 1 FROM impact_credits WHERE impact_id = %s AND person = %s",
        (int(impact_id), person)))


def get_impact(impact_id):
    ensure_impact_schema()
    rows = bot_db.query("SELECT * FROM impacts WHERE id = %s", (int(impact_id),))
    if not rows:
        return None
    r = rows[0]
    story = _story(r)
    arts = bot_db.query(
        "SELECT * FROM impact_articles WHERE impact_id = %s "
        "ORDER BY published NULLS LAST, id", (int(impact_id),))
    credits = bot_db.query(
        "SELECT id, person, note FROM impact_credits WHERE impact_id = %s ORDER BY id",
        (int(impact_id),))
    return {
        "id": r["id"],
        "title": r["title"],
        "essence": r["essence"],
        "status": r["status"],
        "error": r["error"],
        "source_url": r["source_url"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "what_happened": story.get("what_happened") or "",
        "significance": story.get("significance") or "",
        "articles": [{
            "id": a["id"], "article_id": a["article_id"], "url": a["url"],
            "title": a["title"],
            "date": (datetime.fromtimestamp(int(a["published"]), KYIV_TZ)
                     .strftime("%d.%m.%Y") if a["published"] else "—"),
            "role": a["role"], "authors": a["authors"],
            "project_name": a["project_name"], "partner_name": a["partner_name"],
        } for a in arts],
        "credits": [{"id": c["id"], "person": c["person"], "note": c["note"]}
                    for c in credits],
    }


def update_impact(impact_id, title=None, essence=None,
                  what_happened=None, significance=None):
    ensure_impact_schema()
    imp = get_impact(impact_id)
    if not imp:
        return None
    sets, params = ["updated_at = now()"], []
    if title is not None and title.strip():
        sets.append("title = %s")
        params.append(title.strip()[:300])
    if essence is not None:
        sets.append("essence = %s")
        params.append(essence.strip()[:600] or None)
    if what_happened is not None or significance is not None:
        story = json.dumps({
            "what_happened": (what_happened if what_happened is not None
                              else imp["what_happened"]).strip(),
            "significance": (significance if significance is not None
                             else imp["significance"]).strip(),
        }, ensure_ascii=False)
        sets.append("story = %s")
        params.append(story)
    params.append(int(impact_id))
    bot_db.execute(f"UPDATE impacts SET {', '.join(sets)} WHERE id = %s", tuple(params))
    return get_impact(impact_id)


def add_article(impact_id, url):
    """Додати матеріал у серію руками. Рятівний вхід, коли збір не побачив
    текст (стаття поза норою, беклінка немає): людина знає свій ключовий
    матеріал краще за будь-який пошук. Метадані (автор, проєкт, донор) —
    ті самі, що при автозборі."""
    from handlers.helpers import extract_article_id

    ensure_impact_schema()
    aid = extract_article_id(url or "")
    if not aid:
        return None, "Не впізнав URL — треба лінк на матеріал nikvesti.com"
    row = _nora_article(aid) or _site_article(aid)
    if not row:
        return None, f"Матеріалу {aid} не знайшов ні в норі, ні в БД сайту"
    meta = _site_meta([int(aid)]).get(int(aid)) or {}
    bot_db.execute(
        "INSERT INTO impact_articles (impact_id, article_id, url, title, "
        "published, role, authors, project_id, project_name, partner_name) "
        "VALUES (%s, %s, %s, %s, %s, 'series', %s, %s, %s, %s) "
        "ON CONFLICT (impact_id, url) DO NOTHING",
        (int(impact_id), int(aid), _nora_url(row),
         (row.get("title_ua") or row.get("title_ru") or "").strip(),
         int(row.get("published") or 0), meta.get("authors"),
         meta.get("project_id"), meta.get("project_name"),
         meta.get("partner_name")),
    )
    return True, None


def set_key_article(impact_id, row_id):
    """Перепризначити ключовий текст: вага в серії різна, і останнє слово за
    людиною, не за суддею."""
    ensure_impact_schema()
    with bot_db.transaction():
        bot_db.execute(
            "UPDATE impact_articles SET role = 'series' "
            "WHERE impact_id = %s AND role = 'key'", (int(impact_id),))
        return bot_db.execute(
            "UPDATE impact_articles SET role = 'key' "
            "WHERE impact_id = %s AND id = %s AND role <> 'fixer'",
            (int(impact_id), int(row_id)))


def remove_article(impact_id, row_id):
    ensure_impact_schema()
    return bot_db.execute(
        "DELETE FROM impact_articles WHERE impact_id = %s AND id = %s AND role <> 'fixer'",
        (int(impact_id), int(row_id)))


def add_credit(impact_id, person, note=None):
    ensure_impact_schema()
    person = (person or "").strip()[:120]
    if not person:
        return None
    note = (note or "").strip()[:200] or None
    bot_db.execute(
        "INSERT INTO impact_credits (impact_id, person, note) VALUES (%s, %s, %s) "
        "ON CONFLICT (impact_id, person) DO UPDATE SET note = EXCLUDED.note",
        (int(impact_id), person, note))
    imp = bot_db.query("SELECT title FROM impacts WHERE id = %s", (int(impact_id),))
    _notify_credit(impact_id, imp[0]["title"] if imp else None, person, note)
    return True


def remove_credit(impact_id, credit_id):
    """Зняти медальку — разом із її подією в «Подіях» людини. Інакше в стрічці
    висів би привид: «імпакт за твоєї участі», якого в «Моїх імпактах» немає.
    Якщо медальку потім повернуть, подія прийде заново — і це правильно."""
    ensure_impact_schema()
    rows = bot_db.query(
        "SELECT person FROM impact_credits WHERE impact_id = %s AND id = %s",
        (int(impact_id), int(credit_id)))
    with bot_db.transaction():
        deleted = bot_db.execute(
            "DELETE FROM impact_credits WHERE impact_id = %s AND id = %s",
            (int(impact_id), int(credit_id)))
        if rows:
            _drop_credit_events(impact_id, rows[0]["person"])
    return deleted


def _drop_credit_events(impact_id, person=None):
    """Прибирає події impact_credit кейсу (однієї людини або всіх) разом із
    позначками прочитання — сироти в reads нікому не заважають, але й не
    потрібні."""
    from handlers import team_notifications

    # на свіжій базі таблиць сповіщень могло ще не бути
    team_notifications.ensure_notifications_schema()
    if person is not None:
        cond, params = "dedup_key = %s", (f"impact_credit:{impact_id}:{person}",)
    else:
        cond, params = ("object_type = 'impact' AND object_id = %s",
                        (str(int(impact_id)),))
    bot_db.execute(
        f"DELETE FROM team_notification_reads WHERE notification_id IN "
        f"(SELECT id FROM team_notifications WHERE {cond})", params)
    bot_db.execute(f"DELETE FROM team_notifications WHERE {cond}", params)


def delete_impact(impact_id):
    """Видалення кейсу тягне за собою і його події-медальки: серія і медальки
    падають каскадом БД, а сповіщення живуть в іншій таблиці без FK — без
    цього кроку в Аліни лишалась би медалька про кейс, якого немає."""
    ensure_impact_schema()
    with bot_db.transaction():
        _drop_credit_events(impact_id)
        return bot_db.execute("DELETE FROM impacts WHERE id = %s", (int(impact_id),))


# ---------- Експорт ----------

def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_html(impact_id):
    """Самодостатній HTML-файл кейсу: друкується в PDF із браузера, шлеться
    донору як є. Стилі інлайном — файл живе окремо від апки."""
    imp = get_impact(impact_id)
    if not imp or imp["status"] != "ready":
        return None, None
    key = next((a for a in imp["articles"] if a["role"] == "key"), None)
    fixer = next((a for a in imp["articles"] if a["role"] == "fixer"), None)
    links = "\n".join(
        f'<li><a href="{_esc(a["url"])}">{_esc(a["title"] or a["url"])}</a>'
        f'<span class="m"> — {_esc(a["date"])}'
        f'{" · " + _esc(a["authors"]) if a["authors"] else ""}'
        f'{" · ключовий матеріал" if a["role"] == "key" else ""}'
        f'{" · " + _esc(a["partner_name"]) if a["partner_name"] else ""}</span></li>'
        for a in imp["articles"])
    credits = ", ".join(
        f'{_esc(c["person"])}{" (" + _esc(c["note"]) + ")" if c["note"] else ""}'
        for c in imp["credits"])
    donor = key and key["partner_name"]
    donor_line = (f'<p class="donor">Ключовий матеріал вийшов у межах проєкту '
                  f'«{_esc(key["project_name"])}» за підтримки {_esc(donor)}.</p>'
                  if donor else "")
    html = f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(imp["title"])}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; color: #16283c;
         max-width: 760px; margin: 40px auto; padding: 0 20px; line-height: 1.55; }}
  h1 {{ font-size: 26px; line-height: 1.25; }}
  h2 {{ font-size: 17px; margin-top: 28px; }}
  .brand {{ font-family: -apple-system, Arial, sans-serif; font-size: 13px;
            color: #6a7f96; letter-spacing: .04em; text-transform: uppercase; }}
  .m {{ color: #6a7f96; font-size: 14px; }}
  .donor {{ background: #f3f7fc; border-left: 3px solid #1f6fd6;
            padding: 10px 14px; border-radius: 6px; }}
  ul {{ padding-left: 20px; }} li {{ margin-bottom: 8px; }}
  a {{ color: #1f6fd6; }}
  @media print {{ body {{ margin: 0; }} }}
</style></head><body>
<div class="brand">МикВісті · nikvesti.com · імпакт-кейс</div>
<h1>{_esc(imp["title"])}</h1>
<p>{_esc(imp["what_happened"])}</p>
<h2>Значення та вплив</h2>
<p>{_esc(imp["significance"])}</p>
{donor_line}
<h2>Матеріали серії</h2>
<ul>{links}</ul>
{f'<h2>Команда</h2><p>{credits}</p>' if credits else ''}
<p class="m">Зафіксовано {_esc(fixer["date"] if fixer else "")} · архів впливу МикВісті</p>
</body></html>"""
    fname = f"impact-{impact_id}.html"
    return fname, html
