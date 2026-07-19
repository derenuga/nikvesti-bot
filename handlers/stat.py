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
from handlers import stat_store

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
    """Витягуємо числовий ID з URL. Два формати:
    - зі слагом: /news/justice/320102-slug → '320102' (ID перед дефісом);
    - без слага (старі матеріали, зокрема /ru/…): /news/politics/111079 →
      '111079' (ID — останній сегмент шляху)."""
    clean = _clean_url(url)
    match = re.search(r'/(\d{4,})-', clean)
    if match:
        return match.group(1)
    match = re.search(r'/(\d{4,})$', clean)
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


def _reel_ts(reel):
    """created_time рілза → unix (0, якщо не розпарсити)."""
    try:
        return int(datetime.strptime(reel.get("created_time", ""),
                                     "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except Exception:
        return 0


def _get_fb_reels(since_ts=None, until_ts=None, max_pages=15):
    """Рілзи сторінки. /video_reels не має фільтра since/until, віддає від
    найновіших — тому гортаємо paging.next з ранньою зупинкою, щойно дійшли до
    рілзів старших за since (як стрічка постів). Так рілз знаходиться на
    будь-якій глибині, а не лише в останніх ~50 (старий відомий gap). Без
    since — одна сторінка найновіших (фолбек, коли дату статті не визначили)."""
    url = f"https://graph.facebook.com/v25.0/{FACEBOOK_PAGE_ID}/video_reels"
    params = {
        "fields": "id,description,permalink_url,created_time",
        "limit": 100,
        "access_token": FACEBOOK_PAGE_TOKEN,
    }
    out = []
    pages = max_pages if since_ts else 1
    for _ in range(pages):
        data = requests.get(url, params=params, timeout=15).json()
        if "error" in data:
            break
        reached_older = False
        for reel in data.get("data", []):
            ts = _reel_ts(reel)
            if until_ts and ts and ts >= until_ts:
                continue                      # новіше за вікно
            if since_ts and ts and ts < since_ts:
                reached_older = True          # найновіші → далі лише старіші
                break
            out.append(reel)
        next_url = data.get("paging", {}).get("next")
        if reached_older or not next_url:
            break
        url, params = next_url, None  # next_url уже містить усі параметри
    return out


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
    """Шукає ВСІ публікації про матеріал: звичайні пости і рілзи з посиланням
    в описі — обидва в СПІЛЬНОМУ вікні від дати публікації (рілзи гортаються
    пагінацією до старого краю вікна, тож знаходяться на будь-якій глибині).

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

    # Вікно пошуку — СПІЛЬНЕ для рілзів і стрічки постів: від дати публікації
    # вперед FB_SEARCH_FORWARD_DAYS (статтю постять у цей проміжок). Без дати —
    # дефолт 14 днів назад.
    if pub_date is None:
        pub_date = get_article_published_date(article_url)
    now = datetime.now()
    if pub_date:
        since_dt = pub_date.replace(tzinfo=None) - timedelta(days=1)
        until_dt = min(pub_date.replace(tzinfo=None) + timedelta(days=FB_SEARCH_FORWARD_DAYS), now)
    else:
        until_dt = now
        since_dt = until_dt - timedelta(days=14)
    since_ts, until_ts = int(since_dt.timestamp()), int(until_dt.timestamp())

    # Рілзи спершу — щоб потім впізнати і прибрати їх дублі зі стрічки постів.
    # З вікном і пагінацією рілз знаходиться на будь-якій глибині (без дати —
    # лише найновіша сторінка, як раніше)
    try:
        reels = _get_fb_reels(since_ts if pub_date else None,
                              until_ts if pub_date else None)
        for reel in reels:
            if not _matches_article(reel.get("description") or "", clean, article_id):
                continue
            reactions, comments, shares = get_reel_insights(reel["id"])
            reels_out.append({
                "type": "reel",
                "id": str(reel["id"]),  # ключ швидкого шляху /stat (article_stats)
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

    # Стрічка постів — те саме вікно (пораховане вище, спільне з рілзами).
    # Помилку стрічки ловимо окремо — щоб не занулити всю секцію: рілзи вже
    # зібрані, і повертаємо ознаку помилки
    error = None
    try:
        posts = _get_fb_posts(since_ts, until_ts)
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
            "id": str(post["id"]),  # ключ швидкого шляху /stat (article_stats)
            "permalink": f"https://www.facebook.com/nikvesti/posts/{post_id_short}",
            "date": _fb_date(post.get("created_time")),
            "views": _get_post_views(post["id"]),
            "reactions": post.get("reactions", {}).get("summary", {}).get("total_count", 0),
            "comments": post.get("comments", {}).get("summary", {}).get("total_count", 0),
            "shares": post.get("shares", {}).get("count", 0),
        })

    return posts_out + reels_out, len(posts), error


def get_fb_stats_by_objects(stored_items):
    """Швидкий шлях /stat: post/reel id відомі з індексу (article_stats) —
    минаємо вікно, пагінацію і матчинг, одразу тягнемо свіжі метрики об'єктів.
    permalink/дата/тип — зі снімка (не змінюються). Повертає той самий кортеж,
    що get_fb_stats. Кидає виняток при помилці Graph API — виклик фолбекне
    на снімок з Нори."""
    out = []
    for it in stored_items:
        oid = it.get("id")
        if not oid:
            continue
        if it.get("type") == "reel":
            reactions, comments, shares = get_reel_insights(oid)
            out.append({**it, "views": _get_reel_views(oid), "reactions": reactions,
                        "comments": comments, "shares": shares, "method": "index"})
        else:
            data = requests.get(
                f"https://graph.facebook.com/v25.0/{oid}",
                params={"fields": "reactions.summary(true),comments.summary(true),shares",
                        "access_token": FACEBOOK_PAGE_TOKEN},
                timeout=15,
            ).json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message") or "пост недоступний")
            out.append({
                **it, "views": _get_post_views(oid),
                "reactions": data.get("reactions", {}).get("summary", {}).get("total_count", 0),
                "comments": data.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": data.get("shares", {}).get("count", 0),
                "method": "index",
            })
    if not out:
        raise RuntimeError("жоден збережений об'єкт не прочитався")
    return out, None, None


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


def _nora_note(items):
    """Рядок-помітка фолбека: items узяті зі снімка Нори (живе джерело впало)."""
    if items and isinstance(items[0], dict) and items[0].get("nora"):
        return f'🗄 <i>живе джерело не відповіло — знімок з Нори від {items[0]["nora"]}</i>'
    return None


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
        note = _nora_note(fb_stats)
        if note:
            lines.append(note)
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
        note = _nora_note(ig_stats)
        if note:
            lines.append(note)
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
            note = _nora_note(tt_stats)
            if note:
                lines.append(note)
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
            note = _nora_note(yt_stats)
            if note:
                lines.append(note)
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

    # Індекс попереднього /stat з Нори (article_stats): object_id знайдених
    # постів/відео. Канали, що вже в індексі, минають пошук (вікна, листинги,
    # скоринг, суддю) і тягнуть метрики одразу — повторні /stat у рази швидші.
    index = await asyncio.to_thread(stat_store.load_index, article_id)
    fb_idx = (index.get("facebook") or {}).get("items")
    ig_idx = (index.get("instagram") or {}).get("items")
    tt_idx = (index.get("tiktok") or {}).get("items")
    yt_idx = (index.get("youtube") or {}).get("items")

    # Сторінку статті тягнемо ОДИН раз — дата публікації (вікна пошуку) +
    # сигнатура (семантика IG/TikTok/YouTube). Коли ВСІ соцканали в індексі —
    # пошуку не буде, сторінка не потрібна взагалі.
    pub_date, sig = None, None
    if not (fb_idx and ig_idx and tt_idx and yt_idx):
        try:
            ctx = await asyncio.to_thread(_fetch_article_context, article_url)
            pub_date = ctx.get("pub_date")
            sig = ctx.get("signature")
        except Exception as e:
            print(f"stat: помилка читання сторінки — {e}")

    # Живий прогрес: канали завершуються в різний час — по кожному оновлюємо
    # статус-рядок у повідомленні (✅/⏳), щоб не висіло німе «Збираю…».
    # Тротлінг 1.2с — щоб не впертись у ліміт Telegram на edit_text; помилки
    # редагування ковтаємо (флуд-контроль не має ламати збір). Останній канал
    # не редагує — одразу прийде фінальний текст зі статистикою.
    _order = ["ga4", "fb", "ig", "tt", "yt", "tg"]
    _labels = {"ga4": "сайт", "fb": "ФБ", "ig": "інста", "tt": "тікток",
               "yt": "ютуб", "tg": "ТГ"}
    _done = set()
    _last_edit = [0.0]

    async def _track(key, awaitable):
        try:
            return await awaitable
        finally:
            _done.add(key)
            now = asyncio.get_running_loop().time()
            if len(_done) < len(_order) and now - _last_edit[0] >= 1.2:
                _last_edit[0] = now
                ticks = "  ".join(
                    ("✅ " if k in _done else "⏳ ") + _labels[k] for k in _order
                )
                try:
                    await msg.edit_text(f"Збираю статистику…\n{ticks}")
                except Exception:
                    pass

    # Усі канали — паралельно; кожен соцканал — швидким шляхом (по індексу)
    # або повним пошуком. return_exceptions=True — збій одного не валить решту.
    fb_res, ga4_res, tg_res, ig_res, tt_res, yt_res = await asyncio.gather(
        _track("fb", asyncio.to_thread(get_fb_stats_by_objects, fb_idx) if fb_idx
               else asyncio.to_thread(get_fb_stats, article_url, article_id, pub_date)),
        _track("ga4", asyncio.to_thread(get_ga4_stat, article_id)),
        _track("tg", asyncio.to_thread(get_tg_stat, article_id, pub_date)),
        _track("ig", stat_instagram.get_instagram_stat_by_ids(ig_idx) if ig_idx
               else stat_instagram.get_instagram_stat(article_url, pub_date, sig)),
        _track("tt", stat_tiktok.get_tiktok_stat_by_ids(tt_idx) if tt_idx
               else stat_tiktok.get_tiktok_stat(article_url, pub_date, sig)),
        _track("yt", stat_youtube.get_youtube_stat_by_ids(yt_idx) if yt_idx
               else stat_youtube.get_youtube_stat(article_url, pub_date, sig)),
        return_exceptions=True,
    )

    # Збій каналу + є снімок у Норі → показуємо снімок з поміткою дати
    # (краще вчорашня цифра з позначкою, ніж «не знайдено» через ліміт API)
    if isinstance(fb_res, Exception):
        print(f"stat: помилка Facebook — {fb_res}")
        if index.get("facebook"):
            fb_stats, fb_scanned, fb_error = stat_store.mark_nora(index["facebook"]), None, None
        else:
            fb_stats, fb_scanned, fb_error = [], None, str(fb_res)
    else:
        fb_stats, fb_scanned, fb_error = fb_res
        if fb_error:
            print(f"stat: Facebook стрічка постів — {fb_error}")
            if not fb_stats and index.get("facebook"):
                fb_stats, fb_error = stat_store.mark_nora(index["facebook"]), None

    if isinstance(ga4_res, Exception):
        print(f"stat: помилка GA4 — {ga4_res}")
        ga4_stat = {}
        if index.get("site"):
            site_items = index["site"]["items"]
            ga4_stat = site_items[0].get("by_lang", {}) if site_items else {}
    else:
        ga4_stat = ga4_res

    if isinstance(tg_res, Exception):
        tg_stat = None
        print(f"stat: помилка Telegram — {tg_res}")
    else:
        tg_stat = tg_res

    if isinstance(ig_res, Exception):
        print(f"stat: помилка Instagram — {ig_res}")
        ig_stats = stat_store.mark_nora(index["instagram"]) if index.get("instagram") else []
    else:
        ig_stats = ig_res

    # TikTok/YouTube: None = OAuth не налаштовано → блок ховаємо; тому на збої
    # без снімка теж None (не []), щоб не показувати «не знайдено» на помилці
    if isinstance(tt_res, Exception):
        print(f"stat: помилка TikTok — {tt_res}")
        tt_stats = stat_store.mark_nora(index["tiktok"]) if index.get("tiktok") else None
    else:
        tt_stats = tt_res

    if isinstance(yt_res, Exception):
        print(f"stat: помилка YouTube — {yt_res}")
        yt_stats = stat_store.mark_nora(index["youtube"]) if index.get("youtube") else None
    else:
        yt_stats = yt_res

    text = format_stat_message(article_url, fb_stats, ga4_stat, tg_stat, pub_date,
                               fb_scanned, fb_error, ig_stats, tt_stats, yt_stats)
    await msg.edit_text(text, parse_mode="HTML")

    # Снімок у Нору (upsert «останній стан»): лише СВІЖІ дані — фолбеки з Нори
    # (позначені nora) не пересохраняємо. Після відповіді, щоб не тримати юзера.
    def _fresh(items):
        return items and not any(it.get("nora") for it in items)

    per_channel = {}
    if ga4_stat and not isinstance(ga4_res, Exception):
        per_channel["site"] = {"by_lang": ga4_stat}
    if _fresh(fb_stats):
        per_channel["facebook"] = fb_stats
    if _fresh(ig_stats):
        per_channel["instagram"] = ig_stats
    if tt_stats and _fresh(tt_stats):
        per_channel["tiktok"] = tt_stats
    if yt_stats and _fresh(yt_stats):
        per_channel["youtube"] = yt_stats
    if tg_stat:
        per_channel["telegram"] = tg_stat
    if per_channel:
        try:
            await asyncio.to_thread(stat_store.save_snapshot, article_id, per_channel)
        except Exception as e:
            print(f"stat: не вдалось зберегти снімок — {e}")
