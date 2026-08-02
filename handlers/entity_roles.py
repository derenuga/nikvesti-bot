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
import math
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

_TR_FROM = ("‐‑–—−"      # тире → дефіс
            "    "      # нерозривні/тонкі пробіли → пробіл
            "«»“”„‟\"'`"   # лапки — геть
            "ʼ’‘")       # …і апострофи
_TR_TO = "-----    "
# 5 тире → '-', 4 пробільні → ' ', решта (лапки, апострофи) видаляється.
# Апострофи додано 02.08 після заміру: «премʼєр-міністр» і «премєр-міністр» це
# одна посада, питати про неї не треба взагалі — у розкладці такі пари сиділи
# в «друкарській різниці» й з'їдали чергу. Зміна нормалізації НЕ безкоштовна:
# по role_norm() побудований індекс і нею ж ключується довідник, тому
# ROLE_NORM_VERSION нижче примушує перебудувати індекс і перекласти ключі.

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
ALTER TABLE role_pairs ADD COLUMN IF NOT EXISTS cls TEXT;
ALTER TABLE role_pairs ADD COLUMN IF NOT EXISTS cls_detail TEXT;
ALTER TABLE role_pairs ADD COLUMN IF NOT EXISTS stake INT;
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

# Піднімати ПРИ КОЖНІЙ зміні role_norm(). Postgres не перебудовує вираженний
# індекс сам, коли IMMUTABLE-функцію підмінили через CREATE OR REPLACE — індекс
# тихо лишається від старої логіки й починає промахуватись. Плюс ключі довідника
# (role_variants.raw_norm, role_pairs.a/b_norm) теж рахувались старою функцією.
ROLE_NORM_VERSION = 2
ROLE_NORM_VERSION_KEY = "role_norm_version"


def _renorm_dictionary():
    """Перекласти ключі довідника на нову нормалізацію. Дублі, що злиплись
    після зміни (саме заради них зміна й робилась), прибираємо — лишається
    перший запис."""
    for row in bot_db.query("SELECT raw_norm, raw_sample FROM role_variants"):
        new = role_norm(row["raw_sample"] or row["raw_norm"])
        if new and new != row["raw_norm"]:
            n = bot_db.execute(
                "UPDATE role_variants SET raw_norm = %s WHERE raw_norm = %s "
                "AND NOT EXISTS (SELECT 1 FROM role_variants x WHERE x.raw_norm = %s)",
                (new, row["raw_norm"], new))
            if not n:
                bot_db.execute("DELETE FROM role_variants WHERE raw_norm = %s",
                               (row["raw_norm"],))
    # Пари перерахує наступний /roles_dedup; невирішені просто скидаємо, а
    # рішення людини переносимо на нові ключі.
    bot_db.execute("DELETE FROM role_pairs WHERE verdict IS NULL")
    for row in bot_db.query("SELECT id, a_norm, b_norm FROM role_pairs"):
        a, b = role_norm(row["a_norm"]), role_norm(row["b_norm"])
        if (a, b) != (row["a_norm"], row["b_norm"]) and a and b:
            if a > b:
                a, b = b, a
            n = bot_db.execute(
                "UPDATE role_pairs SET a_norm = %s, b_norm = %s WHERE id = %s "
                "AND NOT EXISTS (SELECT 1 FROM role_pairs x "
                "                WHERE x.a_norm = %s AND x.b_norm = %s)",
                (a, b, row["id"], a, b))
            if not n:
                bot_db.execute("DELETE FROM role_pairs WHERE id = %s", (row["id"],))


def ensure_schema(force=False):
    """Ідемпотентно: функція нормалізації, таблиці довідника, індекс, view.
    При зміні ROLE_NORM_VERSION додатково перебудовує індекс і ключі."""
    if _schema_done["flag"] and not force:
        return
    stored = bot_db.get_state(ROLE_NORM_VERSION_KEY)
    changed = stored is not None and int(stored) != ROLE_NORM_VERSION
    bot_db.execute(ROLE_NORM_DDL)
    bot_db.execute(ROLES_DDL)
    if changed:
        # Спершу індекс — інакше подальші запити по role_norm() читали б старий.
        bot_db.execute("DROP INDEX IF EXISTS idx_ae_role_norm")
    bot_db.execute(ROLE_INDEX_DDL)
    bot_db.execute(ROLE_VIEW_DDL)
    if changed:
        _renorm_dictionary()
    if stored is None or changed:
        bot_db.set_state(ROLE_NORM_VERSION_KEY, ROLE_NORM_VERSION)
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


