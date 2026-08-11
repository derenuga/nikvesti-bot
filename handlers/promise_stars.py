"""
Зірочка на обіцянці: «веду цю тему сам».

Навіщо. Черга банку одна на редакцію, і в ній сотні записів — а журналіст
веде п'ять. Без свого кошика банк лишається спільним звалищем тем, куди
заходять, коли згадають. Зірочка робить його особистим: обране фільтрується
в черзі, а КОЖНА нова ревізія прилітає сама — у стрічку апки й у приват.

Чому саме ревізія — головна подія. Ревізія це і є те, заради чого банк
ведеться: «перенесли на 2027», «пообіцяли ще раз», «тепер це робитиме ДП».
Помітити її вручну неможливо — вона з'являється в статті, яку ти не читав,
через місяць після того, як тему відклали. Тому сторож щогодини звіряє
лічильник `revisions` зі знімком у зірочці й каже про різницю.

Зірочка ОСОБИСТА (ключ «обіцянка + людина»). Спільний прапорець у
commitments зробив би стрічку кожного шумом для іншого: Олег стежить за
бюджетом, Аліна за міськрадою, і одне «обране» на двох означало б, що
жодному з них воно не належить.

Два канали, і другий не дублює перший. Стрічка апки — місце, куди людина
приходить сама; приват — те, що знаходить її, коли вона в апку не зайшла.
Для теми, яку людина сама позначила «моя», це саме той випадок, коли
штовхнути доречно: вона просила про це явно, а не отримала розсилку.
"""

import asyncio

import entity_pipeline as ep
import promise_pipeline as pp
from handlers import team_notifications, team_roster
from handlers.helpers import escape_html
from handlers.notifier import notify_error

# Скільки подій за раз кажемо одній людині в приват. Більше — і це вже
# розсилка, а не сигнал; решта лишається у стрічці апки й у фасеті.
PRIVATE_MAX = 5


def _pick():
    """Хто за чим стежить і що там нового. Один запит, без моделі."""
    conn = ep.connect()
    try:
        conn.autocommit = True
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            rows = pp.star_news(cur)
            if not rows:
                return [], {}
            revs = pp.revisions(cur, [r["commitment_id"] for r in rows])
            links = {}
            from handlers.promises import _links_for
            from handlers.promise_app import _images_for
            art_ids = {v["article_id"] for v in revs if v.get("article_id")}
            links = _links_for(cur, art_ids)
            images = _images_for(cur, art_ids)
            # Остання ревізія кожної обіцянки — саме вона й є новина.
            last = {}
            for v in revs:
                cur_last = last.get(v["commitment_id"])
                if not cur_last or (v.get("published") or 0) >= (cur_last.get("published") or 0):
                    last[v["commitment_id"]] = v
            for r in rows:
                r["rev"] = last.get(r["commitment_id"])
                aid = (r["rev"] or {}).get("article_id")
                r["link"] = links.get(aid) or {}
                r["image"] = images.get(aid)
            return rows, links
    finally:
        conn.close()


def _mark(rows):
    """Позначити побачене — рівно те, про що сказали, і кожне окремо."""
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            for r in rows:
                pp.star_seen(cur, r["commitment_id"], r["person"],
                             r.get("revisions") or 0)
        conn.commit()
    finally:
        conn.close()


def _bell(rows):
    """Стрічка апки. Подія адресна — audience person, а не менеджерам."""
    for r in rows:
        link = r.get("link") or {}
        # meta — стрічка малює подію банку тем за макетом: підпис типу,
        # назва обіцянки жирним, новина з мініатюрою (як у promise_fulfil)
        meta = {
            "promise": True,
            "label": "Нова згадка у твоїй темі",
            "title": r["title"],
            "news_title": link.get("title"),
            "image": r.get("image"),
        }
        try:
            team_notifications.notify_safe(
                "promise_star",
                f"Нова згадка про тему, за якою ти стежиш: {r['title']}",
                body=((r.get("rev") or {}).get("quote") or "")[:300],
                url=link.get("url"),
                meta={k: v for k, v in meta.items() if v},
                audience="person",
                person=r["person"],
                object_type="promise",
                object_id=r["commitment_id"],
                # Одна ревізія смикає раз: ключ несе номер ревізії, тож
                # наступна подія по тій самій обіцянці прийде, а ця — ні.
                dedup_key=f"promise_star:{r['commitment_id']}:{r['person']}:"
                          f"{r.get('revisions') or 0}")
        except Exception as e:
            print(f"promise_stars: стрічка не прийняла — {e}")


