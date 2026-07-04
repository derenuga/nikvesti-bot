"""
Дзеркало архіву новин сайту у власній БД бота (хвиля A, ARCHIVE_INTELLIGENCE.md).

Тягне nodes (type='news') з production-MySQL сайту (handlers/db.py, read-only)
у Postgres бота (handlers/bot_db.py, таблиця articles): id, дати, заголовки
ua/ru, slug і ЧИСТИЙ ТЕКСТ тіла (HTML → текст конвертується один раз тут,
а не при кожному пошуку). Поверх дзеркала працює archive_search (FTS) і /dossier.

Чому дзеркало, а не запити напряму: production-БД read-only (FULLTEXT-індекс
не створиш), лімітована (5 з'єднань, 10 000 запитів/год), і кожен важкий LIKE
по longtext — ризик для сайту. Дзеркало знімає всі три обмеження.

Два режими:
1. Первинний бекфіл — /archive_backfill: порціями по BACKFILL_BATCH за id,
   з паузами (повага до лімітів KEY4), resumable (курсор у sync_state,
   при обриві продовжує з місця зупинки). Сотні тисяч статей ≈ 1-2 години.
2. Інкрементальний sync — щогодини о :50 (scheduler): добирає створене
   і відредаговане з моменту останнього запуску (курсор по max(updated,
   published), з перекриттям — краще двічі upsert-нути, ніж пропустити).

Обидва тихо пропускаються, якщо не налаштована будь-яка з двох БД.
"""

import asyncio
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from handlers import bot_db, db
from handlers.helpers import escape_html
from handlers.notifier import notify_error

KYIV_TZ = ZoneInfo("Europe/Kiev")
BASE_URL = "https://nikvesti.com"

# Порція бекфілу: 150 рядків з longtext-контентом — це одиниці МБ на запит,
# вкладається в read_timeout 30с production-БД. ~2000 запитів на 300 тис.
# статей, з паузою BACKFILL_PAUSE — далеко в межах 10 000 запитів/год.
BACKFILL_BATCH = 150
BACKFILL_PAUSE = 1.0  # секунд між порціями
# Інкремент: більше 500 змін за годину — аномалія (масова правка), добереться
# наступними запусками, курсор рухається по факту оброблених рядків.
INCREMENTAL_LIMIT = 500
# Перекриття курсора: перечитуємо 2 хв "назад", щоб не втратити рядки,
# записані в ту саму секунду, що й курсор.
CURSOR_OVERLAP_SEC = 120

_NODE_COLUMNS = (
    "id, published, updated, status, own_material, owner_id, "
    "title_ua, title, slug_ua, slug, content_ua, content"
)

_backfill_running = {"flag": False}

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


# ---------- HTML → чистий текст ----------

# Службові блоки, які прибираємо з тіла (не текст статті): скрипти, стилі,
# фрейми, фото з підписами (figure/imgbox/lightbox), галереї, рекламні й
# «читайте також»-врізки за типовими класами сайту. Список свідомо
# консервативний — краще лишити трохи шуму, ніж вирізати справжній абзац.
# ⚠️ Перевірити на реальних статтях через /archive_sample і за потреби
# розширити (структуру старого HTML з dev-середовища не видно).
_JUNK_TAGS = ["script", "style", "iframe", "figure", "noscript", "form"]
_JUNK_CLASS_RE = re.compile(
    r"imgbox|lightbox|gallery|related|read-?also|readmore|banner|advert|social|share|subscribe",
    re.IGNORECASE,
)


