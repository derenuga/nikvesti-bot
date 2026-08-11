"""
Контент-звіт проєкту за місяць (Mini App «Команда», сторінка проєкту).

Олег, 11.08: «я зашел в проект IMS, жму "контент-звіт", выбираю "август" —
оно сразу подсвечивает, что сделано 50 публикаций, жму — и у меня html-отчет».

Що всередині звіту:
- реквізити проєкту: назва, донор із лого, строки, квота з CMS, звітний місяць;
- публікації місяця, згруповані по ТЕМАТИКАХ проєкту (тематика відома з
  зарахованих матчів team_task_matches — auto/confirmed; без матчу матеріал
  іде в купу «Поза тематиками», але у звіті лишається: він вийшов у проєкті);
- кожна публікація: № · дата · заголовок-лінк · автор із круглою аватаркою ·
  перегляди сайту (лічильник nodes, накопичувальний);
- під нею — лінк на Facebook-пост із метриками і лінк на Telegram-пост із
  переглядами.

Звідки беруться лінки соцмереж (порядок — рішення Олега 11.08):
1. НОРА: article_stats (знімки /stat, object_id постів FB) і індекс tg_posts
   у storage — якщо матеріал уже «статили», пошук не потрібен, лише свіжі
   метрики по відомих id.
2. БД сайту: колонки nodes fb_post_id / fb_post_url / fb_post_views і
   tg_post_url / tg_post_views (точні назви зняв Олег з information_schema
   11.08). По fb_post_id тягнуться свіжі метрики Graph; збій → лінк із
   лічильником *_post_views сайту. Значення все одно звіряється регекспом
   і типом — чужу схему правлять без нас.
3. Живий пошук, ОДНИМ прогоном на весь місяць, а не по матеріалу: стрічка
   постів FB + рілзи в вікні [місяць − 1 день; кінець + 10 днів] і одна
   прокрутка t.me/s по вікну місяця. Побічний продукт /stat поштучно тут не
   годиться: 50 публікацій × пагінація вікна = сотні запитів до Graph API.
Знайдене живим пошуком ОДРАЗУ пишеться в Нору (save_snapshot + tg_posts) —
наступний звіт і /stat підуть швидким шляхом («если в Норе нет — надо ее
туда добавлять»).

Побудова асинхронна, як імпакт-кейси: кнопка в апці відповідає одразу, бот
шле «збираю…» в приват, за кілька хвилин туди ж прилітає .html (друкується
в PDF з браузера). Збій — теж у приват, текстом.
"""

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from handlers import db, bot_db, storage, stat_store, team_projects
from handlers.helpers import MONTHS_UA
from handlers.news_stats import KYIV_TZ, _table_columns, _views_column

BASE_URL = "https://nikvesti.com"

# Скільки публікацій максимум тягнемо у звіт (місяць великого проєкту — десятки;
# сотня+ означає помилку відбору, і краще чесно обрізати з позначкою)
MAX_PUBLICATIONS = 120

# Вікно живого пошуку FB: пост про матеріал виходить у ці дні після публікації
# (та сама константа-логіка, що FB_SEARCH_FORWARD_DAYS у /stat)
FB_FORWARD_DAYS = 10

_jobs_lock = threading.Lock()
_jobs = set()  # (project_id, ym) — щоб дабл-тап не запускав збір двічі


# ---------- Періоди ----------

def _month_range(ym):
    """('2026-08') → (start_ts, end_ts, label, (y, m)) за Києвом."""
    y, m = int(ym[:4]), int(ym[5:7])
    start = datetime(y, m, 1, tzinfo=KYIV_TZ)
    end = datetime(y + 1, 1, 1, tzinfo=KYIV_TZ) if m == 12 else \
        datetime(y, m + 1, 1, tzinfo=KYIV_TZ)
    return int(start.timestamp()), int(end.timestamp()), \
        f"{MONTHS_UA[m]} {y}", (y, m)


