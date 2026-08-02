"""
Сміття в КАРТКАХ сутнісного шару — §3 docs/ENTITY_DATA_FIXES.md.

Це третя вісь ремонту, поруч зі злиттям карток (`entity_merge.py`) і каноном
ролей (`entity_roles.py`). Різниця в тому, що тут картку не з чим зливати:
вона взагалі не мала з'являтись.

**Дві купи, обидві — наслідок одного дефекту витягу: у картку писалась не
НАЗВА, а переказ статті.**

1. **Одноразові `event`/`document`.** «Замах на Дональда Трампа», «Трамп:
   Другий шанс?» — таку назву неможливо повторити дослівно в іншій статті,
   тому картка живе рівно в одній і не зв'язує нічого ні з чим. Промпт витягу
   виправлено 02.08 (подія має бути назвою, яку можна повторити), але старі
   картки лишились тисячами. Критерій прибирання рівно один і перевіряється в
   даних, а не на око: **картка згадана не більш ніж в ОДНІЙ статті**, тобто
   між статтями не повторюється жодного разу. Дві статті — вже повтор, така
   картка щось зв'язує, і ми її не чіпаємо.

2. **Посади, записані як організації.** Картки `org` «Президент США»,
   «Президент України» — це не установи, це посади, і вони вже є на своєму
   місці: у `article_entities.role_at_time` людини. Ловляться двома
   сигналами: те саме написання ТРАПЛЯЄТЬСЯ роллю в норі, або перше слово
   назви — слово посади. Сигнал показується в списку, рішення — людина.

**Чого тут немає і не буде:** автоматичного прибирання. Прогін одноразових
показує масштаб ЧИСЛОМ і чекає підтвердження кнопкою; посади прибираються
поштучно, по одному тапу на картку.

**Відкат.** Видалення картки незворотне так само, як злиття, тому ходить тим
самим журналом `entity_merges` (`record_purge`, winner_id = 0): одна
картка — `/entity_unmerge <id>`, весь прогін — `/entity_junk_undo <прогін>`.
Без цього масове прибирання не можна було б робити взагалі.

**Кого не чіпаємо навіть у купі одноразових** — усе, чого вже торкалась
людина або довідник:
  • картка, на яку вказує правило злиття (`entity_merge_rules`);
  • картка, в яку людина щось зливала (журнал, не відкочений);
  • картка, вказана органом канону посади (`role_canon.org_entity_id`) —
    видалення лишило б афіліацію посилатись у нікуди. Такі рахуються окремо
    й показуються числом: лікуються через /roles_org, а не тут.
"""

import asyncio
import json
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import bot_db, entity_merge as em, entity_roles as er
import entity_pipeline as ep

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# Переказом сюжету витяг робив саме ці два типи. Людей, організації й місця
# сюди не пускаємо ніколи: одноразова згадка людини — це нормальна людина,
# а не сміття (§2 доку: рідкісний носій ≠ помилка).
JUNK_KINDS = ("event", "document")

# Скільки статей робить картку придатною. 1 — картка живе в одній статті й не
# зв'язує нічого; 2 — вже повтор між статтями, тобто назву вдалось ужити
# двічі, і це саме те, заради чого сутнісний шар існує.
ONEOFF_MAX_ARTICLES = 1


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


def _ensure():
    """Схеми, від яких залежать запити нижче: журнал (він же адреса відкату),
    правила злиття і довідник ролей (role_canon + SQL-функція role_norm)."""
    em.ensure_schema()
    er.ensure_schema()
    bot_db.execute(ep.MERGE_RULES_DDL)


# Захищені картки — усе, чого вже торкалась людина або довідник. Умова живе
# одним шматком, бо потрібна і вибірці на прибирання, і лічильнику захищених:
# розійдуться — звіт покаже одне число, а прибере інше.
PROTECTED_SQL = """
  (EXISTS (SELECT 1 FROM entity_merge_rules r WHERE r.entity_id = e.id)
   OR EXISTS (SELECT 1 FROM entity_merges m
              WHERE m.winner_id = e.id AND m.undone IS NULL)
   OR EXISTS (SELECT 1 FROM role_canon rc WHERE rc.org_entity_id = e.id))
"""

ONEOFF_SQL = f"""
SELECT e.id, e.kind, coalesce(e.name_ua, e.name_ru) AS name,
       coalesce(l.n, 0) AS articles
FROM entities e
LEFT JOIN (SELECT entity_id, count(DISTINCT article_id) AS n
           FROM article_entities GROUP BY entity_id) l ON l.entity_id = e.id
WHERE e.kind = ANY(%s) AND coalesce(l.n, 0) <= %s AND NOT {PROTECTED_SQL}
ORDER BY e.id
"""

ONEOFF_COUNTS_SQL = f"""
SELECT e.kind,
       count(*) AS total,
       count(*) FILTER (WHERE coalesce(l.n, 0) <= %s AND NOT {PROTECTED_SQL}) AS oneoff,
       count(*) FILTER (WHERE coalesce(l.n, 0) <= %s AND {PROTECTED_SQL}) AS protected,
       count(*) FILTER (WHERE coalesce(l.n, 0) > %s) AS repeated
FROM entities e
LEFT JOIN (SELECT entity_id, count(DISTINCT article_id) AS n
           FROM article_entities GROUP BY entity_id) l ON l.entity_id = e.id
WHERE e.kind = ANY(%s)
GROUP BY e.kind ORDER BY total DESC
"""


