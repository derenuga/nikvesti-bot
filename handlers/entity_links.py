"""
ЗВ'ЯЗКИ МІЖ КАРТКАМИ сутнісного шару — четверта вісь роботи з норою.

Три сусідні модулі відповідають на питання «це та сама картка?»
(`entity_merge`), «це та сама посада?» (`entity_roles`) і «цій картці взагалі
місце в базі?» (`entity_junk`). Тут питання інше й досі не поставлене:
**як картки пов'язані між собою.**

Приводом стало питання Олега 16.08.2026: «а у нас в цілому є зв'язки між
прокуратурами?». Не було. Картки лежали плоским списком, і нора не могла
відповісти навіть на «Миколаївська окружна — це частина обласної?».

**Чому співпояви замало.** Формально зв'язок ніби є: дві картки трапляються
в одних статтях (`article_entities`), і з цього навіть будується граф
(/entity_export_links). Але співпояв показує СЮЖЕТ, а не структуру — той
самий урок, що з афіліацією ролей: за співпоявою детектор пропонував
міністрові юстиції НАБУ і «Квартал 95», бо вони просто трапляються поруч.
Структуру не можна вивести, її можна тільки ЗНАТИ.

**Звідки береться знання.** З тієї ж роботи, що й злиття. Розбираючи дублі
ДСНС, поліції й прокуратури, людина щоразу встановлює структуру органу — і
досі це знання вмирало в коментарях .txt-пакета. Тому зв'язок ставиться ТИМ
САМИМ пакетом `# cards-fix`, рядком `link`, поки орган і так під руками.

**Три типи, і межа між ними проведена свідомо:**

  • `підрозділ` — B є ЧАСТИНОЮ A. Окружна прокуратура в обласній, районне
    управління поліції в ГУНП, природоохоронна міжрайонна в обласній.
  • `підпорядкований` — B слухається A, але часткою A не є. Саме такий
    випадок УПП: управління патрульної поліції підпорядковане Департаменту
    патрульної поліції, а не входить у ГУНП, і плутати ці два типи означало б
    втратити рівно ту різницю, заради якої ми їх і не злили.
  • `правонаступник` — A БУЛА і стала B. ДФС до 2019 року поділилась на ДПС
    і Держмитслужбу: злити їх не можна (це різні органи), а зв'язок реальний
    і потрібен досьє.

**Напрямок пишеться першим id, як і в `merge`.** `link 3271 2866` читається
«3271 має підрозділ 2866». Для правонаступництва перший — ПОПЕРЕДНИК:
`link 28842 2872 правонаступник` = «ДФС → правонаступник ДПС». Щоб на цьому
не помилятись мовчки, прев'ю пише зв'язок РЕЧЕННЯМ повними назвами, а не
стрілкою між номерами: переплутаний напрямок видно очима до натискання
кнопки.

**Один рядок на пару.** Дзеркального рядка не пишемо ніколи — дві записи про
один факт колись розійдуться (та сама інженерна поправка, що в §5.1
протоколу дублів). Читається пара з обох боків запитом.

**Зв'язок переживає злиття.** Картка, яку злили, зникає, тож посилання на неї
перевішується на переможця — так само, як зв'язки зі статтями, правила й
банк тем (`entity_merge.record_merge`). Без цього повторилась би історія §4
протоколу: посилання в нікуди, мовчки. Якщо після перевішування зв'язок
став би сам на себе (злили дві картки, між якими стояв зв'язок), рядок
прибирається — але лягає у знімок, тож відкат його поверне.

Картку, вписану в структуру, не можна прибрати як сміття: вона додана в
захищені в `entity_junk.PROTECTED_SQL`.
"""

import asyncio
import os
import time

from handlers import bot_db

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

