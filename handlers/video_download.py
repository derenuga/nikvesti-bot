"""
Завантаження відео за лінком — сторінка /video на домені Mini App.

НАВІЩО: журналісту регулярно треба забрати чуже відео — заяву мера з прямого
ефіру, допис у Facebook, який завтра приберуть, рілз конкурента для звірки.
Робиться це зараз через випадкові сайти-качалки з рекламою й «встановіть наш
застосунок», тобто чужий сервер бачить, ЩО саме редакція збирає, і будь-коли
може підсунути замість відео що завгодно. Своя сторінка знімає обидві біди.

──────────────────────────────────────────────────────────────────────────────
ГОЛОВНЕ ПРО ДЖЕРЕЛА (заміряно 14.08.2026 з серверної адреси, не за чутками):

  YouTube — працює БЕЗ кук. Знято живий форматний список (30 форматів,
      2160p…360p, data/video_formats_sample.json) і завантажено файл наскрізь.
  Facebook — ЗАЛЕЖИТЬ ВІД ТИПУ ЛІНКА, і це найважливіше тут відкриття:
      • відео сторінки (`/watch/?v=…`) віддається без кук — знято живий
        форматний список (data/video_formats_fb_sample.json);
      • РІЛЗ (`/reel/…`) без кук не віддається взагалі: на живому лінку
        Facebook відповів серверу HTTP 400 і 1542 байти без жодного
        playable_url.
  Instagram — просить куки («empty media response»), профіль логвідному
      гостю віддається редіректом.

Одна засторога, щоб не наступити двічі: перший замір по YouTube я зробив на
нетиповому відео («Me at the zoo») і отримав «Sign in to confirm you're not a
bot» на ВСІХ дев'яти player-клієнтах — тобто одне відео здатне збрехати про
весь сервіс. На звичайних відео дефолтний клієнт проходить. Отже, капча на
конкретному лінку — привід дати куки, а не привід переписувати модуль.

──────────────────────────────────────────────────────────────────────────────
ЧОМУ КУКИ КЛАДЕ ЛЮДИНА, А НЕ БРАУЗЕР САМ (питання Олега: «я ж буду залогінений
у ФБ, нехай качає ніби від мене» і «хотів би розшарити сторінку на команду»):

Качає СЕРВЕР, а не браузер того, хто відкрив сторінку. Взяти куки відвідувача
сторінка не може ФІЗИЧНО — браузер не дає жодному сайту читати куки чужого
домену, і це не наше обмеження, а те правило, на якому тримається веб: інакше
будь-який сайт читав би вашу пошту. Тому «качати від імені людини» можливе
рівно в один спосіб: людина сама віддає боту експорт своїх кук.

Звідси конструкція, максимально близька до задуму:
  • у КОЖНОГО свій вхід (`/video?k=…`, токен видає команда /video) — видно,
    хто качає, і відкликати можна одного, не чіпаючи решту;
  • хто поклав боту СВОЇ куки — качає під собою; хто ні — під редакційними;
  • сторінка закрита токеном не з любові до замків: із куками вона дістає й
    те, що видно лише залогіненому, тож відкритою в інтернет їй бути не можна.

Куки живуть тижнями й помирають так само тихо, як токен Facebook (рідня цієї
біди — fb_token.py). Тому вони лежать у стані бота, а не у змінній Railway:
заміна не має вимагати редеплою. Порада, вивірена практикою: експортувати з
ОКРЕМОГО акаунта в режимі інкогніто — головний акаунт редакції не має ходити
із серверної адреси.
──────────────────────────────────────────────────────────────────────────────

Флоу сторінки: лінк → POST /api/video/probe (назва, канал, тривалість,
обкладинка, варіанти якості з приблизним розміром) → тап по якості →
POST /api/video/start заводить фонову задачу → сторінка щосекунди питає
GET /api/video/status (відсоток, швидкість, скільки лишилось) → GET
/api/video/file віддає готовий файл вкладенням.

Чому задача, а не один довгий запит: скачування півгодинного ефіру триває
хвилини, і запит без жодної ознаки життя браузер (та й будь-який проксі
попереду) має повне право обірвати — людина при цьому бачить просто «не
вийшло». З фоновою задачею видно смугу прогресу, а обірваний таб не вбиває
завантаження.

Склеювання доріжок (1080p на YouTube — це окремо відео й окремо звук) робить
ffmpeg. Якщо його в системі немає — модуль не падає, а пропонує лише ті
якості, що лежать одним файлом, і чесно підписує, чому список коротший.
"""

import asyncio
import base64
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from urllib.parse import quote, urlparse

from handlers import storage
from handlers.helpers import normalize_https_url

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — веб-шар неактивний
    web = None

WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))