def scan_oneoff():
    """Скільки одноразових event/document у норі — і скільки лишається
    працювати (повторюваних) та скільки захищено. Read-only."""
    _ensure()
    counts = bot_db.query(ONEOFF_COUNTS_SQL,
                          (ONEOFF_MAX_ARTICLES, ONEOFF_MAX_ARTICLES,
                           ONEOFF_MAX_ARTICLES, list(JUNK_KINDS)))
    sample = bot_db.query(
        ONEOFF_SQL + " LIMIT 6", (list(JUNK_KINDS), ONEOFF_MAX_ARTICLES))
    return {"counts": counts, "sample": sample,
            "total": sum(c["oneoff"] for c in counts),
            "protected": sum(c["protected"] for c in counts),
            "repeated": sum(c["repeated"] for c in counts)}


ONEOFF_EXPORT_SQL = f"""
SELECT e.id, e.kind, coalesce(e.name_ua, e.name_ru) AS name,
       array_to_string(e.aliases, ' | ') AS aliases,
       a.id AS article_id,
       to_char(to_timestamp(a.published), 'YYYY-MM-DD') AS published,
       coalesce(a.title_ua, a.title_ru) AS title
FROM entities e
LEFT JOIN (SELECT entity_id, count(DISTINCT article_id) AS n,
                  min(article_id) AS one
           FROM article_entities GROUP BY entity_id) l ON l.entity_id = e.id
LEFT JOIN articles a ON a.id = l.one
WHERE e.kind = ANY(%s) AND coalesce(l.n, 0) <= %s AND NOT {PROTECTED_SQL}
ORDER BY e.kind, e.id
"""


