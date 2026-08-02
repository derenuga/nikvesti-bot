"""
Канон ролей сутнісного шару — друга вісь дедупу (docs/ENTITY_MERGE_PLAN.md §3.1).

Одна посада живе в норі десятками написань: «мер Миколаєва», «міський голова»,
«Миколаївський міський голова», «очільник Миколаєва». Це НЕ дублі карток —
роль лежить у `article_entities.role_at_time` вільним текстом, окремо на кожну
статтю. Тому й лікується не злиттям сутностей, а довідником поруч:

    role_canon    — канонічна посада (+ org_entity_id: орган, тобто АФІЛІАЦІЯ)
    role_variants — сире написання → канон
    role_pairs    — черга питань і ПАМ'ЯТЬ ВІДМОВ («ні, різні» назавжди)

**Сирий `role_at_time` не переписується ніколи.** Він факт статті: показує, як
саме людину називали тоді. Канон лягає поруч, довідником, і зводиться через
view `v_entity_roles`. Через це відкат тривіальний — рядок у `role_variants`
(/roles_forget), а не відновлення видалених даних, як довелося б при злитті
карток.

Чому це робиться ПЕРШИМ (§3.1 плану):
  • сигнал сильніший — дві різні строки ролі в ОДНОГО носія в перетинні
    періоди майже напевно одна посада; однофамільців тут немає в принципі,
    тобто головна пастка §2 (однофамілець виглядає як зміна посади) не діє;
  • ризику незворотно склеїти двох людей немає — нічого не видаляється;
  • `role_canon.org_entity_id` — це і є афіліація людина↔організація, якої
    бракує банку тем (PROMISES_BANK.md §6): «усі обіцянки посадовців ОВА»
    стає JOIN-ом, а не пошуком по підрядку.

Детектор кандидатів — БЕЗ AI (§3.1): спільний носій (з перевіркою перетину
періодів), trgm-схожість, вкладеність токенів, спільне рідкісне слово органу.
Рішення ухвалює ЛЮДИНА інлайн-кнопками. Автозлиття немає і не планується.

Команди (whitelist ALLOWED_USER_IDS):
    /roles                — стан довідника: варіанти, канони, покриття, черга
    /roles_dedup [N]      — прогнати детектор і питати кнопками
    /roles_canon [N]      — список канонів з варіантами й афіліацією
    /roles_rename <id> <текст> — переназвати канон
    /roles_forget <текст|id>   — відкат: зняти варіант з канону / видалити канон
"""

import asyncio
import os
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers import bot_db

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# ---------- Нормалізація ролі ----------
#
# Живе В ДВОХ місцях — SQL-функцією (щоб нею можна було групувати, індексувати
# і будувати view) і в Python (щоб детектор рахував токени без походу в БД).
# Розійтися вони не мають права: test_entity_roles.py звіряє їх на КОЖНОМУ
# унікальному написанні ролі з нори, а не на вигаданих зразках.
#
# Порядок кроків однаковий: translate (тире→дефіс, нерозривні пробіли→пробіл,
# декоративні лапки геть) → lower → схлопування пробілів → обрізання крайової
# пунктуації. Лапки прибираємо з ключа, а не з тексту: «в.о. голови» і
# "в.о. голови" мають бути однією роллю.

_TR_FROM = "‐‑–—−    «»“”„‟\"'`"
_TR_TO = "-----    "          # 5 тире → '-', 4 пробільні → ' ', решта видаляється

_PY_TRANS = {}
for _i, _ch in enumerate(_TR_FROM):
    _PY_TRANS[ord(_ch)] = _TR_TO[_i] if _i < len(_TR_TO) else None

_STRIP_CHARS = " .,;:-"
# POSIX [:space:] один в один — щоб Python і SQL схлопували РІВНО ті самі
# символи (звичайний \s у Python ширший за [[:space:]] у Postgres; нерозривні
# пробіли обидва бачать однаково лише тому, що їх зняв translate вище).
_SPACE_RE = re.compile(r"[ \t\n\r\f\v]+")

# SQL-дублікат тієї самої нормалізації. IMMUTABLE — щоб по ній можна було
# будувати індекс і view.
ROLE_NORM_DDL = """
CREATE OR REPLACE FUNCTION role_norm(s TEXT) RETURNS TEXT AS $$
  SELECT nullif(
    btrim(
      regexp_replace(
        lower(translate(coalesce(s, ''), '{f}', '{t}')),
        '[[:space:]]+', ' ', 'g'),
      ' .,;:-'),
    '')
$$ LANGUAGE SQL IMMUTABLE
""".format(f=_TR_FROM.replace("'", "''"), t=_TR_TO)


