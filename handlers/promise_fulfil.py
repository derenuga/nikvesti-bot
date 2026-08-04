"""
Детектор ВИКОНАННЯ обіцянок: новина «боларди поставили» закриває обіцянку.

**Навіщо.** Питання Олега 04.08: «коли в норі з'явиться новина, що боларди
поставили, чи поміняється статус на виконано?» Відповідь була — ні, і це
головна дірка банку: він умів лише накопичуватись. Витяг ловить заяви про
МАЙБУТНЄ, а «боларди встановили» це констатація минулого, тож у банк вона не
потрапляла взагалі. Обіцянку могли виконати, а вона й далі стояла
простроченою і нагадувала про себе в канал.

**Як шукає.** Пре-фільтр безкоштовний: беремо відкриті обіцянки, чий предмет
або об'єкт — картка сутнісного шару, яка згадується у свіжій статті. Збіг іде
по КАРТЦІ, а не по тексту: «боларди біля зоопарку» і «обмежувальні стовпчики
на Богоявленському» — різні рядки й одна картка. Модель кличеться тільки там,
де такий збіг уже є.

**Два рівні, обидва за рішенням Олега (04.08):**

- `high` — бот **ставить статус сам**. Обіцянка йде у фасет «Перевірені» з
  підписом, що закрив її Лис, і з лінком на новину, яка це доводить. Вона не
  зникає: зірвана чи виконана обіцянка — це факт, на який посилаються в
  наступному тексті;
- `medium` — фасет «Схоже, виконано» в апці **плюс сповіщення Каті**. Я
  пропонував не чіпати її стрічку, щоб не залити; Олег: «там нічо не
  зіллється, вона має відслідковувати сповіщення в нуль постійно».

**Чому суддя схильний казати «ні».** Хибно закрита обіцянка ЗНИКАЄ з черги —
тобто редакція перестає перевіряти те, що, можливо, не зробили. Пропущене
виконання коштує лише зайвого рядка. Тому в промпті прямо: краще `none`, ніж
помилкове `done`, а заява самого обіцяльника про виконання ніколи не дає
`high`.

**Відкат є завжди** — `/promise_reopen <id>`: статус назад у «чекаємо», ознаки
знімаються. Будь-яке автоматичне рішення мусить мати одну кнопку відкату,
інакше помилка ховає живу тему назавжди й мовчки.
"""

import asyncio
import os
import time

from handlers import bot_db, team_notifications
from handlers.helpers import escape_html
from handlers.notifier import notify_error
import entity_pipeline as ep
import promise_pipeline as pp
from handlers import promise_judge as pj

# Скільки днів свіжих статей дивимось за прогін. Тиждень: щогодинний прогін
# бере ті самі статті знову, але пари вже судились і повторно не платяться.
DEFAULT_DAYS = 7
# Скільки пар за раз. Пре-фільтр по картці сутності дає їх небагато, але на
# першому прогоні по накопиченому може вистрелити.
MAX_PAIRS = 60
# Скільки тексту статті даємо судді. Виконання зазвичай у першому абзаці, а
# повний текст на кожну пару — це гроші без користі.
TEXT_CAP = 1800


def _load(since, limit):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            pairs = pp.fulfil_candidates(cur, since, limit)
            if not pairs:
                return []
            cids = list({p["commitment_id"] for p in pairs})
            aids = list({p["article_id"] for p in pairs})
            cur.execute(f"SELECT {pp.COMMITMENT_COLS} FROM commitments c "
                        "WHERE c.id = ANY(%s)", (cids,))
            rows = {r["id"]: r for r in pp._rows(cur)}
            quotes = {}
            for rev in pp.revisions(cur, cids):
                quotes.setdefault(rev["commitment_id"], rev.get("quote") or "")
            cur.execute("SELECT id, coalesce(title_ua, title_ru), "
                        "       coalesce(text_ua, text_ru) "
                        "FROM articles WHERE id = ANY(%s)", (aids,))
            arts = {r[0]: {"title": r[1] or "", "text": (r[2] or "")[:TEXT_CAP]}
                    for r in cur.fetchall()}
    finally:
        conn.close()
    out = []
    for p in pairs:
        row, art = rows.get(p["commitment_id"]), arts.get(p["article_id"])
        if not row or not art or not art["text"]:
            continue
        out.append({
            "commitment_id": p["commitment_id"],
            "article_id": p["article_id"],
            "promise": {"title": row.get("title"),
                        "owner_text": row.get("owner_text"),
                        "deadline": row.get("deadline"),
                        "quote": (quotes.get(p["commitment_id"]) or "")[:400]},
            "article": art,
        })
    return out


