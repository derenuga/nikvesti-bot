"""
Команда /stat <url> — збирає статистику матеріалу nikvesti.com:
- Facebook: перегляди (post_media_view), реакції, коментарі, шери
- GA4: перегляди по мовних версіях (ua/ru/en) за ID матеріалу
- Telegram: перегляди поста в каналі @nikvesti (індекс + пошук по t.me/s,
  деталі — handlers/telegram_stats.py)

Використання: /stat https://nikvesti.com/news/...
"""

import asyncio
import os
import re
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import unquote
from datetime import datetime, timedelta
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension, FilterExpression, Filter
from google.oauth2 import service_account

from handlers.telegram_stats import get_tg_stat, SEARCH_MAX_PAGES
from handlers.facebook import get_reel_insights, fix_permalink
from handlers import stat_instagram
from handlers import stat_tiktok
from handlers import stat_youtube

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_CREDENTIALS = os.environ.get("GA4_CREDENTIALS")

LANG_FLAGS = {
    "ua": "🇺🇦",
    "ru": "🇷🇺",
    "en": "🇬🇧",
}


# ---------- Утиліти ----------

def _clean_url(url):
    return url.split("?")[0].split("#")[0].rstrip("/")

def _extract_article_id(url):
    """Витягуємо числовий ID з URL: /news/justice/320102-... → '320102'"""
    match = re.search(r'/(\d{4,})-', url)
    return match.group(1) if match else None


FB_SEARCH_FORWARD_DAYS = 10  # від дати публікації дивимось уперед — статтю постять у ці дні


def _parse_iso_date(value):
    """'2026-05-14T09:30:00+03:00' / '2026-05-14' → datetime (або None)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    for parse in (
        lambda x: datetime.fromisoformat(x),
        lambda x: datetime.strptime(x[:10], "%Y-%m-%d"),
    ):
        try:
            return parse(v)
        except (ValueError, TypeError):
            continue
    return None


def _find_date_published(node):
    """Рекурсивно шукає datePublished у JSON-LD (може бути об'єкт, список, @graph)."""
    if isinstance(node, dict):
        if node.get("datePublished"):
            dt = _parse_iso_date(node["datePublished"])
            if dt:
                return dt
        for value in node.values():
            found = _find_date_published(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_date_published(item)
            if found:
                return found
    return None


def get_article_published_date(article_url):
    """Дата публікації матеріалу з JSON-LD (datePublished) або
    <meta article:published_time>. None якщо не вдалося — тоді пошук
    у FB відкотиться на дефолтне вікно 14 днів назад."""
    try:
        resp = requests.get(
            article_url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NikVesti-Bot/1.0)"},
        )
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            dt = _find_date_published(data)
            if dt:
                return dt

        meta = soup.find("meta", property="article:published_time")
        if meta and meta.get("content"):
            return _parse_iso_date(meta["content"])
        return None
    except Exception as e:
        print(f"stat: не вдалося визначити дату публікації — {e}")
        return None


def _fetch_article_context(article_url):
    """Один HTTP-запит сторінки статті → {'pub_date', 'signature'}. Раніше
    сторінку тягли до 4 разів (дата публікації + сигнатура окремо в кожному з
    IG/TikTok/YouTube) — тепер один раз, а /stat роздає результат усім. pub_date
    — з JSON-LD datePublished / <meta article:published_time>; signature =
    {'title' (og:title), 'lead' (<meta description>)} для семантичного пошуку по
    соцмережах."""
    ctx = {"pub_date": None, "signature": None}
    try:
        resp = requests.get(
            article_url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NikVesti-Bot/1.0)"},
        )
        if resp.status_code != 200:
            return ctx
        soup = BeautifulSoup(resp.text, "html.parser")

        # Дата публікації (JSON-LD → meta)
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            dt = _find_date_published(data)
            if dt:
                ctx["pub_date"] = dt
                break
        if ctx["pub_date"] is None:
            meta = soup.find("meta", property="article:published_time")
            if meta and meta.get("content"):
                ctx["pub_date"] = _parse_iso_date(meta["content"])

        # Сигнатура (заголовок + лід) для семантичного пошуку
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()
        elif soup.find("h1"):
            title = soup.find("h1").get_text(strip=True)
        lead = ""
        desc = soup.find("meta", attrs={"name": "description"}) or \
            soup.find("meta", property="og:description")
        if desc and desc.get("content"):
            lead = desc["content"].strip()
        if title or lead:
            ctx["signature"] = {"title": title, "lead": lead}
    except Exception as e:
        print(f"stat: не вдалося зчитати сторінку статті — {e}")
    return ctx


