import asyncio
import os
import time

import requests
from datetime import datetime, timedelta
from handlers.helpers import parse_month_arg

INSTAGRAM_TOKEN = os.environ.get("INSTAGRAM_TOKEN")
FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
INSTAGRAM_USER_ID = "17841400860799899"

# ---------- Якими дверима заходити в інсту ----------
#
# Meta має ДВА продукти на ті самі дані, і токен від одного не завжди годиться
# другому: «Instagram API with Instagram Login» живе на graph.instagram.com, а
# «Instagram API with Facebook Login» — на graph.facebook.com, і в другому
# випадку читати наш IG-акаунт може ЗВИЧАЙНИЙ токен сторінки Facebook, бо
# акаунт до неї прив'язаний. Модуль роками ходив лише в перші двері.
#
# 21.08.2026 це стало дорогим: INSTAGRAM_TOKEN протух 12 серпня, і на питання
# «як зробити новий» чесна відповідь була «спершу з'ясуй, якого він типу» —
# тобто людина мусила розбиратись у продуктовій плутанині Meta, щоб бот
# порахував лайки. Тому двері тепер ПІДБИРАЮТЬСЯ: пробуємо по черзі, беремо
# перші, що відповіли, і кешуємо. Поки INSTAGRAM_TOKEN живий, вибір той
# самий, що й був, — поведінка не змінюється ні на байт.
IG_ROUTES = (
    ("https://graph.instagram.com/v21.0", "INSTAGRAM_TOKEN"),
    ("https://graph.facebook.com/v21.0", "INSTAGRAM_TOKEN"),
    ("https://graph.facebook.com/v21.0", "FACEBOOK_PAGE_TOKEN"),
)
_ROUTE_TTL = 600          # секунд; двері не міняються щохвилини
_route_cache = {"at": 0.0, "value": None}


def _token_by_name(name):
    return INSTAGRAM_TOKEN if name == "INSTAGRAM_TOKEN" else FACEBOOK_PAGE_TOKEN


def probe_route(base, token):
    """Чи відкриваються ці двері: найдешевший запит по нашому IG-акаунту.
    → (True, None) | (False, текст помилки)."""
    if not token:
        return False, "змінної немає"
    try:
        data = requests.get(f"{base}/{INSTAGRAM_USER_ID}",
                            params={"fields": "id", "access_token": token},
                            timeout=15).json()
    except Exception as e:
        return False, str(e)
    if isinstance(data, dict) and "error" in data:
        return False, data["error"].get("message") or "невідома помилка"
    return True, None


def credentials(force=False):
    """(base_url, token) — двері, якими інста читається ЗАРАЗ.
    Кешується на _ROUTE_TTL: підбір коштує один запит, а всі виклики модуля
    ходять тими самими дверима, поки кеш живий."""
    now = time.time()
    if not force and _route_cache["value"] and now - _route_cache["at"] < _ROUTE_TTL:
        return _route_cache["value"]
    for base, token_name in IG_ROUTES:
        token = _token_by_name(token_name)
        ok, err = probe_route(base, token)
        if ok:
            _route_cache.update(at=now, value=(base, token))
            return base, token
        print(f"instagram: {base} + {token_name} — {err}")
    # Жодні двері не відкрились. Повертаємо ПЕРШІ, а не (None, None): виклик
    # усе одно впаде, але впаде з РЕАЛЬНИМ текстом Meta («Session has
    # expired»), який можна показати людині, а не з «Invalid URL 'None/…'»
    # від urllib. Пояснювати треба причину, а не наслідок нашої заглушки.
    fallback = (IG_ROUTES[0][0], _token_by_name(IG_ROUTES[0][1]))
    _route_cache.update(at=now, value=fallback)
    return fallback


def route_report():
    """Діагностика для /ig_token: які двері відкрились, а які ні. Значень
    токенів НЕ показує — лише назви змінних і відповідь Meta."""
    rows = []
    for base, token_name in IG_ROUTES:
        ok, err = probe_route(base, _token_by_name(token_name))
        rows.append({"base": base, "token": token_name, "ok": ok, "error": err})
    return rows

def get_instagram_profile():
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}"
    params = {
        "fields": "followers_count,media_count",
        "access_token": token
    }
    return requests.get(url, params=params).json()

