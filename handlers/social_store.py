"""
Пам'ять соцмереж у БД бота (Postgres): тижневі зрізи + місячна історія.

Навіщо саме БД: Meta НЕ дає дістати метрики заднім числом — API віддає охоплення/
взаємодії лише за недавнє фіксоване вікно (~тиждень). Тобто історію соцмереж
неможливо бекфілити; єдиний спосіб її мати — накопичувати знімки. Якщо не знімати
зараз — ця історія втрачається назавжди.

ДВА ГРЕЙНИ, і кожен зі своєї причини:

1. `social_stats` — ТИЖНЕВІ зрізи. Facebook та Instagram п'ємо піггібеком на
   недільні звіти (15:00 / 18:00) — дані там уже зібрані, жодного зайвого
   виклику Meta. Telegram / TikTok / YouTube / Viber — окремим тихим захватом
   у неділю (`capture_rest`): у них немає «звіту в чат», на який можна
   присісти, а тижневий розріз потрібен дашборду Mini App.

2. `social_monthly` — МІСЯЧНА історія всіх мереж. Досі вона жила лише в
   Google-таблиці «Аналітика МикВісті»: місячний знімок писав її туди й
   забував (питання Олега 27.07 — «нора має зберігати весь наш прогрес»).
   Тепер той самий знімок осідає і в Норі (`record_month`, source='api'), а
   накопичену історію таблиці — включно з перенесеною старою ручною з 2024-02 —
   заливає `/social_import_sheet` (handlers/social_import.py, source='sheet').

Ядро метрик — у колонках, решта + сирий словник — у raw/extra JSONB (Meta
регулярно перейменовує поля: напр. IG перейшов з reach на views — тому тримаємо
reach і views обидва; YouTube має години перегляду, TikTok — лайки/поширення).

Тихо пропускається без BOT_DATABASE_URL — як analytics_store / archive_mirror.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from handlers import bot_db

KYIV_TZ = ZoneInfo("Europe/Kiev")

FACEBOOK = "facebook"
INSTAGRAM = "instagram"
TELEGRAM = "telegram"
TIKTOK = "tiktok"
YOUTUBE = "youtube"
VIBER = "viber"

# Порядок і людські назви — спільні для дашборда Mini App і звітів
PLATFORM_TITLES = {
    FACEBOOK: "Facebook", INSTAGRAM: "Instagram", TELEGRAM: "Telegram",
    TIKTOK: "TikTok", YOUTUBE: "YouTube", VIBER: "Viber",
}
PLATFORM_ORDER = [FACEBOOK, INSTAGRAM, TELEGRAM, TIKTOK, YOUTUBE, VIBER]

# Ключі блоків місячного знімка (social_sheet) → платформи Нори
SHEET_BLOCK_PLATFORM = {
    "fb": FACEBOOK, "ig": INSTAGRAM, "tg": TELEGRAM,
    "tt": TIKTOK, "yt": YOUTUBE, "vb": VIBER,
}

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


def is_ready():
    return bot_db.is_configured()


def _to_int(value):
    """Метрики Meta інколи приходять рядком/None — акуратно в int або None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


async def _record(platform, followers, reach, views, engagement, posts, raw):
    if not is_ready():
        return
    week_end = datetime.now().strftime("%Y-%m-%d")
    row = (
        platform, week_end,
        _to_int(followers), _to_int(reach), _to_int(views),
        _to_int(engagement), _to_int(posts),
        json.dumps(raw, ensure_ascii=False) if raw else None,
    )
    await asyncio.to_thread(bot_db.upsert_social_stats, [row])


async def capture_facebook(page, stats, total_posts, total_reels):
    """Знімок FB зі зібраних даних недільного звіту. Meta задепрекейтила
    impressions-родину (лист. 2025) → охоплення тепер page_media_view (перегляди
    контенту), тому пишемо у колонку views (reach=None, як і IG). Взаємодії —
    page_post_engagements; підписники — followers_count (page_fans задепрекейчено,
    альтернатива page_follows)."""
    followers = page.get("followers_count") or stats.get("page_follows")
    views = stats.get("page_media_view")
    engagement = stats.get("page_post_engagements")
    raw = {
        "followers_count": page.get("followers_count"),
        "page_media_view": stats.get("page_media_view"),
        "page_post_engagements": stats.get("page_post_engagements"),
        "page_follows": stats.get("page_follows"),
        "total_posts": total_posts,
        "total_reels": total_reels,
    }
    await _record(FACEBOOK, followers, None, views, engagement,
                  (total_posts or 0) + (total_reels or 0), raw)


