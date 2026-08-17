"""
Генератор карток для соцмереж (сторінка /card на домені Mini App).

Флоу: журналіст кидає лінк на матеріал nikvesti.com → бекенд скрапить сторінку
(JSON-LD NewsArticle: заголовок, опис, УСІ фото з підписами) → у браузері
редактор: вибір фото, правка заголовка, виділення слів «білим» (як на брендових
картках), зум/зсув фото всередині фіксованого слота (псевдокроп) → готовий JPG
малюється Canvas-ом прямо в браузері й завантажується без жодного бекенд-рендера.

Бекенд тут потрібен рівно для двох речей, які браузер сам не може:
  1. POST /api/card/scrape — витягти дані статті (CORS не пустить fetch на
     чужий origin із браузера).
  2. GET /api/card/img?u=… — проксі картинок з nikvesti.com: без same-origin
     зображення canvas стає "tainted" і toBlob/JPG-експорт кидає SecurityError.
     Це НЕ відкритий проксі: віддає лише хости зі списку ALLOWED_HOSTS.

Авторизації немає свідомо (сторінку відкривають із браузера, не з Telegram,
тож initData тут ні до чого): endpoint-и вміють читати лише nikvesti.com і
нічого не пишуть. Стани немає — все живе в браузері.

Дані статті: перше джерело — JSON-LD NewsArticle (headline, description,
image[] з caption — там УСІ фото матеріалу, не тільки og:image). Фолбек —
og:image + <title>, якщо структурні дані колись зникнуть.
"""

import asyncio
import json
import os
import re
from urllib.parse import urlparse

import requests

from handlers.helpers import normalize_https_url

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — модуль неактивний
    web = None

# Хости, які можна скрапити і проксювати. Не відкритий проксі.
ALLOWED_HOSTS = {"nikvesti.com", "www.nikvesti.com"}

# Домен Mini App, на якому живе сторінка /card. Тут же — конвенція лінка на
# неї, щоб її не переписували в кожного споживача (fb_missing, NLQ-роутер):
# сторінка сама скрапить новину за id, тож у лінк іде рівно id.
WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))

FETCH_TIMEOUT = 15
# Ліміт відповіді проксі: оригінали фото ~1–3 МБ, 20 МБ — запас із головою.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_UA = "Mozilla/5.0 (compatible; MykVistiCardMaker/1.0; +https://nikvesti.com)"

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")


def card_url(article_id):
    """Лінк у генератор карток одразу з цією новиною. None, якщо WEBAPP_URL не
    налаштований або id порожній — краще повідомлення без кнопки, ніж кнопка
    в нікуди."""
    article_id = str(article_id or "").strip()
    if not WEBAPP_URL or not article_id:
        return None
    return f"{WEBAPP_URL}/card?url={article_id}"


def card_button(article_id, text="🎨 Зробити картку"):
    """Готова інлайн-клавіатура з однією кнопкою в генератор карток (або None).
    Спільна для всіх, хто пропонує зробити картку: монітор новин без ФБ і
    NLQ-роутер, коли Лис написав пост для соцмереж."""
    url = card_url(article_id)
    if not url:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])


