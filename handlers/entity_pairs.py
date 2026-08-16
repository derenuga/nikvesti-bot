"""
Пам'ять «ні, це різні картки» — §5.1 docs/ENTITY_DEDUP_PROTOCOL.md.

Для РОЛЕЙ така пам'ять є з 02.08 (`role_pairs`) і працює саме так: сказане
людиною «різні» не питається вдруге. Для карток її не було, і 16.08 це
вилізло на розборі дублів: детектор чесно приносив «Олександр Харченко ×
Олександр Марченко», «Володимир Кличко × Володимир Клочко», «Альона Кара ×
Альона Карая» — і після рішення людини не мав куди його записати. Список
довелось вести коментарем у .txt-файлі, тобто наступний прогін приніс би
ті самі пари знову.

**Один рядок на пару, читається з обох боків.** Формулювання Олега було
«у сутності A має бути пояснення, що це не B, і навпаки», але два дзеркальні
записи про один факт колись розійдуться — тому id зберігаються в канонічному
порядку (менший перший), а перевірка не залежить від того, з якого боку
прийшло питання.

**Причина словами обов'язкова.** Через рік «просто різні» не пояснить нічого,
а рішення доведеться ухвалювати наново — і саме тоді, коли контекст уже
забуто. «Різні прізвища», «перевірено по сайту міськради», «районна, а не
міська» — цього досить.

Де пам'ять діє: у детекторі дублів організацій (`/entity_org_dupes`, включно
з парами за абревіатурою) і в прев'ю пакета `# cards-fix`, де позначена пара
підписується попередженням. Злиття вона НЕ забороняє: людина має право
передумати, побачивши нові докази, — забороняти рішення людини машиною тут
не можна.
"""

import asyncio
import os
import time

from handlers import bot_db

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

PAIRS_DDL = """
CREATE TABLE IF NOT EXISTS entity_pairs (
    a_id       BIGINT NOT NULL,
    b_id       BIGINT NOT NULL,
    reason     TEXT NOT NULL,
    decided_by TEXT,
    created    BIGINT,
    PRIMARY KEY (a_id, b_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_pairs_b ON entity_pairs (b_id);
"""

_schema_done = {"flag": False}


def ensure_schema(force=False):
    if _schema_done["flag"] and not force:
        return
    bot_db.execute(PAIRS_DDL)
    _schema_done["flag"] = True


def key(a_id, b_id):
    """Канонічний порядок пари — менший id першим."""
    a, b = int(a_id), int(b_id)
    return (a, b) if a <= b else (b, a)


def mark_different(a_id, b_id, reason, decided_by=None):
    ensure_schema()
    a, b = key(a_id, b_id)
    if a == b:
        raise ValueError("це одна й та сама картка")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("потрібна причина словами — через рік «просто різні» "
                         "нічого не пояснить")
    bot_db.execute(
        "INSERT INTO entity_pairs (a_id, b_id, reason, decided_by, created) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (a_id, b_id) DO UPDATE SET "
        "reason = EXCLUDED.reason, decided_by = EXCLUDED.decided_by",
        (a, b, reason, decided_by, int(time.time())))
    return (a, b)


def forget(a_id, b_id):
    ensure_schema()
    a, b = key(a_id, b_id)
    return bot_db.execute(
        "DELETE FROM entity_pairs WHERE a_id = %s AND b_id = %s", (a, b))


def load_different():
    """Усі відомі пари «різні» як множина кортежів — для фільтра детекторів.

    Читається одним запитом на прогін: пар тут сотні, а не мільйони, і
    перевіряти кожну окремим SELECT'ом означало б сотні запитів на список."""
    ensure_schema()
    return {(r["a_id"], r["b_id"])
            for r in bot_db.query("SELECT a_id, b_id FROM entity_pairs")}


def is_different(a_id, b_id, known=None):
    pair = key(a_id, b_id)
    if known is not None:
        return pair in known
    ensure_schema()
    return bool(bot_db.query(
        "SELECT 1 FROM entity_pairs WHERE a_id = %s AND b_id = %s", pair))


LIST_SQL = """
SELECT p.a_id, p.b_id, p.reason, p.decided_by,
       to_char(to_timestamp(p.created), 'DD.MM') AS at,
       coalesce(a.name_ua, a.name_ru) AS a_name,
       coalesce(b.name_ua, b.name_ru) AS b_name
FROM entity_pairs p
LEFT JOIN entities a ON a.id = p.a_id
LEFT JOIN entities b ON b.id = p.b_id
ORDER BY p.created DESC NULLS LAST
LIMIT %s
"""


def list_pairs(limit=30):
    ensure_schema()
    return bot_db.query(LIST_SQL, (limit,))


# ---------- команди ----------


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


async def entity_notdupe_handler(update, context):
    """/entity_notdupe <id> <id> <причина> — запам'ятати «це різні».

    Без аргументів показує вже розведені пари, `undo <id> <id>` повертає пару
    в детектор."""
    if not _allowed(update):
        return
    args = context.args or []

    if not args:
        try:
            rows = await asyncio.to_thread(list_pairs, 30)
        except Exception as e:
            await update.message.reply_text(f"❌ Нора недоступна: {e}")
            return
        if not rows:
            await update.message.reply_text(
                "🦊 Розведених пар ще немає.\n\n"
                "Формат: /entity_notdupe <id> <id> <причина>\n"
                "Причина обов'язкова — без неї рішення нічого не пояснить "
                "через рік.")
            return
        lines = [f"🦊 «Це різні картки» — {len(rows)} пар\n"]
        for r in rows:
            lines.append(f"• «{r['a_name']}» ({r['a_id']}) × «{r['b_name']}» "
                         f"({r['b_id']})\n  {r['reason']} — {r['at']}"
                         + (f", {r['decided_by']}" if r["decided_by"] else ""))
        lines.append("\nПовернути пару в детектор: /entity_notdupe undo <id> <id>")
        await update.message.reply_text("\n".join(lines)[:4000])
        return

    if args[0].lower() == "undo" and len(args) >= 3:
        try:
            n = await asyncio.to_thread(forget, int(args[1]), int(args[2]))
        except Exception as e:
            await update.message.reply_text(f"❌ {e}")
            return
        await update.message.reply_text(
            "✅ Пара знову в детекторі." if n else "Такої пари не було.")
        return

    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text(
            "Формат: /entity_notdupe <id> <id> <причина>\n"
            "Наприклад: /entity_notdupe 12716 15214 різні прізвища, "
            "Кличко і Клочко")
        return
    who = update.effective_user.full_name if update.effective_user else None
    try:
        a, b = await asyncio.to_thread(mark_different, int(args[0]), int(args[1]),
                                       " ".join(args[2:]), who)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    await update.message.reply_text(
        f"✅ Запам'ятав: {a} і {b} — різні. Детектор більше не запропонує цю "
        f"пару.\n\nПередумав — /entity_notdupe undo {a} {b}")
