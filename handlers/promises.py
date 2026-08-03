"""
Банк тем — реєстр обіцянок і горизонтів поверх сутнісного шару.

Місто щодня щось обіцяє: добудувати, відремонтувати, запустити. Обіцянки тихо
переносяться, і ніхто не повертається перевірити. Цей модуль веде реєстр «що
кому обіцяли і на коли», показує ланцюг перенесень із лінком на кожен факт — і
готує ґрунт для сторожа, який штовхне редакцію, коли строк вийде.

Задум, розбори семи реальних статей і схема — `docs/PROMISES_BANK.md`.
Схема й похідні поля — `promise_pipeline.py` (без AI), витяг —
`promise_extract_api.py` + `promise_extract_prompt.md`.

**Поділ праці (§7).** Сесія розробки не має ні Telegram, ні прод-бази, тому
інструментом перевірки якості є КОМАНДА в боті: Олег ганяє, приносить вивід,
промпт правиться за фактурою. Звідси /promise_test — він нічого не пише.

Команди (whitelist ALLOWED_USER_IDS):
    /promise_test <id|URL>    — прогін витягу на одній реальній статті: що
                                САМЕ записалось би (усі поля + цитати +
                                рішення про ланцюг). НІЧОГО не пише
    /promise_estimate [з] [по] — read-only оцінка: скільки статей проходить
                                пре-фільтр і скільки коштуватиме Batch API
    /promise_scan <YYYY-MM> [all] — прогін ОДНОГО місяця (оцінка → кнопка →
                                батчі → звіт по темах). `all` — без
                                пре-фільтра, щоб заміряти його ціну (§7 крок 7)
    /promise_resume           — переприв'язати полінг після редеплою
    /promises [фільтр|слово]  — черга банку тем / пошук по об'єкту чи людині
    /promise_show <id|URL>    — картка: історія питання з лінками на факти
    /promise_checked <id>     — «перевірили»: тема йде вниз черги, з банку не
                                зникає (обіцянку можна перевірити й не писати)
    /promise_forget <id>      — прибрати помилковий запис (зі знімком у журнал)
    /promise_restore <id>     — повернути прибране зі знімка
    /promise_retest <id|URL>  — перечитати статтю після правки промпту

**Чого тут свідомо немає.** Нагадувань (§5) і екрана в апці (§6) — це наступні
кроки, коли реєстр наповниться і стане видно, що в ньому лежить. Формат картки
при цьому продуманий одразу: він однаковий у чаті й в апці, переписувати його
двічі дурість. Автоматичного закриття обіцянки як «виконано» без людини немає
і не буде.
"""

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import bot_db
from handlers.helpers import escape_html, extract_article_id
from handlers.notifier import notify_error
from handlers.storage import record_ai_usage

import entity_pipeline as ep
import promise_extract_api as api
import promise_pipeline as pp

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# Стан активного прогону — у норі, щоб переживав редеплой Railway (як
# entity_layer.STATE_KEY). Наявність ключа = є незавершений скан.
STATE_KEY = "promise_api_run"
POLL_INTERVAL = 60

# Ланцюг зшиває СУДДЯ, і його помилка дорога: злиті обіцянки роз'єднати важче,
# ніж не злити вчасно. Тому тут SMART-модель, а не Haiku — кандидати
# трапляються рідко (лише коли предмет уже є в банку), і на місяць це центи.
JUDGE_MODEL = "claude-sonnet-5"

QUEUE_PAGE = 8            # карток на екран /promises
QUOTE_CAP = 160           # цитата в списку; повна — у картці
CHAIN_CAP = 20            # кроків ланцюга в картці

_poll_running = {"flag": False}


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


def _load_state():
    raw = bot_db.get_state(STATE_KEY)
    return json.loads(raw) if raw else None


def _save_state(state):
    bot_db.set_state(STATE_KEY, json.dumps(state, ensure_ascii=False))


def _clear_state():
    bot_db.execute("DELETE FROM sync_state WHERE key = %s", (STATE_KEY,))


# ---------- Форматування (той самий формат, що піде в апку) ----------

CLASS_ICON = {"overdue": "⏰", "soon": "🗓", "waiting": "🔭",
              "stale": "💤", "noproof": "🫥", "open": "•", "closed": "✔️"}

METHOD_ICON = {"field_check": "👟", "document_request": "📄",
               "official_statement": "📞", "data": "📊"}

FILTER_WORDS = {
    "минув": "overdue", "прострочені": "overdue", "прострочене": "overdue",
    "скоро": "soon",
    "подія": "waiting", "події": "waiting", "чекає": "waiting",
    "давно": "stale", "мовчання": "stale",
    "популізм": "noproof", "нічим": "noproof",
    # «Нові» — не клас черги, а СОРТУВАННЯ за датою виявлення. Решта черги
    # шикується за терміновістю, і свіжознайдене в ній тоне: обіцянка, знайдена
    # сьогодні зі строком у грудні, стоїть нижче за прострочену торішню. Це
    # правильно для роботи й непридатно для «що бот приніс, поки я не дивився».
    "нові": "__fresh__", "нове": "__fresh__", "свіжі": "__fresh__",
    "усе": None, "все": None,
}
FRESH_DAYS = 7


def _state_text(row, now):
    cls = row["class"]
    if cls == "overdue":
        return f"⏰ Строк минув {pp.human_gap(now - int(row['deadline']))} тому"
    if cls == "soon":
        return f"🗓 Строк за {pp.human_gap(int(row['deadline']) - now)}"
    if cls == "waiting":
        return "🔭 Чекає події"
    if cls == "stale":
        silence = max(row.get("checked_at") or 0, row.get("last_seen") or 0)
        return f"💤 Не питали {pp.human_gap(now - silence)}"
    if cls == "noproof":
        return "🫥 Перевірити нічим"
    if cls == "closed":
        return f"✔️ {pp.STATE_WORD.get(row.get('status'), 'закрито')}"
    if row.get("deadline"):
        return f"🗓 Строк {pp.fmt_date(row['deadline'])}"
    return "🗓 Строку не дали"


def _how_text(row):
    method = row.get("verification_method")
    if not method:
        return None
    icon = METHOD_ICON.get(method, "•")
    return f"{icon} {pp.METHOD_WORD.get(method, method)}"


def _who_text(row, rev=None):
    """Хто обіцяв і хто переказав. Переказ — не виноска: саме до посередника
    редакція піде по відповідь, коли строк вийде (§2.3).

    Формулювання навмисно без дієслова в минулому часі: обіцяльником буває
    жінка, чоловік і безіменний колектив («власники зупинкового комплексу»),
    і «обіцяв» брехало б на двох випадках із трьох. «Хто обіцяв:» і
    «переказує» працюють для всіх.
    """
    parts = []
    promiser = (rev or {}).get("promiser_text") or row.get("owner_text")
    role = (rev or {}).get("promiser_role")
    if promiser:
        who = f"<b>{escape_html(promiser)}</b>"
        if role:
            who += f", {escape_html(role)}"
        parts.append("Хто обіцяв: " + who)
    elif row.get("actor_hidden"):
        parts.append("Хто саме — не названо")
    reported = (rev or {}).get("reported_by_text")
    if reported:
        parts.append(f"переказує <b>{escape_html(reported)}</b>")
    owner = row.get("owner_text")
    if owner and owner != promiser:
        parts.append(f"відповідає {escape_html(owner)}")
    return " · ".join(parts)


def _amount_text(amount):
    if not amount:
        return None
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}".replace(".", ",").rstrip("0").rstrip(",") + " млн грн"
    if amount >= 1000:
        return f"{amount / 1000:.0f} тис грн"
    return f"{amount:.0f} грн"


def format_item(row, rev=None, n=None, now=None):
    """Картка обіцянки списком. Той самий набір, що в макеті екрана (§6):
    стан · спосіб перевірки · дія · хто · ДОСЛІВНА цитата · метадані."""
    now = now or int(time.time())
    head = _state_text(row, now)
    how = _how_text(row)
    if how:
        head += f" · {how}"
    lines = [f"<b>{n}.</b> {head}" if n else head,
             f"<b>{escape_html(row['title'] or '—')}</b>"]
    who = _who_text(row, rev)
    if who:
        lines.append(who)
    quote = (rev or {}).get("quote")
    if quote:
        short = quote if len(quote) <= QUOTE_CAP else quote[:QUOTE_CAP] + "…"
        lines.append(f"<i>«{escape_html(short)}»</i>")
    if row.get("populism"):
        # Підказка, а не вирок, і ніколи без підстави: рішення брати тему в
        # роботу лишається за людиною, бот лише показує, чого в тексті немає.
        lines.append(f"🏷 <b>Схоже на популізм</b>: {escape_html(row['populism'])}")
    meta = []
    if row.get("deadline"):
        meta.append(f"строк {pp.fmt_date(row['deadline'])}")
    if row.get("trigger_event"):
        meta.append(f"розбудить: {escape_html(row['trigger_event'][:60])}")
    elif row.get("polarity") == "not_do":
        # Тригер не вигадуємо: якщо в тексті події не названо, чесно кажемо, як
        # ця обіцянка перевіряється взагалі.
        meta.append("перевіряється дією")
    if row.get("revisions"):
        meta.append(f"{row['revisions']} ревіз." if row["revisions"] > 1 else "1 ревізія")
    money = _amount_text(row.get("amount"))
    if money:
        meta.append(money)
    if row.get("audience") == "media":
        meta.append("обіцяно журналістам")
    if row.get("condition"):
        meta.append("умовна — не прострочується")
    meta.append(f"/promise_show {row['id']}")
    lines.append("<i>" + " · ".join(meta) + "</i>")
    return "\n".join(lines)


def _clip(text, limit=3900):
    """Обрізати довге повідомлення ПО МЕЖІ РЯДКА.

    Зріз рівно на ліміті Telegram рано чи пізно припадає всередину <b>…</b>, і
    замість картки людина отримує помилку парсингу. Кожен рядок тут закриває
    свої теги сам, тож межа рядка — єдине безпечне місце для ножа.
    """
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip() + "\n\n<i>…далі обрізано.</i>"


def _bounds_line(bounds, shown=None):
    """Межі даних — у КОЖНОМУ виводі (§6). Порожнеча має бути пояснена,
    інакше екран виглядає зламаним, а не порожнім.

    `shown` — скільки обіцянок під цим рядком. Без нього рядок одного разу вже
    збрехав: сказав «банк ще порожній» над списком із 19 обіцянок (сліди
    прогонів були, а от статей у норі — ні). Порожній банк і невідомі межі —
    різні речі, і плутати їх не можна.
    """
    if not bounds or not bounds.get("from"):
        if shown:
            return ("<i>Межі даних невідомі — сліди прогонів не збереглись. "
                    "Самі обіцянки на місці.</i>")
        return ("<i>Банк тем ще порожній — обіцянки збираються командою "
                "/promise_scan &lt;YYYY-MM&gt;.</i>")
    since = datetime.fromtimestamp(int(bounds["from"])).strftime("%m.%Y")
    return (f"<i>Обіцянки збираються з {since} · переглянуто "
            f"{bounds['articles']} матеріалів.</i>")


def _links_for(cur, ids):
    """Лінки на статті — ЗАВЖДИ через archive_search._fmt_item.

    Руками URL не збирати: на сайті вони мають вигляд /news/<рубрика>/<id>-<слаг>
    для новин і /articles/<слаг> для статей, а nikvesti.com/<id> не існує. У
    картці обіцянки лінк на кожну ревізію — головний доказ, і мертвий лінк
    убиває довіру до всього банку.
    """
    from handlers import archive_search

    ids = [int(i) for i in ids if i]
    if not ids:
        return {}
    cols = ("id", "published", "title_ua", "title_ru", "slug", "category", "kind")
    cur.execute(
        "SELECT id, published, title_ua, title_ru, slug, category, kind "
        "FROM articles WHERE id = ANY(%s)", (ids,))
    out = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        out[d["id"]] = archive_search._fmt_item(0, d)
    return out


# ---------- /promises ----------

def _queue_payload(arg):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            bounds = pp.data_bounds(cur)
            matched = []
            cls = None
            if arg and arg.lower() in FILTER_WORDS:
                cls = FILTER_WORDS[arg.lower()]
                if cls == "__fresh__":
                    cls = None
                    all_rows = pp.list_queue(cur, limit=None)
                    since = int(time.time()) - FRESH_DAYS * 86400
                    rows = sorted(
                        (r for r in all_rows if (r.get("first_seen") or 0) >= since),
                        key=lambda r: -(r.get("first_seen") or 0))
                else:
                    rows = pp.list_queue(cur, cls=cls, limit=None)
                    all_rows = rows if cls is None else pp.list_queue(cur, limit=None)
            elif arg:
                rows, matched = pp.search(cur, arg)
                all_rows = pp.list_queue(cur, limit=None)
            else:
                all_rows = pp.list_queue(cur, limit=None)
                rows = all_rows
            counts = pp.facet_counts(all_rows)
            head = rows[:QUEUE_PAGE]
            # Перша ревізія кожної обіцянки — джерело цитати для картки
            first = {}
            if head:
                cur.execute(
                    "SELECT DISTINCT ON (commitment_id) commitment_id, quote, "
                    "       promiser_text, promiser_role, reported_by_text "
                    "FROM commitment_revisions WHERE commitment_id = ANY(%s) "
                    "ORDER BY commitment_id, id", ([r["id"] for r in head],))
                for r in cur.fetchall():
                    first[r[0]] = {"quote": r[1], "promiser_text": r[2],
                                   "promiser_role": r[3], "reported_by_text": r[4]}
            return {"rows": head, "total": len(rows), "counts": counts,
                    "bounds": bounds, "first": first, "matched": matched,
                    "cls": cls}
    finally:
        conn.close()


