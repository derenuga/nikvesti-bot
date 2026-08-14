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
   Фіксацією буває і ЧУЖА публікація (Олег, 14.08): ІМІ пише, що суд послався
   на матеріали МикВісті, — і саме чужа сторінка є доказом впливу. Тоді все,
   що бот звично бере з бази (id, автор, донор), у ній відсутнє: сторінка
   читається як текст, а нашу історію бот шукає трьома шляхами — лінки на нас
   із самої сторінки, наш матеріал, підказаний людиною в тому ж вікні
   («приложи ссылочку»), і пошук по норі за запитами, які складає модель.
   Третій шлях і є відповіддю на «на зовнішніх джерелах немає прямої
   гіперссилки на нас».
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
# Скільки тексту чужої сторінки читаємо: тригер описує подію, а не переказує
# нашу серію — на розуміння сюжету вистачає перших абзаців
EXTERNAL_TEXT_CHARS = 4000
# Скільки збігів бере кожен пошуковий запит по норі, коли лінка на нас немає
SEARCH_PER_QUERY = 8

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
    # Ключовість — ОКРЕМИЙ прапорець, а не роль: новина-фіксація (лінк, який
    # кинули в «+») цілком може бути і головним текстом серії. Поки key жив у
    # role, зірочку на фіксацію повісити було неможливо (Олег, 30.07: «в чем
    # прикол?» — прикол був у моделі). Міграція нижче переносить старі кейси.
    "ALTER TABLE impact_articles ADD COLUMN IF NOT EXISTS is_key BOOLEAN NOT NULL DEFAULT FALSE",
    "UPDATE impact_articles SET is_key = TRUE, role = 'series' WHERE role = 'key'",
    # Наш матеріал, підказаний людиною при заведенні кейсу з ЧУЖОГО джерела:
    # на сторонній сторінці лінка на нас може не бути взагалі, а зв'язок
    # людина знає. Живе в базі, бо «перезібрати заново» має бачити ту саму
    # підказку — інакше друга спроба слабша за першу.
    "ALTER TABLE impacts ADD COLUMN IF NOT EXISTS our_url TEXT",
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

def _fetch_soup(url):
    """BeautifulSoup живої сторінки. Читаємо БАЙТИ, а не resp.text: у
    imi.org.ua Content-Type приходить без charset, і requests за RFC вважає
    таку сторінку ISO-8859-1 — увесь український текст перетворювався на
    кракозябри (перевірено на живій сторінці 14.08). bs4 бере кодування з
    <meta> самої сторінки і не помиляється."""
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, timeout=20,
                        headers={"User-Agent": "Mozilla/5.0 (nikvesti-bot)"})
    resp.raise_for_status()
    return BeautifulSoup(resp.content, "html.parser")


def _body_node(soup):
    """Контейнер тіла статті; не знайшли — вся сторінка (зайве відсіється
    далі відсутністю id матеріалу)."""
    return (soup.find("article")
            or soup.find(class_=re.compile("article|content|news-text"))
            or soup)


# Обвіска, яку віддає get_text разом із текстом: кнопки поширення, теги,
# «читайте також». На живій сторінці imi.org.ua текст статті починався з
# «Поширити на Facebook Поширити у Telegram … Скопійовано!» — двісті символів
# сміття рівно там, де модель шукає, про що новина.
_NOISE_CLASS = re.compile(
    # «sharing-links» (саме так у imi.org.ua) під «share» не підпадає —
    # тому корінь, а не ціле слово
    "shar|social|breadcrumb|related|subscribe|tags|comment|banner|advert", re.I)


def _strip_noise(node):
    for tag in node.find_all(["script", "style", "noscript", "nav", "aside", "form"]):
        tag.decompose()
    for tag in node.find_all(class_=_NOISE_CLASS):
        tag.decompose()
    return node


def _our_links(soup, page_url):
    """Лінки на nikvesti.com із тіла сторінки, у порядку появи, без дублів.
    Відносні шляхи добудовуються від САМОЇ сторінки (urljoin), а не від
    nikvesti.com: на чужому сайті «/news/foo» — чужа новина, і сліпе
    приклеювання нашого домену робило б із неї наш неіснуючий матеріал."""
    from urllib.parse import urljoin, urlparse

    out, seen = [], set()
    for a in _body_node(soup).find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("?")[0].split("#")[0]
        if not urlparse(href).netloc.endswith("nikvesti.com"):
            continue
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