async def scan(days=DEFAULT_DAYS, limit=MAX_PAIRS, on_progress=None):
    """Прогін детектора.

    Повертає (закрито, у чергу, переглянуто, розклад вердиктів). Розклад —
    не прикраса: без нього «0 з 60» однаково читається і як чесна робота
    скупого судді, і як шістдесят мовчазних збоїв моделі.
    """
    if not bot_db.is_configured() or not os.environ.get("ANTHROPIC_API_KEY"):
        return 0, 0, 0, {}
    since = int(time.time()) - int(days) * 86400
    pairs = await asyncio.to_thread(_load, since, limit)
    if not pairs:
        return 0, 0, 0, {}

    sem = asyncio.Semaphore(pj.CONCURRENCY)
    done_n = 0

    async def one(p):
        nonlocal done_n
        async with sem:
            p["verdict"] = await pj.judge_fulfil(p["promise"], p["article"])
        done_n += 1
        if on_progress and done_n % 15 == 0:
            try:
                await on_progress(done_n, len(pairs))
            except Exception:
                pass
        return p

    judged = list(await asyncio.gather(*(one(p) for p in pairs)))

    def apply():
        closed, queued = [], []
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                for p in judged:
                    v = p.get("verdict")
                    # Збій моделі — не рішення: пари немає в журналі, отже
                    # наступний прогін спитає знову.
                    if not v or v.get("state") not in ("done", "failed"):
                        continue
                    conf = v.get("confidence")
                    if conf not in ("high", "medium"):
                        continue
                    auto = conf == "high"
                    if not pp.record_closure(cur, p["commitment_id"],
                                             p["article_id"], v, applied=auto):
                        continue          # цю пару вже судили
                    if auto:
                        pp.mark_checked(cur, p["commitment_id"], "Лис",
                                        outcome=v["state"],
                                        note=f"{v.get('why') or ''} "
                                             f"[матеріал {p['article_id']}]")
                        closed.append(p)
                    else:
                        queued.append(p)
            conn.commit()
        finally:
            conn.close()
        return closed, queued

    closed, queued = await asyncio.to_thread(apply)
    if queued:
        await asyncio.to_thread(_notify, queued)
    breakdown = {}
    for p in judged:
        v = p.get("verdict")
        key = "збій" if not v else f"{v.get('state')}/{v.get('confidence')}"
        breakdown[key] = breakdown.get(key, 0) + 1
    return len(closed), len(queued), len(judged), breakdown


def _notify(queued):
    """Сповіщення Каті по кожній ознаці, яку бот не наважився застосувати."""
    from handlers.promises import _links_for

    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            links = _links_for(cur, {p["article_id"] for p in queued})
    finally:
        conn.close()
    for p in queued:
        v = p.get("verdict") or {}
        link = links.get(p["article_id"]) or {}
        word = "виконано" if v.get("state") == "done" else "зірвано"
        try:
            team_notifications.notify_safe(
                "promise_closure",
                f"Схоже, {word}: {p['promise']['title']}",
                body=(v.get("why") or "")
                     + (f" · {link.get('title')}" if link.get("title") else ""),
                url=link.get("url"),
                object_type="promise",
                object_id=p["commitment_id"],
                # Одна стаття про одну обіцянку смикає раз.
                dedup_key=f"promise_closure:{p['commitment_id']}:{p['article_id']}")
        except Exception as e:
            print(f"promise_fulfil: сповіщення не пішло — {e}")


async def hourly(bot):
    """Щогодини о :40 — після витягу (:25) і сутностей попередньої години.

    Тихий: пише лише тоді, коли щось закрив. Опт-ін не потрібен — детектор
    нічого не розсилає в канал, а помилка відкатна.
    """
    try:
        closed, queued, _seen, _by = await scan()
        if closed:
            print(f"банк тем: закрито за фактом виконання — {closed}, "
                  f"у чергу Каті — {queued}")
    except Exception as e:
        await notify_error(bot, "детектор виконання обіцянок", e)


# ---------- Команди ----------