def export_oneoff_csv():
    """CSV усіх одноразових карток — із ЗАГОЛОВКОМ статті, у якій вони живуть.

    Без заголовка список нечитабельний: «Гаазька конвенція 1954» і «замах на
    Дональда Трампа» відрізняються не кількістю згадок (в обох одна), а тим,
    чи це НАЗВА, яку можна повторити, чи переказ сюжету. Перше видно з самої
    назви, друге — лише поруч зі статтею."""
    import csv
    import io
    _ensure()
    rows = bot_db.query(ONEOFF_EXPORT_SQL, (list(JUNK_KINDS), ONEOFF_MAX_ARTICLES))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "kind", "назва", "аліаси", "стаття", "дата",
                "заголовок статті"])
    for r in rows:
        w.writerow([r["id"], r["kind"], r["name"], r["aliases"],
                    r["article_id"] or "", r["published"] or "", r["title"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8"), len(rows)


def oneoff_ids():
    _ensure()
    return [r["id"] for r in bot_db.query(
        ONEOFF_SQL, (list(JUNK_KINDS), ONEOFF_MAX_ARTICLES))]


# ---------- Посади, записані як організації ----------
#
# Перше слово назви. Список свідомо короткий і точний (збіг СЛОВОМ, не
# префіксом): «мер*» зачепило б «мерія», а це якраз установа. Довгий хвіст
# ловить другий сигнал — те, що написання вже трапляється роллю в норі.
POSITION_WORDS = {
    "президент", "президентка", "віцепрезидент", "експрезидент",
    "премєр", "премєрміністр", "премєр-міністр", "віцепремєр",
    "міністр", "міністерка", "ексміністр", "генпрокурор", "прокурор",
    "прокурорка", "мер", "мерка", "градоначальник", "губернатор",
    "голова", "головуючий", "очільник", "очільниця", "керівник", "керівниця",
    "начальник", "начальниця", "директор", "директорка", "гендиректор",
    "командувач", "головнокомандувач", "депутат", "депутатка", "секретар",
    "секретарка", "спікер", "посол", "послиця", "ректор", "проректор",
    "канцлер", "король", "королева", "принц", "папа", "лідер", "глава",
    "заступник", "заступниця", "радник", "радниця", "уповноважений",
    "уповноважена", "омбудсман", "омбудсмен", "суддя", "митрополит",
    "єпископ", "патріарх", "головлікар", "олігарх", "бізнесмен",
}

ORG_CARDS_SQL = """
SELECT e.id, coalesce(e.name_ua, e.name_ru) AS name, e.name_ua, e.name_ru,
       coalesce(e.mentions, 0) AS mentions
FROM entities e
WHERE e.kind = 'org' AND coalesce(e.name_ua, e.name_ru) IS NOT NULL
"""

ROLE_USAGE_SQL = """
SELECT role_norm(role_at_time) AS rn, count(*) AS n
FROM article_entities
WHERE role_at_time IS NOT NULL AND role_norm(role_at_time) IS NOT NULL
GROUP BY 1
"""

PROTECTED_IDS_SQL = f"SELECT e.id FROM entities e WHERE {PROTECTED_SQL}"


def scan_positions():
    """Картки org, які насправді є посадами. Два сигнали, обидва показуються
    в списку — рішення все одно за людиною, бо «Офіс Президента України» це
    установа, а «Президент України» ні, і відрізняє їх лише читач."""
    _ensure()
    usage = {r["rn"]: r["n"] for r in bot_db.query(ROLE_USAGE_SQL)}
    protected = {r["id"] for r in bot_db.query(PROTECTED_IDS_SQL)}
    out = []
    for r in bot_db.query(ORG_CARDS_SQL):
        if r["id"] in protected:
            continue
        signals = []
        as_role = 0
        for nm in (r["name_ua"], r["name_ru"]):
            as_role = max(as_role, usage.get(er.role_norm(nm) or "", 0))
        if as_role:
            signals.append(f"трапляється роллю ×{as_role}")
        rn = er.role_norm(r["name_ua"] or r["name_ru"]) or ""
        head = (rn.split() or [""])[0]
        if head in POSITION_WORDS:
            signals.append(f"перше слово — посада («{head}»)")
        if signals:
            out.append({"id": r["id"], "name": r["name"],
                        "mentions": r["mentions"], "as_role": as_role,
                        "signals": " · ".join(signals)})
    out.sort(key=lambda x: (-x["as_role"], -x["mentions"]))
    return out


# ---------- Канон посилань на закон (обчислена назва) ----------
#
# Найцінніший клас документів і водночас найбільша купа дублів: одна стаття
# кодексу живе десятком написань, кожне зі своєю карткою по одній згадці —
# тобто виглядає «сміттям», хоч насправді це покажчик «за якими статтями ми
# писали справи».
#
# Лікування і профілактика тут — ОДИН код. `ep.canon_document` рахує назву
# детерміновано, і write_results кличе його на кожному витягу: інкремент,
# батчі архіву, /entity_resync. Тому нових дублів не буде взагалі, а наявні
# зводяться цією ж функцією разово — назва обчислюється, старе написання йде
# в аліаси (пошук по ньому лишається живим), збіги зливаються звичайним
# злиттям із журналом, тож відкат є.

DOC_CARDS_SQL = """
SELECT e.id, e.name_ua, e.name_ru, coalesce(e.mentions, 0) AS mentions,
       array_to_string(e.aliases, ' | ') AS aliases
FROM entities e WHERE e.kind = 'document'
ORDER BY coalesce(e.mentions, 0) DESC, e.id
"""


def scan_doc_canon():
    """Read-only: скільки карток отримають обчислену назву і скільки груп
    злипнеться. Нічого не пише."""
    _ensure()
    plan, renames = {}, []
    for r in bot_db.query(DOC_CARDS_SQL):
        canon = (ep.canon_document(r["name_ua"])
                 or ep.canon_document(r["name_ru"]))
        if not canon:
            continue
        if ep.norm(canon) != ep.norm(r["name_ua"]):
            renames.append((r["id"], r["name_ua"], canon))
        plan.setdefault(ep.norm(canon), []).append(dict(r, canon=canon))
    groups = {k: v for k, v in plan.items() if len(v) > 1}
    return {"renames": renames, "groups": groups,
            "recognised": sum(len(v) for v in plan.values()),
            "canonical": len(plan),
            "in_groups": sum(len(v) for v in groups.values()),
            "links": sum(c["mentions"] for v in groups.values() for c in v)}


def apply_doc_canon(decided_by=None):
    """Проставити обчислені назви й злити збіги. Ідемпотентно: повторний
    прогін нічого не змінює, бо назви вже канонічні."""
    scan = scan_doc_canon()
    merged = renamed = 0
    for cards in scan["groups"].values():
        # Лишається найзгадуваніша картка — на неї перевішуються решта.
        winner = max(cards, key=lambda c: (c["mentions"], -c["id"]))
        _rename_doc(winner)
        renamed += 1
        for c in cards:
            if c["id"] == winner["id"]:
                continue
            em.merge_cards(winner["id"], c["id"], decided_by)
            merged += 1
    # Поодинокі картки теж перейменовуємо: наступна стаття з іншим написанням
    # тієї самої статті кодексу має знайти саме її, а не завести нову.
    done = {c["id"] for v in scan["groups"].values() for c in v}
    for eid, old, canon in scan["renames"]:
        if eid in done:
            continue
        _rename_doc({"id": eid, "name_ua": old, "canon": canon})
        renamed += 1
    return {"merged": merged, "renamed": renamed}


def _rename_doc(card):
    """Канонічна назва + старе написання в аліаси. Без аліаса перейменування
    було б втратою: пошук по «частині 5 статті 191» перестав би знаходити."""
    if ep.norm(card["canon"]) == ep.norm(card["name_ua"]):
        return
    bot_db.execute(
        "UPDATE entities SET name_ua = %s, aliases = ("
        "  SELECT coalesce(array_agg(DISTINCT x ORDER BY x), '{}') "
        "  FROM unnest(aliases || %s::text[]) AS x) "
        "WHERE id = %s",
        (card["canon"], [card["name_ua"]], card["id"]))


def format_doc_canon(scan):
    lines = ["🦊 Канон посилань на закон (нічого ще не змінено)\n",
             "Одна стаття кодексу живе десятком написань, і кожне завело "
             "власну картку по одній згадці. Назва рахується з тексту, тому "
             "далі така картка буде одна — і в інкременті, і в батчах архіву.\n"]
    lines.append(f"Упізнано написань: {scan['recognised']} → "
                 f"{scan['canonical']} статей і законопроєктів")
    lines.append(f"Груп дублів: {len(scan['groups'])} · карток у них: "
                 f"{scan['in_groups']} · згадок на кону: {scan['links']}")
    big = sorted(scan["groups"].items(), key=lambda kv: -len(kv[1]))[:5]
    for _k, cards in big:
        lines.append(f"\n{cards[0]['canon']} ← {len(cards)} карток:")
        for c in cards[:4]:
            lines.append(f"   · {c['name_ua']}")
    lines.append("\nСтаре написання кожної картки лишається аліасом — пошук "
                 "по ньому працюватиме. Кожне злиття лишає знімок у журналі "
                 "(/entity_merge_log, /entity_unmerge).")
    return "\n".join(lines)[:4000]


async def entity_docs_canon_handler(update, context):
    """/entity_docs_canon — звести посилання на закон до обчисленої назви."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Рахую посилання на закон…")
    try:
        scan = await asyncio.to_thread(scan_doc_canon)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахував: {type(e).__name__}: {e}")
        return
    if not scan["recognised"]:
        await msg.edit_text("🦊 Посилань на статті кодексів у норі не бачу.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"✅ Звести {len(scan['groups'])} груп", callback_data="ejd:go"),
        InlineKeyboardButton("❌ Ні", callback_data="ejd:no"),
    ]]) if scan["groups"] or scan["renames"] else None
    await msg.edit_text(format_doc_canon(scan), reply_markup=kb)


async def entity_docs_canon_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    await query.answer()
    if query.data == "ejd:no":
        await query.edit_message_text("❌ Скасовано, картки не чіпав.")
        return
    await query.edit_message_text("🦊 Зводжу посилання на закон…")
    who = query.from_user.full_name if query.from_user else None
    try:
        res = await asyncio.to_thread(apply_doc_canon, who)
    except Exception as e:
        await query.edit_message_text(f"❌ Не звелось: {type(e).__name__}: {e}")
        return
    await query.edit_message_text(
        f"🦊 Готово: злито {res['merged']} карток, канонічну назву отримали "
        f"{res['renamed']}.\n"
        f"Старі написання лишились аліасами, кожне злиття — у журналі "
        f"(/entity_merge_log, відкат /entity_unmerge <id>).\n"
        f"Далі витяг рахує назву сам — нових дублів по статтях не буде.")


# ---------- Правова форма організації (КП / ОКП / ТОВ / АТ) ----------
#
# Та сама хвороба, що зі статтями кодексу, і найбільша за розміром: витяг то
# пише правову форму, то ні, і одна установа розповзається на 4–6 карток.
# Замір по норі 02.08: 264 групи, 590 карток, 7412 згадок — і це головні
# герої місцевих новин («Миколаївобленерго» був розколотий на п'ять карток,
# «Миколаївоблтеплоенерго» на шість).
#
# На відміну від статей кодексу назву тут НЕ переписуємо: яка форма
# «правильна» — питання смаку, а не факту. Лишається найзгадуваніша картка,
# решта зливається в неї звичайним злиттям, тобто з аліасами й журналом.

ORG_GROUPS_SQL = """
SELECT e.id, e.name_ua, e.name_ru, coalesce(e.mentions, 0) AS mentions
FROM entities e WHERE e.kind = 'org'
  AND coalesce(e.name_ua, e.name_ru) IS NOT NULL
ORDER BY coalesce(e.mentions, 0) DESC, e.id
"""


def scan_org_forms():
    """Read-only: які картки організацій різняться ЛИШЕ правовою формою."""
    _ensure()
    plan = {}
    for r in bot_db.query(ORG_GROUPS_SQL):
        key = ep.org_key(r["name_ua"]) or ep.org_key(r["name_ru"])
        if key:
            plan.setdefault(key, []).append(r)
    groups = {}
    for key, cards in plan.items():
        if len(cards) < 2:
            continue
        # Однакові назви — це робота /entity_dedup, тут нас цікавить саме
        # різниця у формі. Інакше звіт показував би те, що вже полагоджено.
        if len({ep.norm(c["name_ua"] or c["name_ru"]) for c in cards}) < 2:
            continue
        groups[key] = cards
    return {"groups": groups,
            "cards": sum(len(v) for v in groups.values()),
            "links": sum(c["mentions"] for v in groups.values() for c in v)}


def apply_org_forms(decided_by=None):
    """Злити картки, що різняться лише правовою формою. Ідемпотентно."""
    scan = scan_org_forms()
    merged = 0
    for cards in scan["groups"].values():
        winner = max(cards, key=lambda c: (c["mentions"], -c["id"]))
        for c in cards:
            if c["id"] == winner["id"]:
                continue
            em.merge_cards(winner["id"], c["id"], decided_by)
            merged += 1
    return {"merged": merged, "groups": len(scan["groups"])}


def format_org_forms(scan):
    lines = ["🦊 Правова форма — не інша установа\n",
             "«Миколаївобленерго», «АТ «Миколаївобленерго»», «КП "
             "«Миколаївобленерго»» — одна компанія під трьома картками, бо "
             "витяг то пише форму, то ні.\n",
             f"Груп: {len(scan['groups'])} · карток: {scan['cards']} · "
             f"згадок на кону: {scan['links']}"]
    big = sorted(scan["groups"].values(), key=lambda v: -sum(
        c["mentions"] for c in v))[:6]
    for cards in big:
        winner = max(cards, key=lambda c: (c["mentions"], -c["id"]))
        lines.append(f"\n✅ {winner['name_ua']} ({winner['mentions']})")
        for c in sorted(cards, key=lambda c: -c["mentions"]):
            if c["id"] != winner["id"]:
                lines.append(f"   🗑 {c['name_ua']} ({c['mentions']})")
    lines.append("\nЛишається найзгадуваніша картка, решта йде в її аліаси. "
                 "Кожне злиття — у журналі (/entity_merge_log, "
                 "відкат /entity_unmerge <id>).")
    return "\n".join(lines)[:4000]


async def entity_org_forms_handler(update, context):
    """/entity_org_forms — звести картки, що різняться лише правовою формою."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Шукаю однакові установи під різними формами…")
    try:
        scan = await asyncio.to_thread(scan_org_forms)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахував: {type(e).__name__}: {e}")
        return
    if not scan["groups"]:
        await msg.edit_text("🦊 Установ, що різняться лише правовою формою, не бачу.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Звести {len(scan['groups'])} груп",
                             callback_data="ejf:go"),
        InlineKeyboardButton("❌ Ні", callback_data="ejf:no"),
    ]])
    await msg.edit_text(format_org_forms(scan), reply_markup=kb)