# ---------- Facebook ----------

def _get_fb_posts(since_ts, until_ts, max_pages=15):
    """Усі пости у вікні [since, until] з пагінацією. FB віддає по 100 і
    найновіші перші — без гортання старий край вікна (де і лежить пост
    про давню статтю) не потрапляє у вибірку."""
    url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/posts"
    params = {
        "fields": "id,message,story,permalink_url,created_time,reactions.summary(true),comments.summary(true),shares,attachments{media_type,target,url,unshimmed_url}",
        "since": since_ts,
        "until": until_ts,
        "limit": 100,
        "access_token": FACEBOOK_PAGE_TOKEN,
    }
    all_posts = []
    for _ in range(max_pages):
        # Один ретрай на таймаут: сторінка з summary-полями інколи відповідає
        # довше 30с, повторна спроба зазвичай проходить
        data = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, timeout=30)
                data = resp.json()
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    continue
                raise
        if "error" in data:
            if all_posts:
                break  # частину вже маємо — віддаємо, що встигли
            raise Exception(data["error"]["message"])
        all_posts.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, None  # next_url уже містить усі параметри
    return all_posts

def _get_post_views(post_id):
    url = f"https://graph.facebook.com/v25.0/{post_id}/insights"
    params = {"metric": "post_media_view", "access_token": FACEBOOK_PAGE_TOKEN}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "error" in data:
        return None
    try:
        return data["data"][0]["values"][0]["value"]
    except Exception:
        return None

def _fb_date(created_time):
    try:
        dt = datetime.strptime(created_time or "", "%Y-%m-%dT%H:%M:%S+0000")
        return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return created_time or ""


def _matches_article(text, clean_url, article_id):
    """Точний матч: повний URL або /ID- (щоб 320362 не ловив 1320362)."""
    if not text:
        return False
    return clean_url in text or f"/{article_id}-" in text


def _collect_attachment_urls(post):
    """Усі URL з вкладень поста (url, unshimmed_url, target.url) — рекурсивно.
    Посилання на статтю у ФБ часто не в тексті підпису, а у прикріпленому лінку."""
    urls = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("url", "unshimmed_url") and isinstance(value, str):
                    urls.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(post.get("attachments", {}))
    return urls


def _post_matches_article(post, clean_url, article_id):
    """Пост стосується статті, якщо посилання є в тексті/story АБО у вкладенні
    (fb-лінки бувають shimmed — l.facebook.com/l.php?u=... — тому декодуємо)."""
    if _matches_article(post.get("message", "") or "", clean_url, article_id):
        return True
    if _matches_article(post.get("story", "") or "", clean_url, article_id):
        return True
    for u in _collect_attachment_urls(post):
        if _matches_article(unquote(u), clean_url, article_id):
            return True
    return False