def _text(r):
    link = r.get("link") or {}
    quote = ((r.get("rev") or {}).get("quote") or "").strip()
    out = [f"⭐ <b>{escape_html(r['title'] or '—')}</b>",
           "З'явилась нова згадка про тему, за якою ти стежиш."]
    if quote:
        out.append(f"«{escape_html(quote[:400])}»")
    if link.get("url"):
        out.append(f"Джерело: <a href=\"{link['url']}\">"
                   f"{escape_html(link.get('title') or 'матеріал')}</a>")
    return "\n\n".join(out)


async def _private(bot, rows):
    """Приват тому, хто поставив зірочку. Без tg_id — тихо пропускаємо: у
    стрічці апки подія вже лежить, і губиться лише штовхання."""
    from handlers.helpers import app_link_with_param, resolve_app_link
    try:
        app_url, _ = await resolve_app_link(bot)
    except Exception:
        app_url = None
    by_person = {}
    for r in rows:
        by_person.setdefault(r["person"], []).append(r)
    for person, items in by_person.items():
        tg_id = await asyncio.to_thread(team_roster.tg_id_for, person)
        if not tg_id:
            continue
        for r in items[:PRIVATE_MAX]:
            markup = None
            if app_url:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Відкрити картку",
                    url=app_link_with_param(app_url,
                                            f"promise_{r['commitment_id']}"))]])
            try:
                await bot.send_message(tg_id, _text(r), parse_mode="HTML",
                                       disable_web_page_preview=True,
                                       reply_markup=markup)
            except Exception as e:
                print(f"promise_stars: приват «{person}» не пройшов — {e}")
        if len(items) > PRIVATE_MAX:
            try:
                await bot.send_message(
                    tg_id, f"⭐ …і ще {len(items) - PRIVATE_MAX} по темах, за "
                           f"якими ти стежиш — у стрічці апки.")
            except Exception:
                pass


async def hourly(bot):
    """Щогодини о :50 — після витягу (:25) і детектора виконання (:40).

    Тихий: мовчить, коли нових ревізій немає. Позначаємо ПІСЛЯ відправки —
    збій каналу не має з'їдати подію (правило нагадувань).
    """
    try:
        rows, _ = await asyncio.to_thread(_pick)
        if not rows:
            return
        await asyncio.to_thread(_bell, rows)
        await _private(bot, rows)
        await asyncio.to_thread(_mark, rows)
        print(f"банк тем: зірочки — {len(rows)} подій")
    except Exception as e:
        await notify_error(bot, "зірочки банку тем", e)


# ---------- /promise_star ----------

async def promise_star_handler(update, context):
    """/promise_star <id> — поставити або зняти зірочку з чату.

    Той самий тап, що в апці, але з чату: обіцянку часто бачать саме тут —
    у нагадуванні каналу, у виводі /promises, у відповіді Лиса.
    """
    from handlers.promises import _allowed

    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Використання: /promise_star <id обіцянки>")
        return
    cid = int(args[0])
    person = await asyncio.to_thread(
        team_roster.resolve_person,
        update.effective_user.id,
        update.effective_user.username if update.effective_user else None)
    if not person:
        await update.message.reply_text(
            "🦊 Не впізнав тебе в ростері — зірочка особиста, тож без цього "
            "нема кому її записати.")
        return

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                if not row:
                    return None, False
                on = person not in pp.star_watchers(cur, cid)
                pp.star(cur, cid, person, on)
            conn.commit()
            return row, on
        finally:
            conn.close()

    row, on = await asyncio.to_thread(run)
    if not row:
        await update.message.reply_text("🦊 Такої обіцянки немає.")
        return
    if on:
        await update.message.reply_text(
            f"⭐ Стежу за темою: <b>{escape_html(row.get('title') or cid)}</b>\n"
            f"Кожна нова згадка прилетить сюди і в стрічку апки. "
            f"Фасет «Обрані» — у банку.",
            parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"Зняв зірочку: <b>{escape_html(row.get('title') or cid)}</b>",
            parse_mode="HTML")