def role_norm(s):
    """Нормалізоване написання ролі — ключ довідника. Мусить збігатися з
    SQL-функцією role_norm() до символу (перевіряється тестом)."""
    if not s:
        return None
    s = _SPACE_RE.sub(" ", s.translate(_PY_TRANS).lower())
    s = s.strip(_STRIP_CHARS)
    return s or None


# ---------- Схема ----------

ROLES_DDL = """
CREATE TABLE IF NOT EXISTS role_canon (
    id            BIGSERIAL PRIMARY KEY,
    canon         TEXT NOT NULL,
    canon_norm    TEXT NOT NULL UNIQUE,
    org_entity_id BIGINT,
    created       BIGINT
);
CREATE TABLE IF NOT EXISTS role_variants (
    raw_norm   TEXT PRIMARY KEY,
    raw_sample TEXT,
    canon_id   BIGINT NOT NULL REFERENCES role_canon (id) ON DELETE CASCADE,
    decided_by TEXT,
    created    BIGINT
);
CREATE INDEX IF NOT EXISTS idx_role_variants_canon ON role_variants (canon_id);
CREATE TABLE IF NOT EXISTS role_pairs (
    id         BIGSERIAL PRIMARY KEY,
    a_norm     TEXT NOT NULL,
    b_norm     TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    signals    TEXT,
    verdict    TEXT,
    decided_by TEXT,
    updated    BIGINT,
    UNIQUE (a_norm, b_norm)
);
CREATE INDEX IF NOT EXISTS idx_role_pairs_open ON role_pairs (score DESC) WHERE verdict IS NULL;
"""

# Індекс по нормалізованій ролі: усі запити довідника групують саме по ній,
# без нього кожна картка питання — seq scan по всьому article_entities.
ROLE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_ae_role_norm "
    "ON article_entities (role_norm(role_at_time))"
)

# Зведений погляд для споживачів (досьє, банк тем): сира роль ЛИШАЄТЬСЯ на
# місці, поруч їде канон і орган. Без канону — canon дорівнює сирій ролі, тож
# JOIN працює однаково і до дедупу, і після.
ROLE_VIEW_DDL = """
CREATE OR REPLACE VIEW v_entity_roles AS
SELECT ae.article_id,
       ae.entity_id,
       ae.role_at_time,
       ae.salience,
       rc.id                          AS canon_id,
       coalesce(rc.canon, ae.role_at_time) AS role_canon,
       rc.org_entity_id
FROM article_entities ae
LEFT JOIN role_variants rv ON rv.raw_norm = role_norm(ae.role_at_time)
LEFT JOIN role_canon rc    ON rc.id = rv.canon_id
WHERE ae.role_at_time IS NOT NULL AND ae.role_at_time <> ''
"""

_schema_done = {"flag": False}


def ensure_schema(force=False):
    """Ідемпотентно: функція нормалізації, таблиці довідника, індекс, view."""
    if _schema_done["flag"] and not force:
        return
    bot_db.execute(ROLE_NORM_DDL)
    bot_db.execute(ROLES_DDL)
    bot_db.execute(ROLE_INDEX_DDL)
    bot_db.execute(ROLE_VIEW_DDL)
    _schema_done["flag"] = True


# ---------- Детектор кандидатів (без AI) ----------

MIN_LINKS = 3        # роль має траплятись хоч у стількох матеріалах
TOP_ROLES = 500      # скільки найчастіших ролей беремо в перебір пар
SIM_MIN = 0.45       # поріг trgm-схожості написання
SCORE_MIN = 2.0      # нижче — в чергу не кладемо
RARE_DF = 0.15       # токен «рідкісний», якщо він у ≤15% ролей (слово органу)

ROLE_STATS_SQL = """
SELECT role_norm(ae.role_at_time) AS rn,
       count(*)                   AS links,
       count(DISTINCT ae.entity_id) AS people,
       min(a.published)           AS lo,
       max(a.published)           AS hi,
       mode() WITHIN GROUP (ORDER BY ae.role_at_time) AS sample
FROM article_entities ae
JOIN articles a ON a.id = ae.article_id
WHERE ae.role_at_time IS NOT NULL
  AND role_norm(ae.role_at_time) IS NOT NULL
GROUP BY 1
HAVING count(*) >= %s
ORDER BY links DESC
LIMIT %s
"""

