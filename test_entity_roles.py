"""
Тест канону ролей і журналу злиттів на ЖИВОМУ Postgres
(handlers/entity_roles.py + handlers/entity_merge.py + /entity_dedup).

Не мок: піднімає реальну схему нори, кладе статті, сутності і зв'язки з
ролями — і перевіряє рівно ті інваріанти, заради яких ENTITY_MERGE_PLAN
взагалі писався.

**Головні інваріанти (кожен — окремий спосіб зіпсувати дані назавжди):**

- `article_entities.role_at_time` НЕ переписується ніколи: канон лягає поруч
  довідником. Сира роль — факт статті, вона показує, як людину називали ТОДІ;
  переписав — і траєкторія посад перетворилась на плоский список.
- відкат ролі тривіальний: `/roles_forget` знімає рядок довідника, сирий текст
  на місці, а пара повертається в чергу питань.
- «ні, різні» пам'ятається НАЗАВЖДИ — інакше та сама пара спливала б щотижня
  і людина перестала б відповідати.
- нормалізація ролі однакова в Python і в SQL. Розійдуться — довідник почне
  промахуватись повз половину зв'язків мовчки, тому звіряємо їх на КОЖНОМУ
  написанні, а не на вигаданих зразках.
- злиття КАРТОК (/entity_dedup) лишає знімок, з якого програшна картка
  відновлюється повністю: id, імена, аліаси, зв'язки з їхніми role_at_time.
  Аліаси переможця при цьому не губляться, агрегати перераховуються з даних.

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_entity_roles.py
"""

import asyncio
import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, entity_roles as er, entity_merge as em  # noqa: E402
from handlers import entity_layer as el                              # noqa: E402
import entity_pipeline as ep                                         # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ---------- дані ----------
#
# Сєнкевич — той самий мер під трьома написаннями в ПЕРЕТИННІ періоди
# (сильний сигнал: один носій, різні строки ролі, час той самий).
# Кім — зміна посади: голова ОВА → міністр. Періоди НЕ перетинаються, спільного
# слова немає, схожості написання немає → детектор не має пропонувати їх злити,
# бо це §2 плану в чистому вигляді.
ARTICLES = [(1000 + i, 1700000000 + i * 86400 * 30) for i in range(24)]

LINKS = [
    # (article_idx, entity_id, role_at_time)
    # мер Миколаєва — три написання, роки 0..8 (перетинаються)
    (0, 1, "міський голова Миколаєва"),
    (1, 1, "мер Миколаєва"),
    (2, 1, "Миколаївський міський голова"),
    (3, 1, "міський голова Миколаєва"),
    (4, 1, "мер Миколаєва"),
    (5, 1, "Миколаївський міський голова"),
    (6, 1, "міський голова  Миколаєва"),      # подвійний пробіл
    (7, 1, "«мер» Миколаєва"),                 # лапки
    (8, 1, "мер Миколаєва."),                  # крапка в кінці
    # Кім: голова ОВА (роки 9..13), далі міністр (роки 18..23) — розрив
    (9, 2, "голова Миколаївської ОВА"),
    (10, 2, "керівник Миколаївської ОВА"),
    (11, 2, "голова Миколаївської ОВА"),
    (12, 2, "керівник Миколаївської ОВА"),
    (13, 2, "голова Миколаївської ОВА"),
    (18, 2, "міністр у справах ветеранів"),
    (19, 2, "міністр у справах ветеранів"),
    (20, 2, "міністр у справах ветеранів"),
    # депутатка — роль, яку ні з чим не плутають
    (14, 3, "депутатка міської ради"),
    (15, 3, "депутатка міської ради"),
    (16, 3, "депутатка міської ради"),
    # ІНША людина під вкладеним написанням: «депутатка міської ради» ⊂
    # «депутатка міської ради Львова». Спільного носія немає — рівно той клас
    # «уточнення», який на реальних даних забивав верх черги.
    (21, 5, "депутатка міської ради Львова"),
    (22, 5, "депутатка міської ради Львова"),
    (23, 5, "депутатка міської ради Львова"),
    # орган, який має спливти як афіліація мера
    (0, 4, None), (1, 4, None), (2, 4, None), (3, 4, None), (4, 4, None),
]

