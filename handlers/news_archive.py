"""
Архів новин сайту (БД) — пошук по темі + генерація журналістського «беку».

Перший контентний модуль поверх прямого доступу до production-БД сайту
(handlers/db.py). Відповідає на питання редакції типу «що ми останнє писали
про Сєнкевича?» через NLQ (query_router дає Лису tools звідси), показує список
«дата — заголовок (лінк)» і вміє скласти бек («Нагадаємо, раніше…») з лідів
вибраних новин.

Схема БД (розвідано 03.07.2026, /dbquery + Railway console):
- nodes.title_ua / title — заголовок ua/ru; content_ua / content — повний HTML
  тіла ua/ru (content без суфікса = російська версія!);
- slug_ua вже містить id ("320651-reabilitatsiia-…"), колонка url порожня →
  URL матеріалу = https://nikvesti.com/news/{slug_ua};
- перший елемент content_ua часто <div class="imgbox-…"> з фото і підписом —
  лід шукаємо як перший змістовний <p>, а не тупо перший блок.

Пошук — LIKE по title_ua/title (індексу немає, але varchar(500) сканується
швидко навіть на 17-річній таблиці; content у пошук не беремо — longtext).
"""

import asyncio
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import db, storage
from handlers.ai_messages import FOX_SYSTEM_PROMPT, FOX_MODEL_SMART, async_client, clean_ai_text

BASE_URL = "https://nikvesti.com"
KYIV_TZ = ZoneInfo("Europe/Kiev")

SEARCH_LIMIT_MAX = 20
# Лід: перший <p> коротший за це — швидше службовий рядок/підпис, ніж абзац.
LEAD_MIN_CHARS = 60
LEAD_MAX_CHARS = 700
# Скільки новин максимум тягнемо в бек (щоб не роздувати промпт і текст).
# Той самий кап обмежує і кількість галочок вибору (toggle_selection): скільки
# бек візьме — стільки й можна вибрати, не більше. Список у пошуку зазвичай ≤10.
BACK_MAX_ITEMS = 10

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# ---------- Пам'ять останнього пошуку ----------
#
# Для кнопок відбору і посилань «бек по 1 і 3»: результати останнього пошуку
# на (chat_id, user_id) з TTL. Зберігаємо в storage (Railway Volume), а не в
# пам'яті процесу — інакше редеплой бота між пошуком і натисканням кнопки
# давав «Результати застаріли» одразу після деплою.

RESULTS_TTL_MINUTES = 30


def _dialog_id(dialog_key):
    return f"{dialog_key[0]}:{dialog_key[1]}"


def remember_results(dialog_key, items, turn_id=None, query=None):
    # query — людський текст пошуку: тема для обліку беків (usage_report),
    # «по каким вопросам беки формировали» у щоденному звіті адміну.
    storage.save_news_search(_dialog_id(dialog_key), {
        "items": items, "selected": [], "at": datetime.now().isoformat(),
        "turn_id": turn_id, "query": query,
    })


def _get_entry(dialog_key):
    """Останній пошук розмови або None (немає/протух). selected — set[int]."""
    entry = storage.get_news_search(_dialog_id(dialog_key))
    if not entry or not entry.get("items"):
        return None
    try:
        at = datetime.fromisoformat(entry.get("at", ""))
    except (ValueError, TypeError):
        return None
    if datetime.now() - at > timedelta(minutes=RESULTS_TTL_MINUTES):
        return None
    entry["selected"] = set(entry.get("selected", []))
    return entry


def get_last_results(dialog_key):
    entry = _get_entry(dialog_key)
    return entry["items"] if entry else []


SELECTION_LIMIT_REACHED = "limit"  # сигнал: вибір уперся в кап (більше не можна)


def toggle_selection(dialog_key, n):
    """Перемкнути вибір новини №n для беку. Повертає entry, None (застаріло) або
    SELECTION_LIMIT_REACHED — коли намагаються вибрати понад BACK_MAX_ITEMS
    (бек однаково більше не візьме, тож і виділяти більше не даємо)."""
    entry = _get_entry(dialog_key)
    if not entry:
        return None
    if n in entry["selected"]:
        entry["selected"].discard(n)
    elif len(entry["selected"]) >= BACK_MAX_ITEMS:
        return SELECTION_LIMIT_REACHED
    else:
        entry["selected"].add(n)
    storage.save_news_search(_dialog_id(dialog_key), {
        "items": entry["items"],
        "selected": sorted(entry["selected"]),
        "at": entry["at"],  # TTL рахується від часу пошуку, не від тапів
    })
    return entry