async def entity_org_forms_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    await query.answer()
    if query.data == "ejf:no":
        await query.edit_message_text("❌ Скасовано, картки не чіпав.")
        return
    await query.edit_message_text("🦊 Зводжу установи…")
    who = query.from_user.full_name if query.from_user else None
    try:
        res = await asyncio.to_thread(apply_org_forms, who)
    except Exception as e:
        await query.edit_message_text(f"❌ Не звелось: {type(e).__name__}: {e}")
        return
    await query.edit_message_text(
        f"🦊 Зведено {res['groups']} груп, злито {res['merged']} карток.\n"
        f"Старі написання лишились аліасами, кожне злиття — у журналі "
        f"(/entity_merge_log, відкат /entity_unmerge <id>).\n"
        f"Далі витяг зіставляє за ключем без форми — «КП «Миколаївводоканал»» "
        f"більше не заведе окрему картку.")


# ---------- Саме прибирання ----------

def purge_cards(ids, reason, decided_by=None, run=None):
    """Прибрати картки зі знімком кожної в журналі. Одна транзакція на весь
    прогін: або журнал і видалення разом, або нічого.

    `run` — мітка прогону, спільна для всіх карток; за нею відкочується вся
    купа одразу (/entity_junk_undo). Без неї відкат тритисячного прогону був
    би трьома тисячами команд."""
    if not ids:
        return {"run": run, "removed": 0, "merge_ids": []}
    _ensure()
    run = run or f"{reason}-{int(time.time())}"
    merge_ids = []
    conn = ep.connect()
    cur = conn.cursor()
    try:
        for eid in ids:
            merge_id = em.record_purge(cur, eid, reason, run, decided_by)
            if merge_id is None:
                continue        # картки вже немає — прогін ідемпотентний
            cur.execute("DELETE FROM article_entities WHERE entity_id = %s", (eid,))
            cur.execute("DELETE FROM entities WHERE id = %s", (eid,))
            merge_ids.append(merge_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return {"run": run, "removed": len(merge_ids), "merge_ids": merge_ids}


def undo_run(run):
    """Відкотити весь прогін прибирання. У зворотному порядку id — тим самим
    правилом, що й кластер злиттів."""
    em.ensure_schema()
    rows = bot_db.query(
        "SELECT id FROM entity_merges "
        "WHERE loser_snapshot ->> 'run' = %s AND undone IS NULL "
        "ORDER BY id DESC", (run,))
    done = 0
    for r in rows:
        if em.restore_merge(r["id"]):
            done += 1
    return done


# ---------- /entity_junk ----------

def _oneoff_text(scan):
    lines = ["🦊 Сміття в картках · одноразові події й документи\n",
             "Витяг робив із них переказ сюжету статті замість назви, яку "
             "можна повторити. Така картка живе в одній статті й не зв'язує "
             "нічого ні з чим.\n"]
    for c in scan["counts"]:
        lines.append(f"{c['kind']}: усього {c['total']} · "
                     f"одноразових {c['oneoff']} · "
                     f"повторюваних {c['repeated']} (лишаються) · "
                     f"захищених {c['protected']}")
    lines.append(f"\nПід прибирання підпадає: {scan['total']}")
    lines.append(f"Лишаються працювати (повтор між статтями): {scan['repeated']}")
    if scan["protected"]:
        lines.append(f"Не чіпаю (людина або довідник уже торкались): "
                     f"{scan['protected']} — це /entity_merge_log і /roles_org")
    if scan["sample"]:
        lines.append("\nПриклади:")
        for r in scan["sample"]:
            lines.append(f"  [{r['id']}] {r['kind']} · {r['name']} "
                         f"({r['articles']} стаття)")
    lines.append("\nКожне видалення лишає знімок у журналі — відкат є завжди.")
    return "\n".join(lines)[:4000]


def _junk_markup(scan, positions):
    rows = []
    if scan["total"]:
        rows.append([InlineKeyboardButton(
            f"🗑 Прибрати одноразові ({scan['total']})",
            callback_data="ejb:ask")])
    if positions:
        rows.append([InlineKeyboardButton(
            f"👤 Посади в org ({len(positions)})", callback_data="ejp:list")])
    return InlineKeyboardMarkup(rows) if rows else None


async def entity_junk_handler(update, context):
    """/entity_junk — показати масштаб сміття числом і дати кнопки.
    Нічого не видаляє без окремого підтвердження."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Рахую сміття в картках…")
    try:
        scan = await asyncio.to_thread(scan_oneoff)
        positions = await asyncio.to_thread(scan_positions)
    except Exception as e:
        await msg.edit_text(f"❌ Не порахував: {type(e).__name__}: {e}")
        return
    await asyncio.to_thread(_save_positions, msg.chat_id, msg.message_id, positions)
    tail = ("" if not positions else
            f"\n\nПосад, записаних організаціями: {len(positions)} — "
            f"окремою кнопкою, там прибирання поштучне.")
    await msg.edit_text(_oneoff_text(scan) + tail,
                        reply_markup=_junk_markup(scan, positions))

    # І список файлом. Шість прикладів у чаті нічого не вирішують: серед
    # одноразових карток лежить і справжнє сміття («замах на Дональда
    # Трампа»), і нормальні назви, які просто ще не повторились («Гаазька
    # конвенція 1954», «Маккабіада-2026»). Відрізнити їх можна лише
    # переглядом усього списку — і не в чаті.
    import io
    if positions:
        # Посади окремим файлом із тієї ж причини: рішення про картку
        # ухвалюють, подивившись на неї, а не на число в кнопці.
        import csv
        pbuf = io.StringIO()
        pw = csv.writer(pbuf)
        pw.writerow(["id", "назва", "згадки", "чому запідозрено"])
        for p in positions:
            pw.writerow([p["id"], p["name"], p["mentions"], p["signals"]])
        await update.message.reply_document(
            document=io.BytesIO(("﻿" + pbuf.getvalue()).encode("utf-8")),
            filename=f"entity_positions_{len(positions)}.csv",
            caption=(f"🦊 Ті самі {len(positions)} карток org, які схожі на "
                     f"посади — з підставою по кожній. Кнопка в повідомленні "
                     f"вище лише РОЗГОРТАЄ цей список, видаляє тап по "
                     f"конкретній картці."))
    if not scan["total"]:
        return
    try:
        data, n = await asyncio.to_thread(export_oneoff_csv)
    except Exception as e:
        await update.message.reply_text(f"(списком не віддалось: {e})")
        return
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"entity_junk_{n}.csv",
        caption=(f"🦊 Усі {n} карток під прибирання — з заголовком статті, у "
                 f"якій кожна живе.\n\nЦе для розбору поза ботом: за назвою "
                 f"видно, чи це переказ сюжету («Трамп: Другий шанс?»), чи "
                 f"нормальна назва, яка просто ще не повторилась («Закон про "
                 f"створення Українського національного пантеону»). Кнопка "
                 f"прибирає ВСІ — тому спершу список."))


# Список посад-в-org живе в норі поруч із повідомленням: перерахунок на кожен
# тап означав би повний скан article_entities після кожної прибраної картки.
POS_STATE_PREFIX = "ejunk:"


def _pos_key(chat_id, message_id):
    return f"{POS_STATE_PREFIX}{chat_id}:{message_id}"


def _save_positions(chat_id, message_id, positions):
    bot_db.set_state(_pos_key(chat_id, message_id),
                     json.dumps(positions, ensure_ascii=False))


def _load_positions(chat_id, message_id):
    raw = bot_db.get_state(_pos_key(chat_id, message_id))
    return json.loads(raw) if raw else None


POS_PAGE = 8


def _positions_text(positions):
    if not positions:
        return ("🦊 Посад, записаних організаціями, не лишилось.\n"
                "Дублі самих органів — /entity_org_dupes.")
    lines = ["🦊 Посади, записані організаціями — "
             f"{er.plural(len(positions), 'картка', 'картки', 'карток')}\n",
             "Посада — це не установа: вона вже лежить у role_at_time людини. "
             "«Президент України» прибираємо, «Офіс Президента України» — ні.\n"]
    for p in positions[:POS_PAGE]:
        lines.append(f"[{p['id']}] {p['name']} · "
                     f"{er.plural(p['mentions'], 'згадка', 'згадки', 'згадок')}"
                     f"\n      {p['signals']}")
    if len(positions) > POS_PAGE:
        lines.append(f"\n…і ще {len(positions) - POS_PAGE} — покажу, коли "
                     f"розберешся з цими.")
    lines.append("\nТап по картці прибирає ЇЇ ОДНУ (зі знімком у журналі).")
    return "\n".join(lines)[:4000]


def _positions_markup(positions):
    rows = [[InlineKeyboardButton(f"🗑 {p['name']} ({p['mentions']})",
                                  callback_data=f"ejp:{p['id']}")]
            for p in positions[:POS_PAGE]]
    return InlineKeyboardMarkup(rows) if rows else None


async def entity_junk_callback(update, context):
    """ejb:* — купа одноразових (масштаб → підтвердження → прибирання);
    ejp:* — посади в org, поштучно."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        action, arg = query.data.split(":", 1)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()
    who = query.from_user.full_name if query.from_user else None
    chat_id, message_id = query.message.chat_id, query.message.message_id

    if action == "ejb" and arg == "ask":
        try:
            scan = await asyncio.to_thread(scan_oneoff)
        except Exception as e:
            await query.edit_message_text(f"❌ Не порахував: {e}")
            return
        if not scan["total"]:
            await query.edit_message_text("🦊 Одноразових карток не лишилось.")
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Так, прибрати {scan['total']}",
                                 callback_data="ejb:go"),
            InlineKeyboardButton("❌ Ні", callback_data="ejb:no"),
        ]])
        await query.edit_message_text(
            f"🦊 Прибрати {scan['total']} одноразових карток "
            f"(event/document, кожна живе в одній статті)?\n\n"
            f"Повторювані ({scan['repeated']}) лишаються на місці.\n"
            f"Кожна прибрана лишить знімок — відкат усього прогону однією "
            f"командою, окремої картки — /entity_unmerge <id>.",
            reply_markup=kb)
        return

    if action == "ejb" and arg == "no":
        await query.edit_message_text("❌ Скасовано, нічого не чіпав.")
        return

    if action == "ejb" and arg == "go":
        await query.edit_message_text("🦊 Прибираю (знімок кожної картки в журнал)…")
        try:
            ids = await asyncio.to_thread(oneoff_ids)
            res = await asyncio.to_thread(purge_cards, ids, "oneoff", who)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Не прибралось (нічого не змінено): {type(e).__name__}: {e}")
            return
        await query.edit_message_text(
            f"🦊 Прибрано карток: {res['removed']}.\n"
            f"Прогін: `{res['run']}`\n"
            f"Відкотити весь: /entity_junk_undo {res['run']}\n"
            f"Одну картку: /entity_unmerge <id> (журнал — /entity_merge_log)")
        return

    if action != "ejp":
        return

    positions = await asyncio.to_thread(_load_positions, chat_id, message_id)
    if positions is None:
        try:
            positions = await asyncio.to_thread(scan_positions)
        except Exception as e:
            await query.edit_message_text(f"❌ Не порахував: {e}")
            return
    if arg == "list":
        await asyncio.to_thread(_save_positions, chat_id, message_id, positions)
        await query.edit_message_text(_positions_text(positions),
                                      reply_markup=_positions_markup(positions))
        return

    if not arg.isdigit():
        return
    eid = int(arg)
    hit = next((p for p in positions if p["id"] == eid), None)
    try:
        res = await asyncio.to_thread(purge_cards, [eid], "position_as_org", who)
    except Exception as e:
        await query.edit_message_text(f"❌ Не прибралось: {type(e).__name__}: {e}")
        return
    rest = [p for p in positions if p["id"] != eid]
    await asyncio.to_thread(_save_positions, chat_id, message_id, rest)
    undo = (f"Відкат: /entity_unmerge {res['merge_ids'][0]}"
            if res["merge_ids"] else "Картки вже не було.")
    name = hit["name"] if hit else eid
    await query.edit_message_text(
        f"🗑 Прибрано «{name}». {undo}\n\n" + _positions_text(rest),
        reply_markup=_positions_markup(rest))


