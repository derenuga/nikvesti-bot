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
    "title_ua, title, slug_ua, slug, content_ua, content, category, region"
)

_backfill_running = {"flag": False}
_backfill_stop = {"flag": False}  # сигнал «зупинити поточний бекфіл» (/archive_stop)

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


# ---------- HTML → чистий текст ----------

# Службові блоки, які прибираємо з тіла (не текст статті): скрипти, стилі,
# фрейми, фото з підписами, галереї, рекламні й «читайте також»-врізки.
# Список свідомо консервативний — краще лишити трохи шуму, ніж вирізати абзац.
# Перевірено на реальному HTML сайту (id 80912 2016, id 262482 2023).
_JUNK_TAGS = ["script", "style", "iframe", "figure", "noscript", "form"]
# Фото-контейнер старого движка: <div id="imgbox-N" class="photo right"> з
# <span class="title"> підписом усередині. imgbox тут в ID (клас — 'photo'),
# тому ловимо і по id, і по класу; заодно ріжемо підписи caption/title
# (на випадок, якщо підпис поза imgbox) і сучасний lightbox-блок.
_JUNK_ID_RE = re.compile(r"imgbox", re.IGNORECASE)
_JUNK_CLASS_RE = re.compile(
    r"imgbox|lightbox|gallery|related|read-?also|readmore|banner|advert|"
    r"social|share|subscribe|caption|\btitle\b",
    re.IGNORECASE,
)


