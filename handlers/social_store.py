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
from datetime import datetime

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