CARRIERS_SQL = """
SELECT role_norm(ae.role_at_time) AS rn, ae.entity_id,
       min(a.published) AS lo, max(a.published) AS hi
FROM article_entities ae
JOIN articles a ON a.id = ae.article_id
WHERE ae.role_at_time IS NOT NULL
  AND role_norm(ae.role_at_time) = ANY(%s)
GROUP BY 1, 2
"""

SIM_SQL = """
SELECT a.rn AS a_norm, b.rn AS b_norm, similarity(a.rn, b.rn) AS sim
FROM unnest(%s::text[]) AS a(rn)
JOIN unnest(%s::text[]) AS b(rn) ON a.rn < b.rn
WHERE similarity(a.rn, b.rn) >= %s
"""


def _tokens(rn):
    return [t for t in re.split(r"[^\w]+", rn or "") if t]


def plural(n, one, few, many):
    """«1 згадка · 2 згадки · 5 згадок» — числа читає людина, а не парсер."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def find_pairs(min_links=MIN_LINKS, top_roles=TOP_ROLES):
    """ЧИСТИЙ детектор: рахує кандидатів і НІЧОГО не пише (з нього ж живе
    read-only замір /entity_scale). Повертає (ролей у переборі, [(a, b, score,
    signals)])."""
    ensure_schema()
    stats = bot_db.query(ROLE_STATS_SQL, (min_links, top_roles))
    roles = {r["rn"]: r for r in stats}
    names = list(roles)
    if len(names) < 2:
        return len(names), []

    # (а) спільний носій. Найсильніший сигнал: одна людина під двома написаннями
    # у ПЕРЕТИННІ періоди — це майже напевно одна посада, а не підвищення.
    shared, overlap = {}, {}
    by_entity = {}
    for r in bot_db.query(CARRIERS_SQL, (names,)):
        by_entity.setdefault(r["entity_id"], []).append(
            (r["rn"], r["lo"] or 0, r["hi"] or 0))
    for spans in by_entity.values():
        spans.sort()
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                a, alo, ahi = spans[i]
                b, blo, bhi = spans[j]
                key = (a, b)
                shared[key] = shared.get(key, 0) + 1
                if alo <= bhi and blo <= ahi:
                    overlap[key] = overlap.get(key, 0) + 1

    # (б) схожість написання (pg_trgm). Ловить «Кім/Ким», подвоєння, дефіси.
    sims = {}
    try:
        bot_db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for r in bot_db.query(SIM_SQL, (names, names, SIM_MIN)):
            sims[(r["a_norm"], r["b_norm"])] = float(r["sim"])
    except Exception as e:
        # Без розширення детектор просто слабший — решта сигналів працює.
        print(f"entity_roles: pg_trgm недоступний, схожість написання пропущено — {e}")

    # (в) вкладеність токенів («мер міста миколаєва» ⊃ «мер миколаєва»)
    # і (г) спільне рідкісне слово — зазвичай саме назва органу.
    toks = {rn: set(_tokens(rn)) for rn in names}
    df = {}
    for ts in toks.values():
        for t in ts:
            df[t] = df.get(t, 0) + 1
    rare_cap = max(1, int(len(names) * RARE_DF))

    cands = set(shared) | set(sims)
    # вкладеність шукаємо лише серед уже підозрілих + пар зі спільним рідкісним
    # словом, інакше це повний квадрат по всіх ролях без користі
    by_rare = {}
    for rn, ts in toks.items():
        for t in ts:
            if len(t) >= 5 and df.get(t, 0) <= rare_cap:
                by_rare.setdefault(t, []).append(rn)
    for group in by_rare.values():
        group.sort()
        if len(group) > 40:      # надто широке слово — сигналом не рахуємо
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                cands.add((group[i], group[j]))

    rows = []
    for a, b in cands:
        if a not in roles or b not in roles:
            continue
        sig, score = [], 0.0
        ov = overlap.get((a, b), 0)
        sh = shared.get((a, b), 0)
        if ov:
            score += 3
            sig.append(f"спільних носіїв у перетинні періоди: {ov}")
        elif sh:
            score += 1
            sig.append(f"спільних носіїв (періоди не перетинаються): {sh}")
        sim = sims.get((a, b))
        if sim is not None:
            score += 2 if sim >= 0.6 else 1
            sig.append(f"схожість написання {sim:.2f}")
        ta, tb = toks[a], toks[b]
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if len(short) >= 2 and short < long_:
            score += 2
            sig.append("одне написання вкладене в інше")
        common_rare = sorted(
            t for t in (ta & tb) if len(t) >= 5 and df.get(t, 0) <= rare_cap)
        if common_rare:
            score += 1
            sig.append("спільне слово: " + ", ".join(common_rare[:2]))
        # рідкісне слово САМЕ ПО СОБІ кандидатом не робить: «департаменту»
        # ділять десятки різних посад
        if not (sh or sim is not None or "вкладене" in " ".join(sig)):
            continue
        if score < SCORE_MIN:
            continue
        rows.append((a, b, score, " · ".join(sig)))
    rows.sort(key=lambda r: -r[2])
    return len(names), rows


def scan_pairs(min_links=MIN_LINKS, top_roles=TOP_ROLES):
    """Прогнати детектор і покласти нові пари в чергу.

    Нічого не вирішує і не чіпає вже вирішені пари: рішення людини («ні, різні»)
    живе назавжди, тому повторний прогін лише додає нове й освіжає бали
    невирішених. Повертає (ролей у переборі, кандидатів, з них нових/оновлених,
    у черзі)."""
    n_roles, rows = find_pairs(min_links, top_roles)
    added = 0
    now = int(time.time())
    for a, b, score, sig in rows:
        n = bot_db.execute(
            "INSERT INTO role_pairs (a_norm, b_norm, score, signals, updated) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (a_norm, b_norm) DO UPDATE SET "
            "  score = EXCLUDED.score, signals = EXCLUDED.signals, "
            "  updated = EXCLUDED.updated "
            "WHERE role_pairs.verdict IS NULL",
            (a, b, score, sig, now))
        added += 1 if n else 0
    return n_roles, len(rows), added, _pending_count()


def _pending_count():
    rows = bot_db.query(
        """
        SELECT count(*) AS n
        FROM role_pairs p
        LEFT JOIN role_variants va ON va.raw_norm = p.a_norm
        LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm
        WHERE p.verdict IS NULL
          AND (va.canon_id IS NULL OR vb.canon_id IS NULL
               OR va.canon_id <> vb.canon_id)
        """)
    return rows[0]["n"] if rows else 0


# ---------- Злиття ролей у канон ----------

# Народні/розмовні форми посади. Канон має бути офіційним написанням, інакше
# у досьє й заявках стоятиме «мер» там, де в документах «міський голова».
_COLLOQUIAL = {"мер", "мера", "меру", "мером", "очільник", "очільника",
               "градоначальник", "градоначальника", "шеф", "мерка"}


def pick_canon(sample_a, sample_b):
    """Яке з двох написань стає канонічним: офіційне переважає розмовне,
    за інших рівних — повніше (§5 плану: канон не «за частотою», а за повнотою)."""
    a, b = (sample_a or "").strip(), (sample_b or "").strip()
    if not a:
        return b
    if not b:
        return a
    ca = bool(_COLLOQUIAL & set(_tokens(role_norm(a))))
    cb = bool(_COLLOQUIAL & set(_tokens(role_norm(b))))
    if ca != cb:
        return b if ca else a
    return a if len(a) >= len(b) else b


def _variant_canon(raw_norm):
    rows = bot_db.query(
        "SELECT canon_id FROM role_variants WHERE raw_norm = %s", (raw_norm,))
    return rows[0]["canon_id"] if rows else None


def _canon_size(canon_id):
    rows = bot_db.query(
        "SELECT count(*) AS n FROM role_variants WHERE canon_id = %s", (canon_id,))
    return rows[0]["n"] if rows else 0


def merge_roles(a_norm, b_norm, sample_a=None, sample_b=None, decided_by=None):
    """Звести два написання до одного канону. Повертає (canon_id, canon_text).

    Чотири випадки: обидва без канону (створюємо), один без канону (чіпляємо),
    обидва в одному (нічого), обидва в різних (переносимо варіанти меншого
    канону в більший і видаляємо порожній). Ідемпотентно."""
    ensure_schema()
    now = int(time.time())
    sample_a = sample_a or a_norm
    sample_b = sample_b or b_norm
    ca, cb = _variant_canon(a_norm), _variant_canon(b_norm)

    if ca and cb and ca == cb:
        rows = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (ca,))
        return ca, (rows[0]["canon"] if rows else None)

    if ca and cb:
        # обидва вже мають канон — зливаємо канони (більший поглинає менший)
        keep, drop = (ca, cb) if _canon_size(ca) >= _canon_size(cb) else (cb, ca)
        with bot_db.transaction():
            bot_db.execute(
                "UPDATE role_variants SET canon_id = %s WHERE canon_id = %s",
                (keep, drop))
            bot_db.execute(
                "UPDATE role_canon SET org_entity_id = coalesce(org_entity_id, "
                "  (SELECT org_entity_id FROM role_canon WHERE id = %s)) "
                "WHERE id = %s", (drop, keep))
            bot_db.execute("DELETE FROM role_canon WHERE id = %s", (drop,))
        rows = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (keep,))
        return keep, (rows[0]["canon"] if rows else None)

    if ca or cb:
        canon_id = ca or cb
        new_norm = b_norm if ca else a_norm
        new_sample = sample_b if ca else sample_a
        bot_db.execute(
            "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, decided_by, created) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (raw_norm) DO UPDATE SET canon_id = EXCLUDED.canon_id",
            (new_norm, new_sample, canon_id, decided_by, now))
        rows = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (canon_id,))
        return canon_id, (rows[0]["canon"] if rows else None)

    canon = pick_canon(sample_a, sample_b)
    cnorm = role_norm(canon)
    with bot_db.transaction():
        rows = bot_db.query(
            "INSERT INTO role_canon (canon, canon_norm, created) VALUES (%s, %s, %s) "
            "ON CONFLICT (canon_norm) DO UPDATE SET canon = EXCLUDED.canon "
            "RETURNING id", (canon, cnorm, now))
        canon_id = rows[0]["id"]
        for rn, sample in ((a_norm, sample_a), (b_norm, sample_b)):
            bot_db.execute(
                "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, decided_by, created) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (raw_norm) DO UPDATE SET canon_id = EXCLUDED.canon_id",
                (rn, sample, canon_id, decided_by, now))
    return canon_id, canon


def set_verdict(pair_id, verdict, decided_by=None):
    bot_db.execute(
        "UPDATE role_pairs SET verdict = %s, decided_by = %s, updated = %s "
        "WHERE id = %s", (verdict, decided_by, int(time.time()), pair_id))


# ---------- Дані для картки питання ----------

ROLE_CARD_SQL = """
SELECT count(*) AS links,
       count(DISTINCT ae.entity_id) AS people,
       to_char(to_timestamp(min(a.published)), 'YYYY-MM') AS lo,
       to_char(to_timestamp(max(a.published)), 'YYYY-MM') AS hi,
       mode() WITHIN GROUP (ORDER BY ae.role_at_time) AS sample