async def promises_handler(update, context):
    """/promises [фільтр|слово] — черга банку тем або пошук.

    Головне — саме ЧЕРГА: журналіст, який відкриває банк тем, здебільшого не
    знає, кого шукати, він хоче «що горить сьогодні» (§6). Пошук — другий
    шлях, а не головний екран.
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора не налаштована (BOT_DATABASE_URL).")
        return
    arg = " ".join(context.args or []).strip()
    msg = await update.message.reply_text("🦊 Дивлюсь банк тем…")
    try:
        data = await asyncio.to_thread(_queue_payload, arg)
    except Exception as e:
        await msg.edit_text(f"❌ Банк тем недоступний: {e}")
        return

    counts, now = data["counts"], int(time.time())
    total = sum(counts.values())
    is_search = bool(arg) and arg.lower() not in FILTER_WORDS
    header = ["🦊 <b>Банк тем</b>"
              + (f" — пошук «{escape_html(arg)}»" if is_search else "")]
    if data["matched"]:
        header.append("Знайдено за сутностями: " + ", ".join(
            escape_html(m["name"]) for m in data["matched"][:5]))
    facets = " · ".join(
        f"{CLASS_ICON.get(c,'•')} {pp.CLASS_WORD[c]} <b>{counts.get(c,0)}</b>"
        for c in pp.CLASS_ORDER if counts.get(c))
    header.append(f"Усього в роботі: <b>{total}</b>" + (f"\n{facets}" if facets else ""))
    header.append(_bounds_line(data["bounds"], shown=total))

    body = [format_item(r, data["first"].get(r["id"]), n=i, now=now)
            for i, r in enumerate(data["rows"], start=1)]
    if not body:
        body = ["<i>За цим запитом порожньо.</i>"]
    tail = []
    if data["total"] > len(data["rows"]):
        tail.append(f"<i>Показано {len(data['rows'])} з {data['total']}.</i>")
    tail.append("<i>Фільтри: /promises нові · минув · скоро · подія · давно · популізм\n"
                "Пошук: /promises Сєнкевич · /promises гімназія</i>")
    await msg.edit_text(_clip("\n\n".join(header + body + tail)),
                        parse_mode="HTML", disable_web_page_preview=True)


# ---------- /promise_show ----------

def _card_payload(commitment_id=None, article_id=None):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            bounds = pp.data_bounds(cur)
            if article_id:
                rows = pp.by_article(cur, article_id)
                if not rows:
                    return {"bounds": bounds, "article": article_id, "rows": []}
                ids = [r["id"] for r in rows]
                revs = pp.revisions(cur, ids)
                links = _links_for(cur, {r["article_id"] for r in revs})
                return {"bounds": bounds, "article": article_id, "rows": rows,
                        "revisions": revs, "links": links}
            row = pp.get(cur, commitment_id)
            if not row:
                # Число могло бути id СТАТТІ, а не обіцянки: у чаті вони
                # виглядають однаково, і вимагати від людини памʼятати, який
                # номер що означає, — це рівно та зайва вимога, через яку
                # командою перестають користуватись. Пробуємо друге прочитання.
                rows = pp.by_article(cur, commitment_id)
                if rows:
                    revs = pp.revisions(cur, [r["id"] for r in rows])
                    links = _links_for(cur, {r["article_id"] for r in revs})
                    return {"bounds": bounds, "article": commitment_id,
                            "rows": rows, "revisions": revs, "links": links}
                return {"bounds": bounds, "row": None}
            siblings = pp.topic_commitments(cur, row["topic_id"], exclude=row["id"])
            ids = [row["id"]] + [s["id"] for s in siblings]
            revs = pp.revisions(cur, ids)
            links = _links_for(cur, {r["article_id"] for r in revs})
            topic = None
            if row["topic_id"]:
                cur.execute("SELECT title FROM topics WHERE id = %s", (row["topic_id"],))
                t = cur.fetchone()
                topic = t[0] if t else None
            cur.execute(
                "SELECT coalesce(e.name_ua, e.name_ru) FROM commitment_objects o "
                "JOIN entities e ON e.id = o.entity_id WHERE o.commitment_id = %s",
                (row["id"],))
            objects = [r[0] for r in cur.fetchall() if r[0]]
            return {"bounds": bounds, "row": row, "siblings": siblings,
                    "revisions": revs, "links": links, "topic": topic,
                    "objects": objects}
    finally:
        conn.close()


def _chain_lines(revs, links, by_commitment=None):
    """Ланцюг ревізій — це і є «історія питання»: усе, що казали про цю тему,
    у хронології, з лінком на КОЖЕН факт."""
    out = []
    for r in revs[:CHAIN_CAP]:
        when = pp.fmt_date(r.get("published"))
        mod = pp.MODALITY_WORD.get(r.get("modality"), r.get("modality") or "—")
        line = f"<b>{when}</b> · {mod.upper()}"
        if r.get("stated_deadline"):
            line += f" → строк {pp.fmt_date(r['stated_deadline'])}"
        out.append(line)
        out.append(f"<i>«{escape_html((r.get('quote') or '')[:400])}»</i>")
        src = []
        if r.get("promiser_text"):
            src.append(escape_html(r["promiser_text"]))
        if r.get("reported_by_text"):
            src.append("переказує " + escape_html(r["reported_by_text"]))
        stype = pp.SOURCE_WORD.get(r.get("source_type"))
        if stype:
            src.append(stype)
        if by_commitment and r.get("commitment_id") in by_commitment:
            src.append(by_commitment[r["commitment_id"]])
        link = links.get(r.get("article_id"))
        if link:
            src.append(f"<a href=\"{link['url']}\">матеріал</a>")
        if r.get("condition"):
            src.append("умова: " + escape_html(r["condition"][:80]))
        if src:
            out.append("<i>" + " · ".join(src) + "</i>")
        out.append("")
    if len(revs) > CHAIN_CAP:
        out.append(f"<i>…і ще {len(revs) - CHAIN_CAP} ревізій.</i>")
    return out


async def promise_show_handler(update, context):
    """/promise_show <id обіцянки | id або URL статті> — картка.

    Це та сама картка, що піде в апку (§6), тільки текстом: стан, ланцюг
    ревізій із датами, цитатами й лінками, решта зобов'язань тієї ж теми.
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Використання: /promise_show &lt;id обіцянки&gt;\n"
            "Або лінк чи id статті — покаже, що записано саме з неї.",
            parse_mode="HTML")
        return
    arg = args[0]
    article_id = None
    commitment_id = None
    if "/" in arg:
        article_id = extract_article_id(arg)
        if not article_id:
            await update.message.reply_text("З лінка не видно id матеріалу.")
            return
        article_id = int(article_id)
    elif arg.isdigit():
        commitment_id = int(arg)
    else:
        await update.message.reply_text("Потрібен id обіцянки або лінк на статтю.")
        return

    msg = await update.message.reply_text("🦊 Піднімаю історію питання…")
    try:
        data = await asyncio.to_thread(_card_payload, commitment_id, article_id)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        return

    now = int(time.time())
    if data.get("article") or (article_id and "rows" in data):
        article_id = data.get("article", article_id)
        # Режим §8: що записано з цієї статті (петля виправлень)
        if not data["rows"]:
            # Обіцянки може не бути з двох різних причин, і плутати їх не можна
            await msg.edit_text(
                f"🦊 З матеріалу {article_id} у банку тем нічого немає.\n"
                f"Це або «витяг тут зобов'язань не побачив», або «стаття ще не "
                f"проходила витяг» — перевірити: /promise_test {article_id}.\n\n"
                + _bounds_line(data["bounds"], shown=0), parse_mode="HTML")
            return
        parts = [f"🦊 <b>З матеріалу {article_id}</b> записано "
                 f"{len(data['rows'])} зобов'язан(ня):"]
        for i, r in enumerate(data["rows"], start=1):
            rev = next((x for x in data["revisions"]
                        if x["commitment_id"] == r["id"]
                        and x["article_id"] == article_id), None)
            parts.append(format_item(r, rev, n=i, now=now))
        parts.append(f"<i>Перечитати після правки промпту: /promise_retest {article_id}</i>")
        await msg.edit_text(_clip("\n\n".join(parts)), parse_mode="HTML",
                            disable_web_page_preview=True)
        return

    row = data["row"]
    if not row:
        await msg.edit_text(f"Обіцянки {commitment_id} немає. Черга — /promises")
        return

    tags = [_state_text(row, now)]
    money = _amount_text(row.get("amount"))
    if money:
        tags.append(money)
    how = _how_text(row)
    if how:
        tags.append(how)
    moved = max(0, (row.get("revisions") or 1) - 1)
    if moved:
        tags.append(f"переформульовано {moved} "
                    f"{pp.plural(moved, 'раз', 'рази', 'разів')}")
    if not row.get("rings"):
        tags.append("не дзвонить")

    parts = [f"🦊 <b>{escape_html(row['title'] or '—')}</b>",
             " · ".join(tags)]
    if data.get("topic"):
        parts.append(f"Тема: <b>{escape_html(data['topic'])}</b>")
    if data.get("objects"):
        parts.append("Об'єкти: " + ", ".join(escape_html(o) for o in data["objects"]))
    if row.get("criterion"):
        parts.append(f"Перевіряти за: {escape_html(row['criterion'])}")
    if row.get("trigger_event"):
        parts.append(f"Розбудить подія: {escape_html(row['trigger_event'])}")
    if row.get("condition"):
        cond = escape_html(row["condition"])
        if row.get("condition_self_judged"):
            cond += " <i>(межу проводить сам обіцяльник)</i>"
        parts.append(f"Умова: {cond}")
    if row.get("based_on_document"):
        parts.append(f"Підстава: {escape_html(row['based_on_document'])}")
    if row.get("populism"):
        parts.append(f"🏷 <b>Схоже на популізм</b>: {escape_html(row['populism'])}"
                     "\n<i>Це підказка, а не вирок: брати тему в роботу — "
                     "рішення людини.</i>")
    if row.get("actor_hidden"):
        parts.append("<i>У тексті безособова форма — актора названо поруч, "
                     "а не в самій обіцянці.</i>")
    if row.get("checked_at"):
        parts.append(f"<i>Перевіряли {pp.fmt_date(row['checked_at'])}"
                     + (f" — {escape_html(row['checked_by'])}" if row.get("checked_by") else "")
                     + "</i>")

    labels = {}
    if data["siblings"]:
        for s in data["siblings"]:
            labels[s["id"]] = f"інше зобов'язання теми → /promise_show {s['id']}"
    parts.append("<b>Історія питання:</b>")
    parts += _chain_lines(data["revisions"], data["links"], labels)
    if data["siblings"]:
        parts.append("<b>На цю ж тему:</b>\n" + "\n".join(
            f"• {escape_html(s['title'])} — /promise_show {s['id']}"
            for s in data["siblings"][:6]))
    parts.append(f"<i>Перевірили: /promise_checked {row['id']} · "
                 f"помилка: /promise_forget {row['id']}</i>")
    parts.append(_bounds_line(data["bounds"], shown=1))

    text = "\n\n".join(p for p in parts if p)
    await msg.edit_text(_clip(text), parse_mode="HTML", disable_web_page_preview=True)


# ---------- /promise_checked, /promise_forget, /promise_restore ----------

async def promise_checked_handler(update, context):
    """/promise_checked <id> — «перевірили»: тема йде вниз черги, але з банку
    НЕ зникає. Обіцянку можна перевірити й не писати про неї (§6)."""
    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Використання: /promise_checked <id>")
        return
    cid = int(args[0])
    who = update.effective_user.full_name if update.effective_user else None

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                if not row:
                    return None
                pp.mark_checked(cur, cid, who)
                return row
        finally:
            conn.close()

    row = await asyncio.to_thread(run)
    if not row:
        await update.message.reply_text(f"Обіцянки {cid} немає.")
        return
    await update.message.reply_text(
        f"🦊 Позначив перевіреною: {escape_html(row['title'] or '')}\n"
        f"<i>З банку не зникає — просто йде вниз черги.</i>",
        parse_mode="HTML")


async def promise_forget_handler(update, context):
    """/promise_forget <id> — прибрати помилковий запис.

    Зі знімком у журнал: реєстр буде помилятись, і правити його доведеться
    парами «лінк на реальну ситуацію + правило», тож кожна правка мусить бути
    зворотною (/promise_restore).
    """
    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Використання: /promise_forget <id> [причина]\n"
            "Повернути: /promise_restore <номер знімка>")
        return
    cid = int(args[0])
    reason = " ".join(args[1:]) or None
    who = update.effective_user.full_name if update.effective_user else None

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.forget(cur, cid, reason, who)
            conn.commit()
            return result
        finally:
            conn.close()

    result = await asyncio.to_thread(run)
    if not result:
        await update.message.reply_text(f"Обіцянки {cid} немає.")
        return
    await update.message.reply_text(
        f"🦊 Прибрав: {escape_html(result['commitment']['title'] or '')}\n"
        f"Ревізій знято: {result['revisions']}\n"
        f"Відкат: /promise_restore {result['purge_id']}", parse_mode="HTML")


async def promise_restore_handler(update, context):
    """/promise_restore <номер знімка> — повернути прибране."""
    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Використання: /promise_restore <номер знімка>")
        return
    pid = int(args[0])

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.restore(cur, pid)
            conn.commit()
            return result
        finally:
            conn.close()

    result = await asyncio.to_thread(run)
    if not result:
        await update.message.reply_text(f"Знімка {pid} немає.")
        return
    if result.get("already"):
        await update.message.reply_text("Цей знімок уже повертали.")
        return
    await update.message.reply_text(
        f"🦊 Повернув обіцянку {result['commitment_id']} "
        f"({result['revisions']} ревізій).\n/promise_show {result['commitment_id']}")


# ---------- /promise_prune — чистка від немиколаївських ----------
#
# Потрібна тому, що перший прогін місяця пішов по всіх статтях нори і банк
# набрався загальнонаціональним. Фільтр на вході вже стоїть — тут прибираємо
# набране. Показуємо ЧИСЛОМ і прикладами перед видаленням, як /entity_junk:
# сотні записів зникають одним тапом, і бачити треба до, а не після.

def _prune_scan():
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            st = pp.prune_scan(cur, api.REGION_MYKOLAIV)
            # Хто лишиться ПІСЛЯ чистки за регіоном. Це і є список кандидатів
            # для другої осі: регіон ловить рубрику, а не зміст, і чужий актор
            # у миколаївській статті переживає чистку спокійно.
            st["remaining"] = pp.top_promisers(cur, region=api.REGION_MYKOLAIV)
            return st
    finally:
        conn.close()


def _remaining_block(st):
    """Готові рядки другої осі. Без них «почисти за обіцяльником» означало б
    ходити чергою очима — рівно те, чого просили не робити."""
    if not st.get("remaining"):
        return []
    return ["", "<b>Хто обіцяє в миколаївських статтях</b> (лишиться після чистки):",
            *[f"{escape_html(w)} — {n} · <code>/promise_prune_who {escape_html(w)}</code>"
              for w, n in st["remaining"]],
            "<i>Це не список на видалення, а числа: загальнонаціональний актор "
            "потрапляє і в миколаївську статтю, і регіоном його не прибрати.</i>"]