ENTITIES = [
    (1, "person", "Олександр Сєнкевич", "Александр Сенкевич"),
    (2, "person", "Віталій Кім", "Виталий Ким"),
    (3, "person", "Аліна Квітко", None),
    (4, "org", "Миколаївська міська рада", "Николаевский городской совет"),
    (5, "person", "Оксана Львівська", None),
]


def setup():
    bot_db.ensure_schema()
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute("DELETE FROM article_entities")
        cur.execute("DELETE FROM entities")
        cur.execute("DELETE FROM articles")
        cur.execute("DROP VIEW IF EXISTS v_entity_roles")
        cur.execute("DROP TABLE IF EXISTS role_variants")
        cur.execute("DROP TABLE IF EXISTS role_canon")
        cur.execute("DROP TABLE IF EXISTS role_pairs")
        cur.execute("DROP TABLE IF EXISTS entity_merges")
    conn.close()
    er.ensure_schema(force=True)
    em.ensure_schema(force=True)

    for aid, pub in ARTICLES:
        bot_db.execute(
            "INSERT INTO articles (id, published, title_ua, text_ua) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (aid, pub, f"Стаття {aid}", f"Текст {aid}"))
    for eid, kind, ua, ru in ENTITIES:
        bot_db.execute(
            "INSERT INTO entities (id, kind, name_ua, name_ru) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (eid, kind, ua, ru))
    for idx, eid, role in LINKS:
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES (%s, %s, %s, 'main') ON CONFLICT DO NOTHING",
            (ARTICLES[idx][0], eid, role))
    bot_db.execute(
        "SELECT setval(pg_get_serial_sequence('entities', 'id'), "
        "GREATEST((SELECT max(id) FROM entities), 1))")
    # Агрегати рахуємо з даних, як це робить write_results: без них mentions
    # усюди 0, і /entity_dedup обрав би переможцем випадкову картку.
    bot_db.execute(em.RECALC_AGG_SQL, ([e[0] for e in ENTITIES] + [99],))
    bot_db.execute(em.RECALC_ROLE_SQL, ([e[0] for e in ENTITIES] + [99],))


def raw_roles_snapshot():
    return sorted(
        (r["article_id"], r["entity_id"], r["role_at_time"])
        for r in bot_db.query(
            "SELECT article_id, entity_id, role_at_time FROM article_entities"))


# ---------- перевірки ----------

def test_norm_matches_sql():
    """Python і SQL нормалізують ролі однаково — на КОЖНОМУ написанні з нори
    плюс на патологічних зразках (нерозривний пробіл, тире, лапки, крапка)."""
    extra = ["в.о. голови  ОВА", "екс—мер Миколаєва", "  «Голова» ,",
             "ГОЛОВА\tОВА\n", "директор — департаменту"]
    rows = bot_db.query(
        "SELECT DISTINCT role_at_time AS r FROM article_entities "
        "WHERE role_at_time IS NOT NULL")
    samples = [r["r"] for r in rows] + extra
    bad = []
    for s in samples:
        sql = bot_db.query("SELECT role_norm(%s) AS n", (s,))[0]["n"]
        if er.role_norm(s) != sql:
            bad.append((s, er.role_norm(s), sql))
    check("нормалізація ролі: Python == SQL на всіх написаннях",
          not bad, f"розбіжності: {bad[:3]}" if bad else f"звірено {len(samples)}")


def test_detector():
    n_roles, pairs = er.find_pairs(min_links=1, top_roles=200)
    got = {(r[0], r[1]) for r in pairs}

    def has(x, y):
        return (x, y) in got or (y, x) in got

    check("детектор бачить «мер Миколаєва» ~ «міський голова миколаєва»",
          has("мер миколаєва", "міський голова миколаєва"),
          f"пар: {len(pairs)}")
    check("детектор бачить «мер» ~ «миколаївський міський голова»",
          has("мер миколаєва", "миколаївський міський голова"))
    check("детектор бачить «голова ОВА» ~ «керівник ОВА»",
          has("голова миколаївської ова", "керівник миколаївської ова"))
    # §2 плану: зміна посади (ОВА → міністр) — НЕ кандидат на злиття ролей
    check("зміна посади (ОВА → міністр) кандидатом НЕ стає",
          not has("голова миколаївської ова", "міністр у справах ветеранів"),
          "розрив у часі, спільних слів немає")
    dep = {(a, b) for a, b in got if "депутатка" in a or "депутатка" in b}
    check("стороння роль (депутатка) чіпляється лише до свого уточнення",
          dep == {("депутатка міської ради", "депутатка міської ради львова")},
          f"{dep}")
    # §3.1: контекст (спільний носій у перетинні періоди) має важити більше за
    # голу схожість рядків — інакше нагору черги полізуть однослівні збіги.
    score = {}
    for a, b, sc, *_ in pairs:
        score[(a, b)] = score[(b, a)] = sc
    real = score.get(("мер миколаєва", "міський голова миколаєва"))
    lexical = score.get(("голова миколаївської ова", "миколаївський міський голова"))
    check("спільний носій важить більше за голу схожість написання",
          real is not None and lexical is not None and real > lexical,
          f"носій {real} vs схожість {lexical}")


def test_classes():
    """Класи кандидатів (крок А після заміру 02.08). Кожен рядок — реальний
    приклад із заміру по норі: саме на них вирішувалось, що черга «одна пара —
    одне питання» не тягне і потрібна розкладка."""
    cases = [
        # перестановка слів — 76% схожих пар по картках, дубль механічно
        ("проспект центральний", "центральний проспект", "permutation"),
        ("вулиця соборна", "соборна вулиця", "permutation"),
        ("державна служба з надзвичайних ситуацій україни",
         "державна служба україни з надзвичайних ситуацій", "permutation"),
        # скорочення / розгортання
        ("голова миколаївської ова", "голова миколаївської обласної "
         "військової адміністрації", "abbrev"),
        # уточнення — НЕ синонім: різницю має бачити людина
        ("міський голова", "міський голова львова", "containment"),
        ("керівник обласної військової адміністрації",
         "керівник обласної військової адміністрації херсонської області",
         "containment"),
        # друкарська різниця
        ("олександр сєнкевич", "олександр сенкевич", "typo"),
        # одне слово замінено — синонім посади
        ("голова миколаївської обласної військової адміністрації",
         "очільник миколаївської обласної військової адміністрації", "word_swap"),
        # …і той самий клас, але посади РІЗНІ — тому гуртом його не закриєш
        ("голова миколаївської обласної ради",
         "депутатка миколаївської обласної ради", "word_swap"),
        # скорочення зі збігом решти слів
        ("головнокомандувач зсу", "головнокомандувач збройних сил україни",
         "abbrev"),
        ("директор кп миколаївські парки",
         "директор комунального підприємства миколаївські парки", "abbrev"),
        # ПАСТКА заміру 02.08: «КК = Кримінального Кодексу» збігається, але
        # статті різні. Це не скорочення — масове злиття склеїло б 190 і 310.
        ("стаття 190 кк україни", "стаття 310 кримінального кодексу україни",
         "other"),
    ]
    bad = [(a, b, want, er.classify_pair(a, b)[0])
           for a, b, want in cases if er.classify_pair(a, b)[0] != want]
    check("класифікатор розкладає реальні приклади із заміру", not bad, f"{bad}")

    cls, detail = er.classify_pair("міський голова", "міський голова львова")
    check("клас «уточнення» показує САМЕ зайве слово (це і є рішення)",
          cls == "containment" and detail == "львова", f"{cls}/{detail}")

    _, detail = er.classify_pair("стаття 190 кк україни",
                                 "стаття 310 кримінального кодексу україни")
    check("пара з різними числами показує саме числа, а не «скорочення»",
          detail is None or "190" in detail or "310" in detail, f"{detail}")

    cls, detail = er.classify_pair("голова обласної ва", "очільник обласної ва")
    check("клас «одне слово замінено» показує обидва слова",
          cls == "word_swap" and detail == "голова → очільник", f"{cls}/{detail}")

    # Поділ «уточнень» на доповнення й розрізнювачі — це те, чим вирішується,
    # чи можна закривати клас гуртом.
    _, fill = er.classify_pair("премєр-міністр", "премєр-міністр україни")
    _, disc = er.classify_pair("заступник міського голови",
                               "перший заступник міського голови")
    check("доповнення назви і розрізнювач відрізняються за словом",
          not (set(fill.split()) & er.DISCRIMINATING)
          and (set(disc.split()) & er.DISCRIMINATING), f"{fill!r} vs {disc!r}")

    cls, _ = er.classify_pair("мер миколаєва", "міський голова миколаєва",
                              has_carrier=True)
    check("«мер» ~ «міський голова» — клас «лексично різні, спільний носій»",
          cls == "carrier_only", cls)
    cls, _ = er.classify_pair("мер миколаєва", "міський голова миколаєва")
    check("без спільного носія та сама пара в carrier_only НЕ падає",
          cls == "other", cls)

    # Вкладеність більше не тягне «уточнення» нагору черги (було 2 бали, стало 1)
    _, pairs = er.find_pairs(min_links=1, top_roles=200)
    by = {(a, b): (sc, cls) for a, b, sc, _sig, cls, _d, _st in pairs}
    # Вкладеність БЕЗ спільного носія («депутатка міської ради» ~ «…Львова»)
    # має стояти нижче за справжню пару зі спільним носієм — інакше вечір
    # починається з уточнень, які все одно доведеться відхилити.
    nested = [sc for (a, b), (sc, cls) in by.items()
              if cls == "containment" and "депутатка" in a]
    carrier = [sc for (a, b), (sc, cls) in by.items() if cls == "carrier_only"]
    check("вкладеність без спільного носія стоїть нижче за спільного носія",
          nested and carrier and max(carrier) > max(nested),
          f"носій {carrier} vs вкладеність {nested}")

    # Ставка: питати про 900 зв'язків раніше, ніж про 3 — за інших рівних
    stakes = {(a, b): st for a, b, _s, _sig, _c, _d, st in pairs}
    check("у парі рахується ставка — менша з двох частот",
          stakes and all(s >= 1 for s in stakes.values()), f"{list(stakes.values())[:5]}")


def test_class_breakdown():
    # Регресія 02.08: розкладка карток мовчки їхала на дефолтному
    # pg_trgm.similarity_threshold = 0.3 (оператор `%` бере поріг із сеансу, а
    # не з тексту запиту) — замість 3757 пар виходило 194308, з них 92% сміття.
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, mentions) VALUES "
        "(201, 'place', 'вулиця Повстанська', 5), "
        "(202, 'place', 'вулиця 4 Ялтинська', 5), "
        "(203, 'place', 'Велика Морська вулиця', 5), "
        "(204, 'place', 'вулиця Велика Морська', 5) "
        "ON CONFLICT (id) DO NOTHING")
    strict = em.classify_cards(0.8)
    loose = em.classify_cards(0.3)
    check("поріг схожості справді застосовується (регресія дефолтних 0.3)",
          strict["total"] < loose["total"], f"{strict['total']} vs {loose['total']}")
    perm = strict["classes"].get("permutation", {}).get("n", 0)
    check("перестановку слів видно і на строгому порозі", perm >= 1, f"{perm}")

    cards = em.classify_cards()
    roles = em.classify_roles()
    check("розкладка карток рахується і не падає на порожньому",
          isinstance(cards.get("total"), int), f"{cards.get('total')}")
    check("розкладка ролей бачить класи",
          roles["total"] > 0 and roles["classes"], f"{list(roles['classes'])}")
    txt = em.format_classes("Класи · РОЛІ", roles)
    check("звіт розкладки читабельний і зі шматками прикладів",
          "пар" in txt and "·" in txt, txt.split("\n")[0])
    before = raw_roles_snapshot()
    em.classify_cards()
    em.classify_roles()
    check("розкладка нічого не змінює", raw_roles_snapshot() == before)


