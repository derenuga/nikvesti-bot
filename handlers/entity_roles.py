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
-- Зв'язки кожної сторони окремо. Потрібні саме поштучно, а не сумою: пара
-- питається в порядку РЕАЛЬНОГО приросту покриття, а він рахує лише ті
-- сторони, які ще не під каноном (див. NEXT_PAIR_SQL).
ALTER TABLE role_pairs ADD COLUMN IF NOT EXISTS links_a INT;
ALTER TABLE role_pairs ADD COLUMN IF NOT EXISTS links_b INT;
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
SELECT role_norm(ae.role_at_time) AS rn, ae.entity_id, count(*) AS c,
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
    "status_diff": "різний РАНГ або статус (віце / заступник / в.о. / екс)",
    "place_swap": "ІНШЕ МІСЦЕ — замінено топонім",
    "level_swap": "ІНШИЙ РІВЕНЬ (обласна / міська / районна)",
    "permutation": "ті самі слова, інший порядок",
    "function_words": "різниця лише у службових словах (кома / «та»)",
    "abbrev": "скорочення / розгортання",
    "containment_fill": "уточнення: доповнює назву",
    "containment_geo": "уточнення: ІНШЕ МІСЦЕ (країна/область/місто)",
    "containment_own": "уточнення: наша країна/область — повніша назва",
    "containment_disc": "уточнення: розрізняє (колишній/перший/дитяча)",
    "word_swap": "одне слово замінено",
    "typo": "друкарська різниця",
    "carrier_only": "лексично різні, спільний носій",
    "other": "інше",
}

# Класи, які можна закривати ГУРТОМ, і в який бік. Решта — по одній парі.
#
# `containment_fill` звідси ПРИБРАНО (питання Олега 02.08, і воно влучило):
# «прем'єр-міністр» ~ «прем'єр-міністр Італії» — зайве слово це топонім, і
# масове злиття зробило б з італійського прем'єра українського. Топоніми тепер
# ловляться списком місць із нори, але висновок ширший: у класі «вкладеність»
# зайве слово МОЖЕ виявитись розрізнювачем завжди («управління» ~ «управління
# розвитку» — це різні управління), тому гуртом його не закривають узагалі.
# Голу форму посади («прем'єр-міністр» без країни) взагалі не можна злити ні з
# чим конкретним: у кожній статті вона означає свою людину.
BULK_MERGE = {"permutation", "abbrev", "typo", "function_words"}
BULK_REJECT = {"numbers", "status_diff", "place_swap", "level_swap",
               "containment_geo", "containment_disc"}

# Корені топонімів: у «керівник обласної ВА» ~ «керівник обласної ВА
# ХЕРСОНСЬКОЇ області» зайве слово не доповнює назву, а вказує на інше місце,
# і злиття зліпило б очільників двох ОВА.
#
# Список — ПІДЛОГА, а не джерело істини: головне джерело це топоніми з нори
# (`_place_prefixes`). Але покладатись лише на нору не можна — якщо потрібного
# місця в ній ще немає, пара мовчки поїде в «доповнює назву». Тому країни, які
# постійно трапляються в новинах, зашиті тут. Українські області теж — навіть
# «миколаїв», бо голу форму посади не можна зливати НІ з чим конкретним:
# у кожній статті вона означає свою людину.
# Топонім зводиться до КОДУ КРАЇНИ/ОБЛАСТІ, а не порівнюється як слово.
# Інакше «лідер РФ» ~ «президент РОСІЇ» читається як різні місця, хоча це одна
# країна в трьох написаннях — і 14 пар класу «інше місце» виявились такими,
# крім однієї справжньої («Великої Британії» ~ «РФ»).
#
# Це ПІДЛОГА, а не повний довідник: широкий список місць із нори в
# класифікації не бере участі взагалі (він містить «Обласну лікарню» і
# «Міський парк», і на префіксах з ними збігаються слова посади).
REGION_CODES = (
    ("ua", ("україн", "украин")),
    ("mk", ("микола", "николае")),
    ("kherson", ("херсон",)),
    ("odesa", ("одес",)),
    # Родовий відмінок міняє і→о («Києва», «Львова», «Харкова»), і без
    # цих коренів топонім не впізнавався саме там, де він найчастіше й
    # стоїть у назві посади: «міський голова Києва» вважався шаблоном
    # без установи.
    ("kyiv", ("київ", "киев", "києв")),
    ("lviv", ("львів", "львов")),
    ("kharkiv", ("харків", "харьков", "харков")),
    ("dnipro", ("дніпро", "днепро", "дніпропетров")),
    ("zapor", ("запоріж", "запорож")),
    ("vinn", ("вінниц", "винниц")),
    ("poltava", ("полтав",)),
    ("cherkasy", ("черка",)),
    ("zhytomyr", ("житомир",)),
    ("chernihiv", ("чернігів", "чернигов", "чернігов")),
    ("sumy", ("сум",)),
    ("rivne", ("рівн", "ровен")),
    ("volyn", ("волин", "луцьк")),
    ("ternopil", ("тернопіл",)),
    ("zakarp", ("ужгород", "закарпат")),
    ("ivfr", ("івано", "франків")),
    ("chernivtsi", ("чернівц",)),
    ("kropyv", ("кіровоград", "кропивниц")),
    ("luhansk", ("луган",)),
    ("donetsk", ("донец",)),
    ("crimea", ("крим", "севастопол")),
    ("ru", ("росі", "росс", "рф", "російськ", "россий", "кремл")),
    ("us", ("сша", "америк", "американ")),
    ("pl", ("польщ", "польськ", "польск")),
    ("it", ("італі", "итал", "італійськ")),
    ("de", ("німеччин", "німецьк", "герман")),
    ("fr", ("франці", "французьк", "франц")),
    ("uk_gb", ("британ", "англі", "англій")),
    ("tr", ("туреччин", "турецьк", "турц")),
    ("hu", ("угорщин", "угорськ", "венгр")),
    ("ro", ("румун",)),
    ("md", ("молдов",)),
    ("by", ("білорус", "белорус")),
    ("il", ("ізраїл", "израил")),
    ("cn", ("кита", "китайськ")),
    ("jp", ("японі", "японськ", "япон")),
    ("ca", ("канад",)),
    ("es", ("іспан", "испан")),
    ("ge", ("грузі", "груз")),
    ("lt", ("литв",)),
    ("lv", ("латві",)),
    ("ee", ("естон",)),
    ("sk", ("словач",)),
    ("cz", ("чехі", "чеськ")),
    ("bg", ("болгар",)),
    ("at", ("австрі", "австрій")),
    ("ch", ("швейцар",)),
    ("se", ("швеці", "шведськ")),
    ("no", ("норвег",)),
    ("fi", ("фінлянд", "финлянд")),
    ("dk", ("дані", "данськ")),
    ("nl", ("нідерланд", "нидерланд")),
    ("be", ("бельг",)),
    ("gr", ("грец",)),
    ("eg", ("єгипт", "египт")),
    ("in", ("інді", "инди")),
    ("iq", ("ірак",)),
    ("ir", ("іран",)),
    ("sy", ("сирі",)),
    ("af", ("афган",)),
    ("kr", ("корей",)),
    ("vn", ("вєтнам", "вьетнам")),
    ("br", ("бразил",)),
    ("ar", ("аргентин",)),
    ("mx", ("мексик",)),
    ("au", ("австрал",)),
    # «європейськ» СВІДОМО немає: «Європейська солідарність» це партія, і на
    # ній детектор називав місцем назву політсили («депутат від ЄВРОПЕЙСЬКОЇ
    # солідарності»). Ціна пропуску «Європейського Союзу» в назві посади
    # менша за ціну хибного «інше місце».
    ("eu", ("євросоюз", "евросоюз")),
    ("nato", ("нато",)),
)
OTHER_REGIONS = tuple(r for _c, roots in REGION_CODES for r in roots)
OUR_CODES = {"ua", "mk"}


def _region_code(word):
    for code, roots in REGION_CODES:
        if any(word.startswith(r) for r in roots):
            return code
    return None



# Маркери СТАТУСУ посади. «в.о. мера Миколаєва» — це не мер: якщо звести їх в
# один канон, довідник на питання «хто був мером» відповість «Сєнкевич і
# Гранатуров», і це неправда. Через токенізацію «в.о.» розпадається на «в» і
# «о» (обидва по одному символу), тому в DISCRIMINATING воно не ловилось, і
# пара падала в «лексично різні, спільний носій» — тобто в найцінніший клас,
# де людина схильна тиснути «так».
ACTING_RE = re.compile(
    r"\bв\.?\s?о\.?\s|виконувач|тимчасов|\bекс\b|екс-|колишн|"
    r"новообран|майбутн|обраний")


def _status_marker(text):
    m = ACTING_RE.search(text or "")
    return m.group(0).strip() if m else None


# Службові слова. «депутат міськради ТА голова ОВА» і «депутат міськради,
# голова ОВА» — це той самий рядок із комою замість сполучника, а не дві різні
# посади. Такі пари безпечно зводити гуртом: семантики в різниці немає.
FUNCTION_WORDS = {"та", "і", "й", "а", "з", "із", "зі", "у", "в", "на", "по",
                  "до", "від", "при", "для", "про", "the", "of"}


def _has_digits(tokens):
    return sorted(t for t in tokens if any(c.isdigit() for c in t))


# Наші місця. «міністр оборони» ~ «міністр оборони УКРАЇНИ» — це та сама
# посада з повнішою назвою, а «прем'єр-міністр» ~ «прем'єр-міністр ІТАЛІЇ» —
# різні. Обидва випадки топонімні, тому одного прапорця «місце» замало:
# рішення протилежне, і клас має це казати.
OUR_PLACES = ("україн", "украин", "микола", "николае")


def _is_place_word(word):
    """Чи є слово топонімом узагалі (байдуже, нашим чи чужим)."""
    if any(word.startswith(root) for root in OTHER_REGIONS):
        return True
    return word[:5] in _place_prefixes()


def _is_own_place(word):
    return _region_code(word) in OUR_CODES


def _is_region_word(word):
    """СТРОГА перевірка топоніма — лише за курованими коренями регіонів і
    країн, без списку місць із нори.

    Широка перевірка (`_is_place_word`) бере топоніми з `entities` kind='place',
    а там лежить усе підряд: «Голованівськ», «Міський парк», «Обласна
    лікарня». П'ятисимвольний префікс від них збігається зі словами ПОСАДИ —
    «голова», «міський», «обласної», — і порівняння наборів топонімів видало
    686 пар, серед них «мер Миколаєва» ~ «міський голова Миколаєва», тобто
    найцінніше злиття в системі, у класі «зазвичай ні».

    Тому там, де порівнюються НАБОРИ слів (а не одне зайве слово), беремо
    тільки куровані корені: помилка тут коштує назавжди закритої пари."""
    return _region_code(word) is not None


def _is_other_region(word):
    """Чи вказує зайве слово на ЧУЖЕ місце.

    Курований список покривав лише українські області, і на ньому я вже
    провалився: «прем'єр-міністр» ~ «прем'єр-міністр ІТАЛІЇ» потрапляли в
    «доповнює назву» й пішли б у масове злиття. Тому топоніми беруться з самої
    нори (`entities` kind='place') — там і країни, і чужі міста.

    Але саме «чуже» тут ключове: «міністр оборони» ~ «міністр оборони
    УКРАЇНИ» — це та сама посада з повнішою назвою (той самий Умєров), а не
    інше місце. Наші топоніми йдуть в окремий клас, бо рішення протилежне."""
    # СТРОГО, за курованими коренями: широкий список місць із нори містить
    # «Обласну лікарню» й «Міський парк», і на п'ятисимвольному префіксі слова
    # ПОСАДИ («обласної», «міський», «голова») починають виглядати топонімами.
    return _is_region_word(word) and not _is_own_place(word)

# Рівень органу. «обласна» ВА і «міська» ВА — різні установи, і заміна одного
# слова на інше НЕ робить із них одну посаду. Разом із топонімами це те, через
# що канон №7 зібрав шість областей, три міські ВА і одну районну: обидві
# заміни падали в клас «одне слово замінено», який позначений «по одній», і
# людина на сотому питанні тиснула «так».
# Рівень зводиться до КОДУ, а не порівнюється як слово: інакше «ОВА» і
# «обласної військової адміністрації» виглядають як різні рівні, хоча це те
# саме в скороченні. Абревіатури — точним збігом (корінь «ова» зловив би
# «овальний»), решта — префіксом.
LEVEL_CODES = (
    ("обл", ("обласн",), {"ова", "ода"}),
    ("міс", ("міськ",), {"мва", "мда"}),
    ("рай", ("районн",), {"рва", "рда"}),
    ("сіл", ("сільськ",), set()),
    ("сел", ("селищн",), set()),
    ("гром", ("громад",), set()),
)
ADMIN_LEVELS = tuple(r for _c, roots, _a in LEVEL_CODES for r in roots)


def _level_code(word):
    for code, roots, abbr in LEVEL_CODES:
        if word in abbr or any(word.startswith(r) for r in roots):
            return code
    return None


def _is_level_word(word):
    return _level_code(word) is not None