def html_to_text(html):
    """Чистий текст тіла матеріалу: без скриптів/стилів/фото/підписів/врізок,
    пробіли схлопнуті, кап TEXT_CAP (захист tsvector від переповнення)."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_JUNK_TAGS):
        tag.decompose()
    for tag in soup.find_all(id=_JUNK_ID_RE):   # фото-контейнер разом із підписом
        tag.decompose()
    for tag in soup.find_all(class_=_JUNK_CLASS_RE):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:bot_db.TEXT_CAP] or None


def _row_to_tuple(row, tags_text=None):
    """MySQL-рядок nodes → tuple для bot_db.upsert_articles, або None якщо
    статтю не беремо в корпус.
    Мовні версії РОЗДІЛЕНІ: content_ua → text_ua, content (рос.) → text_ru;
    так само title_ua / title (рос.). Нічого не змішуємо і не втрачаємо —
    матеріал може бути лише рос. (до 2023), лише укр., або мати обидві версії.
    Порожні заглушки (обидві текстові версії порожні — напр. технічні записи
    2001-го, самі заголовки без тіла) в корпус НЕ беремо: для пошуку/досьє вони
    марні. tags_text — назви тегів статті одним рядком для індексу."""
    text_ua = html_to_text(row.get("content_ua"))
    text_ru = html_to_text(row.get("content"))
    if not text_ua and not text_ru:
        return None
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
        text_ua,
        text_ru,
        (row.get("category") or "").strip() or None,
        row.get("region"),
        tags_text or None,
    )


def _change_marker(row):
    """"Момент останньої зміни" рядка nodes — max(updated, published)."""
    return max(row.get("updated") or 0, row.get("published") or 0)


# ---------- Теги (довідник tag + звʼязки node_tag) ----------
#
# Теги наслоювались із роками — у старих (2008-2009) матеріалів їх немає, це
# нормально. Довідник tag малий (тисячі рядків), тягнемо його раз на прогін.
# node_tag читаємо ПАЧКАМИ (на кожні BACKFILL_BATCH статей — один запит),
# а не по одній статті, інакше 320 тис. запитів вбили б бекфіл.

async def _refresh_tags():
    """Заливає канонічні теги в нору і повертає (resolve_map, names_map):
      resolve_map — будь-який tag_id → канонічний id (розмерджені зведені);
      names_map   — канонічний id → рядок назв (ua + ru) для tags_text.
    None, якщо довідник tag недоступний (тоді синк іде без тегів — не падає)."""
    try:
        rows = await db.aquery(
            "SELECT id, redirect_tag_id, name, name_ru, name_en, "
            "google_category, description FROM tag"
        )
    except Exception as e:
        print(f"archive: довідник tag недоступний ({e}) — синк без тегів")
        return None

    redirect = {r["id"]: r.get("redirect_tag_id") for r in rows}

    def canon(tid):
        # Зводимо ланцюг редиректів до канонічного тега (із запобіжником циклів)
        seen = set()
        while redirect.get(tid) and tid not in seen:
            seen.add(tid)
            tid = redirect[tid]
        return tid

    resolve_map = {r["id"]: canon(r["id"]) for r in rows}
    by_id = {r["id"]: r for r in rows}

    tag_rows, names_map = [], {}
    for cid in set(resolve_map.values()):
        r = by_id.get(cid)
        if not r:
            continue
        name_ua = (r.get("name") or "").strip() or None
        name_ru = (r.get("name_ru") or "").strip() or None
        name_en = (r.get("name_en") or "").strip() or None
        tag_rows.append((
            cid, name_ua, name_ru, name_en,
            (r.get("google_category") or "").strip() or None,
            (r.get("description") or "").strip() or None,
        ))
        # Локалізації ОДНОГО тегу для tags_text: якщо ua==ru ('ДТП'/'ДТП') —
        # один екземпляр, якщо різні ('Миколаїв'/'Николаев') — обидва. Тег як
        # сутність не чіпаємо (обидві назви є в tags), це лише рядок для індексу.
        names = list(dict.fromkeys(x for x in (name_ua, name_ru) if x))
        if names:
            names_map[cid] = " ".join(names)

    await asyncio.to_thread(bot_db.upsert_tags, tag_rows)
    return resolve_map, names_map


async def _fetch_node_tags(ids):
    """Рядки node_tag для пачки node_id (один запит). [] якщо порожньо/помилка."""
    if not ids:
        return []
    placeholders = ", ".join(["%s"] * len(ids))
    try:
        return await db.aquery(
            f"SELECT node_id, tag_id FROM node_tag WHERE node_id IN ({placeholders})",
            tuple(ids),
        )
    except Exception as e:
        print(f"archive: node_tag недоступний ({e}) — пачка без тегів")
        return []


def _build_batch(rows, node_tag_rows, resolve_map, names_map):
    """CPU-частина синку пачки (чистка HTML + збирання тегів) — у to_thread.
    Повертає (tuples для upsert_articles, pairs для article_tags)."""
    tags_by_node = {}
    for p in node_tag_rows:
        cid = resolve_map.get(p["tag_id"], p["tag_id"])
        if cid in names_map:
            tags_by_node.setdefault(p["node_id"], set()).add(cid)
    tuples, pairs = [], []
    for r in rows:
        cids = tags_by_node.get(r["id"], ())
        # Кожен тег дає свої (вже дедупнуті per-tag) локалізації; між різними
        # тегами не склеюємо — tsvector сам зберігає кожну лексему один раз.
        tags_text = " ".join(names_map[c] for c in cids) if cids else None
        t = _row_to_tuple(r, tags_text or None)
        if t is None:
            continue  # порожня заглушка — не в корпус (і теги для неї не пишемо)
        tuples.append(t)
        for c in cids:
            pairs.append((r["id"], c))
    return tuples, pairs


async def _sync_rows(rows, tags_ctx):
    """Заливає пачку nodes-рядків у нору: статті + (якщо теги доступні) звʼязки.
    Порожні заглушки пропускаються (_row_to_tuple → None). tags_ctx =
    (resolve_map, names_map) або None (синк без тегів). Повертає list залитих id."""
    ids = [r["id"] for r in rows]
    if tags_ctx:
        resolve_map, names_map = tags_ctx
        node_tag_rows = await _fetch_node_tags(ids)
        tuples, pairs = await asyncio.to_thread(
            _build_batch, rows, node_tag_rows, resolve_map, names_map
        )
        kept_ids = [t[0] for t in tuples]
        if tuples:
            await asyncio.to_thread(bot_db.upsert_articles, tuples)
        if kept_ids:
            await asyncio.to_thread(bot_db.replace_article_tags, kept_ids, pairs)
        return kept_ids
    else:
        tuples = await asyncio.to_thread(
            lambda: [t for t in (_row_to_tuple(r) for r in rows) if t is not None]
        )
        if tuples:
            await asyncio.to_thread(bot_db.upsert_articles, tuples)
        return [t[0] for t in tuples]


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
    _backfill_stop["flag"] = False  # новий запуск скидає попередній сигнал стопу
    try:
        await asyncio.to_thread(bot_db.ensure_schema)
        tags_ctx = await _refresh_tags()
        last_id = int(await asyncio.to_thread(bot_db.get_state, "backfill_last_id", "0"))
        done = 0
        reached_end = False
        while limit is None or done < limit:
            if _backfill_stop["flag"]:
                break  # зупинено вручну; reached_end=False → «завершено» не ставимо, курсор збережено
            batch = BACKFILL_BATCH if limit is None else min(BACKFILL_BATCH, limit - done)
            now_ts = int(datetime.now().timestamp())
            # Тільки реально опубліковані: status=1, published — валідний unix-час
            # у минулому. Чернетки (status=0), недатовані й відкладені (published
            # у майбутньому) в нору не беремо (за рішенням Олега, 04.07).
            rows = await db.aquery(
                f"SELECT {_NODE_COLUMNS} FROM nodes "
                "WHERE type = 'news' AND status = 1 AND published > 0 AND published <= %s "
                "AND id > %s ORDER BY id LIMIT %s",
                (now_ts, last_id, batch),
            )
            if not rows:
                reached_end = True  # джерело вичерпано — це справжнє завершення
                break
            await _sync_rows(rows, tags_ctx)
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
#
# Беремо статті з РІЗНИХ ЕПОХ, а не тільки з країв: найстаріші — часто порожні
# заглушки 2001-го, найновіші — сучасний укр. HTML. Найголовніше перевірити
# середину (2008-2015, російськомовний старий HTML) — саме там ризик чистки.

SAMPLE_YEARS = [2008, 2012, 2016, 2020, 2023]  # «зонди» по роках + oldest/newest


async def load_sample():
    """Заливає в нору по 1-2 опублікованих новини з кількох епох (найстаріші,
    по роках-зондах, найновіші) — щоб очима перевірити чистку HTML і розділення
    мовних версій на різному історичному HTML. Реальні статті, лишаються в базі
    (повний бекфіл їх перезапише). Повертає list[id]."""
    now_ts = int(datetime.now().timestamp())
    seen = {}

    async def grab(extra_where, params, order, limit):
        rows = await db.aquery(
            f"SELECT {_NODE_COLUMNS} FROM nodes WHERE type='news' AND status=1 "
            f"AND published > 0 AND published <= %s {extra_where} "
            f"ORDER BY published {order} LIMIT %s",
            params,
        )
        for r in rows:
            seen.setdefault(r["id"], r)

    await grab("", (now_ts, 2), "ASC", 2)  # найстаріші
    for y in SAMPLE_YEARS:
        floor = int(datetime(y, 1, 1).timestamp())
        if floor < now_ts:
            await grab("AND published >= %s", (now_ts, floor, 1), "ASC", 1)  # перша у році y
    await grab("", (now_ts, 2), "DESC", 2)  # найновіші

    rows = list(seen.values())
    rows.sort(key=lambda r: r.get("published") or 0)
    tags_ctx = await _refresh_tags()
    return await _sync_rows(rows, tags_ctx)  # лише реально залиті (без порожніх)


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
    # Тягнемо змінене БЕЗ фільтра status — щоб побачити й зняті з публікації
    # (status 1→0) та відкладені, і прибрати їх з нори.
    rows = await db.aquery(
        f"SELECT {_NODE_COLUMNS} FROM nodes "
        "WHERE type = 'news' AND GREATEST(COALESCE(updated,0), COALESCE(published,0)) >= %s "
        "ORDER BY GREATEST(COALESCE(updated,0), COALESCE(published,0)) ASC LIMIT %s",
        (since, INCREMENTAL_LIMIT),
    )
    if not rows:
        return 0
    now_ts = int(datetime.now().timestamp())
    # «Живі» = ті, що мають бути в норі: опубліковані, не майбутні. Решта
    # (чернетки, зняті, відкладені) — видаляємо з нори, якщо там були.
    live = [r for r in rows
            if r.get("status") == 1 and (r.get("published") or 0) > 0
            and (r.get("published") or 0) <= now_ts]
    live_ids = {r["id"] for r in live}
    dead_ids = [r["id"] for r in rows if r["id"] not in live_ids]

    tags_ctx = await _refresh_tags()
    if live:
        await _sync_rows(live, tags_ctx)
    if dead_ids:
        await asyncio.to_thread(bot_db.delete_articles, dead_ids)

    # Курсор клампуємо на «зараз»: відкладений пост має published у майбутньому,
    # і без клампа курсор стрибнув би туди, пропустивши все до того часу.
    # Такі пости лишаються в вікні (їх щоразу перечитуємо кілька штук — дешево),
    # доки не стануть опублікованими й не потраплять у live.
    new_cursor = min(max(_change_marker(r) for r in rows), now_ts)
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
            elif _backfill_stop["flag"]:
                tail = ("Наступний /archive_backfill [N] продовжить з id "
                        f"{info['sync_state'].get('backfill_last_id', '?')}.")
                head = f"⏹ Зупинено вручну: +{done} статей за цей запуск."
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


async def archive_stop_handler(update, context):
    """/archive_stop — м'яко зупинити поточний бекфіл (після поточної пачки).
    Resumable: повторний /archive_backfill продовжить з місця зупинки."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not _backfill_running["flag"]:
        await update.message.reply_text("🦊 Зараз бекфіл не йде — зупиняти нічого.")
        return
    _backfill_stop["flag"] = True
    await update.message.reply_text(
        "🦊 Зупиняю бекфіл після поточної пачки (кілька секунд).\n"
        "Курсор збережено — /archive_backfill [N] продовжить з місця зупинки."
    )


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
            "SELECT id, published, title_ua, title_ru, slug, category, region, "
            "own_material, tags_text, "
            "left(text_ua, 300) AS tua, left(text_ru, 300) AS tru, "
            "length(text_ua) AS lua, length(text_ru) AS lru "
            "FROM articles WHERE id = ANY(%s) ORDER BY published ASC",
            (ids,),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось узяти зразок: {e}")
        return

    parts = [f"🦊 Зразок нори ({len(rows)} статей) — звір із сайтом:\n"]
    for r in rows:
        url = f"{BASE_URL}/news/{r['slug']}" if r.get("slug") else f"{BASE_URL}/news/{r['id']}"
        own = "власний" if r.get("own_material") else "рерайт/агентське"
        meta = f"рубрика={r.get('category') or '—'} · регіон={r.get('region') if r.get('region') is not None else '—'} · {own}"
        parts.append(f"— {_fmt_ts(r['published'])} · id {r['id']}\n{url}\n  [{meta}]")
        if r.get("title_ua"):
            parts.append(f"  UA заголовок: {r['title_ua']}")
        if r.get("title_ru"):
            parts.append(f"  RU заголовок: {r['title_ru']}")
        parts.append(f"  теги: {r.get('tags_text') or '— (не було)'}")
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
        "🦊 <b>Лисяча нора</b> (корпус архіву)",
        f"Postgres: <b>{escape_html(str(info['version']))}</b> ({info['elapsed_ms']} мс)",
        f"Статей у норі: <b>{info['articles']}</b> (з тегами: {info.get('tagged_articles', 0)})",
        f"Довідник тегів: <b>{info.get('tags', 0)}</b>",
        f"Діапазон публікацій: {_fmt_ts(info['oldest_published'])} — {_fmt_ts(info['newest_published'])}",
        f"Бекфіл завершено: {_fmt_ts(sync.get('backfill_done_at'))}",
        f"Бекфіл дійшов до id: {sync.get('backfill_last_id', '—')} (зараз іде: {running})",
        f"Курсор інкременту: {_fmt_ts(sync.get('mirror_cursor'))}",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="HTML")


