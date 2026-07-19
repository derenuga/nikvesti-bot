"""
TikTok Display API (v2) через OAuth — для TikTok-блоку таблиці аналітики
(handlers/social_sheet.py).

Що дає (обмеження TikTok, чесно):
- get_user_stats() — поточні підписники (follower_count) + lifetime лайки/відео.
- get_month_video_stats() — сума переглядів/лайків/поширень/коментарів по
  ВІДЕО, опублікованих у місяці (з /v2/video/list/). Перегляди відео —
  lifetime-лічильник кожного відео, тому це «перегляди роликів місяця станом
  на зараз», а не точні перегляди за календарний місяць (TikTok account-level
  monthly views в API немає).
- ОХОПЛЕННЯ (reach), час перегляду, FYP-покази, демографія — у публічному API
  НЕМАЄ (лише в дашборді застосунку). Колонка «Охоплення» лишається ручною.
- ІСТОРІЇ немає: API віддає лише знімок «на зараз», помісячного розрізу
  минулого немає — тому бекфілу як у YouTube нема, тільки вперед. Історія
  TikTok у таблиці — з міграції старої таблиці.

Одноразове налаштування (робить власник акаунта). PRODUCTION TikTok НЕ
одобрює для «internal/personal use» — але нам він і не потрібен: свій аккаунт
читаємо через SANDBOX (без ревью):
1. developers.tiktok.com → app → вкладка Sandbox → створити пісочницю;
   Products: Login Kit + Display API; Scopes: user.info.basic,
   user.info.stats, video.list; Sandbox settings → Target users: додати
   @nikvesti.com (свій акаунт); URL properties: зареєструвати redirect URI
   (напр. https://nikvesti.com/ — сторінка будь-яка, код читаємо з адреси).
2. У Railway: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET (з ВКЛАДКИ SANDBOX,
   не Production) + TIKTOK_REDIRECT_URI (той самий, що зареєстрував).
3. У боті: /tiktok_auth (без аргументів) → бот дасть посилання згоди →
   увійти як @nikvesti, дозволити → скопіювати ?code=… з адреси →
   /tiktok_auth <code>. Бот обміняє код на refresh token і збереже у
   storage.tiktok_oauth. (Ручний TIKTOK_REFRESH_TOKEN у env — необов'язковий
   фолбек-сід, /tiktok_auth зручніший.)

Токени (звірено з developers.tiktok.com/doc/oauth-user-access-token-management):
access token 24 год, refresh token 365 днів; TikTok РОТУЄ refresh token на
кожному оновленні («returned refresh_token may be different — use the new
one») — тому актуальний живе в storage, бот бере новий щоразу. Раз на рік
варто перепройти /tiktok_auth про запас.
"""

import os
import time
from urllib.parse import urlencode, unquote

import requests

from handlers import storage

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"
VIDEO_QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"

SCOPES = "user.info.basic,user.info.stats,video.list"


class TikTokAuthError(RuntimeError):
    """Refresh token недійсний/протух — потрібна переавторизація (/tiktok_auth).
    Окремо від мережевих збоїв, щоб алертити лише коли справді треба re-auth."""


def build_authorize_url(redirect_uri, state="nikvesti"):
    """URL згоди TikTok: відкрити, увійти як @nikvesti (target user sandbox),
    дозволити scopes — TikTok перекине на redirect_uri з ?code=… в адресі."""
    return AUTH_URL + "?" + urlencode({
        "client_key": os.environ.get("TIKTOK_CLIENT_KEY", ""),
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    })