async def capture_instagram(profile, stats, follows, unfollows, total_posts, reels):
    """Знімок IG зі зібраних даних недільного звіту. IG перейшов з reach на
    views — зберігаємо обидва (views як основне охоплення), взаємодії —
    total_interactions."""
    followers = profile.get("followers_count")
    reach = stats.get("reach")
    views = stats.get("views")
    engagement = stats.get("total_interactions")
    raw = {
        "reach": stats.get("reach"),
        "views": stats.get("views"),
        "total_interactions": stats.get("total_interactions"),
        "accounts_engaged": stats.get("accounts_engaged"),
        "follows_gained": follows,
        "follows_lost": unfollows,
        "total_posts": total_posts,
        "reels": reels,
    }
    await _record(INSTAGRAM, followers, reach, views, engagement, total_posts, raw)


# ---------- Тижневий захват решти мереж (Telegram/TikTok/YouTube/Viber) ----------
#
# У FB та IG знімок піггібеком на недільний звіт; у цих чотирьох звіту в чат
# немає, тож ходимо по джерела самі — раз на тиждень це дешево (TG: ~8 сторінок
# стрічки t.me; TikTok/YouTube: 2-3 запити API; Viber: одна публічна сторінка
# запрошення — його API метрик аудиторії не віддає).
#
# Вікно — СІМ ДНІВ, що закінчуються сьогодні, як і в Meta-звітах: тижневий зріз
# має означати те саме, з якої мережі його не взяти.

WEEK_DAYS = 7


