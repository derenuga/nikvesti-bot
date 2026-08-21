"""
Команда /post <лінк на пост> — метрики САМЕ ЦЬОГО поста в соцмережі.

Зворотний бік /stat. Там дають лінк матеріалу, і бот шукає, де ми його
постили; тут дають лінк ПОСТА, і бот має спершу впізнати, який це наш об'єкт
у Graph API. Без цього кроку метрик не буде взагалі: перегляди, охоплення,
поширення й збереження віддає тільки Graph і тільки на об'єкт, який ми
адмініструємо — з самої публічної сторінки не читається нічого.

ЧОМУ ЦЕ НЕ ОДИН ЗАПИТ. Половина лінків, якими діляться в чаті, id об'єкта не
містить узагалі. Заміряно на живих лінках Олега 21.08.2026, із серверної
адреси (не з ноутбука — це різні відповіді):

  • `facebook.com/share/p/1AmpyiGUfk/` → 302 на
    `facebook.com/nikvesti/posts/pfbid02nabJowuvNoET…?rdid=…` — тобто шортлінк
    просто ХОВАЄ pfbid, а не несе інший ідентифікатор. Отже редирект треба
    пройти, але сам по собі він нічого не вирішує;
  • сторінка pfbid відповідає HTTP 400 (!), АЛЕ в тілі відповіді лежать
    справжні `og:description` (текст поста, ~250 символів) і `og:image`.
    Facebook малює логін-волл, УЖЕ розв'язавши pfbid у себе, і лишає нам рівно
    те, за чим пост можна впізнати у власній стрічці. Тому 400 тут не помилка,
    і тіло читається попри код;
  • pfbid не розкодовується: це непрозорий токен, а не кодування id. Спокуса
    «взяти найдовше число зі сторінки» — пастка: число 1509803480261504 лежить
    у КОЖНІЙ такій сторінці однаково (перевірено на трьох різних постах), тобто
    це чужий лічильник, а не наш пост;
  • `og:type` бреше: на фото-пості про тендер Facebook віддав `video.other`.
    Тип беремо з вкладень об'єкта в Graph, а не зі сторінки;
  • `graph.facebook.com/oembed_post` без токена віддає лише заглушку з тим
    самим href — жодного id. Тупик, перевірено;
  • `instagram.com/<нік>/p/<shortcode>/` віддає нашому серверу порожню
    оболонку без жодного og. З інсти зі сторінки не витягти НІЧОГО — і не
    треба: shortcode стоїть у полі `permalink` кожного нашого медіа, тобто
    зіставляється точно й без здогадів;
  • shortcode розкодовується в 19-значне число (`DcEW0bgDE2U` →
    3964393931957620116), але це pk внутрішнього API, а НЕ id Graph API
    (там 17-18 знаків). Тому декодування тут свідомо не використовується —
    воно дало б впевнений на вигляд, але чужий ідентифікатор.

ЗВІДСИ СХОДИНКИ. Кожна наступна дорожча за попередню, і на кожній видно, чим
саме впізнали (поле `method` — та сама діагностика, що в /stat):

  1. `direct`  — id є прямо в лінку (`/posts/123`, `story_fbid=`, `/reel/123`,
     `?fbid=`). Один запит, точно.
  2. `photo`   — pfbid: id фото з `og:image` → `page_story_id` фото. Один
     запит, і відповідь САМА себе перевіряє: page_story_id має початись з
     нашого page_id, інакше сходинка не зарахована.
  3. `nora`    — текст поста з `og:description` шукаємо в норі повнотекстово,
     беремо дату матеріалу і дивимось стрічку саме біля неї. Дві-три сторінки
     замість десятка.
  4. `feed`    — сліпий прохід стрічки від найновіших, із межею FEED_SCAN_PAGES.
     Наша сторінка дає 15-20 постів на добу, тобто 12 сторінок ≈ два місяці.

Не впізнали — так і кажемо, разом із текстом поста, який усе-таки прочитали:
знати, ПРО ЩО пост, і не знати цифр — чесніше, ніж мовчазне «не знайдено».

Межа модуля: рахуються лише НАШІ сторінки (@nikvesti у Facebook та Instagram).
Чужий пост метрик не має і мати не може — Graph API віддає інсайти лише
адміністратору сторінки, а публічна сторінка конкурента нашому серверу
показує логін-волл. Тому чужий лінк відсікається одразу й на словах, а не
після десяти запитів у порожнечу.
"""