# ---------- Клавіатура відбору новин під результатами пошуку ----------
#
# Номерні кнопки — чекбокси: тап ставить/знімає ✅ біля номера, підпис кнопки
# беку міняється на «Бек з новин 1+3…». Поки нічого не вибрано — бек з усіх.

BACK_CALLBACK_DATA = "fox_news_back"
SELECT_CALLBACK_PREFIX = "fox_news_sel:"
KEYBOARD_ROW = 5


def build_keyboard(dialog_key):
    """Клавіатура під списком: номери-чекбокси + кнопка беку з динамічним підписом."""
    entry = _get_entry(dialog_key)
    if not entry or not entry["items"]:
        return None
    selected = entry["selected"]
    number_buttons = [
        InlineKeyboardButton(
            f"{it['n']} ✅" if it["n"] in selected else str(it["n"]),
            callback_data=f"{SELECT_CALLBACK_PREFIX}{it['n']}",
        )
        for it in entry["items"]
    ]
    rows = [number_buttons[i:i + KEYBOARD_ROW] for i in range(0, len(number_buttons), KEYBOARD_ROW)]
    if selected:
        label = "🦊 Бек з новин " + "+".join(str(n) for n in sorted(selected))
    else:
        label = "🦊 Бек з усіх цих новин"
    rows.append([InlineKeyboardButton(label, callback_data=BACK_CALLBACK_DATA)])
    return InlineKeyboardMarkup(rows)


# ---------- Звірка показаного списку з памʼяттю ----------
#
# Кнопки-номери мають відповідати РІВНО тому списку, що Лис показав у тексті —
# інакше вони безглузді. Два реальні баги, які це лагодить:
#   1) Лис нічого не показав («нічого не знайшов»), але пошук наповнив памʼять
#      25 результатами → раніше зʼявлялось 25 кнопок під відповіддю без списку.
#   2) Лис показав N новин, але надрукував URL із рубрикою, а в памʼяті URL без
#      рубрики (старі статті норою зберігались без category) → точний матч URL
#      ламався, і з N новин лишалась 1 кнопка.
# Тому ПІСЛЯ відповіді Лиса парсимо рядки «N. … <a href="URL">» і зводимо памʼять
# рівно під показаний список. Матч показаного лінка з памʼяттю — гнучкий: повний
# URL → slug-хвіст (останній сегмент) → id зі слага. Так розбіжність рубрики чи
# дрібна нормалізація URL уже не викидає новину.

_SHOWN_LINE_RE = re.compile(r'^\s*(\d+)\.\s.*?<a\s+href="([^"]+)"', re.MULTILINE)


def _match_shown_url(url, by_url, by_tail, by_id):
    """Знайти новину памʼяті за показаним лінком: точний URL → slug-хвіст → id."""
    if url in by_url:
        return by_url[url]
    tail = _slug_from_url(url)
    if tail and tail in by_tail:
        return by_tail[tail]
    m = re.match(r"^(\d+)", tail or "")
    if m and int(m.group(1)) in by_id:
        return by_id[int(m.group(1))]
    return None


def reconcile_shown(dialog_key, shown_text):
    """Звести памʼять останнього пошуку до новин, які Лис реально показав.

    Повертає список показаних новин (перенумерований 1..k), під який зведено
    памʼять — саме по ньому треба будувати кнопки. None — якщо у тексті НЕ
    розпізнано список новин (напр. Лис відповів «нічого не знайшов»): тоді
    кнопки показувати не можна, бо їм нема до чого приʼвʼязатись."""
    entry = _get_entry(dialog_key)
    if not entry or not entry["items"]:
        return None
    pairs = _SHOWN_LINE_RE.findall(shown_text or "")
    if not pairs:
        return None
    by_url, by_tail, by_id = {}, {}, {}
    for it in entry["items"]:
        by_url[it["url"]] = it
        tail = _slug_from_url(it["url"])
        if tail:
            by_tail.setdefault(tail, it)
        by_id[it["id"]] = it
    shown, seen = [], set()
    for _n, url in pairs:
        it = _match_shown_url(url, by_url, by_tail, by_id)
        if it and it["id"] not in seen:
            seen.add(it["id"])
            shown.append({**it, "n": len(shown) + 1})
    if not shown:
        return None
    remember_results(dialog_key, shown, turn_id=entry.get("turn_id"),
                     query=entry.get("query"))
    return shown