FROM article_entities ae
JOIN articles a ON a.id = ae.article_id
WHERE role_norm(ae.role_at_time) = %s
"""

ROLE_CARRIERS_SQL = """
SELECT coalesce(e.name_ua, e.name_ru) AS name, count(*) AS c
FROM article_entities ae
JOIN entities e ON e.id = ae.entity_id
WHERE role_norm(ae.role_at_time) = %s
GROUP BY 1 ORDER BY c DESC LIMIT 2
"""

ORG_CANDIDATES_SQL = """
SELECT e.id, coalesce(e.name_ua, e.name_ru) AS name, count(*) AS c
FROM article_entities ae
JOIN entities e ON e.id = ae.entity_id AND e.kind = 'org'
WHERE ae.article_id IN (
    SELECT ae2.article_id FROM article_entities ae2
    WHERE role_norm(ae2.role_at_time) = ANY(%s))
GROUP BY 1, 2 ORDER BY c DESC LIMIT 3
"""

NEXT_PAIR_SQL = """
SELECT p.id, p.a_norm, p.b_norm, p.score, p.signals
FROM role_pairs p
LEFT JOIN role_variants va ON va.raw_norm = p.a_norm
LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm
WHERE p.verdict IS NULL
  AND (va.canon_id IS NULL OR vb.canon_id IS NULL OR va.canon_id <> vb.canon_id)
  AND NOT (p.id = ANY(%s))
