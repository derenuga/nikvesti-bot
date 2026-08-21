"""
Команда /post <лінк на пост> — метрики САМЕ ЦЬОГО поста в соцмережі.

Зворотний бік /stat: там дають лінк матеріалу і бот шукає, де ми його
постили; тут дають лінк ПОСТА, і бот віддає його числа.

ГОЛОВНИЙ УРОК МОДУЛЯ (21.08.2026). Половина лінків, якими діляться в чаті,
числового id не містить: `share/p/CODE` редиректить на pfbid-адресу, а pfbid —
непрозорий токен. Перша версія зробила з цього висновок «отже, пост треба
ВПІЗНАВАТИ за вмістом» і збудувала сходинки: читання og-тегів сторінки,
підбір User-Agent, фото → page_story_id, повнотекстовий пошук у норі, перебір
стрічки, схожість текстів. Дві сторінки коду трималися на ОДНОМУ
неперевіреному припущенні — що Graph API не приймає pfbid. Перевірка (Олег,
Graph API Explorer, 21.08) зайняла два запити:

    GET pfbid02nab…?fields=id           → (#12) singular statuses API is
                                          deprecated — тобто об'єкт ЗНАЙДЕНО,
                                          відмова стосується лише форми запиту
    GET {page_id}_pfbid02nab…?fields=id → {"id": "301719373180657_1715521640576310"}

Graph РОЗУМІЄ pfbid у складеній формі `{page_id}_{pfbid}` — так само, як
голий числовий id поста. Тож увесь шлях: пройти редирект share-лінка →
приклеїти page_id → один запит у Graph. Сходинки впізнавання видалені як
непотрібні. Урок на майбутнє: СПЕРШУ міряти найдешевший шлях, і лише коли він
довів свою неможливість — будувати обхід.

pfbid чужої сторінки відпадає сам: із нашим page_id Graph такого об'єкта не
знайде. А чужий нік у лінку відсікається ще раніше, на словах: інсайти
чужого поста Graph не віддає нікому ззовні, тож і питати нічого.

Instagram: shortcode з лінка стоїть дослівно в полі `permalink` кожного
нашого медіа — зіставлення точне, листинг гортається до збігу. Розкодовувати
shortcode в число не можна (`DcEW0bgDE2U` → 3964393931957620116 — це pk
внутрішнього API, а не id Graph).

Відмова API — не «не знайшли»: причина доїжджає до людини, а протухлий токен
називається токеном і смикає сторож (12.08 INSTAGRAM_TOKEN помер, і дев'ять
днів про це не казав ніхто).
"""

import asyncio
import os
import re
import requests
from urllib.parse import urlparse, parse_qs

from handlers import fb_token
from handlers import instagram
from handlers import stat_instagram
from handlers.facebook import (
    FACEBOOK_PAGE_SLUG, fix_permalink,
    graph_get as _graph, get_post_metrics as _get_post_metrics,
    get_reel_insights,
)
from handlers.stat import _fb_date, _get_reel_views

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

# Скільки сторінок листингу інсти гортати в пошуку shortcode. Там 2-5 дописів
# на добу, тож 12 сторінок по 100 покривають більше року.
MEDIA_SCAN_PAGES = 12

