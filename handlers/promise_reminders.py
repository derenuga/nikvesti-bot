"""
Нагадування банку тем у канал «🦊 Микита винюхав» (docs/PROMISES_BANK.md §5).

**Навіщо.** Дев'ятсот обіцянок у банку нічого не варті, поки все тримається на
«зайди й подивись»: строк минає, і про це не дізнається ніхто, доки хтось сам
не відкриє апку. Реєстр стає сторожем тільки тоді, коли він дзвонить.

**Чому в «винюхав», а не в редакцію.** Там уже живуть тендери, документи влади
й правоохоронці — тобто рівно те саме: машина знайшла, людина вирішує. Чат
редакції лишається для розмов, а не для стрічки сигналів.

**Що дзвонить, а що ні** (§5, і це головна частина модуля):

- дзвонить лише те, що має право (`pp.rings`): `hedged`/`considered` не
  дзвонять — нема чого прострочувати; умовні («після завершення війни») теж,
  бо горизонт поза контролем того, хто обіцяв, і рахувати таке зривом нечесно;
- строк перевіряється З ГРЕЙСОМ за точністю дати: «до кінця 2025» не дзвонить
  1 січня о 00:00, а через два тижні. Без цього банк стає будильником, який
  вимикають;
- «давно не питали» — раз на тиждень, не щодня: недатовані обіцянки нікуди не
  біжать, а щоденне їх повторення привчає гортати повідомлення не читаючи.

**Ідемпотентність.** Кожне нагадування пишеться в `promise_reminders` з
UNIQUE(commitment_id, kind) — та сама обіцянка не смикає двічі за той самий
привід. Позначка ставиться ЛИШЕ після успішної відправки: інакше збій мережі
з'їдав би нагадування назавжди (урок `report_reminders`).
"""

import asyncio
import os
import time

from handlers import bot_db
from handlers.helpers import escape_html, resolve_app_link
from handlers.notifier import notify_error
import entity_pipeline as ep
import promise_pipeline as pp

CHAT_ID = os.environ.get("DOCUMENTS_CHAT_ID") or os.environ.get("CHAT_ID")