LINKS_DDL = """
CREATE TABLE IF NOT EXISTS entity_links (
    id          BIGSERIAL PRIMARY KEY,
    parent_id   BIGINT NOT NULL,
    child_id    BIGINT NOT NULL,
    kind        TEXT NOT NULL,
    note        TEXT,
    decided_by  TEXT,
    created     BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_links_pair
    ON entity_links (parent_id, child_id);
CREATE INDEX IF NOT EXISTS idx_entity_links_child ON entity_links (child_id);
"""

# Тип → як він читається з боку старшої картки і з боку молодшої. Формулювання
# живуть тут одним місцем, бо їх показують і прев'ю пакета, і /entity_links:
# розійдуться — людина побачить у прев'ю одне, а в картці інше.
KINDS = {
    "підрозділ": ("має підрозділ", "входить до"),
    "підпорядкований": ("керує", "підпорядкований"),
    "правонаступник": ("правонаступник", "попередник"),
}
DEFAULT_KIND = "підрозділ"

# Скорочення, які людина напише швидше за повний термін. Пакет розбирає і їх,
# інакше рядок мовчки поїде в помилки через одну літеру.
KIND_ALIASES = {
    "підрозділ": "підрозділ", "pidrozdil": "підрозділ", "part": "підрозділ",
    "структура": "підрозділ", "входить": "підрозділ",
    "підпорядкований": "підпорядкований", "підпорядкування": "підпорядкований",
    "sub": "підпорядкований", "керує": "підпорядкований",
    "правонаступник": "правонаступник", "наступник": "правонаступник",
    "successor": "правонаступник", "попередник": "правонаступник",
}

_schema_done = {"flag": False}


def ensure_schema(force=False):
    if _schema_done["flag"] and not force:
        return
    bot_db.execute(LINKS_DDL)
    _schema_done["flag"] = True


def normalize_kind(word):
    """Слово з пакета → канонічний тип. None, якщо такого типу немає."""
    if not word:
        return DEFAULT_KIND
    return KIND_ALIASES.get(word.strip().lower())


# ---------- запис ----------


def add_link(parent_id, child_id, kind=DEFAULT_KIND, note=None, decided_by=None):
    """Поставити зв'язок. Повертає dict із тим, що сталось, або кидає ValueError
    із причиною ЛЮДСЬКОЮ мовою — вона піде просто в чат."""
    ensure_schema()
    if parent_id == child_id:
        raise ValueError("картка не може бути пов'язана сама з собою")
    if kind not in KINDS:
        raise ValueError(f"невідомий тип зв'язку «{kind}»; є: "
                         + ", ".join(KINDS))
    rows = bot_db.query(
        "SELECT id, kind, coalesce(name_ua, name_ru) AS name FROM entities "
        "WHERE id = ANY(%s)", ([parent_id, child_id],))
    found = {r["id"]: r for r in rows}
    missing = [i for i in (parent_id, child_id) if i not in found]
    if missing:
        raise ValueError("немає картки з id "
                         + ", ".join(str(i) for i in missing))
    # Дзеркальний рядок — це майже завжди переплутаний напрямок, а не другий
    # факт. Мовчки прийняти його означало б завести структуру, де прокуратура
    # водночас старша й молодша за саму себе.
    back = bot_db.query(
        "SELECT kind FROM entity_links WHERE parent_id = %s AND child_id = %s",
        (child_id, parent_id))
    if back:
        raise ValueError(
            f"зворотний зв'язок уже стоїть: «{found[child_id]['name']}» "
            f"{KINDS[back[0]['kind']][0]} «{found[parent_id]['name']}». "
            f"Спершу зніми його: /entity_unlink {child_id} {parent_id}")
    bot_db.execute(
        "INSERT INTO entity_links (parent_id, child_id, kind, note, decided_by, "
        "created) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (parent_id, child_id) DO UPDATE SET kind = EXCLUDED.kind, "
        "note = coalesce(EXCLUDED.note, entity_links.note), "
        "decided_by = EXCLUDED.decided_by",
        (parent_id, child_id, kind, note, decided_by, int(time.time())))
    return {"parent": found[parent_id], "child": found[child_id], "kind": kind}