def html_to_text(html):
    """Чистий текст тіла матеріалу: без скриптів/стилів/фото/врізок,
    пробіли схлопнуті, кап TEXT_CAP (захист tsvector від переповнення)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_JUNK_TAGS):
        tag.decompose()
    for tag in soup.find_all(class_=_JUNK_CLASS_RE):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:bot_db.TEXT_CAP] or None


def _row_to_tuple(row):
    """MySQL-рядок nodes → tuple для bot_db.upsert_articles.
    Мовні версії РОЗДІЛЕНІ: content_ua → text_ua, content (рос.) → text_ru;
    так само title_ua / title (рос.). Нічого не змішуємо і не втрачаємо —
    матеріал може бути лише рос. (до 2023), лише укр., або мати обидві версії."""
    slug = (row.get("slug_ua") or row.get("slug") or "").strip() or None
    return (
        row["id"],
        row.get("published"),
        row.get("updated"),
        row.get("status"),
        row.get("own_material"),
        row.get("owner_id"),
        (row.get("title_ua") or "").strip() or None,
        (row.get("title") or "").strip() or None,
        slug,
        html_to_text(row.get("content_ua")),
        html_to_text(row.get("content")),
    )


def _change_marker(row):
    """"Момент останньої зміни" рядка nodes — max(updated, published)."""
    return max(row.get("updated") or 0, row.get("published") or 0)


# ---------- Первинний бекфіл ----------

async def run_backfill(limit=None, progress_cb=None):
    """Бекфіл дзеркала. Resumable: курсор backfill_last_id у sync_state.
    limit — скільки статей залити ЗА ЦЕЙ ЗАПУСК (для фазування; None — усі
    решта). Наступний виклик продовжить з того ж місця.
    progress_cb(done_total, last_id) — опційний async-колбек для прогресу.
    Повертає кількість залитих за цей запуск рядків."""
    if _backfill_running["flag"]:
        raise RuntimeError("Бекфіл уже запущено — другий паралельно не потрібен.")
    _backfill_running["flag"] = True
    try:
        await asyncio.to_thread(bot_db.ensure_schema)
        last_id = int(await asyncio.to_thread(bot_db.get_state, "backfill_last_id", "0"))
        done = 0
        reached_end = False
        while limit is None or done < limit:
            batch = BACKFILL_BATCH if limit is None else min(BACKFILL_BATCH, limit - done)
            rows = await db.aquery(
                f"SELECT {_NODE_COLUMNS} FROM nodes "
                "WHERE type = 'news' AND status = 1 AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_id, batch),
            )
            if not rows:
                reached_end = True  # джерело вичерпано — це справжнє завершення
                break
            tuples = await asyncio.to_thread(
                lambda: [_row_to_tuple(r) for r in rows]
            )
            await asyncio.to_thread(bot_db.upsert_articles, tuples)
            last_id = rows[-1]["id"]
            done += len(rows)
            await asyncio.to_thread(bot_db.set_state, "backfill_last_id", last_id)
            if progress_cb:
                await progress_cb(done, last_id)
            await asyncio.sleep(BACKFILL_PAUSE)
        # Позначку «завершено» і курсор інкременту ставимо ЛИШЕ коли справді
        # дійшли до кінця. При частковій порції (спрацював limit) — ні, інакше
        # інкремент вирішив би, що дзеркало повне, і решту архіву не долив би.
        if reached_end:
            now_ts = int(datetime.now().timestamp())
            await asyncio.to_thread(bot_db.set_state, "mirror_cursor", now_ts)
            await asyncio.to_thread(bot_db.set_state, "backfill_done_at", now_ts)
        return done, reached_end
    finally:
        _backfill_running["flag"] = False


# ---------- Тестовий зразок (перевірка чистки і розділення мов) ----------

SAMPLE_PER_EDGE = 3  # скільки найстаріших і скільки найновіших брати


async def load_sample():
    """Заливає в дзеркало кілька найстаріших (рос. епоха) і найновіших (укр.)
    опублікованих новин — щоб очима перевірити чистку HTML і що мовні версії
    розклались по правильних полях. Це реальні статті, лишаються в базі
    (при повному бекфілі просто перезапишуться). Повертає list[id]."""
    now_ts = int(datetime.now().timestamp())
    oldest = await db.aquery(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE type='news' AND status=1 "
        "AND published > 0 AND published <= %s ORDER BY published ASC LIMIT %s",
        (now_ts, SAMPLE_PER_EDGE),
    )
    newest = await db.aquery(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE type='news' AND status=1 "
        "AND published > 0 AND published <= %s ORDER BY published DESC LIMIT %s",
        (now_ts, SAMPLE_PER_EDGE),
    )
    rows = oldest + newest
    tuples = await asyncio.to_thread(lambda: [_row_to_tuple(r) for r in rows])
    await asyncio.to_thread(bot_db.upsert_articles, tuples)
    return [r["id"] for r in rows]


# ---------- Інкрементальний sync (scheduler, щогодини о :50) ----------

async def sync_incremental():
    """Дотягує в дзеркало створене/відредаговане з моменту курсора.
    Тихо виходить, якщо БД не налаштовані або бекфіл ще не завершено.
    Повертає кількість оновлених рядків (для діагностики)."""
    if not (bot_db.is_configured() and db.is_configured()):
        return 0
    if _backfill_running["flag"]:
        return 0  # бекфіл і так заливає все — не товчемось у ту саму базу
    cursor = await asyncio.to_thread(bot_db.get_state, "mirror_cursor")
    if cursor is None:
        return 0  # дзеркала ще немає — чекаємо /archive_backfill
    since = int(cursor) - CURSOR_OVERLAP_SEC
    # Без фільтра status: якщо матеріал зняли з публікації (status=0),
    # дзеркало має це віддзеркалити — пошук фільтрує status=1 сам.
    rows = await db.aquery(
        f"SELECT {_NODE_COLUMNS} FROM nodes "
        "WHERE type = 'news' AND GREATEST(COALESCE(updated,0), COALESCE(published,0)) >= %s "
        "ORDER BY GREATEST(COALESCE(updated,0), COALESCE(published,0)) ASC LIMIT %s",
        (since, INCREMENTAL_LIMIT),
    )
    if not rows:
        return 0
    tuples = await asyncio.to_thread(lambda: [_row_to_tuple(r) for r in rows])
    await asyncio.to_thread(bot_db.upsert_articles, tuples)
    new_cursor = max(_change_marker(r) for r in rows)
    if new_cursor > since:
        await asyncio.to_thread(bot_db.set_state, "mirror_cursor", new_cursor)
    return len(rows)


async def run_archive_sync(bot):
    """Обгортка для scheduler: помилка → алерт Олегу, як у решти моніторів."""
    try:
        await sync_incremental()
    except Exception as e:
        print(f"Помилка sync дзеркала архіву: {e}")
        await notify_error(bot, "sync дзеркала архіву", e)


# ---------- Команди ----------

def _fmt_ts(ts):
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts), KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    except (ValueError, OSError):
        return str(ts)


async def archive_backfill_handler(update, context):
    """/archive_backfill — разовий повний бекфіл дзеркала (resumable).
    Запускається у фоні, прогрес — редагуванням повідомлення."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована.\n"
            "На Railway: додати Postgres → зареференсити її URL у сервіс бота "
            "як BOT_DATABASE_URL (або DATABASE_URL)."
        )
        return
    if not db.is_configured():
        await update.message.reply_text("🦊 БД сайту не налаштована (DB_* env) — нема звідки лити.")
        return
    if _backfill_running["flag"]:
        await update.message.reply_text("🦊 Бекфіл уже йде — дивись прогрес у попередньому повідомленні.")
        return

    # Необов'язковий ліміт: /archive_backfill 5000 — залити 5000 за цей запуск
    # і зупинитись (фазування; наступний виклик продовжить з місця).
    limit = None
    if context.args:
        try:
            limit = max(1, int(context.args[0]))
        except ValueError:
            pass
    scope = f"порцію на {limit} статей" if limit else "весь архів (~1-2 год)"
    msg = await update.message.reply_text(f"🦊 Починаю заливати {scope}, resumable…")
    state = {"last_edit": 0.0}

    async def progress(done, last_id):
        # Не частіше ніж раз на ~20 сек, щоб не впертись у rate limit Telegram
        now = asyncio.get_event_loop().time()
        if now - state["last_edit"] < 20:
            return
        state["last_edit"] = now
        try:
            await msg.edit_text(f"🦊 Заливаю: {done} статей за цей запуск, дійшов до id {last_id}…")
        except Exception:
            pass

    async def task():
        try:
            done, reached_end = await run_backfill(limit=limit, progress_cb=progress)
            info = await asyncio.to_thread(bot_db.ping)
            if reached_end:
                tail = "Далі дзеркало оновлюється само щогодини о :50."
                head = f"✅ Бекфіл завершено: +{done} статей за цей запуск."
            else:
                tail = ("Порцію залито. Наступний /archive_backfill "
                        f"[N] продовжить з id {info['sync_state'].get('backfill_last_id', '?')}.")
                head = f"✅ Порцію залито: +{done} статей."
            await msg.edit_text(
                f"{head}\n"
                f"У дзеркалі всього: {info['articles']} статей "
                f"({_fmt_ts(info['oldest_published'])} — {_fmt_ts(info['newest_published'])}).\n"
                f"{tail}"
            )
        except Exception as e:
            try:
                await msg.edit_text(
                    f"❌ Бекфіл обірвався: {e}\n"
                    "Повторний /archive_backfill продовжить з місця зупинки (курсор збережено)."
                )
            except Exception:
                pass

    # У фон: команда відповідає одразу, заливка живе своїм життям.
    asyncio.create_task(task())