# ---------- Класи кандидатів ----------
#
# Замір 02.08 показав, що черга «одна пара — одне питання» при 3757 парах по
# картках і 2091 по ролях не працює: навіть по 10 секунд це десяток годин, а на
# третій сотні людина перестає читати підстави. Тому кандидати спершу
# розкладаються на КЛАСИ — у кожного класу своя ціна рішення:
#
#   permutation — ті самі слова, інший порядок («проспект Центральний» ~
#                 «Центральний проспект»). Дубль механічно, без сумнівів.
#   abbrev      — скорочення/розгортання («ОВА» ~ «обласна військова
#                 адміністрація»). Теж майже завжди дубль — АЛЕ лише за умови,
#                 що решта слів збігається; чому саме так, у _abbrev_match.
#   containment — одне написання ВКЛАДЕНЕ в інше. Найпідступніший клас: сюди
#                 разом падають «голова обласної ВА» ~ «голова МИКОЛАЇВСЬКОЇ
#                 обласної ВА» (та сама посада) і «міський голова» ~ «міський
#                 голова ЛЬВОВА» (різні). Розрізняє їх саме зайве слово, тому
#                 воно віддається окремо — і в картку питання, і в гістограму.
#   word_swap   — та сама конструкція, одне слово замінено («ГОЛОВА …ОВА» ~
#                 «ОЧІЛЬНИК …ОВА»). Теж двоїстий клас — «голова» ~ «депутатка»
#                 тієї самої ради це різні посади, — але питання зводиться до
#                 двох слів, тож рішення займає секунду.
#   typo        — та сама фраза з ПОСИМВОЛЬНОЮ різницею («Сєнкевич»/«Сенкевич»).
#   carrier_only— лексично не схожі взагалі, спільний лише носій. Це і є
#                 «мер» ~ «міський голова», «президент росії» ~ «президент рф»:
#                 найцінніший клас і єдиний, де без людини не обійтись.
#
# Клас — це НЕ вердикт. Він лише каже, скільки коштує рішення і чи можна
# питати класом, а не парою.

CLASS_LABELS = {
    "numbers": "РІЗНІ НОМЕРИ — не злиття",
    "permutation": "ті самі слова, інший порядок",
    "abbrev": "скорочення / розгортання",
    "containment_fill": "уточнення: доповнює назву",
    "containment_geo": "уточнення: ІНШИЙ регіон",
    "containment_disc": "уточнення: розрізняє (колишній/перший/дитяча)",
    "word_swap": "одне слово замінено",
    "typo": "друкарська різниця",
    "carrier_only": "лексично різні, спільний носій",
    "other": "інше",
}

# Класи, які можна закривати ГУРТОМ, і в який бік. Решта — по одній парі.
BULK_MERGE = {"permutation", "abbrev", "typo", "containment_fill"}
BULK_REJECT = {"numbers", "containment_geo", "containment_disc"}

# Корені інших регіонів: у «керівник обласної ВА» ~ «керівник обласної ВА
# ХЕРСОНСЬКОЇ області» зайве слово не доповнює назву, а вказує на іншу область,
# і злиття зліпило б очільників двох ОВА. «Миколаївське» в цей список свідомо
# НЕ входить — для нашого архіву це доповнення, а не уточнення.
OTHER_REGIONS = (
    "херсон", "одес", "київ", "киев", "львів", "львов", "харків", "харьков",
    "дніпро", "днепро", "запоріж", "запорож", "вінниц", "винниц", "полтав",
    "черка", "житомир", "чернігів", "чернигов", "сум", "рівн", "ровен",
    "волин", "тернопіл", "ужгород", "закарпат", "івано", "франків",
    "чернівц", "кіровоград", "кропивниц", "луган", "донец", "крим", "севастопол",
)


def _has_digits(tokens):
    return sorted(t for t in tokens if any(c.isdigit() for c in t))


def _is_other_region(word):
    return any(word.startswith(root) for root in OTHER_REGIONS)

# Слова, які в класі «уточнення» РОЗРІЗНЯЮТЬ, а не доповнюють: «заступник» ≠
# «перший заступник», «мер» ≠ «колишній мер». Решта зайвих слів (україни,
# миколаївської, ради, міської) назву саме доповнюють. Поділ потрібен, щоб
# питати класом: доповнення можна закрити гуртом, розрізнювачі — ні.
DISCRIMINATING = {
    "колишній", "колишня", "екс", "ексміністр", "перший", "перша", "другий",
    "друга", "третій", "тимчасовий", "тимчасово", "виконувач", "виконувачка",
    "во", "заступник", "заступниця", "помічник", "помічниця", "радник",
    "радниця", "майбутній", "новий", "нова", "обраний", "обрана", "почесний",
    "почесна", "старший", "молодший", "головний", "дитяча", "дитячий",
    "не", "без",
}