async def promise_prune_handler(update, context):
    """/promise_prune — прибрати з банку обіцянки з немиколаївських статей."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Потрібна нора.")
        return
    msg = await update.message.reply_text("🦊 Рахую (нічого не видаляю)…")
    try:
        st = await asyncio.to_thread(_prune_scan)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not st["total"]:
        await msg.edit_text(_clip("\n".join([
            "🦊 Немиколаївських обіцянок у банку немає — прибирати нічого.",
            f"Змішаних (є і миколаївська ревізія): {st['mixed']} · "
            f"без відомого регіону: {st['unknown']} — ці лишаються.",
            *_remaining_block(st)])), parse_mode="HTML",
            disable_web_page_preview=True)
        return
    lines = ["🦊 <b>Чистка банку від немиколаївських</b>",
             f"Приберу зобов'язань: <b>{st['total']}</b>"]
    if st["promisers"]:
        lines.append("Хто обіцяв: " + " · ".join(
            f"{escape_html(w)} {n}" for w, n in st["promisers"]))
    if st["sample"]:
        lines.append("")
        lines += [f"• {escape_html(s['title'] or '—')}"
                  + (f" — {escape_html(s['promiser'])}" if s["promiser"] else "")
                  for s in st["sample"]]
    lines += ["",
              f"<i>Лишаються: {st['mixed']} зі змішаного ланцюга (є хоч одна "
              f"миколаївська ревізія) і {st['unknown']} без відомого регіону — "
              f"на «не знаємо» не видаляємо. Кожна прибрана лягає в журнал, "
              f"відкат усього прогону — /promise_prune_undo.</i>"]
    lines += _remaining_block(st)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        f"Прибрати {st['total']}", callback_data=f"ppr:{st['total']}")]])
    await msg.edit_text(_clip("\n".join(lines)), parse_mode="HTML",
                        reply_markup=kb, disable_web_page_preview=True)


async def promise_prune_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    who = update.effective_user.full_name if update.effective_user else None
    await query.edit_message_text("🦊 Прибираю…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.prune(cur, api.REGION_MYKOLAIV,
                                  reason="не Миколаїв", who=who)
            conn.commit()
            return result
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(run)
    except Exception as e:
        await query.edit_message_text(f"❌ Не прибралось: {e}")
        return
    await query.edit_message_text(
        f"🦊 Прибрав {result['removed']} зобов'язань"
        + (f", тем спорожніло {result['topics']}" if result["topics"] else "")
        + f".\nВідкат усього прогону: /promise_prune_undo {result['run']}")


# ---------- /promise_export і пакет рішень файлом ----------
#
# Той самий шлях, яким лікували ролі й картки сутностей, і з тієї ж причини:
# коли рішень сотні, по одному їх не натиснути. Бот віддає ВЕСЬ банк файлом,
# розбір іде поза ботом, назад приїжджає .txt із маркером — бот показує, що
# зробить, і чекає кнопку.
#
# Формат JSON, а не CSV: у обіцянки є вкладені ревізії з цитатами й лінками,
# і плаский рядок їх втрачає — а саме цитата відрізняє дубль від двох різних
# обіцянок про той самий об'єкт.

FIX_MARKER = "promises-fix"


def _export_payload():
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            rows = pp.export_all(cur)
            links = _links_for(cur, {r["article_id"] for row in rows
                                     for r in row["revisions_list"]
                                     if r.get("article_id")})
            bounds = pp.data_bounds(cur)
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "topic_id": r["topic_id"], "title": r["title"],
            "subject": r["subject"], "subject_key": r["subject_key"],
            "owner": r["owner_text"], "polarity": r["polarity"],
            "deadline": pp.fmt_date(r["deadline"]) if r["deadline"] else None,
            "deadline_precision": r["deadline_precision"],
            "modality": r["modality"], "source_type": r["source_type"],
            "verifiability": r["verifiability"], "class": r["class"],
            "verification_method": r["verification_method"],
            "criterion": r["criterion"], "based_on_document": r["based_on_document"],
            "condition": r["condition"], "trigger_event": r["trigger_event"],
            "amount": r["amount"], "populism": r["populism"],
            "checked_at": pp.fmt_date(r["checked_at"]) if r["checked_at"] else None,
            "revisions": [{
                "article_id": v["article_id"],
                "date": pp.fmt_date(v["published"]) if v["published"] else None,
                "article_title": v.get("art_title"),
                "url": (links.get(v["article_id"]) or {}).get("url"),
                "promiser": v["promiser_text"], "role": v["promiser_role"],
                "reported_by": v["reported_by_text"],
                "modality": v["modality"], "source_type": v["source_type"],
                "stated_deadline": (pp.fmt_date(v["stated_deadline"])
                                    if v["stated_deadline"] else None),
                "quote": v["quote"],
            } for v in r["revisions_list"]],
        })
    return out, bounds


async def promise_export_handler(update, context):
    """/promise_export — увесь банк тем одним JSON-файлом."""
    if not _allowed(update):
        return
    msg = await update.message.reply_text("🦊 Збираю…")
    try:
        rows, bounds = await asyncio.to_thread(_export_payload)
    except Exception as e:
        await msg.edit_text(f"❌ Не зібралось: {e}")
        return
    if not rows:
        await msg.edit_text("🦊 Банк порожній.")
        return
    payload = json.dumps(
        {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "bounds": bounds, "count": len(rows), "commitments": rows},
        ensure_ascii=False, indent=1).encode("utf-8")
    await msg.delete()
    await update.message.reply_document(
        document=BytesIO(payload), filename="promises.json",
        caption=(
            f"🦊 Банк тем: {len(rows)} зобов'язань з ревізіями, цитатами й "
            f"лінками.\n\nРозбирається поза ботом, назад — .txt із рядком "
            f"<code># promises-fix</code> і рішеннями:\n"
            f"<code>merge &lt;що лишається&gt; &lt;дубль&gt;</code> — склеїти "
            f"ланцюг (цитати обох лишаються)\n"
            f"<code>drop &lt;id&gt; [причина]</code> — прибрати запис\n"
            f"<code>check &lt;id&gt;</code> — позначити перевіреним\n\n"
            f"Покажу, що зроблю, і чекатиму кнопку. Усе зі знімками — "
            f"відкат /promise_prune_undo."),
        parse_mode="HTML")


def parse_fix(text):
    """Пакет рішень → список дій. Рядки, скопійовані зі звіту як
    `/promise_merge 12 34`, теж розуміються: людина копіює те, що бачить."""
    actions, errors = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip("/").replace("promise_", "", 1)
        parts = line.split()
        verb = parts[0].lower() if parts else ""
        try:
            if verb == "merge" and len(parts) >= 3:
                actions.append(("merge", int(parts[1]), int(parts[2]), None))
            elif verb in ("keep", "notdupe", "different") and len(parts) >= 3:
                actions.append(("keep", int(parts[1]), int(parts[2]), None))
            elif verb in ("drop", "forget") and len(parts) >= 2:
                actions.append(("drop", int(parts[1]), None,
                                " ".join(parts[2:]) or None))
            elif verb in ("check", "checked") and len(parts) >= 2:
                actions.append(("check", int(parts[1]), None, None))
            elif verb == "title" and len(parts) >= 3:
                # Переписати назву. Потрібне для битих назв (ієрогліф замість
                # слова) і для кривих формулювань: назва написана моделлю,
                # звіряти її нема з чим, тож правка людиною — єдиний шлях.
                actions.append(("title", int(parts[1]), None,
                                " ".join(parts[2:]).strip()))
            else:
                errors.append(raw.strip()[:80])
        except ValueError:
            errors.append(raw.strip()[:80])
    return actions, errors


def describe_fixes(actions):
    """Що саме зробить пакет — з НАЗВАМИ, а не самими id. Число «merge 218 164»
    людина перевірити не може, назви — може."""
    ids = {a[1] for a in actions} | {a[2] for a in actions if a[2]}
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM commitments WHERE id = ANY(%s)",
                        (list(ids),))
            names = dict(cur.fetchall())
    finally:
        conn.close()
    lines, counts, missing = [], {"merge": 0, "keep": 0, "drop": 0,
                                  "check": 0, "title": 0}, 0
    for verb, a, b, note in actions:
        na, nb = names.get(a), names.get(b)
        if na is None or (b is not None and nb is None):
            missing += 1
            continue
        counts[verb] += 1
        if verb == "merge":
            lines.append(f"🔗 {escape_html(na)}\n   ← {escape_html(nb)}")
        elif verb == "keep":
            lines.append(f"✂️ РІЗНІ (обидві лишаються): {escape_html(na)}\n"
                         f"   ↔ {escape_html(nb)}")
        elif verb == "title":
            lines.append(f"✏️ {escape_html(na)}\n   → {escape_html(note or '')}")
        elif verb == "drop":
            lines.append(f"🗑 {escape_html(na)}"
                         + (f" — {escape_html(note)}" if note else ""))
        else:
            lines.append(f"✔️ {escape_html(na)}")
    return lines, counts, missing


async def promises_fix_document(msg, text, who=None):
    """Пакет рішень .txt у приваті. Викликається з єдиного обробника
    документів, тому маркер перевіряє викликач."""
    actions, errors = parse_fix(text)
    if not actions:
        await msg.reply_text("🦊 Пакет порожній — жодного рядка не розібрав.\n"
                             + "\n".join(errors[:5]))
        return
    try:
        lines, counts, missing = await asyncio.to_thread(describe_fixes, actions)
    except Exception as e:
        await msg.reply_text(f"❌ Не змалював пакет: {type(e).__name__}: {e}")
        return
    if not lines:
        await msg.reply_text(
            f"🦊 Жодної з {len(actions)} обіцянок у банку немає — "
            f"схоже, пакет зібрано зі старого експорту.")
        return
    head = (f"🦊 <b>Пакет рішень</b>: склеїти {counts['merge']} · "
            f"розвести {counts['keep']} · прибрати {counts['drop']} · "
            f"переназвати {counts['title']} · позначити {counts['check']}")
    if missing:
        head += f"\n<i>{missing} рядків пропущено — таких id у банку немає.</i>"
    body = "\n".join(lines[:40])
    if len(lines) > 40:
        body += f"\n…і ще {len(lines) - 40} дій"
    if errors:
        body += "\n\n⚠️ не розібрав:\n" + "\n".join(escape_html(e) for e in errors[:5])
    key = f"promise_fix:{int(time.time())}"
    await asyncio.to_thread(
        bot_db.set_state, key, json.dumps(actions, ensure_ascii=False))
    await msg.reply_text(
        _clip(head + "\n\n" + body + "\n\n<i>Склеювання не видаляє цитат: "
              "ревізії другого запису переходять у перший. Усе зі знімками.</i>"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Виконати {len(lines)}",
                                 callback_data=f"pfx:{key}"),
            InlineKeyboardButton("❌ Ні", callback_data="pfx:no"),
        ]]))


async def promises_fix_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    key = query.data.split(":", 1)[1]
    if key == "no":
        await query.edit_message_text("Скасував — нічого не змінював.")
        return
    raw = await asyncio.to_thread(bot_db.get_state, key)
    if not raw:
        await query.edit_message_text("Пакет загубився — надішли файл ще раз.")
        return
    actions = json.loads(raw)
    who = update.effective_user.full_name if update.effective_user else None
    await query.edit_message_text("🦊 Виконую…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            done = {"merge": 0, "keep": 0, "drop": 0, "check": 0, "title": 0}
            skipped, already = [], 0
            with conn.cursor() as cur:
                run_id = pp.next_run(cur)
                for verb, a, b, note in actions:
                    try:
                        if verb == "merge":
                            ok = pp.merge_commitments(cur, a, b, who=who, run=run_id)
                            if not ok:
                                # Пакет ідемпотентний, і найчастіша причина
                                # «не спрацювало» — його вже застосували:
                                # програшної картки просто немає. Це не збій,
                                # і лякати ним не можна, інакше людина почне
                                # шукати поломку там, де все зроблено.
                                cur.execute("SELECT 1 FROM commitments WHERE id = %s",
                                            (b,))
                                if not cur.fetchone():
                                    already += 1
                                    continue
                        elif verb == "keep":
                            ok = pp.reject_pair(cur, a, b, who)
                        elif verb == "title":
                            ok = bool(pp.apply_glitch_fix(
                                cur, commitment_id=a, title=note))
                        elif verb == "drop":
                            ok = pp.forget(cur, a, reason=note or "пакет рішень",
                                           who=who, run=run_id)
                        else:
                            pp.mark_checked(cur, a, who)
                            ok = True
                    except Exception as e:
                        skipped.append(f"{verb} {a}: {type(e).__name__}")
                        continue
                    if ok:
                        done[verb] += 1
                    else:
                        skipped.append(f"{verb} {a}")
            conn.commit()
            return run_id, done, skipped, already
        finally:
            conn.close()

    try:
        run_id, done, skipped, already = await asyncio.to_thread(run)
    except Exception as e:
        await query.edit_message_text(f"❌ Не виконалось: {e}")
        return
    text = (f"🦊 Готово: склеїв {done['merge']} · розвів {done['keep']} · "
            f"прибрав {done['drop']} · переназвав {done['title']} · "
            f"позначив {done['check']}.\n"
            f"Відкат усього пакета: /promise_prune_undo {run_id}")
    if already:
        text += (f"\n\n✔️ {already} злиттів уже було зроблено раніше — "
                 f"пакет ідемпотентний, повторний прогін нічого не ламає.")
    if skipped:
        text += f"\n\n⚠️ пропущено {len(skipped)}: " + ", ".join(skipped[:5])
    await query.edit_message_text(text)


async def promise_dupes_handler(update, context):
    """/promise_dupes — та сама обіцянка, записана двічі.

    Число, яке пояснює, чому в банку 473 записи за місяць: суддя ланцюга
    спрацював 63 рази на 762 статті (пре-фільтр кандидатів шукає спільну
    КАРТКУ предмета, а «майданчик „Казка"» у сутнісному шарі не завжди є), і
    та сама обіцянка з двох статей лягла двома рядками.

    Зливати зручніше в апці — там обидві цитати поруч. Тут лише перелік і
    готові рядки: рішення однаково людське.
    """
    if not _allowed(update):
        return

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                return pp.dupe_count(cur), pp.dupe_pairs(cur, limit=12)
        finally:
            conn.close()

    msg = await update.message.reply_text("🦊 Шукаю…")
    try:
        total, pairs = await asyncio.to_thread(run)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not total:
        await msg.edit_text("🦊 Схожих пар не видно — банк не роздутий дублями.")
        return
    lines = [f"🦊 <b>Схоже на дублі: {total}</b>",
             "<i>однаковий строк + спільний предмет або обіцяльник. "
             "Злиття не видаляє: ревізії другого переходять у перший, "
             "обидві цитати лишаються доказами.</i>", ""]
    for p in pairs:
        lines += [f"• {escape_html(p['title_a'])}",
                  f"  {escape_html(p['title_b'])}",
                  f"  <i>схожість {p['sim']} · строк {pp.fmt_date(p['deadline'])}"
                  f"</i> — <code>/promise_merge {p['a']} {p['b']}</code>", ""]
    lines.append("<i>Зручніше — в апці: /team → Утиліти → Банк тем → «Схоже "
                 "на дублі». Там видно цитати обох.</i>")
    await msg.edit_text(_clip("\n".join(lines)), parse_mode="HTML",
                        disable_web_page_preview=True)


async def promise_vague_handler(update, context):
    """/promise_vague — назви, за якими обіцянку не впізнати.

    Віддає ФАЙЛОМ із готовими рядками `title <id> <нова назва>`: правити
    десятки назв по одній команді неможливо, а в файлі це п'ять хвилин. Назад
    той самий пакет `# promises-fix`.
    """
    if not _allowed(update):
        return
    msg = await update.message.reply_text("🦊 Дивлюсь назви…")

    def scan():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                rows = pp.vague_titles(cur)
                links = _links_for(cur, {r["article_id"] for r in rows})
            return rows, links
        finally:
            conn.close()

    try:
        rows, links = await asyncio.to_thread(scan)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not rows:
        await msg.edit_text("🦊 Загальних назв не видно.")
        return
    out = ["# promises-fix",
           "# Назви, за якими обіцянку не впізнати: у них немає ні цифри, ні",
           "# власної назви, ні назви в лапках — саме те слово, заради якого",
           "# новина існувала, витяг і викинув.",
           "#",
           "# Перепиши рядок після id і надішли файл назад. Рядки, які не",
           "# чіпав, можна лишити або видалити — назва не зміниться, якщо",
           "# вона збігається з нинішньою.", ""]
    for r in rows:
        link = links.get(r["article_id"]) or {}
        out += [f"# предмет: {r['subject'] or '—'}",
                f"# цитата: {(r['quote'] or '')[:180]}",
                f"# стаття: {link.get('title') or r['article_id']}",
                f"#          {link.get('url') or ''}",
                f"title {r['id']} {r['title']}", ""]
    payload = "\n".join(out).encode("utf-8")
    await msg.delete()
    await update.message.reply_document(
        document=BytesIO(payload), filename="promise-titles.txt",
        caption=(f"🦊 Назв без жодної конкретики: <b>{len(rows)}</b>\n\n"
                 f"Приклад того, що ловиться: «Винести проєкт рішення про "
                 f"виділення землі на розгляд депутатів повторно» — а йшлося "
                 f"про землю під модульні будинки для переселенців.\n\n"
                 f"Правиш рядки <code>title …</code> і кидаєш файл назад. "
                 f"Правило вже підтягнуто в промпт, тож нові такі назви мають "
                 f"з'являтись рідше."),
        parse_mode="HTML")


async def promise_glitches_handler(update, context):
    """/promise_glitches — записи з ієрогліфом замість українського слова.

    Не мохібейк і не биті дані сайту: 電 це «електрика», 續/続 — «триває».
    Модель підставляє ієрогліф замість слова, це витік мови в багатомовних
    моделях. Замір на червні: 4 записи з 473.

    ЦИТАТУ лікуємо самі — вона мусить бути дослівна, а текст статті лежить у
    норі, отже справжній фрагмент можна просто знайти. НАЗВУ звіряти нема з
    чим (її написала модель), тому такі записи показуємо окремо: там або
    правка людиною, або /promise_retest на статтю.
    """
    if not _allowed(update):
        return
    msg = await update.message.reply_text("🦊 Шукаю…")

    def scan():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                return pp.glitched(cur)
        finally:
            conn.close()

    try:
        rows = await asyncio.to_thread(scan)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not rows:
        await msg.edit_text("🦊 Ієрогліфів у банку немає.")
        return
    fixable = [r for r in rows if r["fixed_quote"]]
    manual = [r for r in rows if r["title_broken"]]
    lines = [f"🦊 <b>Ієрогліф замість слова: {len(rows)}</b>",
             "<i>續/続 = «триває», 電 = «електро». Це витік мови в моделі, "
             "а не биті дані сайту.</i>", ""]
    for r in rows[:10]:
        lines.append(f"<b>{r['commitment_id']}</b> · ст.{r['article_id']}")
        if r["title_broken"]:
            lines.append(f"  назва: {escape_html(r['title'])}")
        if r["fixed_quote"]:
            lines.append(f"  було: <i>{escape_html(r['quote'][:110])}</i>")
            lines.append(f"  стане: <b>{escape_html(r['fixed_quote'][:110])}</b>")
        elif pp.has_glitch(r["quote"]):
            lines.append(f"  цитата не лагодиться: <i>{escape_html(r['quote'][:110])}</i>")
        lines.append("")
    lines.append(f"Цитат лагодиться автоматично: <b>{len(fixable)}</b>")
    if manual:
        lines.append(f"Назв доведеться правити руками: <b>{len(manual)}</b> — "
                     f"кидаю файлом нижче.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        f"Полагодити {len(fixable)} цитат", callback_data="pgl:go")]]) if fixable else None
    await msg.edit_text(_clip("\n".join(lines)), parse_mode="HTML", reply_markup=kb)
    if not manual:
        return
    # Биту НАЗВУ автоматично не полагодити, і лишати людину з питанням «а що
    # тепер натискати» — теж не діло. Тому одразу файл того самого формату,
    # що /promise_vague: правиш рядок і кидаєш назад.
    out = ["# promises-fix",
           "# Ієрогліф стоїть замість українського слова (電 «електро»,",
           "# 續/続 «триває»). Перепиши назву після id — цитата поруч підкаже,",
           "# про що йшлося, — і надішли файл назад.", ""]
    for r in manual:
        out += [f"# цитата: {(r['quote'] or '')[:200]}",
                f"# стаття: {r['article_id']}",
                f"title {r['commitment_id']} {r['title']}", ""]
    await update.message.reply_document(
        document=BytesIO("\n".join(out).encode("utf-8")),
        filename="promise-glitch-titles.txt",
        caption="Правиш рядки <code>title …</code> і кидаєш назад.",
        parse_mode="HTML")


async def promise_glitches_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    await query.edit_message_text("🦊 Лагоджу…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                rows = pp.glitched(cur)
                n = 0
                for r in rows:
                    if r["fixed_quote"]:
                        pp.apply_glitch_fix(cur, revision_id=r["revision_id"],
                                            quote=r["fixed_quote"])
                        n += 1
            conn.commit()
            return n, sum(1 for r in rows if r["title_broken"])
        finally:
            conn.close()

    try:
        fixed, manual = await asyncio.to_thread(run)
    except Exception as e:
        await query.edit_message_text(f"❌ Не полагодилось: {e}")
        return
    text = f"🦊 Полагодив {fixed} цитат — текстом самих статей, без моделі."
    if manual:
        text += (f"\nЛишилось {manual} з битою НАЗВОЮ: її звіряти нема з чим, "
                 f"тому або /promise_retest <id статті>, або правка руками "
                 f"пакетом (<code>title &lt;id&gt; &lt;текст&gt;</code>).")
    await query.edit_message_text(text, parse_mode="HTML")


async def promise_notdupe_handler(update, context):
    """/promise_notdupe <id> <id> — «це різні», запам'ятати назавжди.

    Без аргументів показує, що вже розведено: помилкове «різні» ховає
    справжній дубль мовчки, і побачити свої рішення має бути де.
    """
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not all(a.isdigit() for a in args[:2]):

        def listing():
            conn = ep.connect()
            try:
                pp.ensure_schema(conn)
                conn.autocommit = True
                with conn.cursor() as cur:
                    return pp.rejected_pairs(cur)
            finally:
                conn.close()

        rows = await asyncio.to_thread(listing)
        if not rows:
            await update.message.reply_text(
                "Використання: /promise_notdupe &lt;id&gt; &lt;id&gt;\n"
                "Прибрати рішення — /promise_notdupe undo &lt;id&gt; &lt;id&gt;\n\n"
                "Поки нічого не розведено.", parse_mode="HTML")
            return
        lines = ["🦊 <b>Розведені пари</b> (детектор їх більше не показує)", ""]
        for r in rows:
            lines += [f"• {escape_html(r['title_a'] or ('#' + str(r['a'])))}",
                      f"  ↔ {escape_html(r['title_b'] or ('#' + str(r['b'])))}",
                      f"  <code>/promise_notdupe undo {r['a']} {r['b']}</code>", ""]
        await update.message.reply_text(_clip("\n".join(lines)), parse_mode="HTML")
        return
    undo = args[0].lower() == "undo"
    if undo:
        if len(args) < 3:
            await update.message.reply_text("Використання: /promise_notdupe undo <id> <id>")
            return
        a, b = int(args[1]), int(args[2])
    else:
        a, b = int(args[0]), int(args[1])
    who = update.effective_user.full_name if update.effective_user else None

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                ok = (pp.unreject_pair(cur, a, b) if undo
                      else pp.reject_pair(cur, a, b, who))
            conn.commit()
            return ok
        finally:
            conn.close()

    ok = await asyncio.to_thread(run)
    if undo:
        await update.message.reply_text(
            f"🦊 Пара {a}/{b} повернулась у детектор." if ok
            else f"Пари {a}/{b} серед розведених немає.")
        return
    await update.message.reply_text(
        f"🦊 Запам'ятав: {a} і {b} — різні. Детектор більше про них не спитає.\n"
        f"Передумав: /promise_notdupe undo {a} {b}")


async def promise_merge_handler(update, context):
    """/promise_merge <що лишається> <дубль> — склеїти ланцюг."""
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not all(a.isdigit() for a in args[:2]):
        await update.message.reply_text(
            "Використання: /promise_merge &lt;id що лишається&gt; &lt;id дубля&gt;\n"
            "Кандидатів показує /promise_dupes", parse_mode="HTML")
        return
    keep, dup = int(args[0]), int(args[1])
    who = update.effective_user.full_name if update.effective_user else None

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.merge_commitments(cur, keep, dup, who=who)
            conn.commit()
            return result
        finally:
            conn.close()

    result = await asyncio.to_thread(run)
    if not result:
        await update.message.reply_text("Однієї з обіцянок немає (або id однакові).")
        return
    await update.message.reply_text(
        f"🦊 Склеїв: {result['revisions']} ревізій з {dup} перейшли в {keep}.\n"
        f"Дивитись: /promise_show {keep}\n"
        f"Відкат: /promise_restore {result['purge_id']}")


# ---------- /promise_prune_who — чистка за обіцяльником ----------
#
# Друга вісь. Регіон ловить рубрику, а не зміст: «Зеленський пообіцяв виплати
# всім ВПО» може лежати в миколаївській статті через місцевий бек, і регіоном
# таке не приберемо ніколи. Вирішити, чи актор нам чужий, машина не може —
# Укрзалізниця завтра обіцятиме приміський поїзд на Миколаїв. Тому бот називає
# кандидатів числами, показує, що саме зникне, і чекає команду людини.
#
# Ім'я в callback_data не кладеться свідомо: там 64 байти, а кирилиця по два —
# «Кабінет Міністрів України» вже не влазить. Запит живе в норі під коротким
# ключем, тож і редеплой посеред підтвердження його не з'їдає.

def _who_key(who):
    return hashlib.md5(who.strip().lower().encode()).hexdigest()[:10]


def _prune_who_scan(who):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            return pp.prune_who_scan(cur, who, region=api.REGION_MYKOLAIV)
    finally:
        conn.close()


async def promise_prune_who_handler(update, context):
    """/promise_prune_who <обіцяльник> — прибрати обіцянки цього актора."""
    if not _allowed(update):
        return
    who = " ".join(context.args or []).strip()
    if len(who) < 3:
        await update.message.reply_text(
            "Використання: /promise_prune_who &lt;обіцяльник&gt;\n"
            "Напр.: <code>/promise_prune_who Зеленський</code>\n\n"
            "Збіг за підрядком — «Зеленський» забере й «Володимир Зеленський». "
            "Кого прибирати, підкаже /promise_prune: він показує топ "
            "обіцяльників банку.", parse_mode="HTML")
        return
    msg = await update.message.reply_text("🦊 Рахую (нічого не видаляю)…")
    try:
        st = await asyncio.to_thread(_prune_who_scan, who)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not st["total"]:
        await msg.edit_text(f"🦊 Обіцянок від «{escape_html(who)}» у банку немає.",
                            parse_mode="HTML")
        return
    lines = [f"🦊 <b>Прибрати обіцянки: {escape_html(who)}</b>",
             f"Зобов'язань: <b>{st['total']}</b>"]
    if st["writings"]:
        lines.append("Підпадають написання: " + " · ".join(
            f"{escape_html(w)} {n}" for w, n in st["writings"]))
    if st["local"]:
        # Найважливіший рядок екрана: підрядок міг захопити зайве, і саме
        # миколаївські записи — те, заради чого банк існує.
        lines.append(f"⚠️ З них <b>{st['local']}</b> із МИКОЛАЇВСЬКИХ статей — "
                     f"перевір написання перед тим, як тиснути.")
    if st["sample"]:
        lines.append("")
        lines += [f"• {escape_html(s['title'] or '—')}"
                  + (f" — {escape_html(s['promiser'])}" if s["promiser"] else "")
                  for s in st["sample"]]
    lines += ["", "<i>Кожна прибрана лягає в журнал; відкат усього прогону — "
                  "/promise_prune_undo.</i>"]
    key = _who_key(who)
    await asyncio.to_thread(bot_db.set_state, f"promise_prune_who:{key}", who)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        f"Прибрати {st['total']}", callback_data=f"ppw:{key}")]])
    await msg.edit_text(_clip("\n".join(lines)), parse_mode="HTML",
                        reply_markup=kb, disable_web_page_preview=True)


async def promise_prune_who_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    key = query.data.split(":", 1)[1]
    who = await asyncio.to_thread(bot_db.get_state, f"promise_prune_who:{key}")
    if not who:
        await query.edit_message_text(
            "Запит загубився — повтори /promise_prune_who.")
        return
    decided_by = update.effective_user.full_name if update.effective_user else None
    await query.edit_message_text("🦊 Прибираю…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.prune_who(cur, who, decided_by=decided_by)
            conn.commit()
            return result
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(run)
    except Exception as e:
        await query.edit_message_text(f"❌ Не прибралось: {e}")
        return
    await query.edit_message_text(
        f"🦊 Прибрав {result['removed']} зобов'язань «{who}»"
        + (f", тем спорожніло {result['topics']}" if result["topics"] else "")
        + f".\nВідкат усього прогону: /promise_prune_undo {result['run']}")


async def promise_prune_undo_handler(update, context):
    """/promise_prune_undo [прогін] — повернути весь прогін чистки."""
    if not _allowed(update):
        return
    args = context.args or []

    def runs():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                return pp.purge_runs(cur)
        finally:
            conn.close()

    if not args or not args[0].isdigit():
        rows = await asyncio.to_thread(runs)
        if not rows:
            await update.message.reply_text("Масових прибирань ще не було.")
            return
        lines = ["🦊 <b>Прогони прибирання</b>", ""]
        for r in rows:
            state = (f"повернуто {r['restored']} з {r['n']}"
                     if r["restored"] else f"{r['n']} зобов'язань")
            lines.append(f"<code>{r['run']}</code> — {pp.fmt_date(r['created'])} · "
                         f"{escape_html(r['reason'] or '')} · {state}")
        lines += ["", "<i>Повернути: /promise_prune_undo &lt;прогін&gt;</i>"]
        await update.message.reply_text(_clip("\n".join(lines)), parse_mode="HTML")
        return

    run_id = int(args[0])

    def undo():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                result = pp.prune_undo(cur, run_id)
            conn.commit()
            return result
        finally:
            conn.close()

    msg = await update.message.reply_text("🦊 Повертаю…")
    try:
        result = await asyncio.to_thread(undo)
    except Exception as e:
        await msg.edit_text(f"❌ Не повернулось: {e}")
        return
    if not result["snapshots"]:
        await msg.edit_text(f"Прогону {run_id} немає, або його вже повернули.")
        return
    await msg.edit_text(
        f"🦊 Повернув {result['restored']} із {result['snapshots']} зобов'язань "
        f"прогону {run_id}.")


# ---------- Витяг: спільне ядро ----------

def _extract_sync(articles):
    """Витяг зобов'язань звичайним API (не батч) — для /promise_test і
    /promise_retest. Повертає (results, errors, usage)."""
    import anthropic

    client = anthropic.Anthropic()
    results, errors = [], []
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    for art in articles:
        try:
            items, u = api.extract_one(client, art)
            results.append({"article": art, "commitments": items[:8]})
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_creation"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        except Exception as e:
            errors.append(f"{art['id']}: {type(e).__name__}: {e}")
            # Помилка в самому ЗАПИТІ (несхвалена схема, немає ключа, немає
            # моделі) валить усі статті однаково — ганяти решту означає лише
            # відкласти діагноз і обрізати його довгим списком копій.
            if len(errors) >= 2 and not results:
                errors.append(f"…решту {len(articles) - len(errors) + 1} статей "
                              f"пропущено: помилка не в тексті, а в запиті")
                break
    return results, errors, usage


_JUDGE_TOOL = {
    "name": "verdict",
    "description": "Чи це та сама обіцянка, що одна з наведених, чи нова.",
    "input_schema": {
        "type": "object",
        "properties": {
            "same_commitment_id": {
                "type": ["integer", "null"],
                "description": "id обіцянки, ЯКУ переформульовує нова згадка; "
                               "null — це самостійне зобов'язання",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high — те саме зобов'язання того самого актора "
                               "про той самий предмет; інакше medium/low",
            },
        },
        "required": ["same_commitment_id", "confidence"],
    },
}

JUDGE_RULES = (
    "Правила:\n"
    "• Та сама обіцянка — це те саме зобов'язання ТОГО САМОГО актора про ТОЙ "
    "САМИЙ предмет, просто переказане пізніше або з новою датою.\n"
    "• Різні дії щодо одного об'єкта — РІЗНІ обіцянки: реставрація, укриття й "
    "закупівля обладнання для тієї самої школи це три зобов'язання, а не три "
    "формулювання одного.\n"
    "• Інший актор — інша обіцянка, навіть якщо предмет той самий: підрядник, "
    "ОВА і Кабмін відповідають кожен за своє (це одна тема, але різні "
    "зобов'язання).\n"
    "• «Зробити» і «не робити» ніколи не одне й те саме.\n"
    "• confidence=high лише коли збіг очевидний. Сумніваєшся — null або low: "
    "не злити вчасно дешевше, ніж злити помилково."
)


def _judge_chain_sync(client, article, prepared, candidates):
    """Суддя ланцюга (§4 крок 2). Синхронний — увесь ingest іде одним потоком
    з одним з'єднанням до нори, і сунути сюди async нема сенсу."""
    lines = []
    for c in candidates:
        bits = [f"id={c['id']}", f"«{c['title']}»"]
        if c.get("promiser"):
            bits.append(f"обіцяв: {c['promiser']}")
        if c.get("deadline"):
            bits.append(f"строк {pp.fmt_date(c['deadline'])}")
        if c.get("quote"):
            bits.append(f"цитата: «{c['quote'][:160]}»")
        lines.append("- " + ", ".join(bits))
    new_bits = [f"«{prepared.get('title')}»", f"предмет: {prepared.get('subject')}"]
    if prepared.get("promiser"):
        new_bits.append(f"обіцяв: {prepared['promiser']}")
    if prepared.get("deadline"):
        new_bits.append(f"строк {pp.fmt_date(prepared['deadline'])}")
    prompt = (
        "Ти зшиваєш ланцюг обіцянок міської влади: та сама обіцянка, повторена "
        "через рік з новою датою, має лягти РЕВІЗІЄЮ до наявної, а не новим "
        "записом.\n\n"
        f"НОВА ЗГАДКА (стаття «{article.get('title_ua') or article.get('title_ru')}», "
        f"{article.get('published')}):\n"
        + ", ".join(new_bits) + "\n"
        f"Цитата: «{(prepared.get('quote') or '')[:300]}»\n\n"
        f"ВЖЕ В БАНКУ (по тому самому предмету):\n" + "\n".join(lines) + "\n\n"
        + JUDGE_RULES + "\nВиклич інструмент verdict."
    )
    msg = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=300,
        thinking={"type": "disabled"},
        tools=[_JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "verdict"},
        messages=[{"role": "user", "content": prompt}],
    )
    verdict = next((b.input for b in msg.content if b.type == "tool_use"), None) or {}
    known = {c["id"] for c in candidates}
    cid = verdict.get("same_commitment_id")
    if cid not in known:
        cid = None
    return {"commitment_id": cid, "confidence": verdict.get("confidence") or "low",
            "usage": msg.usage}