# ---------- Санітизація лінків у тексті від моделі ----------
#
# КРИТИЧНО (інцидент 20.07.2026): модель у беку ВИГАДАЛА слаги для старих
# новин — транслітерувала заголовок у неіснуючий хвіст URL, і лінки віддавали
# 404 (в базі у старих матеріалів слага немає, канонічний лінк — /news/{cat}/{id}).
# Промптова заборона не гарантія, тому лінки чистимо детермінований кодом:
# кожен href на nikvesti.com звіряється з URL-ами новин, які реально віддали
# tools. Не збігся точно → пробуємо витягти id і підставити КАНОНІЧНИЙ url
# цієї новини з бази; id невідомий → тег <a> знімаємо, лишаючи текст без лінка.
# Жоден вигаданий лінк назовні не виходить.

_ANCHOR_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def sanitize_news_links(text, items):
    """Замінити/зняти вигадані моделлю лінки nikvesti.com у тексті.

    items — новини, які реально віддавали tools (потрібні поля id та url).
    Повертає текст, у якому кожен nikvesti-лінк або точно з items, або
    виправлений на канонічний url за id, або розлінкований."""
    if not text or not items:
        return text
    allowed = {it["url"] for it in items if it.get("url")}
    by_id = {str(it["id"]): it["url"] for it in items if it.get("url")}
    fixed, stripped = [], []

    def _fix(m):
        url, inner = m.group(1), m.group(2)
        if url in allowed or "nikvesti.com" not in url:
            return m.group(0)
        # id шукаємо і в хвості (/news/politics/143444-slug), і в будь-якому
        # сегменті шляху (/ru/news/143444) — головне знайти відому новину.
        for num in re.findall(r"\d{3,}", url):
            if num in by_id:
                fixed.append(url)
                return f'<a href="{by_id[num]}">{inner}</a>'
        stripped.append(url)
        return inner  # вигаданий лінк без відомого id — знімаємо тег

    result = _ANCHOR_RE.sub(_fix, text)
    # Слід у логах Railway: видно, ЩО саме модель вигадала і що з цим зробили
    # (діагностика: «лінків нема, бо модель не поставила» vs «санітизатор зняв»).
    if fixed or stripped:
        print(
            f"sanitize_news_links: виправлено {len(fixed)} лінків на канонічні "
            f"{fixed[:3]}, розлінковано {len(stripped)} невідомих {stripped[:3]}"
        )
    return result


def append_missing_links(text, items):
    """Детермінований дожим лінків у бек: новини з items, чий url НЕ зʼявився
    лінком у тексті, дописуються блоком «Джерела» з канонічними лінками.

    Модель стохастична: попри промпт вона періодично «соромиться» коротких
    URL старих новин і не ставить лінки (інцидент 20.07, скріни 2/5). Промпт
    гарантій не дає — гарантію дає код: кожна новина беку в результаті
    залінкована або в тексті, або в цьому блоці."""
    if not text or not items:
        return text
    missing = [it for it in items if it.get("url") and it["url"] not in text]
    if not missing:
        return text
    lines = []
    for it in missing:
        title = (it.get("title") or it.get("url") or "").strip()
        title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f'• {it.get("date", "")} — <a href="{it["url"]}">{title}</a>')
    print(f"append_missing_links: модель не залінкувала {len(missing)} новин, дописуємо блоком")
    return text + "\n\n📎 Згадані матеріали без лінка в тексті:\n" + "\n".join(lines)


# ---------- Пошук ----------

def _news_url(row):
    """Канонічний URL: /news/{category}/{slug}. Без рубрики двіжок сайту
    редиректить (/news/{slug} → /news/incidents/{slug}), але такі лінки
    розходяться по репостах і плодять дублі шляхів у GA4 — тому рубрику
    (nodes.category) вставляємо одразу."""
    slug = (row.get("slug_ua") or row.get("slug") or "").strip()
    category = (row.get("category") or "").strip()
    # Без слага (старі матеріали) хвіст URL — id, але рубрику лишаємо:
    # /news/politics/269222, а не /news/269222 (інакше двіжок редиректить).
    tail = slug or str(row.get("id") or "")
    if not tail:
        return f"{BASE_URL}/news/{row['id']}"
    if category:
        return f"{BASE_URL}/news/{category}/{tail}"
    return f"{BASE_URL}/news/{tail}"


