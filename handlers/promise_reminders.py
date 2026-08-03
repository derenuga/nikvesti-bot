"""
Нагадування банку тем у канал «🦊 Микита винюхав» (docs/PROMISES_BANK.md §5).

**Навіщо.** Дев'ятсот обіцянок у банку нічого не варті, поки все тримається на
«зайди й подивись»: строк минає, і про це не дізнається ніхто, доки хтось сам
не відкриє апку. Реєстр стає сторожем тільки тоді, коли він дзвонить.

**Чому в «винюхав», а не в редакцію.** Там уже живуть тендери, документи влади
й правоохоронці — тобто рівно те саме: машина знайшла, людина вирішує. Чат
редакції лишається для розмов, а не для стрічки сигналів.

---

## Три запобіжники, без яких канал заллє

Банк наповнювався прогонами по місяцях архіву, тому «строк минув» у ньому
СОТНІ — і майже всі історичні. Якщо просто ввімкнути щоденний прогін, канал
дістане звалище про 2020 рік, і його вимкнуть першого ж дня. Тому:

1. **Опт-ін.** Модуль спить, поки Олег не увімкне його кнопкою. Розклад о
   09:10 стоїть, але тихо виходить, якщо прапорця немає.
2. **Baseline при вмиканні** (правило 4 `CLAUDE.md`): усе накопичене
   позначається як «уже дзвонило», КРІМ `BASELINE_KEEP` найгарячіших у кожному
   класі — вони йдуть одразу, щоб перевірити, що формат і відправка працюють.
   Далі дзвонить лише те, що перетнуло поріг ПІСЛЯ вмикання, тобто кілька штук
   на день. Історичні нікуди не діваються — вони лишаються в банку й в апці,
   просто канал не для звалища.
3. **Стеля на повідомлення** (`KIND_LIMIT`). Навіть якщо великий скан приведе
   двісті нових прострочених за раз, у канал піде обмежений список, а решта
   зачекає наступного дня.

**Позначається РІВНО те, що пішло.** Раніше в позначку летіла вся вибірка, а в
повідомлення — перші шість: решта тихо ставала «уже дзвонили» і не дзвонила
ніколи. Прев'ю і відправка тепер ходять однією функцією `plan()` — інакше
«показав одне, надіслав інше».

**Що дзвонить, а що ні** (§5, і це головна частина модуля):

- дзвонить лише те, що має право (`pp.rings`): `hedged`/`considered` не
  дзвонять — нема чого прострочувати; умовні («після завершення війни») теж,
  бо горизонт поза контролем того, хто обіцяв, і рахувати таке зривом нечесно;
- строк перевіряється З ГРЕЙСОМ за точністю дати: «до кінця 2025» не дзвонить
  1 січня о 00:00, а через два тижні. Без цього банк стає будильником, який
  вимикають;
- «давно не питали» — раз на тиждень, не щодня: недатовані обіцянки нікуди не
  біжать, а щоденне їх повторення привчає гортати повідомлення не читаючи.

**Верстка — rich message** (Bot API 10.1, `handlers/rich.py`): заголовки
класів, дослівна цитата в `<blockquote>` з підписом, хто обіцяв, ілюстрація
статті і згортний блок «ще N» замість обрізання. Останнє важливе не красою:
`<details>` прибирає компроміс «показати все або обрізати» — довгий хвіст є в
повідомленні, але не займає екран. Якщо rich не пройде, летить звичайний HTML.
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

# Наявність ключа = щоденні нагадування увімкнено.
ON_KEY = "promise_remind_on"

# За скільки днів до строку попереджаємо. Тиждень — щоб лишався час зробити
# запит і дочекатись відповіді, а не просто зафіксувати зрив.
SOON_DAYS = 7
# Скільки позицій розгорнуто в повідомленні, і скільки всього піде за прогон у
# кожному класі (решта — у згортному блоці). Стеля потрібна не для краси: без
# неї один великий скан вивалює в канал двісті рядків за раз.
SHOW_PER_KIND = 4
KIND_LIMIT = 10
# Скільки ілюстрацій. Картинка на кожен рядок перетворює сигнал на стрічку,
# тому по одній на клас — на найгарячішій темі, тобто саме там, куди хочемо
# привести очі.
IMAGES_PER_KIND = 1
# Скільки найгарячіших лишаємо недоторканими при baseline. Три на клас — щоб
# перше ж повідомлення показало реальний формат на реальних даних (правило 4),
# але не стало тим самим звалищем, від якого baseline і рятує.
BASELINE_KEEP = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promise_reminders (
    commitment_id BIGINT NOT NULL,
    kind          TEXT NOT NULL,
    sent          BIGINT,
    PRIMARY KEY (commitment_id, kind)
);
"""