# Не відкритий проксі: приймаємо лише названі джерела. Хост нормалізується
# (www./m./web./mbasic. зрізаються), тож мобільні лінки з телефона працюють
# нарівні з десктопними. Додати TikTok чи Twitter — один рядок, yt-dlp їх уміє.
ALLOWED_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "facebook.com", "fb.watch", "fb.com",
    "instagram.com", "instagr.am", "ig.me",
}

# Стеля на файл. Railway дає фіксовану квоту диска, і півгодинний ефір у 1080p
# — це вже сотні мегабайт; 500 МБ вистачає на робочі випадки й не дає одному
# лінку з'їсти диск. Змінюється VIDEO_MAX_MB без правки коду.
MAX_BYTES = int(os.environ.get("VIDEO_MAX_MB", "500")) * 1024 * 1024

# Скільки завантажень крутиться одночасно: процес тут один і той самий, що
# веде бота, тож третє паралельне вже відбирає час у розсилок.
MAX_ACTIVE = 2

# Скільки живе готовий файл. Людина тапнула «зберегти» — далі файл не
# потрібен, але буває, що браузер спитав куди зберігати, а людина відійшла.
JOB_TTL = 30 * 60

# Розбір лінка кешується: між «розібрати» і «скачати» проходять секунди, а
# другий похід по ту саму сторінку — це ще один шанс отримати капчу.
PROBE_TTL = 5 * 60

_PROBE_CACHE = {}   # (url, хто) -> (час, info)
_JOBS = {}          # job_id -> стан задачі

_HEIGHT_STEPS = [2160, 1440, 1080, 720, 480, 360, 240]


# ---------- лінки й хости ----------

def _host_of(url):
    """Нормалізований хост лінка: m.youtube.com і www.youtube.com — це
    youtube.com. Порожній рядок, якщо це взагалі не http(s)-лінк."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.hostname or "").lower()
    return re.sub(r"^(?:www|m|mobile|web|mbasic|music)\.", "", host)


def host_allowed(url):
    return _host_of(url) in ALLOWED_HOSTS


def source_name(url):
    """Як джерело називає людина — для повідомлень про помилки."""
    host = _host_of(url)
    if "instagr" in host or host == "ig.me":
        return "Instagram"
    if "facebook" in host or host.startswith("fb."):
        return "Facebook"
    return "YouTube"


def video_url(token, source_url=""):
    """Особистий лінк на сторінку. None, коли веб-шар спить: кнопка в нікуди
    гірша за її брак."""
    if not WEBAPP_URL:
        return None
    link = f"{WEBAPP_URL}/video?k={quote(token)}"
    if source_url:
        link += f"&u={quote(source_url, safe='')}"
    return link


# ---------- хто заходить ----------

def token_for(user_id, name=""):
    """Особистий токен людини (створюється при першій потребі й живе далі).

    Стабільний свідомо: сторінку кладуть у закладки, а лінк, що змінюється
    щоразу, означає «щоразу йди в бот». Заразом це журнал — видно, хто качає.
    """
    import secrets

    access = storage.get_video_access() or {}
    for token, who in access.items():
        if str(who.get("user_id")) == str(user_id):
            if name and who.get("name") != name:      # людину перейменували
                who["name"] = name
                storage.save_video_access(access)
            return token
    token = secrets.token_urlsafe(12)
    access[token] = {"user_id": user_id, "name": name,
                     "at": datetime.now().isoformat(timespec="seconds")}
    storage.save_video_access(access)
    return token


def whois(token):
    """{"user_id", "name"} за токеном або None."""
    if not token:
        return None
    return (storage.get_video_access() or {}).get(token)


def access_ok(request):
    return bool(whois(request.query.get("k", "")))


def _user_of(request):
    who = whois(request.query.get("k", ""))
    return str(who.get("user_id")) if who else ""


# ---------- куки ----------

_cookie_files = {}   # ключ власника -> (позначка часу, шлях до файлу)


def normalize_cookies(raw):
    """Будь-який експорт кук → формат Netscape, який розуміє yt-dlp.

    Розширення експортують по-різному, і людина не має про це думати:
      • cookies.txt як є — найчастіший випадок;
      • JSON-масив [{domain, name, value, path, expirationDate…}] — так віддає
        частина розширень Chrome;
      • base64 будь-чого з попереднього — на випадок, коли текст їде каналом,
        що псує табуляції.
    Заголовок «# Netscape HTTP Cookie File» дописується завжди: без нього
    yt-dlp мовчки відмовляється читати файл, і людина шукала б помилку в
    куках, а не в першому рядку.

    Повертає (текст, домени) або кидає ValueError із причиною.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("порожньо")

    if not raw.startswith("[") and "\t" not in raw and "Netscape" not in raw:
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError):
            pass                       # не base64 — розбираємось як є

    if raw.startswith("["):            # JSON-експорт
        import json
        try:
            items = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"JSON не читається: {e}")
        lines = ["# Netscape HTTP Cookie File"]
        for c in items:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            domain = c.get("domain") or ""
            lines.append("\t".join([
                domain,
                "TRUE" if domain.startswith(".") else "FALSE",
                c.get("path") or "/",
                "TRUE" if c.get("secure") else "FALSE",
                str(int(c.get("expirationDate") or c.get("expires") or 0)),
                str(c["name"]), str(c.get("value") or ""),
            ]))
        raw = "\n".join(lines)
    elif "\\t" in raw and "\t" not in raw:   # табуляції поїхали в escape
        raw = raw.replace("\\t", "\t").replace("\\n", "\n")

    domains, count = set(), 0
    for line in raw.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domains.add(parts[0].lstrip("."))
            count += 1
    if not count:
        raise ValueError("не знайшов жодної куки — це точно експорт cookies.txt?")
    if not raw.lstrip().startswith("#"):
        raw = "# Netscape HTTP Cookie File\n" + raw
    return (raw if raw.endswith("\n") else raw + "\n"), sorted(domains)


