"""
Тест прибирання сміттєвих карток на ЖИВОМУ Postgres
(handlers/entity_junk.py + журнал видалень у handlers/entity_merge.py).

Не мок: піднімає реальну схему нори, кладе статті, картки й зв'язки — і
перевіряє рівно ті інваріанти, через які §3 docs/ENTITY_DATA_FIXES.md узагалі
можна виконувати автоматично.

**Головні інваріанти (кожен — окремий спосіб зіпсувати дані назавжди):**

- прибирається ЛИШЕ те, що не повторюється між статтями: картка з двох статей
  щось зв'язує, і чіпати її не можна, скільки б сюжетно вона не виглядала;
- люди, організації й місця не прибираються НІКОЛИ, навіть з однією згадкою
  (рідкісний носій ≠ помилка, §2 доку);
- усе, чого вже торкалась людина або довідник (правило злиття, журнал, орган
  канону посади), у прибирання не потрапляє;
- КОЖНЕ видалення лишає знімок у entity_merges, і з нього картка
  відновлюється повністю: id, імена, аліаси, зв'язки з role_at_time і
  salience. Без цього прибирання незворотне, а значить заборонене;
- відкат прогону цілком — одна команда: три тисячі карток руками не повернеш;
- сирий role_at_time при цьому не переписується — ані при прибиранні, ані при
  відкаті;
- посада, записана організацією, знаходиться за сигналом, а не за здогадкою,
  і «Офіс Президента України» під нього не підпадає.

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_entity_junk.py
"""

import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, entity_junk as ej          # noqa: E402
from handlers import entity_merge as em, entity_roles as er   # noqa: E402
import entity_pipeline as ep                            # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


# ---------- дані ----------
#
# Картки навмисно взяті з живої нори (§3 доку): «замах на Дональда Трампа» —
# переказ сюжету однієї статті, «Президент США» — посада, записана org,
# «Офіс Президента України» — справжня установа, яку чіпати не можна.
ARTICLES = [(9000 + i, 1780000000 + i * 86400) for i in range(6)]

CARDS = [
    # (id, kind, name_ua, статті)
    (7001, "event", "замах на Дональда Трампа", [9000]),          # одноразова
    (7002, "event", "Трамп: Другий шанс?", [9001]),               # одноразова
    (7003, "document", "рішення про бюджет-2026 Миколаєва", [9002]),  # одноразова
    (7004, "event", "повномасштабне вторгнення", [9000, 9001, 9002]),  # повтор
    (7005, "document", "Закон про мобілізацію", [9003, 9004]),    # повтор
    (7006, "person", "Таміла Ксьонжик", [9000, 9001, 9005]),      # людина з роллю
    (7007, "place", "Вознесенськ", [9005]),                       # місце, 1 стаття
    (7008, "event", "картка без жодного зв'язку", []),            # сирота
    # захищені: правило злиття / журнал / орган канону посади
    (7009, "event", "картка з правилом злиття", [9000]),
    (7010, "document", "картка-переможець злиття", [9001]),
    (7011, "event", "картка-орган канону", [9002]),
    # org: посади й справжні установи
    (7020, "org", "Президент США", [9000, 9001]),
    (7021, "org", "Президент України", [9002]),
    (7022, "org", "Офіс Президента України", [9003, 9004]),
    (7023, "org", "Миколаївська міська рада", [9005]),
    (7024, "org", "Офіс президента", [9000]),
]

# Роль, під якою «Президент США» ЖИВЕ по-справжньому — у зв'язку людини.
ROLE_LINKS = [(9000, 7006, "Президент США"), (9001, 7006, "президент США")]