def month_options(project_id, start_date=None):
    """Місяці для пікера звіту з к-стю публікацій у кожному — щоб апка одразу
    «підсвічувала, що зроблено 50 публікацій». Межі — від старту проєкту (або
    першої публікації) до поточного місяця, свіжі перші."""
    if not db.is_configured():
        return []
    rows = db.query(
        "SELECT published FROM nodes WHERE partner_project = %s AND status = 1 "
        "AND published <= UNIX_TIMESTAMP()", (int(project_id),))
    counts = {}
    for r in rows:
        dt = datetime.fromtimestamp(int(r["published"]), KYIV_TZ)
        counts[(dt.year, dt.month)] = counts.get((dt.year, dt.month), 0) + 1

    now = datetime.now(KYIV_TZ)
    first = None
    if start_date:
        d = datetime.fromtimestamp(int(start_date), KYIV_TZ)
        first = (d.year, d.month)
    if counts:
        oldest = min(counts)
        first = min(first, oldest) if first else oldest
    if not first:
        first = (now.year, now.month)

    out = []
    y, m = now.year, now.month
    while (y, m) >= first:
        out.append({
            "ym": f"{y:04d}-{m:02d}",
            "label": f"{MONTHS_UA[m]} {y}",
            "count": counts.get((y, m), 0),
        })
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    if not out:  # старт проєкту в майбутньому — хай пікер не буде глухим
        out.append({"ym": f"{now.year:04d}-{now.month:02d}",
                    "label": f"{MONTHS_UA[now.month]} {now.year}", "count": 0})
    return out


# ---------- Публікації місяця (БД сайту) ----------

# Колонки nodes із постами соцмереж — ТОЧНІ назви, зняті Олегом з БД 11.08
# (information_schema): fb_post_id, fb_post_url, fb_post_views, tg_post_url,
# tg_post_views. Наявність кожної все одно звіряємо інтроспекцією (кеш
# _table_columns) — схему сайту правлять без нас, і зникла колонка не має
# класти SELECT. Значення URL додатково перевіряємо регекспом: перший підхід
# «вгадати колонку за назвою» впіймав числовий прапорець і поклав збір.
_SITE_SOCIAL_COLS = ("fb_post_id", "fb_post_url", "fb_post_views",
                     "tg_post_url", "tg_post_views")
_FB_URL_RE = re.compile(r"https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.com|fb\.watch)/\S+", re.I)
_TG_URL_RE = re.compile(r"https?://(?:t|telegram)\.me/\S+", re.I)


def _social_link_cols():
    """Наявні в nodes колонки соцпостів (список назв)."""
    cols = _table_columns("nodes")
    return [c for c in _SITE_SOCIAL_COLS if c in cols]


def _pub_url(row):
    """URL матеріалу з урахуванням типу (як _nora_url в impact_archive)."""
    tail = (row.get("slug_ua") or row.get("slug") or "").strip() or str(row["id"])
    if (row.get("type") or "news") == "article":
        return f"{BASE_URL}/articles/{tail}"
    cat = (row.get("category") or "").strip()
    return f"{BASE_URL}/news/{cat}/{tail}" if cat else f"{BASE_URL}/news/{tail}"


def _as_url(value, pattern):
    """Значення колонки → URL, якщо воно строкове і схоже на потрібну мережу
    (тип у чужій схемі — не факт: інцидент 11.08, int замість URL)."""
    m = pattern.search(value) if isinstance(value, str) else None
    return m.group(0) if m else None


def _as_int(value):
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def list_publications(project_id, start_ts, end_ts):
    """Публікації проєкту за місяць із автором, аватаркою, переглядами сайту
    і соцколонками nodes (fb_post_*, tg_post_*)."""
    vcol = _views_column()
    vsel = f", n.{vcol} AS views" if vcol else ""
    soc_cols = _social_link_cols()
    extra = "".join(f", n.{c}" for c in soc_cols)
    rows = db.query(
        "SELECT n.id, n.published, n.title_ua, n.title, n.slug_ua, n.slug, "
        f"n.category, n.own_material, n.type{vsel}{extra}, u.avatar, "
        "TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS author "
        "FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
        "WHERE n.partner_project = %s AND n.status = 1 "
        "AND n.published >= %s AND n.published < %s AND n.published <= UNIX_TIMESTAMP() "
        f"ORDER BY n.published LIMIT {MAX_PUBLICATIONS + 1}",
        (int(project_id), int(start_ts), int(end_ts)))
    pubs = []
    for r in rows[:MAX_PUBLICATIONS]:
        dt = datetime.fromtimestamp(int(r["published"]), KYIV_TZ)
        fb_id = r.get("fb_post_id")
        pubs.append({
            "id": int(r["id"]),
            "title": (r.get("title_ua") or r.get("title") or "").strip(),
            "url": _pub_url(r),
            "date": dt.strftime("%d.%m.%Y"),
            "ts": int(r["published"]),
            "own": int(r.get("own_material") or 0) == 1,
            "kind": r.get("type") or "news",
            "views": int(r.get("views") or 0) if vcol else None,
            "author": (r.get("author") or "").strip() or None,
            "avatar": team_projects.avatar_url(r.get("avatar"),
                                               team_projects.AVATAR_SIZE_SM),
            "site_fb_id": str(fb_id).strip() if fb_id not in (None, "", 0) else None,
            "site_fb": _as_url(r.get("fb_post_url"), _FB_URL_RE),
            "site_fb_views": _as_int(r.get("fb_post_views")),
            "site_tg": _as_url(r.get("tg_post_url"), _TG_URL_RE),
            "site_tg_views": _as_int(r.get("tg_post_views")),
        })
    return pubs, len(rows) > MAX_PUBLICATIONS