def save_cookies(text, user_id="", name="", shared=False):
    """Покласти куки: свої (за user_id) або редакційні (shared=True).

    Повертає список доменів, які вони покривають.
    """
    normalized, domains = normalize_cookies(text)
    state = storage.get_video_cookies() or {}
    entry = {"text": normalized, "domains": domains, "by": name,
             "at": datetime.now().isoformat(timespec="seconds")}
    if shared or not user_id:
        state["shared"] = entry
    else:
        state.setdefault("users", {})[str(user_id)] = entry
    storage.save_video_cookies(state)
    return domains


def forget_cookies(user_id="", shared=False):
    """Прибрати куки. Повертає True, якщо було що прибирати."""
    state = storage.get_video_cookies() or {}
    if shared or not user_id:
        gone = bool(state.pop("shared", None))
    else:
        gone = bool((state.get("users") or {}).pop(str(user_id), None))
    storage.save_video_cookies(state or None)
    return gone


def cookie_info(user_id=""):
    """Що зараз відомо про куки для цієї людини — для сторінки й команди."""
    state = storage.get_video_cookies() or {}
    mine = (state.get("users") or {}).get(str(user_id)) if user_id else None
    shared = state.get("shared")
    env = bool(os.environ.get("YTDLP_COOKIES")
               or os.environ.get("YTDLP_COOKIES_FILE"))
    use = mine or shared
    return {
        "ok": bool(use or env),
        "own": bool(mine),
        "shared": bool(shared),
        "env": env,
        "at": (use or {}).get("at", ""),
        "by": (use or {}).get("by", ""),
        "domains": (use or {}).get("domains", []),
    }


def _cookie_file(user_id=""):
    """Шлях до файлу кук для yt-dlp: спершу СВОЇ, потім редакційні, потім env.

    Файл на диску переписується, щойно змінився джерельний текст (позначка
    часу) — інакше після заміни протухлих кук бот до перезапуску ходив би зі
    старими, а людина шукала б причину в чому завгодно, крім кешу.
    """
    state = storage.get_video_cookies() or {}
    entry = ((state.get("users") or {}).get(str(user_id)) if user_id else None)
    key = f"u{user_id}"
    if not entry:
        entry, key = state.get("shared"), "shared"

    raw = (entry or {}).get("text") or ""
    stamp = (entry or {}).get("at") or ""
    if not raw:
        path = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
        if path and os.path.exists(path):
            return path
        env_raw = os.environ.get("YTDLP_COOKIES", "").strip()
        if not env_raw:
            return None
        try:
            raw, _ = normalize_cookies(env_raw)
        except ValueError as e:
            print(f"video_download: куки з YTDLP_COOKIES не читаються — {e}")
            return None
        key, stamp = "env", "env"

    cached = _cookie_files.get(key)
    if cached and cached[0] == stamp and os.path.exists(cached[1]):
        return cached[1]

    fd, path = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    _cookie_files[key] = (stamp, path)
    return path


def has_ffmpeg():
    """Чи є чим склеїти відео зі звуком. Без нього доступні лише ті якості,
    що лежать на сервері одним файлом (на YouTube це зазвичай 360p)."""
    return bool(shutil.which("ffmpeg"))


def _ydl_opts(extra=None, user_id=""):
    opts = {
        "quiet": True,
        "no_warnings": True,
        # quiet сам по собі НЕ глушить смугу прогресу: без noprogress yt-dlp
        # сипле в stdout сотні рядків «[download] 42.3% of …», і логи Railway
        # (спільні з ботом) стають нечитабельними на кожному завантаженні
        "noprogress": True,
        "noplaylist": True,      # лінк на відео зі списку — це одне відео
        "socket_timeout": 30,
        "retries": 3,
    }
    cookies = _cookie_file(user_id)
    if cookies:
        opts["cookiefile"] = cookies
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy
    if extra:
        opts.update(extra)
    return opts


