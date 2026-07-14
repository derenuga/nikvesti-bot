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

Одноразове налаштування (робить власник акаунта):
1. developers.tiktok.com → створити app → додати продукт «Login Kit» +
   «Display API»; scopes: user.info.basic, user.info.stats, video.list.
   Пройти App review (TikTok перевіряє вручну — дні).
2. Дістати refresh token OAuth-флоу від власника акаунта @nikvesti.com.
3. У Railway: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN
   (останній — сід; далі бот сам ротує й тримає у storage.tiktok_oauth).

УВАГА: TikTok ротує refresh token на кожному оновленні — тому актуальний
токен живе в storage, а env лише сідує перший раз.
"""

import os
import time

import requests

from handlers import storage

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"


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
        raise RuntimeError(f"TikTok refresh не вдався: "
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
