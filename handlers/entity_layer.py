"""
Сутнісний шар нори — керування бэкфілом через Batch API з бота (крок C, шлях А).

Оркеструє entity_backfill_api (розбивка на батчі, відправка, полінг) і
entity_pipeline.write_results (злиття в entities/article_entities) прямо на
Railway: у бота вже є ANTHROPIC_API_KEY і внутрішній доступ до нори
(BOT_DATABASE_URL), тож зовнішній термінал не потрібен.

Команди (whitelist ALLOWED_USER_IDS):
    /entity_estimate [з] [по]  — к-сть статей у діапазоні + оцінка вартості
                                 (read-only, грошей не витрачає; дефолт
                                 2022-01-01..2027-01-01)
    /entity_backfill <з> <по>  — ПЛАТНО: відправити діапазон у Batch API
                                 (Haiku 4.5, −50%), полінг у фоні, по
                                 завершенню — злиття в нору і звіт у чат
    /entity_status             — сутності/зв'язки по kind + стан батчів
    /entity_resume             — переприв'язати полінг після редеплою
                                 (стан батчів живе в sync_state нори)

Патерн — як /archive_backfill: фонова задача asyncio, стан у БД (переживає
редеплой), збої — у notify_error. Вартість акумулюється в storage.record_ai_usage
(/aicost її бачить). Злиття ідемпотентне — повторний ingest безпечний.
"""

import asyncio
import io
import json
import os
import time

from telegram.constants import ParseMode

from handlers import bot_db
from handlers.notifier import notify_error
from handlers.storage import record_ai_usage

import entity_pipeline as ep
import entity_backfill_api as api

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# Ключ стану в sync_state нори: JSON {"batch_ids": [...], "done": [...],
# "from": ..., "to": ..., "chat_id": ..., "started": unix}. Наявність ключа =
# є незавершений прогін (полінг активний або втрачений після редеплою).
STATE_KEY = "entity_api_run"

POLL_INTERVAL = 60  # сек між перевірками статусу батчів

_poll_running = {"flag": False}


def _allowed(update):
    return not _ALLOWED_USER_IDS or update.effective_user.id in _ALLOWED_USER_IDS


def _load_state():
    raw = bot_db.get_state(STATE_KEY)
    return json.loads(raw) if raw else None


def _save_state(state):
    bot_db.set_state(STATE_KEY, json.dumps(state, ensure_ascii=False))


def _clear_state():
    bot_db.execute("DELETE FROM sync_state WHERE key = %s", (STATE_KEY,))


# ---------- /entity_estimate ----------

async def entity_estimate_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    from_date = args[0] if len(args) > 0 else "2022-01-01"
    to_date = args[1] if len(args) > 1 else "2027-01-01"
    msg = await update.message.reply_text("🦊 Рахую діапазон (read-only)…")
    try:
        arts = await asyncio.to_thread(api.fetch_range, from_date, to_date)
        n = len(arts)
        est_in = n * api.EST_IN_PER_ART
        est_out = n * api.EST_OUT_PER_ART
        cost = est_in * api.PRICE_IN + est_out * api.PRICE_OUT
        sys_len = len(api.get_system_prompt().encode("utf-8"))
        n_chunks = len(api.chunk_articles(arts, sys_len)) if arts else 0
        await msg.edit_text(
            f"🦊 Оцінка бэкфіла {from_date}…{to_date}\n\n"
            f"Статей: {n}\n"
            f"Токени: ~{est_in/1e6:.0f}M вх + ~{est_out/1e6:.0f}M вих\n"
            f"Вартість (Haiku 4.5, батч −50%): ≈ ${cost:.0f}\n"
            f"(без урахування prompt-cache — по факту дешевше)\n"
            f"Батчів: {n_chunks}\n\n"
            f"Запуск (платно): /entity_backfill {from_date} {to_date}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Помилка оцінки: {e}")


# ---------- /entity_backfill ----------