def get_instagram_stats(since_ts=None, until_ts=None):
    """Зведені метрики акаунта. Без аргументів — фіксований тиждень Meta
    (period=week), як у тижневому звіті. Зі since/until — тотал за довільне
    вікно тією самою схемою period=day + metric_type=total_value, якою
    get_follows_week давно ходить у проді. УВАГА: reach за вікно Meta рахує
    сумою денних охоплень — людина, що заходила в різні дні, порахується
    повторно; для чесного порівняння це треба озвучувати."""
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "reach,views,total_interactions,accounts_engaged",
        "period": "week",
        "metric_type": "total_value",
        "access_token": token
    }
    if since_ts:
        params["period"] = "day"
        params["since"] = since_ts
        params["until"] = until_ts or int(datetime.now().timestamp())
    data = requests.get(url, params=params).json()
    if "error" in data:
        raise Exception(data["error"]["message"])
    stats = {}
    for item in data.get("data", []):
        stats[item["name"]] = item.get("total_value", {}).get("value", 0)
    return stats

def get_follows_week(since=None, until=None):
    if since is None:
        since = int((datetime.now() - timedelta(days=7)).timestamp())
    if until is None:
        until = int(datetime.now().timestamp())
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}/insights"
    params = {
        "metric": "follows_and_unfollows",
        "period": "day",
        "metric_type": "total_value",
        "breakdown": "follow_type",
        "since": since,
        "until": until,
        "access_token": token
    }
    data = requests.get(url, params=params).json()
    try:
        follows = 0
        unfollows = 0
        for day in data["data"]:
            for result in day["total_value"]["breakdowns"][0]["results"]:
                if result["dimension_values"][0] == "FOLLOWER":
                    follows += result["value"]
                elif result["dimension_values"][0] == "NON_FOLLOWER":
                    unfollows += result["value"]
        return follows, unfollows
    except:
        return None, None

def get_top_media(since=None, until=None, max_pages=3):
    """Топ-5 медіа вікна за лайками+коментарями. З пагінацією: одна сторінка
    (limit=100) місяць уже не вміщує, і топ рахувався б по обрізку — лайки
    приходять у самому листингу, тож догортати дешево."""
    if since is None:
        since = int((datetime.now() - timedelta(days=7)).timestamp())
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "id,media_type,permalink,like_count,comments_count,caption,timestamp",
        "since": since,
        "access_token": token,
        "limit": 100
    }
    if until:
        params["until"] = until
    media = []
    for _ in range(max_pages):
        data = requests.get(url, params=params).json()
        if "error" in data:
            break
        media.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}  # next уже містить усі параметри
    for m in media:
        m["engagement"] = m.get("like_count", 0) + m.get("comments_count", 0)
    media.sort(key=lambda x: x["engagement"], reverse=True)
    return media[:5]

def get_media_in_window(since_ts, until_ts, max_pages=6):
    """Усі дописи каналу у вікні [since, until] (unix-час) з пагінацією —
    для пошуку допису про конкретний матеріал (/stat). Instagram фільтрує
    /media за timestamp дописа; віддає найновіші перші, тому гортаємо
    paging.next. Поля — ті, що потрібні для зіставлення (caption) і виводу.
    Помилку ковтаємо (повертаємо що встигли), як у стрічці постів FB."""
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count",
        "since": since_ts,
        "until": until_ts,
        "limit": 100,
        "access_token": token,
    }
    out = []
    for _ in range(max_pages):
        try:
            data = requests.get(url, params=params, timeout=20).json()
        except Exception:
            break
        if "error" in data:
            break
        out.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, None  # next_url уже містить усі параметри
    return out


def get_media_item(media_id):
    """Одне медіа за id (лайки/коментарі/permalink/timestamp) — швидкий шлях
    /stat: коли media_id вже відомий з індексу (article_stats), минаємо
    листинг вікна і скоринг. Кидає RuntimeError, якщо медіа недоступне
    (видалене) — виклик фолбекне на збережений снімок."""
    base, token = credentials()
    url = f"{base}/{media_id}"
    params = {
        "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
        "access_token": token,
    }
    data = requests.get(url, params=params, timeout=15).json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message") or "media недоступне")
    return data


def get_media_insights(media_id, media_type=None):
    """Метрики конкретного допису: перегляди (views) + охоплення (reach).
    Назви метрик у Graph відрізняються за типом і версією API, тому пробуємо
    від найбагатшого набору до мінімального — беремо перше, що віддалось.
    Повертає dict name→value (може містити views, reach, likes, comments,
    shares, saved) або порожній dict, якщо інсайти недоступні."""
    def _fetch(metric):
        try:
            base, token = credentials()
            url = f"{base}/{media_id}/insights"
            params = {"metric": metric, "access_token": token}
            data = requests.get(url, params=params, timeout=15).json()
            if "error" in data:
                return None
            res = {}
            for item in data.get("data", []):
                vals = item.get("values")
                if vals:
                    res[item["name"]] = vals[0].get("value")
                else:
                    res[item["name"]] = item.get("total_value", {}).get("value")
            return res or None
        except Exception:
            return None

    for metric in (
        "views,reach,likes,comments,shares,saved",
        "reach,likes,comments,shares,saved",
        "views,reach",
        "reach",
    ):
        res = _fetch(metric)
        if res:
            return res
    return {}