# ---------- розбір лінка ----------

def _rate(fmt):
    return fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0


def _size_of(fmt, duration=0):
    """Розмір доріжки в байтах: точний, приблизний або порахований із бітрейту.
    Нуль — коли джерело не сказало нічого (у Facebook так і буває)."""
    size = (fmt or {}).get("filesize") or (fmt or {}).get("filesize_approx") or 0
    if not size and duration and _rate(fmt or {}):
        size = int(_rate(fmt) * 1000 / 8 * duration)
    return int(size or 0)


def human_size(num):
    if not num:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit in ("Б", "КБ") else f"{num:.1f} {unit}"
        num /= 1024.0
    return ""


def human_duration(seconds):
    seconds = int(seconds or 0)
    if not seconds:
        return ""
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean_title(title):
    """Назва відео, придатна і для картки, і для імені файлу.

    Facebook кладе в назву геть усе: заміряно на живому відео — «5.9K views ·
    62 reactions | 🇬🇧 Борис Джонсон назвав… | SPILNO.UA», тобто лічильники
    попереду, текст допису посередині й назва сторінки в хвості. Як імʼя файлу
    це нечитабельно, а обрізане до сотні байт — ще й з половиною слова.

    Тому: зрізаємо лічильники попереду, беремо ЗМІСТОВНУ частину (найдовший
    шматок між «|»), схлопуємо переноси. YouTube від цього не страждає — у
    нього в назві й так рівно назва.
    """
    text = re.sub(r"\s+", " ", (title or "").strip())
    if not text:
        return "Відео"
    text = re.sub(r"^[\d.,KkМмMm]+\s*views?\s*(·[^|]*)?\|\s*", "", text)
    if "|" in text:
        parts = [p.strip() for p in text.split("|") if p.strip()]
        if parts:
            text = max(parts, key=len)
    return text.strip(" -–—·") or "Відео"


def safe_filename(title, ext):
    """Імʼя файлу без того, що ламає файлові системи й заголовок вкладення.

    Емодзі лишаються (Linux і сучасні Windows їх тримають), а от слеші,
    двокрапки й лапки — ні. Довжина ріжеться ПО СЛОВАХ, щоб не лишалось
    обрізаного посеред слова хвоста.
    """
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", clean_title(title))
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 90:
        cut = name[:90].rsplit(" ", 1)[0]
        name = (cut or name[:90]).rstrip(" ,.;-") + "…"
    return f"{name}.{(ext or 'mp4').lstrip('.')}"


def curate_formats(info, ffmpeg=True):
    """Кілька зрозумілих варіантів замість сорока рядків yt-dlp.

    Людині треба вибрати якість, а не кодек: у сирому списку YouTube лежить по
    півсотні форматів, де половина — фрагменти без звуку, а ще три — розкадровки
    mhtml із висотами 27/45/90. Тому лишаємо по одному варіанту на висоту кадру
    плюс «тільки звук», і кожному рахуємо приблизний розмір — саме він вирішує,
    чи качати це з мобільного інтернету.

    Без ffmpeg пропонуються ЛИШЕ ті висоти, що існують одним файлом (відео зі
    звуком разом) — інакше кнопка обіцяла б 1080p і віддавала німе відео.
    """
    formats = [f for f in (info.get("formats") or []) if f.get("format_id")]
    duration = info.get("duration") or 0

    vids = [f for f in formats if (f.get("vcodec") or "none") != "none"]
    auds = [f for f in formats
            if (f.get("acodec") or "none") != "none"
            and (f.get("vcodec") or "none") == "none"]
    progressive = [f for f in vids if (f.get("acodec") or "none") != "none"]

    best_audio = max(auds, key=_rate, default=None)
    audio_size = _size_of(best_audio, duration) if best_audio else 0

    pool = vids if ffmpeg else progressive
    heights = sorted({f["height"] for f in pool if f.get("height")}, reverse=True)
    # Беремо ВСІ наявні висоти, а не лише звичні сходинки: у Facebook вони
    # нестандартні (заміряно на живому відео — 848 і 540), і відбір «лише
    # 1080/720/480» лишав там один варіант замість двох. Ріжемо, тільки якщо
    # джерело сипле десятком висот — тоді звичні сходинки йдуть першими.
    keep = heights
    if len(heights) > 6:
        keep = sorted(set([h for h in heights if h in _HEIGHT_STEPS][:6]
                          or heights[:6]), reverse=True)

    options = []
    for height in keep[:6]:
        same = [f for f in pool if f.get("height") == height]
        best = max(same, key=_rate, default=None)
        if not best:
            continue
        merged = ffmpeg and (best.get("acodec") or "none") == "none"
        size = _size_of(best, duration) + (audio_size if merged else 0)
        if ffmpeg:
            selector = (f"bestvideo[height<={height}]+bestaudio/"
                        f"best[height<={height}]/best")
        else:
            selector = (f"best[height<={height}][acodec!=none][vcodec!=none]/"
                        f"best[height<={height}]")
        options.append({
            "id": f"v{height}",
            "kind": "video",
            "label": f"{height}p",
            "note": human_size(size),
            "bytes": size,
            "ext": "mp4" if merged else (best.get("ext") or "mp4"),
            "selector": selector,
        })

    # Джерело не сказало ЖОДНОЇ висоти (буває у Facebook і в HLS-стрічках) —
    # тоді просто «найкраща якість», бо вибирати немає з чого
    if not options and formats:
        best = max(vids or formats, key=_rate, default=None)
        options.append({
            "id": "best", "kind": "video", "label": "Найкраща якість",
            "note": human_size(_size_of(best, duration)),
            "bytes": _size_of(best, duration),
            "ext": (best or {}).get("ext") or "mp4",
            "selector": "best",
        })

    if best_audio:
        options.append({
            "id": "audio", "kind": "audio", "label": "Тільки звук",
            "note": human_size(audio_size), "bytes": audio_size,
            "ext": best_audio.get("ext") or "m4a",
            "selector": "bestaudio/best",
        })
    return options