async def archive_sample_handler(update, context):
    """/archive_sample — залити кілька найстаріших і найновіших статей і показати,
    що осіло в базі (перевірка чистки HTML і розділення мовних версій)."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not (bot_db.is_configured() and db.is_configured()):
        await update.message.reply_text(
            "🦊 Потрібні обидві БД: BOT_DATABASE_URL (Postgres бота) і DB_* (БД сайту)."
        )
        return
    msg = await update.message.reply_text("🦊 Беру зразок статей з обох епох і чищу…")
    try:
        ids = await load_sample()
        if not ids:
            await msg.edit_text("🦊 Дивно — жодної опублікованої новини не знайшлось.")
            return
        rows = await asyncio.to_thread(
            bot_db.query,
            "SELECT id, published, title_ua, title_ru, slug, "
            "left(text_ua, 320) AS tua, left(text_ru, 320) AS tru, "
            "length(text_ua) AS lua, length(text_ru) AS lru "
            "FROM articles WHERE id = ANY(%s) ORDER BY published ASC",
            (ids,),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось узяти зразок: {e}")
        return

    parts = [f"🦊 Зразок дзеркала ({len(rows)} статей) — звір із сайтом:\n"]
    for r in rows:
        url = f"{BASE_URL}/news/{r['slug']}" if r.get("slug") else f"{BASE_URL}/news/{r['id']}"
        parts.append(f"— {_fmt_ts(r['published'])} · id {r['id']}\n{url}")
        if r.get("title_ua"):
            parts.append(f"  UA заголовок: {r['title_ua']}")
        if r.get("title_ru"):
            parts.append(f"  RU заголовок: {r['title_ru']}")
        if r.get("tua"):
            parts.append(f"  UA текст [{r['lua']} симв.]: {r['tua']}…")
        if r.get("tru"):
            parts.append(f"  RU текст [{r['lru']} симв.]: {r['tru']}…")
        if not r.get("tua") and not r.get("tru"):
            parts.append("  ⚠️ текст не витягся (обидві версії порожні)")
        parts.append("")
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n…(обрізано)"
    # Plain text: у превʼю сирий текст статей із будь-якими символами
    await msg.edit_text(text, disable_web_page_preview=True)


async def archive_status_handler(update, context):
    """/archive_status — стан дзеркала: скільки статей, межі, курсори."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text(
            "🦊 БД бота ще не налаштована (BOT_DATABASE_URL). "
            "Дзеркало архіву й /dossier поки недоступні."
        )
        return
    msg = await update.message.reply_text("🦊 Дивлюсь стан дзеркала…")
    try:
        info = await asyncio.to_thread(bot_db.ping)
    except Exception as e:
        await msg.edit_text(
            f"❌ Не вдалось під'єднатись до БД бота:\n<code>{escape_html(f'{type(e).__name__}: {e}')}</code>",
            parse_mode="HTML",
        )
        return
    sync = info["sync_state"]
    running = "так (зараз іде)" if _backfill_running["flag"] else "ні"
    lines = [
        "🦊 <b>Дзеркало архіву</b>",
        f"Postgres: <b>{escape_html(str(info['version']))}</b> ({info['elapsed_ms']} мс)",
        f"Статей у дзеркалі: <b>{info['articles']}</b>",
        f"Діапазон публікацій: {_fmt_ts(info['oldest_published'])} — {_fmt_ts(info['newest_published'])}",
        f"Бекфіл завершено: {_fmt_ts(sync.get('backfill_done_at'))}",
        f"Бекфіл дійшов до id: {sync.get('backfill_last_id', '—')} (зараз іде: {running})",
        f"Курсор інкременту: {_fmt_ts(sync.get('mirror_cursor'))}",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="HTML")