ORDER BY p.score DESC, p.id
LIMIT 1
"""


def _role_card(rn):
    rows = bot_db.query(ROLE_CARD_SQL, (rn,))
    card = dict(rows[0]) if rows else {}
    card["carriers"] = [r["name"] for r in bot_db.query(ROLE_CARRIERS_SQL, (rn,))
                        if r["name"]]
    card["sample"] = card.get("sample") or rn
    return card


def next_question(exclude_ids):
    rows = bot_db.query(NEXT_PAIR_SQL, (list(exclude_ids or []),))
    if not rows:
        return None
    p = dict(rows[0])
    p["a"] = _role_card(p["a_norm"])
    p["b"] = _role_card(p["b_norm"])
    return p


def _question_text(p):
    def block(letter, card):
        who = (" · " + ", ".join(card["carriers"])) if card["carriers"] else ""
        period = (f"{card.get('lo')} … {card.get('hi')}"
                  if card.get("lo") else "період невідомий")
        return (f"{letter}: «{card['sample']}»\n"
                f"   {plural(card.get('links', 0), 'згадка', 'згадки', 'згадок')} · "
                f"{plural(card.get('people', 0), 'носій', 'носії', 'носіїв')} · "
                f"{period}{who}")

    return ("🦊 Це та сама посада?\n\n"
            + block("А", p["a"]) + "\n"
            + block("Б", p["b"]) + "\n\n"
            + f"Підстава: {p['signals']}")


def _question_markup(pair_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, одне", callback_data=f"rdp:s:{pair_id}"),
        InlineKeyboardButton("❌ Ні, різні", callback_data=f"rdp:d:{pair_id}"),
        InlineKeyboardButton("↷ Пропустити", callback_data=f"rdp:x:{pair_id}"),
    ]])


# У пам'яті: що людина пропустила в цій сесії. Свідомо НЕ в БД — «пропустити»
# означає «не зараз», а не «різні»; після перезапуску бота питання повернеться.
_skipped = {}


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


async def _send_next(chat, user_id, bot=None):
    """Наступне питання черги — або підсумок, коли черга скінчилась."""
    skip = _skipped.setdefault(user_id, set())
    p = await asyncio.to_thread(next_question, skip)
    if not p:
        n = await asyncio.to_thread(_pending_count)
        tail = (f"\nПропущено в цій сесії: {len(skip)} — вони повернуться "
                f"наступним /roles_dedup." if skip else "")
        text = ("🦊 Черга питань по ролях порожня." if not n
                else f"🦊 Питань більше немає ({n} у черзі закрито канонами).")
        await chat.send_message(text + tail + "\nСтан довідника: /roles")
        return
    await chat.send_message(_question_text(p), reply_markup=_question_markup(p["id"]))


# ---------- /roles_dedup ----------

async def roles_dedup_handler(update, context):
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    args = context.args or []
    try:
        top = int(args[0]) if args else TOP_ROLES
    except ValueError:
        top = TOP_ROLES
    top = min(max(top, 20), 2000)
    msg = await update.message.reply_text(
        f"🦊 Шукаю однакові посади в різних написаннях (топ-{top} ролей)…")
    try:
        n_roles, n_pairs, n_new, pending = await asyncio.to_thread(
            scan_pairs, MIN_LINKS, top)
    except Exception as e:
        await msg.edit_text(f"❌ Детектор упав: {type(e).__name__}: {e}")
        return
    await msg.edit_text(
        f"🦊 Ролей у переборі: {n_roles} · кандидатів: {n_pairs} "
        f"(нових/оновлених: {n_new}) · у черзі: {pending}\n"
        f"Сирий текст ролей не чіпається — довідник лягає поруч.")
    _skipped.pop(update.effective_user.id, None)
    await _send_next(update.effective_chat, update.effective_user.id)


# ---------- колбеки кнопок ----------

async def roles_pair_callback(update, context):
    """Відповідь на питання «це та сама посада?»."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        _, action, raw_id = query.data.split(":", 2)
        pair_id = int(raw_id)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()

    rows = await bot_db.aquery(
        "SELECT a_norm, b_norm FROM role_pairs WHERE id = %s", (pair_id,))
    if not rows:
        await query.edit_message_text("Пара зникла з черги.")
        return
    a_norm, b_norm = rows[0]["a_norm"], rows[0]["b_norm"]
    who = query.from_user.full_name if query.from_user else None

    if action == "x":
        _skipped.setdefault(user_id, set()).add(pair_id)
        await query.edit_message_text(
            (query.message.text or "") + "\n\n↷ Пропущено (повернеться наступного разу).")
        await _send_next(query.message.chat, user_id)
        return

    if action == "d":
        await asyncio.to_thread(set_verdict, pair_id, "different", who)
        await query.edit_message_text(
            (query.message.text or "") + "\n\n❌ Різні посади — більше не спитаю.")
        await _send_next(query.message.chat, user_id)
        return

    if action != "s":
        return

    def do_merge():
        a = _role_card(a_norm)
        b = _role_card(b_norm)
        canon_id, canon = merge_roles(a_norm, b_norm, a["sample"], b["sample"], who)
        set_verdict(pair_id, "same", who)
        rows_ = bot_db.query(
            "SELECT count(*) AS n, bool_or(org_entity_id IS NOT NULL) AS has_org "
            "FROM role_canon rc JOIN role_variants rv ON rv.canon_id = rc.id "
            "WHERE rc.id = %s", (canon_id,))
        n_vars = rows_[0]["n"] if rows_ else 2
        has_org = bool(rows_[0]["has_org"]) if rows_ else False
        orgs = []
        if not has_org:
            variants = [r["raw_norm"] for r in bot_db.query(
                "SELECT raw_norm FROM role_variants WHERE canon_id = %s", (canon_id,))]
            orgs = bot_db.query(ORG_CANDIDATES_SQL, (variants,))
        return canon_id, canon, n_vars, has_org, orgs

    try:
        canon_id, canon, n_vars, has_org, orgs = await asyncio.to_thread(do_merge)
    except Exception as e:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n❌ Не вдалось звести: {e}")
        return

    text = ((query.message.text or "")
            + f"\n\n✅ Одна посада: «{canon}» ({n_vars} написань).")
    if orgs:
        text += "\n\nЧий це орган? (дасть афіліацію людина↔організація)"
        rows_kb = [[InlineKeyboardButton(f"{o['name']} ({o['c']})",
                                         callback_data=f"rdo:{canon_id}:{o['id']}")]
                   for o in orgs]
        rows_kb.append([InlineKeyboardButton(
            "↷ Не зараз", callback_data=f"rdo:{canon_id}:0")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows_kb))
    else:
        await query.edit_message_text(text)
    await _send_next(query.message.chat, user_id)