def test_queue_and_merge():
    before = raw_roles_snapshot()
    er.scan_pairs(min_links=1, top_roles=200)
    p = er.next_question(set())
    check("черга віддає питання з підставою",
          p is not None and p["signals"], f"{p['signals'] if p else None}")
    # Картка має нести ПІДСТАВУ і числа (§4: людина вирішує по них), а
    # callback_data Telegram обрізає на 64 байтах — тому в кнопці лише id пари.
    txt = er._question_text(p)
    cbs = [b.callback_data for row in er._question_markup(p["id"]).inline_keyboard
           for b in row]
    check("картка питання показує носіїв, періоди й підставу",
          all(w in txt for w in ("згадк", "носі", "Підстава")), txt.replace("\n", " | "))
    check("callback_data кнопок вкладається в ліміт Telegram (64 байти)",
          all(len(c.encode()) <= 64 for c in cbs), f"{cbs}")

    canon_id, canon = er.merge_roles(
        "мер миколаєва", "міський голова миколаєва",
        "мер Миколаєва", "міський голова Миколаєва", "тест")
    er.set_verdict(p["id"], "same", "тест") if p else None
    check("канон обрано офіційний, а не розмовний",
          canon == "міський голова Миколаєва", f"canon={canon}")

    # ГОЛОВНЕ: сирий role_at_time недоторканий
    check("сирий role_at_time не змінився після зведення",
          raw_roles_snapshot() == before)

    rows = bot_db.query(
        "SELECT DISTINCT role_canon FROM v_entity_roles "
        "WHERE role_norm(role_at_time) IN "
        "('мер миколаєва', 'міський голова миколаєва')")
    check("view зводить обидва написання до одного канону",
          len(rows) == 1 and rows[0]["role_canon"] == "міський голова Миколаєва",
          f"{rows}")

    # третє написання чіпляємо до вже наявного канону
    cid2, _ = er.merge_roles("миколаївський міський голова", "мер миколаєва",
                             "Миколаївський міський голова", "мер Миколаєва", "тест")
    check("третє написання чіпляється до наявного канону, нового не плодить",
          cid2 == canon_id, f"{cid2} vs {canon_id}")
    n = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    check("канон один на посаду", n == 1, f"канонів: {n}")

    # злиття двох РІЗНИХ канонів (обидва написання вже мали свій)
    er.merge_roles("голова миколаївської ова", "керівник миколаївської ова",
                   "голова Миколаївської ОВА", "керівник Миколаївської ОВА", "тест")
    n = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    check("другий канон створився окремо", n == 2, f"канонів: {n}")
    er.merge_roles("мер миколаєва", "голова миколаївської ова")
    n = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    v = bot_db.query("SELECT count(DISTINCT canon_id) AS n FROM role_variants")[0]["n"]
    check("злиття двох канонів переносить варіанти й прибирає порожній",
          n == 1 and v == 1, f"канонів {n}, різних canon_id у варіантах {v}")

    # Розводимо назад: варіанти ОВА знімаємо з довідника й зводимо в свій
    # канон — далі перевіряємо афіліацію саме мера, і ОВА не має до неї липнути.
    bot_db.execute("DELETE FROM role_variants WHERE raw_norm LIKE %s",
                   ("%миколаївської ова",))
    er.merge_roles("голова миколаївської ова", "керівник миколаївської ова",
                   "голова Миколаївської ОВА", "керівник Миколаївської ОВА", "тест")
    n = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    check("відв'язані варіанти зводяться в окремий канон", n == 2, f"канонів: {n}")