def _host_allowed(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in ALLOWED_HOSTS


def parse_article(html_text):
    """Розбирає сторінку матеріалу: заголовок, опис, дата, фото з підписами.

    Повертає dict; фото — список [{url, caption}] у порядку з JSON-LD
    (перше — головне). Перевірено на реальній статті 322045 (05.08.2026).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")

    result = {"title": "", "description": "", "date": "", "images": [],
              "is_ad": False, "quotes": []}

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        # Рекламні матеріали мають @type=AdvertiserContentArticle (стаття
        # 322051, Renault) — приймаємо будь-який *Article, а не лише
        # NewsArticle; @type буває і списком
        types = data.get("@type") or ""
        if isinstance(types, str):
            types = [types]
        if not any("Article" in t for t in types):
            continue
        result["is_ad"] = any("Advertiser" in t for t in types)
        result["title"] = (data.get("headline") or "").strip()
        result["description"] = (data.get("description") or "").strip()
        result["date"] = data.get("datePublished") or ""
        images = data.get("image") or []
        if isinstance(images, dict):
            images = [images]
        for img in images:
            if isinstance(img, str):
                url, caption = img, ""
            else:
                url = img.get("url") or ""
                caption = (img.get("caption") or "").strip()
            if url and _host_allowed(url):
                result["images"].append({"url": url, "caption": caption})
        break

    # Фото з тіла статті (<img class="photo …">) — друге джерело: у частини
    # матеріалів JSON-LD без image, а «оригінал» без розмірного префікса не
    # існує (стаття 322051, живі лише _1500.webp з тіла). Дублікати того
    # самого кадру в різних розмірах відсікаються за базовим іменем файлу.
    seen = {_img_key(im["url"]) for im in result["images"]}
    for img in soup.find_all("img", class_="photo"):
        src = img.get("src") or ""
        if src.startswith("/"):
            src = "https://nikvesti.com" + src
        # інлайн-мініатюри (_280.webp тощо) закороткі для канваса 1600px
        m = re.search(r"_(\d{2,4})(?=\.\w+$)", src)
        if m and int(m.group(1)) < 800:
            continue
        if _host_allowed(src) and _img_key(src) not in seen:
            seen.add(_img_key(src))
            result["images"].append({"url": src, "caption": ""})

    # Цитати зі статті (blockquote) — для шаблону «Цитата»: зручно виділити
    # й скопіювати потрібний фрагмент, не ходячи на сайт
    for bq in soup.find_all("blockquote"):
        text = bq.get_text(" ", strip=True)
        if len(text) > 20 and text not in result["quotes"]:
            result["quotes"].append(text)

    # Фолбеки, якщо JSON-LD зник або неповний
    if not result["title"]:
        og = soup.find("meta", property="og:title") or soup.find("title")
        result["title"] = (og.get("content") if og and og.has_attr("content")
                           else og.get_text() if og else "").strip()
    if not result["images"]:
        # og:image як є (ресайз 600x315): зрізати префікс не можна —
        # для 322051 такий «оригінал» віддавав HTML-заглушку замість фото
        og = soup.find("meta", property="og:image")
        if og and og.get("content") and _host_allowed(og["content"]):
            result["images"].append({"url": og["content"], "caption": ""})

    return result


# Тіло статті. Прямі діти цього контейнера — і тільки вони: усередині лежить
# промо-вставка Клубу МикВісті (noindex > div.donation-box), і селектор
# «div p» без «>» затягує її абзаци в «текст статті». Заміряно 17.08.2026 на
# статті 322389: 10 справжніх абзаців проти 14 разом із промо.
BODY_SELECTOR = "div.main-post-content-text"

# Максимальний розмір, який віддає ресайзер аватарок. Більший (510x510 тощо)
# відповідає 200 з ПОРОЖНІМ тілом і content-type text/html — тобто «успіх» за
# кодом відповіді тут брехня, перевіряти треба тип (див. probe_image).
AUTHOR_PHOTO_SIZE = "255x255"


def parse_article_body(html_text):
    """Повний текст статті, цитати по порядку і картка автора.

    Потрібне генератору каруселей: JSON-LD дає заголовок, опис і фото, але
    articleBody у ньому НЕМАЄ — агенту нема з чого складати слайди.

    Повертає {"paragraphs": [...], "quotes": [...], "author": {...} | None}.
    quotes — саме список у порядку появи в тексті: агент повертає лише НОМЕР
    цитати, а текст бере сервер, тож порядок тут є частиною контракту й
    рахується з того самого контейнера, що й абзаци.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "html.parser")

    result = {"paragraphs": [], "quotes": [], "author": None}

    box = soup.select_one(BODY_SELECTOR)
    if box:
        for node in box.find_all(["p", "blockquote"], recursive=False):
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            if node.name == "blockquote":
                result["quotes"].append(text)
            else:
                result["paragraphs"].append(text)

    # Підписи фото з ТІЛА статті: <div class="imgbox…"><img …><span
    # class="title">Підпис</span></div>. Потрібні як друге джерело, бо в
    # JSON-LD підпис буває сміттям — у 322371 там лежить «Your credit balance
    # is too low to access» від сервіса автопідписів, який вичерпав ліміт, а
    # правильний підпис тим часом стоїть тут (перевірено 17.08.2026).
    result["photo_titles"] = {}
    for span in soup.select("span.title"):
        box = span.find_parent("div")
        img = box.find("img") if box else None
        src = (img.get("src") or "") if img else ""
        title = span.get_text(" ", strip=True)
        if src and title:
            result["photo_titles"].setdefault(_img_key(_absolute(src) or src), title)

    name = soup.select_one("div.article-author__name")
    if name:
        position = soup.select_one("div.article-author__position")
        photo = soup.select_one("img.article-author__photo")
        result["author"] = {
            "name": name.get_text(" ", strip=True),
            "position": position.get_text(" ", strip=True) if position else "",
            "photo": _absolute(photo.get("src")) if photo and photo.get("src") else "",
        }

    return result


def _absolute(src):
    """Відносний шлях сайту → повний URL (порожньо для чужих хостів)."""
    src = (src or "").strip()
    if not src:
        return ""
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = "https://nikvesti.com" + src
    return src if _host_allowed(src) else ""