def _fmt_date(published):
    return datetime.fromtimestamp(int(published), KYIV_TZ).strftime("%d.%m.%Y")


def search_news(dialog_key, keywords, limit=10, period_days=None, turn_id=None):
    """Пошук опублікованих новин по заголовку.

    keywords — список слів/фраз, всі мають зустрітись у заголовку (AND).
    Кожне слово шукається як підрядок (LIKE %слово%), тому передавати
    варто основу слова без відмінкового закінчення ("Сєнкевич", "Океан").

    turn_id — маркер одного NLQ-запиту (передає query_router): якщо Лис
    робить КІЛЬКА пошуків за один запит ("дорога на Одесу" + "траса"),
    результати зливаються в один список з наскрізною нумерацією (дублікати
    по id відкидаються). Інакше другий пошук затирав перший, і номери на
    кнопках не збігалися з тим, що Лис показав у тексті (баг з бекем по
    трасі: галочки застосувались до іншого списку, новини "губились").
    """
    words = [w.strip() for w in (keywords or []) if w and w.strip()]
    if not words:
        return {"error": "Порожній пошуковий запит."}
    limit = min(int(limit or 10), SEARCH_LIMIT_MAX)

    now = int(datetime.now().timestamp())
    sql = (
        "SELECT id, published, title_ua, title, slug_ua, slug, category "
        "FROM nodes WHERE status = 1 AND type = 'news' AND published <= %s"
    )
    params = [now]
    if period_days:
        sql += " AND published >= %s"
        params.append(now - int(period_days) * 86400)
    for w in words:
        # Слово шукаємо і в ua-, і в ru-заголовку: старі матеріали бувають
        # тільки з title (ru), а власні назви часто збігаються.
        sql += " AND (title_ua LIKE %s OR title LIKE %s)"
        like = f"%{w}%"
        params.extend([like, like])
    sql += " ORDER BY published DESC LIMIT %s"
    params.append(limit)

    rows = db.query(sql, tuple(params))

    # Той самий NLQ-запит вже шукав? Доклеюємо до існуючого списку
    # (наскрізна нумерація), а не затираємо його.
    prev_items = []
    prev_query = None
    entry = _get_entry(dialog_key)
    if turn_id and entry and entry.get("turn_id") == turn_id:
        prev_items = entry["items"]
        prev_query = entry.get("query")
    seen_ids = {it["id"] for it in prev_items}

    new_items = []
    for row in rows:
        if row["id"] in seen_ids:
            continue
        title = (row.get("title_ua") or row.get("title") or "").strip()
        new_items.append({
            "n": None,  # проставляється після сортування всього списку
            "id": row["id"],
            "published": int(row["published"]),
            "date": _fmt_date(row["published"]),
            "title": title,
            "url": _news_url(row),
        })
    # Кілька пошуків одного запиту зливаються (turn_id). Кожен пошук окремо
    # відсортований DESC, але склеєні блоки давали немонотонний по датах
    # список (найсвіжіша новина опинялась усередині). Тому сортуємо ВЕСЬ
    # накопичений список за датою публікації (найсвіжіше зверху) і лише тоді
    # проставляємо наскрізні номери n — щоб і текст, і кнопки йшли хронологічно.
    all_items = prev_items + new_items
    all_items.sort(key=lambda it: it.get("published", 0), reverse=True)
    for i, it in enumerate(all_items, start=1):
        it["n"] = i
    # Кілька пошуків одного запиту → теми склеюються («Панченко + Кантор»)
    q_text = " ".join(words)
    if prev_query and prev_query != q_text:
        q_text = f"{prev_query} + {q_text}"
    remember_results(dialog_key, all_items, turn_id=turn_id, query=q_text)
    return {
        "query": words,
        "found_new": len(new_items),
        "note": (
            "Пошук по заголовках опублікованих новин сайту (БД). "
            "Якщо результатів мало — спробуй коротшу основу слова або синонім. "
            "У items — ПОВНИЙ накопичений список цього запиту (кілька пошуків "
            "зливаються). Показуй користувачу ВСІ items рівно під цими номерами n — "
            "не пропускай, не перенумеровуй: кнопки під повідомленням прив'язані "
            "саме до цих номерів."
        ),
        "items": all_items,
    }