def _abbrev_match(short_tokens, long_tokens):
    """Скорочення → розгортання: токен-акронім у короткому написанні і
    суцільний відрізок довгого, чиї перші літери його дають.

    Ключова умова — **решта слів має збігтись**. Без неї «стаття 190 КК
    України» ~ «Стаття 310 Кримінального кодексу України» проходить як
    скорочення (КК = Кримінального Кодексу) і масове злиття склеїло б дві
    РІЗНІ статті кодексу. Реальний приклад із заміру 02.08."""
    if not long_tokens or len(long_tokens) < 2:
        return None
    long_set = set(long_tokens)
    for i, t in enumerate(short_tokens):
        if t in long_set or not (2 <= len(t) <= 6):
            continue
        for start in range(len(long_tokens)):
            for end in range(start + 2, len(long_tokens) + 1):
                run = long_tokens[start:end]
                if "".join(w[0] for w in run) != t:
                    continue
                rest_short = short_tokens[:i] + short_tokens[i + 1:]
                rest_long = long_tokens[:start] + long_tokens[end:]
                if sorted(rest_short) == sorted(rest_long):
                    return f"{t} = {' '.join(run)}"
    return None


def classify_pair(a, b, has_carrier=False):
    """Клас пари нормалізованих написань → (ключ, деталь). Деталь — те, що
    людині треба побачити, щоб вирішити за секунду (зайві слова для
    containment). Спільна для обох осей: імена карток і ролі різняться лише
    тим, звідки взялись.

    `has_carrier` є тільки в ролей: коли структурного зв'язку між рядками
    немає взагалі, а носій спільний — це і є «мер» ~ «міський голова», клас
    carrier_only. Для карток такого сигналу немає, і залишок іде в other."""
    ta, tb = _tokens(a), _tokens(b)
    sa, sb = set(ta), set(tb)
    # НОМЕРИ — перед усім іншим. «Стаття 127 КК України» ~ «стаття 366-3 КК
    # України» відрізняються на кілька символів і за будь-яким рядковим
    # критерієм виглядають друкарською помилкою; у замірі 02.08 таких пар було
    # 1324 в одному класі з реальними описками. Це різні статті кодексу, різні
    # ліцеї, різні частини — злиття тут не буває ніколи.
    na, nb = _has_digits(ta), _has_digits(tb)
    if na != nb:
        return "numbers", f"{' '.join(na) or '—'} ≠ {' '.join(nb) or '—'}"
    if sorted(ta) == sorted(tb) and ta != tb:
        return "permutation", None
    short_t, long_t = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    hit = _abbrev_match(short_t, long_t)
    if hit:
        return "abbrev", hit
    if sa < sb or sb < sa:
        extra = sorted((sb - sa) if sa < sb else (sa - sb))
        detail = " ".join(extra)
        if any(_is_other_region(w) for w in extra):
            return "containment_geo", detail
        if set(extra) & DISCRIMINATING:
            return "containment_disc", detail
        return "containment_fill", detail
    import difflib
    # одне слово замінено: та сама конструкція, різниця рівно в одному токені
    if len(ta) == len(tb):
        diff = [(x, y) for x, y in zip(ta, tb) if x != y]
        if len(diff) == 1:
            x, y = diff[0]
            # «Сєнкевич»/«Сенкевич» теж різняться одним токеном, але це не
            # заміна слова, а описка — інакше друкарський клас спорожнів би
            if difflib.SequenceMatcher(None, x, y).ratio() < 0.8:
                return "word_swap", f"{x} → {y}"
            return "typo", f"{x} / {y}"
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return "typo", None
    return ("carrier_only" if has_carrier else "other"), None


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
            # Єдиний сигнал, що означає саме «та сама посада», а не «схожі
            # рядки». Тому важить більше за будь-яку лексичну комбінацію:
            # інакше пара «депутатка міської ради» ~ «...ради Львова», у якої
            # спільного носія немає взагалі, ставала б урівень із «мер» ~
            # «міський голова».
            score += 4
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
        nested = len(short) >= 2 and short < long_
        cls, detail = classify_pair(a, b, has_carrier=bool(sh))
        if cls == "permutation":
            # ті самі слова в іншому порядку — це дубль механічно, без сумнівів
            score += 3
            sig.append("ті самі слова, інший порядок")
        elif cls == "abbrev":
            score += 2
            sig.append(f"скорочення: {detail}")
        elif cls in ("containment_geo", "containment_disc"):
            # Уточнення, яке РОЗРІЗНЯЄ (інший регіон, «перший», «колишній»).
            # Балів не додаємо взагалі — питати про це варто востаннє.
            sig.append(f"уточнення, різниця: {detail}")
        elif cls == "word_swap":
            # «голова» → «очільник» те саме, «голова» → «депутатка» ні —
            # клас двоїстий, тому бал маленький, а різниця йде в картку.
            score += 1
            sig.append(f"одне слово замінено: {detail}")
        elif cls == "numbers":
            sig.append(f"різні номери: {detail}")
        elif nested:
            # Вкладеність СВІДОМО важить мало (було 2, стало 1): на реальних
            # даних вона масово тягне нагору «уточнення» — «міський голова» ~
            # «міський голова Львова», — які зливати не можна. Сигналом лишається
            # (спорідненість справжня), але чергу більше не забиває; що саме
            # відрізняє пару, видно з класу в картці питання.
            score += 1
            sig.append(f"вкладене, різниця: {detail}" if detail
                       else "одне написання вкладене в інше")
        common_rare = sorted(
            t for t in (ta & tb) if len(t) >= 5 and df.get(t, 0) <= rare_cap)
        if common_rare:
            score += 1
            sig.append("спільне слово: " + ", ".join(common_rare[:2]))
        # рідкісне слово САМЕ ПО СОБІ кандидатом не робить: «департаменту»
        # ділять десятки різних посад
        if not (sh or sim is not None or nested):
            continue
        if score < SCORE_MIN:
            continue
        # Скільки зв'язків на кону. Питати про пару з 2345 зв'язками треба
        # раніше, ніж про пару з вісьмома, навіть коли сигнали в них однакові —
        # інакше вечір іде на посади, які трапились двічі.
        stake = min(roles[a]["links"], roles[b]["links"])
        score += min(2.0, math.log10(max(stake, 1)))
        rows.append((a, b, round(score, 2), " · ".join(sig), cls, detail, stake))
    rows.sort(key=lambda r: (-r[2], -r[6]))
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
    for a, b, score, sig, cls, detail, stake in rows:
        n = bot_db.execute(
            "INSERT INTO role_pairs (a_norm, b_norm, score, signals, cls, "
            "  cls_detail, stake, updated) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (a_norm, b_norm) DO UPDATE SET "
            "  score = EXCLUDED.score, signals = EXCLUDED.signals, "
            "  cls = EXCLUDED.cls, cls_detail = EXCLUDED.cls_detail, "
            "  stake = EXCLUDED.stake, updated = EXCLUDED.updated "
            "WHERE role_pairs.verdict IS NULL",
            (a, b, score, sig, cls, detail, stake, now))
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