async def entity_junk_undo_handler(update, context):
    """/entity_junk_undo <прогін> — повернути всі картки одного прибирання."""
    if not _allowed(update):
        return
    args = context.args or []
    if not args:
        def recent():
            em.ensure_schema()
            return bot_db.query(
                "SELECT loser_snapshot ->> 'run' AS run, count(*) AS n, "
                "       count(*) FILTER (WHERE undone IS NULL) AS alive, "
                "       to_char(to_timestamp(max(created)), 'DD.MM HH24:MI') AS at "
                "FROM entity_merges WHERE loser_snapshot ->> 'op' = 'purge' "
                "GROUP BY 1 ORDER BY max(created) DESC LIMIT 10")
        try:
            rows = await asyncio.to_thread(recent)
        except Exception as e:
            await update.message.reply_text(f"❌ Нора недоступна: {e}")
            return
        if not rows:
            await update.message.reply_text(
                "Прибирань ще не було — /entity_junk покаже масштаб.")
            return
        lines = ["🦊 Прогони прибирання\n"]
        for r in rows:
            lines.append(f"`{r['run']}` · {r['at']} · карток {r['n']}, "
                         f"не відкочено {r['alive']}")
        lines.append("\nВідкотити: /entity_junk_undo <прогін>")
        await update.message.reply_text("\n".join(lines))
        return
    run = args[0]
    msg = await update.message.reply_text(f"🦊 Відкочую прогін {run}…")
    try:
        n = await asyncio.to_thread(undo_run, run)
    except Exception as e:
        await msg.edit_text(f"❌ Не відкотилось: {type(e).__name__}: {e}")
        return
    await msg.edit_text(
        f"🦊 Повернув карток: {n} (разом із їхніми зв'язками й ролями)."
        if n else f"У прогоні {run} нема чого відкочувати.")


