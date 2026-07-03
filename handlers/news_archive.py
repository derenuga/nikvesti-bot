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
BACK_MAX_ITEMS = 6

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


def remember_results(dialog_key, items):
    storage.save_news_search(_dialog_id(dialog_key), {
        "items": items, "selected": [], "at": datetime.now().isoformat(),
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


def toggle_selection(dialog_key, n):
    """Перемкнути вибір новини №n для беку. Повертає entry або None (застаріло)."""
    entry = _get_entry(dialog_key)
    if not entry:
        return None
    if n in entry["selected"]:
        entry["selected"].discard(n)
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


# ---------- Пошук ----------

def _news_url(row):
    slug = (row.get("slug_ua") or row.get("slug") or "").strip()
    if slug:
        return f"{BASE_URL}/news/{slug}"
    return f"{BASE_URL}/news/{row['id']}"


def _fmt_date(published):
    return datetime.fromtimestamp(int(published), KYIV_TZ).strftime("%d.%m.%Y")


def search_news(dialog_key, keywords, limit=10, period_days=None):
    """Пошук опублікованих новин по заголовку.

    keywords — список слів/фраз, всі мають зустрітись у заголовку (AND).
    Кожне слово шукається як підрядок (LIKE %слово%), тому передавати
    варто основу слова без відмінкового закінчення ("Сєнкевич", "Океан").
    """
    words = [w.strip() for w in (keywords or []) if w and w.strip()]
    if not words:
        return {"error": "Порожній пошуковий запит."}
    limit = min(int(limit or 10), SEARCH_LIMIT_MAX)

    now = int(datetime.now().timestamp())
    sql = (
        "SELECT id, published, title_ua, title, slug_ua, slug "
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
    items = []
    for i, row in enumerate(rows, start=1):
        title = (row.get("title_ua") or row.get("title") or "").strip()
        items.append({
            "n": i,
            "id": row["id"],
            "date": _fmt_date(row["published"]),
            "title": title,
            "url": _news_url(row),
        })
    remember_results(dialog_key, items)
    return {
        "query": words,
        "found": len(items),
        "note": (
            "Пошук по заголовках опублікованих новин сайту (БД). "
            "Якщо результатів мало — спробуй коротшу основу слова або синонім."
        ),
        "items": items,
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
    """Ліди для списку items (елементи з id/date/title/url). Один SELECT."""
    ids = [it["id"] for it in items]
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    rows = db.query(
        f"SELECT id, content_ua, content FROM nodes WHERE id IN ({placeholders})",
        tuple(ids),
    )
    by_id = {r["id"]: r for r in rows}
    result = []
    for it in items:
        row = by_id.get(it["id"], {})
        # content_ua — українська версія; content (без суфікса) — російська,
        # беремо її тільки як фолбек для дуже старих матеріалів.
        lead = extract_lead(row.get("content_ua")) or extract_lead(row.get("content"))
        result.append({**it, "lead": lead or "(лід не вдалося витягти)"})
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


# ---------- Генерація беку (для кнопки) ----------

def _back_prompt(items):
    blocks = []
    for it in items:
        blocks.append(
            f"[{it['n']}] {it['date']} — {it['title']}\n"
            f"URL: {it['url']}\nЛід: {it['lead']}"
        )
    news_block = "\n\n".join(blocks)
    return f"""Склади журналістський бек («бекграунд») для матеріалу МикВісті на основі наших попередніх новин по цій темі.

Попередні новини (дата, заголовок, лід):

{news_block}

Вимоги:
- 2-4 короткі абзаци суцільним текстом, без списків і заголовків.
- Почни з «Нагадаємо,» — далі від свіжішого до давнішого («Раніше…», «Перед тим…»), природними переходами.
- Тільки факти з лідів і заголовків — нічого не додумуй, дати вказуй де доречно (місяць/рік, не обов'язково число).
- Це чернетка для журналіста: чиста українська, стиль стрічки новин, без емодзі і без коментарів від себе.
- Посилання на кожну новину встав HTML-гіперлінком прямо в текст: <a href="URL">анкор</a>. Анкор — дієслівна фраза факту («повідомляли», «писали», «ставало відомо», «атакували громаду», «планували облаштувати»), НЕ «тут»/«за посиланням» і НЕ голий URL. Кожна новина — один лінк.
- Символи & < > у звичайному тексті екрануй як &amp; &lt; &gt;. Крім тегів <a> — жодного іншого HTML."""


async def compose_back(items):
    """Один виклик Claude: бек з лідів. Використовується кнопкою «Написати бек»
    (в NLQ-циклі Лис пише бек сам через tool get_news_leads)."""
    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=1200,
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
    return clean_ai_text("".join(b.text for b in message.content if b.type == "text")).strip()


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
    entry = toggle_selection(dialog_key, n)
    if entry is None:
        await query.answer("Результати пошуку застаріли — повтори запит.", show_alert=True)
        return
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=build_keyboard(dialog_key))
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
    entry = _get_entry(dialog_key)
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
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло скласти бек: {e}")