# ---------- Лід (перший змістовний абзац) ----------

# Службові початки абзаців, які лідом не є (підписи до фото, врізки).
_SKIP_PREFIXES = ("фото:", "фото ", "читайте також", "нагадаємо", "джерело:")


def extract_lead(html):
    """Перший змістовний абзац з HTML матеріалу.

    Перший блок часто <div class="imgbox…"> з фото і підписом — тому йдемо
    по всіх <p> і беремо перший достатньо довгий, що не схожий на підпис.
    Якщо <p> немає взагалі (старі матеріали) — беремо чистий текст без
    картинок і скриптів.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "figure"]):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(r"imgbox|lightbox|title")):
        tag.decompose()

    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) < LEAD_MIN_CHARS:
            continue
        if text.lower().startswith(_SKIP_PREFIXES):
            continue
        return text[:LEAD_MAX_CHARS]

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:LEAD_MAX_CHARS] or None


def _fetch_leads(items):
    """Ліди для списку items (елементи з id/date/title/url). Один SELECT.

    Заодно ПЕРЕСКЛАДАЄ url із БД сайту (slug_ua/slug + category): у беку
    посилання мають бути канонічними (/news/{рубрика}/{slug}). Дзеркало-нора
    для старих статей могло не мати рубрики, і url виходив без неї
    (/news/269222 замість /news/politics/269222) — БД сайту рубрику має завжди."""
    ids = [it["id"] for it in items]
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    rows = db.query(
        f"SELECT id, slug_ua, slug, category, content_ua, content "
        f"FROM nodes WHERE id IN ({placeholders})",
        tuple(ids),
    )
    by_id = {r["id"]: r for r in rows}
    result = []
    for it in items:
        row = by_id.get(it["id"])
        if row:
            # content_ua — українська версія; content (без суфікса) — російська,
            # беремо її тільки як фолбек для дуже старих матеріалів.
            lead = extract_lead(row.get("content_ua")) or extract_lead(row.get("content"))
            url = _news_url(row)
        else:
            lead, url = None, it.get("url")
        result.append({**it, "url": url, "lead": lead or "(лід не вдалося витягти)"})
    return result


def get_news_leads(dialog_key, numbers=None, node_ids=None):
    """Ліди (перші абзаци) новин з останнього пошуку.

    numbers — номери зі списку останнього пошуку (1-based); без numbers і
    node_ids — усі знайдені (до BACK_MAX_ITEMS)."""
    items = get_last_results(dialog_key)
    if node_ids:
        wanted = {int(i) for i in node_ids}
        items = [it for it in items if it["id"] in wanted]
    elif numbers:
        wanted = {int(n) for n in numbers}
        items = [it for it in items if it["n"] in wanted]
    if not items:
        return {"error": (
            "Немає збережених результатів пошуку (минуло понад 30 хв або "
            "пошук ще не робився). Спочатку виклич search_news_archive."
        )}
    items = items[:BACK_MAX_ITEMS]
    return {"items": _fetch_leads(items)}


# ---------- Бек із надісланих посилань ----------
#
# Користувач кидає кілька URL nikvesti.com і просить бек саме з них. Дістаємо
# id по slug (slug_ua у двіжку містить id-префікс, але буває й без нього — тоді
# матчимо чистий slug; якщо slug починається з цифр-id, є фолбек на nodes.id).

def _slug_from_url(url):
    """Останній сегмент шляху URL новини (= slug_ua у БД). Без query/anchor."""
    path = re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1].strip()


def fetch_items_by_urls(urls):
    """Знайти новини за їхніми URL і повернути items з лідами.

    Матчимо по slug_ua/slug (як у посиланні), фолбек — id із цифрового префікса
    слага. Порядок результату — свіжіше→давніше (за датою публікації, НЕ за
    порядком посилань), нумерація n наскрізна. missing — URL, яких немає в базі.
    Тул для беку з конкретних посилань (get_leads_from_urls у NLQ)."""
    if not urls:
        return {"error": "Не передано жодного посилання."}
    found, seen_ids, missing = [], set(), []
    for url in urls:
        slug = _slug_from_url(url)
        if not slug:
            missing.append(url)
            continue
        cols = ("id, published, title_ua, title, slug_ua, slug, category, "
                "content_ua, content")
        rows = db.query(
            f"SELECT {cols} FROM nodes "
            "WHERE type='news' AND (slug_ua = %s OR slug = %s) LIMIT 1",
            (slug, slug),
        )
        if not rows:
            # id: і з префікса слага (143444-slug), і ГОЛИЙ id без слага
            # (/news/business/143444 — канонічний лінк старих новин).
            m = re.match(r"^(\d+)(?:-|$)", slug)
            if m:
                rows = db.query(
                    f"SELECT {cols} FROM nodes WHERE id = %s LIMIT 1",
                    (int(m.group(1)),),
                )
        if not rows:
            missing.append(url)
            continue
        row = rows[0]
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        title = (row.get("title_ua") or row.get("title") or "").strip()
        lead = extract_lead(row.get("content_ua")) or extract_lead(row.get("content"))
        found.append({
            "id": row["id"],
            "published": int(row["published"]),
            "date": _fmt_date(row["published"]),
            "title": title,
            "url": _news_url(row),
            "lead": lead or "(лід не вдалося витягти)",
        })
    found.sort(key=lambda it: it["published"], reverse=True)
    for i, it in enumerate(found, start=1):
        it["n"] = i
    result = {"items": found}
    if missing:
        result["missing"] = missing
    if not found:
        result["error"] = "Жодне посилання не знайшлось у базі новин."
    return result


# ---------- Генерація беку (для кнопки) ----------

def _back_prompt(items):
    blocks = []
    for it in items:
        blocks.append(
            f"[{it['n']}] {it['date']} — {it['title']}\n"
            f"URL: {it['url']}\nЛід: {it['lead']}"
        )
    news_block = "\n\n".join(blocks)
    current_year = datetime.now(KYIV_TZ).year
    return f"""Склади журналістський бек («бекграунд») для матеріалу МикВісті на основі наших попередніх новин по цій темі.