# ---------- /entity_org_dupes ----------
#
# «Офіс президента» і «Офіс Президента України» — та сама установа під двома
# написаннями, і саме такі пари /entity_find не показує, поки не знаєш, що
# шукати. Тут вони знаходяться самі, але зливає їх усе одно людина: тап
# відкриває ЗВИЧАЙНЕ прев'ю /entity_merge (спільні статті й сусіди) з кнопкою.
#
# Поріг схожості нижчий за картковий (0.8): «офіс президента» проти «офіс
# президента україни» дає ~0.68 — довша назва додає трграми, і на 0.8 пара,
# заради якої команда й писалась, не знаходиться взагалі.
ORG_SIM_THRESHOLD = 0.55

ORG_PAIRS_SQL = """
SELECT a.id AS aid, a.name_ua AS an, coalesce(a.mentions, 0) AS am,
       b.id AS bid, b.name_ua AS bn, coalesce(b.mentions, 0) AS bm
FROM entities a JOIN entities b ON b.kind = a.kind AND a.id < b.id
WHERE a.kind = 'org' AND a.name_ua IS NOT NULL AND b.name_ua IS NOT NULL
  AND lower(a.name_ua) %% lower(b.name_ua)
ORDER BY coalesce(a.mentions, 0) + coalesce(b.mentions, 0) DESC
LIMIT %s
"""