def _page_scrape(url):
    """(беклінки, og:image) з ЖИВОЇ сторінки матеріалу. Нора тримає чистий
    текст без розмітки, тож і передісторія-беклінки, і фото беруться лише зі
    сторінки. Збій → ([], None), не виняток: FTS-кандидати все одно будуть."""
    try:
        soup = _fetch_soup(url)
        og = soup.find("meta", property="og:image")
        image = (og.get("content") or "").strip() if og else None
        return _our_links(soup, url), image or None
    except Exception as e:
        print(f"impact: сторінка не зчиталась — {e}")
        return [], None


def _json_ld_date(soup):
    """datePublished зі schema.org-розмітки сторінки. Обходимо @graph
    рекурсивно: у WordPress-сайтів (imi.org.ua) дата лежить не в корені."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("datePublished", "dateCreated"):
                if isinstance(node.get(key), str):
                    found.append(node[key])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or "{}"))
        except (ValueError, TypeError):
            continue
    return found[0] if found else None


def _parse_ts(value):
    """ISO-дата → ts за Києвом. Без дати кейс не падає: він просто стане в
    архіві без року (і буде видимий за будь-якого фільтра)."""
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV_TZ)
    return int(dt.timestamp())


def _external_source(url):
    """Чужа публікація як тригер кейсу: заголовок, текст, дата, картинка і
    лінки на нас.

    Олег, 14.08: «сделай возможность добавить импакт по внешней ссылке» —
    ІМІ пише, що суд послався на наші матеріали, і саме ця чужа сторінка є
    фіксацією впливу. Наших id у ній немає взагалі, тож увесь звичний шлях
    (нора → БД сайту → автор → донор) тут не працює: сторінку доводиться
    читати як текст, а нашу передісторію шукати окремо."""
    from urllib.parse import urlparse

    try:
        soup = _fetch_soup(url)
    except Exception as e:
        raise ValueError(f"Сторінку {url} не вдалось прочитати — {e}")

    def _meta(*keys):
        for key in keys:
            tag = (soup.find("meta", property=key)
                   or soup.find("meta", attrs={"name": key}))
            if tag and (tag.get("content") or "").strip():
                return tag["content"].strip()
        return None

    title = _meta("og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = (h1.get_text(" ", strip=True) if h1
                 else (soup.title.get_text(strip=True) if soup.title else ""))
    # Чистимо ДО читання тексту й лінків: у прибраних блоках лінки чужі
    # (кнопки поширення), а нашу передісторію журналіст ставить у самому тексті
    body = _strip_noise(_body_node(soup))
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
    if not (title or "").strip() and not text:
        raise ValueError("На сторінці не знайшлось ні заголовка, ні тексту")
    published = _parse_ts(_json_ld_date(soup)
                          or _meta("article:published_time", "publish-date", "date"))
    return {
        "url": url,
        "title": (title or "").strip(),
        "text": text[:EXTERNAL_TEXT_CHARS],
        "image": _meta("og:image", "twitter:image"),
        "published": published,
        "site": urlparse(url).netloc.replace("www.", ""),
        "links": _our_links(soup, url),
    }


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


def _site_of(url):
    """Домен джерела для підпису «зовнішня публікація · imi.org.ua»."""
    from urllib.parse import urlparse

    return (urlparse(url or "").netloc or "").replace("www.", "") or None


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


def _mark_hinted(candidates, our_url):
    """Позначити матеріал, який ВКАЗАЛА людина. Для судді це найсильніший
    сигнал у всій таблиці: пошук може принести сусідній сюжет, беклінк —
    випадкову згадку, а редактор знає, з чого все почалось."""
    from handlers.helpers import extract_article_id

    aid = extract_article_id(our_url or "")
    if not aid:
        return candidates
    for c in candidates:
        if c["id"] == int(aid):
            c["hinted"] = True
    return candidates


def _candidate_rows(ordered_ids, from_backlink, extra_meta_ids=()):
    """Кандидати серії як список карток (заголовок, дата, автор, донор,
    витяг). Спільне для обох входів — нашої новини-фіксації і чужої
    публікації: далі суддя бачить однакову таблицю незалежно від того,
    звідки кейс почався."""
    from handlers.archive_search import get_excerpts

    ordered_ids = ordered_ids[:MAX_CANDIDATES]
    # get_excerpts віддає список — перекладаємо в мапу за id
    excerpts = {int(e["id"]): e.get("excerpt") or ""
                for e in get_excerpts(ordered_ids, max_chars=EXCERPT_CHARS)}
    meta = _site_meta(list(ordered_ids) + [int(i) for i in extra_meta_ids])

    rows = []
    for aid in ordered_ids:
        # беклінки — ручна праця журналістів, їх не кидаємо через дірку в
        # норі: добираємо з БД сайту (їх мало, ліміти MySQL не страждають)
        row = _nora_article(aid) or (
            _site_article(aid) if aid in from_backlink else None)
        if not row:
            continue
        published = int(row.get("published") or 0)
        rows.append({
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
    return rows, meta


def _search_our_archive(queries):
    """id матеріалів нори за пошуковими запитами. Запит ЗВУЖУЄТЬСЯ через AND
    (`_build_tsquery` склеює всі слова), тож довга фраза не знаходить нічого:
    якщо збігів немає — прибираємо останнє слово і пробуємо коротше. Слова
    планувальник ставить від найважливішого, тож відпадає хвіст."""
    from handlers.archive_search import search_items

    out, seen = [], set()
    for query in (queries or [])[:5]:
        words = [w for w in (query or "").split() if w]
        while words:
            try:
                items = search_items(" ".join(words), limit=SEARCH_PER_QUERY)
            except Exception as e:
                print(f"impact: пошук «{query}» не вдався — {e}")
                items = []
            if items:
                for it in items:
                    if int(it["id"]) not in seen:
                        seen.add(int(it["id"]))
                        out.append(int(it["id"]))
                break
            words = words[:-1]
    return out


def _collect_candidates(source_url, our_url=None):
    """(тригерна стаття, кандидати серії). Кандидати: беклінки зі сторінки
    (найцінніші — їх поставили самі журналісти) + FTS нори за заголовком."""
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

    # Підказаний людиною матеріал іде ПЕРШИМ і рівний беклінку: людина знає
    # свій сюжет краще за пошук
    if our_url:
        _take(our_url)
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
    for aid in _search_our_archive([title]):
        if aid != int(src_id) and aid not in from_backlink:
            ordered_ids.append(aid)

    candidates, meta = _candidate_rows(ordered_ids, from_backlink, [int(src_id)])
    _mark_hinted(candidates, our_url)
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


def _collect_external(page, our_url=None, queries=None):
    """(чужа публікація як тригер, кандидати серії з нори).

    Три джерела нашої історії, у порядку надійності:
    1) підказаний людиною наш матеріал — вона знає сюжет краще за будь-який
       пошук, і саме для цього поле в формі («дай мені можливість прикласти
       ссылочку», Олег 14.08);
    2) лінки на нас із самої чужої сторінки — їх поставив редактор чужого
       видання, тобто це вже підтверджений зв'язок. У кейсі ІМІ такий лінк
       рівно один і веде на нашу новину 2019 року — тобто самих беклінків
       мало: свіжого матеріалу, через який усе сталось, серед них немає;
    3) пошук по норі за запитами, які склав планувальник (нижче) — саме
       випадок «на зовнішньому джерелі немає прямої гіперссилки на нас».
    """
    from handlers.helpers import extract_article_id

    ordered_ids, from_backlink = [], set()

    def _take(href):
        aid = extract_article_id(href or "")
        if aid and int(aid) not in from_backlink:
            from_backlink.add(int(aid))
            ordered_ids.append(int(aid))

    if our_url:
        _take(our_url)
    for href in page.get("links") or []:
        _take(href)
    # Другий рівень — передісторія самих знайдених матеріалів (той самий
    # обмежувач, що й у внутрішньому зборі: це передісторія, а не павук)
    for aid in list(ordered_ids)[:5]:
        row = _nora_article(aid) or _site_article(aid)
        if not row:
            continue
        deeper, _img = _page_scrape(_nora_url(row))
        for href in deeper:
            _take(href)

    for aid in _search_our_archive(queries):
        if aid not in from_backlink:
            ordered_ids.append(aid)

    candidates, _meta = _candidate_rows(ordered_ids, from_backlink)
    _mark_hinted(candidates, our_url)
    published = int(page.get("published") or 0)
    trigger = {
        # id немає свідомо: чужа публікація не наш матеріал і в норі її бути
        # не може. Далі по цьому None і відрізняється зовнішній тригер
        "id": None,
        "external": True,
        "site": page.get("site"),
        "title": page.get("title") or page.get("url"),
        "url": page.get("url"),
        "published": published,
        "date": (datetime.fromtimestamp(published, KYIV_TZ).strftime("%d.%m.%Y")
                 if published else "—"),
        "text": page.get("text") or "",
        "image": page.get("image"),
        "authors": None, "project_id": None,
        "project_name": None, "partner_name": None,
    }
    return trigger, candidates


# ---------- Планувальник пошуку по норі ----------

_QUERIES_TOOL = {
    "name": "archive_queries",
    "description": "Запити для пошуку нашої передісторії в архіві МикВісті",
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array", "items": {"type": "string"},
                "description": "2–5 запитів по 1–4 слова, від найважливішого "
                               "слова до менш важливого",
            },
            "about": {"type": "string", "description":
                      "Одним реченням: про що має бути наш матеріал"},
        },
        "required": ["queries"],
    },
}


async def _plan_queries(page, essence=None, our_url=None):
    """Пошукові запити по норі за текстом чужої публікації.

    Навіщо модель, а не заголовок: пошук нори склеює всі слова через AND
    (`_build_tsquery`), тож заголовок «Суд послався на матеріали Nikcenter і
    „МикВісті“ в ухвалі щодо земель військових полігонів» не знайде нічого —
    у ньому половина слів про сам факт цитування, а не про сюжет. Треба
    вибрати з тексту ВЛАСНІ назви й предмет справи («землі оборони»,
    «Золотакевич», «Кульбакине»), і саме це модель робить дешево.

    Збій моделі — не привід валити кейс: лишається заголовок як запит."""
    from handlers.ai_messages import FOX_MODEL_FAST, async_client

    fallback = [(page.get("title") or "").strip()]
    prompt = f"""Чуже видання ({page.get('site')}) написало про результат, до якого
