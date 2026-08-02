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
    (6, "place", "Миколаїв", "Николаев"),
    (7, "place", "Очаків", None),
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
        cur.execute("DROP TABLE IF EXISTS entity_merge_rules")
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
        ("міський голова", "міський голова львова", "containment_geo"),
        ("керівник обласної військової адміністрації",
         "керівник обласної військової адміністрації херсонської області",
         "containment_geo"),
        # Питання Олега, яке зловило дірку: зайве слово це КРАЇНА, а мій
        # список «інших регіонів» містив лише українські області.
        ("премєр-міністр", "премєр-міністр італії", "containment_geo"),
        ("міністр закордонних справ", "міністр закордонних справ польщі",
         "containment_geo"),
        ("премєр-міністр", "премєр-міністр україни", "containment_geo"),
        # …а це вже НЕ з зашитого списку: «Очаків» бот знає лише тому, що
        # такий топонім є в норі. Без цього джерела список країн ніколи не
        # покрив би всі села Миколаївщини.
        ("староста", "староста очакова", "containment_geo"),
        ("начальник управління", "начальник управління фінансів",
         "containment_fill"),
        ("заступник міського голови", "перший заступник міського голови",
         "containment_disc"),
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
         "numbers"),
        ("стаття 127 кримінального кодексу україни",
         "стаття 366-3 кримінального кодексу україни", "numbers"),
        ("миколаївський ліцей 8", "миколаївський ліцей 9", "numbers"),
    ]
    bad = [(a, b, want, er.classify_pair(a, b)[0])
           for a, b, want in cases if er.classify_pair(a, b)[0] != want]
    check("класифікатор розкладає реальні приклади із заміру", not bad, f"{bad}")

    cls, detail = er.classify_pair("міський голова", "міський голова львова")
    check("клас «уточнення» показує САМЕ зайве слово (це і є рішення)",
          cls == "containment_geo" and detail == "львова", f"{cls}/{detail}")

    cls, detail = er.classify_pair("стаття 190 кк україни",
                                   "стаття 310 кримінального кодексу україни")
    check("пара з різними числами — окремий клас і показує самі числа",
          cls == "numbers" and "190" in detail and "310" in detail,
          f"{cls}/{detail}")
    check("клас «різні номери» ніколи не йде в гурт «звести»",
          "numbers" in er.BULK_REJECT and "numbers" not in er.BULK_MERGE)
    check("апостроф знімається нормалізацією — питання не виникає взагалі",
          er.role_norm("премʼєр-міністр") == er.role_norm("премєр-міністр")
          == "премєр-міністр", er.role_norm("премʼєр-міністр"))

    cls, detail = er.classify_pair("голова обласної ва", "очільник обласної ва")
    check("клас «одне слово замінено» показує обидва слова",
          cls == "word_swap" and detail == "голова → очільник", f"{cls}/{detail}")

    # Поділ «уточнень» на три купи — це те, чим вирішується, чи можна
    # закривати клас гуртом.
    check("жодна вкладеність не йде в гурт «звести» — зайве слово буває "
          "розрізнювачем завжди",
          not any(c.startswith("containment") for c in er.BULK_MERGE)
          and "containment_disc" in er.BULK_REJECT
          and "containment_geo" in er.BULK_REJECT, f"{er.BULK_MERGE}")
    check("гуртом зводяться лише механічні класи",
          er.BULK_MERGE == {"permutation", "abbrev", "typo"}, f"{er.BULK_MERGE}")

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
              if cls.startswith("containment") and "депутатка" in a]
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