def get_media_counts(since=None, until=None, max_pages=3):
    """Скільки опубліковано за вікно, по типах. З пагінацією — місяць не
    влазить в одну сторінку (limit=100) і лічильник брехав би в менший бік."""
    if since is None:
        since = int((datetime.now() - timedelta(days=7)).timestamp())
    base, token = credentials()
    url = f"{base}/{INSTAGRAM_USER_ID}/media"
    params = {
        "fields": "media_type",
        "since": since,
        "access_token": token,
        "limit": 100
    }
    if until:
        params["until"] = until
    counts = {"IMAGE": 0, "VIDEO": 0, "CAROUSEL_ALBUM": 0}
    for _ in range(max_pages):
        data = requests.get(url, params=params).json()
        if "error" in data:
            break
        for m in data.get("data", []):
            t = m.get("media_type", "")
            if t in counts:
                counts[t] += 1
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, {}
    return counts

def short_caption(caption, words=5):
    if not caption:
        return "без підпису"
    w = caption.split()
    if len(w) <= words:
        return caption
    return " ".join(w[:words]) + "..."

async def ig_token_handler(update, context):
    """/ig_token — ЯКІ двері в інсту відкриті просто зараз.

    Потрібна тому, що «зроби новий токен» — порада, яку неможливо виконати, не
    знаючи, ЯКОГО типу токен потрібен: Meta має два продукти на ті самі дані,
    і в Graph API Explorer вибір не тієї гілки дає «Invalid platform app»
    (Олег, 21.08). Команда нічого не змінює й не показує значень токенів —
    лише назви змінних і те, що відповіла Meta."""
    msg = await update.message.reply_text("⏳ Стукаю в усі двері інсти…")
    rows = await asyncio.to_thread(route_report)
    lines = ["<b>Доступ до Instagram</b>", ""]
    for r in rows:
        host = "graph.instagram.com" if "graph.instagram" in r["base"] else "graph.facebook.com"
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"{mark} {host} + {r['token']}")
        if not r["ok"]:
            lines.append(f"   <i>{(r['error'] or '')[:110]}</i>")
    lines.append("")
    if any(r["ok"] for r in rows):
        good = next(r for r in rows if r["ok"])
        lines.append(f"Читаю інсту через <b>{good['token']}</b> — усе працює, "
                     "міняти нічого не треба.")
    else:
        lines.append("Жодні двері не відкриті — цифр з інсти зараз немає ніде: "
                     "ні в /stat, ні в /instagram, ні в недільному звіті.")
        lines.append("")
        lines.append("Потрібен ОДИН токен сторінки Facebook, якому додано права "
                     "<code>instagram_basic</code> і "
                     "<code>instagram_manage_insights</code>. Інстаграмний логін "
                     "не потрібен — саме він дає «Invalid platform app».")
    await msg.edit_text("\n".join(lines), parse_mode="HTML",
                        disable_web_page_preview=True)


