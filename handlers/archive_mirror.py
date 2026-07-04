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

def html_to_text(html):
    """Чистий текст тіла матеріалу: без скриптів/стилів/підписів до фото,
    пробіли схлопнуті, кап TEXT_PLAIN_CAP (захист tsvector від переповнення)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "figure"]):
        tag.decompose()
    # Ті самі службові блоки, що ріже news_archive.extract_lead
    for tag in soup.find_all(class_=re.compile(r"imgbox|lightbox")):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:bot_db.TEXT_PLAIN_CAP] or None


def _row_to_tuple(row):
    """MySQL-рядок nodes → tuple для bot_db.upsert_articles.
    Текст: українська версія, фолбек — російська (старі матеріали)."""
    text = html_to_text(row.get("content_ua")) or html_to_text(row.get("content"))
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
        text,
    )


def _change_marker(row):
    """"Момент останньої зміни" рядка nodes — max(updated, published)."""
    return max(row.get("updated") or 0, row.get("published") or 0)


# ---------- Первинний бекфіл ----------

async def run_backfill(progress_cb=None):
    """Повний бекфіл дзеркала. Resumable: курсор backfill_last_id у sync_state.
    progress_cb(done_total, last_id) — опційний async-колбек для прогресу.
    Повертає кількість залитих за цей запуск рядків."""
    if _backfill_running["flag"]:
        raise RuntimeError("Бекфіл уже запущено — другий паралельно не потрібен.")
    _backfill_running["flag"] = True
    try:
        await asyncio.to_thread(bot_db.ensure_schema)
        last_id = int(await asyncio.to_thread(bot_db.get_state, "backfill_last_id", "0"))
        done = 0
        while True:
            rows = await db.aquery(
                f"SELECT {_NODE_COLUMNS} FROM nodes "
                "WHERE type = 'news' AND status = 1 AND id > %s "
                "ORDER BY id LIMIT %s",
                (last_id, BACKFILL_BATCH),
            )
            if not rows:
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
        # Бекфіл дійшов до кінця → ставимо курсор інкременту на "зараз",
        # далі дотягуватиме тільки нове/відредаговане.
        now_ts = int(datetime.now().timestamp())
        await asyncio.to_thread(bot_db.set_state, "mirror_cursor", now_ts)
        await asyncio.to_thread(bot_db.set_state, "backfill_done_at", now_ts)
        return done
    finally:
        _backfill_running["flag"] = False


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

    msg = await update.message.reply_text("🦊 Починаю заливати архів у дзеркало… (це на 1-2 години, resumable)")
    state = {"last_edit": 0.0}

    async def progress(done, last_id):
        # Не частіше ніж раз на ~20 сек, щоб не впертись у rate limit Telegram
        now = asyncio.get_event_loop().time()
        if now - state["last_edit"] < 20:
            return
        state["last_edit"] = now
        try:
            await msg.edit_text(f"🦊 Заливаю архів: {done} статей за цей запуск, дійшов до id {last_id}…")
        except Exception:
            pass

    async def task():
        try:
            done = await run_backfill(progress_cb=progress)
            info = await asyncio.to_thread(bot_db.ping)
            await msg.edit_text(
                f"✅ Бекфіл завершено: +{done} статей за цей запуск.\n"
                f"У дзеркалі всього: {info['articles']} статей "
                f"({_fmt_ts(info['oldest_published'])} — {_fmt_ts(info['newest_published'])}).\n"
                "Далі дзеркало оновлюється само щогодини о :50."
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