KINDS = ("overdue", "soon", "stale")

HEAD = {
    "overdue": ("⏰", "Строк минув"),
    "soon": ("🗓", "Строк за тиждень"),
    "stale": ("💤", "Давно не питали"),
}


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


def is_on():
    return bot_db.get_state(ON_KEY) is not None


def _already(cur, kind):
    cur.execute("SELECT commitment_id FROM promise_reminders WHERE kind = %s",
                (kind,))
    return {r[0] for r in cur.fetchall()}


def _due(cur, weekly, now, ignore_sent):
    """Усе, що має право дзвонити сьогодні, по класах і без стелі."""
    rows = pp.list_queue(cur, limit=None, now=now)
    out = {}
    for kind in KINDS:
        if kind == "stale" and not weekly:
            continue
        seen = set() if ignore_sent else _already(cur, kind)
        picked = []
        for r in rows:
            if r["id"] in seen or not pp.rings(r):
                continue
            if r["class"] != kind:
                continue
            if kind == "soon":
                if not r.get("deadline"):
                    continue
                if int(r["deadline"]) - now > SOON_DAYS * 86400:
                    continue
            picked.append(r)
        if picked:
            out[kind] = picked
    return out


def plan(weekly=False, now=None, ignore_sent=False):
    """Що саме піде в повідомлення — і рівно це потім позначиться.

    Прев'ю і відправка ходять цією функцією разом: якщо їх розвести, кнопка
    «надіслати» перестає означати те, що людина щойно бачила.
    """
    now = now or int(time.time())
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            due = _due(cur, weekly, now, ignore_sent)
            groups, extra, waiting = {}, {}, {}
            for kind, rows in due.items():
                waiting[kind] = len(rows)
                groups[kind] = rows[:KIND_LIMIT]
                extra[kind] = len(rows) - len(groups[kind])

            ids = [r["id"] for g in groups.values() for r in g]
            first = {}
            if ids:
                for rev in pp.revisions(cur, ids):
                    first.setdefault(rev["commitment_id"], rev)
            art_ids = {v["article_id"] for v in first.values()
                       if v.get("article_id")}

            from handlers.promises import _links_for
            from handlers.promise_app import _authors_for, _images_for
            links = _links_for(cur, art_ids)
            try:
                authors = _authors_for(cur, art_ids)
            except Exception as e:    # БД сайту опційна — без неї просто без імені
                print(f"promise_reminders: автори недоступні — {e}")
                authors = {}
            images = _images_for(cur, art_ids)
    finally:
        conn.close()
    return {"groups": groups, "extra": extra, "waiting": waiting,
            "first": first, "links": links, "authors": authors,
            "images": images, "now": now}


# ---------- Baseline ----------