async def roles_org_callback(update, context):
    """Прив'язка канону посади до органу — та сама афіліація, якої бракує
    банку тем (PROMISES_BANK.md §6)."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        _, canon_id, org_id = query.data.split(":", 2)
        canon_id, org_id = int(canon_id), int(org_id)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()
    base = (query.message.text or "").split("\n\nЧий це орган?")[0]
    if not org_id:
        await query.edit_message_text(base + "\n\n↷ Орган не вказано.")
        return

    def bind():
        bot_db.execute("UPDATE role_canon SET org_entity_id = %s WHERE id = %s",
                       (org_id, canon_id))
        rows = bot_db.query(
            "SELECT coalesce(name_ua, name_ru) AS name FROM entities WHERE id = %s",
            (org_id,))
        return rows[0]["name"] if rows else str(org_id)

    try:
        name = await asyncio.to_thread(bind)
    except Exception as e:
        await query.edit_message_text(base + f"\n\n❌ Не вдалось прив'язати орган: {e}")
        return
    await query.edit_message_text(base + f"\n\n🏛 Орган: {name}")


# ---------- /roles ----------

async def roles_handler(update, context):
    """Стан довідника ролей: покриття, канони, черга питань."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Рахую довідник ролей…")

    def build():
        ensure_schema()
        tot = bot_db.query(
            "SELECT count(*) AS links, "
            "       count(DISTINCT role_norm(role_at_time)) AS variants "
            "FROM article_entities "
            "WHERE role_at_time IS NOT NULL AND role_norm(role_at_time) IS NOT NULL")[0]
        cov = bot_db.query(
            "SELECT count(*) AS links "
            "FROM article_entities ae JOIN role_variants rv "
            "  ON rv.raw_norm = role_norm(ae.role_at_time)")[0]["links"]
        canons = bot_db.query(
            "SELECT count(*) AS n, count(org_entity_id) AS with_org FROM role_canon")[0]
        top = bot_db.query(
            """
            SELECT rc.canon, count(DISTINCT rv.raw_norm) AS variants,
                   count(ae.article_id) AS links,
                   coalesce(o.name_ua, o.name_ru) AS org
            FROM role_canon rc
            JOIN role_variants rv ON rv.canon_id = rc.id
            LEFT JOIN article_entities ae ON role_norm(ae.role_at_time) = rv.raw_norm
            LEFT JOIN entities o ON o.id = rc.org_entity_id
            GROUP BY rc.id, rc.canon, o.name_ua, o.name_ru
            ORDER BY links DESC LIMIT 5
            """)
        return tot, cov, canons, top, _pending_count()

    try:
        tot, cov, canons, top, pending = await asyncio.to_thread(build)
    except Exception as e:
        await msg.edit_text(f"❌ Нора недоступна: {e}")
        return
    links = tot["links"] or 0
    pct = (100 * cov // links) if links else 0
    lines = ["🦊 Канон ролей\n",
             f"Сирих написань: {tot['variants']} на {links} зв'язків",
             f"Зведено до канону: {canons['n']} посад, покриття {cov} зв'язків ({pct}%)",
             f"З афіліацією (орган вказано): {canons['with_org']}",
             f"У черзі питань: {pending}"]
    if top:
        lines.append("\nНайбільші канони:")
        for r in top:
            org = f" · {r['org']}" if r["org"] else ""
            links = plural(r["links"], "зв’язок", "зв’язки", "зв’язків")
            lines.append(f"  «{r['canon']}» — {r['variants']} написань, "
                         f"{links}{org}")
    lines.append("\nПитати кнопками: /roles_dedup · список: /roles_canon")
    await msg.edit_text("\n".join(lines))


# ---------- /roles_canon ----------

async def roles_canon_handler(update, context):
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
            """
            SELECT rc.id, rc.canon, coalesce(o.name_ua, o.name_ru) AS org,
                   array_agg(rv.raw_sample ORDER BY rv.raw_sample) AS variants
            FROM role_canon rc
            JOIN role_variants rv ON rv.canon_id = rc.id
            LEFT JOIN entities o ON o.id = rc.org_entity_id
            GROUP BY rc.id, rc.canon, o.name_ua, o.name_ru
            ORDER BY count(*) DESC, rc.id
            LIMIT %s
            """, (n,))

    try:
        rows = await asyncio.to_thread(build)
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    if not rows:
        await update.message.reply_text(
            "Канонів ще немає — /roles_dedup поставить перші питання.")
        return
    lines = ["🦊 Канон посад\n"]
    for r in rows:
        org = f" · 🏛 {r['org']}" if r["org"] else ""
        lines.append(f"[{r['id']}] «{r['canon']}»{org}")
        lines.append("   " + " | ".join(v for v in r["variants"] if v))
    lines.append("\nПереназвати: /roles_rename <id> <текст> · "
                 "відкат: /roles_forget <текст|id>")
    await update.message.reply_text("\n".join(lines))


