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


def test_export(ids):
    """Список файлом. Шість прикладів у чаті нічого не вирішують: серед
    одноразових карток лежить і справжнє сміття («замах на Дональда Трампа»),
    і нормальні назви, які просто ще не повторились («Гаазька конвенція
    1954»). Тому у вивантаженні мусить бути ЗАГОЛОВОК статті — без нього
    список нечитабельний."""
    data, n = ej.export_oneoff_csv()
    text = data.decode("utf-8-sig")
    check("вивантажуються рівно ті картки, що йдуть під прибирання",
          n == len(ids), f"{n} vs {len(ids)}")
    check("у файлі є заголовок статті, у якій картка живе",
          "Стаття 9000" in text, text.splitlines()[1] if len(text.splitlines()) > 1 else "")
    check("картка без жодного зв'язку теж у списку (їй статтю не покажеш)",
          "картка без жодного зв'язку" in text)
    check("повторювані картки у файл не потрапляють",
          "повномасштабне вторгнення" not in text)


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


def test_doc_canon_parser():
    """Обчислена назва посилання на закон (ep.canon_document).

    Тут дві помилки коштують по-різному. Не впізнати написання — просто
    лишити дубль, це видно й лікується. А ЗЛИТИ РІЗНІ статті — брехня в
    даних: «статті 191 та 209» це не 191-а, а 9256-д не 9256 (доопрацьований
    законопроєкт — інший документ). Тому обидва випадки тут перевіряються
    поіменно, на живих написаннях із нори."""
    same = [
        "стаття 191 КК",
        "Кримінальний кодекс України, стаття 191",
        "частина п'ята статті 191 Кримінального кодексу України",
        "кримінальне провадження за частиною 4 статті 191 КК України",
        "ч. 4 ст. 191 ККУ",
    ]
    got = {ep.canon_document(x) for x in same}
    check("усі написання однієї статті дають ОДНУ назву",
          got == {"стаття 191 КК України"}, str(got))
    check("частина статті в назву не входить (картка — це стаття)",
          ep.canon_document("частина 2 статті 115 КК України")
          == ep.canon_document("Кримінальний кодекс України, стаття 115, частина 2")
          == "стаття 115 КК України")
    for compound in ("Кримінальний кодекс України, статті 191 та 209",
                     "обвинувальний акт за статтями 109, 436-2, 114-2 "
                     "Кримінального кодексу України",
                     "Статті 410, 62 Конституції України",
                     "Кримінальний Кодекс України, частина 2 статті 28, "
                     "частини 2 статті 204"):
        check(f"складене посилання не зводиться: {compound[:34]}…",
              ep.canon_document(compound) is None,
              str(ep.canon_document(compound)))
    check("кодекс без статті не зводиться",
          ep.canon_document("Кримінальний кодекс України") is None)
    check("«статус» не читається як «стаття»",
          ep.canon_document("Про статус ветеранів війни") is None)
    check("рік не плутається з номером статті",
          ep.canon_document("стаття 5 Закону про мобілізацію від 2020 року") is None)
    check("законопроєкт зводиться за номером",
          ep.canon_document("Законопроєкт № 12000 про держбюджет на 2025 рік")
          == ep.canon_document("законопроєкт №12000") == "законопроєкт №12000")
    check("суфікс номера законопроєкту — частина номера, а не сміття",
          ep.canon_document("законопроєкт №9256-д") == "законопроєкт №9256-д"
          != ep.canon_document("законопроєкт №9256"))
    check("номер скликання лишається у верхньому регістрі",
          ep.canon_document("законопроєкт 4220-IX") == "законопроєкт №4220-IX")
    check("звичайний документ не чіпається",
          ep.canon_document("Гаазька конвенція про захист культурних цінностей "
                            "1954") is None)


