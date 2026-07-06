"""
Пам'ять тижневих зрізів соцмереж (Facebook/Instagram) у БД бота (Postgres).

Навіщо саме БД: Meta НЕ дає дістати метрики заднім числом — API віддає охоплення/
взаємодії лише за недавнє фіксоване вікно (~тиждень). Тобто історію соцмереж
неможливо бекфілити; єдиний спосіб її мати — накопичувати знімки. Якщо не знімати
зараз — ця історія втрачається назавжди.

Знімок п'ємо ПІГГІБЕКОМ на недільні звіти FB (15:00) та IG (18:00) — дані там уже
зібрані, жодного зайвого виклику Meta. Ядро метрик — у колонках social_stats,
решта + сирий словник — у raw JSONB (Meta регулярно перейменовує поля: напр. IG
перейшов з reach на views — тому тримаємо reach і views обидва).

Тихо пропускається без BOT_DATABASE_URL — як analytics_store / archive_mirror.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import requests

from handlers import bot_db

FACEBOOK = "facebook"
INSTAGRAM = "instagram"

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
    """/social_capture — зняти зріз FB+IG зараз і покласти в social_stats.
    Знімок автоматично п'ється щонеділі зі звітів; ця команда — щоб засіяти
    першу точку одразу, не чекаючи неділі, і для перевірки."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_ready():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована (BOT_DATABASE_URL) — нема куди зберігати."
        )
        return
    from handlers import facebook as fb, instagram as ig

    msg = await update.message.reply_text("🦊 Знімаю поточний зріз FB та IG…")
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

    await msg.edit_text("🦊 Зріз збережено:\n" + "\n".join(results))
