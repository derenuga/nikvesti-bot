"""
Злиття дублів сутностей — замір масштабу і журнал (docs/ENTITY_MERGE_PLAN.md).

Тут живуть дві речі, обидві навколо КАРТОК (дублі РОЛЕЙ — окрема вісь, вона в
handlers/entity_roles.py):

1. **/entity_scale — read-only замір §6.** Скільки в норі дублів насправді:
   окремо по картках (схожість написання, картки-одноденки, однакове рос.
   написання при різному укр.), окремо по ролях (скільки написань на посаду,
   скільки людей носять кілька ролей, скільки пар знайшов би детектор).
   Нічого не пише і нічого не вирішує — від цих чисел залежить, чи потрібні
   детектор карток, черга і суддя (§7 крок 1).

2. **Журнал злиттів `entity_merges` (§5).** `/entity_dedup` перевішує зв'язки і
   ВИДАЛЯЄ програшну картку — без снапшота помилкове злиття не відкотиш ніяк.
   Тепер кожне злиття лишає по собі повний знімок програшної картки, її
   зв'язків і того стану переможця, поверх якого лягло злиття, тож
   /entity_unmerge відновлює все точно, у зворотному порядку.

Автозлиття без людини тут немає і не буде (§7, «чого не робити»): однофамільці
злипаються незворотно, а `/entity_dedup` дозволений лише тому, що зливає
ТОЧНИЙ збіг нормалізованого імені.
"""

import asyncio
import json
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import bot_db, entity_roles
import entity_pipeline as ep

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

MERGES_DDL = """
CREATE TABLE IF NOT EXISTS entity_merges (
    id             BIGSERIAL PRIMARY KEY,
    winner_id      BIGINT NOT NULL,
    loser_id       BIGINT NOT NULL,
    loser_snapshot JSONB NOT NULL,
    decided_by     TEXT,
    created        BIGINT,
    undone         BIGINT
);
CREATE INDEX IF NOT EXISTS idx_entity_merges_created ON entity_merges (created DESC);
"""

# Trigram-індекси по іменах: без них замір схожості — квадрат по всіх картках.
TRGM_DDL = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS idx_entities_nameua_trgm "
    "ON entities USING gin (lower(name_ua) gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_entities_nameru_trgm "
    "ON entities USING gin (lower(name_ru) gin_trgm_ops)",
]

_schema_done = {"flag": False}


def ensure_schema(force=False):
    if _schema_done["flag"] and not force:
        return
    bot_db.execute(MERGES_DDL)
    _schema_done["flag"] = True


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


# ---------- Журнал злиттів (§5) ----------

def record_merge(cur, winner_id, loser_id, decided_by=None):
    """Знімок ПЕРЕД злиттям — викликати до перевішування зв'язків.

    Працює на переданому курсорі, бо /entity_dedup веде своє з'єднання й одну
    транзакцію на весь прогін: журнал має комітитись рівно з тим злиттям, яке
    описує, інакше знімок брехатиме.

    Крім самої програшної картки зберігаємо і стан ПЕРЕМОЖЦЯ на цей момент
    (імена, аліаси, набір статей). Без нього відкат не знав би, які аліаси
    дописало саме це злиття і які зв'язки переможець мав до нього — а в
    кластері з трьох карток відкат по одній має бути точним."""
    cur.execute(
        "SELECT id, kind, subtype, name_ua, name_ru, aliases, role_last, "
        "       first_seen, last_seen, mentions FROM entities WHERE id = %s",
        (loser_id,))
    row = cur.fetchone()
    if not row:
        return None
    card = {"id": row[0], "kind": row[1], "subtype": row[2], "name_ua": row[3],
            "name_ru": row[4], "aliases": list(row[5] or []), "role_last": row[6],
            "first_seen": row[7], "last_seen": row[8], "mentions": row[9]}
    cur.execute(
        "SELECT article_id, role_at_time, salience FROM article_entities "
        "WHERE entity_id = %s ORDER BY article_id", (loser_id,))
    links = [[r[0], r[1], r[2]] for r in cur.fetchall()]
    cur.execute(
        "SELECT id, name_ua, name_ru, aliases FROM entities WHERE id = %s",
        (winner_id,))
    w = cur.fetchone()
    cur.execute("SELECT article_id FROM article_entities WHERE entity_id = %s",
                (winner_id,))
    w_articles = [r[0] for r in cur.fetchall()]
    # Правила, які вже вказували на програшну картку, мають переїхати на
    # переможця — інакше після другого злиття в кластері вони вели б у нікуди.
    cur.execute(ep.MERGE_RULES_DDL)
    cur.execute(
        "UPDATE entity_merge_rules SET entity_id = %s WHERE entity_id = %s "
        "RETURNING kind, norm", (winner_id, loser_id))
    repointed = [[r[0], r[1]] for r in cur.fetchall()]

    snapshot = {
        "card": card,
        "links": links,
        "winner": {"id": w[0] if w else winner_id,
                   "name_ua": w[1] if w else None,
                   "name_ru": w[2] if w else None,
                   "aliases": list(w[3] or []) if w else [],
                   "articles": w_articles},
        "rules_repointed": repointed,
    }
    cur.execute(
        "INSERT INTO entity_merges (winner_id, loser_id, loser_snapshot, "
        "decided_by, created) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (winner_id, loser_id, json.dumps(snapshot, ensure_ascii=False),
         decided_by, int(time.time())))
    merge_id = cur.fetchone()[0]

    # І головне: запам'ятати САМЕ РІШЕННЯ. Інакше наступна стаття зі старим
    # написанням створить дубль заново, і людина зливатиме його щотижня.
    canon = {ep.norm(w[1]) if w else None, ep.norm(w[2]) if w else None}
    now = int(time.time())
    for nm in [card["name_ua"], card["name_ru"]] + list(card["aliases"]):
        n = ep.norm(nm)
        if not n or n in canon:
            continue
        cur.execute(
            "INSERT INTO entity_merge_rules (kind, norm, entity_id, merge_id, created) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, norm) DO UPDATE SET entity_id = EXCLUDED.entity_id, "
            "merge_id = EXCLUDED.merge_id",
            (card["kind"], n, winner_id, merge_id, now))
    return merge_id


