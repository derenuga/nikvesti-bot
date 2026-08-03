"""
Банк тем на СПРАВЖНІХ витягах із семи еталонних статей (§6.1).

Відмінність від `test_promises.py`: там перевіряються інваріанти на зібраних
руками наборах полів, тут — увесь шлях від витягу до черги на тому, що реально
лежить у живих матеріалах МикВісті. Фікстура — `promise_eval.REFERENCE`:
читання семи публічних статей за тим самим промптом, з ДОСЛІВНИМИ цитатами.

Що це ловить і чого не ловить: якість самого витягу Haiku тут не перевіряється
(це `/promise_eval` у боті, там платні виклики). Перевіряється те, що станеться
з ПРАВИЛЬНИМ витягом далі — і саме тут виявився найдорожчий баг:

**два горизонти з одного речення.** У 294413 Скарлат каже одним реченням
«Основну частину робіт планується завершити до кінця цього року, але остаточне
завершення може бути відкладене до 2025 року». Це два зобов'язання з різними
датами й різною модальністю (§2, висновок 2). Ключ ідемпотентності «стаття +
цитата» другого з них не зберігав узагалі — і жоден тест на вигаданих даних
цього б не показав, бо вигадуючи приклад, пишеш дві різні цитати.

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_promise_eval.py
"""

import os
import sys
import time

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, entity_roles as er   # noqa: E402
import entity_pipeline as ep                      # noqa: E402
import promise_eval as pev                        # noqa: E402
import promise_extract_api as api                 # noqa: E402
import promise_pipeline as pp                     # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


# Справжні дати виходу і слаги — звірені з живими сторінками сайту.
ARTICLES = {
    294413: (1725408000, "Реставрація Миколаївської гімназії №2 затримується",
             "restavratsiini-roboti-istorichnoyi-budivli-mykolaivskoyi-gimnazii-2", "business"),
    311271: (1763424000, "Обіцяні журналістам відкриті апаратні наради перенесли",
             "obitsyani-zhurnalistam-vidkriti-aparatni-naradi", "politics"),
    312757: (1766880000, "Держава обіцяє дати гроші на завершення відбудови ліцею №2",
             "derzhava-obitsiae-dati-groshi", "business"),
    317853: (1777507200, "Ільюк за юридичну ліквідацію 3 дитсадків",
             "ilyuk-pidtrymuye-yurydychnu-likvidatsiyu", "politics"),
    320092: (1782172800, "Кім пообіцяв, що «залишилось трохи»",
             "povernemo-krashe-nizh-bulo-kim", "politics"),
    320276: (1782432000, "Власники пообіцяли зрізати за вихідні незаконну огорожу",
             "vlasniki-poobitsiali-zrizati", "public"),
    321833: (1785456000, "У Миколаєві планують розробити нову житлову стратегію",
             "u-mikolayevi-planuyut-rozrobyty-novu-zhytlovu-strategiyu", "municipal"),
}

# Картки сутнісного шару. Гімназія і ліцей — ДВІ РІЗНІ картки, як воно і є в
# норі до ручного злиття (§2.6): саме на цьому перевіряється, що станеться з
# темою, коли об'єкт перейменували.
ENTITIES = [
    (201, "place", "Миколаївська гімназія №2", None, ["гімназія №2"]),
    (202, "place", "Миколаївський ліцей №2", None, ["ліцей №2"]),
    (203, "place", "Коблеве", None, []),
    (204, "person", "Віталій Кім", "Виталий Ким", []),
    (205, "person", "Олександр Сєнкевич", None, []),
    (206, "org", "Миколаївська міська рада", None, []),
    (207, "org", "Миколаївська обласна військова адміністрація", None, ["Миколаївська ОВА"]),
    (208, "org", "Житлопромбуд-8", None, []),
    (209, "place", "проспект Центральний", None, []),
    (210, "person", "Артем Ільюк", None, []),
    (211, "person", "Віталій Луков", None, []),
    (212, "org", "Кабінет Міністрів України", None, ["КМУ"]),
]


def setup():
    bot_db.ensure_schema()
    er.ensure_schema(force=True)
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute(pp.DDL)
        for t in ("commitment_revisions", "commitment_objects", "commitments",
                  "topics", "promise_attempts", "promise_purges",
                  "article_entities", "entities", "articles"):
            cur.execute(f"DELETE FROM {t}")
        for aid, (published, title, slug, category) in ARTICLES.items():
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, text_ua) VALUES (%s,%s,1,%s,%s,%s,'news',%s)",
                (aid, published, title, slug, category, title))
        for eid, kind, ua, ru, aliases in ENTITIES:
            cur.execute(
                "INSERT INTO entities (id, kind, name_ua, name_ru, aliases, mentions) "
                "VALUES (%s,%s,%s,%s,%s,10)", (eid, kind, ua, ru, aliases))
        cur.execute("SELECT setval(pg_get_serial_sequence('entities','id'), 300)")
    conn.close()