def _themes_for(project_id, pub_ids):
    """{node_id: theme_name} із зарахованих матчів (auto/confirmed) Нори."""
    if not pub_ids or not bot_db.is_configured():
        return {}
    try:
        from handlers import team_matches
        team_matches.ensure_match_schema()
        rows = bot_db.query(
            "SELECT m.ref, t.theme_name FROM team_task_matches m "
            "LEFT JOIN team_creative_tasks t ON t.id = m.task_id "
            "WHERE m.source = 'site' AND m.status IN ('auto', 'confirmed') "
            "AND m.ref = ANY(%s)",
            ([str(i) for i in pub_ids],))
    except Exception as e:
        print(f"content_report: тематики з матчів не прочитались — {e}")
        return {}
    return {int(r["ref"]): (r["theme_name"] or "").strip() or None
            for r in rows if str(r["ref"]).isdigit()}


def _theme_order(project_id):
    """Порядок тематик як у картці проєкту (team_project_themes) +
    {назва: план}."""
    if not bot_db.is_configured():
        return [], {}
    try:
        from handlers import team_tasks
        themes = [t for t in team_tasks.list_themes()
                  if t["project_id"] == int(project_id)]
    except Exception as e:
        print(f"content_report: тематики проєкту не прочитались — {e}")
        return [], {}
    return [t["name"] for t in themes], \
        {t["name"]: t.get("planned") for t in themes}


# ---------- Facebook ----------

def _fb_configured():
    import os
    return bool(os.environ.get("FACEBOOK_PAGE_TOKEN")
                and os.environ.get("FACEBOOK_PAGE_ID"))


def _fb_from_nora(pub, entry):
    """Знімок Нори → свіжі метрики по відомих object_id; збій Graph API →
    сам знімок із позначкою дати (той самий фолбек, що в /stat)."""
    if not entry or not entry.get("items"):
        return None
    from handlers import stat
    try:
        items, _, _ = stat.get_fb_stats_by_objects(entry["items"])
        return items
    except Exception as e:
        print(f"content_report: FB по індексу {pub['id']} не оновився — {e}")
        return stat_store.mark_nora(entry)


def _fb_item_from_post(post, metrics):
    from handlers.facebook import fix_permalink
    short = post["id"].split("_")[1] if "_" in post["id"] else post["id"]
    permalink = fix_permalink(post.get("permalink_url") or "") or \
        f"https://www.facebook.com/nikvesti/posts/{short}"
    return {
        "type": "post", "id": str(post["id"]), "permalink": permalink,
        "date": post.get("created_time") or "",
        "views": metrics["views"], "reactions": metrics["reactions"],
        "comments": metrics["comments"], "shares": metrics["shares"],
    }


