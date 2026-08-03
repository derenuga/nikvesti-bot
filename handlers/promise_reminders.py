"""
Нагадування банку тем у канал «🦊 Микита винюхав» (docs/PROMISES_BANK.md §5).

**Навіщо.** Дев'ятсот обіцянок у банку нічого не варті, поки все тримається на
«зайди й подивись»: строк минає, і про це не дізнається ніхто, доки хтось сам
не відкриє апку. Реєстр стає сторожем тільки тоді, коли нагадує сам.

**Чому в «винюхав», а не в редакцію.** Там уже живуть тендери, документи влади
й правоохоронці — тобто рівно те саме: машина знайшла, людина вирішує. Чат
редакції лишається для розмов, а не для стрічки сигналів.

---

## Одне повідомлення — одна обіцянка

Перша версія складала все в ОДНЕ повідомлення із заголовками приводів,
списками й згортними блоками. Олег розібрав її по кісточках 03.08, і головне
заперечення було не про красу: **таке повідомлення неможливо переслати й
неможливо обговорити.** У чаті редакції на нього не відповіси — незрозуміло,
про яку з десяти обіцянок ідеться. Тому одна обіцянка = одне повідомлення, і
стеля на день — це буквально кількість повідомлень (`DAILY_MAX`).

Друге заперечення — заголовком стояв СТАН («Строк минув · 52»), а не сама
обіцянка. «52» — це лічильник бази, у каналі він читається як хвилини. Числа
лишились у пульті `/promise_remind`, де вони службові й доречні.

Третє — мова. «Клас», «дзвонять», «позиції», «заглушити» — це переклад з
англійської технічної говірки, а не мова редакції. У повідомленні їх немає
жодного.

## Виділення: рівно один <mark>

Кольору тексту Telegram не дає взагалі (перевірено по схемі Bot API, не на
око): із виділення всередині рядка є `<b> <i> <u> <s> <code> <mark> <sub>
<sup> <tg-spoiler>`. Тому підсвічується РІВНО ОДИН факт на повідомлення — той,
що відповідає на питання «чому це прийшло саме зараз»: минуло два місяці ·
лишилось 6 днів · востаннє писали пів року тому. Якщо підсвічувати ще й дату
та суму, підсвітка перестає щось означати.

## Три запобіжники, без яких канал заллє

Банк наповнювався прогонами по місяцях архіву, тому прострочених у ньому сотні
— і майже всі давні. Наївний щоденний прогін вивалив би в канал звалище про
2020 рік, і його вимкнули б першого ж дня. Тому:

1. **Опт-ін.** Модуль спить, поки Олег не увімкне його кнопкою. Розклад о
   09:10 стоїть, але тихо виходить, якщо прапорця немає.
2. **Baseline при вмиканні** (правило 4 `CLAUDE.md`): усе накопичене
   позначається як «уже нагадували», КРІМ `BASELINE_KEEP` найважливіших —
   вони йдуть одразу, щоб перевірити на живому, що формат і відправка
   працюють. Кнопка вмикання підписана числом, тобто ціна відома до тапу.
   Давні обіцянки нікуди не діваються — вони лишаються в банку й в апці,
   просто канал не для звалища.
3. **Стеля на день** (`DAILY_MAX`). Навіть якщо великий скан приведе двісті
   нових прострочених, у канал піде три повідомлення, а решта зачекає.

**Позначається РІВНО те, що пішло**, і кожне повідомлення — окремо, одразу
після успішної відправки. Перша версія позначала всю вибірку, а показувала
перші шість: решта тихо ставала «уже нагадували» і не нагадувала ніколи.

**Що взагалі має право нагадувати** (§5):

- лише те, що проходить `pp.rings`: `hedged`/`considered` не нагадують — нема
  чого прострочувати; умовні («після завершення війни») теж, бо горизонт поза
  контролем того, хто обіцяв, і рахувати таке зривом нечесно;
- строк перевіряється З ГРЕЙСОМ за точністю дати: «до кінця 2025» не смикає
  1 січня о 00:00, а через два тижні;
- «давно не питали» — раз на тиждень, у понеділок: обіцянки без строку нікуди
  не біжать, а щоденне їх повторення привчає гортати не читаючи.
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
# запит і дочекатись відповіді, а не просто зафіксувати зрив постфактум.
SOON_DAYS = 7
# Скільки ПОВІДОМЛЕНЬ на день. Канал спільний з тендерами, документами влади й
# правоохоронцями: четверте повідомлення підряд про обіцянки — і його
# починають гортати. Решта не зникає, а приходить наступного дня.
DAILY_MAX = 3
# Скільки найважливіших лишаємо нагадувати при вмиканні. Три — щоб перше ж
# увімкнення показало формат на живих даних (правило 4), але не стало тим
# самим звалищем, від якого baseline і рятує.
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

# Підпис приводу для ПУЛЬТА (у самому повідомленні його немає — там стан
# написаний реченням).
REASON_WORD = {
    "overdue": "Строк минув",
    "soon": "Строк спливає цього тижня",
    "stale": "Без строку, давно не питали",
}

# Спосіб перевірки в повідомленні НЕ пишемо (Олег, 03.08: «зайва інфа»).
# Він лишається в черзі й в апці, де сортує теми за дешевизною перевірки, —
# але журналісту, який щойно прочитав обіцянку з цитатою, не треба пояснювати,
# що на місце можна доїхати.


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
    """Усе, що має право нагадати сьогодні, по приводах і без стелі."""
    rows = pp.list_queue(cur, limit=None, now=now)
    out = {}
    for kind in KINDS:
        if kind == "stale" and not weekly:
            continue
        seen = set() if ignore_sent else _already(cur, kind)
        picked = []
        for r in rows:
            if r["id"] in seen or not pp.rings(r) or r["class"] != kind:
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


def plan(weekly=False, now=None, ignore_sent=False, limit=None):
    """Що саме піде — і рівно це потім позначиться.

    Прев'ю і відправка ходять цією функцією разом: якщо їх розвести, кнопка
    «надіслати» перестає означати те, що людина щойно бачила.

    Повертає `items` — плаский список обіцянок, по одній на повідомлення,
    відсортований за пріоритетом СКРІЗЬ (а не всередині приводу): якщо
    прострочене вже неважливе, а завтрашній строк гарячий, першим має піти
    завтрашній.
    """
    now = now or int(time.time())
    limit = DAILY_MAX if limit is None else limit
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            due = _due(cur, weekly, now, ignore_sent)
            counts = {k: len(v) for k, v in due.items()}
            flat = []
            for kind, rows in due.items():
                for r in rows:
                    r["_kind"] = kind
                    flat.append(r)
            flat.sort(key=lambda r: -r["priority"])
            items = flat[:limit] if limit else flat
            held = len(flat) - len(items)

            first = {}
            if items:
                for rev in pp.revisions(cur, [r["id"] for r in items]):
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
    return {"items": items, "counts": counts, "held": held, "first": first,
            "links": links, "authors": authors, "images": images, "now": now}


# ---------- Baseline ----------

def baseline(now=None, keep=BASELINE_KEEP):
    """Позначити накопичене як «уже нагадували», лишивши `keep` найважливіших.

    Повертає (скільки притишено, скільки лишили). Самі обіцянки при цьому НЕ
    зникають і не змінюються — позначка живе окремою таблицею й означає лише
    «про це в канал уже казали».
    """
    now = now or int(time.time())
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        ensure_schema(conn)
        with conn.cursor() as cur:
            # weekly=True: притишуємо й «давно не питали», інакше найближчий
            # понеділок вивалить у канал усю давню купу.
            due = _due(cur, True, now, False)
            flat = []
            for kind, rows in due.items():
                for r in rows:
                    r["_kind"] = kind
                    flat.append(r)
            flat.sort(key=lambda r: -r["priority"])
            silenced = 0
            for r in flat[keep:]:
                cur.execute(
                    "INSERT INTO promise_reminders (commitment_id, kind, sent) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (r["id"], r["_kind"], now))
                silenced += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return silenced, min(keep, len(flat))


def _mark_one(commitment_id, kind, now):
    conn = ep.connect()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO promise_reminders (commitment_id, kind, sent) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (commitment_id, kind, now))
        conn.commit()
    finally:
        conn.close()


# ---------- Верстка одного повідомлення ----------

def _attr(value):
    """Екранування значення АТРИБУТА. `escape_html` лапок не чіпає, а og:image
    приїжджає зі сторонньої сторінки — однієї лапки досить, щоб розвалити
    розмітку всього повідомлення."""
    return escape_html(value).replace('"', "&quot;")


def status_line(row, now, mark=True):
    """Чому це прийшло саме зараз — одним реченням.

    Тут єдина підсвітка повідомлення. Формулювання чесні до того, що бот
    насправді знає: він не знає, поставили боларди чи ні — він знає, що строк
    вийшов, а редакція не поверталась.
    """
    def hi(text):
        return f"<mark>{text}</mark>" if mark else text

    kind = row.get("_kind")
    deadline = row.get("deadline")
    checked = int(row.get("checked_at") or 0)
    seen = int(row.get("last_seen") or 0)

    if kind == "overdue" and deadline:
        gap = pp.human_gap(now - int(deadline))
        tail = (f", востаннє перевіряли {pp.human_gap(now - checked)} тому"
                if checked else ", ніхто не перевіряв")
        return f"Обіцяли до {pp.fmt_date(deadline)}. {hi(f'Минуло {gap}')}{tail}."

    if kind == "soon" and deadline:
        left = pp.human_gap(int(deadline) - now)
        return (f"Обіцяли до {pp.fmt_date(deadline)}. "
                f"{hi(f'Лишилось {left}')}.")

    if checked and checked >= seen:
        return ("Строку не називали. "
                f"{hi(f'Востаннє перевіряли {pp.human_gap(now - checked)} тому')}.")
    if seen:
        return ("Строку не називали. "
                f"{hi(f'Востаннє писали про це {pp.human_gap(now - seen)} тому')}.")
    return "Строку не називали."


def _author_html(name):
    """Автор матеріалу з TG-хендлом, щоб його тегнуло автоматом.

    Резолвер один на бота — `fb_missing._team_tg` (він знає і `@username`, і
    форму `<a href="tg://user?id=…">` для тих, у кого хендла немає). Друга
    копія тут розійшлася б із першою, щойно хтось зміниться в TEAM.
    """
    if not name:
        return None
    try:
        from handlers.fb_missing import _team_tg
        tg = _team_tg(name)
    except Exception:
        tg = None
    # У TEAM трапляється буквальне «(тег за id)» — це нотатка для людини, а не
    # розмітка, і в повідомленні воно виглядало б як помилка.
    if tg and (tg.startswith("@") or tg.startswith("<a ")):
        return f"{escape_html(name)} — {tg}"
    return escape_html(name)


def _who(row, rev):
    """Підпис під цитатою: ім'я і посада на момент заяви. Посада важлива —
    «Віталій Луков» без неї нічого не каже читачеві поза редакцією."""
    name = (rev or {}).get("promiser_text") or row.get("owner_text")
    if not name:
        return None
    role = ((rev or {}).get("promiser_role") or "").strip()
    return f"{name}, {role}" if role and role.lower() not in name.lower() else name


def render_one(item, data):
    """Одне повідомлення = одна обіцянка. Rich-верстка (Bot API 10.1)."""
    now = data["now"]
    rev = data["first"].get(item["id"]) or {}
    aid = rev.get("article_id")
    link = data["links"].get(aid) or {}
    parts = []

    image = data["images"].get(aid)
    if image:
        # Без підпису: підпис дублював би заголовок матеріалу, який і так унизу.
        parts.append(f'<figure><img src="{_attr(image)}"/></figure>')

    parts.append("<p>🦊 Обіцянка, яку можна перевірити</p>")
    # <b> усередині <h3> свідомо: заголовок має бути видно вагою, а не лише
    # кеглем — у стрічці каналу поміж тендерів рядок ловиться саме жирним.
    parts.append(f"<h3><b>{escape_html(item.get('title') or '—')}</b></h3>")
    parts.append(f"<p>📅 {status_line(item, now)}</p>")

    quote = (rev.get("quote") or "").strip()
    if quote:
        who = _who(item, rev)
        cite = f"<cite>{escape_html(who)}</cite>" if who else ""
        parts.append(f"<blockquote>{escape_html(quote)}{cite}</blockquote>")

    if link.get("url"):
        src = (f'Джерело: <a href="{_attr(link["url"])}">'
               f'{escape_html(link.get("title") or "матеріал")}</a>')
        tail = [x for x in (_author_html(data["authors"].get(aid)),
                            escape_html(link.get("date") or "")) if x]
        if tail:
            src += "<br>" + " · ".join(tail)
        parts.append(f"<footer>{src}</footer>")
    return "".join(parts)


def render_one_plain(item, data):
    """Фолбек, коли rich не пройшов: та сама фактура, звичайним HTML."""
    now = data["now"]
    rev = data["first"].get(item["id"]) or {}
    aid = rev.get("article_id")
    link = data["links"].get(aid) or {}
    out = ["🦊 <i>Обіцянка, яку можна перевірити</i>", "",
           f"<b>{escape_html(item.get('title') or '—')}</b>",
           "📅 " + status_line(item, now, mark=False)]
    quote = (rev.get("quote") or "").strip()
    if quote:
        who = _who(item, rev)
        out.append("")
        out.append(f"<blockquote>{escape_html(quote)}</blockquote>")
        if who:
            out.append(f"<i>{escape_html(who)}</i>")
    if link.get("url"):
        tail = [x for x in (_author_html(data["authors"].get(aid)),
                            escape_html(link.get("date") or "")) if x]
        out.append("")
        out.append(f'Джерело: <a href="{_attr(link["url"])}">'
                   f'{escape_html(link.get("title") or "матеріал")}</a>'
                   + ("\n" + " · ".join(tail) if tail else ""))
    return "\n".join(out)


async def _keyboard(bot, commitment_id=None):
    app_url, _ = await resolve_app_link(bot)
    if not app_url:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    start = "promises" if commitment_id is None else f"promise_{commitment_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "Відкрити в банку тем", url=f"{app_url}?startapp={start}")]])


# ---------- Відправка ----------

async def send_one(bot, chat_id, item, data):
    from handlers import rich
    return await rich.send_rich(bot, chat_id, render_one(item, data),
                                fallback=render_one_plain(item, data),
                                reply_markup=await _keyboard(bot, item["id"]))


async def send_reminders(bot, weekly=False, chat_id=None, mark=True, data=None):
    """Нагадування в «винюхав», по одному повідомленню на обіцянку.
    Тихе, коли нагадувати нема про що. Повертає, скільки пішло."""
    if not bot_db.is_configured():
        return 0
    target = chat_id or CHAT_ID
    if not target:
        return 0
    if data is None:
        data = await asyncio.to_thread(plan, weekly, None, not mark)
    sent = 0
    for item in data["items"]:
        try:
            await send_one(bot, target, item, data)
        except Exception as e:
            # Одне невдале повідомлення не має з'їдати решту й не має
            # позначатись: наступного дня спробуємо знову.
            print(f"promise_reminders: не пішло {item['id']} — {e}")
            continue
        sent += 1
        # Позначка ОДРАЗУ після успішної відправки саме цього повідомлення:
        # інакше збій посеред пачки або з'їдав би решту назавжди, або слав би
        # її вдруге (урок report_reminders).
        if mark:
            await asyncio.to_thread(_mark_one, item["id"], item["_kind"],
                                    data["now"])
    return sent


async def daily(bot):
    """Щодня о 09:10. Понеділок додає «давно не питали».

    Мовчить, поки нагадування не увімкнені кнопкою: банк наповнювався сканами
    архіву, і без опт-іну перший же прогін вивалив би в канал сотні давніх
    прострочень.
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
    counts = data["counts"]
    if not counts:
        return "🦊 Нагадувати нема про що — про все, що мало, вже нагадали."
    lines = [f"🦊 <b>Обіцянки, які можна перевірити</b>",
             f"Щоденні нагадування: {'увімкнені, 09:10' if on else '<b>вимкнені</b>'}",
             ""]
    for kind in KINDS:
        n = counts.get(kind)
        if n:
            lines.append(f"{REASON_WORD[kind]} — <b>{n}</b>")
    n = len(data["items"])
    lines.append("")
    lines.append(f"Сьогодні пішло б <b>{n}</b> "
                 f"{pp.plural(n, 'повідомлення', 'повідомлення', 'повідомлень')}"
                 " — ті, які найпростіше перевірити."
                 + (f" Решта ({data['held']}) чекає на наступні дні."
                    if data["held"] else ""))
    return "\n".join(lines)


