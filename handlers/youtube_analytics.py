"""
YouTube Analytics API (+ Data API) через OAuth — для YouTube-блоку таблиці
аналітики (handlers/social_sheet.py).

Чому OAuth, а не простий ключ / сервісний акаунт: метрики каналу (перегляди,
години перегляду, лайки/коментарі) дає лише YouTube Analytics API, а Google
його НЕ дозволяє ні через API key, ні через service account — потрібен
OAuth 2.0 з входом власника каналу («there is no way to link a Service
Account to a YouTube account», developers.google.com/youtube/reporting).

Одноразове налаштування (робить власник каналу):
1. Google Cloud → APIs & Services: увімкнути «YouTube Analytics API» і
   «YouTube Data API v3».
2. Credentials → створити OAuth client ID типу «Desktop app» → отримати
   client_id і client_secret.
3. Дістати refresh token (найпростіше — developers.google.com/oauthplayground:
   ⚙ → «Use your own OAuth credentials» → вставити client_id/secret →
   авторизувати scope `https://www.googleapis.com/auth/yt-analytics.readonly`
   акаунтом власника каналу → Exchange authorization code → скопіювати
   Refresh token).
4. У Railway: YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET /
   YOUTUBE_OAUTH_REFRESH_TOKEN.

Що дає:
- get_channel_stats() — поточні lifetime-лічильники (Data API через той самий
  OAuth-токен): підписники, всього переглядів, всього відео.
- get_month_totals(start, end) — агрегат за місяць: перегляди + години
  перегляду (для щомісячного знімка).
- get_monthly_report(start, end) — ПОМІСЯЧНИЙ розріз за діапазон (dimension=
  month): один запит віддає всю історію каналу — перегляди й години перегляду
  по місяцях. Це і є історичний бекфіл YouTube.

CTR/покази через API стабільно не віддаються (живуть у Studio) — колонка CTR
лишається ручною.
"""

import os
import time

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
DATA_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

_token_cache = {"access_token": None, "expires_at": 0}


def is_configured():
    return all(os.environ.get(k) for k in (
        "YOUTUBE_OAUTH_CLIENT_ID",
        "YOUTUBE_OAUTH_CLIENT_SECRET",
        "YOUTUBE_OAUTH_REFRESH_TOKEN",
    ))


def _access_token():
    """Свіжий access token із refresh token (кеш до закінчення строку)."""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 60 > now:
        return _token_cache["access_token"]
    if not is_configured():
        raise RuntimeError(
            "YouTube OAuth не налаштовано (YOUTUBE_OAUTH_CLIENT_ID/"
            "CLIENT_SECRET/REFRESH_TOKEN) — див. handlers/youtube_analytics.py"
        )
    resp = requests.post(TOKEN_URL, data={
        "client_id": os.environ["YOUTUBE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_OAUTH_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_OAUTH_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=20).json()
    if "access_token" not in resp:
        raise RuntimeError(f"OAuth refresh не вдався: {resp.get('error_description') or resp}")
    _token_cache["access_token"] = resp["access_token"]
    _token_cache["expires_at"] = now + int(resp.get("expires_in", 3600))
    return _token_cache["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_access_token()}"}


def get_channel_stats():
    """Поточні lifetime-лічильники власного каналу (Data API через OAuth):
    {'subscribers', 'views', 'videos'}."""
    resp = requests.get(DATA_CHANNELS_URL, params={
        "part": "statistics", "mine": "true",
    }, headers=_auth_headers(), timeout=20).json()
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message"))
    items = resp.get("items")
    if not items:
        raise RuntimeError("канал власника токена не знайдено (mine=true)")
    st = items[0]["statistics"]
    return {"subscribers": int(st.get("subscriberCount", 0)),
            "views": int(st.get("viewCount", 0)),
            "videos": int(st.get("videoCount", 0))}


def _query_reports(start_date, end_date, dimensions=None):
    params = {
        "ids": "channel==MINE",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": "views,estimatedMinutesWatched",
    }
    if dimensions:
        params["dimensions"] = dimensions
        params["sort"] = dimensions
    resp = requests.get(ANALYTICS_URL, params=params,
                        headers=_auth_headers(), timeout=30).json()
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message"))
    return resp


def get_month_totals(start_date, end_date):
    """Агрегат за період (без розрізу): {'views', 'watch_hours'} або None,
    якщо даних немає."""
    resp = _query_reports(start_date, end_date)
    rows = resp.get("rows")
    if not rows:
        return None
    views, minutes = int(rows[0][0]), int(rows[0][1])
    return {"views": views, "watch_hours": round(minutes / 60, 1)}


def get_monthly_report(start_date, end_date):
    """Помісячний розріз за діапазон (dimension=month) — історичний бекфіл.
    Повертає list[{'month': 'YYYY-MM', 'views', 'watch_hours'}] від давніх
    до свіжих. Один запит покриває всю історію каналу."""
    resp = _query_reports(start_date, end_date, dimensions="month")
    out = []
    for row in resp.get("rows", []):
        month = str(row[0])[:7]  # API віддає 'YYYY-MM'
        out.append({"month": month, "views": int(row[1]),
                    "watch_hours": round(int(row[2]) / 60, 1)})
    return out