# Орган посади: ДВА джерела кандидатів, і порядок між ними принциповий.
#
# Спершу було лише співпояв — «яка організація найчастіше трапляється в статтях
# із цією роллю». На Галущенку це дало НАБУ, САП і «Квартал 95»: він міністр
# юстиції, але пишуть про нього в матеріалах про корупційне провадження. Тобто
# співпояв ловить не афіліацію, а СЮЖЕТ, і для національних політиків він
# систематично бреше.
#
# Тому головне джерело тепер лексичне: орган, чия НАЗВА перегукується з назвою
# посади («міністр юстиції» → «Міністерство юстиції України», «голова
# Миколаївської ОВА» → «Миколаївська обласна військова адміністрація»). Це
# майже завжди правильна відповідь. Співпояв лишається другим — він рятує там,
# де назва посади органу не містить («мер Миколаєва» → «Миколаївська міська
# рада»), — але в кнопці підписано, на якій підставі кандидат запропонований,
# щоб «48 спільних статей» не виглядало як «це його орган».

ORG_BY_NAME_SQL = """
SELECT e.id, coalesce(e.name_ua, e.name_ru) AS name,
       similarity(lower(coalesce(e.name_ua, e.name_ru)), %s) AS sim
FROM entities e
WHERE e.kind = 'org' AND coalesce(e.name_ua, e.name_ru) IS NOT NULL
  AND similarity(lower(coalesce(e.name_ua, e.name_ru)), %s) >= 0.25
ORDER BY sim DESC, coalesce(e.mentions, 0) DESC
LIMIT 3
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


def org_candidates(canon_id):
    """Кандидати в орган канону: спершу за назвою посади, потім за співпоявою.
    Повертає [{id, name, basis}] — basis іде в кнопку, щоб було видно, ЧОМУ
    бот це пропонує."""
    ensure_schema()
    rows = bot_db.query(
        "SELECT rc.canon, array_agg(rv.raw_norm) AS variants "
        "FROM role_canon rc JOIN role_variants rv ON rv.canon_id = rc.id "
        "WHERE rc.id = %s GROUP BY rc.canon", (canon_id,))
    if not rows:
        return []
    canon, variants = rows[0]["canon"], rows[0]["variants"]
    out, seen = [], set()
    try:
        for r in bot_db.query(ORG_BY_NAME_SQL, (role_norm(canon), role_norm(canon))):
            out.append({"id": r["id"], "name": r["name"],
                        "basis": "за назвою посади"})
            seen.add(r["id"])
    except Exception as e:
        print(f"entity_roles: лексичний пошук органу пропущено — {e}")
    for r in bot_db.query(ORG_CANDIDATES_SQL, (variants,)):
        if r["id"] in seen:
            continue
        out.append({"id": r["id"], "name": r["name"],
                    "basis": f"{r['c']} спільних статей"})
    return out[:4]


def org_markup(canon_id, cands):
    rows = [[InlineKeyboardButton(f"{c['name']} — {c['basis']}",
                                  callback_data=f"rdo:{canon_id}:{c['id']}")]
            for c in cands]
    rows.append([InlineKeyboardButton("↷ Не зараз", callback_data=f"rdo:{canon_id}:0")])
    return InlineKeyboardMarkup(rows)

NEXT_PAIR_SQL = """
SELECT p.id, p.a_norm, p.b_norm, p.score, p.signals, p.cls, p.cls_detail
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
    # Носій із ЛІЧИЛЬНИКОМ: «Кім (314), Прокудін (1)» одразу показує, що
    # другий — випадкова помилка витягу, а не другий носій посади. Без числа
    # такий викид виглядав би як рівноправний факт і міг збити рішення.
    card["carriers"] = [f"{r['name']} ({r['c']})"
                        for r in bot_db.query(ROLE_CARRIERS_SQL, (rn,)) if r["name"]]
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

    # Клас окремим рядком: він вирішує питання швидше за підставу. «уточнення,
    # різниця: львова» — це миттєве «ні», а не читання балів.
    cls = CLASS_LABELS.get(p.get("cls") or "", "")
    if cls and p.get("cls_detail"):
        cls = f"{cls} — «{p['cls_detail']}»"
    return ("🦊 Це та сама посада?\n\n"
            + block("А", p["a"]) + "\n"
            + block("Б", p["b"]) + "\n\n"
            + (f"Клас: {cls}\n" if cls else "")
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
        orgs = [] if has_org else org_candidates(canon_id)
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
        text += ("\n\nЧий це орган? (дасть афіліацію людина↔організація; "
                 "змінити потім — /roles_org)")
        await query.edit_message_text(text, reply_markup=org_markup(canon_id, orgs))
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


# ---------- /roles_outliers ----------
#
# Шукає РІДКІСНИХ носіїв сталої посади. Привід — дефект, знайдений 02.08: у
# статті про Миколаївщину в беку стоїть «у Херсоні теж був обстріл, голова ОВА
# Прокудін повідомив…», і витяг ДОПОВНИВ роль регіоном статті, повісивши на
# голову Херсонської ОВА миколаївську посаду.
#
# **Але рідкісний носій — це НЕ синонім помилки, і перша версія звіту тут
# брехала заголовком.** У 17-річному архіві попередник на посаді абсолютно
# нормальний: Чайка мер Миколаєва 2001–2013, Сєнкевич з 2015; Порошенко,
# Янукович, Обама, Кеннеді — усі законні «президенти». Гнати їх у
# /entity_resync означало б платити за перечит правильних даних.
#
# Тому звіт РОЗКЛАДАЄ знахідки, а не звинувачує:
#   • схоже ім'я на головного носія → це дубль КАРТКИ («Сєнкевич» при
#     «Олександр Сєнкевич», «Дональд Дж. Трамп» при «Дональд Трамп») або
#     однофамілець (пастка §2) — вісь карток, ролі ні до чого;
#   • інша людина → або попередник (норма), або помилка витягу. Розрізняє їх
#     людина, і для цього поруч друкуються ПЕРІОДИ обох: у попередника свій
#     відрізок часу, у помилки — одна згадка посеред періоду головного носія.
#
# Правити сирий role_at_time бот не буде ніколи: він факт статті, і якщо винен
# текст — виправляти треба статтю, а потім перечитати її.

OUTLIERS_SQL = """
WITH r AS (
    SELECT role_norm(ae.role_at_time) AS rn, ae.entity_id, count(*) AS c,
           to_char(to_timestamp(min(a.published)), 'YYYY-MM') AS lo,
           to_char(to_timestamp(max(a.published)), 'YYYY-MM') AS hi
    FROM article_entities ae
    JOIN articles a ON a.id = ae.article_id
    WHERE ae.role_at_time IS NOT NULL AND role_norm(ae.role_at_time) IS NOT NULL
    GROUP BY 1, 2
), tot AS (
    SELECT rn, max(c) AS top, count(*) AS carriers FROM r GROUP BY rn
), main AS (
    SELECT DISTINCT ON (rn) rn, entity_id, c, lo, hi FROM r ORDER BY rn, c DESC
)
SELECT r.rn, coalesce(e.name_ua, e.name_ru) AS name, r.c, r.lo, r.hi,
       tot.top, tot.carriers,
       coalesce(m.name_ua, m.name_ru) AS main_name,
       main.lo AS main_lo, main.hi AS main_hi,
       (SELECT array_agg(ae2.article_id) FROM (
            SELECT ae3.article_id FROM article_entities ae3
            WHERE ae3.entity_id = r.entity_id
              AND role_norm(ae3.role_at_time) = r.rn
            ORDER BY ae3.article_id DESC LIMIT 3) ae2) AS articles
FROM r
JOIN tot ON tot.rn = r.rn
JOIN main ON main.rn = r.rn
JOIN entities e ON e.id = r.entity_id
JOIN entities m ON m.id = main.entity_id
WHERE tot.top >= %s AND r.c * %s <= tot.top AND r.entity_id <> main.entity_id
ORDER BY tot.top DESC, r.c
LIMIT %s
"""

# Поріг «усталеної» посади і в скільки разів носій має бути рідшим за головного,
# щоб вважатись викидом. 20× — щоб не чіпати реальну зміну посадовця (у неї
# зазвичай десятки згадок), а ловити саме одноразові приписування.
OUTLIER_MIN_TOP = 20
OUTLIER_RATIO = 20


async def roles_outliers_handler(update, context):
    """/roles_outliers [N] — де роль приписана явно не тому носієві."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    args = context.args or []
    try:
        n = int(args[0]) if args else 20
    except ValueError:
        n = 20
    n = min(max(n, 1), 100)
    msg = await update.message.reply_text("🦊 Шукаю ролі, приписані не тому носієві…")

    def build():
        ensure_schema()
        return bot_db.query(OUTLIERS_SQL, (OUTLIER_MIN_TOP, OUTLIER_RATIO, n))

    try:
        rows = await asyncio.to_thread(build)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {type(e).__name__}: {e}")
        return
    if not rows:
        await msg.edit_text(
            "🦊 Рідкісних носіїв не знайшов: у кожної сталої посади носії "
            "пропорційні.")
        return

    import difflib

    dup, other = [], []
    for r in rows:
        same = difflib.SequenceMatcher(
            None, (r["name"] or "").lower(), (r["main_name"] or "").lower()).ratio()
        (dup if same >= 0.5 else other).append(r)

    lines = [f"🦊 Рідкісні носії сталої посади — {len(rows)}\n",
             "Рідкісний ≠ помилковий: у 17-річному архіві попередник на посаді "
             "це норма (Чайка мер 2001–2013, Сєнкевич з 2015). Тому дивись на "
             "періоди, а не на числа.\n"]

    if dup:
        lines.append(f"━ Схоже ім'я — дубль КАРТКИ або однофамілець ({len(dup)}) ━")
        lines.append("Це вісь карток, ролі ні до чого: одна людина під двома "
                     "картками. Але обережно — так само виглядає й однофамілець.")
        for r in dup:
            lines.append(f"«{r['rn']}» · {r['name']} ({r['c']}) ~ "
                         f"{r['main_name']} ({r['top']})")
        lines.append("")

    ids = []
    if other:
        lines.append(f"━ Інша людина — попередник або помилка витягу ({len(other)}) ━")
        for r in other:
            arts = list(r["articles"] or [])
            ids.extend(arts)
            lines.append(
                f"«{r['rn']}»\n   {r['name']} ({r['c']}) {r['lo']}…{r['hi']}"
                f"  ·  при {r['main_name']} ({r['top']}) {r['main_lo']}…{r['main_hi']}"
                f"\n   статті: " + ", ".join(str(a) for a in arts))
        lines.append(
            "\nПопередник має СВІЙ відрізок часу — його не чіпаємо. "
            "Помилка витягу виглядає як одна-дві згадки посеред періоду "
            "головного носія.")
        lines.append("Подивитись текст: /nora_article <id>")
        lines.append("Полагодити ЛИШЕ помилкові (спершу правимо статтю на "
                     "сайті, якщо винен текст):\n/entity_resync "
                     + " ".join(str(i) for i in ids[:20]))
    await msg.edit_text("\n".join(lines)[:4000])


# ---------- /roles_org ----------

async def roles_org_handler(update, context):
    """/roles_org <id канону> [id сутності|0] — переглянути/змінити/зняти орган.

    Питання про орган ставиться один раз, одразу після зведення, і легко
    промахнутись: у першій же версії співпояв запропонував Галущенку НАБУ й
    «Квартал 95» замість Мін'юсту. Тому рішення має бути виправним без деплою."""
    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Формат: /roles_org <id канону> — показати кандидатів у орган\n"
            "        /roles_org <id канону> <id сутності> — поставити\n"
            "        /roles_org <id канону> 0 — зняти\n"
            "id канону підкаже /roles_canon")
        return
    canon_id = int(args[0])

    if len(args) > 1 and args[1].lstrip("-").isdigit():
        org_id = int(args[1])

        def do():
            ensure_schema()
            n = bot_db.execute(
                "UPDATE role_canon SET org_entity_id = %s WHERE id = %s",
                (org_id or None, canon_id))
            name = None
            if org_id:
                r = bot_db.query(
                    "SELECT coalesce(name_ua, name_ru) AS name FROM entities "
                    "WHERE id = %s", (org_id,))
                name = r[0]["name"] if r else None
            return n, name

        n, name = await asyncio.to_thread(do)
        if not n:
            await update.message.reply_text(f"Канону {canon_id} немає.")
        elif not org_id:
            await update.message.reply_text(f"🦊 Орган канону {canon_id} знято.")
        else:
            await update.message.reply_text(
                f"🏛 Канон {canon_id} → {name or org_id}."
                if name else f"🏛 Канон {canon_id} → сутність {org_id} "
                             f"(такої сутності в норі немає — перевір id).")
        return

    def build():
        ensure_schema()
        rows = bot_db.query(
            "SELECT rc.canon, coalesce(o.name_ua, o.name_ru) AS org "
            "FROM role_canon rc LEFT JOIN entities o ON o.id = rc.org_entity_id "
            "WHERE rc.id = %s", (canon_id,))
        return (rows[0] if rows else None), org_candidates(canon_id)

    row, cands = await asyncio.to_thread(build)
    if not row:
        await update.message.reply_text(f"Канону {canon_id} немає — /roles_canon.")
        return
    head = f"🦊 «{row['canon']}»\nЗараз орган: {row['org'] or '— не вказано'}"
    if not cands:
        await update.message.reply_text(
            head + "\n\nКандидатів бот не знайшов. Постав вручну: "
                   "/roles_org <id канону> <id сутності>")
        return
    await update.message.reply_text(head + "\n\nЧий це орган?",
                                    reply_markup=org_markup(canon_id, cands))


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