async def promise_remind_handler(update, context):
    """/promise_remind [all] — подивитись, що піде в канал, і вирішити.

    Нічого не шле у «винюхав» і не позначає: показує числа й самі повідомлення
    в ЦЕЙ чат, а відправка, вмикання й вимикання — кнопками. Саме тут живе
    обіцянка «контрольовано»: канал не дізнається про банк, поки не тапнули.
    """
    from handlers.promises import _allowed
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not _allowed(update):
        return
    args = context.args or []
    preview_all = bool(args) and args[0].lower() in ("all", "усе", "все", "test")
    msg = await update.message.reply_text("🦊 Дивлюсь, про що пора нагадати…")
    try:
        on = await asyncio.to_thread(is_on)
        data = await asyncio.to_thread(plan, True, None, preview_all)
        if not data["items"]:
            await msg.edit_text(
                _summary(data, on) + ("" if preview_all else
                                      "\n\nПоказати формат: /promise_remind all"),
                parse_mode="HTML")
            return

        rows = [[InlineKeyboardButton("📨 Надіслати в «винюхав»",
                                      callback_data="prm:send")]]
        if on:
            rows.append([InlineKeyboardButton("🔕 Вимкнути щоденні",
                                              callback_data="prm:off")])
        else:
            silence = sum(data["counts"].values()) - BASELINE_KEEP
            rows.append([InlineKeyboardButton(
                f"🔔 Увімкнути · про {max(0, silence)} давніх не нагадувати",
                callback_data="prm:on")])
        await msg.edit_text(_summary(data, on), parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(rows))
        for item in data["items"]:
            await send_one(context.bot, update.effective_chat.id, item, data)
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
        await q.edit_message_text("🦊 Розбираюсь із накопиченим…")
        silenced, kept = await asyncio.to_thread(baseline)
        await asyncio.to_thread(bot_db.set_state, ON_KEY, int(time.time()))
        sent = await send_reminders(context.bot, weekly=True)
        await q.edit_message_text(
            f"🔔 Нагадування увімкнено, щодня о 09:10 у «винюхав», "
            f"не більше {DAILY_MAX} на день.\n\n"
            f"Про <b>{silenced}</b> давніх нагадувати не буду — вони лишились "
            f"у банку й в апці, просто в канал про них не піде.\n"
            f"Лишив <b>{kept}</b>"
            + (f" — {sent} уже пішло в канал, перевір формат."
               if sent else " (у канал зараз нічого не пішло)."),
            parse_mode="HTML")
        return

    if action == "send":
        await q.edit_message_text("🦊 Надсилаю…")
        sent = await send_reminders(context.bot, weekly=True)
        await q.edit_message_text(
            f"📨 Пішло в «винюхав»: {sent}." if sent else
            "🦊 Не було чого слати — про все вже нагадали.")
