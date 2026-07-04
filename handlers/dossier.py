"""
«Досьє» — історія питання з 17-річного архіву (хвиля A, ARCHIVE_INTELLIGENCE.md).

/dossier <тема> → таймлайн по роках з лінками на наші матеріали. Головний
інструмент проти «журналісту 22, і він не знає про скандал 2014 року»:
не список заголовків, а синтез — що відбувалось, хто фігурував, чим closed.

Пайплайн (усе поверх дзеркала архіву, production-БД сайту не торкається):
1. Haiku генерує варіанти пошуку: ua/ru написання, синоніми, дотичні назви
   (старі матеріали російською, вулиці перейменовані — одним запитом не взяти).
2. Кожен варіант → повнотекстовий пошук зі стратифікацією по роках
   (archive_search, spread_years) — історія не тоне під свіжими новинами.
3. Злиття результатів: до MAX_PER_YEAR статей з кожного року, всього до
   MAX_ARTICLES — щоб покрити всі періоди, а не тільки найрелевантніший.
4. Початки текстів вибраних статей (get_excerpts) → Sonnet складає досьє:
   тільки факти з наших текстів, кожен факт з HTML-лінком.

Правило чесності те саме, що в беках: НІЧОГО не додумувати, без лінка факт
не виходить до користувача.
"""

import asyncio
import json
import os
import re
from datetime import datetime

from handlers import archive_search, bot_db, storage
from handlers.ai_messages import (
    FOX_MODEL_FAST, FOX_MODEL_SMART, FOX_SYSTEM_PROMPT,
    async_client, clean_ai_text, fox_generate,
)

MAX_VARIANTS = 5        # варіантів пошукового запиту від Haiku
PER_VARIANT_LIMIT = 30  # результатів на варіант (стратифіковано по роках)
MAX_PER_YEAR = 3        # статей з одного року в фінальній вибірці
MAX_ARTICLES = 22       # статей у досьє (баланс: покриття років vs розмір промпта)
EXCERPT_CHARS = 700     # символів тексту на статтю в промпт
DOSSIER_MAX_TOKENS = 2000

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


# ---------- Крок 1: варіанти пошуку ----------

async def _search_variants(topic):
    """3-5 пошукових формулювань теми: українською, російською (старі матеріали),
    синоніми/дотичні власні назви. Фолбек — сама тема."""
    prompt = f"""Тема для пошуку по архіву новин Миколаєва (17 років, старі матеріали російською): "{topic}"

Дай JSON-масив з 3-5 коротких пошукових запитів (2-4 слова кожен), які разом покриють цю тему в архіві:
- українське і російське написання ключових назв/прізвищ (Сєнкевич і Сенкевич — різні рядки);
- очевидні синоніми або офіційні назви об'єкта, якщо є;
- слова в базовій формі (називний відмінок).
Тільки JSON-масив рядків, без пояснень. Приклад: ["стадіон Центральний", "стадион Центральный", "реконструкція стадіону"]"""
    try:
        text = await fox_generate(prompt, system=None, model=FOX_MODEL_FAST, max_tokens=300)
        match = re.search(r"\[.*\]", text, re.DOTALL)
        variants = json.loads(match.group(0)) if match else []
        variants = [v.strip() for v in variants if isinstance(v, str) and v.strip()]
    except Exception as e:
        print(f"dossier: варіанти пошуку не вдались ({e}), шукаю по сирій темі")
        variants = []
    if topic not in variants:
        variants.insert(0, topic)
    return variants[:MAX_VARIANTS]


# ---------- Кроки 2-3: пошук і злиття ----------

def _collect_articles(variants):
    """Пошук по всіх варіантах + злиття: до MAX_PER_YEAR статей на рік,
    всього до MAX_ARTICLES, хронологічно. Синхронна (викликати в потоці)."""
    merged = {}  # id -> item; порядок появи ~ релевантність
    for variant in variants:
        try:
            items = archive_search.search_items(
                variant, limit=PER_VARIANT_LIMIT, spread_years=True, per_year=MAX_PER_YEAR
            )
        except Exception as e:
            print(f"dossier: пошук '{variant}' не вдався — {e}")
            continue
        for it in items:
            merged.setdefault(it["id"], it)

    # Розкладаємо по роках і беремо до MAX_PER_YEAR з кожного (перші в merged —
    # релевантніші, бо search_items вже відранжував), потім рівномірно ріжемо
    # до MAX_ARTICLES, зберігаючи покриття всіх років.
    by_year = {}
    for it in merged.values():
        year = it["date"][-4:]
        by_year.setdefault(year, []).append(it)
    for year in by_year:
        by_year[year] = by_year[year][:MAX_PER_YEAR]

    years = sorted(by_year)
    selected = []
    depth = 0
    while len(selected) < MAX_ARTICLES:
        added = False
        for year in years:
            if depth < len(by_year[year]) and len(selected) < MAX_ARTICLES:
                selected.append(by_year[year][depth])
                added = True
        if not added:
            break
        depth += 1
    selected.sort(key=lambda it: datetime.strptime(it["date"], "%d.%m.%Y"))
    return selected