причетна редакція МикВісті (Миколаїв). Треба знайти В НАШОМУ архіві матеріали
цього ж сюжету — ті, що були ДО цієї публікації.

ЧУЖА ПУБЛІКАЦІЯ:
{page.get('title')}
{(page.get('text') or '')[:2500]}
{f'Суть словами редактора: {essence}' if essence else ''}
{f'Редактор уже вказав наш матеріал: {our_url}' if our_url else ''}

Склади запити для повнотекстового пошуку через archive_queries.
Правила, без яких пошук не знайде нічого:
- пошук склеює слова через І, тому кожен запит — 1–4 слова, не речення;
- бери ВЛАСНІ назви й предмет справи (прізвища, топоніми, назви установ,
  підприємств, об'єктів), а не слова про сам факт розголосу («суд послався»,
  «журналісти написали», «розслідування ЗМІ»);
- назви наших чи чужих медіа («МикВісті», «Nikcenter», «ІМІ») в запити НЕ
  йдуть: у своєму архіві ми себе не шукаємо;
- запити мають бути різні за кутом: один про об'єкт, один про людину, один
  про орган влади — якщо один нічого не дасть, спрацює інший."""
    try:
        message = await async_client.messages.create(
            model=FOX_MODEL_FAST,
            max_tokens=500,
            tools=[_QUERIES_TOOL],
            tool_choice={"type": "tool", "name": "archive_queries"},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_usage(FOX_MODEL_FAST, message)
        for block in message.content:
            if block.type == "tool_use" and block.name == "archive_queries":
                queries = [q.strip() for q in (block.input.get("queries") or [])
                           if (q or "").strip()]
                return queries or fallback
    except Exception as e:
        print(f"impact: планувальник запитів не спрацював — {e}")
    return fallback


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
                               "велике розслідування/стаття, а не новина-фіксація. "
                               "0, якщо серія порожня"},
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
    external = bool(trigger.get("external"))
    lines = []
    for c in candidates:
        marks = []
        if c.get("hinted"):
            marks.append("РЕДАКТОР вказав цей матеріал як наш по цій темі")
        if c["backlink"]:
            marks.append("лінк із тригерної публікації" if external
                         else "беклінк із тригерної новини")
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
    if external:
        # Тригер — ЧУЖА публікація. Головна пастка: модель бере її як «нашу
        # новину» і пише кейс про те, що зробило чуже видання, або записує
        # медальку його авторам. Тому і рамка, і заборони — прямим текстом.
        head = f"""ТРИГЕР — публікація ЧУЖОГО видання ({trigger.get('site') or 'зовнішнє джерело'},
{trigger['date']}), у якій зафіксовано результат нашої роботи:
{trigger['title']}
{trigger['text']}