async def entity_backfill_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /entity_backfill 2022-01-01 2027-01-01\n"
            "Спершу оцінка (безкоштовно): /entity_estimate"
        )
        return
    if _load_state():
        await update.message.reply_text(
            "Уже є незавершений прогін — /entity_status. "
            "Після редеплою полінг переприв'язується через /entity_resume."
        )
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        await update.message.reply_text("ANTHROPIC_API_KEY не заданий.")
        return
    from_date, to_date = args[0], args[1]
    msg = await update.message.reply_text("🦊 Вивантажую статті й відправляю батчі…")

    # Стан зберігається ДО відправки і після КОЖНОГО батчу — обрив/редеплой
    # посеред відправки не губить уже створені батчі (/entity_resume або
    # /entity_recover їх підхоплять).
    state = {"batch_ids": [], "done": [], "from": from_date, "to": to_date,
             "chat_id": update.effective_chat.id, "started": int(time.time()),
             "articles": 0, "submitting": True}

    def submit():
        import anthropic
        client = anthropic.Anthropic()
        arts = api.fetch_range(from_date, to_date)
        if not arts:
            return 0
        state["articles"] = len(arts)
        _save_state(state)
        sys_len = len(api.get_system_prompt().encode("utf-8"))
        chunks = api.chunk_articles(arts, sys_len)
        for chunk in chunks:
            requests = [api._make_request(client, a) for a in chunk]
            batch = client.messages.batches.create(requests=requests)
            state["batch_ids"].append(batch.id)
            _save_state(state)
        state["submitting"] = False
        _save_state(state)
        return len(arts)

    try:
        n_arts = await asyncio.to_thread(submit)
    except Exception as e:
        if state["batch_ids"]:
            await msg.edit_text(
                f"❌ Збій посеред відправки: {e}\n"
                f"Уже створено {len(state['batch_ids'])} батч(ів) — вони не "
                f"загубляться, /entity_resume доведе їх до кінця; решту статей "
                f"потім доганяємо окремим діапазоном.")
            asyncio.create_task(_poll_task(context.bot))
        else:
            await asyncio.to_thread(_clear_state)
            await msg.edit_text(f"❌ Не вдалося відправити батчі: {e}")
        return
    if not n_arts:
        await asyncio.to_thread(_clear_state)
        await msg.edit_text("У діапазоні немає статей.")
        return

    await msg.edit_text(
        f"🦊 Відправлено {n_arts} статей у {len(state['batch_ids'])} батч(ів).\n"
        f"Полю щохвилини; зазвичай хвилини–година. Стан: /entity_status\n"
        f"Якщо бот редеплоїться — /entity_resume переприв'яже полінг."
    )
    asyncio.create_task(_poll_task(context.bot))


# ---------- полінг + ingest ----------

def _batch_counts(client, batch_ids):
    """Агрегувати живий прогрес по всіх батчах прогону (request_counts).
    Повертає (totals, ended_ids)."""
    tot = {"processing": 0, "succeeded": 0, "errored": 0,
           "canceled": 0, "expired": 0}
    ended = []
    for bid in batch_ids:
        b = client.messages.batches.retrieve(bid)
        rc = b.request_counts
        tot["processing"] += rc.processing
        tot["succeeded"] += rc.succeeded
        tot["errored"] += rc.errored
        tot["canceled"] += rc.canceled
        tot["expired"] += rc.expired
        if b.processing_status == "ended":
            ended.append(bid)
    return tot, ended


def _progress_text(state, tot):
    total = state.get("articles") or (sum(tot.values()) or 1)
    done_reqs = tot["succeeded"] + tot["errored"] + tot["canceled"] + tot["expired"]
    pct = 100 * done_reqs // max(total, 1)
    line = (f"🦊 Витяг сутностей {state['from']}…{state['to']}\n"
            f"Оброблено: {done_reqs} / {total} ({pct}%)\n"
            f"Батчів готово: {len(state['done'])}/{len(state['batch_ids'])}")
    if tot["errored"]:
        line += f"\nПомилок: {tot['errored']}"
    return line


_last_progress = {"text": None}


async def _update_progress(bot, state, tot):
    """Тримаємо в чаті одне повідомлення-прогрес і редагуємо його щополінгу."""
    text = _progress_text(state, tot)
    if text == _last_progress["text"]:
        return
    _last_progress["text"] = text
    try:
        if state.get("progress_msg_id"):
            await bot.edit_message_text(
                text, chat_id=state["chat_id"],
                message_id=state["progress_msg_id"])
        else:
            msg = await bot.send_message(state["chat_id"], text)
            state["progress_msg_id"] = msg.message_id
            await asyncio.to_thread(_save_state, state)
    except Exception:
        pass  # "message is not modified" тощо — прогрес не критичний