def ingest(results, judge=True, mark=True):
    """Злити результати витягу в банк тем.

    results — [{"article": payload, "commitments": [...]}] у будь-якому
    порядку; всередині сортується ХРОНОЛОГІЧНО, бо ланцюг будується вперед у
    часі: ревізія має лягати до вже наявної обіцянки, а не навпаки.

    Одне з'єднання на весь прогін і синхронний суддя: конект коштує ~99% часу
    запиту, а судимо ми рідко — лише коли предмет уже є в банку.
    """
    import anthropic

    client = None
    if judge:
        try:
            client = anthropic.Anthropic()
        except Exception as e:
            # Прогін уже оплачений — втратити його результати через відсутній
            # ключ не можна. Без судді зобов'язання просто ляжуть окремими
            # обіцянками: це видно і лікується руками, на відміну від тиші.
            print(f"promises: суддя ланцюга недоступний ({e}) — пишу без зшивання")
    stats = {"articles": 0, "new": 0, "revisions": 0, "dup": 0, "noquote": 0,
             "glitch": 0, "unverified": 0,
             "judged": 0, "judge_usage": {"input": 0, "output": 0}}
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            ordered = sorted(results,
                             key=lambda r: (r["article"].get("published") or "", r["article"]["id"]))
            marked = api.marked_ids(cur, [r["article"]["id"] for r in ordered])
            for res in ordered:
                art = res["article"]
                stats["articles"] += 1
                found = glitches = 0
                for item in res["commitments"]:
                    prepared = pp.prepare(cur, item)
                    if not (prepared.get("quote") or "").strip():
                        stats["noquote"] += 1
                        continue
                    cands = pp.candidates(cur, prepared)
                    cid, conf = None, None
                    if cands and client:
                        try:
                            v = _judge_chain_sync(client, art, prepared, cands)
                            stats["judged"] += 1
                            stats["judge_usage"]["input"] += v["usage"].input_tokens or 0
                            stats["judge_usage"]["output"] += v["usage"].output_tokens or 0
                            conf = v["confidence"]
                            if conf == "high":
                                cid = v["commitment_id"]
                        except Exception as e:
                            # Збій судді не має губити зобов'язання: пишемо
                            # окремою обіцянкою — це видно і виправляється
                            # руками, на відміну від мовчазної втрати.
                            print(f"promises: суддя впав на статті {art['id']} — {e}")
                    _, outcome = pp.record(cur, art, prepared, cid, conf)
                    if outcome == "new":
                        stats["new"] += 1
                        found += 1
                    elif outcome == "revision":
                        stats["revisions"] += 1
                        found += 1
                    elif outcome == "dup":
                        stats["dup"] += 1
                    elif outcome == "glitch":
                        # Ієрогліф замість українського слова (витік мови в
                        # моделі), який не вдалось полагодити з тексту статті.
                        # Не пишемо і НЕ закриваємо статтю: це шум семплінгу,
                        # наступна спроба майже напевно дасть чистий текст.
                        stats["glitch"] += 1
                        glitches += 1
                if mark:
                    glitched = bool(glitches) and not found
                    pp.mark_attempt(
                        cur, art["id"], marked=art["id"] in marked,
                        error="витік чужої мови в генерації" if glitched else None,
                        done=not glitched, found=found)
                conn.commit()
    finally:
        conn.close()
    if stats["judge_usage"]["input"]:
        record_ai_usage(JUDGE_MODEL,
                        input_tokens=stats["judge_usage"]["input"],
                        output_tokens=stats["judge_usage"]["output"])
    return stats