def _get_fb_reels(limit=50):
    url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/video_reels"
    params = {
        "fields": "id,description,permalink_url,created_time",
        "limit": limit,
        "access_token": FACEBOOK_PAGE_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "error" in data:
        return []
    return data.get("data", [])


def _get_reel_views(reel_id):
    """Перегляди рілза: назва метрики різна залежно від типу відео,
    пробуємо по черзі. None якщо жодна не спрацювала."""
    for metric in ("blue_reels_play_count", "post_video_views"):
        try:
            url = f"https://graph.facebook.com/v25.0/{reel_id}/video_insights"
            params = {"metric": metric, "access_token": FACEBOOK_PAGE_TOKEN}
            data = requests.get(url, params=params, timeout=15).json()
            if "error" in data:
                continue
            return data["data"][0]["values"][0]["value"]
        except Exception:
            continue
    return None


def _digits_in(text, minlen=6):
    """Множина довгих числових ID у тексті/URL (для зіставлення пост↔рілз)."""
    return set(re.findall(rf"\d{{{minlen},}}", text or ""))


def _reel_keys(reel):
    keys = {str(reel.get("id", ""))} | _digits_in(reel.get("permalink_url", ""))
    keys.discard("")
    return keys


def _post_keys(post):
    """ID, за якими пост можна впізнати як рілз-дубль: id вкладення + числа з permalink."""
    keys = set()
    attachments = (post.get("attachments", {}) or {}).get("data", [])
    if attachments:
        target_id = (attachments[0].get("target", {}) or {}).get("id")
        if target_id:
            keys.add(str(target_id))
    keys |= _digits_in(post.get("permalink_url", ""))
    return keys


def get_fb_stats(article_url, article_id, pub_date=None):
    """Шукає ВСІ публікації про матеріал: звичайні пости (вікно від дати
    публікації) і рілзи з посиланням в описі (останні ~50).

    Рілз FB дублює ще й у стрічці постів (як відео-пост із тим самим
    контентом, але іншим лічильником переглядів). Такий дубль прибираємо —
    лишаємо рілз, бо там коректний реловий лічильник. Повертає кортеж
    (список публікацій; кількість переглянутих постів у вікні; текст помилки
    стрічки постів або None). pub_date — дата публікації статті
    (якщо None, визначається тут)."""
    clean = _clean_url(article_url)
    posts_out = []
    reels_out = []
    reel_key_union = set()

    # Рілзи спершу — щоб потім впізнати і прибрати їх дублі зі стрічки постів
    try:
        for reel in _get_fb_reels():
            if not _matches_article(reel.get("description") or "", clean, article_id):
                continue
            reactions, comments, shares = get_reel_insights(reel["id"])
            reels_out.append({
                "type": "reel",
                "permalink": fix_permalink(reel.get("permalink_url", "")),
                "date": _fb_date(reel.get("created_time")),
                "views": _get_reel_views(reel["id"]),
                "reactions": reactions,
                "comments": comments,
                "shares": shares,
            })
            reel_key_union |= _reel_keys(reel)
    except Exception as e:
        print(f"stat: помилка пошуку рілзів — {e}")

    # Вікно пошуку постів: якщо знаємо дату публікації — дивимось від неї
    # вперед FB_SEARCH_FORWARD_DAYS днів (статтю постять у цей проміжок).
    # Так знаходимо і старі матеріали, а не тільки за 14 днів. Без дати —
    # дефолтне вікно 14 днів назад.
    if pub_date is None:
        pub_date = get_article_published_date(article_url)
    now = datetime.now()
    if pub_date:
        since_dt = pub_date.replace(tzinfo=None) - timedelta(days=1)
        until_dt = min(pub_date.replace(tzinfo=None) + timedelta(days=FB_SEARCH_FORWARD_DAYS), now)
    else:
        until_dt = now
        since_dt = until_dt - timedelta(days=14)

    # Помилку стрічки постів (напр. ліміт Graph API) ловимо тут — щоб не
    # занулити всю секцію: рілзи вже зібрані, і повертаємо ознаку помилки
    error = None
    try:
        posts = _get_fb_posts(int(since_dt.timestamp()), int(until_dt.timestamp()))
    except Exception as e:
        posts = []
        error = str(e)

    for post in posts:
        if not _post_matches_article(post, clean, article_id):
            continue
        # той самий рілз, уже врахований вище як рілз — не дублюємо постом
        if reel_key_union and (_post_keys(post) & reel_key_union):
            continue
        post_id_short = post["id"].split("_")[1]
        posts_out.append({
            "type": "post",
            "permalink": f"https://www.facebook.com/nikvesti/posts/{post_id_short}",
            "date": _fb_date(post.get("created_time")),
            "views": _get_post_views(post["id"]),
            "reactions": post.get("reactions", {}).get("summary", {}).get("total_count", 0),
            "comments": post.get("comments", {}).get("summary", {}).get("total_count", 0),
            "shares": post.get("shares", {}).get("count", 0),
        })

    return posts_out + reels_out, len(posts), error


# ---------- GA4 ----------

def get_ga4_client():
    creds_dict = json.loads(GA4_CREDENTIALS)
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=credentials)