RECALC_AGG_SQL = """
UPDATE entities e SET mentions = coalesce(s.cnt, 0),
                      first_seen = s.fmin, last_seen = s.fmax
FROM (SELECT ae.entity_id, count(*) AS cnt,
             min(a.published) AS fmin, max(a.published) AS fmax
      FROM article_entities ae JOIN articles a ON a.id = ae.article_id
      WHERE ae.entity_id = ANY(%s)
      GROUP BY ae.entity_id) s
WHERE e.id = s.entity_id
"""

RECALC_ROLE_SQL = """
UPDATE entities e SET role_last = sub.role
FROM (SELECT DISTINCT ON (ae.entity_id) ae.entity_id, ae.role_at_time AS role
      FROM article_entities ae JOIN articles a ON a.id = ae.article_id
      WHERE ae.entity_id = ANY(%s)
        AND ae.role_at_time IS NOT NULL AND ae.role_at_time <> ''
      ORDER BY ae.entity_id, a.published DESC) sub
WHERE e.id = sub.entity_id
"""


def restore_merge(merge_id):
    """Відкотити одне злиття зі знімка. Ідемпотентно (повторний виклик каже
    «вже відкочено»). Кластер відкочують у зворотному порядку — id спадаючи."""
    ensure_schema()
    rows = bot_db.query(
        "SELECT winner_id, loser_id, loser_snapshot, undone FROM entity_merges "
        "WHERE id = %s", (merge_id,))
    if not rows:
        return None
    rec = rows[0]
    if rec["undone"]:
        return "already"
    snap = rec["loser_snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)
    card, links = snap["card"], snap["links"]
    winner = snap.get("winner") or {}
    winner_id = rec["winner_id"]
    w_articles = set(winner.get("articles") or [])
    loser_articles = [l[0] for l in links]

    with bot_db.transaction():
        # 1. Повернути картку з тим самим id (на неї могли посилатись експорти)
        bot_db.execute(
            "INSERT INTO entities (id, kind, subtype, name_ua, name_ru, aliases, "
            "role_last, first_seen, last_seen, mentions) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, "
            "  subtype = EXCLUDED.subtype, name_ua = EXCLUDED.name_ua, "
            "  name_ru = EXCLUDED.name_ru, aliases = EXCLUDED.aliases, "
            "  role_last = EXCLUDED.role_last",
            (card["id"], card["kind"], card["subtype"], card["name_ua"],
             card["name_ru"], card["aliases"], card["role_last"],
             card["first_seen"], card["last_seen"], card["mentions"]))
        bot_db.execute(
            "SELECT setval(pg_get_serial_sequence('entities', 'id'), "
            "GREATEST((SELECT max(id) FROM entities), 1))")
        # 2. Зняти з переможця ті статті, що прийшли з програшної картки
        #    (ті, де він і до злиття був, лишаються — на те й знімок).
        strangers = [a for a in loser_articles if a not in w_articles]
        if strangers:
            bot_db.execute(
                "DELETE FROM article_entities WHERE entity_id = %s "
                "AND article_id = ANY(%s)", (winner_id, strangers))
        # 3. Повернути зв'язки програшної картки — з role_at_time і salience
        for aid, role, sal in links:
            bot_db.execute(
                "INSERT INTO article_entities (article_id, entity_id, role_at_time, "
                "salience) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (aid, card["id"], role, sal))
        # 4. Зняти з переможця рівно ті аліаси й імена, які дописало це злиття
        added = ({card["name_ua"], card["name_ru"]} | set(card["aliases"])
                 ) - set(winner.get("aliases") or []) - {None}
        if added:
            bot_db.execute(
                "UPDATE entities SET aliases = ("
                "  SELECT coalesce(array_agg(x ORDER BY x), '{}') "
                "  FROM unnest(aliases) AS x WHERE NOT (x = ANY(%s))) "
                "WHERE id = %s", (sorted(added), winner_id))
        # Ім'я переможця чіпаємо ЛИШЕ там, де його заповнило саме це злиття
        # (dedup робить `kua = kua or oua`, тобто дописує тільки в порожнє).
        # Інакше відкат одного злиття в кластері з трьох стирав би ім'я,
        # яке приніс сусід.
        for col, lname in (("name_ua", card["name_ua"]),
                           ("name_ru", card["name_ru"])):
            if winner.get(col) is None and lname:
                bot_db.execute(
                    f"UPDATE entities SET {col} = NULL "
                    f"WHERE id = %s AND {col} = %s", (winner_id, lname))
        # 5. Перерахувати агрегати обох карток із даних
        both = [card["id"], winner_id]
        bot_db.execute(RECALC_AGG_SQL, (both,))
        bot_db.execute(RECALC_ROLE_SQL, (both,))
        # Знімаємо і пам'ять рішення: відкат має відкочувати не лише дані, а й
        # правило, інакше витяг далі клеїв би написання до переможця.
        bot_db.execute(ep.MERGE_RULES_DDL)
        bot_db.execute("DELETE FROM entity_merge_rules WHERE merge_id = %s",
                       (merge_id,))
        for kind, nrm in (snap.get("rules_repointed") or []):
            bot_db.execute(
                "UPDATE entity_merge_rules SET entity_id = %s "
                "WHERE kind = %s AND norm = %s", (card["id"], kind, nrm))
        bot_db.execute("UPDATE entity_merges SET undone = %s WHERE id = %s",
                       (int(time.time()), merge_id))
    return {"winner_id": winner_id, "loser_id": card["id"],
            "name": card["name_ua"] or card["name_ru"], "links": len(links)}


# ---------- /entity_find ----------

# Зливають ЛЮДЕЙ, організації й місця. Картки event/document у пошуку злиття
# лише заважають: витяг робить із них переказ сюжету статті («замах на
# Дональда Трампа», «Трамп: Другий шанс?»), і на запит «трамп» вони забивають
# екран, ховаючи справжній дубль. Тому за замовчуванням їх немає, а слово
# `all` у команді їх повертає — сміття теж треба чимось дивитись.
MERGEABLE_KINDS = ("person", "org", "place")

FIND_SQL = """
SELECT e.id, e.kind, e.subtype, coalesce(e.name_ua, e.name_ru) AS name,
       e.name_ru, e.role_last, coalesce(e.mentions, 0) AS mentions,
       to_char(to_timestamp(e.first_seen), 'YYYY-MM') AS lo,
       to_char(to_timestamp(e.last_seen), 'YYYY-MM') AS hi,
       array_to_string(e.aliases, ' | ') AS aliases
FROM entities e
WHERE (lower(coalesce(e.name_ua, '')) LIKE %s
    OR lower(coalesce(e.name_ru, '')) LIKE %s
    OR EXISTS (SELECT 1 FROM unnest(e.aliases) AS al WHERE lower(al) LIKE %s))
  AND (%s OR e.kind = ANY(%s))
ORDER BY e.mentions DESC NULLS LAST, e.id
LIMIT 15
"""

FIND_HIDDEN_SQL = """
SELECT e.kind, count(*) AS n
FROM entities e
WHERE (lower(coalesce(e.name_ua, '')) LIKE %s
    OR lower(coalesce(e.name_ru, '')) LIKE %s)
  AND NOT (e.kind = ANY(%s))
GROUP BY e.kind ORDER BY n DESC
"""


# Вибір карток кнопками — той самий патерн, що відбір новин на бек у
# news_archive: тапнув потрібні, тапнув дію. Набирати id руками не треба, і
# зникає найдурніша помилка — одна цифра повз.
#
# ПОРЯДОК ТАПІВ = порядок злиття: перша обрана картка лишається, друга йде в
# аліаси. Це видно на самих кнопках («лишиться» / «зникне»), тож правило не
# треба пам'ятати.
#
# Стан живе в sync_state, а не в пам'яті процесу: редеплой Railway посеред
# вибору не має перетворювати кнопки на мовчазні.

FIND_STATE_PREFIX = "efind:"


def _find_key(chat_id, message_id):
    return f"{FIND_STATE_PREFIX}{chat_id}:{message_id}"


def _find_load(chat_id, message_id):
    raw = bot_db.get_state(_find_key(chat_id, message_id))
    return json.loads(raw) if raw else None


def _find_save(chat_id, message_id, state):
    bot_db.set_state(_find_key(chat_id, message_id),
                     json.dumps(state, ensure_ascii=False))


def _find_markup(state):
    rows = []
    sel = state["sel"]
    for it in state["items"]:
        if it["id"] in sel:
            mark = "✅ лишиться · " if sel[0] == it["id"] else "🗑 зникне · "
        else:
            mark = ""
        rows.append([InlineKeyboardButton(
            f"{mark}{it['name']} ({it['mentions']})",
            callback_data=f"efn:{it['id']}")])
    if len(sel) == 2:
        a = next(i for i in state["items"] if i["id"] == sel[0])
        b = next(i for i in state["items"] if i["id"] == sel[1])
        rows.append([InlineKeyboardButton(
            f"🦊 Злити: {b['name']} → {a['name']}", callback_data="efm:go")])
    return InlineKeyboardMarkup(rows)


def _find_text(state):
    lines = [f"🦊 Картки за «{state['q']}» — {len(state['items'])}\n"]
    for it in state["items"]:
        period = f" · {it['lo']}…{it['hi']}" if it["lo"] else ""
        role = f" · {it['role']}" if it["role"] else ""
        lines.append(f"[{it['id']}] {it['name']} ({it['kind']}, "
                     f"{it['mentions']} згадок){period}{role}")
        if it["aliases"]:
            lines.append(f"      аліаси: {it['aliases']}")
    hid = state.get("hidden") or []
    if hid:
        lines.append("\nСховано (події й документи — це переказ сюжету статті, "
                     "не дублі): "
                     + ", ".join(f"{k} {n}" for k, n in hid)
                     + f"\nПоказати: /entity_find {state['q']} all")
    n = len(state["sel"])
    lines.append("\nТапни дубль — ПЕРША обрана картка лишається, друга піде в "
                 "аліаси." if n < 2 else "\nОбрано дві — тисни «Злити».")
    return "\n".join(lines)[:4000]


async def entity_find_handler(update, context):
    """/entity_find <текст> — знайти картки за іменем чи аліасом і дати кнопки.

    Без цього /entity_merge непридатний: id карток нізвідки взяти, а
    /entity_export віддає CSV на 40 тисяч рядків."""
    if not _allowed(update):
        return
    args = list(context.args or [])
    show_all = bool(args) and args[-1].lower() == "all"
    if show_all:
        args = args[:-1]
    q = " ".join(args).strip()
    if len(q) < 2:
        await update.message.reply_text(
            "Формат: /entity_find <частина імені>\n"
            "Напр.: /entity_find сєнкевич\n"
            "Далі тапаєш дублі кнопками — перша обрана картка лишається.\n\n"
            "Показує людей, організації й місця. Щоб побачити ще й події та "
            "документи: /entity_find <текст> all")
        return
    like = f"%{q.lower()}%"
    try:
        rows = await bot_db.aquery(
            FIND_SQL, (like, like, like, show_all, list(MERGEABLE_KINDS)))
        hidden = [] if show_all else await bot_db.aquery(
            FIND_HIDDEN_SQL, (like, like, list(MERGEABLE_KINDS)))
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    if not rows:
        tail = ("" if not hidden else
                "\nЄ картки інших типів: "
                + ", ".join(f"{h['kind']} {h['n']}" for h in hidden)
                + f"\nПоказати: /entity_find {q} all")
        await update.message.reply_text(f"За «{q}» карток не знайшов." + tail)
        return
    state = {"q": q, "hidden": [[h["kind"], h["n"]] for h in hidden],
             "sel": [], "items": [
        {"id": r["id"], "name": r["name"], "kind": r["kind"],
         "mentions": r["mentions"], "lo": r["lo"], "hi": r["hi"],
         "role": r["role_last"], "aliases": r["aliases"]} for r in rows]}
    msg = await update.message.reply_text(_find_text(state),
                                          reply_markup=_find_markup(state))
    await asyncio.to_thread(_find_save, msg.chat_id, msg.message_id, state)


async def entity_find_callback(update, context):
    """efn:<id> — перемкнути вибір картки; efm:go — показати прев'ю злиття."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    chat_id, message_id = query.message.chat_id, query.message.message_id
    state = await asyncio.to_thread(_find_load, chat_id, message_id)
    if not state:
        await query.answer("Результати застаріли — повтори /entity_find.",
                           show_alert=True)
        return

    if query.data.startswith("efn:"):
        eid = int(query.data.split(":", 1)[1])
        sel = state["sel"]
        if eid in sel:
            sel.remove(eid)
        elif len(sel) >= 2:
            await query.answer("Уже обрано дві. Зніми одну, щоб обрати іншу.",
                               show_alert=True)
            return
        else:
            sel.append(eid)
        await query.answer()
        await asyncio.to_thread(_find_save, chat_id, message_id, state)
        try:
            await query.edit_message_text(_find_text(state),
                                          reply_markup=_find_markup(state))
        except Exception:
            pass       # «message is not modified» при подвійному тапі
        return

    if query.data != "efm:go" or len(state["sel"]) != 2:
        await query.answer()
        return
    await query.answer()
    winner_id, loser_id = state["sel"]
    try:
        p, err = await asyncio.to_thread(merge_preview, winner_id, loser_id)
    except Exception as e:
        await query.edit_message_text(f"❌ {type(e).__name__}: {e}")
        return
    if err:
        await query.answer(f"Не зливаю: {err}", show_alert=True)
        return
    await asyncio.to_thread(
        bot_db.execute, "DELETE FROM sync_state WHERE key = %s",
        (_find_key(chat_id, message_id),))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Злити", callback_data=f"emg:{winner_id}:{loser_id}"),
        InlineKeyboardButton("❌ Скасувати", callback_data="emg:0:0"),
    ]])
    await query.edit_message_text(format_preview(p), reply_markup=kb)


# ---------- /entity_merge ----------
#
# Крок 3 §7: ручне злиття карток на вимогу. Детектора, черги й судді тут немає
# свідомо — вони чекають рішення за числами заміру, а ця команда вже знімає
# біль там, де дубль знайдено очима («Сєнкевич» при «Олександр Сєнкевич»).
#
# Запобіжники, без яких злиття робити не можна (§5 і §7 «чого не робити»):
#   • різні kind не зливаються НІКОЛИ (людина з організацією);
#   • перед злиттям — прев'ю з підставою: спільні статті й спільні сусіди, бо
#     саме співпоява відрізняє одну людину від однофамільця (§2), а не ім'я;
#   • рішення тапом по кнопці, а не самим фактом набору команди: id набирають
#     руками, і одна цифра помилки — це злиття не тих карток;
#   • знімок у entity_merges → /entity_unmerge повертає все, разом із
#     role_at_time кожного зв'язку.

OVERLAP_SQL = """
WITH a AS (SELECT article_id FROM article_entities WHERE entity_id = %s),
     b AS (SELECT article_id FROM article_entities WHERE entity_id = %s)
SELECT (SELECT count(*) FROM a JOIN b USING (article_id)) AS shared_articles,
       (SELECT count(*) FROM (
            SELECT entity_id FROM article_entities
            WHERE article_id IN (SELECT article_id FROM a) AND entity_id <> %s
            INTERSECT
            SELECT entity_id FROM article_entities
            WHERE article_id IN (SELECT article_id FROM b) AND entity_id <> %s
        ) t) AS shared_neighbours
"""

CARD_SQL = """
SELECT id, kind, subtype, name_ua, name_ru, role_last,
       coalesce(mentions, 0) AS mentions,
       to_char(to_timestamp(first_seen), 'YYYY-MM') AS lo,
       to_char(to_timestamp(last_seen), 'YYYY-MM') AS hi,
       array_to_string(aliases, ' | ') AS aliases
FROM entities WHERE id = %s
"""


def merge_preview(winner_id, loser_id):
    a = bot_db.query(CARD_SQL, (winner_id,))
    b = bot_db.query(CARD_SQL, (loser_id,))
    if not a or not b:
        missing = [i for i, r in ((winner_id, a), (loser_id, b)) if not r]
        return None, f"немає карток: {', '.join(str(i) for i in missing)}"
    a, b = a[0], b[0]
    if a["kind"] != b["kind"]:
        return None, (f"різні типи ({a['kind']} і {b['kind']}) — такі картки "
                      f"не зливаються ніколи")
    ov = bot_db.query(OVERLAP_SQL, (winner_id, loser_id, loser_id, winner_id))[0]
    return {"a": a, "b": b, "overlap": ov}, None


def format_preview(p):
    def card(letter, c, tail):
        period = f"{c['lo']}…{c['hi']}" if c["lo"] else "період невідомий"
        role = f"\n   роль: {c['role_last']}" if c["role_last"] else ""
        al = f"\n   аліаси: {c['aliases']}" if c["aliases"] else ""
        ru = f" / {c['name_ru']}" if c["name_ru"] else ""
        return (f"{letter} [{c['id']}] {c['name_ua'] or c['name_ru']}{ru}"
                f"\n   {c['mentions']} згадок · {period}{role}{al}\n   {tail}")

    ov = p["overlap"]
    return (
        "🦊 Злити ці картки?\n\n"
        + card("✅", p["a"], "ЛИШАЄТЬСЯ") + "\n"
        + card("🗑", p["b"], "піде в аліаси й зникне") + "\n\n"
        + f"Спільних статей: {ov['shared_articles']} · "
          f"спільних сусідів: {ov['shared_neighbours']}\n"
        + "Саме співпоява відрізняє одну людину від однофамільця — "
          "якщо спільного світу немає, це різні люди.\n\n"
        + "Щоб лишилась ІНША картка — поміняй id місцями в команді."
    )


def merge_cards(winner_id, loser_id, decided_by=None):
    """Перевісити зв'язки, злити аліаси, перерахувати агрегати, прибрати
    програшну картку — зі знімком у журналі. Спільне ядро з /entity_dedup,
    але для довільної пари карток, а не для точного збігу імен."""
    ensure_schema()
    conn = ep.connect()
    cur = conn.cursor()
    try:
        cur.execute(MERGES_DDL)
        cur.execute("SELECT kind, name_ua, name_ru, aliases FROM entities WHERE id = %s",
                    (winner_id,))
        w = cur.fetchone()
        cur.execute("SELECT kind, name_ua, name_ru, aliases FROM entities WHERE id = %s",
                    (loser_id,))
        l = cur.fetchone()
        if not w or not l:
            raise ValueError("картки немає в норі")
        if w[0] != l[0]:
            raise ValueError(f"різні типи: {w[0]} і {l[0]}")

        merge_id = record_merge(cur, winner_id, loser_id, decided_by)

        # Аліаси: усе, чим програшну картку називали, має лишитись шукабельним —
        # інакше пошук по народній назві після злиття не знайде нічого (§5).
        aliases = set(w[3] or [])
        canon = {ep.norm(w[1]), ep.norm(w[2])}
        for nm in (l[1], l[2]):
            if nm and ep.norm(nm) not in canon:
                aliases.add(nm)
        for nm in (l[3] or []):
            if nm and ep.norm(nm) not in canon:
                aliases.add(nm)
        name_ua = w[1] or l[1]
        name_ru = w[2] or l[2]

        cur.execute(
            "UPDATE article_entities ae SET entity_id = %s "
            "WHERE entity_id = %s AND NOT EXISTS ("
            "  SELECT 1 FROM article_entities x "
            "  WHERE x.article_id = ae.article_id AND x.entity_id = %s)",
            (winner_id, loser_id, winner_id))
        moved = cur.rowcount
        cur.execute("DELETE FROM article_entities WHERE entity_id = %s", (loser_id,))
        cur.execute(
            "UPDATE entities SET name_ua = %s, name_ru = %s, aliases = %s WHERE id = %s",
            (name_ua, name_ru, sorted(aliases), winner_id))
        cur.execute("DELETE FROM entities WHERE id = %s", (loser_id,))
        cur.execute(RECALC_AGG_SQL, ([winner_id],))
        cur.execute(RECALC_ROLE_SQL, ([winner_id],))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return {"merge_id": merge_id, "moved": moved, "aliases": sorted(aliases)}


async def entity_merge_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not all(a.isdigit() for a in args[:2]):
        await update.message.reply_text(
            "Формат: /entity_merge <id що ЛИШАЄТЬСЯ> <id дубля>\n"
            "Напр.: /entity_merge 1204 8871\n\n"
            "Знайти id: /entity_find <ім'я>\n"
            "Журнал і відкат: /entity_merge_log, /entity_unmerge <id>")
        return
    winner_id, loser_id = int(args[0]), int(args[1])
    if winner_id == loser_id:
        await update.message.reply_text("Це одна й та сама картка.")
        return
    try:
        p, err = await asyncio.to_thread(merge_preview, winner_id, loser_id)
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")
        return
    if err:
        await update.message.reply_text(f"🦊 Не зливаю: {err}")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Злити", callback_data=f"emg:{winner_id}:{loser_id}"),
        InlineKeyboardButton("❌ Скасувати", callback_data="emg:0:0"),
    ]])
    await update.message.reply_text(format_preview(p), reply_markup=kb)


async def entity_merge_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        _, w, l = query.data.split(":", 2)
        winner_id, loser_id = int(w), int(l)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()
    base = (query.message.text or "").split("\n\nЩоб лишилась")[0]
    if not winner_id:
        await query.edit_message_text(base + "\n\n❌ Скасовано.")
        return
    who = query.from_user.full_name if query.from_user else None
    try:
        res = await asyncio.to_thread(merge_cards, winner_id, loser_id, who)
    except Exception as e:
        await query.edit_message_text(base + f"\n\n❌ Не злилось: {e}")
        return
    await query.edit_message_text(
        base + f"\n\n✅ Злито: {res['moved']} зв'язків перевішено на {winner_id}, "
               f"картка {loser_id} прибрана.\n"
               f"Аліаси: {' | '.join(res['aliases']) or '—'}\n"
               f"Відкат: /entity_unmerge {res['merge_id']}")


# ---------- /entity_merge_log ----------

async def entity_merge_log_handler(update, context):
    """Журнал злиттів: що з чим злилось, коли і скільки зв'язків на кону."""
    if not _allowed(update):
        return
    args = context.args or []
    try:
        n = int(args[0]) if args else 20
    except ValueError:
        n = 20
    n = min(max(n, 1), 100)

    def build():
        ensure_schema()
        return bot_db.query(
            "SELECT id, winner_id, loser_id, decided_by, undone, "
            "       to_char(to_timestamp(created), 'DD.MM HH24:MI') AS at, "
            "       loser_snapshot #>> '{card,name_ua}' AS lname, "
            "       loser_snapshot #>> '{card,name_ru}' AS lname_ru, "
            "       jsonb_array_length(loser_snapshot -> 'links') AS links, "
            "       (SELECT coalesce(w.name_ua, w.name_ru) FROM entities w "
            "        WHERE w.id = m.winner_id) AS wname "
            "FROM entity_merges m ORDER BY id DESC LIMIT %s", (n,))

    try:
        rows = await asyncio.to_thread(build)
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    if not rows:
        await update.message.reply_text(
            "Журнал злиттів порожній — /entity_dedup ще не зливав нічого "
            "з моменту, коли журнал з'явився.")
        return
    lines = ["🦊 Журнал злиттів карток\n"]
    for r in rows:
        mark = " ↩️ відкочено" if r["undone"] else ""
        lines.append(
            f"[{r['id']}] {r['at']} · «{r['lname'] or r['lname_ru']}» "
            f"({r['loser_id']}, {r['links']} зв'язків) → "
            f"«{r['wname']}» ({r['winner_id']}){mark}")
    lines.append("\nВідкотити: /entity_unmerge <id>")
    await update.message.reply_text("\n".join(lines))


# ---------- /entity_unmerge ----------

async def entity_unmerge_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Формат: /entity_unmerge <id зі журналу>\n"
            "Журнал: /entity_merge_log\n"
            "Кластер із кількох карток відкочувати у зворотному порядку "
            "(спершу більший id).")
        return
    merge_id = int(args[0])
    msg = await update.message.reply_text(f"🦊 Відкочую злиття {merge_id}…")
    try:
        res = await asyncio.to_thread(restore_merge, merge_id)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось відкотити: {type(e).__name__}: {e}")
        return
    if res is None:
        await msg.edit_text(f"Запису {merge_id} у журналі немає.")
    elif res == "already":
        await msg.edit_text(f"Злиття {merge_id} вже відкочене.")
    else:
        await msg.edit_text(
            f"🦊 Відновлено картку «{res['name']}» (id {res['loser_id']}) "
            f"з {res['links']} зв'язками; агрегати обох карток перераховано.")


# ---------- /entity_scale (замір §6) ----------

SCALE_RARE_SQL = """
SELECT kind, count(*) FILTER (WHERE coalesce(mentions, 0) <= 2) AS rare,
       count(*) AS total
FROM entities GROUP BY kind ORDER BY total DESC
"""

SCALE_SIM_SQL = """
SELECT count(*) AS n FROM entities a JOIN entities b
  ON a.kind = b.kind AND a.id < b.id
WHERE a.name_ua IS NOT NULL AND b.name_ua IS NOT NULL
  AND coalesce(a.mentions, 0) >= %s AND coalesce(b.mentions, 0) >= %s
  AND lower(a.name_ua) %% lower(b.name_ua)
"""

SCALE_SIM_TOP_SQL = """
SELECT a.kind, a.name_ua AS an, b.name_ua AS bn, a.mentions AS am, b.mentions AS bm,
       similarity(lower(a.name_ua), lower(b.name_ua)) AS sim
FROM entities a JOIN entities b ON a.kind = b.kind AND a.id < b.id
WHERE a.name_ua IS NOT NULL AND b.name_ua IS NOT NULL
  AND coalesce(a.mentions, 0) >= %s AND coalesce(b.mentions, 0) >= %s
  AND lower(a.name_ua) %% lower(b.name_ua)
ORDER BY sim DESC, a.mentions + b.mentions DESC LIMIT 8
"""

SCALE_RU_SQL = """
SELECT name_ru, count(*) AS n, array_agg(name_ua) AS uas, array_agg(mentions) AS ms
FROM entities WHERE kind = 'person' AND name_ru IS NOT NULL AND name_ua IS NOT NULL
GROUP BY name_ru HAVING count(*) > 1
ORDER BY count(*) DESC, sum(mentions) DESC
"""

SCALE_ROLE_TOTALS_SQL = """
SELECT count(*) AS links,
       count(DISTINCT role_at_time) AS raw_variants,
       count(DISTINCT role_norm(role_at_time)) AS norm_variants,
       count(DISTINCT entity_id) AS people
FROM article_entities
WHERE role_at_time IS NOT NULL AND role_norm(role_at_time) IS NOT NULL
"""

SCALE_ROLE_TOP_SQL = """
SELECT role_norm(role_at_time) AS role, count(*) AS links,
       count(DISTINCT entity_id) AS people
FROM article_entities
WHERE role_at_time IS NOT NULL AND role_norm(role_at_time) IS NOT NULL
GROUP BY 1 ORDER BY links DESC LIMIT 8
"""

SCALE_ROLE_MULTI_SQL = """
SELECT count(*) AS n FROM (
  SELECT ae.entity_id
  FROM article_entities ae
  WHERE ae.role_at_time IS NOT NULL AND role_norm(ae.role_at_time) IS NOT NULL
  GROUP BY ae.entity_id HAVING count(DISTINCT role_norm(ae.role_at_time)) > 3
) t
"""

SCALE_ROLE_MULTI_TOP_SQL = """
SELECT coalesce(e.name_ua, e.name_ru) AS name,
       count(DISTINCT role_norm(ae.role_at_time)) AS variants
FROM article_entities ae JOIN entities e ON e.id = ae.entity_id
WHERE ae.role_at_time IS NOT NULL AND role_norm(ae.role_at_time) IS NOT NULL
GROUP BY e.id, e.name_ua, e.name_ru
HAVING count(DISTINCT role_norm(ae.role_at_time)) > 3
ORDER BY variants DESC LIMIT 5
"""

# Нижче цієї кількості згадок картку в замір схожості не беремо: сотні
# одноразових написань дають шум, який усе одно ніхто не буде розбирати руками.
SIM_MIN_MENTIONS = 2
SIM_THRESHOLD = 0.8


def measure():
    """Read-only замір §6 обома осями. Нічого не пише в дані — лише може
    створити trgm-індекси (без них перебір схожості квадратичний)."""
    entity_roles.ensure_schema()
    out = {"trgm": True}
    try:
        for stmt in TRGM_DDL:
            bot_db.execute(stmt)
    except Exception as e:
        out["trgm"] = False
        out["trgm_error"] = str(e)

    out["rare"] = bot_db.query(SCALE_RARE_SQL)
    out["entities"] = sum(r["total"] for r in out["rare"])

    if out["trgm"]:
        with bot_db.session():
            bot_db.execute(f"SET pg_trgm.similarity_threshold = {SIM_THRESHOLD}")
            out["sim_pairs"] = bot_db.query(
                SCALE_SIM_SQL, (SIM_MIN_MENTIONS, SIM_MIN_MENTIONS))[0]["n"]
            out["sim_top"] = bot_db.query(
                SCALE_SIM_TOP_SQL, (SIM_MIN_MENTIONS, SIM_MIN_MENTIONS))

    ru = bot_db.query(SCALE_RU_SQL)
    out["ru_groups"] = [r for r in ru
                        if len({u for u in (r["uas"] or []) if u}) > 1]
    out["roles"] = bot_db.query(SCALE_ROLE_TOTALS_SQL)[0]
    out["role_top"] = bot_db.query(SCALE_ROLE_TOP_SQL)
    out["role_multi"] = bot_db.query(SCALE_ROLE_MULTI_SQL)[0]["n"]
    out["role_multi_top"] = bot_db.query(SCALE_ROLE_MULTI_TOP_SQL)
    names, pairs = entity_roles.find_pairs()
    out["role_scan_roles"] = len(names)
    out["role_pairs"] = len(pairs)
    out["role_pairs_strong"] = sum(1 for p in pairs if p[2] >= 4)
    out["role_pairs_top"] = pairs[:5]
    return out


def format_measure(m):
    lines = ["🦊 Масштаб дублів у норі (замір, нічого не змінено)\n",
             "━━ КАРТКИ ━━"]
    lines.append(f"Сутностей усього: {m['entities']}")
    for r in m["rare"]:
        pct = (100 * r["rare"] // r["total"]) if r["total"] else 0
        lines.append(f"  {r['kind']}: {r['total']}, з них ≤2 згадки — "
                     f"{r['rare']} ({pct}%)")
    if m.get("trgm"):
        lines.append(f"\nПар зі схожим написанням (similarity > {SIM_THRESHOLD}, "
                     f"обидві картки від {SIM_MIN_MENTIONS} згадок): {m['sim_pairs']}")
        for r in m.get("sim_top") or []:
            lines.append(f"  [{r['kind']}] {r['an']} ({r['am']}) ~ "
                         f"{r['bn']} ({r['bm']}) — {r['sim']:.2f}")
    else:
        lines.append(f"\npg_trgm недоступний — схожість написання не міряна "
                     f"({m.get('trgm_error', '')[:100]})")
    ru = m["ru_groups"]
    lines.append(f"\nОдне рос. написання, різні укр. (випадок «Кім/Ким»): {len(ru)}")
    for r in ru[:5]:
        pairs = ", ".join(f"{u} ({mm})" for u, mm in zip(r["uas"], r["ms"]) if u)
        lines.append(f"  {r['name_ru']}: {pairs}")

    ro = m["roles"]
    pl = entity_roles.plural
    lines.append("\n━━ РОЛІ ━━")
    lines.append(f"Зв'язків з роллю: {ro['links']} · носіїв: {ro['people']}")
    lines.append(f"Написань ролі: {ro['raw_variants']} сирих → "
                 f"{ro['norm_variants']} після нормалізації")
    lines.append(f"Людей із >3 різними ролями: {m['role_multi']}")
    for r in m.get("role_multi_top") or []:
        lines.append(f"  {r['name']}: {r['variants']} ролей")
    lines.append("\nНайчастіші ролі:")
    for r in m.get("role_top") or []:
        lines.append(f"  «{r['role']}» — "
                     f"{pl(r['links'], 'зв’язок', 'зв’язки', 'зв’язків')}, "
                     f"{pl(r['people'], 'носій', 'носії', 'носіїв')}")
    lines.append(f"\nДетектор знайшов пар-кандидатів: {m['role_pairs']} "
                 f"(з них сильних: {m['role_pairs_strong']}), "
                 f"ролей у переборі: {m['role_scan_roles']}")
    for a, b, score, sig, cls, detail, stake, *_ in m.get("role_pairs_top") or []:
        lines.append(f"  «{a}» ~ «{b}» ({score:.0f}: {sig})")
    lines.append("\nРозкладка кандидатів по класах: /entity_classes")
    lines.append("Питати кнопками по ролях: /roles_dedup · стан: /roles")
    return "\n".join(lines)


# ---------- /entity_classes (розкладка кандидатів по класах) ----------
#
# Замір 02.08 дав 3757 пар по картках і 2091 по ролях. Черга «одна пара — одне
# питання» на такому обсязі не працює фізично, тому перед тим як будувати
# масове підтвердження, треба знати РОЗМІР КОЖНОГО КЛАСУ: якщо перестановок
# слів три тисячі — картки закриваються за вечір одним підтвердженням на клас;
# якщо триста — масовість не варта коду. Розкладка read-only, вона саме про це.

SCALE_SIM_ALL_SQL = """
SELECT a.kind, a.name_ua AS an, b.name_ua AS bn, a.mentions AS am, b.mentions AS bm
FROM entities a JOIN entities b ON a.kind = b.kind AND a.id < b.id
WHERE a.name_ua IS NOT NULL AND b.name_ua IS NOT NULL
  AND coalesce(a.mentions, 0) >= %s AND coalesce(b.mentions, 0) >= %s
  AND lower(a.name_ua) %% lower(b.name_ua)
"""


def _tally(pairs):
    """pairs: [(cls, detail, ліва, права)] → зведення по класах + гістограма
    зайвих слів класу «уточнення» + поділ цього класу на доповнення й
    розрізнювачі (саме він і вирішує, чи можна закривати клас гуртом)."""
    classes, extra = {}, {}
    for cls, detail, left, right in pairs:
        slot = classes.setdefault(cls, {"n": 0, "examples": []})
        slot["n"] += 1
        if len(slot["examples"]) < 2:
            slot["examples"].append(f"{left} ~ {right}")
        if cls.startswith("containment") and detail:
            for w in detail.split():
                extra[w] = extra.get(w, 0) + 1
    bulk_yes = sum(s["n"] for c, s in classes.items()
                   if c in entity_roles.BULK_MERGE)
    bulk_no = sum(s["n"] for c, s in classes.items()
                  if c in entity_roles.BULK_REJECT)
    return {"total": len(pairs), "classes": classes,
            "extra": sorted(extra.items(), key=lambda kv: -kv[1])[:12],
            "bulk_yes": bulk_yes, "bulk_no": bulk_no}


def classify_cards(threshold=SIM_THRESHOLD):
    """УВАГА на поріг. Оператор `%` бере його з налаштування сеансу
    pg_trgm.similarity_threshold, а не з тексту запиту. Перший прогін
    /entity_classes цього не виставив і мовчки поїхав на дефолтних 0.3 — замість
    3 757 пар вийшло 194 308, з них 92% сміття («вулиця Повстанська» ~ «вулиця
    4 Ялтинська»), і розкладка була неспівставна з /entity_scale. Тому поріг
    ставиться явно, в одній сесії з запитом, і друкується у звіті."""
    with bot_db.session():
        bot_db.execute(f"SET pg_trgm.similarity_threshold = {threshold}")
        rows = bot_db.query(SCALE_SIM_ALL_SQL, (SIM_MIN_MENTIONS, SIM_MIN_MENTIONS))
    pairs = []
    for r in rows:
        a, b = entity_roles.role_norm(r["an"]), entity_roles.role_norm(r["bn"])
        if not a or not b:
            continue
        cls, detail = entity_roles.classify_pair(a, b)
        pairs.append((cls, detail, f"[{r['kind']}] {r['an']}", r["bn"]))
    return _tally(pairs)


def classify_roles():
    _names, rows = entity_roles.find_pairs()
    return _tally([(cls, detail, a, b)
               for a, b, _s, _sig, cls, detail, _st, *_ in rows])


def format_classes(title, data):
    lines = [f"🦊 {title} · {data['total']} пар\n"]
    if not data["total"]:
        lines.append("Кандидатів немає.")
        return "\n".join(lines)
    order = sorted(data["classes"].items(), key=lambda kv: -kv[1]["n"])
    for cls, slot in order:
        pct = 100 * slot["n"] // data["total"]
        lines.append(f"{entity_roles.CLASS_LABELS.get(cls, cls)}: "
                     f"{slot['n']} ({pct}%)")
        for ex in slot["examples"]:
            lines.append(f"   · {ex}")
    if data["extra"]:
        lines.append("\nЗайві слова в «уточненнях» (що саме відрізняє пару):")
        lines.append("   " + ", ".join(f"{w} ×{n}" for w, n in data["extra"]))
    manual = data["total"] - data["bulk_yes"] - data["bulk_no"]
    lines.append(f"\nГуртом «так»: {data['bulk_yes']} · гуртом «ні»: "
                 f"{data['bulk_no']} · по одній: {manual}")
    return "\n".join(lines)


async def entity_classes_handler(update, context):
    """/entity_classes — розкладка кандидатів по класах, обидві осі, read-only.
    Від цих чисел залежить, чи будувати масове підтвердження класом."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    args = context.args or []
    try:
        thr = float(args[0]) if args else SIM_THRESHOLD
    except ValueError:
        thr = SIM_THRESHOLD
    thr = min(max(thr, 0.3), 0.95)
    msg = await update.message.reply_text("🦊 Розкладаю кандидатів по класах…")
    try:
        cards = await asyncio.to_thread(classify_cards, thr)
    except Exception as e:
        await msg.edit_text(f"❌ Картки не розклались: {type(e).__name__}: {e}")
        return
    await msg.edit_text(
        format_classes(f"Класи кандидатів · КАРТКИ (схожість > {thr})",
                       cards)[:4000])
    try:
        roles = await asyncio.to_thread(classify_roles)
    except Exception as e:
        await update.message.reply_text(f"❌ Ролі не розклались: {type(e).__name__}: {e}")
        return
    await update.message.reply_text(
        format_classes("Класи кандидатів · РОЛІ", roles)[:4000]
        + "\n\nМасового підтвердження класом ще немає — це наступний крок, "
          "коли скажеш, який клас закривати гуртом.")


async def entity_scale_handler(update, context):
    """/entity_scale — крок 1 §7: показати масштаб числами, окремо по картках
    і окремо по ролях. Read-only."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text(
        "🦊 Міряю масштаб дублів (це кілька важких запитів, хвилинка)…")
    try:
        m = await asyncio.to_thread(measure)
    except Exception as e:
        await msg.edit_text(f"❌ Замір не вдався: {type(e).__name__}: {e}")
        return
    text = format_measure(m)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await msg.edit_text(text)