def baseline(now=None, keep=BASELINE_KEEP):
    """Заглушити накопичене, лишивши `keep` найгарячіших у кожному класі.

    Повертає (скільки заглушено, скільки лишили дзвонити). Історичні обіцянки
    при цьому НЕ зникають і не змінюються — позначка живе окремою таблицею й
    означає лише «про це в канал уже казали».
    """
    now = now or int(time.time())
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        ensure_schema(conn)
        with conn.cursor() as cur:
            # weekly=True: заглушуємо й «давно не питали», інакше найближчий
            # понеділок вивалить у канал усю історичну купу.
            due = _due(cur, True, now, False)
            silenced = kept = 0
            for kind, rows in due.items():
                for r in rows[keep:]:
                    cur.execute(
                        "INSERT INTO promise_reminders (commitment_id, kind, sent) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (r["id"], kind, now))
                    silenced += cur.rowcount or 0
                kept += min(keep, len(rows))
        conn.commit()
    finally:
        conn.close()
    return silenced, kept


def _mark(rows_by_kind, now):
    conn = ep.connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            for kind, rows in rows_by_kind.items():
                for r in rows:
                    cur.execute(
                        "INSERT INTO promise_reminders (commitment_id, kind, sent) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (r["id"], kind, now))
        conn.commit()
    finally:
        conn.close()


# ---------- Верстка ----------

def _attr(value):
    """Екранування значення АТРИБУТА. `escape_html` лапок не чіпає, а og:image
    приїжджає зі сторонньої сторінки — однієї лапки досить, щоб розвалити
    розмітку всього повідомлення."""
    return escape_html(value).replace('"', "&quot;")


def _meta(row, now):
    """Права частина рядка: строк і спосіб перевірки."""
    tail = []
    if row.get("deadline"):
        if row["class"] == "overdue":
            tail.append(f"строк минув {pp.human_gap(now - int(row['deadline']))} тому")
        else:
            tail.append(f"до {pp.fmt_date(row['deadline'])}")
    method = pp.METHOD_WORD.get(row.get("verification_method"))
    if method:
        tail.append(method)
    return tail


def _rich_item(row, rev, links, authors, images, now, with_image):
    """Одна обіцянка: ілюстрація (як герой класу), назва, ДОСЛІВНА цитата з
    підписом, хто обіцяв, і лінк на матеріал. Цитата тут не прикраса — саме
    вона перетворює нагадування з докору на готовий привід піти й спитати."""
    rev = rev or {}
    aid = rev.get("article_id")
    link = links.get(aid) or {}
    out = []

    if with_image and images.get(aid):
        cap = escape_html(link.get("title") or row.get("title") or "матеріал")
        author = authors.get(aid)
        if author:
            cap += f"<cite>{escape_html(author)}</cite>"
        out.append(f'<figure><img src="{_attr(images[aid])}"/>'
                   f'<figcaption>{cap}</figcaption></figure>')

    head = f"<b>{escape_html(row.get('title') or '—')}</b>"
    meta = _meta(row, now)
    if meta:
        head += " — " + escape_html(" · ".join(meta))
    out.append(f"<p>{head}</p>")

    quote = (rev.get("quote") or "").strip()
    if quote:
        who = rev.get("promiser_text") or row.get("owner_text")
        cite = f"<cite>{escape_html(who)}</cite>" if who else ""
        out.append(f"<blockquote>{escape_html(quote)}{cite}</blockquote>")

    if link.get("url"):
        out.append(f'<p><a href="{_attr(link["url"])}">'
                   f'{escape_html(link.get("title") or "матеріал")}</a></p>')
    return "".join(out)


def render_rich(data):
    """Повідомлення цілком. Хвіст класу — у згортному блоці: він не обрізає
    список і не займає екран, тобто прибирає компроміс, через який раніше
    доводилось писати «…і ще 12»."""
    now = data["now"]
    parts = ["<h3>🦊 Банк тем нагадує</h3>"]
    for kind in KINDS:
        rows = data["groups"].get(kind)
        if not rows:
            continue
        icon, word = HEAD[kind]
        total = data["waiting"].get(kind, len(rows))
        parts.append(f"<h4>{icon} {word}"
                     + (f" · {total}" if total > 1 else "") + "</h4>")
        shown = rows[:SHOW_PER_KIND]
        for i, r in enumerate(shown):
            parts.append(_rich_item(r, data["first"].get(r["id"]), data["links"],
                                    data["authors"], data["images"], now,
                                    with_image=i < IMAGES_PER_KIND))
            if i < len(shown) - 1:
                parts.append("<hr/>")
        rest = rows[SHOW_PER_KIND:]
        if rest:
            items = []
            for r in rest:
                rev = data["first"].get(r["id"]) or {}
                link = data["links"].get(rev.get("article_id")) or {}
                line = f"<b>{escape_html(r.get('title') or '—')}</b>"
                meta = _meta(r, now)
                if meta:
                    line += " — " + escape_html(" · ".join(meta))
                if link.get("url"):
                    line += f' · <a href="{_attr(link["url"])}">матеріал</a>'
                items.append(f"<li>{line}</li>")
            parts.append(f"<details><summary>Ще {len(rest)} у цьому класі"
                         f"</summary><ul>{''.join(items)}</ul></details>")
        held = data["extra"].get(kind) or 0
        if held:
            parts.append(f"<p><i>Ще {held} чекають — прийдуть наступного "
                         f"разу, щоб не залити канал.</i></p>")
    parts.append("<footer>Дзвонять лише обіцянки з горизонтом і без умов: "
                 "«планують» і «після завершення війни» лежать у банку, але "
                 "не смикають.</footer>")
    return "".join(parts)


def render_plain(data):
    """Фолбек, коли rich не пройшов. Та сама фактура, лише без верстки."""
    now = data["now"]
    body = ["🦊 <b>Банк тем нагадує</b>", ""]
    for kind in KINDS:
        rows = data["groups"].get(kind)
        if not rows:
            continue
        icon, word = HEAD[kind]
        total = data["waiting"].get(kind, len(rows))
        body.append(f"{icon} <b>{word}</b>" + (f" · {total}" if total > 1 else ""))
        for r in rows[:SHOW_PER_KIND]:
            rev = data["first"].get(r["id"]) or {}
            link = data["links"].get(rev.get("article_id")) or {}
            who = rev.get("promiser_text") or r.get("owner_text")
            line = [f"• <b>{escape_html(r.get('title') or '—')}</b>"]
            tail = ([escape_html(who)] if who else []) + \
                   [escape_html(x) for x in _meta(r, now)]
            if tail:
                line.append("  " + " · ".join(tail))
            if link.get("url"):
                line.append(f'  <a href="{_attr(link["url"])}">'
                            f'{escape_html(link.get("title") or "матеріал")}</a>')
            body.append("\n".join(line))
        rest = len(rows) - SHOW_PER_KIND + (data["extra"].get(kind) or 0)
        if rest > 0:
            body.append(f"  <i>…і ще {rest} — усі в банку</i>")
        body.append("")
    body.append("<i>Дзвонять лише обіцянки з горизонтом і без умов.</i>")
    return "\n".join(body)


async def _keyboard(bot):
    app_url, _ = await resolve_app_link(bot)
    if not app_url:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "Відкрити банк тем", url=f"{app_url}?startapp=promises")]])