def get_ga4_stat(article_id):
    """
    Запитує GA4 по всіх сторінках де pagePath містить article_id.
    Групує по мові: ua (/news/...), ru (/ru/news/...), en (/en/news/...).
    Повертає dict {"ua": N, "ru": N, "en": N} — тільки ті що є.
    """
    client = get_ga4_client()

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date="2020-01-01", end_date="today")],
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value=article_id,
                )
            )
        ),
        limit=50,
    )

    response = client.run_report(request)

    by_lang = {}
    for row in response.rows:
        path = row.dimension_values[0].value
        views = int(row.metric_values[0].value)

        if path.startswith("/ru/"):
            lang = "ru"
        elif path.startswith("/en/"):
            lang = "en"
        else:
            lang = "ua"

        by_lang[lang] = by_lang.get(lang, 0) + views

    return by_lang


# ---------- Форматування ----------

def _short_fb_error(error):
    """Людський опис помилки Graph API для повідомлення."""
    low = error.lower()
    if "timed out" in low or "timeout" in low:
        return "Facebook відповідав надто довго (таймаут)"
    if "request limit" in low or "rate limit" in low or "#4" in error or "#17" in error:
        return "Facebook тимчасово обмежив запити (ліміт)"
    return f"помилка Facebook API: {error[:150]}"