# ---------- /promise_test ----------

def _test_payload(article_id):
    """Стаття + витяг + що САМЕ записалось би. У базу не йде нічого."""
    arts = api.fetch_ids([article_id])
    if not arts:
        return {"missing": True}
    art = arts[0]
    results, errors, usage = _extract_sync([art])
    items = results[0]["commitments"] if results else []
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            prepared = []
            for item in items:
                p = pp.prepare(cur, item)
                cands = pp.candidates(cur, p)
                prepared.append({"item": p, "candidates": cands})
            links = _links_for(cur, [article_id])
            marked = bool(api.marked_ids(cur, [article_id]))
    finally:
        conn.close()
    return {"article": art, "prepared": prepared, "errors": errors,
            "usage": usage, "link": links.get(int(article_id)), "marked": marked}


def _fmt_test_item(p, n):
    item, cands = p["item"], p["candidates"]
    lines = [f"<b>{n}. {escape_html(item.get('title') or '—')}</b>"]
    lines.append(f"предмет: {escape_html(item.get('subject') or '—')}"
                 + (f" → картка #{item['subject_entity_id']}"
                    if item.get("subject_entity_id") else " → картки немає (ключ по тексту)"))
    who = []
    if item.get("promiser"):
        who.append(escape_html(item["promiser"])
                   + (f" ({escape_html(item['promiser_role'])})" if item.get("promiser_role") else "")
                   + (f" #{item['promiser_entity_id']}" if item.get("promiser_entity_id") else " ✗картки"))
    if item.get("reported_by"):
        who.append("переказує " + escape_html(item["reported_by"]))
    if item.get("owner"):
        who.append("відповідає " + escape_html(item["owner"]))
    if who:
        lines.append("хто: " + " · ".join(who))
    lines.append(
        f"модальність: <b>{pp.MODALITY_WORD.get(item.get('modality'), '—')}</b>"
        f" · джерело: {pp.SOURCE_WORD.get(item.get('source_type'), '—')}"
        f" · {'НЕ робити' if item.get('polarity') == 'not_do' else 'зробити'}")
    lines.append(
        f"строк: {pp.fmt_date(item['deadline']) if item.get('deadline') else '—'}"
        f" ({item.get('deadline_precision') or 'немає'})"
        f" · клас: <b>{item.get('verifiability')}</b>"
        f" · перевірка: {pp.METHOD_WORD.get(item.get('verification_method'), '—')}")
    if item.get("criterion"):
        lines.append("критерій: " + escape_html(item["criterion"]))
    else:
        lines.append("критерій: <b>немає</b>")
    if item.get("condition"):
        lines.append("умова: " + escape_html(item["condition"])
                     + (" (самооцінювана)" if item.get("condition_self_judged") else ""))
    if item.get("trigger_event"):
        lines.append("тригер: " + escape_html(item["trigger_event"]))
    if item.get("based_on_document"):
        lines.append("підстава: " + escape_html(item["based_on_document"]))
    if item.get("amount"):
        lines.append("сума: " + (_amount_text(item["amount"]) or "—"))
    flags = []
    if item.get("actor_hidden"):
        flags.append("актор захований")
    if item.get("framed_as_promise"):
        flags.append("заголовок назвав обіцянкою")
    if item.get("audience"):
        flags.append(f"адресат: {item['audience']}")
    if flags:
        lines.append("прапорці: " + ", ".join(flags))
    reason = pp.populism_reason(item)
    if reason:
        lines.append(f"🏷 <b>схоже на популізм</b>: {escape_html(reason)}")
    lines.append(f"<i>«{escape_html((item.get('quote') or '')[:300])}»</i>")
    if not (item.get("quote") or "").strip():
        lines.append("⚠️ <b>без цитати — такий запис у банк НЕ пишеться</b>")
    if cands:
        lines.append("кандидати ланцюга: " + ", ".join(
            f"#{c['id']} «{escape_html((c['title'] or '')[:40])}»" for c in cands[:3]))
    return "\n".join(lines)