def test_bulk():
    """Підтвердження КЛАСОМ (крок Б). 2109 пар по одній — десяток годин, тому
    клас закривається одним рішенням; але тільки після показу прикладів і
    тільки для класів, де рішення однакове для всіх пар."""
    er.scan_pairs(min_links=1, top_roles=200)
    before = raw_roles_snapshot()
    counts = dict(er.class_counts())
    check("класи в черзі рахуються", counts, f"{counts}")

    geo = er.bulk_apply("containment_geo", "different", "тест")
    check("гурт «ні» закриває клас цілком", geo >= 1, f"{geo}")
    left = dict(er.class_counts()).get("containment_geo", 0)
    check("після гурту «ні» клас із черги зникає", left == 0, f"{left}")
    canons = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    check("гурт «ні» не створює жодного канону", canons == 0, f"{canons}")

    same = er.bulk_apply("carrier_only", "same", "тест")
    check("гурт «так» зводить клас у канони", same >= 1, f"{same}")
    check("сирий role_at_time не змінився і після гурту",
          raw_roles_snapshot() == before)
    canons = bot_db.query("SELECT count(*) AS n FROM role_canon")[0]["n"]
    check("канони після гурту з'явились", canons >= 1, f"{canons}")

    check("повторний гурт по закритому класу нічого не робить",
          er.bulk_apply("carrier_only", "same", "тест") == 0)

    # відкат гурту — той самий, що для одиничного рішення
    v = bot_db.query("SELECT raw_norm FROM role_variants LIMIT 1")
    if v:
        rn = v[0]["raw_norm"]
        bot_db.execute("DELETE FROM role_variants WHERE raw_norm = %s", (rn,))
        er._reopen([rn])
        back = bot_db.query(
            "SELECT count(*) AS n FROM role_pairs WHERE verdict IS NULL "
            "AND (a_norm = %s OR b_norm = %s)", (rn, rn))[0]["n"]
        check("відкат після гурту повертає пари в чергу", back > 0, f"{back}")

    # прибираємо за собою: далі тестам потрібна чиста черга й порожній довідник
    bot_db.execute("DELETE FROM role_variants")
    bot_db.execute("DELETE FROM role_canon")
    bot_db.execute("DELETE FROM role_pairs")


def test_norm_version():
    """Зміна role_norm() без перебудови — тиха поломка: Postgres не оновлює
    вираженний індекс сам, а ключі довідника лишаються від старої функції."""
    bot_db.execute(
        "INSERT INTO role_canon (id, canon, canon_norm, created) "
        "VALUES (900, 'Премʼєр-міністр', 'старий-ключ', 0) "
        "ON CONFLICT (id) DO NOTHING")
    bot_db.execute(
        "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, created) "
        "VALUES ('премʼєр-міністр', 'Премʼєр-міністр', 900, 0) "
        "ON CONFLICT (raw_norm) DO NOTHING")
    bot_db.set_state(er.ROLE_NORM_VERSION_KEY, er.ROLE_NORM_VERSION - 1)
    er.ensure_schema(force=True)
    keys = [r["raw_norm"] for r in bot_db.query(
        "SELECT raw_norm FROM role_variants WHERE canon_id = 900")]
    check("ключі довідника перекладено на нову нормалізацію",
          keys == ["премєр-міністр"], f"{keys}")
    idx = bot_db.query(
        "SELECT count(*) AS n FROM pg_indexes WHERE indexname = 'idx_ae_role_norm'")
    check("індекс по role_norm перебудовано, а не залишено від старої функції",
          idx[0]["n"] == 1, f"{idx}")
    stored = bot_db.get_state(er.ROLE_NORM_VERSION_KEY)
    check("версія нормалізації записана", int(stored) == er.ROLE_NORM_VERSION,
          f"{stored}")
    bot_db.execute("DELETE FROM role_canon WHERE id = 900")


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