Попередні новини (дата, заголовок, лід):

{news_block}

Вимоги:
- 2-4 короткі абзаци суцільним текстом, без списків і заголовків (якщо новин багато — 7+ — можна до 5-6 абзаців, але кожну новину все одно згадай).
- Почни з «Нагадаємо,» — далі від свіжішого до давнішого («Раніше…», «Перед тим…»), природними переходами.
- Тільки факти з лідів і заголовків — нічого не додумуй, дати вказуй де доречно (місяць/рік, не обов'язково число).
- Рік вказуй ЗАВЖДИ при першій згадці події та на кожному новому абзаці / новій події («ще у квітні 2026-го анонсували…», а не просто «ще у квітні»). Якщо в межах того ж абзацу йдеться про ті самі події того ж року — рік далі можна не повторювати. Перескочив на новий абзац чи нову подію — знову назви рік. Поточний рік — {current_year} (для орієнтації, але це не привід не вказувати рік для цьогорічних подій).
- Це чернетка для журналіста: чиста українська, стиль стрічки новин, без емодзі і без коментарів від себе.
- Посилання на кожну новину — HTML-гіперлінк, яким ти обгортаєш 1-3 слова, ЩО ВЖЕ СТОЯТЬ у реченні (зазвичай дієслівну фразу факту): «Ільюк <a href="URL">пропонував провести ротацію</a> керівників адміністрацій». ЗАБОРОНЕНО дописувати анкор окремим хвостом через тире чи кому («…, — заявляв про дитсадки») — речення має читатись однаково і з лінком, і без нього. НЕ «тут»/«за посиланням», НЕ голий URL. Кожна новина — один лінк.
- URL у href бери РІВНО як у полі URL новини, символ-у-символ. Короткий URL без слага (напр. /news/business/143444) — НОРМАЛЬНИЙ робочий канонічний лінк старої новини: лінкуй ним так само впевнено, як і довгим, і НЕ уникай таких новин. ЗАБОРОНЕНО конструювати, «відновлювати» чи транслітерувати слаг із заголовка — вигаданий хвіст дає 404. Лінк має бути в КОЖНОЇ згаданої новини, пропускати лінки не можна.
- Символи & < > у звичайному тексті екрануй як &amp; &lt; &gt;. Крім тегів <a> — жодного іншого HTML."""


async def compose_back(items):
    """Один виклик Claude: бек з лідів. Використовується кнопкою «Написати бек»
    (в NLQ-циклі Лис пише бек сам через tool get_news_leads)."""
    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=1800,
        thinking={"type": "disabled"},
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _back_prompt(items)}],
    )
    try:
        u = message.usage
        storage.record_ai_usage(
            FOX_MODEL_SMART,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception as e:
        print(f"ai_usage: не вдалось записати news_back — {e}")
    text = clean_ai_text("".join(b.text for b in message.content if b.type == "text")).strip()
    # Захист від вигаданих слагів: кожен лінк звіряється з url новин із бази.
    text = sanitize_news_links(text, items)
    # Гарантія лінків: новини без лінка в тексті дописуються блоком «Джерела».
    return append_missing_links(text, items)


# ---------- Колбеки кнопок ----------

def _callback_dialog_key(update):
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    return (update.effective_chat.id, user_id), user_id


async def news_select_callback(update, context):
    """Тап по номерній кнопці: перемкнути ✅ вибору новини для беку
    і перемалювати клавіатуру (текст повідомлення не чіпаємо)."""
    query = update.callback_query
    dialog_key, user_id = _callback_dialog_key(update)
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        n = int(query.data[len(SELECT_CALLBACK_PREFIX):])
    except (ValueError, IndexError):
        await query.answer()
        return
    # Файлові операції storage — у потоці, щоб не підвішувати event loop.
    entry = await asyncio.to_thread(toggle_selection, dialog_key, n)
    if entry is None:
        await query.answer("Результати пошуку застаріли — повтори запит.", show_alert=True)
        return
    if entry == SELECTION_LIMIT_REACHED:
        await query.answer(
            f"Максимум {BACK_MAX_ITEMS} новин на бек. Зніми якусь, щоб вибрати іншу.",
            show_alert=True,
        )
        return
    # Відповідаємо Telegram ДО перемальовування клавіатури: спінер на кнопці
    # гасне одразу, а ✅ доїжджає наступним запитом.
    await query.answer()
    try:
        keyboard = await asyncio.to_thread(build_keyboard, dialog_key)
        await query.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        # "Message is not modified" при подвійному тапі тощо — не страшно.
        pass


async def news_back_callback(update, context):
    """Кнопка беку під результатами пошуку: бек з вибраних (✅) новин,
    а якщо нічого не вибрано — з усіх знайдених."""
    query = update.callback_query
    dialog_key, user_id = _callback_dialog_key(update)
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    entry = await asyncio.to_thread(_get_entry, dialog_key)
    if not entry or not entry["items"]:
        await query.answer("Результати пошуку застаріли — повтори запит.", show_alert=True)
        return
    items = entry["items"]
    selected = entry["selected"]
    if selected:
        items = [it for it in items if it["n"] in selected]
    await query.answer()
    which = "вибраних новин" if selected else "усіх знайдених новин"
    msg = await query.message.reply_text(f"🦊 Читаю ліди {which} і складаю бек…")
    try:
        with_leads = await asyncio.to_thread(_fetch_leads, items[:BACK_MAX_ITEMS])
        text = await compose_back(with_leads)
        if len(items) > BACK_MAX_ITEMS:
            text += f"\n\n(Взяв перші {BACK_MAX_ITEMS} новин — забагато для одного беку.)"
        try:
            await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            # Битий HTML від моделі — шлемо як plain text, лінки лишаться видимими.
            await msg.edit_text(text, disable_web_page_preview=True)
        # Кладемо бек у пам'ять діалогу NLQ, щоб працювали follow-up'и
        # («скороти», «прибери другий абзац»). Імпорт тут — щоб уникнути
        # циклічного імпорту query_router ↔ news_archive на старті.
        from handlers.query_router import remember_exchange
        remember_exchange(dialog_key, "Напиши бек по знайдених новинах", text)
        # Облік для щоденного звіту адміну (usage_report): тема беку —
        # пошуковий запит, з якого прийшов список (фолбек — перший заголовок).
        try:
            from handlers.usage_report import display_name
            topic = entry.get("query") or (items[0].get("title") if items else "")
            await asyncio.to_thread(
                storage.record_usage_back, user_id, display_name(query.from_user),
                topic, len(items[:BACK_MAX_ITEMS]))
        except Exception as e:
            print(f"usage: не вдалось записати бек — {e}")
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло скласти бек: {e}")