import asyncio
import html as html_mod
import os
import re
import requests
import time
from itertools import islice
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

from handlers import instagram
from handlers import stat_instagram
from handlers.facebook import (
    FACEBOOK_PAGE_SLUG, POST_LIST_FIELDS, fix_permalink,
    graph_get as _graph, get_post_metrics as _get_post_metrics,
    get_reel_insights,
)
from handlers.stat import _fb_date, _get_reel_views, _post_keys

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

# Скільки сторінок стрічки гортати сліпим проходом. 100 постів на сторінку,
# 15-20 постів на добу → 12 сторінок ≈ два місяці. Глибше сліпо не лізем:
# запити коштують часу, а для давнього поста є сходинка `nora`, яка приводить
# одразу до потрібного тижня.
FEED_SCAN_PAGES = 12
# Стільки ж для інсти. Там 2-5 дописів на добу, тож 100 медіа ≈ місяць,
# а 12 сторінок покривають більше року.
MEDIA_SCAN_PAGES = 12

# Вікно навколо дати матеріалу для сходинки `nora`: пост зазвичай виходить у
# день публікації, але буває й наступного дня, і за день до (анонс).
NORA_WINDOW_DAYS = 3

# Поріг збігу тексту поста з og:description. Скоринг спільний зі /stat
# (stat_instagram.make_scorer) — той самий стемер і та сама арифметика, тож
# розійтись вони можуть лише разом. Поріг вищий за ACCEPT інсти (0.50): там
# підпис переписують своїми словами, а тут ми порівнюємо текст поста САМ ІЗ
# СОБОЮ, просто обрізаний фейсбуком, і законний збіг близький до одиниці.
TEXT_MATCH_MIN = 0.60

BROWSER_HEADERS = {
    # Facebook віддає og-теги лише «браузерному» запиту; чесний бот-UA дістає
    # голий логін-волл без жодного og (перевірено 21.08.2026).
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}

FB_HOSTS = ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com",
            "mbasic.facebook.com", "fb.com", "www.fb.com", "fb.watch", "www.fb.watch")
IG_HOSTS = ("instagram.com", "www.instagram.com", "instagr.am", "www.instagr.am")

# Shortcode Instagram: base64url, 5-30 символів. Обмеження знизу відсікає
# службові шматки шляху ('p', 'tv'), зверху — сміття.
IG_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,30}$")


# ---------- Розбір лінка ----------

def _host(url):
    return (urlparse(url).netloc or "").lower()


def is_post_link(url):
    """Чи це взагалі лінк на пост соцмережі (а не на матеріал nikvesti.com).
    Потрібне /stat, щоб мовчки віддати такий лінк сюди замість «вкажіть
    посилання на матеріал»."""
    return parse_link(url) is not None