def test_org_suggestion_prefers_name():
    """Співпояв ловить СЮЖЕТ, а не афіліацію: Галущенку він запропонував НАБУ,
    САП і «Квартал 95» — бо міністр юстиції, а пишуть про нього в матеріалах
    про провадження. Тому першими йдуть органи, чия НАЗВА перегукується з
    назвою посади."""
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, mentions) VALUES "
        "(301, 'org', 'Міністерство юстиції України', 12), "
        "(302, 'org', 'Національне антикорупційне бюро України', 480) "
        "ON CONFLICT (id) DO NOTHING")
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, mentions) VALUES "
        "(303, 'person', 'Герман Галущенко', 30) ON CONFLICT (id) DO NOTHING")
    # роль міністра — і в КОЖНІЙ статті поруч НАБУ, а Мін'юсту немає ніде
    for idx in (14, 15, 16, 17):
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES (%s, 303, 'міністр юстиції', 'main') ON CONFLICT DO NOTHING",
            (ARTICLES[idx][0],))
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, salience) "
            "VALUES (%s, 302, 'mentioned') ON CONFLICT DO NOTHING",
            (ARTICLES[idx][0],))
    cid, _ = er.merge_roles("міністр юстиції", "міністр юстиції україни",
                            "міністр юстиції", "міністр юстиції України", "тест")
    cands = er.org_candidates(cid)
    check("орган за назвою посади стоїть перед органом за співпоявою",
          cands and cands[0]["id"] == 301,
          f"{[(c['name'], c['basis']) for c in cands]}")
    check("у кнопці видно ПІДСТАВУ, а не просто число",
          cands and "назв" in cands[0]["basis"], f"{cands[0]['basis'] if cands else None}")
    nabu = [c for c in cands if c["id"] == 302]
    check("співпояв лишається другим джерелом, а не зникає",
          not nabu or "статей" in nabu[0]["basis"], f"{nabu}")
    # орган можна переставити й зняти вже після рішення
    bot_db.execute("UPDATE role_canon SET org_entity_id = 302 WHERE id = %s", (cid,))
    bot_db.execute("UPDATE role_canon SET org_entity_id = NULL WHERE id = %s", (cid,))
    got = bot_db.query(
        "SELECT org_entity_id FROM role_canon WHERE id = %s", (cid,))[0]["org_entity_id"]
    check("орган знімається без сліду (помилкова прив'язка виправна)",
          got is None, f"{got}")
    bot_db.execute("DELETE FROM role_variants WHERE canon_id = %s", (cid,))
    bot_db.execute("DELETE FROM role_canon WHERE id = %s", (cid,))