# Скільки схожих пар беремо на розбір. Стеля стоїть у SQL, бо на живій норі
# організацій тисячі, а показуємо ми все одно десяток найваговитіших.
ORG_PAIRS_SCAN = 300

# Класи, де різниця написань — це та сама установа. Решту (інше місце, інший
# рівень, номери, розрізнювачі) не показуємо взагалі: «управління культури
# МИКОЛАЇВСЬКОЇ» і «управління культури ХЕРСОНСЬКОЇ» — різні органи, і місце
# їм у списку кандидатів на злиття лише спокушає.
ORG_DUPE_CLASSES = {"permutation", "abbrev", "typo", "function_words",
                    "containment_fill", "containment_own"}

ORG_DUPES_LIMIT = 10


def find_org_dupes(threshold=ORG_SIM_THRESHOLD, limit=ORG_DUPES_LIMIT):
    """Кандидати-дублі організацій. Read-only: нічого не зливає й не пише.

    Картки, які насправді є посадами, зі списку прибираються: «Президент
    України» схожий на «Офіс Президента України» рівно так само, як «Офіс
    президента», — і одне необережне злиття загорнуло б посаду всередину
    установи назавжди. Їм місце в іншій купі (/entity_junk), а не тут."""
    _ensure()
    positions = {p["id"] for p in scan_positions()}
    with bot_db.session():
        bot_db.execute(f"SET pg_trgm.similarity_threshold = {threshold}")
        rows = bot_db.query(ORG_PAIRS_SQL, (ORG_PAIRS_SCAN,))
    out, skipped = [], 0
    for r in rows:
        a, b = er.role_norm(r["an"]), er.role_norm(r["bn"])
        if not a or not b or a == b:
            continue
        if r["aid"] in positions or r["bid"] in positions:
            skipped += 1
            continue
        cls, detail = er.classify_pair(a, b)
        if cls not in ORG_DUPE_CLASSES:
            continue
        # Хто лишається: при вкладеності — ПОВНІША офіційна назва («Офіс
        # Президента України», не «Офіс президента»), інакше та картка, у якої
        # більше згадок. Помилку напрямку видно в прев'ю, і виправляється вона
        # тим самим /entity_merge з переставленими id.
        if cls.startswith("containment"):
            keep_a = len(er._tokens(a)) > len(er._tokens(b))
        else:
            keep_a = r["am"] >= r["bm"]
        w = (r["aid"], r["an"], r["am"]) if keep_a else (r["bid"], r["bn"], r["bm"])
        l = (r["bid"], r["bn"], r["bm"]) if keep_a else (r["aid"], r["an"], r["am"])
        out.append({"winner": w, "loser": l, "cls": cls, "detail": detail})
        if len(out) >= limit:
            break
    return out, skipped