def test_affiliation():
    canon = bot_db.query(
        "SELECT rc.id FROM role_canon rc JOIN role_variants rv ON rv.canon_id = rc.id "
        "WHERE rv.raw_norm = 'мер миколаєва'")[0]["id"]
    variants = [r["raw_norm"] for r in bot_db.query(
        "SELECT raw_norm FROM role_variants WHERE canon_id = %s", (canon,))]
    orgs = bot_db.query(er.ORG_CANDIDATES_SQL, (variants,))
    check("кандидат в орган підказується співпоявою",
          orgs and orgs[0]["id"] == 4, f"{[(o['name'], o['c']) for o in orgs]}")
    bot_db.execute("UPDATE role_canon SET org_entity_id = 4 WHERE id = %s", (canon,))
    people = bot_db.query(
        "SELECT DISTINCT entity_id FROM v_entity_roles WHERE org_entity_id = 4")
    check("афіліація дає «усі посадовці органу» звичайним JOIN",
          [r["entity_id"] for r in people] == [1], f"{people}")


def test_rejection_memory_and_rollback():
    pair = bot_db.query(
        "SELECT id, a_norm, b_norm FROM role_pairs WHERE verdict IS NULL "
        "ORDER BY score DESC LIMIT 1")
    if pair:
        pid = pair[0]["id"]
        er.set_verdict(pid, "different", "тест")
        nxt = er.next_question(set())
        check("«ні, різні» прибирає пару з черги назавжди",
              nxt is None or nxt["id"] != pid)
        er.scan_pairs(min_links=1, top_roles=200)
        again = bot_db.query(
            "SELECT verdict FROM role_pairs WHERE id = %s", (pid,))[0]["verdict"]
        check("повторний прогін детектора не стирає рішення людини",
              again == "different", f"verdict={again}")
    else:
        check("«ні, різні» прибирає пару з черги назавжди", False, "черга порожня")

    before = raw_roles_snapshot()
    bot_db.execute("DELETE FROM role_variants WHERE raw_norm = %s", ("мер миколаєва",))
    er._reopen(["мер миколаєва"])
    check("відкат ролі не чіпає сирий текст у статтях",
          raw_roles_snapshot() == before)
    rows = bot_db.query(
        "SELECT canon_id, role_canon, role_at_time FROM v_entity_roles "
        "WHERE role_norm(role_at_time) = 'мер миколаєва'")
    check("після відкату написання знову йде саме за себе (канону немає, "
          "у view — сирий текст)",
          rows and all(r["canon_id"] is None
                       and r["role_canon"] == r["role_at_time"] for r in rows),
          f"{[(r['canon_id'], r['role_canon']) for r in rows]}")
    reopened = bot_db.query(
        "SELECT count(*) AS n FROM role_pairs "
        "WHERE (a_norm = 'мер миколаєва' OR b_norm = 'мер миколаєва') "
        "AND verdict IS NULL")[0]["n"]
    check("пари з відкоченим написанням повертаються в чергу", reopened > 0,
          f"{reopened}")