def _fb_sweep(missing, start_ts, end_ts):
    """ОДИН прогін стрічки постів + рілзів на весь місяць і матчинг усіх
    відсутніх публікацій по ньому. Метрики — поштучно, лише на знайдених.
    Повертає {node_id: items} і пише знайдене в Нору."""
    if not missing or not _fb_configured():
        return {}
    from handlers import stat
    from handlers.facebook import get_page_posts, get_post_metrics, get_reel_insights, fix_permalink

    since = start_ts - 86400
    until = min(end_ts + FB_FORWARD_DAYS * 86400, int(time.time()))
    try:
        posts = get_page_posts(since, until, max_pages=12)
    except Exception as e:
        print(f"content_report: стрічка постів FB не прочиталась — {e}")
        posts = []
    try:
        reels = stat._get_fb_reels(since, until)
    except Exception as e:
        print(f"content_report: рілзи FB не прочитались — {e}")
        reels = []

    out = {}
    for pub in missing:
        clean = stat._clean_url(pub["url"])
        items, reel_keys = [], set()
        for reel in reels:
            if not stat._matches_article(reel.get("description") or "", clean, pub["id"]):
                continue
            reactions, comments, shares = get_reel_insights(reel["id"])
            items.append({
                "type": "reel", "id": str(reel["id"]),
                "permalink": fix_permalink(reel.get("permalink_url", "")),
                "date": reel.get("created_time") or "",
                "views": stat._get_reel_views(reel["id"]),
                "reactions": reactions, "comments": comments, "shares": shares,
            })
            reel_keys |= stat._reel_keys(reel)
        for post in posts:
            if not stat._post_matches_article(post, clean, pub["id"]):
                continue
            if reel_keys and (stat._post_keys(post) & reel_keys):
                continue  # дубль рілза у стрічці — лишаємо рілз
            items.append(_fb_item_from_post(post, get_post_metrics(post["id"])))
        if items:
            out[pub["id"]] = items
            # знайдене живим пошуком лягає в Нору — наступний звіт і /stat
            # підуть швидким шляхом
            stat_store.save_snapshot(pub["id"], {"facebook": items})
    return out


def _fb_object_id(pub):
    """Graph-сумісний id поста з колонок сайту: fb_post_id як є (чи з
    префіксом сторінки, якщо він «короткий»), інакше число з fb_post_url."""
    import os
    raw = pub.get("site_fb_id")
    if raw and re.fullmatch(r"\d+_\d+", raw):
        return raw
    if raw and raw.isdigit():
        return f"{os.environ.get('FACEBOOK_PAGE_ID', '')}_{raw}"
    m = re.search(r"(?:posts/|story_fbid=|videos/|/reel/)(\d{6,})",
                  pub.get("site_fb") or "")
    if m:
        return f"{os.environ.get('FACEBOOK_PAGE_ID', '')}_{m.group(1)}"
    return None


def _fb_from_site(pub):
    """Пост із колонок сайту (fb_post_id/fb_post_url/fb_post_views): свіжі
    метрики по id, збій Graph → лінк із переглядами-лічильником сайту."""
    link = pub.get("site_fb")
    oid = _fb_object_id(pub) if _fb_configured() else None
    if not link and not oid:
        return None
    item = {"type": "post", "id": oid or "", "permalink": link or "", "date": "",
            "views": pub.get("site_fb_views"), "reactions": None,
            "comments": None, "shares": None}
    if pub.get("site_fb_views") is not None:
        item["site_counter"] = True  # перегляди — лічильник сайту, не Graph
    if oid:
        from handlers.facebook import get_post_metrics
        metrics = get_post_metrics(oid)
        if any(metrics[k] is not None for k in ("views", "reactions", "comments", "shares")):
            item.update({"views": metrics["views"], "reactions": metrics["reactions"],
                         "comments": metrics["comments"], "shares": metrics["shares"]})
            item.pop("site_counter", None)
            if not item["permalink"]:
                item["permalink"] = ("https://www.facebook.com/nikvesti/posts/"
                                     + oid.split("_")[1])
    if not item["permalink"]:
        return None
    return [item]


def collect_facebook(pubs, nora_entries, start_ts, end_ts):
    """{node_id: items}: Нора → колонки БД сайту → живий прогін місяця для
    решти. nora_entries — знімки article_stats, прочитані заздалегідь однією
    сесією (мережеві походи не мають тримати з'єднання з Норою)."""
    out = {}
    for pub in pubs:
        items = _fb_from_nora(pub, nora_entries.get(pub["id"]))
        if items:
            out[pub["id"]] = items
            continue
        items = _fb_from_site(pub)
        if items:
            out[pub["id"]] = items
            # у Нору — лише з живим object_id: снімок без id не дає швидкого
            # шляху, а от перекривав би собою спроби знайти пост наступного разу
            if any(it.get("id") for it in items):
                stat_store.save_snapshot(pub["id"], {"facebook": items})
    missing = [p for p in pubs if p["id"] not in out]
    out.update(_fb_sweep(missing, start_ts, end_ts))
    return out


# ---------- Telegram ----------