# ---------- Крок 4: синтез ----------

def _dossier_prompt(topic, articles):
    blocks = []
    for it in articles:
        blocks.append(
            f"[{it['date']}] {it['title']}\nURL: {it['url']}\nТекст: {it['excerpt']}"
        )
    corpus = "\n\n".join(blocks)
    return f"""Склади «досьє» — стислу історію питання для журналіста МикВісті на основі наших матеріалів за різні роки.

Тема: {topic}

Наші матеріали (дата, заголовок, початок тексту):

{corpus}

Вимоги:
- Почни рядком: 🦊 <b>Досьє: {topic}</b>
- Далі таймлайн по періодах, від давнього до свіжого. Кожен період — рядок <b>РІК</b> (або <b>РІК-РІК</b>) і 1-3 стислі речення: що сталось, хто фігурував, суми/рішення якщо є.
- ТІЛЬКИ факти з наданих текстів і заголовків — нічого не додумуй і не узагальнюй поза ними. Якщо матеріали не покривають якийсь період — просто пропусти його, не вигадуй.
- Кожен згаданий факт підкріпи лінком: HTML <a href="URL">…</a>, яким обгортаєш 1-3 слова, що ВЖЕ стоять у реченні. НЕ «тут», НЕ голий URL. Використовуй кожен матеріал не більше одного разу.
- Наприкінці, якщо з текстів це видно: рядок <b>Фігуранти:</b> імена/організації через кому, і рядок <b>Відкриті питання:</b> що обіцяли/почали і чим (не) закінчилось.
- Якщо наданих матеріалів замало для повноцінної історії — чесно скажи це одним рядком на початку і дай те, що є.
- Обсяг: до 3300 символів. Чиста українська, без емодзі (крім першого рядка), без Markdown. Символи & < > у звичайному тексті екрануй як &amp; &lt; &gt; (крім тегів <a> і <b>)."""


async def _compose_dossier(topic, articles):
    message = await async_client.messages.create(
        model=FOX_MODEL_SMART,
        max_tokens=DOSSIER_MAX_TOKENS,
        thinking={"type": "disabled"},
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _dossier_prompt(topic, articles)}],
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
        print(f"ai_usage: не вдалось записати dossier — {e}")
    return clean_ai_text("".join(b.text for b in message.content if b.type == "text")).strip()


# ---------- Команда ----------

async def dossier_handler(update, context):
    """/dossier <тема> — історія питання з архіву, з лінками на матеріали."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    topic = update.message.text.partition(" ")[2].strip()
    if not topic:
        await update.message.reply_text(
            "Використання: /dossier <тема>\n"
            "Напр.: /dossier стадіон Центральний\n/dossier забудова Намиву"
        )
        return
    if not bot_db.is_configured():
        await update.message.reply_text(
            "🦊 Досьє працює поверх дзеркала архіву, а БД бота ще не налаштована "
            "(BOT_DATABASE_URL на Railway)."
        )
        return
    if await asyncio.to_thread(archive_search.count_articles) == 0:
        await update.message.reply_text(
            "🦊 Дзеркало архіву порожнє — спершу разовий /archive_backfill."
        )
        return

    msg = await update.message.reply_text(f"🦊 Піднімаю архів по темі «{topic}»…")
    try:
        variants = await _search_variants(topic)
        await _edit_quiet(msg, f"🦊 Шукаю: {', '.join(variants)}…")
        selected = await asyncio.to_thread(_collect_articles, variants)
        if not selected:
            await msg.edit_text(
                f"🦊 По темі «{topic}» в архіві нічого не знайшов. "
                "Спробуй інше формулювання або конкретну назву/прізвище."
            )
            return
        await _edit_quiet(msg, f"🦊 Знайшов {len(selected)} матеріалів за "
                               f"{selected[0]['date'][-4:]}–{selected[-1]['date'][-4:]} роки, читаю і складаю досьє…")
        with_excerpts = await asyncio.to_thread(
            archive_search.get_excerpts, [it["id"] for it in selected], EXCERPT_CHARS
        )
        text = await _compose_dossier(topic, with_excerpts)
        if len(text) > 4000:
            # Ліміт Telegram 4096; ріжемо по останньому цілому рядку
            text = text[:4000].rsplit("\n", 1)[0] + "\n\n(обрізано — тема надто багата, звузь період)"
        try:
            await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            # Битий HTML від моделі — plain text, лінки Telegram підсвітить сам
            await msg.edit_text(text, disable_web_page_preview=True)
        # У пам'ять діалогу NLQ — щоб працювали follow-up'и («а що по 2016?»,
        # «скороти»). Імпорт тут — проти циклу query_router ↔ dossier.
        from handlers.query_router import remember_exchange
        remember_exchange((update.effective_chat.id, update.effective_user.id),
                          f"Досьє по темі: {topic}", text)
    except Exception as e:
        await _edit_quiet(msg, f"❌ Не вийшло скласти досьє: {e}")


async def _edit_quiet(msg, text):
    try:
        await msg.edit_text(text)
    except Exception:
        pass
