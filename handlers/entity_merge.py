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

from handlers import bot_db, entity_roles

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
    snapshot = {
        "card": card,
        "links": links,
        "winner": {"id": w[0] if w else winner_id,
                   "name_ua": w[1] if w else None,
                   "name_ru": w[2] if w else None,
                   "aliases": list(w[3] or []) if w else [],
                   "articles": w_articles},
    }
    cur.execute(
        "INSERT INTO entity_merges (winner_id, loser_id, loser_snapshot, "
        "decided_by, created) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (winner_id, loser_id, json.dumps(snapshot, ensure_ascii=False),
         decided_by, int(time.time())))
    return cur.fetchone()[0]


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
        bot_db.execute("UPDATE entity_merges SET undone = %s WHERE id = %s",
                       (int(time.time()), merge_id))
    return {"winner_id": winner_id, "loser_id": card["id"],
            "name": card["name_ua"] or card["name_ru"], "links": len(links)}


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
    n_roles, pairs = entity_roles.find_pairs()
    out["role_scan_roles"] = n_roles
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
    for a, b, score, sig in m.get("role_pairs_top") or []:
        lines.append(f"  «{a}» ~ «{b}» ({score:.0f}: {sig})")
    lines.append("\nПитати кнопками по ролях: /roles_dedup · стан: /roles")
    return "\n".join(lines)


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