def describe(info, ffmpeg=None):
    """Нормалізована картка відео для сторінки."""
    if ffmpeg is None:
        ffmpeg = has_ffmpeg()
    thumb = info.get("thumbnail") or ""
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url") or ""
    return {
        "title": clean_title(info.get("title")),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "duration_text": human_duration(info.get("duration")),
        "thumbnail": thumb,
        "source": info.get("extractor_key") or "",
        "webpage_url": info.get("webpage_url") or "",
        "is_live": bool(info.get("is_live")),
        "options": curate_formats(info, ffmpeg),
        "ffmpeg": ffmpeg,
    }


def _extract(url, user_id=""):
    """Синхронний похід yt-dlp по метадані (крутиться в потоці)."""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts({"skip_download": True}, user_id)) as ydl:
        info = ydl.extract_info(url, download=False)
    # Лінк на канал чи плейлист усе одно може приїхати списком — беремо перше
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ValueError("За цим лінком немає жодного відео")
        info = entries[0]
    return info


def probe_cached(url, user_id=""):
    """Метадані з кешу або свіжі. Кеш живе PROBE_TTL — рівно щоб «розібрати» і
    «скачати» не були двома походами по капчу. Ключ включає людину: у різних
    людей різні куки, отже й різний доступ до того самого лінка."""
    key = (url, str(user_id))
    hit = _PROBE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < PROBE_TTL:
        return hit[1]
    info = _extract(url, user_id)
    _PROBE_CACHE[key] = (now, info)
    for old, (stamp, _) in list(_PROBE_CACHE.items()):
        if now - stamp > PROBE_TTL:
            _PROBE_CACHE.pop(old, None)
    return info


# ---------- людські помилки ----------

def humanize_error(text, url=""):
    """Помилка yt-dlp → речення, з якого зрозуміло, що робити.

    Найчастіший випадок — не поламаний лінк, а відмова джерела говорити з
    сервером; сирий текст yt-dlp із посиланнями на GitHub-wiki читається як
    «щось зламалось у боті», хоч зламалось не в боті.
    """
    low = (text or "").lower()
    who = source_name(url)

    if "not a bot" in low or "sign in to confirm" in low:
        return (f"{who} просить сервер підтвердити, що він не робот. "
                f"Лікується куками: кинь боту в приват свій cookies.txt.")
    if ("empty media response" in low or "login required" in low
            or "log in" in low or "cookies" in low or "rate-limit" in low):
        return (f"{who} показав серверу логін-стіну замість відео. "
                f"Потрібні куки: кинь боту в приват свій cookies.txt.")
    if "private" in low:
        return "Відео приватне — доступу немає навіть у браузері."
    if "unavailable" in low or "removed" in low or "deleted" in low:
        return "Відео недоступне: або прибрали, або закрили для нашого регіону."
    if "drm" in low:
        return "Відео захищене DRM — таке не завантажується взагалі."
    if "unsupported url" in low:
        return "Не впізнав лінк. Потрібна адреса самого відео, а не каналу."
    if "cannot parse data" in low or "http error 400" in low:
        return (f"{who} віддав серверу не сторінку відео, а заглушку — так він "
                f"відповідає незалогіненому. Потрібні куки: кинь боту в приват "
                f"свій cookies.txt.")
    if "timed out" in low or "timeout" in low:
        return "Джерело не відповіло вчасно. Спробуй ще раз."
    if "file is larger" in low or "max-filesize" in low:
        return (f"Файл більший за стелю {MAX_BYTES // (1024 * 1024)} МБ — "
                f"візьми нижчу якість.")
    clean = re.sub(r"\s*;?\s*please report this issue.*", "", text or "",
                   flags=re.I | re.S)
    clean = re.sub(r"\s*See\s+https?://\S+.*", "", clean, flags=re.I | re.S)
    return clean.strip() or "Не вдалося прочитати відео за цим лінком."