def _tg_sweep(start_ts, end_ts, wanted_ids, max_pages=90, pace_seconds=0.25):
    """Одна прокрутка t.me/s по вікну місяця: {article_id: {message_id, views}}.
    Перегляди — прямо з блока стрічки, окремий похід на embed не потрібен.
    Лишаємо найменший message_id — оригінальний пост (як у бекфілі індексу)."""
    from handlers import telegram_stats as ts

    if not wanted_ids:
        return {}
    start_dt = datetime.fromtimestamp(start_ts, KYIV_TZ)
    end_dt = datetime.fromtimestamp(end_ts, KYIV_TZ) + timedelta(days=2)
    found = {}
    try:
        before = ts.estimate_message_id(end_dt) + 250
    except Exception as e:
        print(f"content_report: оцінка message_id не вдалась — {e}")
        return {}
    for _ in range(max_pages):
        try:
            html = ts._fetch_html(f"/s/{ts.CHANNEL}", params={"before": before})
        except Exception as e:
            print(f"content_report: стрічка t.me обірвалась — {e}")
            break
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            break
        page_min, oldest = None, None
        for block in blocks:
            msg_id, views, hrefs, dt = ts._parse_message_block(block)
            if msg_id is not None and (page_min is None or msg_id < page_min):
                page_min = msg_id
            if dt is not None and (oldest is None or dt < oldest):
                oldest = dt
            if msg_id is None:
                continue
            for aid in ts._article_ids_in_hrefs(hrefs):
                aid = int(aid)
                if aid not in wanted_ids:
                    continue
                cur = found.get(aid)
                if cur is None or msg_id < cur["message_id"]:
                    found[aid] = {"message_id": msg_id, "views": views}
        if page_min is None or page_min <= 1:
            break
        if oldest is not None and oldest < start_dt:
            break
        before = page_min
        time.sleep(pace_seconds)
    return found


def collect_telegram(pubs, start_ts, end_ts):
    """{node_id: {"url", "views"}}: індекс tg_posts → колонка tg_post_url БД
    сайту → прокрутка місяця ЛИШЕ для тих, кого ніде немає. Перегляди — з
    embed t.me; збій → лічильник tg_post_views сайту. Знайдене пишеться в
    індекс і в Нору."""
    from handlers import telegram_stats as ts

    out, to_index, known, missing = {}, {}, [], []
    for pub in pubs:
        entry = storage.get_tg_post(pub["id"])
        message_id = entry["message_id"] if entry else None
        if not message_id:
            m = re.search(r"(?:t|telegram)\.me/(?:s/)?[\w]+/(\d+)",
                          pub.get("site_tg") or "")
            if m:
                message_id = int(m.group(1))
                to_index[pub["id"]] = message_id
        if message_id:
            known.append((pub, message_id))
        else:
            missing.append(pub)

    swept = _tg_sweep(start_ts, end_ts, {p["id"] for p in missing}) if missing else {}
    for pub in missing:
        hit = swept.get(pub["id"])
        if hit:
            out[pub["id"]] = {"url": f"https://t.me/{ts.CHANNEL}/{hit['message_id']}",
                              "views": hit["views"]}
            to_index[pub["id"]] = hit["message_id"]

    for pub, message_id in known:
        views = None
        try:
            views = ts.fetch_post_views(message_id)
        except Exception as e:
            print(f"content_report: перегляди TG-поста {message_id} — {e}")
        if views is None:
            views = pub.get("site_tg_views")  # лічильник сайту як фолбек
        out[pub["id"]] = {"url": f"https://t.me/{ts.CHANNEL}/{message_id}",
                          "views": views}

    for pub in pubs:
        if pub["id"] in out:
            stat_store.save_snapshot(pub["id"], {"telegram": out[pub["id"]]})
    if to_index:
        try:
            storage.bulk_save_tg_posts(to_index)
        except Exception as e:
            print(f"content_report: індекс tg_posts не записався — {e}")
    return out


# ---------- HTML ----------

def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;") \
        .replace("<", "&lt;").replace(">", "&gt;")


def _num(n):
    """12345 → '12 345' (нерозривний тонкий пробіл)."""
    if n is None:
        return "—"
    return f"{int(n):,}".replace(",", " ")


def _plural(n):
    if n % 10 == 1 and n % 100 != 11:
        return "публікація"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "публікації"
    return "публікацій"