# За скільки днів до строку попереджаємо. Тиждень — щоб лишався час зробити
# запит і дочекатись відповіді, а не просто зафіксувати зрив.
SOON_DAYS = 7
# Скільки позицій показуємо в одному повідомленні. Більше — це вже не сигнал,
# а список, який гортають не читаючи; решта видно в апці за лінком.
MAX_PER_KIND = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promise_reminders (
    commitment_id BIGINT NOT NULL,
    kind          TEXT NOT NULL,
    sent          BIGINT,
    PRIMARY KEY (commitment_id, kind)
);
"""


def ensure_schema(conn=None):
    own = conn is None
    conn = conn or ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
    finally:
        if own:
            conn.close()


def _already(cur, kind):
    cur.execute("SELECT commitment_id FROM promise_reminders WHERE kind = %s",
                (kind,))
    return {r[0] for r in cur.fetchall()}


def collect(weekly=False, now=None, ignore_sent=False):
    """Що саме має продзвонити сьогодні. Нічого не пише і не позначає.

    `ignore_sent` — прев'ю формату: показати те, що вже дзвонило.
    """
    now = now or int(time.time())
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            rows = pp.list_queue(cur, limit=None, now=now)
            out = {}
            for kind in ("overdue", "soon", "stale"):
                if kind == "stale" and not weekly:
                    continue
                seen = set() if ignore_sent else _already(cur, kind)
                picked = []
                for r in rows:
                    if r["id"] in seen or not pp.rings(r):
                        continue
                    if kind == "overdue" and r["class"] != "overdue":
                        continue
                    if kind == "soon":
                        if r["class"] != "soon" or not r.get("deadline"):
                            continue
                        if int(r["deadline"]) - now > SOON_DAYS * 86400:
                            continue
                    if kind == "stale" and r["class"] != "stale":
                        continue
                    picked.append(r)
                if picked:
                    out[kind] = picked
            ids = [r["id"] for group in out.values() for r in group]
            first = {}
            if ids:
                for rev in pp.revisions(cur, ids):
                    first.setdefault(rev["commitment_id"], rev)
            from handlers.promises import _links_for
            links = _links_for(cur, {v["article_id"] for v in first.values()})
    finally:
        conn.close()
    return out, first, links


HEAD = {
    "overdue": "⏰ <b>Строк минув</b>",
    "soon": "🗓 <b>Строк за тиждень</b>",
    "stale": "💤 <b>Давно не питали</b>",
}


def _line(row, rev, links, now):
    """Рядок обіцянки. Лінк на статтю — обов'язковий: нагадування без доказу
    це просто докір, а з цитатою й посиланням із ним уже можна йти питати."""
    link = links.get((rev or {}).get("article_id")) or {}
    who = (rev or {}).get("promiser_text") or row.get("owner_text")
    parts = [f"• <b>{escape_html(row['title'] or '—')}</b>"]
    tail = []
    if who:
        tail.append(escape_html(who))
    if row.get("deadline"):
        if row["class"] == "overdue":
            tail.append(f"строк минув {pp.human_gap(now - int(row['deadline']))} тому")
        else:
            tail.append(f"до {pp.fmt_date(row['deadline'])}")
    method = pp.METHOD_WORD.get(row.get("verification_method"))
    if method:
        tail.append(method)
    if tail:
        parts.append("  " + " · ".join(tail))
    if link.get("url"):
        parts.append(f"  <a href=\"{link['url']}\">{escape_html(link.get('title') or 'матеріал')}</a>")
    return "\n".join(parts)


async def send_reminders(bot, weekly=False, chat_id=None, mark=True):
    """Щоденне нагадування в «винюхав». Тихе, коли дзвонити нема чого."""
    if not bot_db.is_configured():
        return
    target = chat_id or CHAT_ID
    if not target:
        return
    now = int(time.time())
    groups, first, links = await asyncio.to_thread(collect, weekly, now, not mark)
    if not groups:
        return

    body = []
    for kind in ("overdue", "soon", "stale"):
        rows = groups.get(kind)
        if not rows:
            continue
        head = HEAD[kind]
        if len(rows) > MAX_PER_KIND:
            head += f" · {len(rows)}"
        body.append(head)
        for r in rows[:MAX_PER_KIND]:
            body.append(_line(r, first.get(r["id"]), links, now))
        if len(rows) > MAX_PER_KIND:
            body.append(f"  <i>…і ще {len(rows) - MAX_PER_KIND} — усі в банку</i>")
        body.append("")

    app_url, _ = await resolve_app_link(bot)
    tail = ("<i>Дзвонять лише обіцянки з горизонтом і без умов: «планують» і "
            "«після завершення війни» лежать у банку, але не смикають.</i>")
    text = "🦊 <b>Банк тем нагадує</b>\n\n" + "\n".join(body) + tail

    kb = None
    if app_url:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "Відкрити банк тем", url=f"{app_url}?startapp=promises")]])
    await bot.send_message(target, text[:4000], parse_mode="HTML",
                           disable_web_page_preview=True, reply_markup=kb)

    if not mark:
        return

    def remember():
        conn = ep.connect()
        try:
            ensure_schema(conn)
            with conn.cursor() as cur:
                for kind, rows in groups.items():
                    for r in rows:
                        cur.execute(
                            "INSERT INTO promise_reminders (commitment_id, kind, sent) "
                            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (r["id"], kind, now))
            conn.commit()
        finally:
            conn.close()

    # Позначка ЛИШЕ після успішної відправки: інакше збій мережі з'їдав би
    # нагадування назавжди (урок report_reminders).
    await asyncio.to_thread(remember)


async def daily(bot):
    """Щодня о 09:10. Понеділок додає «давно не питали»."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        weekly = datetime.now(ZoneInfo("Europe/Kiev")).weekday() == 0
        await send_reminders(bot, weekly=weekly)
    except Exception as e:
        await notify_error(bot, "нагадування банку тем", e)


async def promise_remind_handler(update, context):
    """/promise_remind [all] — прогнати нагадування зараз, у ЦЕЙ чат.

    Без аргументу поважає позначки (покаже лише те, що ще не дзвонило);
    `all` ігнорує їх і нічого не позначає — прев'ю формату.
    """
    from handlers.promises import _allowed

    if not _allowed(update):
        return
    args = context.args or []
    preview = bool(args) and args[0].lower() in ("all", "усе", "все", "test")
    msg = await update.message.reply_text("🦊 Дивлюсь, кому пора нагадати…")
    try:
        groups, _, _ = await asyncio.to_thread(collect, True, None, preview)
        if not groups:
            await msg.edit_text(
                "🦊 Нагадувати нема про що — усе, що мало продзвонити, уже "
                "дзвонило." + ("" if preview else " Прев'ю формату: "
                               "/promise_remind all"))
            return
        await msg.delete()
        await send_reminders(context.bot, weekly=True,
                             chat_id=update.effective_chat.id,
                             mark=not preview)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