def parse_link(url):
    """URL → опис лінка або None, якщо це не пост FB/IG.

    Ключі: net ('fb'|'ig'), object_id (готовий id Graph, якщо він у лінку),
    kind ('post'|'video'|'photo'), shortcode (IG), shortlink (треба пройти
    редирект), pfbid (треба впізнавати за вмістом), owner (нік сторінки з
    лінка, щоб одразу відсікти чужий пост)."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    host = _host(url)
    if host in FB_HOSTS:
        return _parse_fb(url)
    if host in IG_HOSTS:
        return _parse_ig(url)
    return None


def _parse_fb(url):
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    qs = parse_qs(parts.query)
    segs = [s for s in path.split("/") if s]
    low = [s.lower() for s in segs]

    # fb.watch/CODE і facebook.com/share/{p,v,r}/CODE — шортлінки: id немає,
    # треба пройти редирект (він, як заміряно, веде на pfbid)
    if _host(url).endswith("fb.watch") and segs:
        return {"net": "fb", "shortlink": True, "url": url}
    if low[:1] == ["share"]:
        return {"net": "fb", "shortlink": True, "url": url}

    # permalink.php / story.php: story_fbid + id сторінки — найточніший вигляд
    story = (qs.get("story_fbid") or [None])[0]
    owner = (qs.get("id") or [None])[0]
    if story and story.isdigit():
        oid = f"{owner}_{story}" if (owner or "").isdigit() else story
        return {"net": "fb", "object_id": oid, "kind": "post", "url": url}
    if story and story.startswith("pfbid"):
        return {"net": "fb", "pfbid": story, "url": url, "owner": owner}

    # /watch/?v=123, /photo/?fbid=123, /photo.php?fbid=123
    vid = (qs.get("v") or [None])[0]
    if vid and vid.isdigit():
        return {"net": "fb", "object_id": vid, "kind": "video", "url": url}
    fbid = (qs.get("fbid") or [None])[0]
    if fbid and fbid.isdigit():
        return {"net": "fb", "object_id": fbid, "kind": "photo", "url": url}

    # /reel/123, /videos/123, /<нік>/videos/<опис>/123, /<нік>/posts/123|pfbid…,
    # /<нік>/photos/<альбом>/123
    for anchor, kind in (("reel", "video"), ("reels", "video"), ("videos", "video"),
                         ("posts", "post"), ("photos", "photo"), ("photo", "photo")):
        if anchor not in low:
            continue
        i = low.index(anchor)
        owner = segs[i - 1] if i > 0 else None
        tail = segs[i + 1:]
        for seg in reversed(tail):
            if seg.isdigit():
                return {"net": "fb", "object_id": seg, "kind": kind,
                        "url": url, "owner": owner}
            if seg.startswith("pfbid"):
                return {"net": "fb", "pfbid": seg, "url": url, "owner": owner}
        # anchor є, а хвіст нечитабельний — хай іде як невпізнаний пост
        return {"net": "fb", "url": url, "owner": owner}
    return None


def _parse_ig(url):
    parts = urlparse(url)
    segs = [s for s in parts.path.split("/") if s]
    low = [s.lower() for s in segs]
    for anchor in ("p", "reel", "reels", "tv"):
        if anchor not in low:
            continue
        i = low.index(anchor)
        # /nikvesti/p/CODE — нік стоїть ПЕРЕД якорем; /p/CODE — ніка немає
        owner = segs[i - 1] if i > 0 and low[i - 1] != "share" else None
        if i + 1 < len(segs) and IG_SHORTCODE_RE.match(segs[i + 1]):
            code = segs[i + 1]
            if low[:1] == ["share"]:
                # instagram.com/share/p/CODE — код шортлінка, НЕ shortcode
                return {"net": "ig", "shortlink": True, "url": url}
            return {"net": "ig", "shortcode": code, "url": url, "owner": owner}
        return {"net": "ig", "url": url, "owner": owner}
    if low[:1] == ["share"]:
        return {"net": "ig", "shortlink": True, "url": url}
    return None


def foreign_owner(link):
    """Нік зі лінка, якщо це ЧУЖА сторінка. None — наша або нік не вказаний.
    Дає відсікти чужий пост словами, а не десятком марних запитів у порожнечу."""
    owner = ((link or {}).get("owner") or "").strip().lower()
    if not owner:
        return None
    ours = {FACEBOOK_PAGE_SLUG.lower(), "nikvesti", "profile.php"}
    for extra in (FACEBOOK_PAGE_ID, instagram.INSTAGRAM_USER_ID):
        if extra:
            ours.add(str(extra).lower())
    return None if owner in ours else owner


# ---------- Сторінка поста: редирект + og ----------

def fetch_page(url):
    """Сторінку поста читаємо ОДНИМ запитом: він і проходить редирект
    шортлінка, і приносить og-теги. Тіло беремо навіть при HTTP 400 — саме так
    Facebook віддає og на pfbid-адресах (заміряно 21.08.2026).

    Один тихий повтор, якщо og не приїхали: на кількох запитах поспіль
    Facebook підсовує сторінку «Sorry, something went wrong» — не помилку
    доступу, а тимчасове притримування. Заміряно того ж дня: та сама адреса,
    що двічі віддала og, на третьому запиті дала цю заглушку. Повторюємо один
    раз — далі це вже справді «не читається»."""
    last = ("", "")
    for attempt in range(2):
        if attempt:
            time.sleep(1.5)
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=25,
                                allow_redirects=True)
        except Exception as e:
            print(f"post_stat: {url} — {e}")
            continue
        last = (resp.url, resp.text or "")
        if parse_og(last[1]).get("description"):
            return last
    return last


_OG_RE = re.compile(
    r'<meta\s+property=["\']og:(?P<key>[a-z:]+)["\']\s+content=["\'](?P<val>.*?)["\']',
    re.IGNORECASE | re.DOTALL)


def parse_og(html):
    """og-теги сторінки → dict. Значення розекрановані (Facebook віддає
    кирилицю HTML-ентитями: &#x41c;&#x438; …), інакше жоден текст не
    зіставиться."""
    out = {}
    for m in _OG_RE.finditer(html or ""):
        key = m.group("key").lower()
        if key not in out:
            out[key] = html_mod.unescape(m.group("val")).strip()
    return out


def image_object_ids(og_image):
    """Числові id з імені файлу og:image. Формат fbcdn — «…/775298449_1726257
    996169341_2084948746_n.jpg», і котрий із них вузол фото, наперед не
    відомо. Тому віддаємо всі кандидати: перевірка безкоштовна й САМА себе
    підтверджує (page_story_id має початись із нашого page_id), тож помилитись
    тут неможливо — можна лише не вгадати й піти далі."""
    if not og_image:
        return []
    name = urlparse(og_image).path.rsplit("/", 1)[-1]
    ids = re.findall(r"\d{10,}", name)
    # найдовші першими: id фото довші за службові числа розміру
    return sorted(dict.fromkeys(ids), key=len, reverse=True)[:4]


# ---------- Facebook: впізнавання об'єкта ----------

def _full_id(object_id):
    """Короткий id поста → `{page}_{post}`. Уже складений або id відео/фото
    лишаємо як є."""
    oid = str(object_id)
    if "_" in oid or not FACEBOOK_PAGE_ID:
        return oid
    return f"{FACEBOOK_PAGE_ID}_{oid}"


FB_OBJECT_FIELDS = "id,message,story,permalink_url,created_time,attachments{media_type,target}"
# У вузла ВІДЕО немає ні message, ні attachments — запит із ними Graph
# відхиляє цілком, і рілз за голим id читався б як «не існує». Тому другий,
# вужчий набір: підпис відео лежить у description.
FB_VIDEO_FIELDS = "id,description,permalink_url,created_time"


def _read_object(object_id):
    """Об'єкт Graph за id: пробуємо і `{page}_{id}`, і голий id, і обидва
    набори полів. Пост живе під складеним id і має message, рілз та відео —
    під голим і з description; з лінка не завжди видно, що саме прийшло."""
    tried = []
    for oid in dict.fromkeys([_full_id(object_id), str(object_id)]):
        for fields in (FB_OBJECT_FIELDS, FB_VIDEO_FIELDS):
            data, err = _graph(oid, {"fields": fields})
            if data and data.get("id"):
                return data, None
            tried.append(err or "порожня відповідь")
    return None, tried[0] if tried else "не прочиталось"


def _story_id_from_photo(photo_id):
    """Фото → пост, у якому воно лежить (`page_story_id`). Відповідь
    перевіряємо на належність НАШІЙ сторінці — інакше це чуже фото."""
    data, err = _graph(str(photo_id), {"fields": "page_story_id"})
    if err or not data:
        return None
    story = str(data.get("page_story_id") or "")
    if FACEBOOK_PAGE_ID and story.startswith(f"{FACEBOOK_PAGE_ID}_"):
        return story
    return None


def _iter_feed_pages(since_ts=None, until_ts=None, max_pages=FEED_SCAN_PAGES):
    """Сторінки стрічки від найновіших, лінивo. Генератор, а не список: щойно
    пост знайшовся, гортання спиняється — сліпий прохід інакше коштував би всі
    12 запитів завжди."""
    url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/posts"
    params = {"fields": POST_LIST_FIELDS, "limit": 100,
              "access_token": FACEBOOK_PAGE_TOKEN}
    if since_ts:
        params["since"] = since_ts
    if until_ts:
        params["until"] = until_ts
    for _ in range(max_pages):
        try:
            data = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print(f"post_stat: стрічка постів — {e}")
            return
        if "error" in data:
            print(f"post_stat: стрічка постів — {data['error'].get('message')}")
            return
        yield data.get("data", [])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        url, params = next_url, None


def _post_text(post):
    """Текст об'єкта. Три поля, бо їх заповнюють різні типи: message — пости,
    story — службові («сторінка змінила фото»), description — відео й рілзи."""
    return " ".join(x for x in (post.get("message"), post.get("story"),
                                post.get("description")) if x).strip()


def find_in_feed(og, pages):
    """Пост у стрічці за вмістом сторінки pfbid. Два незалежні сигнали:

    - id фото з og:image серед id вкладень поста — точний збіг, без порогів;
    - текст: og:description проти тексту поста, скорингом /stat.

    `pages` — ітерабельне зі сторінками стрічки (генератор, який спиняється,
    щойно пост знайшовся). Параметром, а не всередині, свідомо: так матчинг
    перевіряється на живих сторінках БЕЗ мережі й токена.

    Повертає (post, method, scanned). Текстовий шлях бере НАЙКРАЩИЙ збіг
    сторінки, а не перший над порогом: сусідні пости про ту саму подію схожі,
    і «перший, що пройшов» — це лотерея."""
    img_ids = set(image_object_ids(og.get("image")))
    score = stat_instagram.make_scorer(
        {"title": "", "lead": og.get("description") or ""})
    scanned = 0
    best, best_score = None, 0.0
    for page in pages:
        for post in page:
            scanned += 1
            if img_ids and (_post_keys(post) & img_ids):
                return post, "photo", scanned
            if score.tokens:
                s = score(_post_text(post))
                if s > best_score:
                    best, best_score = post, s
        if best_score >= TEXT_MATCH_MIN:
            return best, "text", scanned
    if best_score >= TEXT_MATCH_MIN:
        return best, "text", scanned
    return None, None, scanned


def _nora_dates(text):
    """Дати матеріалів нори, схожих на текст поста. Пост зазвичай переказує лід
    статті, тож повнотекстовий пошук приводить до потрібного тижня одним
    безкоштовним запитом — замість десятка сторінок стрічки."""
    if not text:
        return []
    try:
        from handlers import archive_search
        items = archive_search.search_items(text[:300], limit=3)
    except Exception as e:
        print(f"post_stat: нора не відповіла — {e}")
        return []
    out = []
    for it in items:
        ts = it.get("published")
        if ts:
            out.append(datetime.fromtimestamp(int(ts)))
    return out


def resolve_facebook(link, og=None):
    """Лінк → (object_id, method, diag). method — якою сходинкою впізнали."""
    diag = {"scanned": 0}
    if link.get("object_id"):
        return link["object_id"], "direct", diag

    og = og or {}
    # Сходинка 2: фото з og:image знає свій пост
    for pid in image_object_ids(og.get("image")):
        story = _story_id_from_photo(pid)
        if story:
            return story, "photo", diag

    text = og.get("description") or ""
    if not text and not og.get("image"):
        # Сторінку не прочитали (Facebook притримав запити або лінк мертвий) —
        # упізнавати нічим. Сліпий прохід тут коштував би дванадцять запитів і
        # гарантовано нічого не дав би: у стрічці немає з чим порівнювати.
        return None, None, diag

    # Сходинка 3: дві найновіші сторінки стрічки — ≈10-12 днів нашого темпу.
    # Стоять ПЕРЕД норою свідомо: лінк, який кидають у чат, майже завжди
    # свіжий, і тоді відповідь коштує ОДИН запит замість трьох. Генератор
    # спільний із сходинкою 5 — там він продовжиться з місця зупинки, тож
    # межа в FEED_SCAN_PAGES лишається спільною, а не подвоюється.
    feed = _iter_feed_pages()
    post, _, scanned = find_in_feed(og, islice(feed, 2))
    diag["scanned"] += scanned
    if post:
        return post["id"], "feed", diag

    # Сходинка 4: нора дає дату матеріалу → дивимось стрічку саме біля неї.
    # Дві найкращі здогадки, не всі: кожна коштує до трьох запитів, а хибна
    # здогадка нічого не дасть і на десятій.
    for dt in _nora_dates(text)[:2]:
        since = int((dt - timedelta(days=NORA_WINDOW_DAYS)).timestamp())
        until = int((dt + timedelta(days=NORA_WINDOW_DAYS)).timestamp())
        post, _, scanned = find_in_feed(og, _iter_feed_pages(since, until, max_pages=3))
        diag["scanned"] += scanned
        if post:
            return post["id"], "nora", diag

    # Сходинка 5: решта стрічки — той самий генератор, з місця зупинки
    post, _, scanned = find_in_feed(og, feed)
    diag["scanned"] += scanned
    if post:
        return post["id"], "feed", diag
    return None, None, diag


def facebook_post_stat(link, og=None):
    """Метрики поста Facebook. Повертає dict як у /stat (type/permalink/date/
    views/…) плюс text і method, або {'error': …} з людською причиною."""
    object_id, method, diag = resolve_facebook(link, og)
    if not object_id:
        return {"error": "not_found", "scanned": diag.get("scanned", 0)}

    data, err = _read_object(object_id)
    if not data:
        return {"error": "graph", "detail": err, "scanned": diag.get("scanned", 0)}

    oid = str(data["id"])
    attachments = (data.get("attachments", {}) or {}).get("data", [])
    media_type = (attachments[0].get("media_type") if attachments else "") or ""
    # Тип беремо з ДВОХ джерел: вкладення (для постів) і сам лінк (для голого
    # `/reel/123` вкладень немає взагалі). og:type сюди не входить свідомо —
    # він назвав `video.other` фото-пост про тендер.
    is_video = "video" in media_type.lower() or link.get("kind") == "video"

    metrics = _get_post_metrics(oid)
    views = metrics["views"]
    reactions, comments, shares = (metrics["reactions"], metrics["comments"],
                                   metrics["shares"])
    # Відео-пост: перегляди живуть у video_insights, а не в post_media_view.
    # Ідемо туди лише коли звичайний лічильник мовчить — зайвих запитів не треба.
    if is_video and views is None:
        # У поста-відео лічильник живе на ВКЛАДЕННІ, у голого рілза — на ньому
        # самому
        target = ((attachments[0].get("target", {}) or {}).get("id")
                  if attachments else oid)
        if target:
            views = _get_reel_views(target)
            r, c, s = get_reel_insights(target)
            reactions = reactions if reactions is not None else r
            comments = comments if comments is not None else c
            shares = shares if shares is not None else s

    permalink = fix_permalink(data.get("permalink_url") or "")
    if not permalink:
        short = oid.split("_")[-1]
        permalink = f"https://www.facebook.com/{FACEBOOK_PAGE_SLUG}/posts/{short}"
    return {
        "net": "fb",
        "type": "reel" if is_video else "post",
        "id": oid,
        "permalink": permalink,
        "date": _fb_date(data.get("created_time")),
        "text": _post_text(data),
        "views": views,
        "reactions": reactions,
        "comments": comments,
        "shares": shares,
        "eng_note": metrics.get("note"),
        "method": method,
        "scanned": diag.get("scanned", 0),
    }


# ---------- Instagram ----------

def _iter_media_pages(max_pages=MEDIA_SCAN_PAGES):
    url = f"https://graph.instagram.com/v21.0/{instagram.INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
        "limit": 100,
        "access_token": instagram.INSTAGRAM_TOKEN,
    }
    for _ in range(max_pages):
        try:
            data = requests.get(url, params=params, timeout=20).json()
        except Exception as e:
            print(f"post_stat: стрічка інсти — {e}")
            return
        if "error" in data:
            print(f"post_stat: стрічка інсти — {data['error'].get('message')}")
            return
        yield data.get("data", [])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        url, params = next_url, None


def find_media_by_shortcode(shortcode, max_pages=MEDIA_SCAN_PAGES):
    """Медіа за shortcode. Зіставляємо з permalink — це ТОЧНИЙ збіг: shortcode
    із лінка стоїть у ньому дослівно. Розкодовувати shortcode в число не можна
    (див. шапку модуля), тому листинг тут не «на безриб'ї», а єдиний правильний
    шлях. Повертає (media, scanned)."""
    needle = f"/{shortcode}/"
    scanned = 0
    for page in _iter_media_pages(max_pages):
        for media in page:
            scanned += 1
            permalink = media.get("permalink") or ""
            if needle in permalink or permalink.rstrip("/").endswith("/" + shortcode):
                return media, scanned
    return None, scanned


def instagram_post_stat(link):
    """Метрики допису Instagram за shortcode з лінка."""
    shortcode = link.get("shortcode")
    if not shortcode:
        return {"error": "no_shortcode"}
    if not instagram.INSTAGRAM_TOKEN:
        return {"error": "not_configured"}
    media, scanned = find_media_by_shortcode(shortcode)
    if not media:
        return {"error": "not_found", "scanned": scanned}
    packed = stat_instagram._pack(media, "shortcode")
    packed.update({
        "net": "ig",
        "text": media.get("caption") or "",
        "method": "shortcode",
        "scanned": scanned,
    })
    return packed


# ---------- Збірка ----------

def collect(url):
    """Синхронний збір: лінк → метрики поста або {'error': …}. Уся мережа тут,
    тому виклик із бота йде через asyncio.to_thread."""
    link = parse_link(url)
    if not link:
        return {"error": "not_a_post"}

    stranger = foreign_owner(link)
    if stranger:
        return {"error": "foreign", "owner": stranger}

    og = {}
    # Шортлінк і pfbid читаються ЗІ СТОРІНКИ: перший — щоб пройти редирект,
    # другий — щоб дістати текст і фото, за якими пост упізнається у стрічці.
    if link.get("shortlink") or link.get("pfbid") or (
            link["net"] == "fb" and not link.get("object_id")):
        try:
            final_url, html = fetch_page(link["url"])
            og = parse_og(html)
            resolved = parse_link(final_url)
            if resolved and (resolved.get("object_id") or resolved.get("shortcode")):
                resolved["owner"] = resolved.get("owner") or link.get("owner")
                stranger = foreign_owner(resolved)
                if stranger:
                    return {"error": "foreign", "owner": stranger}
                link = resolved
            elif resolved and resolved.get("pfbid"):
                link = {**resolved, "owner": resolved.get("owner") or link.get("owner")}
        except Exception as e:
            print(f"post_stat: сторінка {link['url']} не прочиталась — {e}")

    if link["net"] == "ig":
        if link.get("shortlink") and not link.get("shortcode"):
            return {"error": "ig_shortlink"}
        return instagram_post_stat(link)

    if not FACEBOOK_PAGE_TOKEN:
        return {"error": "not_configured"}
    out = facebook_post_stat(link, og)
    if out.get("error") and og.get("description"):
        # Не впізнали — але текст поста в руках. Знати, ПРО ЩО пост, і не мати
        # цифр чесніше за німе «не знайдено»
        out["og_text"] = og["description"]
    return out


# ---------- Вивід ----------

def _esc(text):
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _num(value):
    return "—" if value is None else f"{value:,}".replace(",", " ")


def _preview(text, limit=280):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


METHOD_NOTE = {
    "direct": None,  # id був просто в лінку — пояснювати нічого
    "photo": "впізнав за фото поста",
    "nora": "id у лінку не було — знайшов у стрічці за датою матеріалу з нори",
    "feed": "id у лінку не було — знайшов, перебравши стрічку сторінки",
    "shortcode": None,
}

ERROR_TEXT = {
    "not_a_post": ("Це не схоже на лінк поста.\n"
                   "Приймаю Facebook (пост, рілз, share-лінк) та Instagram "
                   "(допис, рілз).\n"
                   "Статистика матеріалу — /stat &lt;лінк nikvesti.com&gt;"),
    "not_configured": "Доступ до цієї мережі не налаштовано.",
    "no_shortcode": "З лінка Instagram не читається код допису.",
    "ig_shortlink": ("Instagram не розкриває свої share-лінки нашому серверу.\n"
                     "Відкрий допис у застосунку і скопіюй адресу вигляду "
                     "<code>instagram.com/p/…</code> — за нею знайду."),
}


def format_message(res):
    """Повідомлення про ОДИН пост. Мова та сама, що в /stat, — бо це ті самі
    числа, просто взяті з іншого боку."""
    err = res.get("error")
    if err == "foreign":
        return (f"Це пост чужої сторінки (<b>{_esc(res.get('owner'))}</b>).\n\n"
                "Метрики поста віддає лише Graph API і лише адміністратору "
                "сторінки — чужі перегляди й охоплення не бачить ніхто ззовні. "
                "Рахую тільки наші @nikvesti у Facebook та Instagram.")
    if err == "graph":
        return ("Пост упізнав, але Facebook не віддав його: "
                f"<i>{_esc(str(res.get('detail'))[:180])}</i>")
    if err == "not_found":
        lines = ["Не знайшов цей пост серед наших публікацій."]
        scanned = res.get("scanned") or 0
        if scanned:
            lines.append(f"Перебрав {scanned} останніх публікацій сторінки.")
        if res.get("og_text"):
            lines += ["", "Сторінку поста прочитав, ось його текст:",
                      f"<i>{_esc(_preview(res['og_text']))}</i>", "",
                      "Якщо пост давніший — дай лінк вигляду "
                      "<code>facebook.com/nikvesti/posts/&lt;число&gt;</code>, "
                      "за ним знайду одразу."]
        return "\n".join(lines)
    if err:
        return ERROR_TEXT.get(err, "Не вдалося зібрати статистику поста.")

    if res["net"] == "fb":
        head = "🎬 <b>Рілз у Facebook</b>" if res["type"] == "reel" else "📘 <b>Пост у Facebook</b>"
    else:
        head = ("🎬 <b>Рілз в Instagram</b>" if res.get("media_type") == "VIDEO"
                else "📷 <b>Допис в Instagram</b>")

    lines = [head, f'<a href="{res["permalink"]}">{res["date"]}</a>']

    text = _preview(res.get("text"))
    if text:
        lines += ["", f"<i>{_esc(text)}</i>"]

    lines.append("")
    if res.get("views") is not None:
        lines.append(f'👁 Перегляди: {_num(res["views"])}')
    if res.get("reach") is not None:
        lines.append(f'👀 Охоплення: {_num(res["reach"])}')
    if res["net"] == "fb":
        lines.append(f'❤️ Реакції: {_num(res.get("reactions"))}')
        lines.append(f'💬 Коментарі: {_num(res.get("comments"))}')
        lines.append(f'🔄 Шери: {_num(res.get("shares"))}')
    else:
        lines.append(f'❤️ Лайки: {_num(res.get("likes"))}')
        lines.append(f'💬 Коментарі: {_num(res.get("comments"))}')
        if res.get("shares") is not None:
            lines.append(f'✈️ Поширення: {_num(res["shares"])}')
        if res.get("saved") is not None:
            lines.append(f'🔖 Збереження: {_num(res["saved"])}')

    if res.get("eng_note") and any(res.get(k) is None for k in
                                  ("views", "reactions", "comments", "shares")):
        lines.append(f'<i>частину метрик Facebook не віддав: '
                     f'{_esc(str(res["eng_note"])[:120])}</i>')

    note = METHOD_NOTE.get(res.get("method"))
    if note:
        lines += ["", f"<i>{note}</i>"]
    return "\n".join(lines)


# ---------- Handler ----------

async def reply_post_stat(message, url):
    """Зібрати й відповісти на конкретне повідомлення. Окремо від хендлера,
    бо той самий шлях кличуть троє: /post, /stat із соцлінком і голий лінк
    у приваті."""
    msg = await message.reply_text("⏳ Шукаю пост…")
    try:
        res = await asyncio.to_thread(collect, url)
    except Exception as e:
        print(f"post_stat: збій — {e}")
        await msg.edit_text(f"Не вдалося зібрати статистику: {_esc(e)}",
                            parse_mode="HTML")
        return
    await msg.edit_text(format_message(res), parse_mode="HTML",
                        disable_web_page_preview=True)


async def post_stat_handler(update, context):
    url = context.args[0] if context.args else None
    if not url and update.message and update.message.reply_to_message:
        # Лінк часто вже лежить у чаті — відповісти на нього зручніше,
        # ніж копіювати
        url = first_link(update.message.reply_to_message.text or "")
    if not url:
        await update.message.reply_text(
            "Кинь лінк на пост:\n"
            "<code>/post https://www.facebook.com/share/p/…</code>\n"
            "<code>/post https://www.instagram.com/nikvesti/p/…</code>\n\n"
            "Віддам метрики САМЕ цього поста. Статистика матеріалу — /stat.",
            parse_mode="HTML")
        return
    await reply_post_stat(update.message, url)


_LINK_RE = re.compile(r"https?://\S+")


def first_link(text):
    m = _LINK_RE.search(text or "")
    return m.group(0) if m else None