def test_doc_canon_migration():
    """Наявні картки лікуються тією самою функцією, якою витяг рахує назву.

    Головне тут — що лікування не втрачає нічого: старе написання лишається
    аліасом (пошук по ньому має працювати), згадки складаються, а кожне
    злиття лежить у журналі, тож помилку можна відкотити."""
    for eid, name, arts in ((7101, "стаття 191 КК", [9000]),
                            (7102, "Кримінальний кодекс України, стаття 191",
                             [9001, 9002]),
                            (7103, "частина п'ята статті 191 "
                                   "Кримінального кодексу України", [9003]),
                            (7104, "Кримінальний кодекс України, "
                                   "статті 191 та 209", [9004])):
        bot_db.execute(
            "INSERT INTO entities (id, kind, name_ua, mentions) "
            "VALUES (%s, 'document', %s, %s)", (eid, name, len(arts)))
        for aid in arts:
            bot_db.execute(
                "INSERT INTO article_entities (article_id, entity_id, salience) "
                "VALUES (%s, %s, 'mentioned') ON CONFLICT DO NOTHING", (aid, eid))
    scan = ej.scan_doc_canon()
    check("замір бачить групу дублів однієї статті",
          any(len(v) == 3 for v in scan["groups"].values()),
          str({k: len(v) for k, v in scan["groups"].items()}))
    check("складене посилання в групу не потрапило",
          not any(c["id"] == 7104 for v in scan["groups"].values() for c in v))

    res = ej.apply_doc_canon("тест")
    left = bot_db.query(
        "SELECT id, name_ua, mentions, array_to_string(aliases, ' | ') AS al "
        "FROM entities WHERE id = ANY(%s)", ([7101, 7102, 7103, 7104],))
    ids = {r["id"] for r in left}
    check("від трьох карток лишилась одна", ids == {7102, 7104}, str(sorted(ids)))
    winner = next(r for r in left if r["id"] == 7102)
    check("вона має обчислену назву", winner["name_ua"] == "стаття 191 КК України",
          winner["name_ua"])
    check("усі старі написання лишились аліасами (пошук по них живий)",
          all(x in winner["al"] for x in
              ("стаття 191 КК", "частина п'ята статті 191 Кримінального кодексу України")),
          winner["al"])
    check("згадки склались", winner["mentions"] == 4, str(winner["mentions"]))
    check("складене посилання лишилось окремою карткою", 7104 in ids)
    check("кожне злиття лежить у журналі",
          bot_db.query("SELECT count(*) AS n FROM entity_merges "
                       "WHERE winner_id = 7102 AND undone IS NULL")[0]["n"] == 2)
    check("повторний прогін нічого не змінює (ідемпотентно)",
          ej.apply_doc_canon("тест") == {"merged": 0, "renamed": 0},
          str(ej.apply_doc_canon("тест")))


def test_doc_canon_write_results():
    """Профілактика: НОВІ статті не мають плодити ті самі дублі.

    Це та половина задачі, заради якої нормалізатор живе в write_results, а не
    в разовій команді: через нього проходять усі канали витягу — щогодинний
    інкремент, батчі архіву й /entity_resync."""
    before = {r["id"] for r in bot_db.query(
        "SELECT id FROM entities WHERE kind = 'document'")}
    ep.write_results([
        {"article_id": 9005, "entities": [
            {"kind": "document", "name_ua": "частина 3 статті 286 "
                                            "Кримінального кодексу України",
             "salience": "mentioned"}]},
        {"article_id": 9004, "entities": [
            {"kind": "document", "name_ua": "ч. 1 ст. 286 КК України",
             "salience": "mentioned"}]},
    ])
    new = bot_db.query(
        "SELECT id, name_ua, mentions, array_to_string(aliases, ' | ') AS al "
        "FROM entities WHERE kind = 'document' AND NOT (id = ANY(%s))",
        (sorted(before),))
    check("два різні написання однієї статті дали ОДНУ картку",
          len(new) == 1, str([r["name_ua"] for r in new]))
    if new:
        check("у неї обчислена назва",
              new[0]["name_ua"] == "стаття 286 КК України", new[0]["name_ua"])
        check("обидва сирі написання збереглись аліасами",
              "ч. 1 ст. 286 КК України" in new[0]["al"]
              and "частина 3 статті 286" in new[0]["al"], new[0]["al"])
        check("картка зібрала обидві статті", new[0]["mentions"] == 2,
              str(new[0]["mentions"]))
        bot_db.execute("DELETE FROM article_entities WHERE entity_id = %s",
                       (new[0]["id"],))
        bot_db.execute("DELETE FROM entities WHERE id = %s", (new[0]["id"],))
    bot_db.execute("DELETE FROM article_entities WHERE entity_id = ANY(%s)",
                   ([7101, 7102, 7103, 7104],))
    bot_db.execute("DELETE FROM entities WHERE id = ANY(%s)",
                   ([7101, 7102, 7103, 7104],))
    bot_db.execute("DELETE FROM entity_merges WHERE winner_id = 7102")