def author_photo_url(src, size=AUTHOR_PHOTO_SIZE):
    """Аватарка автора більшого розміру: /96x96/ → /255x255/.

    Для кола ~460 px на слайді 1080 px завширшки 255 впритул, але це стеля
    ресайзера — більший розмір він не малює, а мовчки віддає порожнечу."""
    src = _absolute(src)
    if not src:
        return ""
    return re.sub(r"/\d+x\d+/", f"/{size}/", src, count=1)


def probe_image(url):
    """Чи віддає URL справжню картинку. Саме картинку, а не «200 OK»:
    ресайзер сайту на неіснуючий розмір відповідає 200 з порожнім тілом і
    content-type text/html, тож перевірка за кодом відповіді пропустила б
    биту аватарку в слайд."""
    if not _host_allowed(url):
        return False
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                            headers={"User-Agent": _UA})
        ctype = resp.headers.get("Content-Type", "")
        ok = resp.ok and ctype.split(";")[0].strip().startswith("image/")
        resp.close()
        return ok
    except requests.RequestException:
        return False


def _img_key(url):
    """Базове ім'я кадру без розмірного префікса (/600x315/), суфікса _1500
    і розширення — той самий кадр у різних розмірах дає один ключ."""
    path = urlparse(url).path
    path = re.sub(r"^/\d+x\d+/", "/", path)
    path = re.sub(r"_\d{2,4}(?=\.\w+$)", "", path)
    return re.sub(r"\.\w+$", "", path)


def suggest_photo_caption(caption):
    """З повного підпису JSON-LD («<переказ заголовка>. Скриншот з відео …»)
    лишає останнє речення — джерело фото, як на брендових картках
    («Фото: Скриншот з відео Миколаївської міської ради»). Якщо джерело вже
    підписане «Фото: ОП» — префікс не дублюється (стаття 322032)."""
    if not caption:
        return ""
    parts = [p.strip() for p in caption.split(". ") if p.strip()]
    if not parts:
        return ""
    tail = parts[-1].rstrip(".")
    if re.match(r"фото\s*:", tail, re.IGNORECASE):
        return "Фото: " + re.sub(r"^фото\s*:\s*", "", tail, flags=re.IGNORECASE)
    return f"Фото: {tail}"


# ---------- HTTP handlers ----------

async def page(request):
    """GET /card — сторінка редактора (статичний HTML, усе в одному файлі)."""
    return web.FileResponse(
        os.path.join(_STATIC_DIR, "card.html"),
        headers={"Cache-Control": "no-cache"},
    )


async def api_scrape(request):
    """POST /api/card/scrape {url} → дані статті для редактора."""
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)
    url = (body.get("url") or "").strip()
    if not _host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лише посилання nikvesti.com"}, status=400)

    def fetch():
        resp = requests.get(url, timeout=FETCH_TIMEOUT,
                            headers={"User-Agent": _UA})
        resp.raise_for_status()
        return resp.text, resp.url

    try:
        html_text, final_url = await asyncio.to_thread(fetch)
    except requests.RequestException as e:
        return web.json_response(
            {"error": f"Не вдалося прочитати сторінку: {e}"}, status=502)

    data = parse_article(html_text)
    # Фінальний URL після редіректів: nikvesti.com/<id> → повний лінк зі
    # slug-ом — сторінка підставить його в поле замість короткого
    data["final_url"] = final_url if _host_allowed(final_url) else url
    if not data["title"] and not data["images"]:
        return web.json_response(
            {"error": "Схоже, це не сторінка матеріалу — ні заголовка, "
                      "ні фото не знайшлось"}, status=422)
    if data["images"]:
        data["caption_suggestion"] = suggest_photo_caption(
            data["images"][0]["caption"])
    else:
        data["caption_suggestion"] = ""
    return web.json_response(data)


async def api_image(request):
    """GET /api/card/img?u=<url> — same-origin проксі картинок nikvesti.com,
    щоб canvas не був tainted і JPG експортувався."""
    url = request.query.get("u", "")
    if not _host_allowed(url):
        return web.json_response({"error": "хост не дозволено"}, status=400)

    def fetch():
        resp = requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                            headers={"User-Agent": _UA})
        resp.raise_for_status()
        content = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("картинка завелика")
        return content, resp.headers.get("Content-Type", "image/jpeg")

    try:
        content, ctype = await asyncio.to_thread(fetch)
    except (requests.RequestException, ValueError) as e:
        return web.json_response(
            {"error": f"Не вдалося завантажити картинку: {e}"}, status=502)

    return web.Response(
        body=content, content_type=ctype.split(";")[0],
        headers={"Cache-Control": "public, max-age=3600"},
    )