def test_card_merge_journal():
    """/entity_dedup із журналом: злиття карток і повний відкат зі знімка."""
    # дубль Сєнкевича під тим самим ім'ям (точний збіг norm — саме те, що
    # /entity_dedup має право зливати без людини)
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, name_ru, aliases) "
        "VALUES (99, 'person', 'Олександр Сєнкевич', 'Александр Сенкевич', %s) "
        "ON CONFLICT (id) DO NOTHING", (["Сєнкевич О.", "мер"],))
    bot_db.execute(
        "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
        "VALUES (%s, 99, %s, 'mentioned') ON CONFLICT DO NOTHING",
        (ARTICLES[17][0], "очільник Миколаєва"))
    bot_db.execute(
        "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
        "VALUES (%s, 99, %s, 'mentioned') ON CONFLICT DO NOTHING",
        (ARTICLES[0][0], "мер Миколаєва"))     # стаття, де вже є переможець
    bot_db.execute(
        "UPDATE entities SET name_ru = NULL WHERE id = 1")  # перевіримо добір імені

    before_links = {(r["article_id"], r["role_at_time"], r["salience"])
                    for r in bot_db.query(
                        "SELECT article_id, role_at_time, salience "
                        "FROM article_entities WHERE entity_id = 99")}
    winner_aliases_before = bot_db.query(
        "SELECT aliases FROM entities WHERE id = 1")[0]["aliases"] or []

    n_groups, n_removed = el._dedup_entities("тест")
    check("dedup злив дубль картки", n_removed >= 1,
          f"груп {n_groups}, злито {n_removed}")
    gone = bot_db.query("SELECT count(*) AS n FROM entities WHERE id = 99")[0]["n"]
    check("програшна картка видалена (як і було)", gone == 0)

    al = bot_db.query("SELECT aliases FROM entities WHERE id = 1")[0]["aliases"] or []
    check("аліаси програшної картки перейшли переможцю",
          "Сєнкевич О." in al and "мер" in al, f"{al}")
    agg = bot_db.query(
        "SELECT mentions, first_seen, last_seen FROM entities WHERE id = 1")[0]
    real = bot_db.query(
        "SELECT count(*) AS c, min(a.published) AS lo, max(a.published) AS hi "
        "FROM article_entities ae JOIN articles a ON a.id = ae.article_id "
        "WHERE ae.entity_id = 1")[0]
    check("агрегати переможця перераховані з даних",
          agg["mentions"] == real["c"] and agg["first_seen"] == real["lo"]
          and agg["last_seen"] == real["hi"], f"{agg} vs {real}")

    log = bot_db.query(
        "SELECT id, winner_id, loser_id FROM entity_merges ORDER BY id DESC LIMIT 1")
    check("журнал записав злиття", bool(log) and log[0]["loser_id"] == 99, f"{log}")
    mid = log[0]["id"]

    res = em.restore_merge(mid)
    check("відкат зі знімка повернув картку", isinstance(res, dict), f"{res}")
    card = bot_db.query(
        "SELECT id, kind, name_ua, aliases FROM entities WHERE id = 99")
    check("картка повернулась із тим самим id і іменем",
          bool(card) and card[0]["name_ua"] == "Олександр Сєнкевич", f"{card}")
    after_links = {(r["article_id"], r["role_at_time"], r["salience"])
                   for r in bot_db.query(
                       "SELECT article_id, role_at_time, salience "
                       "FROM article_entities WHERE entity_id = 99")}
    check("зв'язки повернулись РАЗОМ із role_at_time і salience",
          after_links == before_links, f"{after_links} vs {before_links}")
    al2 = bot_db.query("SELECT aliases FROM entities WHERE id = 1")[0]["aliases"] or []
    check("аліаси, дописані злиттям, знято з переможця",
          sorted(al2) == sorted(winner_aliases_before), f"{al2} vs {winner_aliases_before}")
    ru = bot_db.query("SELECT name_ru FROM entities WHERE id = 1")[0]["name_ru"]
    check("ім'я, яким злиття заповнило порожнє поле переможця, теж відкочене",
          ru is None, f"name_ru={ru}")
    win = bot_db.query(
        "SELECT count(*) AS n FROM article_entities "
        "WHERE entity_id = 1 AND article_id = %s", (ARTICLES[17][0],))[0]["n"]
    check("чужа стаття знята з переможця, а «спільна» лишилась",
          win == 0 and bot_db.query(
              "SELECT count(*) AS n FROM article_entities "
              "WHERE entity_id = 1 AND article_id = %s",
              (ARTICLES[0][0],))[0]["n"] == 1)
    agg2 = bot_db.query("SELECT mentions FROM entities WHERE id = 99")[0]["mentions"]
    check("агрегати відновленої картки перераховані",
          agg2 == len(before_links), f"{agg2} vs {len(before_links)}")
    check("повторний відкат ідемпотентний", em.restore_merge(mid) == "already")


def test_measure_readonly():
    before = raw_roles_snapshot()
    n_canon = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    m = em.measure()
    text = em.format_measure(m)
    check("замір рахує обидві осі", "КАРТКИ" in text and "РОЛІ" in text
          and m["roles"]["links"] > 0, f"ролей-зв'язків: {m['roles']['links']}")
    check("замір нічого не змінює",
          raw_roles_snapshot() == before
          and bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"] == n_canon)


async def run():
    test_norm_matches_sql()
    test_detector()
    test_classes()
    test_class_breakdown()
    test_queue_and_merge()
    test_affiliation()
    test_rejection_memory_and_rollback()
    test_card_merge_journal()
    test_measure_readonly()


def main():
    setup()
    asyncio.run(run())
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} перевірок пройдено")
    if bad:
        print("ПРОВАЛЕНО: " + "; ".join(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