async def entity_org_dupes_handler(update, context):
    """/entity_org_dupes — кандидати-дублі організацій із тапом у прев'ю."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Шукаю дублі органів…")
    try:
        pairs, skipped = await asyncio.to_thread(find_org_dupes)
    except Exception as e:
        await msg.edit_text(f"❌ Не знайшов: {type(e).__name__}: {e}")
        return
    if not pairs:
        await msg.edit_text(
            "🦊 Кандидатів-дублів серед організацій не бачу.\n"
            "Точкові пари шукає /entity_find <назва>.")
        return
    lines = [f"🦊 Дублі органів — {len(pairs)} пар\n",
             "Тап відкриє прев'ю злиття: спільні статті й спільні сусіди. "
             "Зливає рішення людини, не список.\n"]
    kb = []
    for p in pairs:
        w, l = p["winner"], p["loser"]
        det = f" ({p['detail']})" if p["detail"] else ""
        lines.append(f"[{l[0]}] {l[1]} ({l[2]}) → [{w[0]}] {w[1]} ({w[2]})"
                     f"\n      {er.CLASS_LABELS.get(p['cls'], p['cls'])}{det}")
        kb.append([InlineKeyboardButton(f"{l[1]} → {w[1]}"[:60],
                                        callback_data=f"ejo:{w[0]}:{l[0]}")])
    if skipped:
        lines.append(f"\nЩе {skipped} пар не показую: там картка-посада "
                     f"(«Президент України»), а це не злиття — /entity_junk.")
    await msg.edit_text("\n".join(lines)[:4000],
                        reply_markup=InlineKeyboardMarkup(kb))


async def entity_org_dupes_callback(update, context):
    """ejo:<переможець>:<дубль> — показати прев'ю злиття з кнопкою.
    Саме злиття робить emg: (handlers/entity_merge.py) — тут лише підготовка,
    щоб не було двох різних шляхів до одного видалення картки."""
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
    try:
        p, err = await asyncio.to_thread(em.merge_preview, winner_id, loser_id)
    except Exception as e:
        await query.edit_message_text(f"❌ {type(e).__name__}: {e}")
        return
    if err:
        await query.answer(f"Не зливаю: {err}", show_alert=True)
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Злити", callback_data=f"emg:{winner_id}:{loser_id}"),
        InlineKeyboardButton("❌ Скасувати", callback_data="emg:0:0"),
    ]])
    await query.edit_message_text(em.format_preview(p), reply_markup=kb)