async def instagram_handler(update, context):
    try:
        args = context.args
        start_dt, end_dt, period_label = parse_month_arg(args)

        # Мережеві виклики Graph API — в окремому потоці, щоб не блокувати event loop
        profile = await asyncio.to_thread(get_instagram_profile)
        followers_now = profile.get("followers_count", 0)

        if start_dt:
            since_ts = int(start_dt.timestamp())
            until_ts = int(end_dt.timestamp())
            follows, unfollows = await asyncio.to_thread(get_follows_week, since_ts, until_ts)
            top_media = await asyncio.to_thread(get_top_media, since_ts)
            counts = await asyncio.to_thread(get_media_counts, since_ts)
            header = f"📱 Instagram МикВісті ({period_label}):\n\n"
        else:
            follows, unfollows = await asyncio.to_thread(get_follows_week)
            top_media = await asyncio.to_thread(get_top_media)
            counts = await asyncio.to_thread(get_media_counts)
            week_end = datetime.now().strftime("%d.%m.%Y")
            week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")
            header = f"📱 Instagram МикВісті ({week_start} — {week_end}):\n\n"
            header += f"👥 Підписники: {followers_now}"
            if follows is not None:
                net = follows - unfollows
                diff = f"+{net}" if net >= 0 else str(net)
                diff += f" (↑{follows} ↓{unfollows})"
                header += f" ({diff} за тиждень)"
            header += "\n\n"

        stats = await asyncio.to_thread(get_instagram_stats)
        photos = counts.get("IMAGE", 0)
        reels = counts.get("VIDEO", 0)
        carousels = counts.get("CAROUSEL_ALBUM", 0)
        total_posts = photos + reels + carousels

        top_text = ""
        for i, m in enumerate(top_media):
            media_type = {"IMAGE": "📸", "VIDEO": "🎬", "CAROUSEL_ALBUM": "🗂"}.get(m.get("media_type"), "📄")
            likes = m.get("like_count", 0)
            comments = m.get("comments_count", 0)
            link = m.get("permalink", "")
            title = short_caption(m.get("caption", ""))
            top_text += f'  {i+1}. {media_type} <a href="{link}">{title}</a>\n      ❤️{likes} 💬{comments}\n'

        text = (
            header +
            f"За період опубліковано {total_posts} матеріалів:\n"
            f"  📸 Постів: {photos + carousels}\n"
            f"  🎬 Рілзів: {reels}\n\n"
            f"📊 Статистика:\n"
            f"  👁 Охоплення: {stats.get('reach', 'н/д')}\n"
            f"  🤝 Взаємодії: {stats.get('total_interactions', 'н/д')}\n"
            f"  👤 Залучені акаунти: {stats.get('accounts_engaged', 'н/д')}\n\n"
            f"🔥 Топ-5 публікацій:\n{top_text}"
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        await update.message.reply_text("Помилка Instagram: " + str(e))

async def send_weekly_instagram_report(bot, chat_id):
    from handlers.ai_messages import generate_instagram_weekly_comment
    try:
        profile = await asyncio.to_thread(get_instagram_profile)
        stats = await asyncio.to_thread(get_instagram_stats)
        top_media = await asyncio.to_thread(get_top_media)
        counts = await asyncio.to_thread(get_media_counts)
        follows, unfollows = await asyncio.to_thread(get_follows_week)

        followers_now = profile.get("followers_count", 0)
        if follows is not None:
            net = follows - unfollows
            diff = f"+{net}" if net >= 0 else str(net)
            diff += f" (↑{follows} ↓{unfollows})"
        else:
            follows, unfollows, net, diff = 0, 0, 0, "н/д"

        week_end = datetime.now().strftime("%d.%m.%Y")
        week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

        photos = counts.get("IMAGE", 0)
        reels = counts.get("VIDEO", 0)
        carousels = counts.get("CAROUSEL_ALBUM", 0)
        total_posts = photos + reels + carousels

        # Знімок у пам'ять соцаналітики (Postgres) — дані вже зібрані, без
        # зайвого виклику Meta. Помилку ковтаємо, щоб не зламати сам звіт.
        try:
            from handlers import social_store
            await social_store.capture_instagram(profile, stats, follows, unfollows, total_posts, reels)
        except Exception as e:
            print(f"social_store: не вдалось зберегти IG-знімок — {e}")

        top_text = ""
        for i, m in enumerate(top_media):
            media_type = {"IMAGE": "📸", "VIDEO": "🎬", "CAROUSEL_ALBUM": "🗂"}.get(m.get("media_type"), "📄")
            likes = m.get("like_count", 0)
            comments = m.get("comments_count", 0)
            link = m.get("permalink", "")
            title = short_caption(m.get("caption", ""))
            top_text += f'  {i+1}. {media_type} <a href="{link}">{title}</a>\n      ❤️{likes} 💬{comments}\n'

        ai_comment = await generate_instagram_weekly_comment(stats, follows, unfollows, total_posts, reels)

        text = (
            ai_comment + "\n\n"
            f"📱 Instagram МикВісті ({week_start} — {week_end}):\n\n"
            f"👥 Підписники: {followers_now} ({diff} за тиждень)\n\n"
            f"За тиждень опубліковано {total_posts} матеріалів:\n"
            f"  📸 Постів: {photos + carousels}\n"
            f"  🎬 Рілзів: {reels}\n\n"
            f"📊 Статистика:\n"
            f"  👁 Охоплення: {stats.get('reach', 'н/д')}\n"
            f"  🤝 Взаємодії: {stats.get('total_interactions', 'н/д')}\n"
            f"  👤 Залучені акаунти: {stats.get('accounts_engaged', 'н/д')}\n\n"
            f"🔥 Топ-5 публікацій:\n{top_text}"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        print("Помилка тижневого Instagram звіту: " + str(e))