def format_stat_message(article_url, fb_stats, ga4_stat, tg_stat, pub_date=None,
                        posts_scanned=None, fb_error=None, ig_stats=None, tt_stats=None,
                        yt_stats=None):
    clean = _clean_url(article_url)
    lines = [
        f"📊 <b>Статистика матеріалу</b>",
        f'<a href="{clean}">{clean}</a>',
        "",
    ]

    # ---- Сайт (GA4) — перше, це першоджерело переглядів ----
    lines.append("🌐 <b>Сайт</b>")
    site_total = None
    if not ga4_stat:
        lines.append("Даних не знайдено")
    else:
        site_total = sum(ga4_stat.values())
        for lang in ["ua", "ru", "en"]:
            if lang in ga4_stat:
                flag = LANG_FLAGS[lang]
                lines.append(f'{flag} {ga4_stat[lang]:,}'.replace(",", " "))
        lines.append("──────")
        lines.append(f'Всього: {site_total:,}'.replace(",", " "))

    # ---- Facebook ----
    lines.append("")
    lines.append("📘 <b>Facebook</b>")
    fb_views_total = None

    if not fb_stats and fb_error:
        # Помилка API — це НЕ "поста немає". Кажемо чесно й радимо повторити.
        lines.append(f"⚠️ {_short_fb_error(fb_error)}")
        lines.append("Спробуйте /stat ще раз за хвилину — це тимчасово.")
    elif not fb_stats:
        # Діагностика: чи визначилась дата + скільки постів переглянуто у вікні.
        # Багато постів і нема збігу → проблема матчингу; мало → вікно/пагінація.
        if pub_date:
            window = f"шукав біля {pub_date.strftime('%d.%m.%Y')}"
        else:
            window = "дату публікації не визначив → шукав за 14 днів"
        if posts_scanned is not None:
            window += f", переглянув {posts_scanned} постів у вікні + рілзи"
        lines.append(f"Публікацій не знайдено ({window})")
    else:
        several = len(fb_stats) > 1
        for i, item in enumerate(fb_stats):
            if i > 0:
                lines.append("")
            label = "Пост" if item["type"] == "post" else "🎬 Рілз"
            num = f"{i + 1}. " if several else ""
            lines.append(f'{num}<a href="{item["permalink"]}">{label} від {item["date"]}</a>')
            if item["views"] is not None:
                lines.append(f'👁 Перегляди: {item["views"]:,}'.replace(",", " "))
            lines.append(f'❤️ Реакції: {item["reactions"]}')
            lines.append(f'💬 Коментарі: {item["comments"]}')
            lines.append(f'🔄 Шери: {item["shares"]}')
        views_known = [it["views"] for it in fb_stats if it["views"] is not None]
        if views_known:
            fb_views_total = sum(views_known)
            if several:
                lines.append("")
                lines.append(f'Разом переглядів: {fb_views_total:,}'.replace(",", " "))

    # ---- Instagram ----
    lines.append("")
    lines.append("📷 <b>Instagram</b>")
    ig_views_total = None
    if not ig_stats:
        lines.append("Допис не знайдено")
    else:
        several_ig = len(ig_stats) > 1
        for i, item in enumerate(ig_stats):
            if i > 0:
                lines.append("")
            label = "🎬 Рілз" if item.get("media_type") == "VIDEO" else "Допис"
            num = f"{i + 1}. " if several_ig else ""
            lines.append(f'{num}<a href="{item["permalink"]}">{label} від {item["date"]}</a>')
            if item.get("views") is not None:
                lines.append(f'👁 Перегляди: {item["views"]:,}'.replace(",", " "))
            if item.get("reach") is not None:
                lines.append(f'👀 Охоплення: {item["reach"]:,}'.replace(",", " "))
            lines.append(f'❤️ Лайки: {item.get("likes", 0)}')
            lines.append(f'💬 Коментарі: {item.get("comments", 0)}')
            if item.get("shares") is not None:
                lines.append(f'✈️ Поширення: {item["shares"]:,}'.replace(",", " "))
            if item.get("saved") is not None:
                lines.append(f'🔖 Збереження: {item["saved"]:,}'.replace(",", " "))
        ig_views_known = [it["views"] for it in ig_stats if it.get("views") is not None]
        if ig_views_known:
            ig_views_total = sum(ig_views_known)
            if several_ig:
                lines.append("")
                lines.append(f'Разом переглядів: {ig_views_total:,}'.replace(",", " "))

    # ---- TikTok ---- (тільки якщо OAuth налаштовано; інакше tt_stats=None)
    tt_views_total = None
    if tt_stats is not None:
        lines.append("")
        lines.append("🎵 <b>TikTok</b>")
        if not tt_stats:
            lines.append("Відео не знайдено")
        else:
            several_tt = len(tt_stats) > 1
            for i, item in enumerate(tt_stats):
                if i > 0:
                    lines.append("")
                num = f"{i + 1}. " if several_tt else ""
                lines.append(f'{num}<a href="{item["permalink"]}">Відео від {item["date"]}</a>')
                if item.get("views") is not None:
                    lines.append(f'👁 Перегляди: {item["views"]:,}'.replace(",", " "))
                if item.get("likes") is not None:
                    lines.append(f'❤️ Лайки: {item["likes"]}')
                if item.get("comments") is not None:
                    lines.append(f'💬 Коментарі: {item["comments"]}')
                if item.get("shares") is not None:
                    lines.append(f'✈️ Поширення: {item["shares"]}')
            tt_views_known = [it["views"] for it in tt_stats if it.get("views") is not None]
            if tt_views_known:
                tt_views_total = sum(tt_views_known)
                if several_tt:
                    lines.append("")
                    lines.append(f'Разом переглядів: {tt_views_total:,}'.replace(",", " "))

    # ---- YouTube ---- (тільки якщо OAuth налаштовано; інакше yt_stats=None)
    yt_views_total = None
    if yt_stats is not None:
        lines.append("")
        lines.append("▶️ <b>YouTube</b>")
        if not yt_stats:
            lines.append("Відео не знайдено")
        else:
            several_yt = len(yt_stats) > 1
            for i, item in enumerate(yt_stats):
                if i > 0:
                    lines.append("")
                num = f"{i + 1}. " if several_yt else ""
                lines.append(f'{num}<a href="{item["permalink"]}">Відео від {item["date"]}</a>')
                if item.get("views") is not None:
                    lines.append(f'👁 Перегляди: {item["views"]:,}'.replace(",", " "))
                if item.get("likes") is not None:
                    lines.append(f'❤️ Лайки: {item["likes"]}')
                if item.get("comments") is not None:
                    lines.append(f'💬 Коментарі: {item["comments"]}')
            yt_views_known = [it["views"] for it in yt_stats if it.get("views") is not None]
            if yt_views_known:
                yt_views_total = sum(yt_views_known)
                if several_yt:
                    lines.append("")
                    lines.append(f'Разом переглядів: {yt_views_total:,}'.replace(",", " "))

    # ---- Telegram ----
    lines.append("")
    lines.append("📣 <b>Telegram</b>")
    tg_views = None

    if tg_stat is None:
        lines.append(f"Пост не знайдено (індекс + останні ~{SEARCH_MAX_PAGES * 20} постів каналу)")
    else:
        lines.append(f'<a href="{tg_stat["url"]}">{tg_stat["url"].replace("https://", "")}</a>')
        if tg_stat["views"] is not None:
            tg_views = tg_stat["views"]
            lines.append(f'👁 Перегляди: {tg_views:,}'.replace(",", " "))
        else:
            lines.append("👁 Перегляди: не вдалося зчитати")

    # ---- Сукупно по всіх каналах ----
    # Сумуємо перегляди звідусіль, де є дані: сайт + Facebook + Telegram
    channel_totals = [v for v in (site_total, fb_views_total, ig_views_total, tt_views_total, yt_views_total, tg_views) if v is not None]
    if channel_totals:
        grand_total = sum(channel_totals)
        lines.append("")
        lines.append("═══════")
        lines.append(
            f'🧮 <b>Сукупно по всіх каналах: {grand_total:,}</b>'.replace(",", " ")
        )

    return "\n".join(lines)