async def promise_test_handler(update, context):
    """/promise_test <id|URL> — прогін витягу на одній РЕАЛЬНІЙ статті.

    Друкує, що саме записалось би — усі поля, похідний клас перевірки, цитати,
    кандидати ланцюга — і НЕ ПИШЕ НІЧОГО. Це інструмент розробки: Олег ганяє,
    приносить вивід, промпт правиться за фактурою (§7).
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        await update.message.reply_text(
            "🦊 Потрібні нора (BOT_DATABASE_URL) і ANTHROPIC_API_KEY.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Використання: /promise_test &lt;id або URL матеріалу&gt;\n"
            "Еталони: 294413 · 320092 · 311271 · 320276 · 317853 · 321833 · 312757",
            parse_mode="HTML")
        return
    raw = args[0]
    aid = extract_article_id(raw) if "/" in raw else (raw if raw.isdigit() else None)
    if not aid:
        await update.message.reply_text("Не видно id матеріалу.")
        return

    msg = await update.message.reply_text("🦊 Читаю статтю і шукаю зобов'язання…")
    try:
        data = await asyncio.to_thread(_test_payload, int(aid))
    except Exception as e:
        await msg.edit_text(f"❌ Витяг не вдався: {type(e).__name__}: {e}")
        return
    if data.get("missing"):
        await msg.edit_text(
            f"🦊 Статті {aid} у норі немає — спершу /nora_resync {aid}.")
        return

    usage = data["usage"]
    record_ai_usage(api.MODEL, input_tokens=usage["input"], output_tokens=usage["output"],
                    cache_read=usage["cache_read"], cache_creation=usage["cache_creation"])
    art = data["article"]
    title = art.get("title_ua") or art.get("title_ru") or "—"
    head = [f"🦊 <b>{escape_html(title[:120])}</b>",
            f"{art.get('published')} ({art.get('weekday')}) · id {art['id']}"
            + (f" · <a href=\"{data['link']['url']}\">матеріал</a>" if data.get("link") else "")]
    if not data["marked"]:
        head.append("⚠️ <i>Стаття НЕ проходить пре-фільтр за маркерами — у "
                    "масовий скан вона б не потрапила.</i>")
    if data["errors"]:
        head.append("⚠️ " + escape_html("; ".join(data["errors"])))

    body = [_fmt_test_item(p, i) for i, p in enumerate(data["prepared"], start=1)]
    if not body:
        body = ["<i>Зобов'язань не знайдено. Для більшості новин це нормальний "
                "результат — але якщо ти бачиш їх очима, це матеріал для правки "
                "промпту.</i>"]
    cost = (usage["input"] * api.PRICE_IN + usage["output"] * api.PRICE_OUT)
    tail = [f"<i>Тест — у банк не записано нічого. ≈ ${cost:.4f}</i>"]
    await msg.edit_text(_clip("\n\n".join(head + body + tail)),
                        parse_mode="HTML", disable_web_page_preview=True)


# ---------- Авто-інкремент: банк росте сам ----------
#
# Рішення Олега 03.08: ретроспектива на роки не потрібна. Досить прогнати
# місяць-другий назад, а далі банк має наповнюватись сам зі свіжого потоку
# нори — так само, як це робить сутнісний шар.
#
# Три речі взяті звідти без змін, бо кожна вже оплачена інцидентом:
#   • відбір за ФАКТОМ відсутності розбору, а не курсором — стаття, чий витяг
#     упав, повертається наступної години (курсорна схема втратила 403 статті
#     і мовчала три тижні);
#   • стеля спроб, щоб одна бита нода не крутилась вічно (pp.MAX_ATTEMPTS);
#   • підлога, нижче якої не заглядаємо: там територія ручного /promise_scan.
#
# А ось пре-фільтр за маркерами тут СВІДОМО ВИМКНЕНО. Замір 03.08 показав, що
# він пропускає 67% статей, тобто на денному потоці (~40 матеріалів) економить
# близько двох центів — і за ці два центи може назавжди втратити обіцянку без
# слова «обіцяв» (§2.5). Побічний виграш: `promise_attempts.marked` усе одно
# заповнюється, тож ціна фільтра тепер міряється САМА на живому потоці, без
# окремого платного прогону.
#
# Зате інший фільтр тут увімкнено назавжди — РЕГІОН (api.REGION_MYKOLAIV). Він
# коштує нуль і відсікає те, чого редакція не перевіряє в принципі: перший
# прогін місяця без нього привів у банк Зеленського, Укрзалізницю й вокзал
# Одеси, які в черзі просто витісняли миколаївське.

INCR_KEY = "promise_incr_on"        # наявність ключа = інкремент увімкнено
INCR_FLOOR_KEY = "promise_incr_floor"   # unix: глибше не заглядаємо
SCAN_FLOOR_KEY = "promise_scan_floor"   # початок найранішого ручного скану
# Скільки статей у черзі — це вже не «інкремент», а прихований бекфіл за
# гроші. Вмикати мовчки не можна: на 132к статей це $790.
INCR_SANITY_MAX = 3000
INCR_MAX_PER_RUN = 60               # ~40 нових матеріалів на добу, запас удвічі

PENDING_SQL = """
SELECT a.id, a.published, a.title_ua, a.title_ru, a.text_ua, a.text_ru
FROM articles a
LEFT JOIN promise_attempts t ON t.article_id = a.id
WHERE a.published >= %s
  AND a.region = %s
  AND coalesce(t.done, false) = false
  AND coalesce(t.attempts, 0) < %s
ORDER BY a.published DESC
LIMIT %s
"""

PENDING_COUNT_SQL = """
SELECT count(*) AS n
FROM articles a
LEFT JOIN promise_attempts t ON t.article_id = a.id
WHERE a.published >= %s
  AND a.region = %s
  AND coalesce(t.done, false) = false
  AND coalesce(t.attempts, 0) < %s
"""


def _incr_floor(explicit=None):
    """Підлога добору: глибше інкремент не заглядає ніколи.

    Раніше вона рахувалась як «найдавніша стаття, якої колись торкався
    витяг» — і це виявилось катастрофічно крихким. Один `/promise_retest 92`
    на статтю 2009 року відкотив підлогу на СІМНАДЦЯТЬ років: у черзі
    опинилось 132 387 статей, тобто ≈$790 і три місяці роботи. Помилки такого
    класу не можна лікувати «акуратніше рахувати мінімум» — треба прибрати
    саму можливість.

    Тепер підлога береться лише з ЯВНИХ джерел, і жодне з них не рухається
    від діагностики:
      1. дата, яку назвала людина (`/promise_increment_on 2026-06-01`);
      2. початок найранішого РУЧНОГО скану (`/promise_scan YYYY-MM` пише його
         сам) — інкремент природно продовжує те, що вже оплачено;
      3. «відсьогодні», якщо сканів не було. Це саме те, чого просили:
         автоперевірка НОВИХ матеріалів, а не архіву.
    """
    if explicit:
        bot_db.set_state(INCR_FLOOR_KEY, int(explicit))
        return int(explicit)
    raw = bot_db.get_state(INCR_FLOOR_KEY)
    if raw is not None:
        return int(raw)
    scanned = bot_db.get_state(SCAN_FLOOR_KEY)
    floor = int(scanned) if scanned else int(time.time())
    bot_db.set_state(INCR_FLOOR_KEY, floor)
    return floor


def _note_scan_floor(from_date):
    """Запам'ятати початок найранішого ручного скану. Саме звідси інкремент
    дізнається, докуди вглиб уже заплачено."""
    ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp())
    cur = bot_db.get_state(SCAN_FLOOR_KEY)
    if cur is None or ts < int(cur):
        bot_db.set_state(SCAN_FLOOR_KEY, ts)


def _pending(floor, limit=None):
    if limit is None:
        rows = bot_db.query(PENDING_COUNT_SQL,
                            (floor, api.REGION_MYKOLAIV, pp.MAX_ATTEMPTS))
        return rows[0]["n"] if rows else 0
    return bot_db.query(PENDING_SQL,
                        (floor, api.REGION_MYKOLAIV, pp.MAX_ATTEMPTS, limit))


async def sync_promises_incremental(bot):
    """Щогодини :25 — свіжі статті нори через витяг зобов'язань.

    Тихий і опт-ін (/promise_increment_on). Час обрано після сутнісного шару
    (:55 попередньої години): витяг обіцянок резолвить предмет і обіцяльника в
    картки сутностей, і якщо карток ще немає, ланцюг і тема зшиваються гірше.
    """
    if await asyncio.to_thread(bot_db.get_state, INCR_KEY) is None:
        return
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        return
    if await asyncio.to_thread(_load_state):
        return          # іде ручний скан — не конкуруємо, доженемо за годину
    try:
        await asyncio.to_thread(pp.ensure_schema)
        floor = await asyncio.to_thread(_incr_floor)
        rows = await asyncio.to_thread(_pending, floor, INCR_MAX_PER_RUN)
        if not rows:
            return
        arts = [api.article_payload(
            (r["id"], r["published"], r["title_ua"], r["title_ru"],
             r["text_ua"], r["text_ru"])) for r in rows]
        results, errors, usage = await asyncio.to_thread(_extract_sync, arts)
        if results:
            await asyncio.to_thread(ingest, results)
        if usage["input"]:
            record_ai_usage(api.MODEL, input_tokens=usage["input"],
                            output_tokens=usage["output"],
                            cache_read=usage["cache_read"],
                            cache_creation=usage["cache_creation"])

        def mark_failures():
            conn = ep.connect()
            try:
                with conn.cursor() as cur:
                    for err in errors:
                        aid = err.split(":")[0].strip()
                        if aid.isdigit():
                            pp.mark_attempt(cur, int(aid), error=err[:200], done=False)
                conn.commit()
            finally:
                conn.close()

        if errors:
            await asyncio.to_thread(mark_failures)
        # Сторож масового збою — той самий поріг, що в сутнісному шарі: якщо
        # впала третина пачки, це не «погана стаття», це щось зламалось.
        if errors and len(errors) * 3 >= len(arts):
            raise RuntimeError(
                f"інкремент банку тем: {len(errors)}/{len(arts)} збоїв; "
                f"перший — {errors[0][:200]}")
    except Exception as e:
        await notify_error(bot, "інкремент банку тем", e)


async def promise_increment_on_handler(update, context):
    """/promise_increment_on [YYYY-MM-DD] — щогодинний добір свіжих статей.

    Дата — підлога: глибше не заглядаємо. Без неї береться початок
    найранішого ручного скану, а якщо сканів не було — сьогодні.
    """
    if not _allowed(update):
        return
    args = context.args or []
    explicit = None
    if args:
        try:
            explicit = int(datetime.strptime(args[0], "%Y-%m-%d").timestamp())
        except ValueError:
            await update.message.reply_text(
                "Формат дати: /promise_increment_on 2026-06-01")
            return
    already = await asyncio.to_thread(bot_db.get_state, INCR_KEY) is not None
    if already and not explicit:
        floor = await asyncio.to_thread(_incr_floor)
        await update.message.reply_text(
            f"Інкремент уже увімкнено (від {pp.fmt_date(floor)}). "
            f"Змінити підлогу — /promise_increment_on YYYY-MM-DD, "
            f"вимкнути — /promise_increment_off")
        return
    await asyncio.to_thread(pp.ensure_schema)
    floor = await asyncio.to_thread(_incr_floor, explicit)
    pending = await asyncio.to_thread(_pending, floor)
    # Запобіжник. Тиха катастрофа 03.08: підлога рахувалась як «найдавніша
    # стаття, якої торкався витяг», один /promise_retest на ноду 2009 року
    # відкотив її на 17 років, і в черзі опинилось 132 387 статей — ≈$790.
    # Число мусить бути ПЕРЕД очима, а не в /promise_status постфактум.
    if pending > INCR_SANITY_MAX:
        await update.message.reply_text(
            f"⛔ Не вмикаю: у черзі <b>{pending}</b> статей від "
            f"{pp.fmt_date(floor)} — це ≈${pending * 0.006:.0f} і "
            f"{pending // (INCR_MAX_PER_RUN * 24) + 1} днів роботи. Це вже не "
            f"добір свіжого, а бекфіл за гроші.\n\n"
            f"Якщо треба саме свіже: <code>/promise_increment_on "
            f"{datetime.now().strftime('%Y-%m-%d')}</code>\n"
            f"Якщо справді потрібна глибина — назви дату явно, і я ввімкну.\n\n"
            f"<i>Свіже при цьому не чекало б у кінці черги — добір іде від "
            f"найновішого. Але платити за архів помісячно дешевше батчами "
            f"(/promise_scan), ніж поштучно.</i>",
            parse_mode="HTML")
        return
    await asyncio.to_thread(bot_db.set_state, INCR_KEY, int(time.time()))
    await update.message.reply_text(
        f"🦊 Авто-інкремент банку тем увімкнено (від {pp.fmt_date(floor)}).\n"
        f"У черзі {pending} статей — щогодини о :25 бере до {INCR_MAX_PER_RUN}"
        f" (≈${pending * 0.006:.2f} на всю чергу).\n"
        f"Пре-фільтр тут вимкнено свідомо: на денному потоці він економив би "
        f"центи, а втратити обіцянку може назавжди.\n"
        f"Вартість видно в /aicost, стан — /promise_status.")


async def promise_increment_off_handler(update, context):
    if not _allowed(update):
        return
    if await asyncio.to_thread(bot_db.get_state, INCR_KEY) is None:
        await update.message.reply_text("Інкремент і так вимкнено.")
        return
    await asyncio.to_thread(
        bot_db.execute, "DELETE FROM sync_state WHERE key = %s", (INCR_KEY,))
    await update.message.reply_text(
        "Авто-інкремент банку тем вимкнено. Банк лишається, але сам більше не "
        "росте — тільки через /promise_scan.")


# ---------- /promise_status ----------

def _status_payload():
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            bounds = pp.data_bounds(cur)
            cur.execute("SELECT count(*) FROM commitments")
            n_com = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM topics")
            n_top = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM commitment_revisions")
            n_rev = cur.fetchone()[0]
            rows = pp.list_queue(cur, limit=None)
            cur.execute(
                "SELECT coalesce(marked, true), count(*), sum(found) "
                "FROM promise_attempts GROUP BY 1")
            prefilter = {bool(k): (n, f or 0) for k, n, f in cur.fetchall()}
            cur.execute(
                "SELECT count(*) FROM promise_attempts "
                "WHERE NOT done AND attempts >= %s", (pp.MAX_ATTEMPTS,))
            giveup = cur.fetchone()[0]
    finally:
        conn.close()
    on = bot_db.get_state(INCR_KEY) is not None
    floor = _incr_floor() if on else None
    return {"bounds": bounds, "commitments": n_com, "topics": n_top,
            "revisions": n_rev, "counts": pp.facet_counts(rows),
            "prefilter": prefilter, "giveup": giveup,
            "incr_on": on, "floor": floor,
            "pending": _pending(floor) if on else 0}


async def promise_status_handler(update, context):
    """/promise_status — стан банку тем і його добору."""
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора не налаштована.")
        return
    msg = await update.message.reply_text("🦊 Дивлюсь стан банку тем…")
    try:
        d = await asyncio.to_thread(_status_payload)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        return
    lines = ["🦊 <b>Банк тем — стан</b>", "",
             f"Обіцянок: <b>{d['commitments']}</b> · тем: {d['topics']} · "
             f"ревізій: {d['revisions']}",
             _bounds_line(d["bounds"], shown=d["commitments"])]
    facets = " · ".join(f"{pp.CLASS_WORD[c]} {d['counts'][c]}"
                        for c in pp.CLASS_ORDER if d["counts"].get(c))
    if facets:
        lines.append(facets)

    lines.append("")
    if d["incr_on"]:
        lines.append(f"✅ Авто-інкремент увімкнено, від {pp.fmt_date(d['floor'])}"
                     f" · щогодини о :25 до {INCR_MAX_PER_RUN} статей")
        if d["pending"]:
            hours = -(-d["pending"] // INCR_MAX_PER_RUN)
            lines.append(f"   у черзі {d['pending']} статей (~{hours} год добору)")
        else:
            lines.append("   черга порожня — усе свіже розібрано")
    else:
        lines.append("⏸ Авто-інкремент вимкнено — банк росте лише через "
                     "/promise_scan. Увімкнути: /promise_increment_on")
    if d["giveup"]:
        lines.append(f"⚠️ {d['giveup']} статей вичерпали {pp.MAX_ATTEMPTS} спроби")

    # Ціна пре-фільтра — рахується сама, з накопиченого (§7 крок 7)
    if False in d["prefilter"]:
        n_un, found_un = d["prefilter"][False]
        n_m, found_m = d["prefilter"].get(True, (0, 0))
        total = found_m + found_un
        share = (100 * found_un / total) if total else 0
        lines.append("")
        lines.append(f"<b>Ціна пре-фільтра</b> (міряється сама): у {n_un} статтях "
                     f"без маркерів знайшлось {found_un} зобов'язань — "
                     f"{share:.0f}% усього знайденого.")
    await msg.edit_text("\n".join(lines), parse_mode="HTML")


# ---------- /promise_eval ----------

def _eval_payload(ids):
    """Прогнати витяг на еталонах і перевірити умову приймання §6.1.
    Нічого не пише — як /promise_test, тільки одразу по всіх семи."""
    import promise_eval as pev

    arts = api.fetch_ids(ids)
    by_id = {a["id"]: a for a in arts}
    results, errors, usage = _extract_sync([by_id[i] for i in ids if i in by_id])
    out = []
    for res in results:
        aid = res["article"]["id"]
        items = res["commitments"]
        out.append({"id": aid, "items": items,
                    "checks": pev.run_checks(aid, items),
                    "why": pev.CASE_WHY.get(aid, "")})
    missing = [i for i in ids if i not in by_id]
    return {"cases": out, "errors": errors, "usage": usage, "missing": missing}


def eval_verdict(ran, expected, passed, total):
    """Вердикт приймання одним рядком.

    Окремою функцією рівно тому, що перший же прогін у проді збрехав: витяг
    упав на ВСІХ семи статтях (несхвалена схема), перевірок не було жодної —
    і «passed == total» дало 0 == 0, тобто зелене «умову приймання пройдено».
    Зелений вердикт над нульовим прогоном — найгірша можлива брехня цього
    інструменту: він існує саме для того, щоб не пускати далі зламане.
    """
    if ran < expected:
        return (f"❌ <b>Приймання не відбулось</b>: витяг відпрацював на "
                f"{ran} з {expected} статей. Причина — у рядку ⚠️ вище; "
                f"поки вона не полагоджена, вердикту немає.")
    if total and passed == total:
        return ("✅ <b>Умову приймання пройдено — можна гнати місяць "
                "(/promise_scan).</b>")
    return ("❌ <b>Приймання НЕ пройдено.</b> Кожен червоний рядок — конкретне "
            "правило промпту, яке не спрацювало; деталі по кейсу — "
            "/promise_test &lt;id&gt;.")


async def promise_eval_handler(update, context):
    """/promise_eval [id] — умова приймання §6.1 однією командою.

    Женемо справжній витяг на семи РЕАЛЬНИХ статтях, кожна з яких свого часу
    зламала наївну модель, і перевіряємо не «схожість на еталон», а рівно те,
    чого не можна допустити: вигадану дату там, де дати немає; злиту з
    риторикою обіцянку; загублену полярність. Без цього приймання виглядало б
    як «сім разів /promise_test і читай очима» — а на третьому кейсі очі
    перестають читати.

    Нічого не пише в банк. Коштує центи (7 статей звичайним API).
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        await update.message.reply_text("🦊 Потрібні нора і ANTHROPIC_API_KEY.")
        return
    import promise_eval as pev

    args = context.args or []
    ids = [int(a) for a in args if a.isdigit()] or list(pev.CASE_IDS)
    unknown = [i for i in ids if i not in pev.CHECKS]
    if unknown:
        await update.message.reply_text(
            f"Немає умови приймання для {unknown}. Еталони: "
            + ", ".join(str(i) for i in pev.CASE_IDS))
        return

    msg = await update.message.reply_text(
        f"🦊 Жену витяг на {len(ids)} еталонах і звіряю з умовою приймання…")
    try:
        data = await asyncio.to_thread(_eval_payload, ids)
    except Exception as e:
        await msg.edit_text(f"❌ Прогін упав: {type(e).__name__}: {e}")
        return

    usage = data["usage"]
    record_ai_usage(api.MODEL, input_tokens=usage["input"], output_tokens=usage["output"],
                    cache_read=usage["cache_read"], cache_creation=usage["cache_creation"])
    total = passed = 0
    lines = ["🦊 <b>Приймання банку тем — еталони §6.1</b>", ""]
    details = []
    for case in data["cases"]:
        ok_n = sum(1 for _, ok, _ in case["checks"] if ok)
        total += len(case["checks"])
        passed += ok_n
        mark = "✅" if ok_n == len(case["checks"]) else "❌"
        lines.append(f"{mark} <b>{case['id']}</b> — {ok_n}/{len(case['checks'])} · "
                     f"зобов'язань {len(case['items'])}")
        lines.append(f"   <i>{escape_html(case['why'])}</i>")
        for name, ok, why in case["checks"]:
            if not ok:
                details.append(f"❌ <b>{case['id']}</b>: {escape_html(name)}\n"
                               f"   <i>{escape_html(why)}</i>")
    if data["missing"]:
        lines.append(f"⚠️ немає в норі: {data['missing']} — спершу /nora_resync")
    for err in data["errors"][:2]:
        # Повністю, а не обрізком: у 400-й відповіді API вся діагностика і є
        # текстом помилки, і саме він потрібен, щоб зрозуміти, що лагодити.
        lines.append("⚠️ " + escape_html(err[:600]))
    if len(data["errors"]) > 2:
        lines.append(f"⚠️ …і ще {len(data['errors']) - 2} таких самих")

    ran = len(data["cases"])
    cost = usage["input"] * api.PRICE_IN + usage["output"] * api.PRICE_OUT
    verdict = eval_verdict(ran, len(ids), passed, total)
    tail = [f"\nРазом: <b>{passed}/{total}</b> перевірок на {ran} статтях",
            verdict, f"<i>У банк не записано нічого. ≈ ${cost:.3f}</i>"]
    await msg.edit_text(_clip("\n".join(lines + [""] + details + tail)),
                        parse_mode="HTML", disable_web_page_preview=True)