def test_outliers():
    """Роль, приписана чужому носієві: у статті про Миколаївщину бек «у Херсоні
    теж… голова ОВА Прокудін», і витяг доповнив роль регіоном СТАТТІ. У сталої
    посади один-два носії, тому такий дефект видно як носія з одним зв'язком
    там, де в головного їх сотні."""
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua) VALUES "
        "(401, 'person', 'Віталій Кім-тест'), (402, 'person', 'Олександр Прокудін') "
        "ON CONFLICT (id) DO NOTHING")
    role = "голова Миколаївської обласної військової адміністрації"
    for aid in range(5000, 5030):     # головний носій — 30 зв'язків
        bot_db.execute(
            "INSERT INTO articles (id, published, title_ua) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING", (aid, 1700000000, f"Стаття {aid}"))
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES (%s, 401, %s, 'main') ON CONFLICT DO NOTHING", (aid, role))
    bot_db.execute(                    # чужак — один зв'язок
        "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
        "VALUES (5000, 402, %s, 'mentioned') ON CONFLICT DO NOTHING", (role,))

    # попередник на посаді — НЕ помилка: у нього свій відрізок часу
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua) VALUES (403, 'person', 'Володимир Чайка') "
        "ON CONFLICT (id) DO NOTHING")
    bot_db.execute(
        "INSERT INTO articles (id, published, title_ua) VALUES (5030, 1300000000, %s) "
        "ON CONFLICT (id) DO NOTHING", ("Стаття 5030",))
    bot_db.execute(
        "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
        "VALUES (5030, 403, %s, 'mentioned') ON CONFLICT DO NOTHING", (role,))
    # дубль КАРТКИ головного носія — інша вісь, не ролі
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua) VALUES (404, 'person', 'Кім-тест') "
        "ON CONFLICT (id) DO NOTHING")
    bot_db.execute(
        "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
        "VALUES (5001, 404, %s, 'mentioned') ON CONFLICT DO NOTHING", (role,))

    rows = bot_db.query(er.OUTLIERS_SQL, (er.OUTLIER_MIN_TOP, er.OUTLIER_RATIO, 20))
    hit = [r for r in rows if r["name"] == "Олександр Прокудін"]
    check("викид (1 зв'язок при 30) знайдено",
          hit and hit[0]["main_name"] == "Віталій Кім-тест",
          f"{[(r['name'], r['c'], r['top']) for r in rows]}")
    check("у звіті є періоди обох носіїв — саме ними попередник відрізняється",
          hit and hit[0]["lo"] and hit[0]["main_lo"],
          f"{hit[0]['lo'] if hit else None} vs {hit[0]['main_lo'] if hit else None}")
    chaika = [r for r in rows if r["name"] == "Володимир Чайка"]
    check("попередник потрапляє у звіт, але зі СВОЇМ періодом",
          chaika and chaika[0]["hi"] < chaika[0]["main_lo"],
          f"{chaika[0]['lo'] if chaika else None}…{chaika[0]['hi'] if chaika else None}")
    # Перевірка по ТЕКСТУ — єдине, що справді розділяє попередника й помилку:
    # періоди тут не працюють, бо ретроспектива про мера 2001-го виходить
    # у 2025-му і має цілком «сучасний» відрізок часу.
    hist = ("Сесія міськради ухвалила рішення. Тодішній мер Миколаєва "
            "Володимир Чайка підписав документ ще у 2011 році.")
    d, found, snip = er.role_near_name(hist, "Володимир Чайка", "мер миколаєва")
    check("роль в одній групі з іменем → попередник, не помилка",
          found and d is not None and d <= er.NEAR_CHARS, f"дистанція={d}")
    check("врізка з тексту повертається — по ній і вирішує людина",
          snip and "Чайка" in snip, f"{snip}")

    wrong = ("Голова Миколаївської ОВА Віталій Кім повідомив про наслідки атаки. "
             + "Далі йшов великий абзац про відбудову. " * 12
             + "Раніше мер Миколаєва Олександр Сєнкевич казав про бюджет.")
    d2, found2, _ = er.role_near_name(wrong, "Віталій Кім", "мер миколаєва")
    check("роль ДАЛЕКО від імені → підозра на помилку витягу",
          found2 and d2 is not None and d2 > er.GREY_CHARS, f"дистанція={d2}")

    # Реальна пастка зі статті 310211: сусідній абзац про ІНШУ людину. Поріг
    # 250 її пропускав як «поруч» — тепер вона має піти в «глянути очима».
    real = ("Також Віталій Кім розповідав, що миколаївці можуть очікувати появу "
            "чистої води в кранах орієнтовно під кінець 2025 року або на початку "
            "наступного.\n\nМер також наголосив, що після запуску нового водогону "
            "у місті триває промивка мереж.")
    d5, _f5, snip5 = er.role_near_name(real, "Віталій Кім", "мер миколаєва")
    check("сусідній абзац про іншу людину НЕ зараховується як «поруч» (310211)",
          d5 is not None and d5 > er.NEAR_CHARS, f"дистанція={d5}")
    check("для сірої зони є врізка, щоб вирішити очима",
          snip5 and "Кім" in snip5, f"{snip5}")

    d3, found3, _ = er.role_near_name("Текст без цієї людини взагалі.",
                                   "Віталій Кім", "мер миколаєва")
    check("імені немає в тексті — теж підозра, і це видно окремо",
          not found3 and d3 is None, f"{d3}/{found3}")

    d4, _, _ = er.role_near_name("Мером Миколаєва тоді був Володимир Чайка.",
                              "Володимир Чайка", "мер миколаєва")
    check("відмінки не ламають перевірку («мером», «Чайки»)",
          d4 is not None and d4 <= er.NEAR_CHARS, f"дистанція={d4}")

    # Топонім із назви посади не має ставати «доказом близькості»: у ролі
    # «міський голова МИКОЛАЄВА» це слово стоїть у кожному абзаці, а в імені
    # «МИКОЛА Леонтович» той самий корінь — звідси фальшива відстань 0.
    er._places["set"] = None                     # перечитати список із нори
    trap = ("У Миколаєві вшанували память композитора. Микола Леонтович "
            "написав «Щедрик». " + "Далі текст про концерт у Миколаєві. " * 8
            + "Міський голова Миколаєва Олександр Сєнкевич поклав квіти.")
    d6, _f6, _s6 = er.role_near_name(trap, "Микола Леонтович",
                                     "міський голова миколаєва")
    check("топонім у ролі не дає фальшивої близькості (Леонтович/Миколаєва)",
          d6 is None or d6 > er.NEAR_CHARS, f"дистанція={d6}")

    # Реальний випадок 319027: «мер» сидить усередині «премєра», і стаття про
    # композитора давала посаду мера за 6 символів від Леонтовича.
    prem = ("Відбулася премєра твору Миколи Леонтовича у філармонії. "
            + "Далі текст про концерт. " * 10
            + "Мер Миколаєва Олександр Сєнкевич привітав музикантів.")
    d7, _f7, _s7 = er.role_near_name(prem, "Микола Леонтович", "мер миколаєва")
    check("«мер» усередині «премєра» більше не рахується (319027)",
          d7 is None or d7 > er.NEAR_CHARS, f"дистанція={d7}")

    d8, _f8, _s8 = er.role_near_name(
        "Мером Миколаєва тоді був Володимир Чайка.", "Володимир Чайка",
        "мер миколаєва")
    check("а справжнє слово посади в іншому відмінку ловиться далі",
          d8 is not None and d8 <= er.NEAR_CHARS, f"дистанція={d8}")

    import difflib
    twin = [r for r in rows if r["name"] == "Кім-тест"]
    check("дубль картки видно за схожістю імені з головним носієм",
          twin and difflib.SequenceMatcher(
              None, twin[0]["name"].lower(), twin[0]["main_name"].lower()).ratio() >= 0.5,
          f"{twin}")
    check("звіт дає id статей для /entity_resync",
          hit and 5000 in (hit[0]["articles"] or []), f"{hit[0]['articles'] if hit else None}")
    main = [r for r in rows if r["name"] == "Віталій Кім-тест"]
    check("головний носій викидом не вважається", not main, f"{main}")

    before = raw_roles_snapshot()
    bot_db.query(er.OUTLIERS_SQL, (er.OUTLIER_MIN_TOP, er.OUTLIER_RATIO, 20))
    check("звіт про викиди нічого не переписує (сира роль недоторкана)",
          raw_roles_snapshot() == before)

    bot_db.execute("DELETE FROM article_entities WHERE article_id >= 5000")
    bot_db.execute("DELETE FROM articles WHERE id >= 5000")
    bot_db.execute("DELETE FROM entities WHERE id IN (401, 402, 403, 404)")