# Слова, які в класі «уточнення» РОЗРІЗНЯЮТЬ, а не доповнюють: «заступник» ≠
# «перший заступник», «мер» ≠ «колишній мер». Решта зайвих слів (україни,
# миколаївської, ради, міської) назву саме доповнюють. Поділ потрібен, щоб
# питати класом: доповнення можна закрити гуртом, розрізнювачі — ні.
DISCRIMINATING = {
    # «віце-мер Миколаєва» ~ «мер Миколаєва» — різниця в одному слові, і це
    # РІЗНІ посади. Без цього слова пара падала в «доповнює назву», тобто
    # туди, де відповідь зазвичай «так».
    "віце", "заступниця",
    # Звання й тип мандата — не уточнення назви, а інша річ: «заслужений
    # тренер України» це звання, а не робота тренера; «народний депутат» це
    # парламент, а не міськрада.
    "заслужений", "заслужена", "народний", "народна", "почесне",
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
    # Статус посади — перед лексикою: «в.о. мера» ~ «мер» лексично схожі, але
    # це різні відповіді на питання «хто обіймав посаду».
    ma, mb = _status_marker(a), _status_marker(b)
    if bool(ma) != bool(mb):
        return "status_diff", f"«{ma or mb}» лише в одному написанні"

    if sorted(ta) == sorted(tb) and ta != tb:
        return "permutation", None
    # Різниця лише у службових словах — кома проти «та» і подібне.
    da = [w for w in ta if w not in FUNCTION_WORDS]
    db = [w for w in tb if w not in FUNCTION_WORDS]
    if da == db and ta != tb:
        extra = sorted(set(ta) ^ set(tb))
        return "function_words", " ".join(extra) or None
    short_t, long_t = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    hit = _abbrev_match(short_t, long_t)
    if hit:
        return "abbrev", hit
    # ПІСЛЯ скорочень — інакше «ова» ↔ «обласної військової адміністрації»
    # читалось би як різний рівень, хоча це те саме слово в скороченні.
    # Обидва написання називають МІСЦЕ, і місця різні — це різні органи, хоч
    # скільки б слів іще відрізнялось. Перевірка стоїть тут, а не лише в гілці
    # «одне слово замінено», бо «депутатКА ОБЛАСНОЇ ради» ~ «депутат МІСЬКОЇ
    # ради» різняться двома словами і провалювались у клас «інше», де рішення
    # доводилось ухвалювати вручну 312 разів.
    pa = {_region_code(w) for w in ta if _is_region_word(w)}
    pb = {_region_code(w) for w in tb if _is_region_word(w)}
    if pa and pb and pa != pb:
        wa = " ".join(sorted(w for w in ta if _is_region_word(w)))
        wb = " ".join(sorted(w for w in tb if _is_region_word(w)))
        return "place_swap", f"{wa} ↔ {wb}"
    la = {_level_code(w) for w in ta if _is_level_word(w)}
    lb = {_level_code(w) for w in tb if _is_level_word(w)}
    if la and lb and la != lb:
        wa = " ".join(sorted(w for w in ta if _is_level_word(w)))
        wb = " ".join(sorted(w for w in tb if _is_level_word(w)))
        return "level_swap", f"{wa} ↔ {wb}"
    if sa < sb or sb < sa:
        extra = sorted((sb - sa) if sa < sb else (sa - sb))
        detail = " ".join(extra)
        if any(_is_other_region(w) for w in extra):
            return "containment_geo", detail
        # Розрізнювач сильніший за «нашу країну»: у «заслужений тренер
        # УКРАЇНИ» ~ «тренер» зайві слова містять і топонім, і звання, але
        # вирішує саме звання — це різні речі, а не повніша назва.
        if set(extra) & DISCRIMINATING:
            return "containment_disc", detail
        if any(_is_own_place(w) for w in extra):
            return "containment_own", detail
        return "containment_fill", detail
    import difflib
    # РАНГ перед усією лексикою: «віцепрезидент США» і «президент США»
    # відрізняються приставкою, тобто посимвольно схожі на 0.82 — і
    # запасне правило нижче читало їх як ДРУКАРСЬКУ різницю, тобто клас,
    # який закривають гуртом «так». Живий випадок 02.08.
    ra, rb = _rank_words(ta), _rank_words(tb)
    if ra != rb:
        extra = " ".join(sorted(rb - ra)) or " ".join(sorted(ra - rb))
        return "status_diff", f"«{extra}» лише в одному написанні"
    # одне слово замінено: та сама конструкція, різниця рівно в одному токені
    if len(ta) == len(tb):
        diff = [(x, y) for x, y in zip(ta, tb) if x != y]
        if len(diff) == 1:
            x, y = diff[0]
            # «Сєнкевич»/«Сенкевич» теж різняться одним токеном, але це не
            # заміна слова, а описка — інакше друкарський клас спорожнів би
            if difflib.SequenceMatcher(None, x, y).ratio() < 0.8:
                # Замінили місце («Миколаївської» → «Херсонської») або рівень
                # («обласної» → «міської») — це різні органи, а не синоніми.
                if _region_code(x) != _region_code(y) and (
                        _is_region_word(x) or _is_region_word(y)):
                    return "place_swap", f"{x} → {y}"
                if _level_code(x) != _level_code(y) and (
                        _is_level_word(x) or _is_level_word(y)):
                    return "level_swap", f"{x} → {y}"
                return "word_swap", f"{x} → {y}"
            return "typo", f"{x} / {y}"
    # Запасне правило «схожі рядки — описка» БЕЗ перевірки слів було
    # найдорожчою помилкою класифікатора: «директор КП «Миколаївводоканал»» і
    # «директор КП «Миколаївські парки»» збігаються на 0.86 посимвольно, а
    # «начальник управління КУЛЬТУРИ …ради» і «начальниця управління ОСВІТИ
    # …ради» — на 0.90. Обидві пари падали в клас, який закривають гуртом
    # «так», тобто одне необережне рішення зліплювало різні органи назавжди.
    #
    # Описка — це коли ЖОДНЕ слово не замінене на інше: усі розбіжні токени
    # схожі між собою («Сєнкевич»/«Сенкевич»), і їх не більше двох.
    # Різниця ЛИШЕ в пунктуації: «премєр-міністр» ~ «премєр міністр» дають
    # однакові токени, тобто жодне слово не замінене — це описка в чистому
    # вигляді, і без цього рядка вона провалювалась би в «інше».
    if ta == tb and a != b:
        return "typo", None
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.85 and len(ta) == len(tb):
        diff = [(x, y) for x, y in zip(ta, tb) if x != y]
        if diff and len(diff) <= 2 and all(
                difflib.SequenceMatcher(None, x, y).ratio() >= 0.8
                for x, y in diff):
            return "typo", " · ".join(f"{x} / {y}" for x, y in diff)
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
        return names, []

    # (а) спільний носій. Найсильніший сигнал: одна людина під двома написаннями
    # у ПЕРЕТИННІ періоди — це майже напевно одна посада, а не підвищення.
    # Носія рахуємо НЕ штукою, а вагою. «президент» (Зеленський 592) і
    # «президент США» (Трамп 1000) ділять Трампа — але на 6 згадок із 610,
    # тобто спільність 1%. За штучним лічильником це виглядало як «спільний
    # носій у перетинні періоди: 2», тобто найсильніший сигнал, і бот
    # пропонував злити Зеленського з Байденом. За вагою — 1%, тобто шум.
    shared, overlap = {}, {}
    by_entity = {}
    for r in bot_db.query(CARRIERS_SQL, (names,)):
        by_entity.setdefault(r["entity_id"], []).append(
            (r["rn"], r["lo"] or 0, r["hi"] or 0, r["c"] or 0))
    for spans in by_entity.values():
        spans.sort()
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                a, alo, ahi, ca = spans[i]
                b, blo, bhi, cb = spans[j]
                key = (a, b)
                w = min(ca, cb)
                shared[key] = shared.get(key, 0) + w
                if alo <= bhi and blo <= ahi:
                    overlap[key] = overlap.get(key, 0) + w

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

    # Порядок у парі канонізуємо ПО-PYTHONʼОМУ. Частина кандидатів приходить
    # із SQL (`ON a.rn < b.rn`), а порівняння рядків у Postgres іде за
    # колацією, яка ігнорує пунктуацію: «премєр-міністр угорщини» там менше за
    # «премєр угорщини», а в Python — навпаки. Через це та сама пара лягала в
    # чергу ДВІЧІ, в обох порядках, і людину питали про неї два рази поспіль.
    def _key(x, y):
        return (x, y) if x < y else (y, x)

    cands = {_key(*k) for k in shared} | {_key(*k) for k in sims}
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
                cands.add(_key(group[i], group[j]))

    rows = []
    for a, b in cands:
        if a not in roles or b not in roles:
            continue
        sig, score = [], 0.0
        ov = overlap.get((a, b), 0)
        sh = shared.get((a, b), 0)
        base = min(roles[a]["links"], roles[b]["links"]) or 1
        ratio = ov / base
        if ov and ratio >= 0.5:
            # Єдиний сигнал, що означає саме «та сама посада», а не «схожі
            # рядки»: ті самі люди носять обидва написання. Тому важить
            # більше за будь-яку лексичну комбінацію.
            score += 4
            sig.append(f"спільні носії дають {ratio:.0%} згадок")
        elif ov and ratio >= 0.15:
            score += 2
            sig.append(f"спільні носії дають {ratio:.0%} згадок")
        elif ov:
            score += 0.5
            sig.append(f"спільні носії лише {ratio:.0%} згадок — слабко")
        elif sh:
            score += 1
            sig.append("спільні носії є, але періоди не перетинаються")
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
        rows.append((a, b, round(score, 2), " · ".join(sig), cls, detail, stake,
                     roles[a]["links"], roles[b]["links"]))
    rows.sort(key=lambda r: (-r[2], -r[6]))
    return names, rows


def scan_pairs(min_links=MIN_LINKS, top_roles=TOP_ROLES):
    """Прогнати детектор і покласти нові пари в чергу.

    Нічого не вирішує і не чіпає вже вирішені пари: рішення людини («ні, різні»)
    живе назавжди, тому повторний прогін лише додає нове й освіжає бали
    невирішених. Повертає (ролей у переборі, кандидатів, з них нових/оновлених,
    у черзі)."""
    names, rows = find_pairs(min_links, top_roles)
    n_roles = len(names)
    # Прибрати з черги пари, які за НИНІШНІМИ правилами кандидатами вже не є.
    # Детектор за сесію переписувався тричі (вага носіїв, класи, пороги), і без
    # цього людина відповідала б на питання, яких бот більше не ставить.
    # Чіпаємо тільки НЕвирішені й тільки в межах поточного перебору — рішення
    # людини не чіпає ніщо.
    keep = {(a, b) for a, b, *_ in rows}
    stale = [r["id"] for r in bot_db.query(
        "SELECT id, a_norm, b_norm FROM role_pairs WHERE verdict IS NULL "
        "AND a_norm = ANY(%s) AND b_norm = ANY(%s)", (names, names))
        if (r["a_norm"], r["b_norm"]) not in keep]
    if stale:
        bot_db.execute("DELETE FROM role_pairs WHERE id = ANY(%s)", (stale,))
    added = 0
    now = int(time.time())
    for a, b, score, sig, cls, detail, stake, la, lb in rows:
        n = bot_db.execute(
            "INSERT INTO role_pairs (a_norm, b_norm, score, signals, cls, "
            "  cls_detail, stake, links_a, links_b, updated) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (a_norm, b_norm) DO UPDATE SET "
            "  score = EXCLUDED.score, signals = EXCLUDED.signals, "
            "  cls = EXCLUDED.cls, cls_detail = EXCLUDED.cls_detail, "
            "  stake = EXCLUDED.stake, links_a = EXCLUDED.links_a, "
            "  links_b = EXCLUDED.links_b, updated = EXCLUDED.updated "
            "WHERE role_pairs.verdict IS NULL",
            (a, b, score, sig, cls, detail, stake, la, lb, now))
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
    ЗАГАЛЬНІШЕ переважає вужче, за інших рівних — повніше (§5 плану: канон не
    «за частотою», а за повнотою).

    Правило «загальніше» додано 02.08 після живого випадку: до канону
    «перший заступник МИКОЛАЇВСЬКОГО міського голови» долучився «перший
    заступник міського голови», який носять і заступники інших міст — і
    вся група тихо стала миколаївською, хоч людина такого не обирала.
    Назва канону мусить бути істинною для КОЖНОГО написання, яке він
    покриває; із двох написань, де одне вкладене в друге, така лише
    загальніша."""
    a, b = (sample_a or "").strip(), (sample_b or "").strip()
    if not a:
        return b
    if not b:
        return a
    ta, tb = set(_tokens(role_norm(a))), set(_tokens(role_norm(b)))
    if ta < tb:
        return a
    if tb < ta:
        return b
    ca = bool(_COLLOQUIAL & ta)
    cb = bool(_COLLOQUIAL & tb)
    if ca != cb:
        return b if ca else a
    return a if len(a) >= len(b) else b


def _general_name(canon, variants):
    """Назва, істинна для КОЖНОГО написання: із двох, де одне вкладене в
    друге, така лише загальніша. `variants` — [(raw_norm, raw_sample)].

    Живе окремою функцією, щоб прев'ю злиття канонів рахувало назву тим самим
    правилом, яким її потім поставить `_regeneralize`. Розійдуться — прев'ю
    покаже одну назву, а бот поставить іншу, і довіри до кнопки не буде."""
    best, best_t = canon, set(_tokens(role_norm(canon) or ""))
    for raw_norm, sample in variants:
        t = set(_tokens(raw_norm or ""))
        if t and t < best_t:
            best, best_t = (sample or raw_norm), t
    return best


def _regeneralize(canon_id):
    """Якщо в каноні з'явилось написання ЗАГАЛЬНІШЕ за його назву — переназвати.

    Назва канону мусить бути істинною для кожного написання всередині. Живий
    випадок 02.08: канон звався «перший заступник МИКОЛАЇВСЬКОГО міського
    голови», людина долучила до нього «перший заступник міського голови» (його
    носять і заступники інших міст) — і група стала миколаївською без жодного
    рішення про це. Назва мала стати загальнішою, а не лишитись вужчою.

    Переназивання безпечне в один бік: загальніше твердження істинне для всіх,
    вужче — ні. Тому звужувати назву тут не можна ніколи."""
    rows = bot_db.query(
        "SELECT rc.canon, rv.raw_sample, rv.raw_norm FROM role_canon rc "
        "JOIN role_variants rv ON rv.canon_id = rc.id WHERE rc.id = %s",
        (canon_id,))
    if not rows:
        return None
    canon = rows[0]["canon"]
    best = _general_name(canon, [(r["raw_norm"], r["raw_sample"]) for r in rows])
    if best != canon:
        # canon_norm унікальний: якщо загальніша назва вже зайнята іншим
        # каноном, мовчки лишаємо стару — переназвати можна руками
        # (/roles_rename), а падати посеред злиття не можна.
        bot_db.execute(
            "UPDATE role_canon SET canon = %s, canon_norm = %s WHERE id = %s "
            "AND NOT EXISTS (SELECT 1 FROM role_canon x WHERE x.canon_norm = %s "
            "                AND x.id <> %s)",
            (best, role_norm(best), canon_id, role_norm(best), canon_id))
    return best


def _variant_canon(raw_norm):
    rows = bot_db.query(
        "SELECT canon_id FROM role_variants WHERE raw_norm = %s", (raw_norm,))
    return rows[0]["canon_id"] if rows else None


def _canon_size(canon_id):
    rows = bot_db.query(
        "SELECT count(*) AS n FROM role_variants WHERE canon_id = %s", (canon_id,))
    return rows[0]["n"] if rows else 0


def merge_canons(keep, drop, decided_by=None):
    """Злити два КАНОНИ: написання програшного переїжджають до переможця,
    орган підтягується, якщо в переможця його не було, порожній канон
    зникає. Повертає (id канону, назва після зведення).

    Ядро спільне з відповіддю «так» на пару написань (там канони зливаються
    побічним ефектом) і з /roles_merge, де це САМЕ рішення людини: два канони
    про один орган інакше звести нічим — детектор питає про написання, а не
    про канони.

    `_regeneralize` наприкінці обов'язковий: після переїзду написань назва
    переможця мусить лишитись істинною для КОЖНОГО з них, інакше вужча назва
    тихо накриває ширшу групу (той самий випадок, що §3.1 доку дефектів)."""
    ensure_schema()
    if keep == drop:
        rows = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (keep,))
        return keep, (rows[0]["canon"] if rows else None)
    with bot_db.transaction():
        bot_db.execute(
            "UPDATE role_variants SET canon_id = %s WHERE canon_id = %s",
            (keep, drop))
        bot_db.execute(
            "UPDATE role_canon SET org_entity_id = coalesce(org_entity_id, "
            "  (SELECT org_entity_id FROM role_canon WHERE id = %s)) "
            "WHERE id = %s", (drop, keep))
        bot_db.execute("DELETE FROM role_canon WHERE id = %s", (drop,))
    _regeneralize(keep)
    rows = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (keep,))
    return keep, (rows[0]["canon"] if rows else None)


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
        return merge_canons(keep, drop)

    if ca or cb:
        canon_id = ca or cb
        new_norm = b_norm if ca else a_norm
        new_sample = sample_b if ca else sample_a
        bot_db.execute(
            "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, decided_by, created) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (raw_norm) DO UPDATE SET canon_id = EXCLUDED.canon_id",
            (new_norm, new_sample, canon_id, decided_by, now))
        _regeneralize(canon_id)
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
    now = int(time.time())
    bot_db.execute(
        "UPDATE role_pairs SET verdict = %s, decided_by = %s, updated = %s "
        "WHERE id = %s", (verdict, decided_by, now, pair_id))
    if verdict == "different":
        _spread_rejection(pair_id, decided_by, now)


def _spread_rejection(pair_id, decided_by, now):
    """«Ні» діє на рівні КАНОНУ, а не написання.

    Реальний випадок 02.08: людина відхилила «прем'єр-міністерка» ~
    «прем'єр-міністр Данії», а наступним питанням отримала «прем'єр-міністр
    Данії» ~ «прем'єр-міністрКА» — інше написання ТОГО САМОГО канону з семи.
    Для бота це різні пари, для людини — те саме питання вдруге, і таких
    повторів у каноні стільки, скільки в ньому написань.

    Відповідь «різні» означає «данська прем'єрка не належить цьому канону», і
    це твердження про канон цілком. Тому гасимо всі невирішені пари, які
    зводяться до тієї самої пари канонів."""
    def key_of(norm, canon_id):
        return f"c{canon_id}" if canon_id else f"n{norm}"

    rows = bot_db.query(
        "SELECT p.id, p.a_norm, p.b_norm, va.canon_id AS ca, vb.canon_id AS cb "
        "FROM role_pairs p "
        "LEFT JOIN role_variants va ON va.raw_norm = p.a_norm "
        "LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm "
        "WHERE p.verdict IS NULL OR p.id = %s", (pair_id,))
    target = next((r for r in rows if r["id"] == pair_id), None)
    if not target:
        return
    want = {key_of(target["a_norm"], target["ca"]),
            key_of(target["b_norm"], target["cb"])}
    # Пара всередині одного канону сенсу не має: обидва ключі однакові, і під
    # таке «ні» підпало б усе підряд.
    if len(want) < 2:
        return
    hit = [r["id"] for r in rows
           if r["id"] != pair_id
           and {key_of(r["a_norm"], r["ca"]), key_of(r["b_norm"], r["cb"])} == want]
    if hit:
        bot_db.execute(
            "UPDATE role_pairs SET verdict = 'different', decided_by = %s, "
            "updated = %s WHERE id = ANY(%s) AND verdict IS NULL",
            (decided_by, now, hit))
    return len(hit)


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
GROUP BY 1 ORDER BY c DESC LIMIT 12
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
LIMIT 12
"""


def _name_overlap(role_text, org_name):
    """Чи ділять посада й організація слово, яке НЕ є топонімом.

    Сама по собі трграмна схожість зводить їх за містом: у «міський голова
    Києва» вона впевнено пропонувала «Господарський суд міста Києва» і
    «Голосіївський районний суд Києва» — спільне в них рівно одне слово, і це
    назва міста. Місто не називає орган, воно лише каже, де він; орган
    називають слова «рада», «суд», «міністерство», «управління».

    Порівнюємо префіксами й у обидва боки, бо відмінки й рід розводять ті самі
    корені: «міськИЙ голова» ~ «міськА рада», «суд» ~ «суддя»."""
    def stems(text):
        return [w for w in _tokens(role_norm(text) or "")
                if len(w) >= 3 and not _is_region_word(w)]
    # РІЗНІ КРАЇНИ — одразу ні, хоч би скільки слів збігалось. «Російський
    # президент» ділить слово «президент» з «Офісом Президента України»,
    # «Президентом США» і «Президентом України», і без цієї перевірки всі троє
    # стояли кандидатами в орган Путіна. Топонім у назві посади — це не шум,
    # це головна прикмета органу.
    ra = {_region_code(w) for w in _tokens(role_norm(role_text) or "")
          if _is_region_word(w)}
    ro = {_region_code(w) for w in _tokens(role_norm(org_name) or "")
          if _is_region_word(w)}
    if ra and ro and not (ra & ro):
        return False
    a, b = stems(role_text), stems(org_name)
    return any(x.startswith(y[:5]) or y.startswith(x[:5]) for x in a for y in b)

ORG_CANDIDATES_SQL = """
SELECT e.id, coalesce(e.name_ua, e.name_ru) AS name, count(*) AS c
FROM article_entities ae
JOIN entities e ON e.id = ae.entity_id AND e.kind = 'org'
WHERE ae.article_id IN (
    SELECT ae2.article_id FROM article_entities ae2
    WHERE role_norm(ae2.role_at_time) = ANY(%s))
GROUP BY 1, 2 ORDER BY c DESC LIMIT 25
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
    # ЖОДЕН із двох сигналів поодинці не працює, і це видно на двох реальних
    # провалах. Сам співпояв дав Галущенку НАБУ, САП і «Квартал 95» — він
    # ловить СЮЖЕТ, а не афіліацію. Сама лексика дала «міському голові Києва»
    # «Київський міський суд», «Міський сад» і «Першу міську лікарню Києва» —
    # у мера з міською радою спільне тільки слово «міськ», рівно як і в суду.
    # Розрізняє їх ПЕРЕТИН: правильний орган і називається схоже, і трапляється
    # в тих самих статтях. Тому спершу ті, у кого є обидва сигнали.
    co = {}
    for r in bot_db.query(ORG_CANDIDATES_SQL, (variants,)):
        co[r["id"]] = {"id": r["id"], "name": r["name"], "c": r["c"]}
    named = {}
    try:
        for r in bot_db.query(ORG_BY_NAME_SQL, (role_norm(canon), role_norm(canon))):
            if _name_overlap(canon, r["name"]):
                named[r["id"]] = {"id": r["id"], "name": r["name"], "c": 0}
    except Exception as e:
        print(f"entity_roles: лексичний пошук органу пропущено — {e}")
    for oid, r in co.items():
        if _name_overlap(canon, r["name"]):
            named.setdefault(oid, r)["c"] = r["c"]

    both = sorted((r for r in named.values() if r["c"]),
                  key=lambda r: -r["c"])
    only_name = [r for r in named.values() if not r["c"]]
    only_co = [r for r in co.values() if r["id"] not in named]
    out = ([{"id": r["id"], "name": r["name"],
             "basis": f"за назвою посади · {r['c']} спільних статей"}
            for r in both]
           + [{"id": r["id"], "name": r["name"], "basis": "за назвою посади"}
              for r in only_name]
           + [{"id": r["id"], "name": r["name"],
               "basis": f"{r['c']} спільних статей"}
              for r in only_co[:3]])
    return out[:4]


def org_markup(canon_id, cands, generic=False):
    skip = InlineKeyboardButton(
        "↷ Немає одного органу" if generic else "↷ Не зараз",
        callback_data=f"rdo:{canon_id}:0")
    rows = [[skip]] if generic else []
    rows += [[InlineKeyboardButton(f"{c['name']} — {c['basis']}",
                                   callback_data=f"rdo:{canon_id}:{c['id']}")]
             for c in cands]
    if not generic:
        rows.append([skip])
    return InlineKeyboardMarkup(rows)


ORG_QUESTION = ("\n\nЧий це орган? (дасть афіліацію людина↔організація; "
                "змінити потім — /roles_org)")
ORG_QUESTION_GENERIC = (
    "\n\n⚠️ Посада не називає конкретну установу — це шаблон для кількох "
    "органів одразу (напр. «голова обласної ВА» = і Миколаївська, і Одеська, "
    "і Херсонська). Одного органу тут немає, тисни «Немає одного органу».\n"
    "Прив'язувати варто лише посаду з назвою: «міський голова Миколаєва» → "
    "Миколаївська міська рада.")

# ПОРЯДОК ПИТАНЬ — за приростом покриття, а не за впевненістю детектора.
#
# Замір 02.08 показав, чому це важливо: у черзі 875 питань, і разом вони варті
# 8072 зв'язків — тобто в середньому по дев'ять. Але розподіл кривий: кілька
# десятків пар несуть сотні зв'язків кожна, а хвіст — по одному-два. При
# сортуванні за score найжирніші пари стояли посеред черги, і щоб дійти до
# них, треба було перетерпіти сотні дрібних; людина втомлюється раніше.
#
# Приріст рахується ЧЕСНО: сторона, яка вже під каноном, покриття не додає,
# тому в сумі лише непокриті. Інакше пара з гігантським каноном на одному боці
# вічно стояла б першою, віддаючи насправді кілька зв'язків.
GAIN_SQL = ("(CASE WHEN va.canon_id IS NULL THEN coalesce(p.links_a, 0) ELSE 0 END"
            " + CASE WHEN vb.canon_id IS NULL THEN coalesce(p.links_b, 0) ELSE 0 END)")

NEXT_PAIR_SQL = f"""
SELECT p.id, p.a_norm, p.b_norm, p.score, p.signals, p.cls, p.cls_detail,
       {GAIN_SQL} AS gain
FROM role_pairs p
LEFT JOIN role_variants va ON va.raw_norm = p.a_norm
LEFT JOIN role_variants vb ON vb.raw_norm = p.b_norm
WHERE p.verdict IS NULL
  AND (va.canon_id IS NULL OR vb.canon_id IS NULL OR va.canon_id <> vb.canon_id)
  AND NOT (p.id = ANY(%s))
  AND (%s IS NULL OR p.cls = %s)
ORDER BY {GAIN_SQL} DESC, p.score DESC, p.id
LIMIT 1
"""


def _role_card(rn):
    rows = bot_db.query(ROLE_CARD_SQL, (rn,))
    card = dict(rows[0]) if rows else {}
    # Носій із ЛІЧИЛЬНИКОМ: «Кім (314), Прокудін (1)» одразу показує, що
    # другий — випадкова помилка витягу, а не другий носій посади. Без числа
    # такий викид виглядав би як рівноправний факт і міг збити рішення.
    rows_c = [r for r in bot_db.query(ROLE_CARRIERS_SQL, (rn,)) if r["name"]]
    card["carriers"] = [f"{r['name']} ({r['c']})" for r in rows_c[:2]]
    card["top"] = rows_c[0]["name"] if rows_c else None
    card["names"] = [r["name"] for r in rows_c]
    card["by_name"] = {r["name"]: r["c"] for r in rows_c}
    card["sample"] = card.get("sample") or rn
    return card


CANON_OF_SQL = """
SELECT rc.id, rc.canon, count(*) AS variants
FROM role_variants rv
JOIN role_canon rc ON rc.id = rv.canon_id
JOIN role_variants all_rv ON all_rv.canon_id = rc.id
WHERE rv.raw_norm = %s
GROUP BY rc.id, rc.canon
"""


def _canon_of(raw_norm):
    rows = bot_db.query(CANON_OF_SQL, (raw_norm,))
    return dict(rows[0]) if rows else None


def next_question(exclude_ids, cls=None):
    rows = bot_db.query(NEXT_PAIR_SQL, (list(exclude_ids or []), cls, cls))
    if not rows:
        return None
    p = dict(rows[0])
    p["a"] = _role_card(p["a_norm"])
    p["b"] = _role_card(p["b_norm"])
    # Чи належить котресь написання вже канону. Без цього питання бреше
    # масштабом: людина відповідає про ДВА написання, а «так» може злити два
    # готові канони по 20 написань кожен — і одна помилка псує обидва
    # (реальний випадок 02.08: херсонська ОБЛАСНА ВА опинилась у каноні
    # херсонської МІСЬКОЇ).
    p["a_canon"] = _canon_of(p["a_norm"])
    p["b_canon"] = _canon_of(p["b_norm"])
    # Клас ПЕРЕРАХОВУЄМО, а не читаємо з role_pairs: у таблиці лежить розкладка
    # того детектора, який колись прогнав скан, і після кожної правки правил
    # вона застаріває мовчки. Реальний випадок 02.08: «голова обласної ВА» ~
    # «очільник обласної ВА» показувалось як «ІНШЕ МІСЦЕ — замінено топонім ←
    # зазвичай ні», бо стара версія вважала «голова» топонімом (Голованівськ);
    # нинішні правила дають «одне слово замінено», і відповідь тут «так».
    # Гурт (/roles_bulk) перераховує з тієї ж причини — питання по одній парі
    # не мало права лишатись єдиним місцем зі старими класами.
    sh = set((p["a"].get("by_name") or {})) & set((p["b"].get("by_name") or {}))
    cls, detail = classify_pair(p["a_norm"], p["b_norm"], has_carrier=bool(sh))
    if (cls, detail) != (p.get("cls"), p.get("cls_detail")):
        # Записуємо назад, інакше та сама пара далі рахувалась би в старому
        # класі в меню /roles_bulk — і числа на екрані розходились би з тим,
        # що людина щойно бачила в питанні.
        bot_db.execute(
            "UPDATE role_pairs SET cls = %s, cls_detail = %s WHERE id = %s "
            "AND verdict IS NULL", (cls, detail, p["id"]))
    p["cls"], p["cls_detail"] = cls, detail
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
    ca, cb = p.get("a_canon"), p.get("b_canon")
    warn = ""
    # Найпростіша перевірка, яку людина робить очима першою: чи це взагалі про
    # тих самих людей. «президент» = Зеленський, «президент США» = Трамп —
    # головні носії різні, і це видно швидше за будь-які бали.
    # Порівнюємо не «перший проти першого», а ПЕРЕТИН: у пари «суддя
    # Центрального райсуду» ~ «…Миколаєва» ті самі Медюк і Алєйніков просто
    # помінялись місцями за кількістю, і попередження про різних носіїв там
    # брехало. Кричимо лише коли головний носій одного взагалі не трапляється
    # в другого — як Зеленський і Трамп.
    ta, tb = p["a"].get("top"), p["b"].get("top")
    na, nb = p["a"].get("names") or [], p["b"].get("names") or []
    if ta and tb and ta != tb and ta not in nb and tb not in na:
        warn += f"\n⚠️ Головні носії РІЗНІ: {ta} vs {tb}\n"
    if ca and cb and ca["id"] != cb["id"]:
        warn = (f"\n⚠️ «Так» зіллє ДВА канони: «{ca['canon']}» "
                f"({ca['variants']} написань) + «{cb['canon']}» "
                f"({cb['variants']}). Помилка зіпсує обидва.\n")
    elif ca or cb:
        # Обов'язково казати, ЯКЕ саме написання вже в каноні: без цього
        # рядок читається як загальна нотатка, а насправді це головне —
        # «президент США» вже в каноні «віце-президентка США» означає, що
        # там уже стоїть помилка, і «так» долучить до неї ще одне написання.
        side, c = ("А", ca) if ca else ("Б", cb)
        # ЧИМ канон відрізняється від того, що до нього долучається. Без цих
        # двох слів рядок читається як нотатка, і людина ковзає по ньому очима:
        # реальний випадок 02.08 — «керівниця управління ОСВІТИ» долучалась до
        # канону «начальник управління КУЛЬТУРИ», і в довгих назвах різниця в
        # одному слові непомітна. Саме вона тут і вирішує.
        joining = (p.get("b_norm") if ca else p.get("a_norm")) or ""
        ct = set(_tokens(role_norm(c["canon"]) or ""))
        jt = set(_tokens(joining or ""))
        only_c = sorted(w for w in ct - jt if len(w) > 3)[:3]
        only_j = sorted(w for w in jt - ct if len(w) > 3)[:3]
        diff = ""
        if only_c and only_j:
            diff = (f"\n   Канон каже «{', '.join(only_c)}», "
                    f"а тут «{', '.join(only_j)}» — звір, чи це одне й те саме")
        warn = (f"\nℹ️ {side} уже в каноні «{c['canon']}» "
                f"({c['variants']} написань) — інше долучиться до нього.{diff}\n")
    # ЧУЖІ НОСІЇ ОДНІЄЇ СТОРОНИ — число, яким людина й вирішує ці питання, і
    # саме його в картці бракувало. «Спільні носії дають 100%» рахує частку
    # від МЕНШОГО написання, тому сліпе до решти: у пари «Миколаївський
    # міський голова» (лише Сєнкевич) ~ «міський голова» (дев'ять носіїв)
    # воно показувало 100%, хоча гола форма покриває ще вісьмох мерів інших
    # міст — і «так» приписало б їх усіх Миколаєву. Те саме, але без жодної
    # вкладеності написань, дає «керівник обласної ВА» (Прокудін, Лисак, Кім,
    # Кіпер) ~ «очільник ХЕРСОНСЬКОЇ обласної ВА» — тому рядок рахується для
    # БУДЬ-ЯКОГО класу, а сторону обирає не довжина написання, а те, у кого
    # більша частка чужих згадок. Саме ця різниця відділяє «секретар МІСЬКОЇ
    # ради» (не зливати) від «міністр фінансів» (зливати).
    def _foreign(one, two):
        others = {n: c for n, c in (one.get("by_name") or {}).items()
                  if n not in (two.get("by_name") or {})}
        got = sum(others.values())
        return others, got, got / max(one.get("links") or 1, 1)
    # Поріг — АБСОЛЮТНИЙ, а не у відсотках. Частка обманює на великих ролях:
    # у пари «президент» ~ «президент України» чужі носії (Трамп і ще восьмеро
    # закордонних президентів) дають 18 згадок із 611 — це 3%, тобто нижче
    # будь-якого відсоткового порога, і картка мовчала на найдорожчому питанні
    # черги (+3103 зв'язків). Двох згадок досить: числа поруч, і людина сама
    # бачить, 18 це чи 2.
    cands = [(sh, o, s, n) for n, one, two in
             (("А", p["a"], p["b"]), ("Б", p["b"], p["a"]))
             for o, s, sh in [_foreign(one, two)]
             if s >= 2]
    # Коли головні носії вже названі різними, це те саме попередження вдруге.
    if cands and "Головні носії РІЗНІ" not in warn:
        # Сторону обирає АБСОЛЮТНА вага чужих носіїв, а не частка: 18 згадок
        # закордонних президентів важать більше за 10% від ролі, що трапилась
        # двадцять разів.
        share, others, got, side = max(cands, key=lambda t: t[2])
        top = sorted(others.items(), key=lambda kv: -kv[1])[:3]
        # КІЛЬКІСТЬ ЗГАДОК ТУТ НІЧОГО НЕ ВИРІШУЄ, і перша версія цього рядка
        # підказувала протилежне («по одній згадці — схоже на викид витягу»).
        # Олег виправив 02.08 на живому прикладі: у пари «директор зоопарку» ~
        # «директор МИКОЛАЇВСЬКОГО зоопарку» ті двоє по одній згадці — цілком
        # справжні директори зоопарків ІНШИХ міст, і «так» зробило б їх
        # миколаївськими назавжди. Одна згадка однаково буває і випадковим
        # зачепом у беку, і законним фактом; відрізнити їх можна тільки
        # глянувши, ХТО ці люди. Тому картка не радить, а показує перевірку.
        noise = ("\n   Подивись, ХТО вони: /roles_who "
                 + (p["a_norm"] if side == "А" else p["b_norm"]))
        warn += (f"\n⚠️ У «{side}» ще "
                 f"{plural(len(others), 'носій', 'носії', 'носіїв')}, "
                 f"яких немає в другого: "
                 + ", ".join(f"{n} ({c})" for n, c in top)
                 + f" — разом {got} з "
                 f"{(p['a'] if side == 'А' else p['b']).get('links') or 0} згадок "
                 f"({share:.0%}). «Так» припише їх спільній посаді"
                 + noise + "\n")
    # Клас із BULK_REJECT — це той, де відповідь майже завжди «ні». Сказати
    # це вголос дешевше, ніж сподіватись, що людина пам'ятає список класів.
    hint = ""
    if (p.get("cls") or "") in BULK_REJECT:
        hint = " ← зазвичай «ні»"
    # Скільки покриття дасть «так». Черга йде за цим числом, і поки воно
    # тризначне — питання варте уваги; коли впало до одиниць, решту вечора
    # можна не витрачати. Без нього людина не бачить, де закінчується користь.
    if p.get("gain"):
        gain = f"На кону: +{p['gain']} зв'язків покриття\n"
    elif p.get("a_canon") and p.get("b_canon"):
        # Нуль тут — не збій рядка, а факт: обидві сторони вже під канонами,
        # тобто їхні зв'язки вже пораховані, і «так» об'єднає САМІ канони.
        # Мовчати про це не можна: рядок зникав, і виглядало як поламане.
        gain = ("На кону: покриття не зміниться — обидва написання вже під "
                "канонами, «так» об'єднає самі канони\n")
    else:
        gain = ""
    return ("🦊 Це та сама посада?\n\n"
            + block("А", p["a"]) + "\n"
            + block("Б", p["b"]) + "\n"
            + warn + "\n"
            + gain
            + (f"Клас: {cls}{hint}\n" if cls else "")
            + f"Підстава: {p['signals']}")


def is_generic_role(canon_text):
    """Чи називає посада КОНКРЕТНУ установу.

    «голова обласної військової адміністрації» без області — це шаблон посади
    для чотирьох різних областей одразу (Кім, Кіпер, Прокудін, Лисак), і
    питати «чий це орган?» тут безглуздо: одного органу не існує. Афіліацію
    для таких ролей дає не канон, а організація в тій самій статті."""
    # lower() обовʼязково: у каноні написання з великої («Миколаєва»),
    # а префікси топонімів зберігаються в нижньому регістрі.
    return not any(_is_region_word(w)
                   for w in _tokens((canon_text or "").lower()))


def _question_markup(pair_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, одне", callback_data=f"rdp:s:{pair_id}"),
        InlineKeyboardButton("❌ Ні, різні", callback_data=f"rdp:d:{pair_id}"),
        InlineKeyboardButton("↷ Пропустити", callback_data=f"rdp:x:{pair_id}"),
    ]])


# У пам'яті: що людина пропустила в цій сесії. Свідомо НЕ в БД — «пропустити»
# означає «не зараз», а не «різні»; після перезапуску бота питання повернеться.
_skipped = {}

# Фільтр «питати лише цей клас»: з екрана класу в /roles_bulk має бути вихід у
# питання по одній парі, інакше людина бачить список і не може на нього
# відповісти — саме так і сталось на першому ж двоїстому класі.
_class_filter = {}


def _allowed(update):
    user = update.effective_user
    return not _ALLOWED_USER_IDS or (user and user.id in _ALLOWED_USER_IDS)


async def _send_next(chat, user_id, bot=None):
    """Наступне питання черги — або підсумок, коли черга скінчилась."""
    skip = _skipped.setdefault(user_id, set())
    cls = _class_filter.get(user_id)
    p = await asyncio.to_thread(next_question, skip, cls)
    if not p:
        if cls:
            _class_filter.pop(user_id, None)
            await chat.send_message(
                f"🦊 Клас «{CLASS_LABELS.get(cls, cls)}» пройдено.\n"
                f"Решта класів — /roles_bulk, уся черга — /roles_dedup")
            return
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
    _class_filter.pop(update.effective_user.id, None)
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
        generic = is_generic_role(canon)
        text += ORG_QUESTION_GENERIC if generic else ORG_QUESTION
        await query.edit_message_text(
            text, reply_markup=org_markup(canon_id, orgs, generic))
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
    base = base.split("\n\n⚠️ Посада не називає")[0]
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


# ---------- /roles_who ----------
#
# Хто носить це написання посади — з лінками на статті для рідкісних носіїв.
#
# Питання, на яке немає відповіді в картці злиття і не може бути: у пари
# «директор зоопарку» ~ «директор МИКОЛАЇВСЬКОГО зоопарку» двоє чужих носіїв
# мали по одній згадці, і я порадив злити («схоже на викид витягу»). Олег
# перевірив: це справжні директори зоопарків інших міст, тобто «так» зробило б
# їх миколаївськими назавжди. Одна згадка однаково буває і зачепом у беку, і
# законним фактом — розрізняє їх ТІЛЬКИ заголовок статті, тому бот дає лінк, а
# рішення лишає людині.

WHO_CARRIERS_SQL = """
SELECT ae.entity_id AS eid, coalesce(e.name_ua, e.name_ru) AS name,
       count(*) AS c,
       to_char(to_timestamp(min(a.published)), 'YYYY-MM') AS lo,
       to_char(to_timestamp(max(a.published)), 'YYYY-MM') AS hi
FROM article_entities ae
JOIN entities e ON e.id = ae.entity_id
JOIN articles a ON a.id = ae.article_id
WHERE role_norm(ae.role_at_time) = %s
GROUP BY 1, 2
ORDER BY c DESC
LIMIT 20
"""

WHO_ARTICLES_SQL = """
SELECT a.id, a.published, a.title_ua, a.title_ru, a.slug, a.category, a.kind,
       ae.role_at_time
FROM article_entities ae
JOIN articles a ON a.id = ae.article_id
WHERE role_norm(ae.role_at_time) = %s AND ae.entity_id = %s
ORDER BY a.published DESC
LIMIT 3
"""

# Скільки згадок ще вважається «рідкісним носієм», якого варто розкрити
# лінками. Троє — бо саме на одній-трьох губиться різниця між помилкою витягу
# і законним фактом; у головного носія з десятками згадок питань не виникає.
WHO_RARE = 3


def who_holds(role_text):
    """(написання, [носії з лінками для рідкісних]). Нічого не змінює."""
    # Імпорт ЛІНИВИЙ: archive_search тягне за собою news_archive → ai_messages
    # → anthropic і db.py → pymysql, тобто пів бота заради одного складача
    # URL. Сутнісний шар має лишатись легким — його ганяють тести на живому
    # Postgres без жодного з цих модулів.
    from handlers import archive_search
    ensure_schema()
    rn = role_norm(role_text)
    if not rn:
        return None, []
    rows = [dict(r) for r in bot_db.query(WHO_CARRIERS_SQL, (rn,))]
    for r in rows:
        if r["c"] <= WHO_RARE:
            r["articles"] = [
                archive_search._fmt_item(i + 1, dict(a))
                | {"raw": a["role_at_time"]}
                for i, a in enumerate(bot_db.query(WHO_ARTICLES_SQL,
                                                   (rn, r["eid"])))]
    return rn, rows


async def roles_who_handler(update, context):
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "🦊 Хто носить посаду: /roles_who <написання>\n"
            "Напр.: /roles_who директор зоопарку")
        return
    msg = await update.message.reply_text("🦊 Дивлюсь носіїв…")
    try:
        rn, rows = await asyncio.to_thread(who_holds, text)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {type(e).__name__}: {e}")
        return
    if not rows:
        await msg.edit_text(f"🦊 Написання «{rn or text}» у норі не носить ніхто.")
        return
    lines = [f"🦊 Хто носить «{rn}»\n"]
    for r in rows:
        lines.append(f"• {r['name']} — "
                     f"{plural(r['c'], 'згадка', 'згадки', 'згадок')} · "
                     f"{r['lo']} … {r['hi']}")
        for a in r.get("articles") or []:
            lines.append(f"    {a['date']} {a['title']}\n    {a['url']}")
    lines.append("\nЛінки — для рідкісних носіїв: із заголовка видно, чи це "
                 "справжня посада іншого міста (тоді злиття «ні»), чи зачеп у "
                 "беку (тоді /entity_resync <id статті>).")
    out = "\n".join(lines)
    await msg.edit_text(out[:4000] + ("…" if len(out) > 4000 else ""),
                        disable_web_page_preview=True)


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
        # СКІЛЬКИ ЩЕ МОЖНА НАБРАТИ ПИТАННЯМИ. Без цього числа відсоток
        # покриття читається як шкала до 100%, а це неправда: написання, у
        # якого немає жодного кандидата на злиття, не потрапить під канон
        # ніколи — і не має, бо зливати його нема з чим. Реальне питання
        # 02.08: «дивно, що % не змінився» після десятка відповідей — уся
        # решта черги коштує кілька відсотків, і це видно тільки так.
        stake = bot_db.query(
            "SELECT count(*) AS links FROM article_entities ae "
            "WHERE role_norm(ae.role_at_time) IN ("
            "   SELECT a_norm FROM role_pairs WHERE verdict IS NULL "
            "   UNION SELECT b_norm FROM role_pairs WHERE verdict IS NULL) "
            "AND role_norm(ae.role_at_time) NOT IN "
            "   (SELECT raw_norm FROM role_variants)")[0]["links"]
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
        return tot, cov, canons, top, _pending_count(), stake

    try:
        tot, cov, canons, top, pending, stake = await asyncio.to_thread(build)
    except Exception as e:
        await msg.edit_text(f"❌ Нора недоступна: {e}")
        return
    links = tot["links"] or 0
    pct = (100 * cov // links) if links else 0
    stake_pct = (100 * stake / links) if links else 0
    lines = ["🦊 Канон ролей\n",
             f"Сирих написань: {tot['variants']} на {links} зв'язків",
             f"Зведено до канону: {canons['n']} посад, покриття {cov} зв'язків ({pct}%)",
             f"З афіліацією (орган вказано): {canons['with_org']}",
             f"У черзі питань: {pending} — на кону ще {stake} зв'язків "
             f"(+{stake_pct:.1f}% покриття, якщо звести ВСЕ)",
             "Решта написань кандидатів на злиття не має — їх нема з чим "
             "зливати, і 100% тут недосяжні за задумом."]
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

# Скільки символів між іменем і словом посади ще означає «це одна іменна
# група»: «мер Миколаєва Олександр Сєнкевич» — 14 символів, «Тодішній мер
# Миколаєва Володимир Чайка» — 23.
#
# Перший поріг був 250 і провалився на реальній статті 310211: там «Також
# Віталій Кім розповідав…» і наступним абзацом «Мер також наголосив…» (це про
# Сєнкевича) стоять за ~130 символів одне від одного — і Кім проходив би як
# законний мер. Тобто «поруч» у масштабі абзацу нічого не доводить: у новині
# абзаци й так про сусідні речі.
NEAR_CHARS = 60
# Сіра зона: посада в тексті є, але не в одній групі з іменем. Ні доказ, ні
# спростування — рівно те місце, де треба показати людині врізку.
GREY_CHARS = 300


def _prefix(word):
    """Грубий «корінь» для пошуку по тексту: відмінки міняють хвіст слова
    («Кіма», «Чайки», «мером»), а не початок. Морфології тут не треба —
    достатньо не промахнутись повз відмінок."""
    return word[:max(4, len(word) - 2)]


# Топоніми, які треба ВИКИНУТИ з перевірки близькості. Без цього вона міряє не
# посаду, а місто: у ролі «міський голова МИКОЛАЄВА» слово «Миколаєва» стоїть у
# кожному абзаці миколаївської новини, а в імені «МИКОЛА Леонтович» той самий
# корінь — і бот бачив відстань 0 там, де посади поруч немає взагалі.
# Список беремо з самої нори (entities kind='place'), а не вигадуємо.
_places = {"set": None}


def _place_prefixes():
    if _places["set"] is None:
        pref = set()
        try:
            for r in bot_db.query(
                    "SELECT lower(name_ua) AS a, lower(name_ru) AS b "
                    "FROM entities WHERE kind = 'place'"):
                for nm in (r["a"], r["b"]):
                    for w in _tokens(nm or ""):
                        if len(w) < 5:
                            continue
                        # Українська міняє корінь у непрямих відмінках:
                        # Очаків → Очакова, Львів → Львова. Тому поруч із
                        # префіксом кладемо і його «о»-варіант — це покриває
                        # найчастіше чергування. Київ → Києва так не лікується,
                        # і це прийнятно: така пара просто піде в «доповнює
                        # назву», яку однаково вирішує людина по одній.
                        pref.add(w[:5])
                        if "і" in w[:5]:
                            pref.add(w[:5].replace("і", "о"))
        except Exception as e:
            print(f"entity_roles: список топонімів недоступний — {e}")
        _places["set"] = pref
    return _places["set"]


def _drop_places(words):
    """Прибрати топоніми, але не лишити список порожнім: якщо в написанні
    самі топоніми (буває в назвах органів) — краще міряти по них, ніж ніяк."""
    pref = _place_prefixes()
    kept = [w for w in words if w[:5] not in pref]
    return kept or words


def _find_all(low, words, min_len):
    """Позиції слів у тексті — по ПОЧАТКУ слова, а не будь-де в рядку.

    Голий підрядок бреше на коротких коренях: «мер» сидить усередині
    «прем'єра», і в статті про композитора Леонтовича бот бачив посаду мера за
    6 символів від імені (реальний випадок 319027). Пошук по межі слова це
    знімає, а хвіст слова лишається вільним — саме там українська і змінює
    відмінок («мером», «Леонтовича»)."""
    out = []
    for w in words:
        if len(w) < min_len:
            continue
        pattern = r"(?<!\w)" + re.escape(_prefix(w))
        out.extend(m.start() for m in re.finditer(pattern, low))
    return out


def role_near_name(text, name, role_norm_text, window=180):
    """Наскільки близько посада стоїть до цього імені — і ВРІЗКА з тексту.

    Відстань потрібна лише щоб відсортувати; вирішує людина по врізці, бо
    жоден поріг тут не буває правильним. Приклад зі статті 310211: «Також
    Віталій Кім розповідав…» і наступним абзацом «Мер також наголосив…» — це
    130 символів і слово «мер», яке стосується Сєнкевича, а не Кіма. Машина
    цього не розрізнить, людина по врізці — за секунду.

    Повертає (відстань, чи знайдене ім'я, врізка).
    """
    if not text or not name:
        return None, False, None
    low = text.lower()
    # Прізвище буває коротким («Кім»), тому для імені поріг 3 — але топоніми
    # з обох боків геть, інакше міряємо збіг «Микола» ↔ «Миколаєва».
    name_pos = _find_all(low, _drop_places(_tokens(name.lower())), 3)
    if not name_pos:
        return None, False, None
    role_pos = _find_all(low, _drop_places(_tokens(role_norm_text or "")), 3)
    if not role_pos:
        return None, True, None
    best = min(((abs(n - r), n) for n in name_pos for r in role_pos),
               key=lambda x: x[0])
    dist, at = best
    lo = max(0, at - window // 2)
    snippet = _SPACE_RE.sub(" ", text[lo:at + window]).strip()
    return dist, True, ("…" + snippet + "…")


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
             "Рідкісний ≠ помилковий: попередник на посаді це норма (Чайка мер "
             "2001–2013, Сєнкевич з 2015). Ані число, ані період цього не "
             "розрізняють — ретроспектива про мера 2001-го виходить у 2025-му. "
             "Тому нижче врізки з тексту: остаточно вирішуєш ти, бот лише "
             "сортує й показує.\n"]

    if dup:
        lines.append(f"━ Схоже ім'я — дубль КАРТКИ або однофамілець ({len(dup)}) ━")
        lines.append("Це вісь карток, ролі ні до чого: одна людина під двома "
                     "картками. Але обережно — так само виглядає й однофамілець.")
        for r in dup:
            arts = ", ".join(str(a) for a in (r["articles"] or []))
            lines.append(f"«{r['rn']}» · {r['name']} ({r['c']}) ~ "
                         f"{r['main_name']} ({r['top']}) · статті: {arts}")
        lines.append("Знайти id карток: /entity_find <ім'я> · злити: "
                     "/entity_merge <id що лишається> <id дубля>")
        lines.append("")

    # Перевірка по ТЕКСТУ: чи стоїть посада поруч з іменем. Періоди тут не
    # рятують — стаття-ретроспектива про мера 2001 року виходить у 2025-му, і
    # відрізок часу в попередника такий самий «сучасний», як у помилки.
    def with_proximity(items):
        art_ids = sorted({a for r in items for a in (r["articles"] or [])})
        texts = {}
        if art_ids:
            for row in bot_db.query(
                    "SELECT id, coalesce(text_ua, text_ru) AS t FROM articles "
                    "WHERE id = ANY(%s)", (art_ids,)):
                texts[row["id"]] = row["t"]
        out = []
        for r in items:
            best, found_any, snip, at = None, False, None, None
            for aid in (r["articles"] or []):
                dist, found, s = role_near_name(texts.get(aid), r["name"], r["rn"])
                found_any = found_any or found
                if dist is not None and (best is None or dist < best):
                    best, snip, at = dist, s, aid
            out.append((r, best, found_any, snip, at))
        return out

    try:
        checked = await asyncio.to_thread(with_proximity, other)
    except Exception as e:
        print(f"roles_outliers: перевірка по тексту пропущена — {e}")
        checked = [(r, None, True, None, None) for r in other]

    near = [c for c in checked if c[1] is not None and c[1] <= NEAR_CHARS]
    grey = [c for c in checked
            if c[1] is not None and NEAR_CHARS < c[1] <= GREY_CHARS]
    far = [c for c in checked if c[1] is None or c[1] > GREY_CHARS]

    def block(r, tail=""):
        arts = ", ".join(str(a) for a in (r["articles"] or []))
        return (f"«{r['rn']}»\n   {r['name']} ({r['c']}) при "
                f"{r['main_name']} ({r['top']}) · статті: {arts}{tail}")

    if near:
        lines.append(f"━ Посада в одній групі з іменем ({len(near)}) ━")
        lines.append("«мер Миколаєва Володимир Чайка» — посада справді про цю "
                     "людину. Найімовірніше попередник, не чіпаємо.")
        for r, d, f, s, at in near:
            lines.append(block(r, f" · {d} симв."))
        lines.append("")

    ids = []
    if grey or far:
        lines.append(f"━ Треба глянути очима ({len(grey) + len(far)}) ━")
        lines.append("Врізка з тексту — вирішуй по ній. Так виглядала помилка у "
                     "310211: «Також Віталій Кім розповідав…» і наступним "
                     "абзацом «Мер також наголосив…» — це про Сєнкевича.")
        for r, d, f, s, at in grey + far:
            ids.extend(r["articles"] or [])
            why = ("імені немає в тексті" if not f
                   else "посади в тексті немає" if d is None else f"{d} симв.")
            lines.append(block(r, f" · {why}"))
            if s:
                lines.append(f"   {at}: {s}")
        lines.append("\nПовний текст: /nora_article <id>")
        lines.append("Полагодити (спершу правимо статтю на сайті, якщо винен "
                     "текст):\n/entity_resync "
                     + " ".join(str(i) for i in ids[:20]))
    text_out = "\n".join(lines)
    if len(text_out) > 4000:
        text_out = text_out[:3960] + "\n…(звіт обрізано, звузь: /roles_outliers 8)"
    await msg.edit_text(text_out)


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
    generic = is_generic_role(row["canon"])
    await update.message.reply_text(
        head + (ORG_QUESTION_GENERIC if generic else "\n\nЧий це орган?"),
        reply_markup=org_markup(canon_id, cands, generic))


# ---------- /roles_canon ----------

async def roles_canon_handler(update, context):
    """/roles_canon — компактний список канонів; /roles_canon <id> — один канон
    з усіма написаннями.

    Без цього поділу команда падала: канон із 51 написання плюс шість десятків
    інших не влазять у ліміт повідомлення Telegram, і огляд ставав недоступним
    рівно тоді, коли він найпотрібніший — коли треба лагодити злиплий канон."""
    if not _allowed(update):
        return
    args = context.args or []
    one = int(args[0]) if args and args[0].isdigit() else None
    # Пошук за назвою — бо в списку канон із двома написаннями стоїть у
    # хвості, а хвіст зрізав ліміт Telegram: «міський голова Києва» просто не
    # доїжджав до екрана, і виправити його орган не було як.
    find = " ".join(args).strip().lower() if args and one is None else None

    def build_one():
        ensure_schema()
        rows = bot_db.query(
            "SELECT rc.id, rc.canon, coalesce(o.name_ua, o.name_ru) AS org "
            "FROM role_canon rc LEFT JOIN entities o ON o.id = rc.org_entity_id "
            "WHERE rc.id = %s", (one,))
        if not rows:
            return None, []
        vars_ = bot_db.query(
            "SELECT rv.raw_sample, rv.raw_norm, "
            "       (SELECT count(*) FROM article_entities ae "
            "        WHERE role_norm(ae.role_at_time) = rv.raw_norm) AS links "
            "FROM role_variants rv WHERE rv.canon_id = %s "
            "ORDER BY links DESC", (one,))
        return rows[0], vars_

    def build_list():
        ensure_schema()
        # Шукаємо і за назвою канону, і за написаннями всередині: людина
        # пам'ятає ту форму, яку бачила в картці, а канон міг назватись інакше.
        where, params = "", ()
        if find:
            where = ("WHERE lower(rc.canon) LIKE %s OR rc.id IN "
                     "  (SELECT canon_id FROM role_variants "
                     "   WHERE raw_norm LIKE %s) ")
            params = (f"%{find}%", f"%{role_norm(find)}%")
        return bot_db.query(
            f"""
            SELECT rc.id, rc.canon, coalesce(o.name_ua, o.name_ru) AS org,
                   count(*) AS variants
            FROM role_canon rc
            JOIN role_variants rv ON rv.canon_id = rc.id
            LEFT JOIN entities o ON o.id = rc.org_entity_id
            {where}
            GROUP BY rc.id, rc.canon, o.name_ua, o.name_ru
            ORDER BY count(*) DESC, rc.id
            """, params)

    try:
        if one is not None:
            head, vars_ = await asyncio.to_thread(build_one)
            if not head:
                await update.message.reply_text(f"Канону {one} немає.")
                return
            org = f"\n🏛 {head['org']}" if head["org"] else "\n🏛 орган не вказано"
            lines = [f"🦊 [{head['id']}] «{head['canon']}»{org}",
                     f"Написань: {len(vars_)}\n"]
            for v in vars_:
                lines.append(f"  {v['links']:>5} · {v['raw_sample'] or v['raw_norm']}")
            lines.append("\nЗняти одне написання: /roles_forget <написання>")
            lines.append(f"Розібрати канон цілком: /roles_forget {head['id']}")
            lines.append(f"Переназвати: /roles_rename {head['id']} <текст>")
            lines.append(f"Змінити орган: /roles_org {head['id']}")
            await update.message.reply_text("\n".join(lines)[:4000])
            return
        rows = await asyncio.to_thread(build_list)
    except Exception as e:
        await update.message.reply_text(f"❌ Нора недоступна: {e}")
        return
    if not rows:
        await update.message.reply_text(
            f"За «{find}» канону не знайшлось — спробуй одне слово."
            if find else
            "Канонів ще немає — /roles_dedup поставить перші питання.")
        return
    head = (f"🦊 Канони за «{find}» — {len(rows)}\n" if find
            else f"🦊 Канон посад — {len(rows)}\n")
    lines = [head]
    for r in rows:
        org = f" · 🏛 {r['org']}" if r["org"] else ""
        lines.append(f"[{r['id']}] «{r['canon']}» — {r['variants']} написань{org}")
    lines.append("\nОдин канон із написаннями: /roles_canon <id> · "
                 "пошук: /roles_canon <слово>")
    lines.append("Переназвати: /roles_rename <id> <текст> · "
                 "відкат: /roles_forget <текст|id>")
    # РІЖЕМО НА ПОВІДОМЛЕННЯ, а не обрізаємо. Список сортований за кількістю
    # написань, тож обрізання з'їдало саме дрібні канони — а лагодити частіше
    # доводиться їх («міський голова Києва» з двома написаннями просто не
    # доїжджав до екрана).
    chunk = []
    size = 0
    for ln in lines:
        if size + len(ln) > 3800 and chunk:
            await update.message.reply_text("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(ln)
        size += len(ln) + 1
    if chunk:
        await update.message.reply_text("\n".join(chunk))


# ---------- /roles_audit: канони, названі вужче, ніж покривають ----------
#
# Код лагодився 02.08 (pick_canon бере загальніше написання, _regeneralize
# переназиває канон, щойно до нього долучилось загальніше), але вже створені
# канони фікс не чіпає: назви лишились ті, що були на момент злиття. Знайти їх
# можна лише очима по всьому списку — саме це й робиться зараз руками.
#
# Дві ознаки, обидві механічні:
#
#   1. У назві канону є ТОПОНІМ, а серед написань є таке саме без топоніма.
#      «перший заступник МИКОЛАЇВСЬКОГО міського голови» покриває «першого
#      заступника міського голови», якого носять заступники інших міст — і
#      миколаївська назва для них хибна. Лікується /roles_rename.
#   2. У назві канону одне ПРЕДМЕТНЕ слово, а в написанні інше: «культури»
#      проти «освіти» — це різні управління й різні люди. Лікується
#      /roles_forget (написання виходить із канону й повертається в чергу).
#
# Звіт read-only й друкує готові рядки команд: рішення однаково за людиною,
# але шукати їх очима більше не треба.

AUDIT_SQL = """
SELECT rc.id, rc.canon, rv.raw_norm, rv.raw_sample,
       (SELECT count(*) FROM article_entities ae
         WHERE role_norm(ae.role_at_time) = rv.raw_norm) AS links
FROM role_canon rc JOIN role_variants rv ON rv.canon_id = rc.id
ORDER BY rc.id
"""

# Слова, що означають «людина на чолі». Заміна одного з них на інше — це
# синонім, а не інша посада: «начальник управління» = «керівник управління»,
# «голова ВР» = «спікер парламенту», «мер Львова» = «міський голова Львова».
# Саме на цьому провалився перший варіант звіту: він показував клас «лексично
# різні», а в каноні це НОРМА — довідник для того й існує, щоб зводити «мера»
# з «міським головою». З 26 рядків живого прогону 23 були такими.
HEAD_WORDS = {
    "голова", "голови", "головою", "головиня", "очільник", "очільниця",
    "керівник", "керівниця", "начальник", "начальниця", "глава", "лідер",
    "мер", "мерка", "градоначальник", "спікер", "спікерка",
    "директор", "директорка", "гендиректор", "губернатор", "президент",
    "прокурор", "прокурорка", "міністр", "міністерка", "командувач",
    "секретар", "секретарка", "посол", "послиця", "ректор", "канцлер",
}

# Слова РАНГУ. Ось вони справді роблять іншу посаду, і саме такий рядок у
# живому прогоні виявився помилкою: під каноном «президент США» лежала
# «віцепрезидентка США». Ловимо префіксом, бо написання буває і через дефіс,
# і разом («віце-президентка», «віцепрезидентка»).
RANK_PREFIXES = ("віце", "заступ", "перш", "друг", "трет", "помічн", "радник",
                 "радниц", "виконувач", "тимчасов", "екс", "колишн", "старш",
                 "молодш", "во", "в.о", "т.в.о")

# Родові слова замість топоніма: «головний архітектор МІСТА» — це загальна
# форма посади «головного архітектора Миколаєва», а не інше місце. Через них
# такі написання потрапляють у ПЕРШИЙ розділ (назва канону завужена), а не в
# другий.
GENERIC_PLACES = {"міста", "місті", "місто", "області", "область", "громади",
                  "громада", "району", "район", "країни", "країна", "держави"}


def _rank_words(tokens):
    return {t for t in tokens if any(t.startswith(p) for p in RANK_PREFIXES)}


def _canon_carriers(norms):
    """{написання → множина носіїв}. Потрібно саме це: лексика не відрізняє
    синонім від чужої посади, а носії відрізняють. «Голову ВР» і «спікера
    парламенту» носить той самий Стефанчук — це синонім; «управління
    культури» і «управління освіти» не має спільного носія жодного разу — це
    різні органи."""
    if not norms:
        return {}
    out = {}
    for r in bot_db.query(
            "SELECT role_norm(role_at_time) AS rn, entity_id FROM article_entities "
            "WHERE role_norm(role_at_time) = ANY(%s) GROUP BY 1, 2", (list(norms),)):
        out.setdefault(r["rn"], set()).add(r["entity_id"])
    return out


def audit_canons():
    """(вужчі назви, чужі написання). Нічого не змінює.

    Другий розділ показує ЛИШЕ те, що механічно означає іншу посаду: ранг
    (віце/заступник/екс), інший рівень органу, інше місто — і предметну
    різницю, підтверджену тим, що спільного носія в написань немає. Раніше
    він показував клас «лексично різні», і живий прогін дав 23 хибні рядки з
    26: у каноні лексична різниця це НОРМА, заради неї довідник і зроблено."""
    ensure_schema()
    canons = {}
    for r in bot_db.query(AUDIT_SQL):
        c = canons.setdefault(r["id"], {"id": r["id"], "canon": r["canon"],
                                        "variants": []})
        c["variants"].append(r)
    carriers = _canon_carriers(
        {v["raw_norm"] for c in canons.values() for v in c["variants"]}
        | {role_norm(c["canon"]) for c in canons.values()})

    narrow, alien = [], []
    for c in canons.values():
        cn = role_norm(c["canon"]) or ""
        ct = _tokens(cn)
        if any(_is_region_word(w) for w in ct):
            # ТА САМА посада без топоніма — і саме «та сама»: написання без
            # топоніма замало. У каноні «начальник управління КУЛЬТУРИ
            # Миколаївської міської ради» лежить «керівниця управління
            # ОСВІТИ міської ради» — топоніма в ньому теж немає, але це інше
            # управління, і перейменувати канон на нього означало б назвати
            # групу чужим предметом. Тому вимагаємо, щоб решта слів написання
            # уже була в назві канону: тоді воно справді лише ширше.
            base = {w for w in ct if not _is_region_word(w)}
            bare = [v for v in c["variants"]
                    if v["raw_norm"] != cn
                    and not any(_is_region_word(w) for w in _tokens(v["raw_norm"]))
                    # Родове слово замість топоніма — НЕ загальніша форма:
                    # «головний архітектор МІСТА» у наших текстах означає
                    # Миколаїв, так його називає редакція. Перейменувати канон
                    # на цю форму означало б втратити місто без потреби, тож
                    # такі написання в розділ не беремо взагалі.
                    and not any(w in GENERIC_PLACES for w in _tokens(v["raw_norm"]))
                    # `<=`, а не `<`: написання без топоніма й так коротше за
                    # назву канону рівно на топонім — рівність тут і є той
                    # самий випадок, заради якого звіт писався.
                    and set(_tokens(v["raw_norm"])) <= base]
            if bare:
                pick = max(bare, key=lambda v: v["links"])
                narrow.append({"id": c["id"], "canon": c["canon"],
                               "suggest": pick["raw_sample"] or pick["raw_norm"],
                               "links": pick["links"], "bare": len(bare)})
        for v in c["variants"]:
            if v["raw_norm"] == cn:
                continue
            why = _alien_reason(cn, ct, v["raw_norm"], carriers)
            if why:
                alien.append({"id": c["id"], "canon": c["canon"],
                              "raw": v["raw_sample"] or v["raw_norm"],
                              "links": v["links"], "why": why[0],
                              "detail": why[1]})
    narrow.sort(key=lambda x: -x["links"])
    alien.sort(key=lambda x: (ALIEN_ORDER.index(x["why"]), -x["links"]))
    return narrow, alien


# Причини, за яких написання в каноні справді означає ІНШУ посаду. Порядок =
# порядок довіри: ранг помиляється рідко, предметна різниця — найчастіше.
ALIEN_RANK = "ранг"
ALIEN_LEVEL = "рівень"
ALIEN_PLACE = "місце"
ALIEN_SUBJECT = "предмет"
ALIEN_ORDER = [ALIEN_RANK, ALIEN_LEVEL, ALIEN_PLACE, ALIEN_SUBJECT]

ALIEN_LABELS = {
    ALIEN_RANK: "РАНГ інший — це не та сама посада",
    ALIEN_LEVEL: "ІНШИЙ РІВЕНЬ органу",
    ALIEN_PLACE: "ІНШЕ МІСЦЕ",
    ALIEN_SUBJECT: "ІНШИЙ ПРЕДМЕТ — і спільного носія немає жодного разу "
                   "(гола форма на кшталт «міста» теж сюди: її міг забрати "
                   "чужий чиновник)",
}


def _alien_reason(canon_norm, canon_tokens, raw_norm, carriers):
    """(причина, деталь) або None. Механічно — і тільки те, що механічно
    видно; синонімію («спікер парламенту» = «голова ВР») машина не розрізняє
    ніяк, тому решту звіт не показує взагалі."""
    vt = _tokens(raw_norm)
    ct = canon_tokens
    # 1. Ранг. «Президент США» ← «віцепрезидентка США»: слово рангу є з
    #    одного боку й немає з другого.
    ra, rb = _rank_words(ct), _rank_words(vt)
    if ra != rb:
        extra = " ".join(sorted(rb - ra)) or " ".join(sorted(ra - rb))
        return ALIEN_RANK, f"«{extra}» лише в одному написанні"
    ma, mb = _status_marker(canon_norm), _status_marker(raw_norm)
    if bool(ma) != bool(mb):
        return ALIEN_RANK, f"«{ma or mb}» лише в одному написанні"
    # 2. Рівень органу: «управління охорони здоров'я МІСЬКРАДИ» ←
    #    «…Миколаївської ОВА». Різні установи й різні люди.
    la = {_level_code(w) for w in ct if _is_level_word(w)}
    lb = {_level_code(w) for w in vt if _is_level_word(w)}
    if la and lb and la != lb:
        return ALIEN_LEVEL, (" ".join(sorted(w for w in ct if _is_level_word(w)))
                             + " ↔ "
                             + " ".join(sorted(w for w in vt if _is_level_word(w))))
    # 3. Місце — лише коли топонім реальний З ОБОХ боків. «Миколаєва» проти
    #    «міста» це не інше місто, а загальна форма: такий рядок належить
    #    першому розділу звіту.
    pa = {_region_code(w) for w in ct if _is_region_word(w)}
    pb = {_region_code(w) for w in vt if _is_region_word(w)}
    if pa and pb and pa != pb:
        return ALIEN_PLACE, (" ".join(sorted(w for w in ct if _is_region_word(w)))
                             + " ↔ "
                             + " ".join(sorted(w for w in vt if _is_region_word(w))))
    # 4. Предмет. Тут лексики самої по собі МАЛО: «голова ВР» ← «спікер
    #    парламенту» теж різняться предметними словами, і це синонім. Тому
    #    вимагаємо другого, незалежного сигналу — жодного спільного носія.
    #    Слова «на чолі» і рівень органу з порівняння викидаємо: заміна
    #    начальника на керівника (чи «ОВА» на «обласну військову
    #    адміністрацію») предмета не змінює.
    #
    #    Сюди ж свідомо потрапляє гола форма з родовим словом: «головний
    #    архітектор МІСТА» під каноном «…Миколаєва». Слово «міста» саме по
    #    собі нічого не вирішує — воно означає Миколаїв рівно доти, доки його
    #    носить миколаївський архітектор; щойно так названо когось із Одеси,
    #    це вже чуже написання. Тому питання вирішує носій, а не словник.
    def _content(tokens):
        return {w for w in tokens
                if w not in HEAD_WORDS and w not in FUNCTION_WORDS
                and not _is_level_word(w)}

    ca, cb = _content(ct), _content(vt)
    only_a, only_b = ca - cb, cb - ca
    if only_a and only_b:
        who_a = carriers.get(canon_norm, set())
        who_b = carriers.get(raw_norm, set())
        if who_a and who_b and not (who_a & who_b):
            return ALIEN_SUBJECT, (" ".join(sorted(only_a)) + " ↔ "
                                   + " ".join(sorted(only_b)))
    return None


def format_audit(narrow, alien):
    lines = ["🦊 Ревізія канонів (read-only, нічого не змінено)\n"]
    lines.append(f"━━ НАЗВА ВУЖЧА, НІЖ ПОКРИТТЯ — {len(narrow)} ━━")
    if not narrow:
        lines.append("Порожньо: жоден канон із топонімом не покриває голої форми.")
    for r in narrow:
        lines.append(f"\n[{r['id']}] «{r['canon']}»")
        lines.append(f"   всередині та сама посада без топоніма: «{r['suggest']}» "
                     f"({plural(r['links'], 'зв’язок', 'зв’язки', 'зв’язків')}"
                     + (f", таких написань {r['bare']}" if r["bare"] > 1 else "")
                     + ")")
        lines.append(f"   /roles_rename {r['id']} {r['suggest']}")
    lines.append(f"\n━━ ЧУЖЕ НАПИСАННЯ В КАНОНІ — {len(alien)} ━━")
    if not alien:
        lines.append("Порожньо: усі написання говорять про той самий предмет.")
    for r in alien:
        det = f" ({r['detail']})" if r["detail"] else ""
        lines.append(f"\n[{r['id']}] «{r['canon']}»")
        lines.append(f"   ← «{r['raw']}» · {ALIEN_LABELS.get(r['why'], r['why'])}"
                     f"{det} · "
                     f"{plural(r['links'], 'зв’язок', 'зв’язки', 'зв’язків')}")
        lines.append(f"   /roles_forget {r['raw']}")
    lines.append("\nОбидва списки — підозри, а не вироки: рішення за людиною. "
                 "Канон цілком видно в /roles_canon <id>.")
    lines.append("Синонімів («спікер парламенту» = «голова ВР») тут немає "
                 "свідомо: машина їх від чужої посади не відрізняє, це видно "
                 "лише по носіях — вони у файлі нижче.")
    return lines


# ---------- Обмін файлами: довідник → людина → пакет рішень ----------
#
# Механіка тут закінчується. Машина бачить, що написання лексично різні, але
# не знає, що «спікер парламенту» це «голова Верховної Ради», а «управління
# культури» — не «управління освіти». Живий прогін 02.08 дав 23 хибні рядки з
# 26 саме тому. Людина ж не може читати сотні рядків у чаті — і не мусить.
#
# Тому обмін іде ФАЙЛАМИ: бот віддає весь довідник CSV-кою (канон, написання,
# скільки зв'язків, ХТО носить — носії й розрізняють синонім від чужої
# посади), її дивиться людина або AI поза ботом, і назад приїжджає простий
# текстовий файл із рішеннями. Бот показує, ЩО зробить, і чекає кнопку —
# рішення все одно ухвалює людина, просто одним тапом замість сотні.

CANON_EXPORT_SQL = """
SELECT rc.id, rc.canon, coalesce(o.name_ua, o.name_ru) AS org,
       rv.raw_sample, rv.raw_norm,
       (SELECT count(*) FROM article_entities ae
         WHERE role_norm(ae.role_at_time) = rv.raw_norm) AS links
FROM role_canon rc
JOIN role_variants rv ON rv.canon_id = rc.id
LEFT JOIN entities o ON o.id = rc.org_entity_id
ORDER BY rc.id, links DESC
"""

CARRIER_NAMES_SQL = """
SELECT role_norm(ae.role_at_time) AS rn,
       coalesce(e.name_ua, e.name_ru) AS name, count(*) AS n
FROM article_entities ae JOIN entities e ON e.id = ae.entity_id
WHERE role_norm(ae.role_at_time) = ANY(%s)
GROUP BY 1, 2 ORDER BY 1, 3 DESC
"""


def export_canons_csv():
    """CSV усього довідника ролей: канон, написання, зв'язки, носії.

    Носії тут не для краси: саме вони відрізняють синонім («голову ВР» і
    «спікера парламенту» носить одна людина) від чужої посади («культури» й
    «освіти» не мають спільного носія жодного разу)."""
    import csv
    import io
    ensure_schema()
    rows = bot_db.query(CANON_EXPORT_SQL)
    names = {}
    for r in bot_db.query(CARRIER_NAMES_SQL,
                          ([r["raw_norm"] for r in rows],)):
        names.setdefault(r["rn"], []).append(f"{r['name']} ×{r['n']}")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["canon_id", "canon", "org", "написання", "зв'язки", "носії"])
    for r in rows:
        w.writerow([r["id"], r["canon"], r["org"] or "",
                    r["raw_sample"] or r["raw_norm"], r["links"],
                    " | ".join(names.get(r["raw_norm"], [])[:4])])
    canons = len({r["id"] for r in rows})
    return ("﻿" + buf.getvalue()).encode("utf-8"), canons, len(rows)


# Маркер пакета рішень. Потрібен, щоб бот не сплутав файл ні з чим іншим:
# у приват йому кидають ще й пакети рішень міськради (.zip/.xlsx) і снапшоти
# бюджету. Обробник бере ЛИШЕ .txt і ЛИШЕ з цим рядком усередині.
FIX_MARKER = "roles-fix"

FIX_OPS = {"forget", "attach", "rename", "merge", "org"}


def parse_fix(text):
    """Текст пакета → (дії, помилки). Формат навмисно тупий, рядок = дія:

        # roles-fix
        forget керівниця управління освіти міської ради
        attach 35 директор департаменту ЖКГ Миколаєва
        rename 29 начальник управління культури
        merge 29 60
        org 29 4711        (0 — зняти орган)

    `attach` — це перевішування написання на правильний канон. Без нього
    єдиним способом прибрати чуже написання було `forget`, тобто викинути
    його в чергу питань і чекати, поки детектор колись запропонує потрібну
    пару. Живий випадок: під «заступником директора департаменту ЖКГ» лежали
    написання про самого ДИРЕКТОРА, а канон директора вже існував поруч —
    єдине, чого бракувало, це переставити рядок.

    Рядки, скопійовані просто зі звіту («/roles_forget …»), теж розуміються:
    у них лише префікс інший, і змушувати людину його прибирати немає сенсу.
    """
    actions, errors = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/roles_"):
            line = line[len("/roles_"):]
        parts = line.split(None, 1)
        op = parts[0].lower().rstrip(":")
        rest = parts[1].strip() if len(parts) > 1 else ""
        if op not in FIX_OPS:
            errors.append(f"не знаю дії: {line[:60]}")
            continue
        if op == "forget":
            if not rest:
                errors.append(f"порожній forget: {line[:60]}")
                continue
            actions.append(("forget", rest))
        elif op == "attach":
            bits = rest.split(None, 1)
            if len(bits) < 2 or not bits[0].isdigit():
                errors.append(f"attach очікує <id канону> <написання>: {line[:60]}")
                continue
            actions.append(("attach", int(bits[0]), bits[1].strip()))
        elif op == "rename":
            bits = rest.split(None, 1)
            if len(bits) < 2 or not bits[0].isdigit():
                errors.append(f"rename очікує <id> <назва>: {line[:60]}")
                continue
            actions.append(("rename", int(bits[0]), bits[1].strip()))
        elif op == "merge":
            bits = rest.split()
            if len(bits) < 2 or not all(b.isdigit() for b in bits[:2]):
                errors.append(f"merge очікує <id> <id>: {line[:60]}")
                continue
            actions.append(("merge", int(bits[0]), int(bits[1])))
        else:
            bits = rest.split()
            if len(bits) < 2 or not all(b.isdigit() for b in bits[:2]):
                errors.append(f"org очікує <id канону> <id сутності|0>: {line[:60]}")
                continue
            actions.append(("org", int(bits[0]), int(bits[1])))
    return actions, errors


def describe_fixes(actions):
    """Що саме станеться — рядками, ПЕРЕД тим як щось робити. Числа тягнемо з
    нори: у пакеті їх немає, а «зняти написання» без кількості зв'язків
    неможливо оцінити."""
    ensure_schema()
    lines, counts = [], {"forget": 0, "attach": 0, "rename": 0, "merge": 0,
                         "org": 0}
    for a in actions:
        counts[a[0]] += 1
        if a[0] == "forget":
            if a[1].strip().isdigit():
                n = bot_db.query(
                    "SELECT count(*) AS n FROM role_variants WHERE canon_id = %s",
                    (int(a[1]),))[0]["n"]
                lines.append(f"🗑 розібрати канон {a[1]} ({n} написань у чергу)")
            else:
                rn = role_norm(a[1])
                links = bot_db.query(
                    "SELECT count(*) AS n FROM article_entities "
                    "WHERE role_norm(role_at_time) = %s", (rn,))[0]["n"]
                known = bot_db.query(
                    "SELECT canon_id FROM role_variants WHERE raw_norm = %s", (rn,))
                where = f" з канону {known[0]['canon_id']}" if known else " (у довіднику немає)"
                lines.append(f"🗑 зняти «{a[1]}»{where} · {links} зв'язків")
        elif a[0] == "attach":
            rn = role_norm(a[2])
            links = bot_db.query(
                "SELECT count(*) AS n FROM article_entities "
                "WHERE role_norm(role_at_time) = %s", (rn,))[0]["n"]
            now_in = bot_db.query(
                "SELECT rc.id, rc.canon FROM role_variants rv "
                "JOIN role_canon rc ON rc.id = rv.canon_id WHERE rv.raw_norm = %s",
                (rn,))
            to = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (a[1],))
            # Показуємо, ЗВІДКИ забираємо: перевішування з чужого канону і
            # додавання нового написання — різні за наслідками дії, а рядок
            # у файлі однаковий.
            src = (f" (зараз у [{now_in[0]['id']}] «{now_in[0]['canon']}»)"
                   if now_in else " (зараз без канону)")
            dst = f"«{to[0]['canon']}»" if to else f"канону {a[1]} НЕМАЄ"
            lines.append(f"↪️ «{a[2]}»{src} → [{a[1]}] {dst} · {links} зв'язків")
        elif a[0] == "rename":
            cur = bot_db.query("SELECT canon FROM role_canon WHERE id = %s", (a[1],))
            was = f"«{cur[0]['canon']}» " if cur else ""
            lines.append(f"✏️ [{a[1]}] {was} → «{a[2]}»")
        elif a[0] == "merge":
            p, err = canon_merge_preview(a[1], a[2])
            lines.append(f"🔗 {a[1]} ← {a[2]}: " +
                         (err if err else
                          f"{p['b']['variants']} написань переїдуть, "
                          f"результат «{p['name']}»"))
        else:
            who = bot_db.query(
                "SELECT coalesce(name_ua, name_ru) AS n FROM entities WHERE id = %s",
                (a[2],)) if a[2] else None
            lines.append(f"🏛 [{a[1]}] орган → "
                         + (who[0]["n"] if who else ("знято" if not a[2] else str(a[2]))))
    return lines, counts


def apply_fixes(actions, decided_by=None):
    """Виконати пакет. Кожна дія окремо: збійна не валить решту, її причина
    їде у звіт — інакше одна помилка в рядку викидала б усю роботу."""
    ensure_schema()
    done, failed = [], []
    for a in actions:
        try:
            if a[0] == "forget":
                kind, n, extra = forget_one(a[1])
                done.append(f"🗑 {a[1]}" if n else f"— {a[1]} (не було)")
            elif a[0] == "attach":
                if not bot_db.query("SELECT 1 AS x FROM role_canon WHERE id = %s",
                                    (a[1],)):
                    failed.append(f"attach {a[1]} — такого канону немає")
                    continue
                rn = role_norm(a[2])
                bot_db.execute(
                    "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, "
                    "decided_by, created) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (raw_norm) DO UPDATE SET canon_id = EXCLUDED.canon_id, "
                    "raw_sample = EXCLUDED.raw_sample, decided_by = EXCLUDED.decided_by",
                    (rn, a[2], a[1], decided_by, int(time.time())))
                # Канон міг лишитись порожнім, якщо забрали його останнє
                # написання, — прибираємо, як це робить /roles_forget.
                bot_db.execute(
                    "DELETE FROM role_canon rc WHERE NOT EXISTS "
                    "(SELECT 1 FROM role_variants rv WHERE rv.canon_id = rc.id)")
                # Назва канону-приймача мусить лишитись істинною для нового
                # написання теж — це те саме правило, що при злитті.
                _regeneralize(a[1])
                done.append(f"↪️ {a[2]} → {a[1]}")
            elif a[0] == "rename":
                n = bot_db.execute(
                    "UPDATE role_canon SET canon = %s, canon_norm = %s WHERE id = %s",
                    (a[2], role_norm(a[2]), a[1]))
                done.append(f"✏️ {a[1]} → {a[2]}" if n else f"— канону {a[1]} немає")
            elif a[0] == "merge":
                cid, canon = merge_canons(a[1], a[2], decided_by)
                done.append(f"🔗 {a[2]} → {a[1]} «{canon}»")
            else:
                n = bot_db.execute(
                    "UPDATE role_canon SET org_entity_id = %s WHERE id = %s",
                    (a[2] or None, a[1]))
                done.append(f"🏛 {a[1]} → {a[2] or 'знято'}"
                            if n else f"— канону {a[1]} немає")
        except Exception as e:
            failed.append(f"{' '.join(str(x) for x in a)[:60]} — {type(e).__name__}: {e}")
    return done, failed


FIX_STATE_PREFIX = "rolesfix:"


def _fix_key(chat_id, message_id):
    return f"{FIX_STATE_PREFIX}{chat_id}:{message_id}"


async def roles_fix_document(update, context):
    """Пакет рішень .txt у приваті — читаємо, показуємо ЩО зробимо, чекаємо
    кнопку. Файли без маркера ігноруємо мовчки: у приват боту кидають ще й
    бюджетні пакети, і плутати їх не можна."""
    import json
    msg = update.effective_message
    doc = msg.document if msg else None
    if not doc or not _allowed(update):
        return
    if not (doc.file_name or "").lower().endswith((".txt", ".roles")):
        return
    f = await context.bot.get_file(doc.file_id)
    data = bytes(await f.download_as_bytearray())
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return
    head = text[:400].lower()
    if "cards-fix" in head:
        # Пакет про КАРТКИ — інша вісь, інший модуль. Розбирати його тут не
        # можна, а мовчки проковтнути не можна тим більше: обробник .txt у
        # боті один, і другий до файла вже не дійде.
        from handlers import entity_junk as ej
        await ej.cards_fix_document(msg, text)
        return
    if FIX_MARKER not in head:
        return          # не наш файл — мовчки повз
    actions, errors = parse_fix(text)
    if not actions:
        await msg.reply_text(
            "🦊 Пакет порожній — жодного рядка не розібрав.\n"
            + ("\n".join(errors[:5]) if errors else ""))
        return
    try:
        lines, counts = await asyncio.to_thread(describe_fixes, actions)
    except Exception as e:
        await msg.reply_text(f"❌ Не змалював пакет: {type(e).__name__}: {e}")
        return
    head = (f"🦊 Пакет рішень: зняти {counts['forget']} · перевісити "
            f"{counts['attach']} · переназвати {counts['rename']} · звести "
            f"{counts['merge']} · орган {counts['org']}\n")
    body = "\n".join(lines[:40])
    if len(lines) > 40:
        body += f"\n…і ще {len(lines) - 40} дій"
    if errors:
        body += "\n\n⚠️ не розібрав:\n" + "\n".join(errors[:5])
    sent = await msg.reply_text(
        head + "\n" + body + "\n\nСирі ролі в статтях не змінюються в жодному "
                             "разі — це рядки довідника.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Виконати {len(actions)}", callback_data="rfx:go"),
            InlineKeyboardButton("❌ Ні", callback_data="rfx:no"),
        ]]))
    await asyncio.to_thread(
        bot_db.set_state, _fix_key(sent.chat_id, sent.message_id),
        json.dumps(actions, ensure_ascii=False))


async def roles_fix_callback(update, context):
    import json
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    await query.answer()
    key = _fix_key(query.message.chat_id, query.message.message_id)
    if query.data == "rfx:no":
        await asyncio.to_thread(bot_db.execute,
                                "DELETE FROM sync_state WHERE key = %s", (key,))
        await query.edit_message_text("❌ Скасовано, довідник не чіпав.")
        return
    raw = await asyncio.to_thread(bot_db.get_state, key)
    if not raw:
        await query.edit_message_text("Пакет застарів — надішли файл ще раз.")
        return
    actions = [tuple(a) for a in json.loads(raw)]
    await query.edit_message_text(f"🦊 Виконую {len(actions)} дій…")
    who = query.from_user.full_name if query.from_user else None
    try:
        done, failed = await asyncio.to_thread(apply_fixes, actions, who)
    except Exception as e:
        await query.edit_message_text(f"❌ Пакет не пройшов: {type(e).__name__}: {e}")
        return
    await asyncio.to_thread(bot_db.execute,
                            "DELETE FROM sync_state WHERE key = %s", (key,))
    text = f"🦊 Виконано {len(done)} дій.\n" + "\n".join(done[:30])
    if len(done) > 30:
        text += f"\n…і ще {len(done) - 30}"
    if failed:
        text += "\n\n❌ не вийшло:\n" + "\n".join(failed[:5])
    text += "\n\nСтан: /roles · перегляд канону: /roles_canon <id>"
    await query.edit_message_text(text[:4000])


async def roles_audit_handler(update, context):
    """/roles_audit — знайти канони, названі вужче, ніж покривають, і чужі
    написання всередині. Read-only, з готовими рядками команд ремонту."""
    if not _allowed(update):
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора недоступна (BOT_DATABASE_URL).")
        return
    msg = await update.message.reply_text("🦊 Переглядаю канони…")
    try:
        narrow, alien = await asyncio.to_thread(audit_canons)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {type(e).__name__}: {e}")
        return
    lines = format_audit(narrow, alien)
    # Ріжемо на повідомлення, а не обрізаємо: підозри відсортовані за вагою,
    # і хвіст — це дрібні канони, які лагодити доводиться найчастіше.
    first, chunk, size = True, [], 0
    for ln in lines:
        if size + len(ln) > 3800 and chunk:
            text = "\n".join(chunk)
            await (msg.edit_text(text) if first else
                   update.message.reply_text(text))
            first, chunk, size = False, [], 0
        chunk.append(ln)
        size += len(ln) + 1
    if chunk:
        text = "\n".join(chunk)
        await (msg.edit_text(text) if first else update.message.reply_text(text))

    # І весь довідник файлом. Механіка бачить лише те, що видно механічно;
    # синонімію («спікер парламенту» = «голова ВР») відрізняє людина або AI,
    # якому цей файл віддають. Назад приїжджає пакет рішень — теж файлом.
    import io
    try:
        data, canons, variants = await asyncio.to_thread(export_canons_csv)
    except Exception as e:
        await update.message.reply_text(f"(довідник файлом не віддався: {e})")
        return
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"roles_canons_{canons}.csv",
        caption=(f"🦊 Довідник цілком: {canons} канонів, {variants} написань, "
                 f"з носіями кожного.\n\nЦе для розбору поза ботом: у колонці "
                 f"«носії» видно, чи це синонім (той самий носій), чи чужа "
                 f"посада (носії різні). Назад — файл .txt із рядком "
                 f"«# roles-fix» і рішеннями; кину його сюди — покажу, що "
                 f"зроблю, і спитаю кнопкою."))


# ---------- /roles_merge: злиття двох КАНОНІВ ----------
#
# Канони досі об'єднувались лише побічним ефектом: людина відповідала «так»
# на пару НАПИСАНЬ, і якщо обидва вже під канонами, канони зчіплювались. Але
# два канони про один орган («начальник управління культури Миколаївської
# міської ради» і «начальник управління культури Миколаєва») можуть не мати
# жодної спільної пари в черзі — і звести їх нічим.
#
# Запобіжники ті самі, що в /entity_merge: прев'ю з підставою (скільки
# написань і зв'язків на кону, який орган лишиться, як зватиметься результат)
# і рішення тапом по кнопці, а не самим фактом набору команди — id набирають
# руками, і одна цифра помилки зчіплює не ті посади.
#
# Сирий role_at_time при цьому не змінюється жодного разу: усе, що
# відбувається, — переїзд рядків довідника.

CANON_CARD_SQL = """
SELECT rc.id, rc.canon, rc.org_entity_id,
       coalesce(o.name_ua, o.name_ru) AS org,
       (SELECT count(*) FROM role_variants rv WHERE rv.canon_id = rc.id) AS variants,
       (SELECT count(*) FROM article_entities ae
          JOIN role_variants rv2 ON rv2.raw_norm = role_norm(ae.role_at_time)
         WHERE rv2.canon_id = rc.id) AS links
FROM role_canon rc LEFT JOIN entities o ON o.id = rc.org_entity_id
WHERE rc.id = %s
"""


def canon_merge_preview(keep, drop):
    """(дані прев'ю, помилка). Назва результату рахується тим самим
    `_general_name`, який поставить її насправді."""
    ensure_schema()
    a = bot_db.query(CANON_CARD_SQL, (keep,))
    b = bot_db.query(CANON_CARD_SQL, (drop,))
    if not a or not b:
        missing = [i for i, r in ((keep, a), (drop, b)) if not r]
        return None, f"немає канонів: {', '.join(str(i) for i in missing)}"
    a, b = a[0], b[0]
    vars_ = bot_db.query(
        "SELECT raw_norm, raw_sample, canon_id FROM role_variants "
        "WHERE canon_id = ANY(%s)", ([keep, drop],))
    name = _general_name(a["canon"], [(v["raw_norm"], v["raw_sample"])
                                      for v in vars_])
    org = a["org"] or b["org"]
    org_from = "" if a["org"] or not b["org"] else " (підтягнеться з другого)"
    sample = [v["raw_sample"] or v["raw_norm"]
              for v in vars_ if v["canon_id"] == drop][:6]
    return {"a": a, "b": b, "name": name, "org": org, "org_from": org_from,
            "moving": sample}, None


def format_canon_preview(p):
    a, b = p["a"], p["b"]

    def card(mark, c, tail):
        org = f" · 🏛 {c['org']}" if c["org"] else " · орган не вказано"
        return (f"{mark} [{c['id']}] «{c['canon']}»{org}\n"
                f"   {plural(c['variants'], 'написання', 'написання', 'написань')}"
                f" · {plural(c['links'], 'зв’язок', 'зв’язки', 'зв’язків')}\n"
                f"   {tail}")

    moving = "\n".join(f"   · {s}" for s in p["moving"])
    return (
        "🦊 Звести ці канони посад?\n\n"
        + card("✅", a, "ЛИШАЄТЬСЯ") + "\n"
        + card("🗑", b, "написання переїдуть сюди ↑, канон зникне") + "\n\n"
        + (f"Переїжджають:\n{moving}\n\n" if moving else "")
        + f"Після зведення: «{p['name']}»\n"
        + f"Орган: {p['org'] or '—'}{p['org_from']}\n\n"
        + "Назва береться ЗАГАЛЬНІША — вона мусить лишитись істинною для "
          "кожного написання всередині.\n"
        + "Сирі ролі в статтях не змінюються: це переїзд рядків довідника.\n"
        + "Щоб лишився ІНШИЙ канон — поміняй id місцями в команді."
    )


async def roles_merge_handler(update, context):
    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not all(a.isdigit() for a in args[:2]):
        await update.message.reply_text(
            "Формат: /roles_merge <id що ЛИШАЄТЬСЯ> <id другого канону>\n"
            "Напр.: /roles_merge 29 60\n\n"
            "id підкаже /roles_canon (або /roles_canon <слово>).\n"
            "Зводить КАНОНИ цілком — коли про один орган їх завелось два. "
            "Одне написання знімається окремо: /roles_forget <написання>")
        return
    keep, drop = int(args[0]), int(args[1])
    if keep == drop:
        await update.message.reply_text("Це той самий канон.")
        return
    try:
        p, err = await asyncio.to_thread(canon_merge_preview, keep, drop)
    except Exception as e:
        await update.message.reply_text(f"❌ {type(e).__name__}: {e}")
        return
    if err:
        await update.message.reply_text(f"🦊 Не зводжу: {err}")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Звести", callback_data=f"rmc:{keep}:{drop}"),
        InlineKeyboardButton("❌ Скасувати", callback_data="rmc:0:0"),
    ]])
    await update.message.reply_text(format_canon_preview(p), reply_markup=kb)


async def roles_merge_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id if query.from_user else None
    if _ALLOWED_USER_IDS and user_id not in _ALLOWED_USER_IDS:
        await query.answer("⛔ Тільки для редакції.", show_alert=True)
        return
    try:
        _, k, d = query.data.split(":", 2)
        keep, drop = int(k), int(d)
    except (ValueError, AttributeError):
        await query.answer()
        return
    await query.answer()
    base = (query.message.text or "").split("\n\nЩоб лишився")[0]
    if not keep:
        await query.edit_message_text(base + "\n\n❌ Скасовано.")
        return
    who = query.from_user.full_name if query.from_user else None
    try:
        canon_id, canon = await asyncio.to_thread(merge_canons, keep, drop, who)
    except Exception as e:
        await query.edit_message_text(base + f"\n\n❌ Не звелось: {e}")
        return
    await query.edit_message_text(
        base + f"\n\n✅ Зведено в [{canon_id}] «{canon}».\n"
               f"Перегляд: /roles_canon {canon_id}\n"
               f"Переназвати: /roles_rename {canon_id} <текст>\n"
               f"Зняти написання назад у чергу: /roles_forget <написання>")


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

def forget_one(arg):
    """Зняти написання з канону (текстом) або розібрати канон цілком (числом).
    Спільне ядро /roles_forget і пакета рішень файлом — рішення однакове,
    хоч тапнуте, хоч приїхало рядком."""
    ensure_schema()
    arg = (arg or "").strip()
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

    try:
        kind, n, extra = await asyncio.to_thread(forget_one, arg)
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


# Скільки пар останній гурт лишив людині (злиття двох готових канонів).
_bulk_skipped = {"n": 0}


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
        done = skipped = 0
        for r in rows:
            # ГУРТ НЕ ЗЧІПЛЮЄ ДВА ГОТОВІ КАНОНИ. Одне «звести всі» по класу з
            # тридцяти пар інакше збирає ланцюжок A→B→C→D і робить канон на
            # півсотні написань: саме так двічі за сесію зліпились усі ОВА
            # країни — спершу під херсонською назвою, потім під одеською.
            # Пара, що об'єднує два канони, — це рішення про десятки написань
            # одразу, і воно має ухвалюватись поштучно, з попередженням у
            # картці. Такі пари лишаються в черзі.
            ca, cb = _variant_canon(r["a_norm"]), _variant_canon(r["b_norm"])
            if ca and cb and ca != cb:
                skipped += 1
                continue
            try:
                merge_roles(r["a_norm"], r["b_norm"],
                            samples.get(r["a_norm"]), samples.get(r["b_norm"]),
                            decided_by)
                set_verdict(r["id"], "same", decided_by)
                done += 1
            except Exception as e:
                print(f"roles_bulk: пара {r['id']} не звелась — {e}")
        _bulk_skipped["n"] = skipped
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
    msg = await update.message.reply_text(
        "🦊 Перераховую класи за нинішніми правилами…")
    try:
        # ОБОВ'ЯЗКОВО перед показом: /roles_bulk раніше лише читав збережені
        # класи, а перераховував їх тільки /roles_dedup. Через це після зміни
        # детектора в меню світились СТАРІ класи — і «відхилити всі» по класу
        # «ІНШЕ МІСЦЕ» закрило б назавжди пари на кшталт «міністр оборони» ~
        # «міністр оборони УКРАЇНИ», які за новими правилами належать до
        # «нашої країни» і мають зливатись. Гурт по застарілій розкладці —
        # найдорожча з можливих помилок, бо «ні» пам'ятається назавжди.
        await asyncio.to_thread(scan_pairs, MIN_LINKS, TOP_ROLES)
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
        bulkable = cls in BULK_MERGE or cls in BULK_REJECT
        hint = ("Зазвичай тут «так»." if cls in BULK_MERGE else
                "Зазвичай тут «ні»." if cls in BULK_REJECT else
                "Клас ДВОЇСТИЙ: у ньому і злиття, і різні посади — рішення "
                "різне для кожної пари, тому кнопок гурту тут немає. "
                "Проходити через /roles_dedup по одній.")
        lines.append("\n" + hint)
        # Кнопки гурту показуємо ТІЛЬКИ там, де гурт дозволений. Інакше під
        # класом, який сам себе називає двоїстим, усе одно висить «Звести всі»
        # — і одного випадкового тапу досить, щоб зліпити «журналіста» з
        # «журналістом-розслідувачем» назавжди.
        kb = []
        if bulkable:
            kb.append([InlineKeyboardButton(f"✅ Звести всі {len(rows)}",
                                            callback_data=f"rbm:{cls}"),
                       InlineKeyboardButton(f"❌ Відхилити всі {len(rows)}",
                                            callback_data=f"rbr:{cls}")])
        kb.append([InlineKeyboardButton(
            f"👉 Пройти по одній ({len(rows)})", callback_data=f"rbo:{cls}")])
        kb.append([InlineKeyboardButton("← Класи", callback_data="rbc:*")])
        await query.edit_message_text("\n".join(lines)[:4000],
                                      reply_markup=InlineKeyboardMarkup(kb))
        return

    if action == "rbo":
        _class_filter[user_id] = cls
        _skipped.pop(user_id, None)
        await query.edit_message_text(
            (query.message.text or "").split("\n\nЗазвичай")[0].split(
                "\n\nКлас ДВОЇСТИЙ")[0]
            + f"\n\n👉 Проходимо клас по одній парі.")
        await _send_next(query.message.chat, user_id)
        return

    if action not in ("rbm", "rbr"):
        return
    if cls not in BULK_MERGE and cls not in BULK_REJECT:
        # Другий запобіжник: кнопка могла лишитись у старому повідомленні,
        # відкритому до того, як клас перевели в «по одній».
        await query.answer("Цей клас гуртом не закривається — /roles_dedup.",
                           show_alert=True)
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
    tail = ""
    if verdict == "same" and _bulk_skipped["n"]:
        tail = (f"\n⚠️ {_bulk_skipped['n']} пар лишив у черзі: вони зчіплюють "
                f"ДВА готові канони, а це рішення про десятки написань одразу "
                f"— тільки поштучно, з попередженням у картці.")
    counts = await asyncio.to_thread(class_counts)
    await query.edit_message_text(
        f"🦊 Клас «{CLASS_LABELS.get(cls, cls)}»: {n} пар {what}.{tail}\n"
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