Це НЕ наш матеріал: у серію він не входить, його авторів у credits немає."""
        rules = """- серія — ЛИШЕ матеріали МикВісті з переліку кандидатів, і лише ті, що справді про цей сюжет;
- якщо серед кандидатів немає матеріалів цього сюжету — поверни порожню серію (series_ids: []) і напиши наратив тільки з чужої публікації; порожньо чесніше, ніж чужий сюжет у кейсі;
- what_happened має називати, ХТО саме зафіксував результат (видання, суд, орган) і ЯКІ наші тексти цьому передували;
- credits — тільки наші журналісти, автори матеріалів серії."""
    else:
        head = f"""ТРИГЕР — наша новина, що зафіксувала результат ({trigger['date']}, автор: {trigger['authors'] or '—'}):
{trigger['title']}
{trigger['text']}"""
        rules = """- у серію бери ЛИШЕ матеріали цього сюжету; тригер у серію не включай — він додасться сам;
- credits: усі дотичні журналісти з поміткою ЩО саме зробив кожен; авторів ключового тексту назви першими. Автор тригера потрапляє в credits лише як «зафіксував(ла) результат», якщо не робив більшого."""
    return f"""Редакція МикВісті збирає імпакт-кейс для донорського звіту.

{head}
{essence_line}