# ---------- фонові задачі ----------

def _sweep():
    """Прибрати старі задачі разом із файлами. Кличеться перед кожним новим
    запуском: диск на Railway не гумовий, а лишити по собі гігабайт тимчасових
    файлів — найпростіший спосіб покласти бота цілком."""
    now = time.time()
    for job_id, job in list(_JOBS.items()):
        if now - job["started"] < JOB_TTL:
            continue
        shutil.rmtree(job.get("dir") or "", ignore_errors=True)
        _JOBS.pop(job_id, None)


def _active_count():
    return sum(1 for j in _JOBS.values() if j["state"] in ("queued", "running"))


def _download_blocking(job, url, selector, merge, user_id=""):
    """Саме завантаження. Крутиться в потоці, стан пише в job."""
    import yt_dlp

    def hook(status):
        if status.get("status") == "downloading":
            done = status.get("downloaded_bytes") or 0
            total = (status.get("total_bytes")
                     or status.get("total_bytes_estimate") or 0)
            if done > MAX_BYTES:
                raise ValueError("file is larger than max-filesize")
            job["done_bytes"] = done
            job["total_bytes"] = total
            job["percent"] = round(done / total * 100, 1) if total else None
            job["speed"] = status.get("speed") or 0
            job["eta"] = status.get("eta") or 0
            idict = status.get("info_dict") or {}
            job["stage"] = ("Звук" if (idict.get("vcodec") or "none") == "none"
                            else "Відео")
        elif status.get("status") == "finished":
            job["percent"] = 100
            if merge:
                job["stage"] = "Складаю файл"

    opts = _ydl_opts({
        "format": selector,
        "outtmpl": os.path.join(job["dir"], "video.%(ext)s"),
        "progress_hooks": [hook],
        "max_filesize": MAX_BYTES,
        "overwrites": True,
    }, user_id)
    if merge:
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    # Файл на диску зветься video.<ext> — назву джерела в шаблон не кладемо
    # свідомо: у Facebook вона довжиною в допис, а ще її обрізало б посеред
    # слова. Людське імʼя дається один раз, при віддачі (safe_filename).
    files = [os.path.join(job["dir"], n) for n in os.listdir(job["dir"])]
    files = [f for f in files if os.path.isfile(f) and not f.endswith(".part")]
    if not files:
        raise ValueError("Файл не з'явився — джерело обірвало завантаження")
    path = max(files, key=os.path.getsize)
    return path, os.path.getsize(path)


async def _run_job(job, url, selector, merge, user_id=""):
    try:
        path, size = await asyncio.to_thread(
            _download_blocking, job, url, selector, merge, user_id)
    except Exception as e:                      # yt-dlp кидає що завгодно
        job["state"] = "error"
        job["error"] = humanize_error(str(e), url)
        shutil.rmtree(job.get("dir") or "", ignore_errors=True)
        return
    ext = os.path.splitext(path)[1] or ".mp4"
    job.update(state="ready", path=path, size=size, percent=100,
               filename=safe_filename(job.get("title"), ext))


def start_job(url, option, user_id=""):
    """Завести задачу. Повертає її стан або кидає ValueError із причиною."""
    _sweep()
    if _active_count() >= MAX_ACTIVE:
        raise ValueError(
            f"Зараз качаються {MAX_ACTIVE} відео — зачекай, поки звільниться "
            f"місце (це той самий процес, що веде бота).")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "state": "running", "started": time.time(),
        "percent": None, "done_bytes": 0, "total_bytes": 0, "speed": 0,
        "eta": 0, "stage": "", "error": "", "path": "", "size": 0,
        "filename": "", "title": option.get("title") or "",
        "user": str(user_id),
        "dir": tempfile.mkdtemp(prefix=f"vid_{job_id}_"),
    }
    _JOBS[job_id] = job
    merge = bool(option.get("kind") == "video" and has_ffmpeg())
    job["task"] = asyncio.create_task(
        _run_job(job, url, option["selector"], merge, user_id))
    return job


def job_state(job):
    """Стан задачі для сторінки (без внутрішніх полів)."""
    return {
        "id": job["id"], "state": job["state"], "percent": job["percent"],
        "stage": job["stage"], "error": job["error"],
        "done": human_size(job["done_bytes"]),
        "total": human_size(job["total_bytes"]),
        "speed": (human_size(job["speed"]) + "/с") if job["speed"] else "",
        "eta": human_duration(job["eta"]),
        "filename": job["filename"], "size": human_size(job["size"]),
    }


# ---------- HTTP ----------

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")