# ---------- Нагляд: /archive_report і /nora_sql ----------

def _fmt_k(n):
    """Компактно: 1234 → '1.2к', 980 → '980'."""
    n = int(n or 0)
    return f"{n/1000:.1f}к".replace(".0к", "к") if n >= 1000 else str(n)


def _build_report():
    """Кураторне здоровкове зведення нори — синхронне (у to_thread)."""
    q = bot_db.query
    base = q(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE text_ua IS NOT NULL AND text_ru IS NOT NULL) AS both_lang, "
        "count(*) FILTER (WHERE text_ua IS NOT NULL AND text_ru IS NULL) AS ua_only, "
        "count(*) FILTER (WHERE text_ua IS NULL AND text_ru IS NOT NULL) AS ru_only, "
        "count(*) FILTER (WHERE own_material = 1) AS own, "
        "count(*) FILTER (WHERE title_ua IS NULL AND title_ru IS NULL) AS no_title, "
        "count(*) FILTER (WHERE length(coalesce(text_ua, text_ru)) < 100) AS very_short, "
        "round(avg(length(coalesce(text_ua, text_ru)))) AS avg_len, "
        "max(length(coalesce(text_ua, text_ru))) AS max_len "
        "FROM articles"
    )[0]
    total = base["total"] or 0
    if not total:
        return "🦊 Нора порожня — спершу /archive_backfill."
    tagged = q("SELECT count(DISTINCT article_id) AS c FROM article_tags")[0]["c"]
    by_year = q(
        "SELECT EXTRACT(YEAR FROM to_timestamp(published))::int AS yr, count(*) AS c, "
        "round(avg(length(coalesce(text_ua, text_ru)))) AS al "
        "FROM articles WHERE published > 0 GROUP BY yr ORDER BY yr"
    )
    cats = q(
        "SELECT coalesce(category, '—') AS category, count(*) AS c "
        "FROM articles GROUP BY category ORDER BY c DESC LIMIT 12"
    )
    regions = q(
        "SELECT coalesce(region::text, '—') AS region, count(*) AS c "
        "FROM articles GROUP BY region ORDER BY c DESC LIMIT 8"
    )

    def pct(n):
        return f"{100 * (n or 0) / total:.0f}%"

    lines = [
        f"🦊 <b>Звіт по норі</b> — {total} статей\n",
        "<b>Мови:</b> "
        f"обидві {base['both_lang']} ({pct(base['both_lang'])}) · "
        f"лише укр {base['ua_only']} · лише рос {base['ru_only']}",
        f"<b>Теги:</b> {tagged} статей ({pct(tagged)}) мають теги",
        f"<b>Власні (own_material=1):</b> {base['own']} ({pct(base['own'])})",
        f"<b>Текст:</b> середній {int(base['avg_len'] or 0)} симв., макс {int(base['max_len'] or 0)}",
    ]
    # Сигнали ризику
    warns = []
    if base["no_title"]:
        warns.append(f"без заголовка: {base['no_title']}")
    if base["very_short"]:
        warns.append(f"дуже короткий текст (&lt;100): {base['very_short']}")
    if warns:
        lines.append("⚠️ <b>Увага:</b> " + " · ".join(warns))

    # По роках: кількість + середня довжина (детектор проблем чистки)
    yr_parts = [f"{r['yr']}: {r['c']} (~{_fmt_k(r['al'])})" for r in by_year]
    lines.append("\n<b>По роках</b> (к-сть, ~середня довжина тексту):\n"
                 + escape_html(" · ".join(yr_parts)))

    cat_parts = [f"{escape_html(r['category'])}: {r['c']}" for r in cats]
    lines.append("\n<b>Рубрики:</b> " + " · ".join(cat_parts))

    reg_parts = [f"{escape_html(r['region'])}: {r['c']}" for r in regions]
    lines.append("<b>Регіони:</b> " + " · ".join(reg_parts))

    return "\n".join(lines)