def test_carrier_counts():
    """Чужий носій ролі (Прокудін під миколаївською ОВА) — помилка витягу, а не
    другий носій посади. У картці він має бути видним як викид, тобто з числом."""
    card = er._role_card("голова миколаївської ова")
    check("носії показані з лічильником згадок",
          card["carriers"] and all("(" in c for c in card["carriers"]),
          f"{card['carriers']}")


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


def test_manual_merge():
    """/entity_merge <id1> <id2> — крок 3. Дубль знайдено очима («Сєнкевич»
    при «Олександр Сєнкевич»), і його треба злити так, щоб пошук по обох
    написаннях далі працював, а помилку можна було відкотити."""
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, name_ru, aliases) VALUES "
        "(501, 'person', 'Олександр Сєнкевич', NULL, %s), "
        "(502, 'person', 'Сєнкевич', 'Сенкевич', '{}'), "
        "(503, 'org', 'Миколаївська міськрада', NULL, '{}') "
        "ON CONFLICT (id) DO NOTHING", (["Сєнкевич О."],))
    for aid, eid, role in ((6000, 501, "мер Миколаєва"),
                           (6001, 502, "міський голова"),
                           (6002, 502, "мер"),
                           (6000, 502, "мер Миколаєва")):   # спільна стаття
        bot_db.execute(
            "INSERT INTO articles (id, published, title_ua) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING", (aid, 1700000000, f"Стаття {aid}"))
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES (%s, %s, %s, 'mentioned') ON CONFLICT DO NOTHING",
            (aid, eid, role))

    p, err = em.merge_preview(501, 503)
    check("картки різних типів не зливаються ніколи",
          p is None and "різні типи" in (err or ""), f"{err}")
    p, err = em.merge_preview(501, 502)
    check("прев'ю показує підставу — спільні статті й сусідів",
          p and p["overlap"]["shared_articles"] == 1, f"{err or p['overlap']}")
    txt = em.format_preview(p)
    check("у прев'ю видно, яка картка лишається",
          "ЛИШАЄТЬСЯ" in txt and "піде в аліаси" in txt)

    before = {(r["article_id"], r["role_at_time"]) for r in bot_db.query(
        "SELECT article_id, role_at_time FROM article_entities "
        "WHERE entity_id IN (501, 502)")}
    res = em.merge_cards(501, 502, "тест")
    check("злиття перевісило зв'язки", res["moved"] == 2, f"{res}")
    gone = bot_db.query("SELECT count(*) AS n FROM entities WHERE id = 502")[0]["n"]
    check("програшна картка прибрана", gone == 0)
    al = bot_db.query("SELECT aliases FROM entities WHERE id = 501")[0]["aliases"]
    check("імена програшної картки пішли в аліаси (інакше пошук по них помре)",
          "Сєнкевич" in al and "Сенкевич" in al and "Сєнкевич О." in al, f"{al}")
    now = {(r["article_id"], r["role_at_time"]) for r in bot_db.query(
        "SELECT article_id, role_at_time FROM article_entities WHERE entity_id = 501")}
    check("role_at_time кожного зв'язку збережено", now == before, f"{now} vs {before}")
    agg = bot_db.query("SELECT mentions FROM entities WHERE id = 501")[0]["mentions"]
    check("агрегати перераховані", agg == 3, f"{agg}")

    back = em.restore_merge(res["merge_id"])
    check("відкат повертає картку з її зв'язками", isinstance(back, dict), f"{back}")
    card = bot_db.query("SELECT name_ua, name_ru FROM entities WHERE id = 502")
    check("відновлена картка та сама", card and card[0]["name_ua"] == "Сєнкевич", f"{card}")
    al2 = bot_db.query("SELECT aliases FROM entities WHERE id = 501")[0]["aliases"]
    check("аліаси переможця повернулись до доздиттєвих",
          sorted(al2) == ["Сєнкевич О."], f"{al2}")

    # --- ГОЛОВНЕ: рішення людини має триматись, а не розвалюватись назавтра ---
    # Зіставлення в write_results іде за name_ua/name_ru, аліаси в ньому участі
    # НЕ беруть — тож без пам'яті рішень наступна стаття зі словом «Сєнкевич»
    # створювала б картку заново, і Олег зливав би той самий дубль щотижня.
    bot_db.execute(
        "INSERT INTO articles (id, published, title_ua) VALUES (6009, %s, %s) "
        "ON CONFLICT (id) DO NOTHING", (1700000000, "Нова стаття"))
    res2 = em.merge_cards(501, 502, "тест")     # зливаємо ще раз після відкату
    n_before = bot_db.query("SELECT count(*) AS n FROM entities")[0]["n"]
    ep.write_results([{"article_id": 6009, "entities": [
        {"kind": "person", "name_ua": "Сєнкевич", "name_ru": None,
         "role": "мер", "salience": "mentioned"}]}])
    n_after = bot_db.query("SELECT count(*) AS n FROM entities")[0]["n"]
    check("дубль НЕ відроджується: нова стаття з «Сєнкевич» іде в злиту картку",
          n_after == n_before, f"{n_before} → {n_after}")
    who = bot_db.query(
        "SELECT entity_id FROM article_entities WHERE article_id = 6009")
    check("зв'язок повісився саме на картку-переможця",
          [r["entity_id"] for r in who] == [501], f"{who}")

    em.restore_merge(res2["merge_id"])
    rules = bot_db.query(
        "SELECT count(*) AS n FROM entity_merge_rules WHERE merge_id = %s",
        (res2["merge_id"],))[0]["n"]
    check("відкат знімає і пам'ять рішення, а не лише дані", rules == 0, f"{rules}")

    # --- сміття в пошуку: event/document ховаються ---
    # Реальний прогін /entity_find трамп повернув 15 карток, з них 12 —
    # переказ сюжету статті («замах на Дональда Трампа», «Трамп: Другий шанс?»).
    # Зливають людей, організації й місця, тож у пошуку злиття їх бути не має.
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua, mentions) VALUES "
        "(601, 'person', 'Дональд Трамп', 1310), "
        "(602, 'person', 'Дональд Дж. Трамп', 1), "
        "(603, 'event', 'замах на Дональда Трампа', 2), "
        "(604, 'event', 'Трамп: Другий шанс?', 1) "
        "ON CONFLICT (id) DO NOTHING")
    like = "%трамп%"
    vis = bot_db.query(em.FIND_SQL, (like, like, like, False,
                                     list(em.MERGEABLE_KINDS)))
    check("подій і документів у пошуку злиття не видно",
          {r["id"] for r in vis} == {601, 602},
          f"{[(r['id'], r['kind']) for r in vis]}")
    hid = bot_db.query(em.FIND_HIDDEN_SQL, (like, like, list(em.MERGEABLE_KINDS)))
    check("але сховане порахували й показали числом",
          hid and hid[0]["kind"] == "event" and hid[0]["n"] == 2, f"{hid}")
    allrows = bot_db.query(em.FIND_SQL, (like, like, like, True,
                                         list(em.MERGEABLE_KINDS)))
    check("«all» повертає й сміття — подивитись на нього теж треба чимось",
          len(allrows) == 4, f"{len(allrows)}")
    txt = em._find_text({"q": "трамп", "hidden": [["event", 2]], "sel": [],
                         "items": [{"id": 601, "name": "Дональд Трамп",
                                    "kind": "person", "mentions": 1310,
                                    "lo": None, "hi": None, "role": None,
                                    "aliases": ""}]})
    check("у тексті пояснено, ЧОМУ сховано, і як показати",
          "Сховано" in txt and "all" in txt, txt.replace("\n", " | "))
    bot_db.execute("DELETE FROM entities WHERE id IN (601, 602, 603, 604)")

    # --- вибір кнопками: порядок тапів вирішує, хто лишається ---
    state = {"q": "сєнкевич", "sel": [], "items": [
        {"id": 501, "name": "Олександр Сєнкевич", "kind": "person",
         "mentions": 2509, "lo": "2024-05", "hi": "2026-07", "role": "мер",
         "aliases": ""},
        {"id": 502, "name": "Сєнкевич", "kind": "person", "mentions": 1,
         "lo": "2026-07", "hi": "2026-07", "role": "мер", "aliases": ""},
        {"id": 503, "name": "Катерина Сєнкевич", "kind": "person", "mentions": 3,
         "lo": "2025-04", "hi": "2026-04", "role": None, "aliases": ""}]}
    state["sel"] = [501, 502]
    kb = em._find_markup(state)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    check("перша обрана картка підписана «лишиться», друга — «зникне»",
          any(l.startswith("✅ лишиться") and "Олександр" in l for l in labels)
          and any(l.startswith("🗑 зникне") and l.count("Сєнкевич") for l in labels),
          f"{labels}")
    check("кнопка злиття зʼявляється лише при двох обраних",
          any("Злити" in l for l in labels)
          and not any("Злити" in b.text
                      for row in em._find_markup({**state, "sel": [501]}).inline_keyboard
                      for b in row))
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("callback_data вкладається в ліміт Telegram",
          all(len(c.encode()) <= 64 for c in cbs), f"{cbs}")

    # стан переживає редеплой — лежить у sync_state, а не в памʼяті процесу
    em._find_save(-100, 777, state)
    got = em._find_load(-100, 777)
    check("вибір карток переживає редеплой (стан у норі)",
          got and got["sel"] == [501, 502], f"{got and got.get('sel')}")
    bot_db.execute("DELETE FROM sync_state WHERE key LIKE %s",
                   (em.FIND_STATE_PREFIX + "%",))

    bot_db.execute("DELETE FROM entity_merge_rules")
    bot_db.execute("DELETE FROM article_entities WHERE article_id >= 6000")
    bot_db.execute("DELETE FROM articles WHERE id >= 6000")
    bot_db.execute("DELETE FROM entities WHERE id IN (501, 502, 503)")


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
    test_bulk()
    test_norm_version()
    test_queue_and_merge()
    test_carrier_counts()
    test_outliers()
    test_org_suggestion_prefers_name()
    test_affiliation()
    test_rejection_memory_and_rollback()
    test_card_merge_journal()
    test_manual_merge()
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