# ---------- Handler ----------

async def stat_handler(update, context):
    if not context.args:
        await update.message.reply_text(
            "Використання: /stat https://nikvesti.com/news/...",
            parse_mode="HTML"
        )
        return

    article_url = context.args[0]
    if "nikvesti.com" not in article_url:
        await update.message.reply_text("Вкажіть посилання на матеріал nikvesti.com")
        return

    article_id = _extract_article_id(article_url)
    if not article_id:
        await update.message.reply_text("Не вдалося визначити ID матеріалу з URL")
        return

    msg = await update.message.reply_text("⏳ Збираю статистику...")

    # Сторінку статті тягнемо ОДИН раз — звідси і дата публікації (вікно пошуку
    # FB/соцмереж + діагностика), і сигнатура (заголовок+лід) для семантичного
    # пошуку в IG/TikTok/YouTube. Раніше сторінка фетчилась до 4 разів.
    try:
        ctx = await asyncio.to_thread(_fetch_article_context, article_url)
    except Exception as e:
        ctx = {"pub_date": None, "signature": None}
        print(f"stat: помилка читання сторінки — {e}")
    pub_date = ctx.get("pub_date")
    sig = ctx.get("signature")

    # Усі канали — паралельно (кожен незалежний): сумарна затримка стає
    # максимальною, а не сумою. return_exceptions=True — збій одного каналу не
    # валить решту; fallback на кожен нижче.
    fb_res, ga4_res, tg_res, ig_res, tt_res, yt_res = await asyncio.gather(
        asyncio.to_thread(get_fb_stats, article_url, article_id, pub_date),
        asyncio.to_thread(get_ga4_stat, article_id),
        asyncio.to_thread(get_tg_stat, article_id, pub_date),
        stat_instagram.get_instagram_stat(article_url, pub_date, sig),
        stat_tiktok.get_tiktok_stat(article_url, pub_date, sig),
        stat_youtube.get_youtube_stat(article_url, pub_date, sig),
        return_exceptions=True,
    )

    if isinstance(fb_res, Exception):
        fb_stats, fb_scanned, fb_error = [], None, str(fb_res)
        print(f"stat: помилка Facebook — {fb_res}")
    else:
        fb_stats, fb_scanned, fb_error = fb_res
        if fb_error:
            print(f"stat: Facebook стрічка постів — {fb_error}")

    if isinstance(ga4_res, Exception):
        ga4_stat = {}
        print(f"stat: помилка GA4 — {ga4_res}")
    else:
        ga4_stat = ga4_res

    if isinstance(tg_res, Exception):
        tg_stat = None
        print(f"stat: помилка Telegram — {tg_res}")
    else:
        tg_stat = tg_res

    if isinstance(ig_res, Exception):
        ig_stats = []
        print(f"stat: помилка Instagram — {ig_res}")
    else:
        ig_stats = ig_res

    # TikTok/YouTube: None = OAuth не налаштовано → блок ховаємо; тому на збої
    # теж None (не []), щоб не показувати «не знайдено» на технічній помилці
    if isinstance(tt_res, Exception):
        tt_stats = None
        print(f"stat: помилка TikTok — {tt_res}")
    else:
        tt_stats = tt_res

    if isinstance(yt_res, Exception):
        yt_stats = None
        print(f"stat: помилка YouTube — {yt_res}")
    else:
        yt_stats = yt_res

    text = format_stat_message(article_url, fb_stats, ga4_stat, tg_stat, pub_date,
                               fb_scanned, fb_error, ig_stats, tt_stats, yt_stats)
    await msg.edit_text(text, parse_mode="HTML")