def _fb_line(items):
    parts = []
    for it in items:
        metrics = []
        if it.get("views") is not None:
            metrics.append(f"{_num(it['views'])} переглядів")
        if it.get("reactions") is not None:
            metrics.append(f"{_num(it['reactions'])} реакцій")
        if it.get("comments") is not None:
            metrics.append(f"{_num(it['comments'])} коментарів")
        if it.get("shares") is not None:
            metrics.append(f"{_num(it['shares'])} поширень")
        label = "рілз" if it.get("type") == "reel" else "пост"
        stale = f' <span class="stale">знімок від {_esc(it["nora"])}</span>' if it.get("nora") else ""
        if it.get("site_counter"):
            stale += ' <span class="stale">лічильник сайту</span>'
        parts.append(
            f'<a href="{_esc(it.get("permalink") or "")}">{label}</a>'
            f'{" — " + " · ".join(metrics) if metrics else ""}{stale}')
    return "; ".join(parts)


def _pub_block(number, pub, fb_items, tg_item):
    ava = (f'<img class="ava" src="{_esc(pub["avatar"])}" alt="">'
           if pub.get("avatar") else '<span class="ava ph"></span>')
    meta_bits = []
    if pub.get("author"):
        meta_bits.append(f'{ava}<span>{_esc(pub["author"])}</span>')
    if pub.get("views") is not None:
        meta_bits.append(f"<span>{_num(pub['views'])} переглядів на сайті</span>")
    soc = []
    if fb_items:
        soc.append(f'<div class="soc"><span class="net fb">Facebook</span> {_fb_line(fb_items)}</div>')
    else:
        soc.append('<div class="soc none"><span class="net fb">Facebook</span> поста не знайшли</div>')
    if tg_item:
        views = f' — {_num(tg_item["views"])} переглядів' if tg_item.get("views") is not None else ""
        soc.append(f'<div class="soc"><span class="net tg">Telegram</span> '
                   f'<a href="{_esc(tg_item["url"])}">пост</a>{views}</div>')
    else:
        soc.append('<div class="soc none"><span class="net tg">Telegram</span> поста не знайшли</div>')
    return f"""<div class="pub">
  <div class="ph1"><span class="num">{number}</span><span class="date">{_esc(pub["date"])}</span>
    <a class="title" href="{_esc(pub["url"])}">{_esc(pub["title"] or pub["url"])}</a></div>
  <div class="meta">{''.join(meta_bits)}</div>
  {''.join(soc)}
</div>"""


def _fmt_range(project):
    def d(ts):
        return datetime.fromtimestamp(int(ts), KYIV_TZ).strftime("%d.%m.%Y") if ts else "…"
    if not project.get("start_date") and not project.get("end_date"):
        return None
    return f"{d(project.get('start_date'))} — {d(project.get('end_date'))}"