_NO_ACCESS = {"error": "Сторінка відкривається лінком із бота: набери /video "
                       "у чаті редакції й тапни кнопку."}


async def page(request):
    """GET /video — сторінка (статичний HTML, усе в одному файлі).

    Сам HTML віддається без перевірки токена свідомо: у ньому немає нічого,
    крім верстки, а перевіряти є що лише на API. Так сторінка ще й може
    сказати людською мовою «зайди через бота» замість голого 403.
    """
    return web.FileResponse(
        os.path.join(_STATIC_DIR, "video.html"),
        headers={"Cache-Control": "no-cache"},
    )


async def api_state(request):
    """GET /api/video/state — чим сервер здатен допомогти саме цій людині.

    Сторінка питає це першою: якщо кук немає, вона має сказати про це ДО того,
    як людина вставить лінк і зачекає п'ять секунд на відмову.
    """
    who = whois(request.query.get("k", ""))
    if not who:
        return web.json_response({"access": False, **_NO_ACCESS}, status=403)
    return web.json_response({
        "access": True,
        "who": who.get("name") or "",
        "cookies": cookie_info(who.get("user_id")),
        "ffmpeg": has_ffmpeg(),
        "max_mb": MAX_BYTES // (1024 * 1024),
    })


async def api_probe(request):
    """POST /api/video/probe {url} → картка відео з варіантами якості."""
    if not access_ok(request):
        return web.json_response(_NO_ACCESS, status=403)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)
    url = (body.get("url") or "").strip()
    if not host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лінки YouTube, Facebook і Instagram."},
            status=400)
    try:
        info = await asyncio.to_thread(probe_cached, url, _user_of(request))
    except Exception as e:
        return web.json_response(
            {"error": humanize_error(str(e), url)}, status=502)
    data = describe(info)
    if not data["options"]:
        return web.json_response(
            {"error": "Не знайшов жодної доріжки, яку можна завантажити."},
            status=422)
    data["url"] = url
    return web.json_response(data)


async def api_start(request):
    """POST /api/video/start {url, option} → id фонової задачі."""
    if not access_ok(request):
        return web.json_response(_NO_ACCESS, status=403)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)
    url = (body.get("url") or "").strip()
    choice = (body.get("option") or "").strip()
    if not host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лінки YouTube, Facebook і Instagram."},
            status=400)
    user_id = _user_of(request)
    try:
        info = await asyncio.to_thread(probe_cached, url, user_id)
    except Exception as e:
        return web.json_response(
            {"error": humanize_error(str(e), url)}, status=502)

    option = next((o for o in curate_formats(info, has_ffmpeg())
                   if o["id"] == choice), None)
    if not option:
        return web.json_response(
            {"error": "Такої якості вже немає — розбери лінк ще раз."},
            status=409)
    option["title"] = info.get("title") or ""
    try:
        job = start_job(url, option, user_id)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=429)
    return web.json_response(job_state(job))


async def api_status(request):
    if not access_ok(request):
        return web.json_response(_NO_ACCESS, status=403)
    job = _JOBS.get(request.query.get("id", ""))
    if not job:
        return web.json_response(
            {"error": "Завантаження не знайшлось — почни спочатку."},
            status=404)
    return web.json_response(job_state(job))


async def api_file(request):
    """GET /api/video/file?id=… — готовий файл вкладенням."""
    if not access_ok(request):
        return web.json_response(_NO_ACCESS, status=403)
    job = _JOBS.get(request.query.get("id", ""))
    if not job or job["state"] != "ready" or not os.path.exists(job["path"]):
        return web.json_response(
            {"error": "Файл уже прибрано — завантаж ще раз."}, status=404)
    name = job["filename"]
    # Кирилиця й емодзі в імені: filename* (RFC 5987) розуміють усі сучасні
    # браузери, простий filename лишається запасним для зовсім старих
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "video.mp4"
    return web.FileResponse(job["path"], headers={
        "Content-Disposition": (f'attachment; filename="{ascii_name}"; '
                                f"filename*=UTF-8''{quote(name)}"),
        "Cache-Control": "no-store",
    })


# ---------- команди бота ----------

def _person_name(update):
    """Імʼя людини для журналу доступів: спершу ростер редакції, далі Telegram."""
    user = update.effective_user
    try:
        from handlers import team_roster
        person = team_roster.resolve_person(user.id, user.username)
        if person:
            return person
    except Exception:
        pass
    return user.full_name or (user.username and "@" + user.username) or str(user.id)