# ---------- /roles_bulk: підтвердження КЛАСОМ, а не парою ----------
#
# Замір 02.08: 2 109 пар-кандидатів по ролях. По одній це десяток годин, і на
# третій сотні людина перестає читати підстави — тобто кнопки стають гіршими
# за їх відсутність. Тому клас закривається одним рішенням, а по одній
# лишаються тільки ті класи, де рішення справді різні для кожної пари
# (carrier_only, word_swap, other).
#
# Гурт НІКОЛИ не діє без перегляду: спершу показуємо десяток прикладів класу,
# і лише потім кнопка. Це та сама вимога «автозлиття без людини не робити»,
# просто людина відповідає за клас, а не за рядок.

CLASS_PAIRS_SQL = """
SELECT p.id, p.a_norm, p.b_norm, p.cls_detail, p.stake
FROM role_pairs p
LEFT JOIN role_variants va ON va.raw_norm = p.a_norm
LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm
WHERE p.verdict IS NULL AND p.cls = %s
  AND (va.canon_id IS NULL OR vb.canon_id IS NULL OR va.canon_id <> vb.canon_id)
ORDER BY p.stake DESC NULLS LAST, p.score DESC
"""

CLASS_COUNTS_SQL = """
SELECT p.cls, count(*) AS n
FROM role_pairs p
LEFT JOIN role_variants va ON va.raw_norm = p.a_norm
LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm
WHERE p.verdict IS NULL
  AND (va.canon_id IS NULL OR vb.canon_id IS NULL OR va.canon_id <> vb.canon_id)
GROUP BY p.cls ORDER BY n DESC
"""