# ---------- /promise_retest ----------

async def promise_retest_handler(update, context):
    """/promise_retest <id|URL> — перечитати статтю ПІСЛЯ правки промпту.

    Спершу знімає все, що з неї записано (інакше стара помилка лишилась би
    поруч із новим результатом і виглядала б як дубль), потім витяг заново і
    запис у банк.
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        await update.message.reply_text("🦊 Потрібні нора і ANTHROPIC_API_KEY.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Використання: /promise_retest <id|URL статті>")
        return
    raw = args[0]
    aid = extract_article_id(raw) if "/" in raw else (raw if raw.isdigit() else None)
    if not aid:
        await update.message.reply_text("Не видно id матеріалу.")
        return
    aid = int(aid)
    # Число могло бути id ОБІЦЯНКИ, а не статті — у чаті вони виглядають
    # однаково, і саме на цьому команда вже зіграла злий жарт: `/promise_retest
    # 92` прочитав 92 як статтю (стаття з таким id у норі є, вона з 2008 року),
    # чесно нічого в ній не знайшов і відзвітував «нових 0» — тобто виглядав
    # так, наче виправлення не спрацювало. `/promise_show` це прочитання вже
    # має; тут його бракувало.
    #
    # Порядок саме такий: спершу стаття. Якщо id є І статтею, І обіцянкою,
    # виграє стаття — бо аргумент команди названий статтею, а обіцянка це
    # здогадка.
    def resolve():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM articles WHERE id = %s", (aid,))
                if cur.fetchone():
                    return aid, None
                cur.execute("SELECT r.article_id FROM commitment_revisions r "
                            "WHERE r.commitment_id = %s ORDER BY r.id LIMIT 1",
                            (aid,))
                row = cur.fetchone()
                return (row[0], aid) if row else (aid, None)
        finally:
            conn.close()

    target, via_commitment = await asyncio.to_thread(resolve)
    msg = await update.message.reply_text(
        "🦊 Знімаю старий запис і перечитую…" if not via_commitment else
        f"🦊 {via_commitment} — це обіцянка, читаю її статтю {target}…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                dropped = pp.drop_article(cur, target)
            conn.commit()
        finally:
            conn.close()
        arts = api.fetch_ids([target])
        if not arts:
            return dropped, None, None, None
        results, errors, usage = _extract_sync(arts)
        stats = ingest(results) if results else {}
        return dropped, stats, errors, usage

    try:
        dropped, stats, errors, usage = await asyncio.to_thread(run)
    except Exception as e:
        await msg.edit_text(f"❌ Перечит не вдався: {type(e).__name__}: {e}")
        return
    if stats is None:
        await msg.edit_text(
            f"🦊 Статті {target} у норі немає — спершу /nora_resync {target}.")
        return
    if usage:
        record_ai_usage(api.MODEL, input_tokens=usage["input"],
                        output_tokens=usage["output"],
                        cache_read=usage["cache_read"],
                        cache_creation=usage["cache_creation"])
    lines = [f"🦊 Перечит статті {target}"
             + (f" (за обіцянкою {via_commitment})" if via_commitment else ""),
             f"Знято старих обіцянок: {len(dropped['removed'])} "
             f"(зачеплено {len(dropped['touched'])})",
             f"Записано: нових {stats.get('new', 0)} · ревізій {stats.get('revisions', 0)}"]
    if stats.get("noquote"):
        lines.append(f"Без цитати (не записано): {stats['noquote']}")
    # Нуль по обидва боки — не «спрацювало», а «нічого не було й не з'явилось».
    # Саме так виглядав промах по id, і відрізнити його від чесного «у статті
    # зобов'язань немає» було неможливо.
    if not dropped["removed"] and not stats.get("new") and not stats.get("revisions"):
        lines.append("<i>У цій статті зобов'язань не знайшлось — ні до, ні "
                     "після. Якщо чекав інше, перевір id: команда бере id "
                     "СТАТТІ або її URL.</i>")
    if errors:
        lines.append("⚠️ " + "; ".join(errors))
    lines.append(f"Дивитись: /promise_show {target}")
    await msg.edit_text("\n".join(lines), parse_mode="HTML")


# ---------- /promise_estimate ----------

def _scope_line(st):
    """Рядок «що саме ми рахуємо». Без нього числа брешуть мовчки: «статей за
    місяць 762» і «статей за місяць 268» виглядають однаково правдоподібно, і
    відрізняє їх лише те, що друге — миколаївські. Показуємо ОБИДВА числа."""
    if st.get("region") is None:
        return "Регіон: усі (загальнонаціональні теж)"
    outside = max(0, (st.get("month_total") or st["total"]) - st["total"])
    return (f"Беремо лише миколаївські: {st['total']} із "
            f"{st.get('month_total') or st['total']} "
            f"(поза Миколаєвом — {outside}, у банк не йдуть)")


def _estimate(from_date, to_date):
    marked = api.count_range(from_date, to_date, marked_only=True)
    every = api.count_range(from_date, to_date, marked_only=False)
    return marked["pending"], every["pending"], marked


async def promise_estimate_handler(update, context):
    """/promise_estimate [з] [по] — скільки статей і скільки грошей.
    Read-only, нічого не запускає і не витрачає."""
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    args = context.args or []
    from_date = args[0] if len(args) > 0 else "2024-01-01"
    to_date = args[1] if len(args) > 1 else "2027-01-01"
    msg = await update.message.reply_text("🦊 Рахую (read-only, безкоштовно)…")
    try:
        n_marked, n_all, st = await asyncio.to_thread(_estimate, from_date, to_date)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    cost_marked, tin, tout = api.estimate(n_marked)
    cost_all, _, _ = api.estimate(n_all)
    share = (100 * st["marked"] / st["total"]) if st["total"] else 0
    await msg.edit_text(
        f"🦊 <b>Оцінка витягу обіцянок {from_date}…{to_date}</b>\n\n"
        f"{_scope_line(st)}\n"
        f"Проходять пре-фільтр за маркерами: {st['marked']} ({share:.0f}%)\n"
        f"З них ще не розібрано: <b>{n_marked}</b>\n"
        f"Без фільтра було б: {n_all}\n\n"
        f"Токени: ~{tin/1e6:.1f}M вх + ~{tout/1e6:.1f}M вих (Haiku 4.5, батч −50%)\n"
        f"Вартість із фільтром: <b>≈ ${cost_marked:.2f}</b>\n"
        f"Без фільтра: ≈ ${cost_all:.2f}\n\n"
        f"<i>Прогін іде ОДНИМ місяцем: /promise_scan YYYY-MM. Глибину нарощуємо "
        f"по місяцю, дивлячись на якість — діапазон у роках за один раз не "
        f"запускається свідомо.</i>",
        parse_mode="HTML")


# ---------- /promise_scan ----------

def _scan_plan(month, marked_only):
    from_date, to_date = api.month_bounds(month)
    st = api.count_range(from_date, to_date, marked_only=marked_only)
    cost, tin, tout = api.estimate(st["pending"])
    return {"month": month, "from": from_date, "to": to_date,
            "articles": st["pending"], "stats": st, "cost": cost,
            "marked_only": marked_only}


async def promise_scan_handler(update, context):
    """/promise_scan <YYYY-MM> [all] — прогін одного місяця.

    Спершу оцінка й підтвердження кнопкою, і тільки потім гроші. Аргумент —
    один місяць, не діапазон років: глибину нарощує Олег, дивлячись, як
    тримається якість (§7).

    `all` вимикає пре-фільтр — саме так міряється його ЦІНА: місяць женеться
    двічі, і другий прогін бере рівно ті статті, які фільтр не пропустив
    (уже розібрані пропускаються, тож удруге ми за них не платимо).
    """
    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY") or not bot_db.is_configured():
        await update.message.reply_text("🦊 Потрібні нора і ANTHROPIC_API_KEY.")
        return
    args = context.args or []
    month = args[0] if args else ""
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit():
        await update.message.reply_text(
            "Формат: /promise_scan YYYY-MM [all]\n"
            "Напр.: /promise_scan 2026-07 — місяць із пре-фільтром;\n"
            "       /promise_scan 2026-07 all — без фільтра (замір його ціни).")
        return
    marked_only = not (len(args) > 1 and args[1].lower() in ("all", "усе", "все"))
    if await asyncio.to_thread(_load_state):
        await update.message.reply_text(
            "Уже є незавершений прогін. Стан і полінг — /promise_resume.")
        return

    msg = await update.message.reply_text("🦊 Рахую місяць (безкоштовно)…")
    try:
        plan = await asyncio.to_thread(_scan_plan, month, marked_only)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахувалось: {e}")
        return
    if not plan["articles"]:
        await msg.edit_text(
            f"🦊 За {month} брати нічого: "
            f"{plan['stats']['total']} статей, усі вже розібрані"
            + ("" if marked_only else " (або поза фільтром)") + ".")
        return
    st = plan["stats"]
    share = (100 * st["marked"] / st["total"]) if st["total"] else 0
    mode = "з пре-фільтром" if marked_only else "БЕЗ пре-фільтра (замір)"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        f"Запустити ≈ ${plan['cost']:.2f}",
        callback_data=f"psc:{month}:{'m' if marked_only else 'a'}")]])
    await msg.edit_text(
        f"🦊 <b>Скан {month}</b> — {mode}\n\n"
        f"{_scope_line(st)}\n"
        f"З маркерами: {st['marked']} ({share:.0f}%)\n"
        f"Піде у витяг: <b>{plan['articles']}</b> (вже розібраних: {st['skipped']})\n"
        f"Вартість: <b>≈ ${plan['cost']:.2f}</b> (Haiku 4.5, батч)\n\n"
        f"<i>Ідемпотентно: повторний виклик не платить удруге.</i>",
        parse_mode="HTML", reply_markup=kb)


async def promise_scan_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    try:
        _, month, mode = query.data.split(":")
    except ValueError:
        return
    marked_only = mode == "m"
    if await asyncio.to_thread(_load_state):
        await query.edit_message_text("Уже є незавершений прогін — /promise_resume.")
        return
    await query.edit_message_text(f"🦊 Вивантажую {month} і відправляю батчі…")

    from_date, to_date = api.month_bounds(month)
    # Ручний скан — єдине джерело правди про те, докуди вглиб уже заплачено.
    # Саме звідси інкремент бере підлогу, і саме тому вона більше не залежить
    # від діагностичних команд на випадкових старих нодах.
    await asyncio.to_thread(_note_scan_floor, from_date)
    state = {"month": month, "from": from_date, "to": to_date,
             "marked_only": marked_only, "batch_ids": [], "done": [],
             "chat_id": query.message.chat_id, "started": int(time.time()),
             "articles": 0}

    def submit():
        import anthropic
        client = anthropic.Anthropic()
        arts, st = api.fetch_range(from_date, to_date, marked_only=marked_only)
        if not arts:
            return 0
        state["articles"] = len(arts)
        _save_state(state)
        sys_len = len(api.get_system_prompt().encode("utf-8"))
        for chunk in api.chunk_articles(arts, sys_len):
            batch = client.messages.batches.create(
                requests=[api._make_request(client, a) for a in chunk])
            state["batch_ids"].append(batch.id)
            _save_state(state)
        return len(arts)

    try:
        n = await asyncio.to_thread(submit)
    except Exception as e:
        if state["batch_ids"]:
            await context.bot.send_message(
                state["chat_id"],
                f"❌ Збій посеред відправки: {e}\nУже створені батчі не "
                f"загубляться — /promise_resume доведе їх до кінця.")
            asyncio.create_task(_poll_task(context.bot))
        else:
            await asyncio.to_thread(_clear_state)
            await context.bot.send_message(state["chat_id"],
                                           f"❌ Не вдалося відправити батчі: {e}")
        return
    if not n:
        await asyncio.to_thread(_clear_state)
        await query.edit_message_text(f"🦊 За {month} брати нічого — усе розібрано.")
        return
    await query.edit_message_text(
        f"🦊 Відправлено {n} статей у {len(state['batch_ids'])} батч(ів).\n"
        f"Полю щохвилини; звіт по темах пришлю сюди.\n"
        f"Після редеплою — /promise_resume.")
    asyncio.create_task(_poll_task(context.bot))


async def resume_on_start(bot):
    """Переприв'язати полінг батчів САМА, щойно бот піднявся.

    Батчі живуть на боці Anthropic 29 днів, а полінг — у пам'яті процесу.
    Будь-який редеплой його вбиває, і оплачений прогін тихо зависає: гроші
    списані, результат лежить, у банку нічого. Саме це й сталось 03.08 —
    липневий скан пішов о 17:14, а деплої йшли один за одним, і місяць не
    доїхав.

    Розраховувати, що людина щоразу згадає /promise_resume, — це закладати
    в процес крок, про який вона дізнається лише постфактум. Стан і так
    лежить у норі, тож відновлення нічого не коштує.
    """
    if not bot_db.is_configured() or not os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        state = await asyncio.to_thread(_load_state)
        if not state or not state.get("batch_ids") or _poll_running["flag"]:
            return
        print(f"promises: підхоплюю незавершений скан {state.get('month')} "
              f"({len(state['batch_ids'])} батчів)")
        asyncio.create_task(_poll_task(bot))
    except Exception as e:
        print(f"promises: не підхопив скан після старту — {e}")


async def promise_resume_handler(update, context):
    """/promise_resume — переприв'язати полінг після редеплою (стан у норі)."""
    if not _allowed(update):
        return
    state = await asyncio.to_thread(_load_state)
    if not state:
        await update.message.reply_text("Незавершених прогонів немає.")
        return
    if not state["batch_ids"]:
        await asyncio.to_thread(_clear_state)
        await update.message.reply_text(
            "Відправка обірвалась до створення першого батчу — гроші не "
            "витрачались. Запускай /promise_scan заново.")
        return
    if _poll_running["flag"]:
        await update.message.reply_text("Полінг уже працює.")
        return
    state["chat_id"] = update.effective_chat.id
    await asyncio.to_thread(_save_state, state)
    await update.message.reply_text("🦊 Переприв'язав полінг, стежу далі…")
    asyncio.create_task(_poll_task(context.bot))