def remove_link(a_id, b_id):
    """Зняти зв'язок. Пару приймаємо в будь-якому порядку: людина пам'ятає, що
    дві картки пов'язані, а не хто з них записаний першим."""
    ensure_schema()
    return bot_db.execute(
        "DELETE FROM entity_links WHERE (parent_id = %s AND child_id = %s) "
        "OR (parent_id = %s AND child_id = %s)", (a_id, b_id, b_id, a_id))


# ---------- читання ----------

LINKS_OF_SQL = """
SELECT l.id, l.kind, l.note, l.parent_id, l.child_id,
       (l.parent_id = %s) AS ya_starshyi,
       coalesce(p.name_ua, p.name_ru) AS pname, coalesce(p.mentions, 0) AS pm,
       coalesce(c.name_ua, c.name_ru) AS cname, coalesce(c.mentions, 0) AS cm
FROM entity_links l
LEFT JOIN entities p ON p.id = l.parent_id
LEFT JOIN entities c ON c.id = l.child_id
WHERE l.parent_id = %s OR l.child_id = %s
ORDER BY (l.parent_id = %s) DESC, coalesce(c.mentions, 0) DESC
"""


def links_of(entity_id):
    ensure_schema()
    return bot_db.query(LINKS_OF_SQL, (entity_id, entity_id, entity_id, entity_id))


def overview():
    """Скільки зв'язків і які. Потрібне, щоб побачити, що структура взагалі
    почала наповнюватись, — інакше про таблицю просто забудуть."""
    ensure_schema()
    by_kind = bot_db.query(
        "SELECT kind, count(*) AS n FROM entity_links GROUP BY kind ORDER BY n DESC")
    top = bot_db.query(
        "SELECT l.parent_id AS id, coalesce(e.name_ua, e.name_ru) AS name, "
        "       count(*) AS n "
        "FROM entity_links l LEFT JOIN entities e ON e.id = l.parent_id "
        "GROUP BY l.parent_id, e.name_ua, e.name_ru ORDER BY n DESC LIMIT 10")
    return by_kind, top


def sentence(row, entity_id=None):
    """Зв'язок РЕЧЕННЯМ, повними назвами. Саме речення ловить переплутаний
    напрямок: «обласна прокуратура входить до окружної» видно оком одразу,
    а «3271 → 2866» не видно ніяк."""
    verb_parent, verb_child = KINDS.get(row["kind"], (row["kind"], row["kind"]))
    if entity_id is not None and not row["ya_starshyi"]:
        return (f"«{row['cname']}» {verb_child} «{row['pname']}» "
                f"({row['parent_id']})")
    return (f"«{row['pname']}» {verb_parent} «{row['cname']}» "
            f"({row['child_id']})")


# ---------- переживання злиття ----------


def repoint_links(cur, loser_id, winner_id):
    """Перевісити зв'язки програшної картки на переможця. Кличеться з
    `entity_merge.record_merge` на ЙОГО курсорі — журнал і перевішування мають
    комітитись разом, інакше знімок описуватиме не те, що сталось.

    Повертає знімок для відкату: що переїхало і що довелось прибрати."""
    cur.execute(LINKS_DDL)
    # Рядки, де переможець УЖЕ стоїть із тим самим партнером: після
    # перевішування вони вперлись би в UNIQUE. Їх не переносимо, а прибираємо —
    # факт уже записаний, просто іншою карткою.
    cur.execute(
        "SELECT id, parent_id, child_id, kind, note FROM entity_links "
        "WHERE parent_id = %s OR child_id = %s", (loser_id, loser_id))
    rows = [{"id": r[0], "parent_id": r[1], "child_id": r[2], "kind": r[3],
             "note": r[4]} for r in cur.fetchall()]
    moved, dropped = [], []
    for r in rows:
        p = winner_id if r["parent_id"] == loser_id else r["parent_id"]
        c = winner_id if r["child_id"] == loser_id else r["child_id"]
        if p == c:
            # Зв'язок став сам на себе: злили дві картки, між якими він стояв.
            cur.execute("DELETE FROM entity_links WHERE id = %s", (r["id"],))
            dropped.append(r)
            continue
        cur.execute("SELECT id FROM entity_links WHERE parent_id = %s "
                    "AND child_id = %s", (p, c))
        if cur.fetchone():
            cur.execute("DELETE FROM entity_links WHERE id = %s", (r["id"],))
            dropped.append(r)
            continue
        cur.execute("UPDATE entity_links SET parent_id = %s, child_id = %s "
                    "WHERE id = %s", (p, c, r["id"]))
        moved.append(r)
    return {"moved": moved, "dropped": dropped}