def class_counts():
    ensure_schema()
    return [(r["cls"] or "other", r["n"]) for r in bot_db.query(CLASS_COUNTS_SQL)]


def _samples(norms):
    """Живі написання для набору нормалізованих ключів — одним запитом, бо
    гурт може чіпати сотні пар, а походи по одному вбили б його."""
    if not norms:
        return {}
    rows = bot_db.query(
        "SELECT role_norm(role_at_time) AS rn, "
        "       mode() WITHIN GROUP (ORDER BY role_at_time) AS sample "
        "FROM article_entities WHERE role_norm(role_at_time) = ANY(%s) "
        "GROUP BY 1", (list(norms),))
    return {r["rn"]: r["sample"] for r in rows}


def bulk_apply(cls, verdict, decided_by=None):
    """Закрити весь клас одним рішенням. verdict: 'same' (звести) або
    'different' (більше не питати). Повертає скільки пар закрито."""
    ensure_schema()
    with bot_db.session():
        rows = bot_db.query(CLASS_PAIRS_SQL, (cls,))
        if not rows:
            return 0
        if verdict != "same":
            bot_db.execute(
                "UPDATE role_pairs SET verdict = 'different', decided_by = %s, "
                "updated = %s WHERE id = ANY(%s)",
                (decided_by, int(time.time()), [r["id"] for r in rows]))
            return len(rows)
        norms = {r["a_norm"] for r in rows} | {r["b_norm"] for r in rows}
        samples = _samples(norms)
        done = 0
        for r in rows:
            try:
                merge_roles(r["a_norm"], r["b_norm"],
                            samples.get(r["a_norm"]), samples.get(r["b_norm"]),
                            decided_by)
                set_verdict(r["id"], "same", decided_by)
                done += 1
            except Exception as e:
                print(f"roles_bulk: пара {r['id']} не звелась — {e}")
        return done


