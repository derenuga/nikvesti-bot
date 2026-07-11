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

    def submit():
        import anthropic
        client = anthropic.Anthropic()
        arts = api.fetch_range(from_date, to_date)
        if not arts:
            return None, 0
        sys_len = len(api.get_system_prompt().encode("utf-8"))
        chunks = api.chunk_articles(arts, sys_len)
        batch_ids = []
        for chunk in chunks:
            requests = [api._make_request(client, a) for a in chunk]
            batch = client.messages.batches.create(requests=requests)
            batch_ids.append(batch.id)
        return batch_ids, len(arts)

    try:
        batch_ids, n_arts = await asyncio.to_thread(submit)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалося відправити батчі: {e}")
        return
    if not batch_ids:
        await msg.edit_text("У діапазоні немає статей.")
        return

    state = {"batch_ids": batch_ids, "done": [], "from": from_date, "to": to_date,
             "chat_id": update.effective_chat.id, "started": int(time.time()),
             "articles": n_arts}
    await asyncio.to_thread(_save_state, state)
    await msg.edit_text(
        f"🦊 Відправлено {n_arts} статей у {len(batch_ids)} батч(ів).\n"
        f"Полю щохвилини; зазвичай хвилини–година. Стан: /entity_status\n"
        f"Якщо бот редеплоїться — /entity_resume переприв'яже полінг."
    )
    asyncio.create_task(_poll_task(context.bot))


# ---------- полінг + ingest ----------

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
            pending = [b for b in state["batch_ids"] if b not in state["done"]]
            if not pending:
                break
            for bid in pending:
                b = await asyncio.to_thread(client.messages.batches.retrieve, bid)
                if b.processing_status == "ended":
                    state["done"].append(bid)
                    await asyncio.to_thread(_save_state, state)
            if len(state["done"]) == len(state["batch_ids"]):
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
        results, n_err = [], 0
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        for bid in state["batch_ids"]:
            for res in client.messages.batches.results(bid):
                if res.result.type != "succeeded":
                    n_err += 1
                    continue
                msg = res.result.message
                u = msg.usage
                usage["input"] += u.input_tokens or 0
                usage["output"] += u.output_tokens or 0
                usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                usage["cache_creation"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                text = next((bl.text for bl in msg.content if bl.type == "text"), None)
                if not text:
                    n_err += 1
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    n_err += 1
                    continue
                results.append({"article_id": int(res.custom_id),
                                "entities": obj.get("entities", [])})
        stats = ep.write_results(results)  # діапазонний бэкфіл — курсор не рухаємо
        return stats, n_err, usage, len(results)

    try:
        stats, n_err, usage, n_ok = await asyncio.to_thread(collect_and_write)
        # облік вартості (/aicost): один агрегований запис на прогін
        record_ai_usage(api.MODEL, input_tokens=usage["input"],
                        output_tokens=usage["output"],
                        cache_read=usage["cache_read"],
                        cache_creation=usage["cache_creation"])
        await asyncio.to_thread(_clear_state)
        cost = (usage["input"] * api.PRICE_IN + usage["output"] * api.PRICE_OUT
                + usage["cache_read"] * api.PRICE_IN * 0.1
                + usage["cache_creation"] * api.PRICE_IN * 1.25)
        await bot.send_message(
            chat_id,
            f"🦊 Бэкфіл {state['from']}…{state['to']} завершено!\n\n"
            f"Статей оброблено: {stats['articles']} (помилок: {n_err})\n"
            f"Зв'язків: {stats['links']}\n"
            f"Нових сутностей: {stats['new_entities']}\n"
            f"Токени: {usage['input']/1e6:.1f}M вх / {usage['output']/1e6:.1f}M вих "
            f"(+{usage['cache_read']/1e6:.1f}M кеш)\n"
            f"Вартість ≈ ${cost:.2f}\n\n"
            f"Зведення: /entity_status",
        )
    except Exception as e:
        # стан НЕ чистимо — /entity_resume дасть повторити ingest (ідемпотентно)
        await notify_error(bot, "entity_backfill (ingest у нору)", e)
        await bot.send_message(
            chat_id,
            f"❌ Збій зливання результатів: {e}\n"
            f"Батчі готові й нікуди не дінуться (29 днів) — /entity_resume повторить.",
        )


# ---------- /entity_resume ----------

async def entity_resume_handler(update, context):
    if not _allowed(update):
        return
    state = await asyncio.to_thread(_load_state)
    if not state:
        await update.message.reply_text("Незавершених прогонів немає.")
        return
    if _poll_running["flag"]:
        await update.message.reply_text("Полінг уже працює — /entity_status.")
        return
    state["chat_id"] = update.effective_chat.id  # звіт — сюди
    await asyncio.to_thread(_save_state, state)
    await update.message.reply_text("🦊 Переприв'язав полінг, стежу далі…")
    asyncio.create_task(_poll_task(context.bot))


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
        done, totalb = len(state["done"]), len(state["batch_ids"])
        poll = "активний" if _poll_running["flag"] else "ВТРАЧЕНИЙ — /entity_resume"
        lines.append(f"\nПрогін {state['from']}…{state['to']}: "
                     f"батчів готово {done}/{totalb}, полінг: {poll}")
    else:
        lines.append("\nАктивних прогонів немає.")
    await update.message.reply_text("\n".join(lines))