def article(aid):
    published, title, *_ = ARTICLES[aid]
    return {"id": aid, "published": time.strftime("%Y-%m-%d", time.localtime(published)),
            "title_ua": title}


def stub_judge(prepared, candidates):
    """Замість справжнього судді — точний збіг назви зобов'язання.

    Свідомо тупий: перевіряємо НЕ якість судді (вона перевіряється грішми на
    реальному прогоні), а те, що механіка ланцюга робить із його рішенням.
    """
    same = pp.ep.norm(prepared.get("title"))
    for c in candidates:
        if pp.ep.norm(c["title"]) == same:
            return c["id"]
    return None


def ingest_reference():
    """Прогнати всі сім еталонів через справжній пайплайн у хронології."""
    stats = {"new": 0, "revision": 0, "dup": 0, "noquote": 0}
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            for aid in sorted(pev.CASE_IDS, key=lambda a: ARTICLES[a][0]):
                art = article(aid)
                for item in pev.reference_items(aid):
                    prepared = pp.prepare(cur, item)
                    cands = pp.candidates(cur, prepared)
                    cid = stub_judge(prepared, cands)
                    _, outcome = pp.record(cur, art, prepared, cid,
                                           "high" if cid else None)
                    stats[outcome] = stats.get(outcome, 0) + 1
                pp.mark_attempt(cur, aid, marked=True, done=True,
                                found=len(pev.reference_items(aid)))
        conn.commit()
    finally:
        conn.close()
    return stats


# ---------- 1. Умова приймання §6.1 ----------

def test_acceptance():
    total = fails = 0
    for aid in pev.CASE_IDS:
        for name, ok, why in pev.run_checks(aid, pev.reference_items(aid)):
            total += 1
            if not ok:
                fails += 1
                print(f"    ❌ {aid}: {name} — {why}")
    check(f"еталонне читання проходить усі {total} перевірок приймання",
          fails == 0, f"впало {fails}")
    check("умова приймання покриває всі сім матеріалів",
          all(pev.CHECKS.get(a) for a in pev.CASE_IDS))


# ---------- 2. Наскрізний прогін ----------

def test_ingest():
    stats = ingest_reference()
    total_ref = sum(len(pev.reference_items(a)) for a in pev.CASE_IDS)
    check("усі зобов'язання еталона записались",
          stats["new"] + stats["revision"] == total_ref,
          f"нових {stats['new']}, ревізій {stats['revision']}, з {total_ref}")
    check("жодного запису не втрачено як «без цитати»", stats["noquote"] == 0)

    conn = ep.connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # ГОЛОВНЕ: два горизонти з ОДНОГО речення Скарлата
            cur.execute(
                "SELECT count(*) FROM commitment_revisions WHERE article_id = 294413")
            n = cur.fetchone()[0]
            check("з однієї статті збереглись усі чотири зобов'язання", n == 4, str(n))
            cur.execute(
                "SELECT c.title, c.deadline, c.modality FROM commitments c "
                "JOIN commitment_revisions r ON r.commitment_id = c.id "
                "WHERE r.article_id = 294413 AND r.quote LIKE 'Основну частину%'")
            main_part = cur.fetchall()
            cur.execute(
                "SELECT count(*) FROM commitment_revisions "
                "WHERE article_id = 294413 AND quote LIKE '%остаточне завершення%'")
            both = cur.fetchone()[0]
            check("два різні горизонти з ОДНОГО речення обидва в банку",
                  len(main_part) == 1 and both == 2,
                  f"«основна частина» {len(main_part)}, згадок остаточного {both}")

            # Ланцюг: тендерна умова переказана в статті 2025 року
            cur.execute(
                "SELECT c.id, c.revisions FROM commitments c "
                "WHERE c.title = 'Виконати реставраційні роботи в гімназії №2 за договором'")
            rows = cur.fetchall()
            check("зобов'язання підрядника лишилось ОДНИМ на дві статті",
                  len(rows) == 1 and rows[0][1] == 2,
                  f"карток {len(rows)}, ревізій {rows[0][1] if rows else 0}")

            # §2.6: об'єкт перейменували — і теми роз'їхались. Це не баг
            # реалізації, а межа детермінованого зшивання; див. коментар нижче.
            cur.execute(
                "SELECT count(DISTINCT topic_id) FROM commitments "
                "WHERE subject_entity_id IN (201, 202)")
            topics = cur.fetchone()[0]
            check("перейменований об'єкт дає ДВІ теми (межа, а не сюрприз)",
                  topics == 2, f"тем {topics}")

            # 311271: риторика відділена від перевірюваної обіцянки
            cur.execute(
                "SELECT c.verifiability, count(*) FROM commitments c "
                "JOIN commitment_revisions r ON r.commitment_id = c.id "
                "WHERE r.article_id = 311271 GROUP BY 1")
            kinds = dict(cur.fetchall())
            check("риторика і перевірювана обіцянка лежать окремо",
                  kinds.get("measurable") and kinds.get("unfalsifiable"), str(kinds))

            # 317853: обіцянка НЕ робити ніколи не «прострочена»
            cur.execute(
                "SELECT c.id FROM commitments c WHERE c.polarity = 'not_do'")
            not_do = [r[0] for r in cur.fetchall()]
            check("обіцянка НЕ робити знайдена", len(not_do) == 1, str(not_do))
            rows = pp.list_queue(cur, limit=None)
            klass = {r["id"]: r["class"] for r in rows}
            check("…і в черзі вона «чекає події», а не «строк минув»",
                  all(klass.get(i) == "waiting" for i in not_do),
                  str({i: klass.get(i) for i in not_do}))

            # 320092: жодної вигаданої дати
            cur.execute(
                "SELECT count(*) FROM commitments c "
                "JOIN commitment_revisions r ON r.commitment_id = c.id "
                "WHERE r.article_id = 320092 AND c.deadline IS NOT NULL")
            check("у Коблевому дата не з'явилась нізвідки", cur.fetchone()[0] == 0)

            # Черга загалом
            counts = pp.facet_counts(rows)
            check("черга розкладається по класах",
                  counts.get("overdue", 0) >= 1 and counts.get("noproof", 0) >= 1,
                  str(counts))
            check("прострочені сортуються першими",
                  rows[0]["class"] == "overdue", rows[0]["class"])
    finally:
        conn.close()