def _bulk_menu_markup(counts):
    rows = []
    for cls, n in counts:
        mark = "✅" if cls in BULK_MERGE else ("❌" if cls in BULK_REJECT else "·")
        rows.append([InlineKeyboardButton(
            f"{mark} {CLASS_LABELS.get(cls, cls)} — {n}",
            callback_data=f"rbc:{cls}")])
    return InlineKeyboardMarkup(rows) if rows else None


def _bulk_menu_text(counts):
    if not counts:
        return ("🦊 Черга порожня — спершу /roles_dedup прожене детектор.")
    total = sum(n for _, n in counts)
    return (f"🦊 Класи в черзі — {total} пар\n\n"
            f"✅ — клас, який зазвичай закривають гуртом «так»\n"
            f"❌ — гуртом «ні» (номери, інший регіон, розрізнювачі)\n"
            f"· — тільки по одній: рішення різне для кожної пари\n\n"
            f"Тапни клас, щоб побачити приклади.")


async def roles_bulk_handler(update, context):
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Рахую класи в черзі…")
    try:
        counts = await asyncio.to_thread(class_counts)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {type(e).__name__}: {e}")
        return
    await msg.edit_text(_bulk_menu_text(counts), reply_markup=_bulk_menu_markup(counts))


async def roles_bulk_callback(update, context):
    """rbc:<клас> — показати приклади; rbm/rbr:<клас> — закрити клас гуртом."""
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        action, cls = query.data.split(":", 1)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()
    who = query.from_user.full_name if query.from_user else None

    if action == "rbc" and cls == "*":
        counts = await asyncio.to_thread(class_counts)
        await query.edit_message_text(_bulk_menu_text(counts),
                                      reply_markup=_bulk_menu_markup(counts))
        return

    if action == "rbc":
        rows = await bot_db.aquery(CLASS_PAIRS_SQL, (cls,))
        if not rows:
            await query.edit_message_text("Цей клас уже порожній.")
            return
        lines = [f"🦊 {CLASS_LABELS.get(cls, cls)} — {len(rows)} пар\n"]
        for r in rows[:10]:
            tail = f"  ({r['cls_detail']})" if r["cls_detail"] else ""
            lines.append(f"· «{r['a_norm']}» ~ «{r['b_norm']}»{tail}")
        if len(rows) > 10:
            lines.append(f"…і ще {len(rows) - 10}")
        hint = ("Зазвичай тут «так»." if cls in BULK_MERGE else
                "Зазвичай тут «ні»." if cls in BULK_REJECT else
                "Клас двоїстий — гуртом краще не чіпати, є /roles_dedup по одній.")
        lines.append("\n" + hint)
        kb = [[InlineKeyboardButton(f"✅ Звести всі {len(rows)}",
                                    callback_data=f"rbm:{cls}"),
               InlineKeyboardButton(f"❌ Відхилити всі {len(rows)}",
                                    callback_data=f"rbr:{cls}")],
              [InlineKeyboardButton("← Класи", callback_data="rbc:*")]]
        await query.edit_message_text("\n".join(lines)[:4000],
                                      reply_markup=InlineKeyboardMarkup(kb))
        return

    if action not in ("rbm", "rbr"):
        return
    verdict = "same" if action == "rbm" else "different"
    await query.edit_message_text(
        f"🦊 Обробляю клас «{CLASS_LABELS.get(cls, cls)}»…")
    try:
        n = await asyncio.to_thread(bulk_apply, cls, verdict, who)
    except Exception as e:
        await query.edit_message_text(f"❌ Гурт не пройшов: {type(e).__name__}: {e}")
        return
    what = ("зведено в канони" if verdict == "same"
            else "позначено різними — більше не спитаю")
    counts = await asyncio.to_thread(class_counts)
    await query.edit_message_text(
        f"🦊 Клас «{CLASS_LABELS.get(cls, cls)}»: {n} пар {what}.\n"
        + ("Відкат — /roles_forget <написання> або /roles_canon для перегляду.\n\n"
           if verdict == "same" else "\n")
        + _bulk_menu_text(counts),
        reply_markup=_bulk_menu_markup(counts))


def _reopen(raw_norms):
    """Скинути вердикт пар, що містять ці написання — щоб бот спитав ще раз."""
    if not raw_norms:
        return
    bot_db.execute(
        "UPDATE role_pairs SET verdict = NULL, decided_by = NULL "
        "WHERE a_norm = ANY(%s) OR b_norm = ANY(%s)",
        (list(raw_norms), list(raw_norms)))