async def video_handler(update, context):
    """/video [лінк] — особистий вхід на сторінку завантаження.

    Лінк найчастіше вже лежить у чаті, тому команда приймає його аргументом і
    підставляє в сторінку — людині лишається обрати якість.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    user = update.effective_user
    arg = " ".join(context.args or []).strip()
    if arg and not host_allowed(arg):
        await update.message.reply_text(
            "Це не схоже на лінк YouTube, Facebook чи Instagram. Приклад:\n"
            "<code>/video https://youtu.be/…</code>", parse_mode="HTML")
        return

    name = await asyncio.to_thread(_person_name, update)
    token = await asyncio.to_thread(token_for, user.id, name)
    link = video_url(token, arg)
    if not link:
        await update.message.reply_text(
            "Сторінка завантаження спить: не налаштований WEBAPP_URL.")
        return

    cookies = await asyncio.to_thread(cookie_info, user.id)
    lines = ["🦊 <b>Завантажити відео</b>",
             "YouTube, Facebook, Instagram. Лінк → вибір якості → файл."]
    if cookies["own"]:
        lines.append("\n🔑 Качає під твоїми куками.")
    elif cookies["shared"] or cookies["env"]:
        lines.append("\n🔑 Качає під редакційними куками.")
    else:
        lines += ["", "⚠️ Кук немає. YouTube працює і без них, а от Facebook "
                      "(рілзи) та Instagram — ні: сервер для них незалогінений "
                      "гість. Щоб качало під тобою — кинь мені в приват файл "
                      "<code>cookies.txt</code> (експорт із браузера "
                      "розширенням «Get cookies.txt LOCALLY»)."]
    lines.append("\n<i>Лінк особистий — він твій вхід, не пересилай його.</i>")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬇️ Відкрити сторінку", url=link)]]))


async def video_cookies_handler(update, context):
    """/video_cookies [off|shared] — стан кук і як їх покласти."""
    user = update.effective_user
    arg = (context.args[0].lower() if context.args else "")

    if arg in ("off", "delete", "прибрати"):
        gone = await asyncio.to_thread(forget_cookies, user.id)
        await update.message.reply_text(
            "🔑 Твої куки прибрав." if gone else "Твоїх кук і не було.")
        return

    info = await asyncio.to_thread(cookie_info, user.id)
    lines = ["🔑 <b>Куки для завантаження відео</b>", ""]
    if info["own"]:
        lines.append(f"Твої куки на місці (покладені {info['at'][:16]}).")
    elif info["shared"]:
        lines.append(f"Своїх кук немає — качаєш під редакційними "
                     f"(поклав {info['by'] or '—'}, {info['at'][:16]}).")
    elif info["env"]:
        lines.append("Своїх кук немає — качаєш під куками зі змінної Railway.")
    else:
        lines.append("Кук немає ні в кого. YouTube працює й так, "
                     "Facebook-рілзи та Instagram — ні.")
    if info["domains"]:
        lines.append("Покривають: " + ", ".join(info["domains"][:8]))
    lines += [
        "",
        "<b>Як покласти свої:</b>",
        "1. Постав у браузер розширення «Get cookies.txt LOCALLY».",
        "2. Залогінься у Facebook (або YouTube/Instagram) і натисни експорт.",
        "3. Кинь файл <code>cookies.txt</code> мені сюди, в приват.",
        "",
        "Порада: краще з окремого акаунта в інкогніто — куки живуть довше, "
        "а головний акаунт не ходить із серверної адреси.",
        "Прибрати свої: <code>/video_cookies off</code>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cookies_document_handler(update, context):
    """cookies.txt, кинутий у приват → куки цієї людини.

    Той самий шлях, яким у боті вже ходять пакети рішень і бюджетні ZIP:
    файл у приваті, де вже стоїть whitelist, — і жодного редеплою заради
    рядка тексту.
    """
    doc = update.message.document
    user = update.effective_user
    try:
        tg_file = await doc.get_file()
        raw = bytes(await tg_file.download_as_bytearray()).decode(
            "utf-8", errors="replace")
    except Exception as e:
        await update.message.reply_text(f"Не зміг прочитати файл: {e}")
        return

    name = await asyncio.to_thread(_person_name, update)
    try:
        domains = await asyncio.to_thread(save_cookies, raw, user.id, name)
    except ValueError as e:
        await update.message.reply_text(
            f"Це не схоже на експорт кук: {e}\n\n"
            f"Потрібен <code>cookies.txt</code> у форматі Netscape — його дає "
            f"розширення «Get cookies.txt LOCALLY».", parse_mode="HTML")
        return

    known = [d for d in domains
             if any(h in d for h in ("facebook", "instagram", "youtube"))]
    lines = ["🔑 Куки прийняв — тепер сторінка качає під тобою.",
             f"Доменів у файлі: {len(domains)}."]
    if known:
        lines.append("Серед них: " + ", ".join(sorted(set(known))[:6]))
    else:
        lines.append("⚠️ Але серед них немає ні facebook, ні instagram, ні "
                     "youtube — схоже, експорт зроблено не на тій вкладці.")
    lines.append("\nПрибрати: /video_cookies off")
    await update.message.reply_text("\n".join(lines))