async def archive_report_handler(update, context):
    """/archive_report — здоровкове зведення нори для нагляду за бекфілом:
    розподіл по роках (рівномірність), мови, теги, рубрики, регіони,
    середня довжина тексту по роках (детектор проблем чистки)."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 БД бота не налаштована (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Рахую зведення по норі…")
    try:
        report = await asyncio.to_thread(_build_report)
    except Exception as e:
        await msg.edit_text(
            f"❌ <code>{escape_html(f'{type(e).__name__}: {e}')}</code>", parse_mode="HTML"
        )
        return
    if len(report) > 4000:
        report = report[:4000].rsplit("\n", 1)[0] + "\n…(обрізано)"
    await msg.edit_text(report, parse_mode="HTML", disable_web_page_preview=True)


# read-only guard: тільки читальні запити (нора — БД бота, писати теоретично
# можна, тому захист тут, а не лише в bot_db). Prefix + заборона DML-ключів
# (щоб не пройшло WITH ... DELETE).
_NORA_READ_PREFIXES = ("select", "with", "explain", "show", "table")
_NORA_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|comment|copy)\b",
    re.IGNORECASE,
)


def _fmt_sql_rows(rows, max_rows=40, max_chars=3500):
    if not rows:
        return "(0 рядків)"
    out = []
    for row in rows[:max_rows]:
        out.append(" | ".join(f"{k}={v}" for k, v in row.items()))
    text = "\n".join(out)
    if len(rows) > max_rows:
        text += f"\n… (+{len(rows) - max_rows} рядків)"
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


async def nora_sql_handler(update, context):
    """/nora_sql <SELECT…> — read-only запит до нори (Postgres бота).
    Тільки для редакції. Для ad-hoc нагляду: складаю запит — ти виконуєш —
    я читаю вивід. Той самий підхід, що /dbquery для БД сайту."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 БД бота не налаштована (BOT_DATABASE_URL).")
        return
    sql = update.message.text.partition(" ")[2].strip()
    if not sql:
        await update.message.reply_text(
            "Використання: /nora_sql <SELECT…>\n"
            "Напр.: /nora_sql SELECT count(*) FROM articles\n"
            "/nora_sql SELECT id, title_ua FROM articles ORDER BY published DESC LIMIT 5"
        )
        return
    low = sql.lstrip().lower()
    if not low.startswith(_NORA_READ_PREFIXES) or _NORA_FORBIDDEN_RE.search(sql):
        await update.message.reply_text("⛔ Дозволені тільки читальні запити (SELECT/WITH/EXPLAIN…).")
        return
    msg = await update.message.reply_text("🦊 Виконую…")
    try:
        rows = await bot_db.aquery(sql)
    except Exception as e:
        await msg.edit_text(
            f"❌ <code>{escape_html(f'{type(e).__name__}: {e}')}</code>", parse_mode="HTML"
        )
        return
    body = _fmt_sql_rows(rows)
    await msg.edit_text(
        f"<b>{len(rows)} рядк(ів):</b>\n<pre>{escape_html(body)}</pre>", parse_mode="HTML"
    )