async def promise_fulfil_handler(update, context):
    """/promise_fulfil [днів] — прогнати детектор зараз."""
    from handlers.promises import _allowed

    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    args = context.args or []
    days = int(args[0]) if args and args[0].isdigit() else DEFAULT_DAYS
    msg = await update.message.reply_text(
        f"🦊 Шукаю ознаки виконання за {days} дн.…")

    async def progress(done, total):
        await msg.edit_text(f"🦊 Дивлюсь: {done} з {total}…")

    try:
        closed, queued, seen, by = await scan(days, on_progress=progress)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
        return
    if not seen:
        await msg.edit_text(
            f"🦊 За {days} дн. немає жодної свіжої новини про об'єкт, "
            f"якому щось обіцяли. Це нормально: збіг рахується по картці "
            f"сутності, а не по словах.")
        return
    # Розклад вердиктів у звіті обов'язковий: «0 з 60» без нього однаково
    # читається і як чесна робота скупого судді, і як шістдесят мовчазних
    # збоїв моделі.
    lines = " · ".join(f"{k}: {v}" for k, v in sorted(by.items()))
    broke = by.get("збій", 0)
    await msg.edit_text(
        f"🦊 <b>Ознаки виконання</b>\n\n"
        f"Перевірено пар: {seen}\n"
        f"Закрито ботом (впевнено): <b>{closed}</b>\n"
        f"Пішло Каті на підтвердження: <b>{queued}</b>\n\n"
        f"<code>{escape_html(lines)}</code>\n"
        + (f"\n⚠️ Модель не відповіла на {broke} — дивись логи Railway.\n"
           if broke else "")
        + f"\n<i>«none» означає, що новина про цей об'єкт є, але про виконання "
          f"не каже — суддя навмисно скупий. Закрите лежить у «Перевірені» з "
          f"лінком на новину-доказ, відкат — /promise_reopen &lt;id&gt;.</i>",
        parse_mode="HTML")


async def promise_reopen_handler(update, context):
    """/promise_reopen <id> — повернути обіцянку в чергу."""
    from handlers.promises import _allowed

    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Використання: /promise_reopen <id обіцянки>")
        return
    cid = int(args[0])
    who = (update.effective_user.full_name if update.effective_user else "—")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                ok = pp.reopen(cur, cid, who)
            conn.commit()
            return ok, row
        finally:
            conn.close()

    ok, row = await asyncio.to_thread(run)
    if not ok:
        await update.message.reply_text("🦊 Такої обіцянки немає.")
        return
    await update.message.reply_text(
        f"↩️ Повернув у чергу: <b>{escape_html((row or {}).get('title') or cid)}</b>",
        parse_mode="HTML")


# ---------- /promise_fulfil_test ----------
#
# Питання Олега 04.08 на реальній парі: обіцянка про дорогу до Матвіївки
# (319402) і пізніша новина «начался ремонт дороги» (321200) — «чому бот не
# зарахував?». Причин рівно три (стаття поза вікном · не збіглася картка
# сутності · суддя сказав «ні»), і мовчазний нуль у звіті їх не розрізняє.
# Ця команда проганяє ОДНУ пару і показує кожен крок.