def test_org_form_key():
    """Правова форма — не інша установа (ep.org_key + міграція + витяг).

    Найбільша купа дублів у норі: 264 групи, 590 карток, 7412 згадок. Пастки
    перевірені на реальному вивантаженні: цифри в назвах («лікарня №1» проти
    «№3») мусять розрізняти установи, а «КОП» — це ІМʼЯ комунального
    підприємства шкільного харчування, а не абревіатура форми; тримати його
    у списку форм означало лишити порожній ключ."""
    check("форма зрізається, назва лишається ключем",
          ep.org_key("КП «Миколаївводоканал»") == ep.org_key("Миколаївводоканал")
          == ep.org_key("МКП «Миколаївводоканал»") == "миколаївводоканал")
    check("довга форма словами зрізається так само",
          ep.org_key("Комунальне підприємство «Миколаївські парки»")
          == ep.org_key("КП «Миколаївські парки»") == "миколаївські парки")
    check("цифра в назві лишається — це різні установи",
          ep.org_key("Миколаївська міська лікарня №3")
          != ep.org_key("Миколаївська міська лікарня №1"))
    check("«КОП» не вважається формою (це назва підприємства)",
          ep.org_key("КОП") == ep.org_key("комунальне підприємство КОП") == "коп")
    check("сама форма без назви ключа не дає",
          ep.org_key("АТ") is None and ep.org_key("КП") is None)

    for eid, name, arts in ((7201, "Миколаївводоканал", [9000, 9001, 9002]),
                            (7202, "КП «Миколаївводоканал»", [9003]),
                            (7203, "МКП «Миколаївводоканал»", [9004]),
                            (7204, "Миколаївська міська лікарня №3", [9005])):
        bot_db.execute(
            "INSERT INTO entities (id, kind, name_ua, mentions) "
            "VALUES (%s, 'org', %s, %s)", (eid, name, len(arts)))
        for aid in arts:
            bot_db.execute(
                "INSERT INTO article_entities (article_id, entity_id, salience) "
                "VALUES (%s, %s, 'mentioned') ON CONFLICT DO NOTHING", (aid, eid))
    # Канон посади показує на картку, яка ЗАРАЗ програє злиття: після нього
    # афіліація мусить переїхати на переможця, інакше вкаже в нікуди.
    cid = bot_db.query(
        "INSERT INTO role_canon (canon, canon_norm, org_entity_id, created) "
        "VALUES ('директор водоканалу', 'директор водоканалу', 7202, 1780000000) "
        "RETURNING id")[0]["id"]

    scan = ej.scan_org_forms()
    check("замір бачить групу «одна установа, різні форми»",
          any(len(v) == 3 for v in scan["groups"].values()),
          str({k: len(v) for k, v in scan["groups"].items()}))
    res = ej.apply_org_forms("тест")
    left = {r["id"]: r for r in bot_db.query(
        "SELECT id, name_ua, mentions, array_to_string(aliases, ' | ') AS al "
        "FROM entities WHERE id = ANY(%s)", ([7201, 7202, 7203, 7204],))}
    check("лишилась найзгадуваніша картка", set(left) == {7201, 7204},
          str(sorted(left)))
    check("її назва не переписана (яка форма «правильна» — не факт, а смак)",
          left[7201]["name_ua"] == "Миколаївводоканал")
    check("форми пішли в аліаси", "КП «Миколаївводоканал»" in left[7201]["al"],
          left[7201]["al"])
    check("згадки склались", left[7201]["mentions"] == 5, str(left[7201]["mentions"]))
    check("лікарня з іншим номером не зачеплена", 7204 in left)
    check("афіліація канону переїхала на переможця, а не повисла",
          bot_db.query("SELECT org_entity_id FROM role_canon WHERE id = %s",
                       (cid,))[0]["org_entity_id"] == 7201)
    check("повторний прогін нічого не зливає",
          ej.apply_org_forms("тест")["merged"] == 0)

    # І профілактика: новий витяг із формою не заводить окрему картку.
    ep.write_results([{"article_id": 9004, "entities": [
        {"kind": "org", "name_ua": "Комунальне підприємство «Миколаївводоканал»",
         "salience": "mentioned"}]}])
    after = bot_db.query(
        "SELECT id, array_to_string(aliases, ' | ') AS al FROM entities "
        "WHERE kind = 'org' AND (name_ua LIKE %s OR %s = ANY(aliases))",
        ("%иколаївводоканал%", "Комунальне підприємство «Миколаївводоканал»"))
    check("витяг із формою причепився до наявної картки, а не завів нову",
          len(after) == 1 and after[0]["id"] == 7201,
          str([(r["id"]) for r in after]))
    check("і сире написання лишилось аліасом",
          "Комунальне підприємство «Миколаївводоканал»" in (after[0]["al"] if after else ""),
          after[0]["al"] if after else "")

    bot_db.execute("DELETE FROM role_canon WHERE id = %s", (cid,))
    bot_db.execute("DELETE FROM article_entities WHERE entity_id = ANY(%s)",
                   ([7201, 7204],))
    bot_db.execute("DELETE FROM entities WHERE id = ANY(%s)", ([7201, 7204],))
    bot_db.execute("DELETE FROM entity_merges WHERE winner_id = 7201")

    # МІСЦЯ: розкриття скорочення типу — так, зрізання типу — ніколи.
    check("скорочення типу розкривається",
          ep.place_key("вул. Космонавтів") == ep.place_key("вулиця Космонавтів"))
    check("а сам тип лишається розрізнювачем: вулиця ≠ площа ≠ бульвар",
          len({ep.place_key("вулиця Лесі Українки"),
               ep.place_key("площа Лесі Українки"),
               ep.place_key("бульвар Лесі Українки")}) == 3)
    for eid, name, arts in ((7301, "вулиця Космонавтів", [9000, 9001]),
                            (7302, "вул. Космонавтів", [9002]),
                            (7303, "площа Космонавтів", [9003])):
        bot_db.execute(
            "INSERT INTO entities (id, kind, name_ua, mentions) "
            "VALUES (%s, 'place', %s, %s)", (eid, name, len(arts)))
        for aid in arts:
            bot_db.execute(
                "INSERT INTO article_entities (article_id, entity_id, salience) "
                "VALUES (%s, %s, 'mentioned') ON CONFLICT DO NOTHING", (aid, eid))
    ej.apply_org_forms("тест", "place")
    left = {r["id"] for r in bot_db.query(
        "SELECT id FROM entities WHERE id = ANY(%s)", ([7301, 7302, 7303],))}
    check("скорочення злилось із повною назвою", 7302 not in left, str(sorted(left)))
    check("площа лишилась окремою карткою — це інший об'єкт", 7303 in left)
    bot_db.execute("DELETE FROM article_entities WHERE entity_id = ANY(%s)",
                   ([7301, 7303],))
    bot_db.execute("DELETE FROM entities WHERE id = ANY(%s)", ([7301, 7303],))
    bot_db.execute("DELETE FROM entity_merges WHERE winner_id = 7301")


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
    test_export(ids)
    test_purge_and_undo(ids)
    test_role_untouched()
    test_positions()
    test_doc_canon_parser()
    test_doc_canon_migration()
    test_doc_canon_write_results()
    test_org_form_key()
    test_org_dupes()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} перевірок пройдено")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(run())
