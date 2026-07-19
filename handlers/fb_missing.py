"""
Монітор власних новин без Facebook-публікації.

Раз на годину (у робочі години) дивиться СВІЖІ власні (own_material=1) новини
сайту напряму в БД (nodes) і для кожної перевіряє, чи є вона у Facebook — ТІЄЮ Ж
логікою, що /stat (`get_fb_stats`: пост/рілз із посиланням на статтю у вікні
дат). Якщо свіжої власної новини у ФБ досі немає — раз (і лише раз) підказує
редакції в чат: дає лінк на новину + згенерований чернетковий пост для ФБ
окремим блоком коду (готовий до копіювання).

Вікно свіжості [now − MAX_AGE, now − MIN_AGE]:
- MIN_AGE — грейс: не смикати за новину, яку ще фізично не встигли запостити
  (SMM постить не миттєво);
- MAX_AGE — «свіжі», старе не піднімаємо.
Плюс гейт на робочі години (щоб напоминання падало, коли його можуть відпрацювати,
а не серед ночі).

Стан (storage 'fb_missing'):
- alerted — id новин, про які вже сказали → нагадуємо РІВНО раз;
- baseline_done — перший запуск проходить ТИХО: усі новини поточного вікна
  позначаються баченими без розсилки (інакше редакцію завалило б добовою
  історією власних матеріалів). Перевірити формат можна командою /fbmissing_test.

Перевірку ФБ робимо лише для НОВИХ (не в alerted) новин, тож за годину це
кілька запитів Graph API — стільки ж, скільки власних новин щойно перетнули
поріг MIN_AGE.
"""

import asyncio
import html
import json
import os
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from handlers import db, storage
from handlers.stat import get_fb_stats

KYIV_TZ = ZoneInfo("Europe/Kiev")
CHAT_ID = os.environ.get("CHAT_ID")
BASE_URL = "https://nikvesti.com"

MIN_AGE_HOURS = 3      # грейс: не чіпати новину молодшу за це (SMM постить не миттєво)
MAX_AGE_HOURS = 24     # «свіжі» — старіше не піднімаємо
WORK_HOUR_START = 9    # гейт на робочі години (Київ), як у моніторі білдера
WORK_HOUR_END = 21
ALERTED_CAP = 3000     # запобіжник росту стану
FOX_LINE_CHANCE = 0.15 # з невеликим шансом Лис жартує про самопостинг

_FOX_LINE = "😔 Я б міг і сам запостити, але Олег ще мені поки не дозволяє...(("


# ---------- БД: свіжі власні новини ----------

def _fetch_recent_own_news():
    """Свіжі власні опубліковані новини у вікні [now−MAX, now−MIN], найновіші
    перші. Автор — за owner_id (колонка author на сайті порожня); тягнемо ім'я
    і username (сайтовий логін = «юзернейм»). Порожній список без БД сайту."""
    if not db.is_configured():
        return []
    now = int(datetime.now().timestamp())
    since = now - MAX_AGE_HOURS * 3600
    until = now - MIN_AGE_HOURS * 3600
    sql = (
        "SELECT n.id AS id, n.title_ua AS title_ua, n.title AS title, "
        "n.slug_ua AS slug_ua, n.slug AS slug, n.category AS category, "
        "n.published AS published, n.owner_id AS owner_id, "
        "TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS name "
        "FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
        "WHERE n.type = 'news' AND n.status = 1 AND n.own_material = 1 "
        "AND n.published >= %s AND n.published <= %s "
        "ORDER BY n.published DESC LIMIT 100"
    )
    return db.query(sql, (since, until))


def _article_url(row):
    """Канонічний URL новини: /news/{category}/{slug}. Та сама логіка, що
    news_archive._news_url — slug_ua у двіжку вже містить id-префікс, рубрику
    вставляємо одразу (без неї двіжок редиректить і плодить дублі шляхів)."""
    slug = (row.get("slug_ua") or row.get("slug") or "").strip()
    category = (row.get("category") or "").strip()
    tail = slug or str(row.get("id") or "")
    if category and tail:
        return f"{BASE_URL}/news/{category}/{tail}"
    return f"{BASE_URL}/news/{tail}" if tail else f"{BASE_URL}/news/{row['id']}"


# ---------- Facebook: чи є пост про новину ----------

def _fb_status(row):
    """'missing' | 'present' | 'unknown'. Логіка пошуку — та сама, що /stat
    (get_fb_stats). 'unknown' при помилці/ліміті Graph API — тоді НЕ алертимо
    і НЕ позначаємо баченою (перепробуємо наступної години), бо помилка API ≠
    «поста немає»."""
    url = _article_url(row)
    article_id = str(row["id"])
    try:
        pub_date = datetime.fromtimestamp(int(row["published"]))
    except Exception:
        pub_date = None
    try:
        fb_stats, _scanned, error = get_fb_stats(url, article_id, pub_date)
    except Exception as e:
        print(f"fb_missing: помилка перевірки ФБ для {article_id} — {e}")
        return "unknown"
    if error:
        return "unknown"
    return "present" if fb_stats else "missing"