# UA для проходу редиректу share-лінка. Заміряно 21.08.2026: редирект
# віддається і чесному ботові (тіло сторінки нам не потрібне взагалі —
# лише фінальна адреса).
REDIRECT_UA = "NikVesti-Bot/1.0 (+https://nikvesti.com)"

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
    kind ('post'|'video'|'photo'), pfbid, shortcode (IG), shortlink (треба
    пройти редирект), owner (нік сторінки з лінка — щоб одразу відсікти
    чужий пост)."""
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
    # редирект приведе на pfbid-адресу
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
    """Нік зі лінка, якщо це ЧУЖА сторінка. None — наша або нік не вказаний."""
    owner = ((link or {}).get("owner") or "").strip().lower()
    if not owner:
        return None
    ours = {FACEBOOK_PAGE_SLUG.lower(), "nikvesti", "profile.php"}
    for extra in (FACEBOOK_PAGE_ID, instagram.INSTAGRAM_USER_ID):
        if extra:
            ours.add(str(extra).lower())
    return None if owner in ours else owner


def follow_redirect(url):
    """Фінальна адреса share-лінка. Тіло сторінки не читаємо взагалі — усе,
    що треба (pfbid), лежить у самій адресі після редиректу. None = мережа
    не відповіла."""
    try:
        resp = requests.get(url, headers={"User-Agent": REDIRECT_UA},
                            timeout=20, allow_redirects=True)
        return resp.url
    except Exception as e:
        print(f"post_stat: редирект {url} — {e}")
        return None


# ---------- Facebook ----------

def _full_id(object_id):
    """Id → складена форма `{page_id}_{id}`. Саме вона робить прямий шлях
    можливим: Graph відповідає нею і на числовий id, і на pfbid (замір у
    шапці модуля). Уже складений лишаємо як є."""
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


def _post_text(post):
    """Текст об'єкта. Три поля, бо їх заповнюють різні типи: message — пости,
    story — службові («сторінка змінила фото»), description — відео й рілзи."""
    return " ".join(x for x in (post.get("message"), post.get("story"),
                                post.get("description")) if x).strip()


def facebook_post_stat(link):
    """Метрики поста Facebook. Повертає dict як у /stat (type/permalink/date/
    views/…) плюс text, або {'error': …} з людською причиною."""
    object_id = link.get("object_id") or link.get("pfbid")
    if not object_id:
        return {"net": "fb", "error": "unresolved"}

    data, err = _read_object(object_id)
    if not data:
        return {"net": "fb", "error": "api", "detail": err}

    oid = str(data["id"])
    attachments = (data.get("attachments", {}) or {}).get("data", [])
    media_type = (attachments[0].get("media_type") if attachments else "") or ""
    # Тип — із вкладень (для постів) і з самого лінка (у голого `/reel/123`
    # вкладень немає взагалі)
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
    }


# ---------- Instagram ----------

def _note_api_error(state, message, where):
    """Причина відмови API мусить ДОЇХАТИ до людини, а не лишитись у логах.
    Перша версія ковтала її: листинг тихо спинявся, і відповідь виходила
    «схоже, пост давніший», хоча токен був мертвий із 12.08."""
    text = message or "невідома помилка"
    print(f"post_stat: {where} — {text}")
    if state is not None and not state.get("api_error"):
        state["api_error"] = text


def _iter_media_pages(max_pages=MEDIA_SCAN_PAGES, state=None):
    base, token = instagram.credentials()
    url = f"{base}/{instagram.INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
        "limit": 100,
        "access_token": token,
    }
    for _ in range(max_pages):
        try:
            data = requests.get(url, params=params, timeout=20).json()
        except Exception as e:
            _note_api_error(state, str(e), "стрічка інсти")
            return
        if "error" in data:
            _note_api_error(state, data["error"].get("message"), "стрічка інсти")
            return
        yield data.get("data", [])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return
        url, params = next_url, None


def find_media_by_shortcode(shortcode, max_pages=MEDIA_SCAN_PAGES):
    """Медіа за shortcode. Зіставляємо з permalink — це ТОЧНИЙ збіг: shortcode
    із лінка стоїть у ньому дослівно. Повертає (media, scanned, причина
    відмови API або None)."""
    needle = f"/{shortcode}/"
    scanned = 0
    state = {}
    for page in _iter_media_pages(max_pages, state=state):
        for media in page:
            scanned += 1
            permalink = media.get("permalink") or ""
            if needle in permalink or permalink.rstrip("/").endswith("/" + shortcode):
                return media, scanned, None
    return None, scanned, state.get("api_error")


def instagram_post_stat(link):
    """Метрики допису Instagram за shortcode з лінка."""
    shortcode = link.get("shortcode")
    if not shortcode:
        return {"net": "ig", "error": "no_shortcode"}
    if not (instagram.INSTAGRAM_TOKEN or instagram.FACEBOOK_PAGE_TOKEN):
        return {"net": "ig", "error": "not_configured"}
    media, scanned, api_error = find_media_by_shortcode(shortcode)
    if api_error:
        return {"net": "ig", "error": "api", "detail": api_error}
    if not media:
        return {"net": "ig", "error": "not_found", "scanned": scanned}
    packed = stat_instagram._pack(media, "shortcode")
    packed.update({"net": "ig", "text": media.get("caption") or ""})
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

    # Шортлінк: редирект розкриває pfbid прямо в адресі. Тіло не читаємо.
    if link.get("shortlink") and link["net"] == "fb":
        final_url = follow_redirect(link["url"])
        resolved = parse_link(final_url) if final_url else None
        if resolved and not resolved.get("shortlink"):
            stranger = foreign_owner(resolved)
            if stranger:
                return {"error": "foreign", "owner": stranger}
            link = resolved

    if link["net"] == "ig":
        if link.get("shortlink"):
            return {"net": "ig", "error": "ig_shortlink"}
        return instagram_post_stat(link)

    if not FACEBOOK_PAGE_TOKEN:
        return {"net": "fb", "error": "not_configured"}
    return facebook_post_stat(link)


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


# Тексти відмов. Правило одне (CLAUDE.md §9): людина спитала СТАТИСТИКУ, тож
# і відповідь — про статистику, а не про те, як бот її шукав.
ERROR_TEXT = {
    "not_a_post": ("Це не схоже на лінк поста.\n"
                   "Приймаю Facebook (пост, рілз, share-лінк) та Instagram "
                   "(допис, рілз).\n"
                   "Статистика матеріалу — /stat &lt;лінк nikvesti.com&gt;"),
    "not_configured": "Доступ до цієї мережі не налаштовано.",
    "no_shortcode": "З лінка Instagram не читається код допису.",
    "ig_shortlink": ("Instagram не розкриває свої share-лінки нашому серверу.\n"
                     "Відкрий допис у застосунку і скопіюй адресу вигляду "
                     "<code>instagram.com/p/…</code> — за нею дам статистику."),
    "unresolved": ("Статистики не дістав — Facebook не розкрив це посилання "
                   "(редирект не привів до поста).\n\n"
                   "Спробуй ще раз за хвилину. Або надішли лінк вигляду "
                   "<code>facebook.com/nikvesti/posts/&lt;число&gt;</code> — "
                   "за ним цифри беруться одразу."),
}


def format_message(res):
    """Повідомлення про ОДИН пост. Мова та сама, що в /stat, — бо це ті самі
    числа, просто взяті з іншого боку."""
    err = res.get("error")
    if err == "foreign":
        return (f"Статистики по цьому посту немає — він не наш "
                f"(<b>{_esc(res.get('owner'))}</b>).\n\n"
                "Перегляди й охоплення чужого поста не бачить ніхто ззовні: "
                "Facebook і Instagram віддають їх лише адміністратору "
                "сторінки. Рахую наші @nikvesti.")
    if err == "api":
        net = "Instagram" if res.get("net") == "ig" else "Facebook"
        detail = str(res.get("detail") or "")
        if fb_token.is_token_error(detail):
            # Протухлий токен сам не оживає — «спробуй ще раз» тут брехня
            return (f"Статистики не дістав — <b>протух токен {net}</b>.\n\n"
                    "Це не тимчасово. Надіслав тобі в приват інструкцію заміни; "
                    "як заміниш — бот сам скаже, що токен знову живий.")
        return (f"Статистики не дістав: {net} відмовив.\n"
                f"<i>{_esc(detail[:180])}</i>")
    if err == "not_found":
        return ("Статистики по цьому посту не дістав.\n\n"
                "Серед дописів @nikvesti в Instagram, які видно ботові, його "
                "немає — схоже, він давніший за рік.")
    if err:
        return ERROR_TEXT.get(err, "Статистики по цьому посту не дістав.")

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

    # Правило редакції: про протухлий токен бот КАЖЕ САМ і з інструкцією
    # заміни. Сторож перевіряє щогодини, але людина, яка спіткнулась просто
    # зараз, не має чекати до :03 — той самий шлях, що у /stat
    detail = str(res.get("detail") or "")
    if res.get("error") == "api" and fb_token.is_token_error(detail):
        if res.get("net") == "ig":
            await fb_token.alert_ig_token_dead(message.get_bot(), detail, source="/post")
        else:
            await fb_token.alert_token_dead(message.get_bot(), detail, source="/post")


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