def test_idempotent():
    before = ingest_reference()
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM commitments")
        n_com = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM commitment_revisions")
        n_rev = cur.fetchone()[0]
    conn.close()
    check("повторний прогін тих самих статей нічого не додав",
          before["new"] == 0 and before["revision"] == 0,
          f"нових {before['new']}, ревізій {before['revision']}, дублів {before['dup']}")
    check("кількість карток і ревізій не змінилась",
          n_com > 0 and n_rev == sum(len(pev.reference_items(a)) for a in pev.CASE_IDS),
          f"карток {n_com}, ревізій {n_rev}")


# ---------- 3. Показ ----------

def test_render():
    from handlers import promises as ph

    data = ph._queue_payload(None)
    first = data["rows"][0]
    text = ph.format_item(first, data["first"].get(first["id"]), n=1)
    check("картка черги малюється з реальних даних", "/promise_show" in text)
    card = ph._card_payload(commitment_id=first["id"])
    chain = "\n".join(ph._chain_lines(card["revisions"], card["links"]))
    check("ланцюг веде на справжні URL матеріалів",
          "nikvesti.com/news/" in chain, chain[:120])
    report = ph._scan_report("2024-09")
    check("звіт місяця будується", "Зобов'язань:" in report, report[:80])


# ---------- 4. Регресії, які вилізли в проді ----------

def test_schema_shape():
    """Схема структурованого виводу: `enum` — лише на полях без null.

    Перший прогін /promise_eval у проді впав на ВСІХ семи статтях з 400
    invalid_request_error: «Enum value 'media' does not match declared type».
    Пара «"type": ["string","null"] + enum» є валідним JSON Schema, але
    структурований вивід Anthropic її не приймає. Прод-схема витягу сутностей
    тримається того самого правила — перевіряємо тепер обидві, щоб наступне
    поле зі словником не поклало прогін знову.
    """
    import entity_backfill_api as eapi

    for name, schema in (("банк тем", api.COMMITMENT_ITEM),
                         ("сутності", eapi.ENTITY_ITEM)):
        bad = [f for f, spec in schema["properties"].items()
               if isinstance(spec.get("type"), list) and "enum" in spec]
        check(f"{name}: жодне nullable-поле не несе enum", not bad, str(bad))
    with_enum = [f for f, spec in api.COMMITMENT_ITEM["properties"].items()
                 if "enum" in spec]
    check("словники обов'язкових полів не загублені разом із фіксом",
          set(with_enum) == {"polarity", "modality", "source_type"}, str(with_enum))
    described = [f for f, spec in api.COMMITMENT_ITEM["properties"].items()
                 if f in ("audience", "deadline_precision", "verification_method")
                 and spec.get("description")]
    check("поля без enum отримали словник у description", len(described) == 3,
          str(described))


def test_eval_verdict():
    """Зелений вердикт над нульовим прогоном — найгірша брехня приймання."""
    from handlers import promises as ph

    check("витяг упав на всіх — це НЕ «пройдено»",
          "не відбулось" in ph.eval_verdict(0, 7, 0, 0))
    check("витяг упав на частині — теж не вердикт",
          "не відбулось" in ph.eval_verdict(5, 7, 20, 20))
    check("усі відпрацювали і все зелене — пройдено",
          "пройдено" in ph.eval_verdict(7, 7, 32, 32))
    check("усі відпрацювали, але є червоне — не пройдено",
          "НЕ пройдено" in ph.eval_verdict(7, 7, 30, 32))


def main():
    setup()
    test_acceptance()
    test_ingest()
    test_idempotent()
    test_render()
    test_schema_shape()
    test_eval_verdict()
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n{ok}/{len(RESULTS)} перевірок пройдено")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