def setup():
    bot_db.ensure_schema()
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute(ep.MERGE_RULES_DDL)
        cur.execute(em.MERGES_DDL)
        cur.execute("DELETE FROM article_entities")
        cur.execute("DELETE FROM entities")
        cur.execute("DELETE FROM articles")
        cur.execute("DELETE FROM entity_merge_rules")
        cur.execute("DELETE FROM entity_merges")
    conn.close()
    er.ensure_schema(force=True)
    em.ensure_schema(force=True)
    bot_db.execute("DELETE FROM role_variants")
    bot_db.execute("DELETE FROM role_canon")
    bot_db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for aid, pub in ARTICLES:
        bot_db.execute(
            "INSERT INTO articles (id, published, title_ua, text_ua) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (aid, pub, f"Стаття {aid}", f"Текст {aid}"))
    for eid, kind, name, arts in CARDS:
        bot_db.execute(
            "INSERT INTO entities (id, kind, name_ua, aliases, mentions) "
            "VALUES (%s, %s, %s, %s, %s)",
            (eid, kind, name, [f"{name} (варіант)"], len(arts)))
        for aid in arts:
            bot_db.execute(
                "INSERT INTO article_entities (article_id, entity_id, salience) "
                "VALUES (%s, %s, 'mentioned') ON CONFLICT DO NOTHING", (aid, eid))
    for aid, eid, role in ROLE_LINKS:
        bot_db.execute(
            "UPDATE article_entities SET role_at_time = %s "
            "WHERE article_id = %s AND entity_id = %s", (role, aid, eid))
    bot_db.execute(
        "SELECT setval(pg_get_serial_sequence('entities', 'id'), "
        "GREATEST((SELECT max(id) FROM entities), 1))")

    # Три способи «людина вже торкалась картки» — усі мають захищати.
    bot_db.execute(
        "INSERT INTO entity_merge_rules (kind, norm, entity_id, created) "
        "VALUES ('event', 'написання з правила', 7009, 1780000000)")
    bot_db.execute(
        "INSERT INTO entity_merges (winner_id, loser_id, loser_snapshot, created) "
        "VALUES (7010, 7999, '{\"card\": {}, \"links\": []}'::jsonb, 1780000000)")
    bot_db.execute(
        "INSERT INTO role_canon (canon, canon_norm, org_entity_id, created) "
        "VALUES ('очільник міста', 'очільник міста', 7011, 1780000000)")


# ---------- перевірки ----------

def test_scan():
    scan = ej.scan_oneoff()
    ids = set(ej.oneoff_ids())
    check("одноразові event/document відібрано всі",
          ids >= {7001, 7002, 7003, 7008}, str(sorted(ids)))
    check("картка з повтором між статтями НЕ прибирається",
          not ({7004, 7005} & ids), str(sorted(ids)))
    check("люди й місця не прибираються ніколи — навіть місце з однієї статті",
          not ({7006, 7007} & ids), str(sorted(ids)))
    check("захищені картки (правило / журнал / орган канону) не в вибірці",
          not ({7009, 7010, 7011} & ids), str(sorted(ids)))
    check("захищених порахували окремо й показали числом",
          scan["protected"] == 3, f"protected={scan['protected']}")
    check("повторювані порахували як ті, що лишаються",
          scan["repeated"] == 2, f"repeated={scan['repeated']}")
    check("масштаб у звіті = розмір вибірки на прибирання",
          scan["total"] == len(ids), f"{scan['total']} vs {len(ids)}")
    text = ej._oneoff_text(scan)
    check("у звіті видно і скільки прибираємо, і скільки лишається",
          str(scan["total"]) in text and "Лишаються працювати" in text,
          text.split("\n")[0])
    return ids


def test_purge_and_undo(ids):
    res = ej.purge_cards(sorted(ids), "oneoff", "тест")
    check("прибрано рівно стільки, скільки показано",
          res["removed"] == len(ids), f"{res['removed']} vs {len(ids)}")
    left = {r["id"] for r in bot_db.query(
        "SELECT id FROM entities WHERE id = ANY(%s)", (sorted(ids),))}
    check("карток у норі не лишилось", not left, str(sorted(left)))
    links = bot_db.query(
        "SELECT count(*) AS n FROM article_entities WHERE entity_id = ANY(%s)",
        (sorted(ids),))[0]["n"]
    check("зв'язки прибраних карток теж знято", links == 0, f"links={links}")
    survived = {r["id"] for r in bot_db.query(
        "SELECT id FROM entities WHERE id = ANY(%s)",
        ([7004, 7005, 7006, 7007, 7009, 7010, 7011],))}
    check("сусіди цілі — повторювані, люди, місця, захищені",
          survived == {7004, 7005, 7006, 7007, 7009, 7010, 7011},
          str(sorted(survived)))

    log = bot_db.query(
        "SELECT id, winner_id, loser_id, loser_snapshot ->> 'op' AS op, "
        "       loser_snapshot ->> 'run' AS run "
        "FROM entity_merges WHERE loser_snapshot ->> 'op' = 'purge' ORDER BY id")
    check("кожне видалення лишило знімок у журналі",
          len(log) == len(ids), f"{len(log)} знімків на {len(ids)} карток")
    check("знімок видалення позначений переможцем 0 (переможця немає)",
          all(r["winner_id"] == em.PURGE_WINNER for r in log))
    check("усі знімки прогону мають спільну мітку",
          {r["run"] for r in log} == {res["run"]}, str({r["run"] for r in log}))

    n = ej.undo_run(res["run"])
    check("відкат прогону однією командою повернув усі картки",
          n == len(ids), f"{n} vs {len(ids)}")
    back = {r["id"]: r for r in bot_db.query(
        "SELECT id, kind, name_ua, aliases, mentions FROM entities "
        "WHERE id = ANY(%s)", (sorted(ids),))}
    check("картки повернулись із ТИМИ САМИМИ id", set(back) == ids,
          str(sorted(back)))
    orig = {eid: (kind, name) for eid, kind, name, _ in CARDS}
    check("імена, типи й аліаси на місці",
          all(back[i]["kind"] == orig[i][0] and back[i]["name_ua"] == orig[i][1]
              and back[i]["aliases"] == [f"{orig[i][1]} (варіант)"] for i in back))
    restored_links = bot_db.query(
        "SELECT count(*) AS n FROM article_entities WHERE entity_id = ANY(%s)",
        (sorted(ids),))[0]["n"]
    expected = sum(len(a) for eid, _k, _n, a in CARDS if eid in ids)
    check("зв'язки повернулись усі", restored_links == expected,
          f"{restored_links} vs {expected}")
    check("повторний відкат нічого не дублює", ej.undo_run(res["run"]) == 0)
    arts = {eid: len(a) for eid, _k, _n, a in CARDS}
    check("агрегати перераховані з даних, а не зі знімка",
          all(r["mentions"] == arts[r["id"]] for r in back.values()),
          str({i: back[i]["mentions"] for i in sorted(back)}))


def test_role_untouched():
    rows = bot_db.query(
        "SELECT role_at_time FROM article_entities "
        "WHERE entity_id = 7006 ORDER BY article_id")
    check("сирий role_at_time не переписаний ані прибиранням, ані відкатом",
          [r["role_at_time"] for r in rows] == ["Президент США", "президент США", None],
          str([r["role_at_time"] for r in rows]))


def test_positions():
    pos = ej.scan_positions()
    ids = {p["id"] for p in pos}
    check("посаду, записану org, знайдено", {7020, 7021} <= ids, str(sorted(ids)))
    check("справжні установи в кандидати не потрапили",
          not ({7022, 7023} & ids), str(sorted(ids)))
    hit = next(p for p in pos if p["id"] == 7020)
    check("сигнал «трапляється роллю» порахований і підписаний",
          hit["as_role"] == 2 and "роллю" in hit["signals"], hit["signals"])
    hit2 = next(p for p in pos if p["id"] == 7021)
    check("посада без ролі в норі ловиться першим словом",
          hit2["as_role"] == 0 and "перше слово" in hit2["signals"],
          hit2["signals"])
    check("найпевніші (з роллю) стоять першими", pos[0]["id"] == 7020,
          str([p["id"] for p in pos]))

    res = ej.purge_cards([7020], "position_as_org", "тест")
    gone = bot_db.query("SELECT id FROM entities WHERE id = 7020")
    check("посада прибирається поштучно", not gone and res["removed"] == 1)
    check("відкат посади — звичайний /entity_unmerge",
          em.restore_merge(res["merge_ids"][0]).get("purge") is True)
    back = bot_db.query("SELECT name_ua FROM entities WHERE id = 7020")
    check("картка повернулась із іменем", back and back[0]["name_ua"] == "Президент США",
          str(back))


def test_org_dupes():
    pairs, skipped = ej.find_org_dupes()
    got = {(p["winner"][0], p["loser"][0]) for p in pairs}
    check("«Офіс президента» знайдено дублем «Офісу Президента України»",
          (7022, 7024) in got, str(sorted(got)))
    check("різні органи в кандидати не лізуть",
          not any(7023 in (p["winner"][0], p["loser"][0]) for p in pairs),
          str(sorted(got)))
    check("лишається ПОВНІША офіційна назва, а не частіша",
          all(p["winner"][0] == 7022 for p in pairs if p["loser"][0] == 7024))
    # «Президент України» схожий на «Офіс Президента України» так само, як
    # «Офіс президента» — але це посада, і місце їй у /entity_junk.
    check("картка-посада в дублі органів не пропонується",
          not any(7021 in (p["winner"][0], p["loser"][0]) for p in pairs)
          and skipped >= 1, f"skipped={skipped} · {sorted(got)}")
    before = bot_db.query("SELECT count(*) AS n FROM entities")[0]["n"]
    ej.find_org_dupes()
    after = bot_db.query("SELECT count(*) AS n FROM entities")[0]["n"]
    check("пошук дублів нічого не зливає сам", before == after)


def run():
    setup()
    ids = test_scan()
    test_purge_and_undo(ids)
    test_role_untouched()
    test_positions()
    test_org_dupes()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} перевірок пройдено")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