# ---------- Відправка ----------

async def rich_send(bot, chat_id, data):
    from handlers import rich
    return await rich.send_rich(bot, chat_id, render_rich(data),
                                fallback=render_plain(data),
                                reply_markup=await _keyboard(bot))


async def send_reminders(bot, weekly=False, chat_id=None, mark=True, data=None):
    """Нагадування в «винюхав». Тихе, коли дзвонити нема чого.

    Повертає True, якщо повідомлення пішло.
    """
    if not bot_db.is_configured():
        return False
    target = chat_id or CHAT_ID
    if not target:
        return False
    if data is None:
        data = await asyncio.to_thread(plan, weekly, None, not mark)
    if not data["groups"]:
        return False

    await rich_send(bot, target, data)

    # Позначка ЛИШЕ після успішної відправки і ЛИШЕ на те, що справді пішло:
    # інакше збій мережі з'їдав би нагадування назавжди (урок report_reminders),
    # а стеля на повідомлення — тихо ховала б хвіст.
    if mark:
        await asyncio.to_thread(_mark, data["groups"], data["now"])
    return True


async def daily(bot):
    """Щодня о 09:10. Понеділок додає «давно не питали».

    Мовчить, поки нагадування не увімкнені кнопкою: банк наповнювався сканами
    архіву, і без опт-іну перший же прогін вивалив би в канал сотні
    історичних прострочень.
    """
    try:
        if not await asyncio.to_thread(is_on):
            return
        from datetime import datetime
        from zoneinfo import ZoneInfo
        weekly = datetime.now(ZoneInfo("Europe/Kiev")).weekday() == 0
        await send_reminders(bot, weekly=weekly)
    except Exception as e:
        await notify_error(bot, "нагадування банку тем", e)


# ---------- /promise_remind ----------