# ---------- /roles_rename ----------

async def roles_rename_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Формат: /roles_rename <id канону> <нова назва>\n"
            "id підкаже /roles_canon")
        return
    canon_id, canon = int(args[0]), " ".join(args[1:]).strip()

    def do():
        ensure_schema()
        return bot_db.execute(
            "UPDATE role_canon SET canon = %s, canon_norm = %s WHERE id = %s",
            (canon, role_norm(canon), canon_id))

    try:
        n = await asyncio.to_thread(do)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалось: {e}")
        return
    await update.message.reply_text(
        f"🦊 Канон {canon_id} → «{canon}»." if n else f"Канону {canon_id} немає.")


# ---------- /roles_forget ----------

async def roles_forget_handler(update, context):
    """Відкат. Сирий role_at_time не чіпався ніколи, тому «відкотити» —
    це прибрати рядок довідника: варіант відв'язується від канону, пари
    повертаються в чергу. Числом — видалити канон цілком."""
    if not _allowed(update):
        return
    arg = " ".join(context.args or []).strip()
    if not arg:
        await update.message.reply_text(
            "Формат: /roles_forget <написання ролі> — зняти варіант з канону\n"
            "        /roles_forget <id канону> — розібрати канон цілком\n"
            "Сирий текст ролі в статтях не чіпається в жодному разі.")
        return

    def do():
        ensure_schema()
        if arg.isdigit():
            cid = int(arg)
            variants = [r["raw_norm"] for r in bot_db.query(
                "SELECT raw_norm FROM role_variants WHERE canon_id = %s", (cid,))]
            n = bot_db.execute("DELETE FROM role_canon WHERE id = %s", (cid,))
            _reopen(variants)
            return ("canon", n, len(variants))
        rn = role_norm(arg)
        n = bot_db.execute("DELETE FROM role_variants WHERE raw_norm = %s", (rn,))
        bot_db.execute(
            "DELETE FROM role_canon rc WHERE NOT EXISTS "
            "(SELECT 1 FROM role_variants rv WHERE rv.canon_id = rc.id)")
        _reopen([rn])
        return ("variant", n, rn)

    try:
        kind, n, extra = await asyncio.to_thread(do)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалось: {e}")
        return
    if kind == "canon":
        await update.message.reply_text(
            f"🦊 Канон розібрано ({extra} написань повернулись у чергу)."
            if n else "Такого канону немає.")
    else:
        await update.message.reply_text(
            f"🦊 «{extra}» відв'язано від канону, пари з ним повернулись у чергу."
            if n else f"«{extra}» у довіднику не було.")


def _reopen(raw_norms):
    """Скинути вердикт пар, що містять ці написання — щоб бот спитав ще раз."""
    if not raw_norms:
        return
    bot_db.execute(
        "UPDATE role_pairs SET verdict = NULL, decided_by = NULL "
        "WHERE a_norm = ANY(%s) OR b_norm = ANY(%s)",
        (list(raw_norms), list(raw_norms)))