# ---------- Чернетка поста для ФБ ----------

def _find_article_node(node):
    """Перший вузол JSON-LD із @type = *Article* (NewsArticle/Article) — саме
    він несе заголовок статті. Потрібно, бо в розмітці є й Organization/WebSite
    з власним name ('МикВісті'), який не є заголовком новини."""
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x and "article" in str(x).lower() for x in types):
            return node
        for value in node.values():
            found = _find_article_node(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_article_node(item)
            if found:
                return found
    return None


def _find_key(node, key):
    """Перше непорожнє значення ключа key будь-де в дереві JSON-LD."""
    if isinstance(node, dict):
        if node.get(key):
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found:
                return found
    return None


def get_post_draft(article_url):
    """Чернетковий пост для ФБ: заголовок (json-ld title) + одне речення за
    змістом (json-ld description) + лінк. Фолбек — og:title / meta description.
    Повертає готовий текст або None, якщо сторінку не прочитати."""
    try:
        resp = requests.get(
            article_url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NikVesti-Bot/1.0)"},
        )
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        datas = []
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                datas.append(json.loads(raw))
            except (ValueError, TypeError):
                continue

        headline, description = None, None
        # Пріоритет — вузол статті (його headline/description)
        for data in datas:
            art = _find_article_node(data)
            if art:
                h = art.get("headline") or art.get("name")
                if h and not headline:
                    headline = str(h).strip()
                if art.get("description") and not description:
                    description = str(art["description"]).strip()
                if headline:
                    break
        # Фолбеки в межах JSON-LD (headline — саме ключ, не name)
        if not headline:
            h = None
            for data in datas:
                h = _find_key(data, "headline")
                if h:
                    break
            if h:
                headline = str(h).strip()
        if not description:
            d = None
            for data in datas:
                d = _find_key(data, "description")
                if d:
                    break
            if d:
                description = str(d).strip()

        if not headline:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                headline = og["content"].strip()
        if not description:
            m = soup.find("meta", attrs={"name": "description"}) or \
                soup.find("meta", property="og:description")
            if m and m.get("content"):
                description = m["content"].strip()

        if not headline and not description:
            return None

        parts = []
        if headline:
            parts.append(headline)
        if description:
            parts.append(re.sub(r"\s+", " ", description).strip())
        parts.append(f"🔗 {article_url}")
        return "\n\n".join(parts)
    except Exception as e:
        print(f"fb_missing: не вдалося зібрати чернетку — {e}")
        return None


# ---------- Розсилка ----------

def _team_tg(name):
    """TG-хендл автора зі словника TEAM (ai_messages) за іменем із БД сайту.
    Значення TEAM['tg'] — це вже готова HTML-розмітка (@username або
    <a href="tg://user?id=…">Ім'я</a>), тож у повідомленні НЕ екрануємо. None,
    якщо автора немає в TEAM (тоді покажемо лише ім'я)."""
    if not name:
        return None
    from handlers.ai_messages import TEAM
    norm = re.sub(r"\s+", " ", name).strip().lower()
    for key, info in TEAM.items():
        if re.sub(r"\s+", " ", key).strip().lower() == norm:
            return info.get("tg")
    # Токен-сет — на випадок іншого порядку ім'я/прізвище
    tokens = set(norm.split())
    for key, info in TEAM.items():
        if set(re.sub(r"\s+", " ", key).strip().lower().split()) == tokens:
            return info.get("tg")
    return None


def _author_html(row):
    """Готовий HTML-рядок автора: ім'я + TG-хендл (якщо є в TEAM). Ім'я
    екрануємо, хендл — ні (це наша довірена розмітка)."""
    name = (row.get("name") or "").strip()
    tg = _team_tg(name)
    if name and tg:
        return f"{html.escape(name)} — {tg}"
    if tg:
        return tg
    if name:
        return html.escape(name)
    return "автор невідомий"