def _summary(data, on):
    lines = []
    total_ready = sum(len(v) for v in data["groups"].values())
    for kind in KINDS:
        n = data["waiting"].get(kind)
        if n:
            icon, word = HEAD[kind]
            go = len(data["groups"].get(kind, []))
            lines.append(f"{icon} {word}: <b>{n}</b>"
                         + (f" (у повідомлення піде {go})" if go < n else ""))
    if not lines:
        return ("🦊 Дзвонити нема про що — усе, що мало продзвонити, уже "
                "дзвонило.")
    head = ("🦊 <b>Нагадування банку тем</b>\n"
            f"Стан: {'увімкнені, щодня о 09:10' if on else '<b>вимкнені</b>'}\n\n")
    tail = (f"\n\nЗараз у канал пішло б <b>{total_ready}</b> позицій. "
            "Нижче — точнісінько те повідомлення, що піде.")
    return head + "\n".join(lines) + tail


async def promise_remind_handler(update, context):
    """/promise_remind [all] — подивитись, що піде в канал, і вирішити.

    Нічого не шле у «винюхав» і не позначає: показує числа й сам рендер у ЦЕЙ
    чат, а відправка, вмикання й вимикання — кнопками. Саме тут живе обіцянка
    «контрольовано»: канал не дізнається про банк, поки не тапнули.
    """
    from handlers.promises import _allowed
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not _allowed(update):
        return
    args = context.args or []
    preview_all = bool(args) and args[0].lower() in ("all", "усе", "все", "test")
    msg = await update.message.reply_text("🦊 Дивлюсь, кому пора нагадати…")
    try:
        on = await asyncio.to_thread(is_on)
        data = await asyncio.to_thread(plan, True, None, preview_all)
        if not data["groups"]:
            await msg.edit_text(
                _summary(data, on) + ("" if preview_all else
                                      "\n\nПрев'ю формату: /promise_remind all"),
                parse_mode="HTML")
            return

        rows = [[InlineKeyboardButton("📨 Надіслати в «винюхав»",
                                      callback_data="prm:send")]]
        if on:
            rows.append([InlineKeyboardButton("🔕 Вимкнути щоденні",
                                              callback_data="prm:off")])
        else:
            silence = sum(max(0, data["waiting"].get(k, 0) - BASELINE_KEEP)
                          for k in KINDS)
            rows.append([InlineKeyboardButton(
                f"🔔 Увімкнути (заглушити {silence} історичних)",
                callback_data="prm:on")])
        await msg.edit_text(_summary(data, on), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(rows))
        await rich_send(context.bot, update.effective_chat.id, data)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")


async def promise_remind_callback(update, context):
    """Кнопки під /promise_remind: надіслати · увімкнути · вимкнути."""
    from handlers.promises import _allowed

    q = update.callback_query
    if not _allowed(update):
        await q.answer("Не для цього чату", show_alert=True)
        return
    action = q.data.split(":", 1)[1]
    await q.answer()

    if action == "off":
        await asyncio.to_thread(
            bot_db.execute, "DELETE FROM sync_state WHERE key = %s", (ON_KEY,))
        await q.edit_message_text(
            "🔕 Щоденні нагадування вимкнено. Банк лишається, просто мовчить.\n"
            "Увімкнути знову — /promise_remind.")
        return

    if action == "on":
        await q.edit_message_text("🦊 Глушу накопичене…")
        silenced, kept = await asyncio.to_thread(baseline)
        await asyncio.to_thread(bot_db.set_state, ON_KEY, int(time.time()))
        sent = await send_reminders(context.bot, weekly=True)
        await q.edit_message_text(
            f"🔔 Нагадування увімкнено, щодня о 09:10 у «винюхав».\n\n"
            f"Заглушив історичних: <b>{silenced}</b> — вони лишились у банку й "
            f"в апці, просто в канал про них не дзвонить.\n"
            f"Лишив дзвонити: <b>{kept}</b>"
            + (" — уже пішли в канал, перевір формат."
               if sent else " (у канал зараз нічого не пішло)."),
            parse_mode="HTML")
        return

    if action == "send":
        await q.edit_message_text("🦊 Надсилаю…")
        sent = await send_reminders(context.bot, weekly=True)
        await q.edit_message_text(
            "📨 Пішло в «винюхав»." if sent else
            "🦊 Не було чого слати — усе вже дзвонило.")