def render_html(project, label, groups, fb, tg, totals, capped):
    """Самодостатній HTML у стилі імпакт-кейсів (Georgia, брендовий синій),
    але з картинками: лого донора в шапці, аватарки в рядках."""
    logo = project.get("logo") or project.get("logo_orig")
    logo_html = (f'<img class="logo" src="{_esc(logo)}" '
                 f'onerror="this.onerror=null;this.src=\'{_esc(project.get("logo_orig") or "")}\'" alt="">'
                 if logo else "")
    quota = (project.get("kpi_news") or 0) + (project.get("kpi_articles") or 0)
    period = _fmt_range(project)
    req_rows = [("Проєкт", project.get("name")),
                ("Донор", project.get("partner")),
                ("Строки проєкту", period),
                ("Квота (CMS)", f"{project.get('kpi_news') or 0} новин + "
                                f"{project.get('kpi_articles') or 0} статей" if quota else None),
                ("Звітний місяць", label)]
    req = "\n".join(
        f'<div class="req"><span class="k">{_esc(k)}</span><span class="v">{_esc(v)}</span></div>'
        for k, v in req_rows if v)

    tiles = [("Публікацій", _num(totals["pubs"]))]
    if totals["site_views"] is not None:
        tiles.append(("Переглядів на сайті", _num(totals["site_views"])))
    if totals["fb_found"]:
        fb_v = f' · {_num(totals["fb_views"])} переглядів' if totals["fb_views"] else ""
        tiles.append(("У Facebook", f"{totals['fb_found']} постів{fb_v}"))
    if totals["tg_found"]:
        tg_v = f' · {_num(totals["tg_views"])} переглядів' if totals["tg_views"] else ""
        tiles.append(("У Telegram", f"{totals['tg_found']} постів{tg_v}"))
    tiles_html = "\n".join(
        f'<div class="tile"><div class="tv">{_esc(v)}</div><div class="tk">{_esc(k)}</div></div>'
        for k, v in tiles)

    sections = []
    number = 0
    for theme_name, plan, pubs in groups:
        head = _esc(theme_name)
        sub = f"{len(pubs)} {_plural(len(pubs))}"
        if plan:
            sub += f" · план {plan}"
        blocks = []
        for pub in pubs:
            number += 1
            blocks.append(_pub_block(number, pub, fb.get(pub["id"]), tg.get(pub["id"])))
        sections.append(f'<h2>{head} <span class="m">{sub}</span></h2>\n' + "\n".join(blocks))

    capped_note = (f'<p class="m">Показано перші {MAX_PUBLICATIONS} публікацій '
                   f'місяця — решта не вмістилась у звіт.</p>' if capped else "")
    generated = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    title = f"Контент-звіт · {project.get('partner') or project.get('name')} · {label}"
    return f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; color: #16283c;
         max-width: 780px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }}
  .brand {{ font-family: -apple-system, Arial, sans-serif; font-size: 13px;
            color: #6a7f96; letter-spacing: .04em; text-transform: uppercase; }}
  .head {{ display: flex; align-items: flex-start; gap: 20px; justify-content: space-between; }}
  h1 {{ font-size: 25px; line-height: 1.25; margin: 8px 0 4px; }}
  h2 {{ font-size: 17px; margin: 30px 0 6px; border-bottom: 1px solid #dbe5f0;
        padding-bottom: 4px; }}
  h2 .m {{ font-weight: normal; }}
  .logo {{ width: 84px; height: 84px; object-fit: contain; border-radius: 10px;
           background: #fff; border: 1px solid #e3ebf4; padding: 6px; flex: none; }}
  .reqs {{ background: #f3f7fc; border-left: 3px solid #1f6fd6; border-radius: 6px;
           padding: 12px 16px; margin: 14px 0; }}
  .req {{ display: flex; gap: 10px; font-size: 14px; padding: 2px 0; }}
  .req .k {{ color: #6a7f96; width: 150px; flex: none;
             font-family: -apple-system, Arial, sans-serif; font-size: 12.5px;
             padding-top: 2px; }}
  .tiles {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0 6px; }}
  .tile {{ border: 1px solid #dbe5f0; border-radius: 8px; padding: 8px 14px; }}
  .tv {{ font-size: 17px; font-weight: bold; }}
  .tk {{ font-family: -apple-system, Arial, sans-serif; font-size: 11.5px; color: #6a7f96; }}
  .m {{ color: #6a7f96; font-size: 13.5px; }}
  .pub {{ padding: 10px 0 8px; border-bottom: 1px dotted #dbe5f0;
          page-break-inside: avoid; }}
  .ph1 {{ display: flex; gap: 10px; align-items: baseline; }}
  .num {{ color: #6a7f96; font-family: -apple-system, Arial, sans-serif;
          font-size: 12.5px; min-width: 20px; text-align: right; flex: none; }}
  .date {{ color: #6a7f96; font-size: 13.5px; white-space: nowrap; flex: none; }}
  .title {{ font-weight: bold; }}
  .meta {{ display: flex; gap: 14px; align-items: center; margin: 4px 0 2px 30px;
           font-size: 13.5px; color: #3c5064; }}
  .meta > span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .ava {{ width: 20px; height: 20px; border-radius: 50%; object-fit: cover;
          vertical-align: middle; }}
  .ava.ph {{ display: inline-block; background: #dbe5f0; }}
  .soc {{ margin: 2px 0 0 30px; font-size: 13.5px; }}
  .soc.none {{ color: #9fb0c2; }}
  .net {{ display: inline-block; font-family: -apple-system, Arial, sans-serif;
          font-size: 11px; letter-spacing: .03em; text-transform: uppercase;
          color: #6a7f96; width: 74px; }}
  .stale {{ color: #b0763c; font-size: 12px; }}
  a {{ color: #1f6fd6; }}
  @media print {{ body {{ margin: 0; }} }}
</style></head><body>
<div class="head">
  <div>
    <div class="brand">МикВісті · nikvesti.com · контент-звіт</div>
    <h1>{_esc(project.get('partner') or project.get('name'))} — {_esc(label)}</h1>
  </div>
  {logo_html}
</div>
<div class="reqs">{req}</div>
<div class="tiles">{tiles_html}</div>
{capped_note}
{''.join(sections)}
<p class="m">Згенеровано {generated} · Лис Микита, бот редакції МикВісті</p>
</body></html>"""


# ---------- Збірка ----------

def build_report(project, ym):
    """Повний блокуючий збір: публікації → тематики → FB → TG → HTML.
    Повертає (fname, html, totals) або кидає виняток із людською причиною."""
    if not db.is_configured():
        raise RuntimeError("БД сайту недоступна — публікації не зібрати")
    start_ts, end_ts, label, _ = _month_range(ym)
    pubs, capped = list_publications(project["id"], start_ts, end_ts)
    if not pubs:
        raise RuntimeError(f"За {label.lower()} у проєкті немає публікацій")

    # Читання Нори — однією сесією; мережеві походи (FB/TG) — поза нею,
    # щоб не тримати Postgres-з'єднання відкритим хвилинами
    nora_entries = {}
    with bot_db.session():
        theme_by_id = _themes_for(project["id"], [p["id"] for p in pubs])
        order, plans = _theme_order(project["id"])
        if bot_db.is_configured():
            for p in pubs:
                try:
                    nora_entries[p["id"]] = (stat_store.load_index(p["id"]) or {}).get("facebook")
                except Exception:
                    nora_entries[p["id"]] = None
    fb = collect_facebook(pubs, nora_entries, start_ts, end_ts)
    tg = collect_telegram(pubs, start_ts, end_ts)

    # Групування по тематиках у порядку картки проєкту; без матчу — в кінець
    by_theme = {}
    for pub in pubs:
        by_theme.setdefault(theme_by_id.get(pub["id"]), []).append(pub)
    groups = []
    for name in order:
        if name in by_theme:
            groups.append((name, plans.get(name), by_theme.pop(name)))
    for name in sorted(n for n in by_theme if n):
        groups.append((name, plans.get(name), by_theme.pop(name)))
    if None in by_theme:
        groups.append(("Поза тематиками", None, by_theme.pop(None)))

    vcol_present = pubs[0]["views"] is not None
    totals = {
        "pubs": len(pubs),
        "site_views": sum(p["views"] or 0 for p in pubs) if vcol_present else None,
        "fb_found": sum(1 for p in pubs if fb.get(p["id"])),
        "fb_views": sum(it.get("views") or 0
                        for items in fb.values() for it in items),
        "tg_found": sum(1 for p in pubs if tg.get(p["id"])),
        "tg_views": sum((tg[k].get("views") or 0) for k in tg),
    }
    html = render_html(project, label, groups, fb, tg, totals, capped)
    fname = f"content-report-{project['id']}-{ym}.html"
    return fname, html, {**totals, "label": label}


def start_report(bot, chat_id, project, ym):
    """Запускає асинхронний збір; False — такий самий уже збирається."""
    key = (int(project["id"]), ym)
    with _jobs_lock:
        if key in _jobs:
            return False
        _jobs.add(key)
    asyncio.create_task(_build_and_send(bot, chat_id, project, ym, key))
    return True


async def _build_and_send(bot, chat_id, project, ym, key):
    """Збір у фоні + файл у приват. Помилки не летять нагору — людині в
    приват, причиною."""
    from io import BytesIO

    _, _, label, _ = _month_range(ym)
    name = project.get("partner") or project.get("name")
    try:
        try:
            await bot.send_message(
                chat_id,
                f"🦊 Збираю контент-звіт «{name}» за {label.lower()} — ходжу по "
                f"Facebook і Telegram, це кілька хвилин. Файл прийде сюди.")
        except Exception as e:
            print(f"content_report: не долетіло «збираю» — {e}")
        fname, html, totals = await asyncio.to_thread(build_report, project, ym)
        buf = BytesIO(html.encode("utf-8"))
        buf.name = fname
        caption = (f"🦊 Контент-звіт «{name}» за {totals['label'].lower()}: "
                   f"{totals['pubs']} публікацій"
                   f"{', у Facebook знайшлось ' + str(totals['fb_found']) if totals['fb_found'] else ''}"
                   f"{', у Telegram — ' + str(totals['tg_found']) if totals['tg_found'] else ''}. "
                   "Відкривається в браузері, звідти — у PDF.")
        await bot.send_document(chat_id=chat_id, document=buf, caption=caption)
    except Exception as e:
        msg = str(e)[:300]
        print(f"content_report: збір {key} впав — {msg}")
        try:
            await bot.send_message(
                chat_id, f"🦊 Контент-звіт «{name}» за {label.lower()} не зібрався: {msg}")
        except Exception:
            pass
    finally:
        with _jobs_lock:
            _jobs.discard(key)