def restore_links(snap, loser_id):
    """Повернути зв'язки на відновлену картку (відкат злиття)."""
    if not snap:
        return 0
    ensure_schema()
    n = 0
    for r in (snap.get("moved") or []):
        n += bot_db.execute(
            "UPDATE entity_links SET parent_id = %s, child_id = %s WHERE id = %s",
            (r["parent_id"], r["child_id"], r["id"]))
    for r in (snap.get("dropped") or []):
        n += bot_db.execute(
            "INSERT INTO entity_links (parent_id, child_id, kind, note, created) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (parent_id, child_id) "
            "DO NOTHING",
            (r["parent_id"], r["child_id"], r["kind"], r["note"], int(time.time())))
    return n


# ---------- пакет # cards-fix ----------


def describe_link_actions(actions):
    """Прев'ю рядків link/unlink повними назвами. Формат рядка той самий, що
    в решті прев'ю: спершу що станеться, потім чому це видно."""
    ensure_schema()
    ids = set()
    for a in actions:
        ids.update(a[1:3])
    names = {}
    if ids:
        for r in bot_db.query(
                "SELECT id, kind, coalesce(name_ua, name_ru) AS name, "
                "coalesce(mentions, 0) AS m FROM entities WHERE id = ANY(%s)",
                (sorted(ids),)):
            names[r["id"]] = r
    out = []
    for a in actions:
        op, pid, cid = a[0], a[1], a[2]
        kind = a[3] if len(a) > 3 else DEFAULT_KIND
        p, c = names.get(pid), names.get(cid)
        if not p or not c:
            missing = pid if not p else cid
            out.append(f"❌ немає картки {missing}")
            continue
        if op == "unlink":
            out.append(f"✂️ зняти зв'язок «{p['name']}» / «{c['name']}»")
            continue
        verb = KINDS.get(kind, (kind, kind))[0]
        out.append(f"«{p['name']}» ({p['m']}) {verb} «{c['name']}» ({c['m']})")
    return out


def apply_link_action(action, decided_by=None):
    """Виконати один рядок пакета. Кидає ValueError із людською причиною."""
    op, pid, cid = action[0], action[1], action[2]
    if op == "unlink":
        if not remove_link(pid, cid):
            raise ValueError("такого зв'язку не було")
        return
    kind = action[3] if len(action) > 3 else DEFAULT_KIND
    note = action[4] if len(action) > 4 else None
    add_link(pid, cid, kind, note, decided_by)


# ---------- команди ----------


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


def _card(entity_id):
    rows = bot_db.query(
        "SELECT id, kind, coalesce(name_ua, name_ru) AS name, "
        "coalesce(mentions, 0) AS m FROM entities WHERE id = %s", (entity_id,))
    return rows[0] if rows else None