def _batch_counts(client, batch_ids):
    tot = {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    ended = []
    for bid in batch_ids:
        b = client.messages.batches.retrieve(bid)
        rc = b.request_counts
        for k in tot:
            tot[k] += getattr(rc, k, 0) or 0
        if b.processing_status == "ended":
            ended.append(bid)
    return tot, ended


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
                return
            tot, ended = await asyncio.to_thread(_batch_counts, client, state["batch_ids"])
            if set(ended) != set(state["done"]):
                state["done"] = ended
                await asyncio.to_thread(_save_state, state)
            if len(ended) == len(state["batch_ids"]):
                break
            await asyncio.sleep(POLL_INTERVAL)
        await _finish(bot, state, client)
    except Exception as e:
        await notify_error(bot, "банк тем (полінг батчів)", e)
    finally:
        _poll_running["flag"] = False


async def _finish(bot, state, client):
    chat_id = state["chat_id"]
    await bot.send_message(
        chat_id, "🦊 Батчі готові — зшиваю ланцюги і зливаю в банк тем…")

    def collect():
        raw, errors = [], []
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        for bid in state["batch_ids"]:
            for res in client.messages.batches.results(bid):
                aid = int(res.custom_id)
                if res.result.type != "succeeded":
                    errors.append((aid, str(res.result.type)))
                    continue
                msg = res.result.message
                u = msg.usage
                usage["input"] += u.input_tokens or 0
                usage["output"] += u.output_tokens or 0
                usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
                usage["cache_creation"] += getattr(u, "cache_creation_input_tokens", 0) or 0
                text = next((bl.text for bl in msg.content if bl.type == "text"), None)
                if not text:
                    errors.append((aid, "порожній вивід"))
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    errors.append((aid, "непарсибельний JSON"))
                    continue
                raw.append((aid, obj.get("commitments", [])[:8]))
        # Шапки статей добираємо ОДНИМ запитом і БЕЗ текстів: далі вони потрібні
        # лише судді (заголовок) і сортуванню (дата).
        by_id = {a["id"]: a for a in api.fetch_ids([aid for aid, _ in raw],
                                                   with_text=False)}
        results = [{"article": by_id[aid], "commitments": items}
                   for aid, items in raw if aid in by_id]
        return results, errors, usage

    def mark_errors(errs):
        """Стаття, чий витяг упав, лишається В ЧЕРЗІ й повертається наступним
        скану — рівно те, чого не робила курсорна схема сутностей. Спроби при
        цьому рахуються, щоб одна бита нода не крутилась вічно."""
        if not errs:
            return
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                for aid, reason in errs:
                    pp.mark_attempt(cur, aid, error=reason[:200], done=False)
            conn.commit()
        finally:
            conn.close()

    try:
        results, errors, usage = await asyncio.to_thread(collect)
        stats = await asyncio.to_thread(ingest, results)
        await asyncio.to_thread(mark_errors, errors)
        record_ai_usage(api.MODEL, input_tokens=usage["input"],
                        output_tokens=usage["output"],
                        cache_read=usage["cache_read"],
                        cache_creation=usage["cache_creation"])
        report = await asyncio.to_thread(_scan_report, state["month"])
        await asyncio.to_thread(_clear_state)
        cost = (usage["input"] * api.PRICE_IN_BATCH
                + usage["output"] * api.PRICE_OUT_BATCH)
        head = (f"🦊 <b>Скан {state['month']} завершено</b>"
                + ("" if state.get("marked_only", True) else " (БЕЗ пре-фільтра)")
                + f"\n\nСтатей оброблено: {stats['articles']}"
                + (f" · збоїв: {len(errors)}" if errors else "")
                + f"\nЗобов'язань записано: <b>{stats['new'] + stats['revisions']}</b>"
                  f" (нових {stats['new']}, ревізій до наявних {stats['revisions']})"
                + (f"\nБез цитати — не записано: {stats['noquote']}" if stats["noquote"] else "")
                + (f"\nСуддя ланцюга викликався: {stats['judged']} раз(и)" if stats["judged"] else "")
                + f"\nВартість витягу ≈ ${cost:.2f}")
        if errors:
            head += ("\n<i>Приклади збоїв: " + escape_html(
                     "; ".join(f"{a}: {r}" for a, r in errors[:3]))
                     + " — статті лишились у черзі, наступний скан повторить.</i>")
        await bot.send_message(chat_id, _clip(head + "\n\n" + report),
                               parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        await notify_error(bot, "банк тем (злиття результатів)", e)
        await bot.send_message(
            chat_id,
            f"❌ Збій зливання: {e}\nБатчі живуть 29 днів — /promise_resume повторить.")


# ---------- Звіт по темах (а не списком) ----------

def _scan_report(month):
    """Звіт після прогону — ПО ТЕМАХ: що взагалі лежить у банку і чи варто
    рити глибше. Списком тут не допоможеш: сто рядків нічого не пояснюють."""
    from_date, to_date = api.month_bounds(month)
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            window = ("r.article_id IN (SELECT id FROM articles "
                      " WHERE published >= extract(epoch FROM %s::date) "
                      "   AND published <  extract(epoch FROM %s::date))")
            params = [from_date, to_date]
            cur.execute(
                f"SELECT count(DISTINCT c.id), count(DISTINCT c.topic_id) "
                f"FROM commitments c JOIN commitment_revisions r ON r.commitment_id = c.id "
                f"WHERE {window}", params)
            n_commitments, n_topics = cur.fetchone()

            def group(expr):
                cur.execute(
                    f"SELECT {expr}, count(DISTINCT c.id) FROM commitments c "
                    f"JOIN commitment_revisions r ON r.commitment_id = c.id "
                    f"WHERE {window} GROUP BY 1 ORDER BY 2 DESC", params)
                return [(k, v) for k, v in cur.fetchall()]

            by_verif = group("c.verifiability")
            by_source = group("c.source_type")
            by_method = group("c.verification_method")
            cur.execute(
                "SELECT coalesce(e.name_ua, e.name_ru), count(DISTINCT c.id) "
                "FROM commitments c JOIN commitment_revisions r ON r.commitment_id = c.id "
                "JOIN entities e ON e.id = c.subject_entity_id "
                f"WHERE {window} GROUP BY 1 ORDER BY 2 DESC LIMIT 8", params)
            top_objects = cur.fetchall()
            cur.execute(
                "SELECT r.promiser_text, count(DISTINCT c.id) FROM commitments c "
                "JOIN commitment_revisions r ON r.commitment_id = c.id "
                f"WHERE {window} AND r.promiser_text IS NOT NULL "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 8", params)
            top_promisers = cur.fetchall()
            # Ціна пре-фільтра (§7 крок 7): скільки зобов'язань лежало в
            # статтях, які фільтр не пропустив би. Рахується щоразу, коли
            # місяць прогнали і з фільтром, і без нього.
            cur.execute(
                "SELECT coalesce(t.marked, true), count(*), sum(t.found) "
                "FROM promise_attempts t JOIN articles a ON a.id = t.article_id "
                "WHERE a.published >= extract(epoch FROM %s::date) "
                "  AND a.published <  extract(epoch FROM %s::date) "
                "GROUP BY 1", params)
            prefilter = {bool(k): (n, f or 0) for k, n, f in cur.fetchall()}
    finally:
        conn.close()

    def fmt(pairs, words):
        return " · ".join(f"{words.get(k, k or '—')} <b>{v}</b>" for k, v in pairs if v)

    lines = [f"<b>Що в банку за {month}</b>",
             f"Зобов'язань: <b>{n_commitments}</b> · тем: <b>{n_topics}</b>"]
    verif_words = {"measurable": "можна прострочити", "undated": "без горизонту",
                   "event_triggered": "чекає події", "unfalsifiable": "перевірити нічим"}
    if by_verif:
        lines.append("Класи перевірки: " + fmt(by_verif, verif_words))
    if by_source:
        lines.append("Джерела: " + fmt(by_source, pp.SOURCE_WORD))
    if by_method:
        lines.append("Як перевіряти: " + fmt(by_method, pp.METHOD_WORD))
    if top_objects:
        lines.append("Найчастіші об'єкти: " + " · ".join(
            f"{escape_html(k)} {v}" for k, v in top_objects))
    if top_promisers:
        lines.append("Хто обіцяє: " + " · ".join(
            f"{escape_html(k[:40])} {v}" for k, v in top_promisers))
    if False in prefilter:
        n_unmarked, found_unmarked = prefilter[False]
        n_marked, found_marked = prefilter.get(True, (0, 0))
        total_found = found_marked + found_unmarked
        share = (100 * found_unmarked / total_found) if total_found else 0
        lines.append(
            f"\n<b>Ціна пре-фільтра</b> (місяць прогнано і з ним, і без):\n"
            f"у {n_unmarked} статтях без маркерів знайшлось {found_unmarked} "
            f"зобов'язань — це {share:.0f}% усього знайденого за місяць.\n"
            f"<i>Саме це число вирішує, чи вмикати фільтр на роках.</i>")
    lines.append("\nДивитись: /promises")
    return "\n".join(lines)