КАНДИДАТИ в серію (наші матеріали, що можуть бути передісторією):
{corpus}

Збери кейс через impact_case:
{rules}
- key_article_id — той ОДИН текст, що зробив найбільше (розслідування, велика стаття, перший викривальний матеріал), не новина-фіксація;
- what_happened і significance — стримано і фактично, як у звіті донору: без пафосу, без «унікальний», лише те, що є в текстах; хронологія і причинність мають читатись."""


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


def _record_usage(model, message):
    """Токени виклику — в /aicost. Спільне для судді й планувальника запитів:
    вони з різних моделей, а рахунок один."""
    try:
        u = message.usage
        storage.record_ai_usage(
            model,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception as e:
        print(f"ai_usage: не записався impact — {e}")


async def _run_judge(trigger, candidates, essence):
    from handlers.ai_messages import FOX_MODEL_SMART, async_client

    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=STORY_MAX_TOKENS,
        tools=[_IMPACT_TOOL],
        tool_choice={"type": "tool", "name": "impact_case"},
        messages=[{"role": "user", "content": _judge_prompt(trigger, candidates, essence)}],
    )
    _record_usage(FOX_MODEL_SMART, message)
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

def create_impact(actor, source_url, essence, our_url=None):
    """Створює чернетку 'building' і повертає її id — сам збір іде окремо
    (build_impact), щоб HTTP-запит апки не висів пів хвилини.

    our_url — наш матеріал, підказаний людиною. Живе в базі, а не тільки в
    пам'яті прогону, бо «перезібрати заново» має збирати з тією ж підказкою:
    інакше друга спроба була б слабшою за першу."""
    ensure_impact_schema()
    rows = bot_db.query(
        "INSERT INTO impacts (essence, source_url, our_url, created_by) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ((essence or "").strip() or None, source_url.strip(),
         (our_url or "").strip() or None, actor),
    )
    return rows[0]["id"]


def is_our_url(url):
    """Чи це лінк на конкретний матеріал nikvesti.com. Усе інше вважається
    зовнішнім джерелом і читається як чужа сторінка."""
    from handlers.helpers import extract_article_id

    return "nikvesti.com" in (url or "") and bool(extract_article_id(url or ""))


async def build_impact(impact_id):
    """Повний збір кейсу. Помилки не летять нагору — лягають у status/error,
    апка покаже і дасть «спробувати ще»."""
    ensure_impact_schema()

    def _load():
        with bot_db.session():
            rows = bot_db.query(
                "SELECT id, source_url, our_url, essence FROM impacts WHERE id = %s",
                (int(impact_id),))
            return rows[0] if rows else None

    imp = await asyncio.to_thread(_load)
    if not imp:
        return

    try:
        source_url = imp["source_url"]
        our_url = (imp.get("our_url") or "").strip() or None
        if is_our_url(source_url):
            trigger, candidates = await asyncio.to_thread(
                _collect_candidates, source_url, our_url)
        else:
            if "nikvesti.com" in source_url:
                raise ValueError("Це лінк на nikvesti.com, але без номера "
                                 "матеріалу — дай лінк на саму новину")
            # Чуже джерело: спершу читаємо сторінку, потім складаємо запити
            # по норі (мережа й БД — у потоках, модель — асинхронно)
            page = await asyncio.to_thread(_external_source, source_url)
            queries = await _plan_queries(page, imp["essence"], our_url)
            trigger, candidates = await asyncio.to_thread(
                _collect_external, page, our_url, queries)
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
        # У зовнішнього тригера id немає, і без серії ключового просто нема:
        # без цієї перевірки None == None робив би ключовою чужу публікацію
        if key_id is None:
            key_id = -1

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
                rows = [(trigger, "fixer")] + [(s, "series") for s in series]
                for art, role in rows:
                    bot_db.execute(
                        "INSERT INTO impact_articles (impact_id, article_id, url, "
                        "title, published, role, is_key, authors, project_id, "
                        "project_name, partner_name) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (impact_id, url) DO NOTHING",
                        (int(impact_id), art["id"], art["url"], art["title"],
                         art["published"], role, art["id"] == key_id,
                         art.get("authors"), art.get("project_id"),
                         art.get("project_name"), art.get("partner_name")),
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
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT a.partner_name), NULL) AS partners,
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
        # Донори — СПИСКОМ, а не склеєним рядком: за ними апка фільтрує архів
        # («покажи все, що зробили для IWPR»), а розбирати назву назад із
        # роздільника означало б розвалити будь-яку назву, де він трапиться
        "partners": list(r["partners"] or []),
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
        "our_url": r.get("our_url"),
        # article_id порожній рівно в одному випадку — це чужа публікація
        # (наш матеріал без id у базу не потрапляє), тож окремої колонки-
        # прапорця не заводимо: він був би другим джерелом тієї самої правди
        "articles": [{
            "id": a["id"], "article_id": a["article_id"], "url": a["url"],
            "title": a["title"],
            "date": (datetime.fromtimestamp(int(a["published"]), KYIV_TZ)
                     .strftime("%d.%m.%Y") if a["published"] else "—"),
            "role": a["role"], "is_key": bool(a.get("is_key")),
            "authors": a["authors"],
            "project_name": a["project_name"], "partner_name": a["partner_name"],
            "external": not a["article_id"],
            "source": _site_of(a["url"]) if not a["article_id"] else None,
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
    людиною, не за суддею. Фіксація теж може бути ключовою — це незалежні
    позначки (Олег, 30.07)."""
    ensure_impact_schema()
    with bot_db.transaction():
        bot_db.execute(
            "UPDATE impact_articles SET is_key = FALSE WHERE impact_id = %s",
            (int(impact_id),))
        return bot_db.execute(
            "UPDATE impact_articles SET is_key = TRUE "
            "WHERE impact_id = %s AND id = %s",
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
    key = next((a for a in imp["articles"] if a.get("is_key")), None)
    fixer = next((a for a in imp["articles"] if a["role"] == "fixer"), None)
    links = "\n".join(
        f'<li><a href="{_esc(a["url"])}">{_esc(a["title"] or a["url"])}</a>'
        f'<span class="m"> — {_esc(a["date"])}'
        # для донора це найцінніший рядок списку: результат зафіксували не ми
        # самі, а стороннє видання — тож джерело називається прямо
        f'{" · публікація " + _esc(a["source"] or "стороннього видання") if a.get("external") else ""}'
        f'{" · " + _esc(a["authors"]) if a["authors"] else ""}'
        f'{" · ключовий матеріал" if a.get("is_key") else ""}'
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