async def _poll_task(bot):
    if _poll_running["flag"]:
        return
    _poll_running["flag"] = True
    try:
        import anthropic
        client = anthropic.Anthropic()
        while True:
            state = await asyncio.to_thread(_load_state)
            if not state:
                return  # прогін завершено/скасовано
            tot, ended = await asyncio.to_thread(
                _batch_counts, client, state["batch_ids"])
            if set(ended) != set(state["done"]):
                state["done"] = ended
                await asyncio.to_thread(_save_state, state)
            await _update_progress(bot, state, tot)
            if len(ended) == len(state["batch_ids"]):
                break
            await asyncio.sleep(POLL_INTERVAL)
        await _ingest(bot, state, client)
    except Exception as e:
        await notify_error(bot, "entity_backfill (полінг батчів)", e)
    finally:
        _poll_running["flag"] = False


async def _ingest(bot, state, client):
    chat_id = state["chat_id"]
    await bot.send_message(chat_id, "🦊 Батчі готові — збираю результати і зливаю в нору…")

    def collect_and_write():
        results, n_err, err_samples = [], 0, []
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

        def note_err(custom_id, what):
            nonlocal n_err
            n_err += 1
            if len(err_samples) < 3:
                err_samples.append(f"{custom_id}: {what}"[:250])

        for bid in state["batch_ids"]:
            for res in client.messages.batches.results(bid):
                if res.result.type != "succeeded":
                    detail = getattr(res.result, "error", None)
                    note_err(res.custom_id, f"{res.result.type} {detail or ''}")
                    continue
                msg = res.result.message
                u = msg.usage
                usage["input"] += u.input_tokens or 0
                usage["output"] += u.output_tokens or 0
                usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                usage["cache_creation"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                text = next((bl.text for bl in msg.content if bl.type == "text"), None)
                if not text:
                    note_err(res.custom_id, f"порожній content (stop={msg.stop_reason})")
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    note_err(res.custom_id, f"непарсибельний JSON: {text[:120]}")
                    continue
                results.append({"article_id": int(res.custom_id),
                                "entities": obj.get("entities", [])})
        stats = ep.write_results(results)  # діапазонний бэкфіл — курсор не рухаємо
        return stats, n_err, usage, len(results), err_samples

    try:
        stats, n_err, usage, n_ok, err_samples = await asyncio.to_thread(collect_and_write)
        # облік вартості (/aicost): один агрегований запис на прогін
        record_ai_usage(api.MODEL, input_tokens=usage["input"],
                        output_tokens=usage["output"],
                        cache_read=usage["cache_read"],
                        cache_creation=usage["cache_creation"])
        await asyncio.to_thread(_clear_state)
        cost = (usage["input"] * api.PRICE_IN + usage["output"] * api.PRICE_OUT
                + usage["cache_read"] * api.PRICE_IN * 0.1
                + usage["cache_creation"] * api.PRICE_IN * 1.25)
        text = (
            f"🦊 Бэкфіл {state['from']}…{state['to']} завершено!\n\n"
            f"Статей оброблено: {stats['articles']} (помилок: {n_err})\n"
            f"Зв'язків: {stats['links']}\n"
            f"Нових сутностей: {stats['new_entities']}\n"
            f"Токени: {usage['input']/1e6:.1f}M вх / {usage['output']/1e6:.1f}M вих "
            f"(+{usage['cache_read']/1e6:.1f}M кеш)\n"
            f"Вартість ≈ ${cost:.2f}\n\n"
            f"Зведення: /entity_status"
        )
        if err_samples:
            text += "\n\nПриклади помилок:\n" + "\n".join(err_samples)
        await bot.send_message(chat_id, text)
    except Exception as e:
        # стан НЕ чистимо — /entity_resume дасть повторити ingest (ідемпотентно)
        await notify_error(bot, "entity_backfill (ingest у нору)", e)
        await bot.send_message(
            chat_id,
            f"❌ Збій зливання результатів: {e}\n"
            f"Батчі готові й нікуди не дінуться (29 днів) — /entity_resume повторить.",
        )


# ---------- авто-інкремент нових статей (щогодини :55) ----------

INCR_CURSOR_KEY = "entity_incr_last_id"
INCR_MAX_PER_RUN = 120   # захист: за один прогін не більше (наздоганяння дірок частинами)


def _extract_one(client, art):
    """Витяг сутностей однієї статті звичайним API (не батч): нових статей
    ~40/день, тому latency не критична, а батч-морока зайва. Системний промпт
    кешується — у межах одного прогону всі запити після першого читають кеш."""
    resp = client.messages.create(
        model=api.MODEL,
        max_tokens=api.MAX_TOKENS,
        system=[{"type": "text", "text": api.get_system_prompt(),
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": json.dumps(art, ensure_ascii=False)}],
        output_config={"format": {"type": "json_schema", "schema": api.ARTICLE_OUT}},
    )
    text = next((bl.text for bl in resp.content if bl.type == "text"), None)
    obj = json.loads(text)
    return obj.get("entities", []), resp.usage


async def sync_entities_incremental(bot):
    """Щогодини :55 (після синку дзеркала о :50): витяг сутностей із нових
    статей нори. Тихий; опт-ін — працює лише коли курсор увімкнено
    (/entity_increment_on). Вартість ~$0.006/статтю → ~$0.25/день."""
    cursor_raw = await asyncio.to_thread(bot_db.get_state, INCR_CURSOR_KEY)
    if cursor_raw is None:
        return  # інкремент вимкнено
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        return
    if await asyncio.to_thread(_load_state):
        return  # іде бэкфіл — не конкуруємо, наздоженемо наступної години
    try:
        rows = await bot_db.aquery(
            "SELECT id, title_ua, title_ru, text_ua, text_ru FROM articles "
            "WHERE id > %s ORDER BY id ASC LIMIT %s",
            (int(cursor_raw), INCR_MAX_PER_RUN))
        if not rows:
            return

        def extract_all():
            import anthropic
            client = anthropic.Anthropic()
            results, n_err = [], 0
            usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
            for r in rows:
                text_ua = (r["text_ua"] or "")[:ep.TEXT_CAP] or None
                text_ru = (r["text_ru"] or "")[:ep.TEXT_CAP] or None
                if text_ua and text_ru:
                    text_ru = None  # одна мова, як у бэкфілі
                art = {"id": r["id"], "title_ua": r["title_ua"],
                       "title_ru": r["title_ru"],
                       "text_ua": text_ua, "text_ru": text_ru}
                try:
                    entities, u = _extract_one(client, art)
                    results.append({"article_id": r["id"], "entities": entities})
                    usage["input"] += u.input_tokens or 0
                    usage["output"] += u.output_tokens or 0
                    usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                    usage["cache_creation"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                except Exception:
                    n_err += 1
            return results, n_err, usage

        results, n_err, usage = await asyncio.to_thread(extract_all)
        if n_err > len(rows) / 2:
            # масовий збій (API лежить?) — курсор НЕ рухаємо, наступна година повторить
            raise RuntimeError(f"інкремент сутностей: {n_err}/{len(rows)} збоїв витягу")
        if results:
            await asyncio.to_thread(ep.write_results, results)
            record_ai_usage(api.MODEL, input_tokens=usage["input"],
                            output_tokens=usage["output"],
                            cache_read=usage["cache_read"],
                            cache_creation=usage["cache_creation"])
        # окремі збої (не масові) пропускаємо: курсор рухаємо, стаття залишиться
        # без сутностей — краще, ніж вічний ретрай однієї битої статті
        await asyncio.to_thread(bot_db.set_state, INCR_CURSOR_KEY, rows[-1]["id"])
    except Exception as e:
        await notify_error(bot, "інкремент сутнісного шару", e)


async def entity_increment_on_handler(update, context):
    """Увімкнути авто-інкремент: курсор стає на поточний max(id) нори —
    обробляються лише статті, що з'являться ПІСЛЯ увімкнення (минуле — бэкфілом)."""
    if not _allowed(update):
        return
    existing = await asyncio.to_thread(bot_db.get_state, INCR_CURSOR_KEY)
    if existing is not None:
        await update.message.reply_text(
            f"Інкремент уже увімкнено (курсор id={existing}). Вимкнути: /entity_increment_off")
        return
    rows = await bot_db.aquery("SELECT max(id) AS m FROM articles")
    max_id = rows[0]["m"] or 0
    await asyncio.to_thread(bot_db.set_state, INCR_CURSOR_KEY, max_id)
    await update.message.reply_text(
        f"🦊 Авто-інкремент сутностей увімкнено (з id={max_id}).\n"
        f"Щогодини о :55 нові статті проходитимуть витяг (~$0.25/день, "
        f"видно в /aicost). Вимкнути: /entity_increment_off")


async def entity_increment_off_handler(update, context):
    if not _allowed(update):
        return
    if await asyncio.to_thread(bot_db.get_state, INCR_CURSOR_KEY) is None:
        await update.message.reply_text("Інкремент і так вимкнено.")
        return
    await asyncio.to_thread(
        bot_db.execute, "DELETE FROM sync_state WHERE key = %s", (INCR_CURSOR_KEY,))
    await update.message.reply_text("Авто-інкремент сутностей вимкнено.")


# ---------- /entity_recover ----------

async def entity_recover_handler(update, context):
    """Відновлення прогону, коли стан у sync_state втрачено (напр. збій між
    відправкою батчів і збереженням стану): перелічує батчі на боці Anthropic
    (вони живуть 29 днів), пересобирає стан і запускає полінг/ingest заново.
    Ingest ідемпотентний — повторна заливка тих самих результатів безпечна."""
    if not _allowed(update):
        return
    if await asyncio.to_thread(_load_state):
        await update.message.reply_text(
            "Стан прогону є — треба /entity_resume, не recover.")
        return
    msg = await update.message.reply_text("🦊 Шукаю батчі на боці Anthropic…")

    def list_recent():
        import anthropic
        client = anthropic.Anthropic()
        found = []
        cutoff = time.time() - 7 * 86400  # не старші тижня
        for b in client.messages.batches.list(limit=20):
            created = b.created_at.timestamp() if b.created_at else 0
            if created < cutoff or b.processing_status == "canceling":
                continue
            rc = b.request_counts
            n_req = rc.processing + rc.succeeded + rc.errored + rc.canceled + rc.expired
            found.append({"id": b.id, "status": b.processing_status, "n": n_req})
        return found

    try:
        found = await asyncio.to_thread(list_recent)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалося перелічити батчі: {e}")
        return
    if not found:
        await msg.edit_text(
            "На боці Anthropic батчів за останній тиждень немає — отже, "
            "відправка не відбулась і гроші не витрачались. "
            "Можна запускати /entity_backfill заново.")
        return

    state = {"batch_ids": [b["id"] for b in found], "done": [],
             "from": "recover", "to": "recover",
             "chat_id": update.effective_chat.id, "started": int(time.time()),
             "articles": sum(b["n"] for b in found)}
    await asyncio.to_thread(_save_state, state)
    lines = [f"🦊 Знайдено {len(found)} батч(ів), стан відновлено:"]
    for b in found:
        lines.append(f"  {b['id']}: {b['status']}, {b['n']} запитів")
    lines.append("Полю далі; по готовності заллю в нору і відзвітую.")
    await msg.edit_text("\n".join(lines))
    asyncio.create_task(_poll_task(context.bot))


# ---------- /entity_resume ----------

async def entity_resume_handler(update, context):
    if not _allowed(update):
        return
    state = await asyncio.to_thread(_load_state)
    if not state:
        await update.message.reply_text(
            "Незавершених прогонів немає. Якщо підозра, що батчі створились, "
            "а стан загубився — /entity_recover пошукає їх на боці Anthropic.")
        return
    if not state["batch_ids"]:
        # обрив до створення першого батчу — грошей не витрачено
        await asyncio.to_thread(_clear_state)
        await update.message.reply_text(
            "Відправка обірвалась до створення першого батчу — гроші не "
            "витрачались. Запускай /entity_backfill заново.")
        return
    if _poll_running["flag"]:
        await update.message.reply_text("Полінг уже працює — /entity_status.")
        return
    state["chat_id"] = update.effective_chat.id  # звіт — сюди
    await asyncio.to_thread(_save_state, state)
    await update.message.reply_text("🦊 Переприв'язав полінг, стежу далі…")
    asyncio.create_task(_poll_task(context.bot))


# ---------- /entity_dedup ----------

def _dedup_entities():
    """Глобальне переслияння за поточними правилами norm() (точний збіг у межах
    kind). Потрібне разово: фаза-1 писалась ДО нормалізації лапок, тож у норі
    лишились близнюки типу «Слуга народу»/Слуга народу. Ідемпотентно."""
    conn = ep.connect()
    cur = conn.cursor()
    cur.execute("SELECT id, kind, name_ua, name_ru, mentions FROM entities")
    mentions = {}
    groups = {}
    for eid, kind, nua, nru, m in cur.fetchall():
        mentions[eid] = m or 0
        for nm in (nua, nru):
            n = ep.norm(nm)
            if n:
                groups.setdefault((kind, n), set()).add(eid)
    # транзитивне об'єднання (сутність може ділити різні ключі) — union-find
    parent = {}
    def find(x):
        root = x
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(x, x) != x:
            parent[x], x = root, parent[x]
        return root
    for ids in groups.values():
        ids = sorted(ids)
        for other in ids[1:]:
            ra, rb = find(ids[0]), find(other)
            if ra != rb:
                parent[rb] = ra
    clusters = {}
    for eid in mentions:
        clusters.setdefault(find(eid), []).append(eid)

    n_groups = n_removed = 0
    for ids in clusters.values():
        if len(ids) < 2:
            continue
        n_groups += 1
        keep = max(ids, key=lambda i: mentions[i])
        others = [i for i in ids if i != keep]
        cur.execute("SELECT id, name_ua, name_ru, aliases FROM entities WHERE id = ANY(%s)",
                    (ids,))
        cards = {r[0]: r for r in cur.fetchall()}
        _, kua, kru, kal = cards[keep]
        aliases = set(kal or [])
        for oid in others:
            _, oua, oru, oal = cards[oid]
            kua = kua or oua
            kru = kru or oru
            aliases |= set(oal or [])
            for nm in (oua, oru):
                if nm and ep.norm(nm) not in {ep.norm(kua), ep.norm(kru)}:
                    aliases.add(nm)
        for oid in others:
            # перевісити зв'язки (дубль-пару статті лишаємо keep-версією)
            cur.execute(
                "UPDATE article_entities ae SET entity_id = %s "
                "WHERE entity_id = %s AND NOT EXISTS ("
                "  SELECT 1 FROM article_entities x "
                "  WHERE x.article_id = ae.article_id AND x.entity_id = %s)",
                (keep, oid, keep))
            cur.execute("DELETE FROM article_entities WHERE entity_id = %s", (oid,))
            cur.execute("DELETE FROM entities WHERE id = %s", (oid,))
            n_removed += 1
        cur.execute(
            "UPDATE entities SET name_ua = %s, name_ru = %s, aliases = %s WHERE id = %s",
            (kua, kru, sorted(aliases), keep))
    # перерахунок агрегатів — ті самі запити, що у write_results
    cur.execute("""
        UPDATE entities e SET mentions = s.cnt, first_seen = s.fmin, last_seen = s.fmax
        FROM (SELECT ae.entity_id, count(*) AS cnt,
                     min(a.published) AS fmin, max(a.published) AS fmax
              FROM article_entities ae JOIN articles a ON a.id = ae.article_id
              GROUP BY ae.entity_id) s
        WHERE e.id = s.entity_id""")
    cur.execute("""
        UPDATE entities e SET role_last = sub.role
        FROM (SELECT DISTINCT ON (ae.entity_id) ae.entity_id, ae.role_at_time AS role
              FROM article_entities ae JOIN articles a ON a.id = ae.article_id
              WHERE ae.role_at_time IS NOT NULL AND ae.role_at_time <> ''
              ORDER BY ae.entity_id, a.published DESC) sub
        WHERE e.id = sub.entity_id""")
    conn.commit()
    cur.close()
    conn.close()
    return n_groups, n_removed


async def entity_dedup_handler(update, context):
    if not _allowed(update):
        return
    if await asyncio.to_thread(_load_state):
        await update.message.reply_text("Іде бэкфіл — дедуп після нього.")
        return
    msg = await update.message.reply_text("🦊 Переслияю дублі (точний збіг norm у межах kind)…")
    try:
        n_groups, n_removed = await asyncio.to_thread(_dedup_entities)
        await msg.edit_text(
            f"🦊 Дедуп завершено: груп-дублів {n_groups}, злито карток {n_removed}.\n"
            f"Зведення: /entity_status")
    except Exception as e:
        await msg.edit_text(f"❌ Збій дедупу: {e}")


# ---------- /entity_export ----------

async def entity_export_handler(update, context):
    """CSV усіх сутностей (за спаданням згадок) — для ручного QA / аналізу
    поза ботом: падежі, дублі, сміттєві сутності, помилкові злиття."""
    if not _allowed(update):
        return
    msg = await update.message.reply_text("🦊 Вивантажую сутності в CSV…")

    def build():
        import csv
        rows = bot_db.query(
            "SELECT e.id, e.kind, e.subtype, e.name_ua, e.name_ru, e.role_last, "
            "e.mentions, "
            "to_char(to_timestamp(e.first_seen), 'YYYY-MM-DD') AS first_seen, "
            "to_char(to_timestamp(e.last_seen), 'YYYY-MM-DD') AS last_seen, "
            "array_to_string(e.aliases, ' | ') AS aliases "
            "FROM entities e ORDER BY e.mentions DESC, e.id")
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "kind", "subtype", "name_ua", "name_ru", "role_last",
                    "mentions", "first_seen", "last_seen", "aliases"])
        for r in rows:
            w.writerow([r["id"], r["kind"], r["subtype"], r["name_ua"],
                        r["name_ru"], r["role_last"], r["mentions"],
                        r["first_seen"], r["last_seen"], r["aliases"]])
        # BOM — щоб Excel відкривав кирилицю без танців
        return ("\ufeff" + buf.getvalue()).encode("utf-8"), len(rows)

    try:
        data, n = await asyncio.to_thread(build)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось вивантажити: {e}")
        return
    await msg.delete()
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"entities_{n}.csv",
        caption=f"🦊 {n} сутностей за спаданням згадок.",
    )


# ---------- /entity_status ----------

async def entity_status_handler(update, context):
    if not _allowed(update):
        return
    try:
        rows = await bot_db.aquery(
            "SELECT kind, count(*) AS c FROM entities GROUP BY kind ORDER BY c DESC")
        n_links = (await bot_db.aquery("SELECT count(*) AS c FROM article_entities"))[0]["c"]
        top = await bot_db.aquery(
            "SELECT kind, coalesce(name_ua, name_ru) AS name, role_last, mentions "
            "FROM entities ORDER BY mentions DESC LIMIT 10")
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    lines = ["🦊 Сутнісний шар нори\n"]
    total = sum(r["c"] for r in rows)
    lines.append(f"Сутностей: {total}, зв'язків: {n_links}")
    for r in rows:
        lines.append(f"  {r['kind']}: {r['c']}")
    if top:
        lines.append("\nТоп за згадками:")
        for r in top:
            role = f" — {r['role_last']}" if r["role_last"] else ""
            lines.append(f"  [{r['kind']}] {r['name']}{role} ({r['mentions']})")
    state = await asyncio.to_thread(_load_state)
    if state:
        poll = "активний" if _poll_running["flag"] else "ВТРАЧЕНИЙ — /entity_resume"
        try:
            import anthropic
            client = anthropic.Anthropic()
            tot, ended = await asyncio.to_thread(
                _batch_counts, client, state["batch_ids"])
            done_reqs = (tot["succeeded"] + tot["errored"]
                         + tot["canceled"] + tot["expired"])
            total = state.get("articles") or done_reqs + tot["processing"]
            lines.append(f"\nПрогін {state['from']}…{state['to']}: "
                         f"{done_reqs}/{total} статей, "
                         f"батчів {len(ended)}/{len(state['batch_ids'])}, "
                         f"помилок {tot['errored']}, полінг: {poll}")
        except Exception:
            lines.append(f"\nПрогін {state['from']}…{state['to']}: "
                         f"батчів готово {len(state['done'])}/{len(state['batch_ids'])}, "
                         f"полінг: {poll}")
    else:
        lines.append("\nАктивних прогонів немає.")
    incr = await asyncio.to_thread(bot_db.get_state, INCR_CURSOR_KEY)
    if incr is not None:
        lines.append(f"Авто-інкремент: увімкнено (курсор id={incr})")
    else:
        lines.append("Авто-інкремент: вимкнено (/entity_increment_on)")
    await update.message.reply_text("\n".join(lines))