async def _send_alert(bot, chat_id, row, note=None):
    """Повідомлення в чат: підказка + автор + лінк, а нижче — чернетка поста
    окремим блоком коду (готова до копіювання)."""
    url = _article_url(row)
    lines = [
        "🦊 Ось цієї власної новини досі немає у Facebook. "
        "Можливо, так і треба, а можливо, й ні — дивіться самі...",
    ]
    if random.random() < FOX_LINE_CHANCE:
        lines.append(_FOX_LINE)
    lines.append(f"✍️ {_author_html(row)}")
    lines.append(f'🔗 <a href="{url}">{html.escape(url)}</a>')
    if note:
        lines.append(f"<i>{html.escape(note)}</i>")

    draft = await asyncio.to_thread(get_post_draft, url)
    if draft:
        lines.append("")
        lines.append("Готовий пост для ФБ:")
        lines.append(f"<pre>{html.escape(draft)}</pre>")

    await bot.send_message(
        chat_id=chat_id, text="\n".join(lines),
        parse_mode="HTML", disable_web_page_preview=True,
    )


def _cap(alerted):
    """Лишаємо останні ALERTED_CAP id (нові в кінці) — стан не росте вічно."""
    return list(alerted)[-ALERTED_CAP:]


# ---------- Основний прогін ----------

async def check_fb_missing(bot, chat_id=None, force=False):
    """Погодинний монітор. chat_id — куди слати (дефолт CHAT_ID). force=True —
    ігнорує гейт робочих годин (для ручного /fbmissing). Повертає короткий
    підсумок (для ручного виклику)."""
    if chat_id is None:
        chat_id = CHAT_ID
    if not db.is_configured():
        return "БД сайту не налаштована — монітор недоступний."

    if not force:
        hour = datetime.now(KYIV_TZ).hour
        if hour < WORK_HOUR_START or hour >= WORK_HOUR_END:
            return None

    rows = await asyncio.to_thread(_fetch_recent_own_news)
    if not rows:
        return "Свіжих власних новин у вікні немає."

    state = storage.get_fb_missing_state()
    alerted = set(state.get("alerted", []))
    baseline_done = state.get("baseline_done", False)

    # Перший запуск — тихо позначаємо все поточне вікно баченим (без розсилки
    # добової історії). Формат перевіряється /fbmissing_test.
    if not baseline_done:
        for row in rows:
            alerted.add(row["id"])
        storage.save_fb_missing_state({"alerted": _cap(alerted), "baseline_done": True})
        return f"Baseline: {len(rows)} власних новин позначено баченими (без розсилки)."

    flagged = 0
    checked = 0
    for row in rows:
        if row["id"] in alerted:
            continue
        status = await asyncio.to_thread(_fb_status, row)
        if status == "unknown":
            continue  # помилка API — перепробуємо наступної години, не марк seen
        checked += 1
        alerted.add(row["id"])  # перевірено → нагадуємо РІВНО раз
        if status == "missing":
            await _send_alert(bot, chat_id, row)
            flagged += 1

    storage.save_fb_missing_state({"alerted": _cap(alerted), "baseline_done": True})
    return f"Перевірено {checked} нових власних новин, без ФБ — {flagged}."


# ---------- Ручні команди ----------

async def fbmissing_handler(update, context):
    """/fbmissing — прогнати монітор зараз (ігнорує гейт годин, поважає «раз»).
    Алерти йдуть у чат, де викликано."""
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⏳ Перевіряю власні новини у Facebook...")
    try:
        summary = await check_fb_missing(context.bot, chat_id=chat_id, force=True)
    except Exception as e:
        await msg.edit_text(f"Помилка: {e}")
        return
    await msg.edit_text(summary or "Поза робочими годинами.")


async def fbmissing_test_handler(update, context):
    """/fbmissing_test — прев'ю формату: бере найсвіжішу власну новину (за
    ~48 год) і шле блок підказки+чернетки в поточний чат, НЕ чіпаючи стан
    (можна ганяти скільки завгодно). Перевірка рендеру без спаму редакції."""
    chat_id = update.effective_chat.id
    if not db.is_configured():
        await update.message.reply_text("БД сайту не налаштована.")
        return
    msg = await update.message.reply_text("⏳ Готую прев'ю...")

    def _newest_own():
        now = int(datetime.now().timestamp())
        sql = (
            "SELECT n.id AS id, n.slug_ua AS slug_ua, n.slug AS slug, "
            "n.category AS category, n.published AS published, n.owner_id AS owner_id, "
            "TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS name "
            "FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
            "WHERE n.type = 'news' AND n.status = 1 AND n.own_material = 1 "
            "AND n.published <= %s ORDER BY n.published DESC LIMIT 1"
        )
        rows = db.query(sql, (now,))
        return rows[0] if rows else None

    try:
        row = await asyncio.to_thread(_newest_own)
        if not row:
            await msg.edit_text("Власних новин не знайдено.")
            return
        await msg.delete()
        await _send_alert(context.bot, chat_id, row, note="(тест формату — стан не змінено)")
    except Exception as e:
        await msg.edit_text(f"Помилка: {e}")