async def promise_fulfil_test_handler(update, context):
    """/promise_fulfil_test <id обіцянки> <id|URL новини> — чому не зарахувало."""
    from handlers.promises import _allowed
    from handlers.helpers import extract_article_id

    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Використання: /promise_fulfil_test <id обіцянки> <id або URL новини>")
        return
    cid = int(args[0])
    raw = args[1]
    aid = extract_article_id(raw) if "/" in raw else (raw if raw.isdigit() else None)
    if not aid:
        await update.message.reply_text("Не видно id новини.")
        return
    aid = int(aid)
    msg = await update.message.reply_text("🦊 Розбираю пару…")

    def load():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                cur.execute(
                    "SELECT published, coalesce(title_ua, title_ru), "
                    "       coalesce(text_ua, text_ru), region "
                    "FROM articles WHERE id = %s", (aid,))
                art = cur.fetchone()
                quote = ""
                for rev in pp.revisions(cur, [cid]):
                    quote = rev.get("quote") or ""
                    break
                # Чи бачить їх ПРЕ-ФІЛЬТР: спільна картка сутності
                cur.execute(
                    "SELECT e.id, coalesce(e.name_ua, e.name_ru) FROM entities e "
                    "JOIN article_entities ae ON ae.entity_id = e.id "
                    "WHERE ae.article_id = %s AND (e.id = %s OR e.id IN "
                    "  (SELECT entity_id FROM commitment_objects WHERE commitment_id = %s))",
                    (aid, (row or {}).get("subject_entity_id") or 0, cid))
                shared = cur.fetchall()
                cur.execute("SELECT count(*) FROM article_entities WHERE article_id = %s",
                            (aid,))
                art_entities = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM commitment_revisions "
                            "WHERE commitment_id = %s AND article_id = %s", (cid, aid))
                is_source = bool(cur.fetchone())
                cur.execute("SELECT state, confidence, why, applied FROM promise_closures "
                            "WHERE commitment_id = %s AND article_id = %s", (cid, aid))
                already = cur.fetchone()
        finally:
            conn.close()
        return row, art, quote, shared, art_entities, is_source, already

    try:
        row, art, quote, shared, art_entities, is_source, already = \
            await asyncio.to_thread(load)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
        return
    if not row:
        await msg.edit_text(f"🦊 Обіцянки {cid} немає.")
        return
    if not art:
        await msg.edit_text(f"🦊 Статті {aid} немає в норі — інкремент дзеркала "
                            f"її ще не забрав.")
        return

    published, title, text, region = art
    days = (int(time.time()) - int(published or 0)) // 86400
    lines = [f"🦊 <b>{escape_html(row.get('title') or cid)}</b>",
             f"проти: {escape_html(title or aid)}", ""]
    lines.append(f"{'✅' if days <= DEFAULT_DAYS else '⚠️'} Новині {days} дн. "
                 f"— щогодинний прогін дивиться {DEFAULT_DAYS}"
                 + ("" if days <= DEFAULT_DAYS else f"; тут потрібен "
                    f"/promise_fulfil {days + 1}"))
    lines.append(f"{'✅' if region == 1 else '❌'} Регіон: {region} (беремо лише 1)")
    lines.append(f"{'❌' if is_source else '✅'} "
                 + ("це стаття, з якої обіцянку й записали — доказом бути не може"
                    if is_source else "стаття не є джерелом самої обіцянки"))
    if shared:
        lines.append("✅ Спільна картка: "
                     + ", ".join(escape_html(n or "—") for _i, n in shared[:5]))
    else:
        lines.append(f"❌ <b>Спільної картки немає</b> — пре-фільтр цю пару не "
                     f"побачить. Сутностей у статті: {art_entities}"
                     + ("" if art_entities else " (стаття ще не розібрана "
                        "сутнісним шаром)"))
    if already:
        lines.append(f"ℹ️ Пару вже судили: {already[0]}/{already[1]} — "
                     f"{escape_html(already[2] or '')}")

    if not text:
        lines.append("\n❌ У статті немає тексту в норі — судити нема що.")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
        return

    lines.append("\n<i>Питаю суддю (нічого не записую)…</i>")
    await msg.edit_text("\n".join(lines), parse_mode="HTML")
    v = await pj.judge_fulfil(
        {"title": row.get("title"), "owner_text": row.get("owner_text"),
         "deadline": row.get("deadline"), "quote": quote[:400]},
        {"title": title or "", "text": (text or "")[:TEXT_CAP]})
    lines.pop()
    if not v:
        lines.append("\n❌ <b>Суддя не відповів</b> — дивись логи Railway.")
    else:
        mark = {"done": "✅", "failed": "🚫", "none": "➖"}.get(v.get("state"), "•")
        lines.append(f"\n{mark} <b>{v.get('state')}/{v.get('confidence')}</b>: "
                     f"{escape_html(v.get('why') or '')}")
        if v.get("state") == "done" and v.get("confidence") == "high":
            lines.append("<i>Такий вердикт закрив би обіцянку сам.</i>")
        elif v.get("state") in ("done", "failed"):
            lines.append("<i>Такий вердикт пішов би Каті на підтвердження.</i>")
        else:
            lines.append("<i>«none» — обіцянка лишається в черзі.</i>")
    await msg.edit_text(_clip_local("\n".join(lines)), parse_mode="HTML")


def _clip_local(text, limit=4000):
    return text if len(text) <= limit else text[:limit - 1] + "…"