def exchange_code(code, redirect_uri):
    """Обмін authorization code на токени (grant_type=authorization_code) і
    збереження refresh token у storage.tiktok_oauth. code одноразовий і живе
    ~10 хв; TikTok кодує в ньому '*' як %2A — розкодовуємо."""
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (key and secret):
        raise RuntimeError("TIKTOK_CLIENT_KEY/CLIENT_SECRET не задано")
    resp = requests.post(TOKEN_URL, data={
        "client_key": key,
        "client_secret": secret,
        "code": unquote(code.strip()),
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20).json()
    if not resp.get("refresh_token"):
        raise RuntimeError(resp.get("error_description") or resp.get("error") or resp)
    storage.save_tiktok_oauth({
        "access_token": resp.get("access_token"),
        "access_expires_at": time.time() + int(resp.get("expires_in", 86400)),
        "refresh_token": resp["refresh_token"],
    })
    return resp


def is_configured():
    has_creds = bool(os.environ.get("TIKTOK_CLIENT_KEY")
                     and os.environ.get("TIKTOK_CLIENT_SECRET"))
    has_refresh = bool(storage.get_tiktok_oauth().get("refresh_token")
                       or os.environ.get("TIKTOK_REFRESH_TOKEN"))
    return has_creds and has_refresh


def _access_token():
    """Свіжий access token. Кеш і РОТОВАНИЙ refresh token — у storage
    (переживає редеплой). env TIKTOK_REFRESH_TOKEN — лише перший сід."""
    oauth = storage.get_tiktok_oauth()
    now = time.time()
    if oauth.get("access_token") and oauth.get("access_expires_at", 0) - 120 > now:
        return oauth["access_token"]
    key = os.environ.get("TIKTOK_CLIENT_KEY")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    refresh = oauth.get("refresh_token") or os.environ.get("TIKTOK_REFRESH_TOKEN")
    if not (key and secret and refresh):
        raise RuntimeError(
            "TikTok OAuth не налаштовано (TIKTOK_CLIENT_KEY/CLIENT_SECRET/"
            "REFRESH_TOKEN) — див. handlers/tiktok_analytics.py"
        )
    resp = requests.post(TOKEN_URL, data={
        "client_key": key,
        "client_secret": secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20).json()
    if "access_token" not in resp:
        raise TikTokAuthError(f"TikTok refresh не вдався: "
                              f"{resp.get('error_description') or resp.get('error') or resp}")
    new = {
        "access_token": resp["access_token"],
        "access_expires_at": now + int(resp.get("expires_in", 86400)),
        "refresh_token": resp.get("refresh_token") or refresh,  # TikTok ротує
    }
    storage.save_tiktok_oauth(new)
    return new["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_access_token()}"}


def get_user_stats():
    """Поточні лічильники профілю: {'followers', 'likes', 'videos'}."""
    resp = requests.get(USER_URL, params={
        "fields": "follower_count,likes_count,video_count",
    }, headers=_auth_headers(), timeout=20).json()
    err = resp.get("error", {})
    if err and err.get("code") not in ("ok", None):
        raise RuntimeError(err.get("message") or err.get("code"))
    user = resp.get("data", {}).get("user", {})
    return {"followers": user.get("follower_count"),
            "likes": user.get("likes_count"),
            "videos": user.get("video_count")}


def get_videos_in_window(since_ts, until_ts, max_pages=25):
    """Відео у вікні [since, until] (unix) з описом і метриками — для
    семантичного пошуку відео про матеріал (/stat). Гортає /v2/video/list/ від
    найновіших, зупиняється, коли дійшли до відео старших за since. Повертає
    list[dict] з полями fields (video_description/title для зіставлення,
    share_url для лінка, лічильники для метрик)."""
    fields = ("id,create_time,video_description,title,share_url,"
              "view_count,like_count,comment_count,share_count")
    cursor = None
    out = []
    for _ in range(max_pages):
        body = {"max_count": 20}
        if cursor is not None:
            body["cursor"] = cursor
        resp = requests.post(VIDEO_LIST_URL, params={"fields": fields}, json=body,
                             headers=_auth_headers(), timeout=30).json()
        err = resp.get("error", {})
        if err and err.get("code") not in ("ok", None):
            raise RuntimeError(err.get("message") or err.get("code"))
        data = resp.get("data", {})
        videos = data.get("videos", [])
        if not videos:
            break
        reached_older = False
        for v in videos:
            ct = v.get("create_time", 0)
            if ct >= until_ts:
                continue                      # новіше за вікно — пропускаємо
            if ct < since_ts:
                reached_older = True          # найновіші → далі лише старіші
                break
            out.append(v)
        if reached_older or not data.get("has_more"):
            break
        cursor = data.get("cursor")
    return out


def get_videos_by_ids(video_ids):
    """Відео за конкретними id (/v2/video/query/, filters.video_ids ≤20) —
    швидкий шлях /stat: id вже відомі з індексу (article_stats), листинг і
    матчинг не потрібні. Повертає list[dict] у форматі get_videos_in_window."""
    fields = ("id,create_time,video_description,title,share_url,"
              "view_count,like_count,comment_count,share_count")
    out = []
    ids = [str(v) for v in video_ids if v]
    for i in range(0, len(ids), 20):
        resp = requests.post(
            VIDEO_QUERY_URL, params={"fields": fields},
            json={"filters": {"video_ids": ids[i:i + 20]}},
            headers=_auth_headers(), timeout=20,
        ).json()
        err = resp.get("error", {})
        if err and err.get("code") not in ("ok", None):
            raise RuntimeError(err.get("message") or err.get("code"))
        out.extend(resp.get("data", {}).get("videos", []))
    return out


def get_month_video_stats(start_ts, end_ts, max_pages=25):
    """Сума метрик по відео, опублікованих у [start_ts, end_ts) (unix).
    Гортає /v2/video/list/ від найновіших; зупиняється, коли дійшли до відео
    старших за start_ts. Повертає {'videos','views','likes','comments',
    'shares'} або None, якщо в місяці відео немає."""
    fields = "id,create_time,view_count,like_count,comment_count,share_count"
    cursor = None
    agg = {"videos": 0, "views": 0, "likes": 0, "comments": 0, "shares": 0}
    for _ in range(max_pages):
        body = {"max_count": 20}
        if cursor is not None:
            body["cursor"] = cursor
        resp = requests.post(VIDEO_LIST_URL, params={"fields": fields}, json=body,
                             headers=_auth_headers(), timeout=30).json()
        err = resp.get("error", {})
        if err and err.get("code") not in ("ok", None):
            raise RuntimeError(err.get("message") or err.get("code"))
        data = resp.get("data", {})
        videos = data.get("videos", [])
        if not videos:
            break
        reached_older = False
        for v in videos:
            ct = v.get("create_time", 0)
            if ct >= end_ts:
                continue                      # новіше за місяць — пропускаємо
            if ct < start_ts:
                reached_older = True          # найновіші → далі лише старіші
                break
            agg["videos"] += 1
            agg["views"] += v.get("view_count", 0) or 0
            agg["likes"] += v.get("like_count", 0) or 0
            agg["comments"] += v.get("comment_count", 0) or 0
            agg["shares"] += v.get("share_count", 0) or 0
        if reached_older or not data.get("has_more"):
            break
        cursor = data.get("cursor")
    return agg if agg["videos"] else None