def _format_card_links(card, rows):
    lines = [f"🦊 Зв'язки картки «{card['name']}» ({card['id']}, "
             f"{card['m']} згадок)\n"]
    up = [r for r in rows if not r["ya_starshyi"]]
    down = [r for r in rows if r["ya_starshyi"]]
    if up:
        lines.append("Над нею:")
        for r in up:
            note = f" — {r['note']}" if r["note"] else ""
            lines.append("  " + sentence(r, card["id"]) + note)
    if down:
        lines.append("\nПід нею:")
        for r in down:
            note = f" — {r['note']}" if r["note"] else ""
            lines.append("  " + sentence(r, card["id"]) + note)
    if not rows:
        lines.append("Зв'язків немає.\n\nПоставити: /entity_link "
                     f"<id старшої> {card['id']} [тип]")
    else:
        lines.append(f"\nЗняти: /entity_unlink {card['id']} <id другої>")
    return "\n".join(lines)[:4000]


async def entity_links_handler(update, context):
    """/entity_links [id] — структура органу: хто над карткою і хто під нею.

    Без аргументу — скільки зв'язків уже є і в кого їх найбільше."""
    if not _allowed(update):
        return
    args = context.args or []
    if args and args[0].isdigit():
        eid = int(args[0])

        def build():
            ensure_schema()
            return _card(eid), links_of(eid)

        try:
            card, rows = await asyncio.to_thread(build)
        except Exception as e:
            await update.message.reply_text(f"❌ Нора недоступна: {e}")
            return
        if not card:
            await update.message.reply_text(f"Картки {eid} немає.")
            return
        await update.message.reply_text(_format_card_links(card, rows))
        return

    try:
        by_kind, top = await asyncio.to_thread(overview)
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    total = sum(r["n"] for r in by_kind)
    if not total:
        await update.message.reply_text(
            "🦊 Структури поки немає — жодного зв'язку між картками.\n\n"
            "Ставиться там же, де розбираються дублі: рядком `link <старша> "
            "<молодша> [тип]` у пакеті `# cards-fix` або командою\n"
            "/entity_link <id старшої> <id молодшої> [тип]\n\n"
            "Типи: " + " · ".join(f"{k} ({v[1]})" for k, v in KINDS.items()))
        return
    lines = [f"🦊 Зв'язків між картками: {total}\n"]
    for r in by_kind:
        lines.append(f"  {r['kind']} — {r['n']}")
    if top:
        lines.append("\nНайбільші вузли:")
        for r in top:
            lines.append(f"  «{r['name']}» ({r['id']}) — {r['n']} під нею")
    lines.append("\nОдна картка: /entity_links <id>")
    await update.message.reply_text("\n".join(lines)[:4000])


async def entity_link_handler(update, context):
    """/entity_link <id старшої> <id молодшої> [тип] [нотатка]"""
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text(
            "Формат: /entity_link <id старшої> <id молодшої> [тип] [нотатка]\n\n"
            "Перший id — старша картка, як і в /entity_merge.\n"
            + "\n".join(f"  {k} — «молодша {v[1]} старшої»"
                        for k, v in KINDS.items())
            + "\n\nДля правонаступництва перший id — ПОПЕРЕДНИК: "
              "/entity_link 28842 2872 правонаступник")
        return
    pid, cid = int(args[0]), int(args[1])
    kind, note = DEFAULT_KIND, None
    rest = args[2:]
    if rest:
        maybe = normalize_kind(rest[0])
        if maybe:
            kind, rest = maybe, rest[1:]
        note = " ".join(rest) or None
    who = update.effective_user.full_name if update.effective_user else None
    try:
        res = await asyncio.to_thread(add_link, pid, cid, kind, note, who)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    verb = KINDS[res["kind"]][0]
    await update.message.reply_text(
        f"✅ «{res['parent']['name']}» {verb} «{res['child']['name']}».\n\n"
        f"Перевірити: /entity_links {pid}\n"
        f"Зняти: /entity_unlink {pid} {cid}")


async def entity_unlink_handler(update, context):
    """/entity_unlink <id> <id> — зняти зв'язок, порядок будь-який."""
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Формат: /entity_unlink <id> <id>")
        return
    try:
        n = await asyncio.to_thread(remove_link, int(args[0]), int(args[1]))
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    await update.message.reply_text(
        "✅ Зв'язок знято." if n else "Такого зв'язку не було.")