def _week_window(now=None):
    """(start, end) вікна тижневого зрізу: [сьогодні−6 днів 00:00 за Києвом,
    зараз]. Дати — З ТАЙМЗОНОЮ: стрічка t.me віддає час із зоною, і порівняти
    його з наївним datetime не вийде (TypeError)."""
    now = now or datetime.now(KYIV_TZ)
    start = (now - timedelta(days=WEEK_DAYS - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start, now


async def capture_telegram():
    """Зріз Telegram: підписники (t.me) + пости й перегляди за тиждень."""
    from handlers import telegram_stats as tg

    start, end = _week_window()
    followers = await asyncio.to_thread(tg.channel_subscribers)
    window = await asyncio.to_thread(tg.collect_window, start, end)
    raw = {"avg_views": window.get("avg_views"), "window_days": WEEK_DAYS}
    await _record(TELEGRAM, followers, None, window.get("views_total"),
                  None, window.get("posts"), raw)
    return {"followers": followers, "views": window.get("views_total"),
            "posts": window.get("posts")}


async def capture_tiktok():
    """Зріз TikTok: підписники + метрики відео, опублікованих за тиждень.
    Охоплення TikTok API не віддає — лишається None назавжди."""
    from handlers import tiktok_analytics as tt

    if not tt.is_configured():
        raise RuntimeError("TikTok OAuth не налаштовано (/tiktok_auth)")
    start, end = _week_window()
    stats = await asyncio.to_thread(tt.get_user_stats)
    vids = await asyncio.to_thread(
        tt.get_month_video_stats, int(start.timestamp()), int(end.timestamp()))
    vids = vids or {}
    engagement = None
    parts = [vids.get(k) for k in ("likes", "comments", "shares")]
    if any(p is not None for p in parts):
        engagement = sum(p or 0 for p in parts)
    raw = {"likes": vids.get("likes"), "comments": vids.get("comments"),
           "shares": vids.get("shares"), "total_likes": stats.get("likes"),
           "window_days": WEEK_DAYS}
    await _record(TIKTOK, stats.get("followers"), None, vids.get("views"),
                  engagement, vids.get("videos"), raw)
    return {"followers": stats.get("followers"), "views": vids.get("views"),
            "posts": vids.get("videos")}


async def capture_youtube():
    """Зріз YouTube: підписники (Data API) + перегляди й години перегляду за
    тиждень (Analytics API приймає довільний діапазон дат)."""
    from handlers import youtube_analytics as yt

    if not yt.is_configured():
        raise RuntimeError("YouTube OAuth не налаштовано (YOUTUBE_OAUTH_*)")
    start, end = _week_window()
    stats = await asyncio.to_thread(yt.get_channel_stats)
    totals = await asyncio.to_thread(
        yt.get_month_totals, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    totals = totals or {}
    raw = {"watch_hours": totals.get("watch_hours"),
           "lifetime_views": stats.get("views"), "window_days": WEEK_DAYS}
    await _record(YOUTUBE, stats.get("subscribers"), None, totals.get("views"),
                  None, None, raw)
    return {"followers": stats.get("subscribers"), "views": totals.get("views")}


async def capture_viber():
    """Зріз Viber: підписники — з публічної сторінки запрошення каналу
    (viber_mirror.channel_followers). Channels Post API метрик аудиторії не
    віддає взагалі — старий код читав неіснуюче subscribers_count із
    get_account_info і мовчки писав None (тому в social_stats у Viber досі
    не було жодного числа). Інших тижневих метрик немає: пости дзеркала —
    місячний лічильник storage."""
    from handlers import viber_mirror as vb

    followers = await asyncio.to_thread(vb.channel_followers)
    await _record(VIBER, followers, None, None, None, None,
                  {"window_days": WEEK_DAYS, "source": "invite_page"})
    return {"followers": followers}


async def capture_rest():
    """Тижневий зріз усіх мереж, крім FB/IG (їх п'ємо піггібеком на звіти).
    Кожна мережа окремо: ненастроєна чи впала — свій рядок звіту, решта
    записується. Повертає {платформа: "✅ …"/"⛔ …"} для лога і /social_capture."""
    out = {}
    for platform, fn in ((TELEGRAM, capture_telegram), (TIKTOK, capture_tiktok),
                         (YOUTUBE, capture_youtube), (VIBER, capture_viber)):
        title = PLATFORM_TITLES[platform]
        try:
            data = await fn()
            bits = [f"підписників {data.get('followers')}"]
            if data.get("views") is not None:
                bits.append(f"переглядів {data['views']}")
            if data.get("posts") is not None:
                bits.append(f"постів {data['posts']}")
            out[platform] = f"{title} ✅ " + ", ".join(bits)
        except Exception as e:
            out[platform] = f"{title} ⛔ {e}"
    return out


async def run_weekly_capture(bot=None):
    """Планове завдання (неділя): тихий тижневий зріз решти мереж. Пише в лог,
    у чат нічого не сипле — це пам'ять, а не звіт."""
    if not is_ready():
        return
    results = await capture_rest()
    for line in results.values():
        print(f"social_store: {line}")


# ---------- Місячна історія (social_monthly) ----------

def _month_row(platform, month_date, followers=None, reach=None, views=None,
               engagement=None, posts=None, extra=None, source="api"):
    clean_extra = {k: v for k, v in (extra or {}).items() if v is not None}
    return (platform, month_date, _to_int(followers), _to_int(reach), _to_int(views),
            _to_int(engagement), _to_int(posts),
            json.dumps(clean_extra, ensure_ascii=False) if clean_extra else None,
            source)


async def record_month(year, month, blocks, source="api"):
    """Кладе місячний знімок соцмереж у Нору. blocks — словник результатів
    збирачів social_sheet: {"fb": {...}, "ig": {...}, "tg": {...}, "tt": {...},
    "yt": {...}, "vb": {...}}; None-блоки пропускаються.

    Викликається з місячного знімка таблиці — щоб ті самі числа, які пішли в
    Google-таблицю, лишались і в боті. Ковтати помилку тут не треба: виклик у
    social_sheet обгорнутий, бо знімок таблиці не має падати через Нору."""
    if not is_ready():
        return 0
    month_date = f"{year:04d}-{month:02d}-01"
    rows = []
    for key, data in (blocks or {}).items():
        platform = SHEET_BLOCK_PLATFORM.get(key)
        if not platform or not data:
            continue
        if platform == TELEGRAM:
            rows.append(_month_row(
                platform, month_date, followers=data.get("subscribers"),
                views=data.get("views_total"), posts=data.get("posts"),
                extra={"avg_views": data.get("avg_views")}, source=source))
        elif platform == VIBER:
            rows.append(_month_row(
                platform, month_date, followers=data.get("subscribers"),
                posts=data.get("posts"), source=source))
        elif platform == TIKTOK:
            parts = [data.get(k) for k in ("likes", "comments", "shares")]
            engagement = (sum(p or 0 for p in parts)
                          if any(p is not None for p in parts) else None)
            rows.append(_month_row(
                platform, month_date, followers=data.get("followers"),
                views=data.get("views"), engagement=engagement,
                extra={"likes": data.get("likes"), "shares": data.get("shares"),
                       "comments": data.get("comments")}, source=source))
        elif platform == YOUTUBE:
            rows.append(_month_row(
                platform, month_date, followers=data.get("followers"),
                views=data.get("views"), posts=data.get("videos"),
                extra={"watch_hours": data.get("watch_hours")}, source=source))
        elif platform == INSTAGRAM:
            rows.append(_month_row(
                platform, month_date, followers=data.get("followers"),
                reach=data.get("reach"), views=data.get("views"),
                engagement=data.get("interactions"), posts=data.get("posts"),
                source=source))
        else:  # facebook
            rows.append(_month_row(
                platform, month_date, followers=data.get("followers"),
                views=data.get("views"), engagement=data.get("engagement"),
                posts=data.get("posts"), source=source))
    if not rows:
        return 0
    return await asyncio.to_thread(bot_db.upsert_social_monthly, rows)


def get_month_rows(start_month, end_month):
    """Місячні рядки соцмереж у діапазоні [start_month, end_month] (дати першого
    числа). list[dict] platform/month/followers/reach/views/engagement/posts/
    extra/source. [] без БД бота."""
    if not is_ready():
        return []
    return bot_db.query(
        "SELECT platform, to_char(month, 'YYYY-MM-DD') AS month, followers, reach, "
        "views, engagement, posts, extra, source FROM social_monthly "
        "WHERE month BETWEEN %s AND %s ORDER BY month, platform",
        (start_month, end_month),
    )


def month_coverage():
    """Що вже є в місячній історії: [{platform, months, oldest, newest}] —
    для звіту імпорту й діагностики."""
    if not is_ready():
        return []
    return bot_db.query(
        "SELECT platform, count(*) AS months, to_char(min(month), 'YYYY-MM') AS oldest, "
        "to_char(max(month), 'YYYY-MM') AS newest FROM social_monthly "
        "GROUP BY platform ORDER BY platform"
    )


# ---------- Читання (NLQ-tool get_social_history) ----------

def get_history(platform=None, limit=12):
    """Історія тижневих зрізів соцмереж, найсвіжіші перші. platform —
    'facebook'/'instagram' або None (обидві). Синхронна (виклик з NLQ через
    to_thread). [] якщо БД бота не налаштована."""
    if not is_ready():
        return []
    limit = min(max(int(limit), 1), 60)
    if platform:
        return bot_db.query(
            "SELECT platform, to_char(week_end, 'YYYY-MM-DD') AS week_end, "
            "followers, reach, views, engagement, posts "
            "FROM social_stats WHERE platform = %s ORDER BY week_end DESC LIMIT %s",
            (platform, limit),
        )
    return bot_db.query(
        "SELECT platform, to_char(week_end, 'YYYY-MM-DD') AS week_end, "
        "followers, reach, views, engagement, posts "
        "FROM social_stats ORDER BY week_end DESC, platform LIMIT %s",
        (limit,),
    )


# ---------- Історичний бекфіл FB (експериментальний) ----------
#
# Meta НЕ віддає історію IG-охоплення, а от FB Page Insights приймає since/until
# і МОЖЕ повернути тижневі бакети за ~1-2 роки: page_post_engagements (взаємодії)
# має довгу історію, page_media_view (перегляди) — лише відколи Meta його рахує
# (~2025). Що віддасть — те й збережемо; чого нема — просто пропуститься.
# Followers/reach історично недоступні (лишаються NULL). Вставляємо ЛИШЕ відсутні
# тижні (insert_social_stats_missing) — реальні недільні знімки не чіпаємо.

FB_HISTORY_METRICS = {"page_media_view": "views", "page_post_engagements": "engagement"}
FB_WINDOW_DAYS = 90  # Meta обмежує вікно insights-запиту ~93 днями — гортаємо по 90


def _fetch_fb_weekly(metric, since_ts, until_ts):
    """Тижневі бакети однієї FB-метрики за вікно [since, until]. Повертає
    list[(week_end 'YYYY-MM-DD', value)]. Кидає при помилці API."""
    page_id = os.environ.get("FACEBOOK_PAGE_ID")
    token = os.environ.get("FACEBOOK_PAGE_TOKEN")
    url = f"https://graph.facebook.com/v19.0/{page_id}/insights"
    data = requests.get(url, params={
        "metric": metric, "period": "week",
        "since": since_ts, "until": until_ts, "access_token": token,
    }, timeout=30).json()
    if "error" in data:
        raise Exception(data["error"].get("message"))
    out = []
    for item in data.get("data", []):
        for v in item.get("values", []):
            end_time = v.get("end_time")
            if end_time:
                out.append((end_time[:10], v.get("value")))  # ISO → YYYY-MM-DD
    return out


async def backfill_facebook(months=24):
    """Історичний бекфіл FB: гортає вікна по FB_WINDOW_DAYS назад на `months`
    місяців, збирає тижневі перегляди/взаємодії. Вставляє лише відсутні тижні.
    Повертає (кількість вставлених рядків, список помилок метрик)."""
    if not is_ready():
        raise RuntimeError("БД бота не налаштована (BOT_DATABASE_URL).")
    now = datetime.now()
    start = now - timedelta(days=int(months * 30.5))
    buckets = {}   # week_end -> {"views": .., "engagement": ..}
    errors = []
    for metric, col in FB_HISTORY_METRICS.items():
        cursor = start
        while cursor < now:
            window_end = min(cursor + timedelta(days=FB_WINDOW_DAYS), now)
            try:
                pairs = await asyncio.to_thread(
                    _fetch_fb_weekly, metric,
                    int(cursor.timestamp()), int(window_end.timestamp()),
                )
            except Exception as e:
                errors.append(f"{metric}: {e}")
                break  # метрика історично недоступна — далі не мучимо
            for week_end, value in pairs:
                buckets.setdefault(week_end, {})[col] = _to_int(value)
            cursor = window_end
    rows = [
        (FACEBOOK, week_end, None, None, m.get("views"), m.get("engagement"), None,
         json.dumps({"backfilled": True, **m}, ensure_ascii=False))
        for week_end, m in sorted(buckets.items())
    ]
    inserted = await asyncio.to_thread(bot_db.insert_social_stats_missing, rows) if rows else 0
    return inserted, errors


async def social_backfill_fb_handler(update, context):
    """/social_backfill_fb [місяців] — спроба залити історію FB (перегляди/взаємодії
    по тижнях) за N місяців (дефолт 24). IG історію Meta не віддає — тільки вперед."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await update.message.reply_text("🦊 БД бота не налаштована (BOT_DATABASE_URL).")
        return
    months = 24
    if context.args:
        try:
            months = max(1, int(context.args[0]))
        except ValueError:
            pass
    msg = await update.message.reply_text(
        f"🦊 Пробую витягти історію FB за ~{months} міс (Meta може віддати не все)…"
    )
    try:
        inserted, errors = await backfill_facebook(months)
        note = f"✅ FB: додано {inserted} історичних тижнів."
        if errors:
            note += "\n⚠️ Частину метрик Meta не віддала: " + "; ".join(errors[:3])
        if not inserted and not errors:
            note = "🦊 Meta не повернула історичних тижнів (типово для агрегованих метрик)."
        note += "\nIG історію Meta не віддає — там тільки знімки вперед."
        await msg.edit_text(note)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")


# ---------- Ручний засів (/social_capture) ----------

async def social_capture_handler(update, context):
    """/social_capture — зняти зріз УСІХ мереж зараз і покласти в social_stats.

    FB та IG знімаються піггібеком на недільні звіти, решта (Telegram, TikTok,
    YouTube, Viber) — окремим захватом у неділю о 18:30. Ця команда робить те
    саме руками: засіяти першу точку одразу, не чекаючи неділі, і перевірити,
    що кожне джерело справді відповідає."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована (BOT_DATABASE_URL) — нема куди зберігати."
        )
        return
    from handlers import facebook as fb, instagram as ig

    msg = await update.message.reply_text(
        "🦊 Знімаю поточний зріз усіх мереж (FB, IG, Telegram, TikTok, YouTube, Viber)…\n"
        "Telegram гортає стрічку — це до пів хвилини.")
    results = []

    try:
        page = await asyncio.to_thread(fb.get_page_followers)
        stats = await asyncio.to_thread(fb.get_page_stats)
        _, total_posts = await asyncio.to_thread(fb.get_top_posts)
        _, total_reels = await asyncio.to_thread(fb.get_top_reels)
        await capture_facebook(page, stats, total_posts, total_reels)
        results.append(
            f"📘 FB ✅ підписників {page.get('followers_count') or stats.get('page_follows')}, "
            f"переглядів {stats.get('page_media_view')}"
        )
    except Exception as e:
        results.append(f"📘 FB ❌ {e}")

    try:
        profile = await asyncio.to_thread(ig.get_instagram_profile)
        stats = await asyncio.to_thread(ig.get_instagram_stats)
        follows, unfollows = await asyncio.to_thread(ig.get_follows_week)
        counts = await asyncio.to_thread(ig.get_media_counts)
        total_posts = sum(counts.values())
        reels = counts.get("VIDEO", 0)
        await capture_instagram(profile, stats, follows, unfollows, total_posts, reels)
        results.append(
            f"📱 IG ✅ підписників {profile.get('followers_count')}, "
            f"переглядів {stats.get('views')} (охоплення {stats.get('reach')})"
        )
    except Exception as e:
        results.append(f"📱 IG ❌ {e}")

    # Решта мереж — тим самим кодом, що й плановий недільний захват
    rest = await capture_rest()
    results += [rest[p] for p in PLATFORM_ORDER if p in rest]

    await msg.edit_text("🦊 Зріз збережено:\n" + "\n".join(results))
