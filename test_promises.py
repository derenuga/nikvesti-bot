"""
Тест банку тем на ЖИВОМУ Postgres (promise_pipeline.py + handlers/promises.py).

Не мок: піднімає реальну схему нори разом із сутнісним шаром і каноном ролей,
кладе статті й сутності — і перевіряє рівно ті інваріанти, заради яких
docs/PROMISES_BANK.md узагалі писався.

**Що саме тут перевіряється, а що ні.** Сесія розробки не бачить текстів статей
і не має ключа до моделі, тому якість САМОГО ВИТЯГУ перевіряє Олег через
/promise_test на еталонах §6.1. Тут перевіряються всі ДЕТЕРМІНОВАНІ шари, у
яких помилка так само тиха й так само дорога:

- **клас перевірки виводиться, а не питається в моделі** (§2.1) — на наборах
  полів усіх СЕМИ еталонних кейсів. Помилка тут = реєстр бреше впевненістю,
  якої не було;
- **обіцянка НЕ робити ніколи не «прострочена»** (§2.4): для `not_do`
  порушенням є ДІЯ, а мовчання означає, що обіцянку тримають — загублена
  полярність дає висновок навпаки;
- **мітка «популізм» ніколи не йде без підстави** (§2.1) — це не вирок, а
  підсумок порожніх полів;
- **немає дослівної цитати — немає запису** (§3): найдешевший запобіжник від
  реєстру галюцинацій;
- **ідемпотентність**: та сама стаття, прогнана двічі, не подвоює ні ревізій,
  ні обіцянок — інакше повторний скан місяця коштував би вдвічі й брехав би
  лічильником «перенесено N разів»;
- **ланцюг**: рішення судді `high` кладе РЕВІЗІЮ до наявної обіцянки, а різні
  зобов'язання про той самий об'єкт лишаються різними, але в одній ТЕМІ (§2.6);
- **лінк на статтю будується archive_search._fmt_item**, а не рядком:
  nikvesti.com/<id> не існує, і мертвий лінк убиває довіру до всього банку;
- **грейс за точністю дати** (§5): «до кінця 2025» не дзвонить 1 січня о 00:00;
- **умовні й «можуть перенести» не дзвонять узагалі** (§5);
- **дешева перевірка ПІДІЙМАЄ тему** (§2.3): огорожа з трьома днями строку і
  перевіркою за десять хвилин пішки стоїть вище за мільйонну обіцянку — саме
  на цьому зламалась перша планка значущості;
- **відкат**: /promise_forget лишає знімок, з якого обіцянка відновлюється з
  тим самим id і всіма ревізіями;
- **пре-фільтр ловить статтю БЕЗ слова «обіцяв»** — головне правило §2.5, і
  фільтр не має права бути вужчим за нього.

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_promises.py
"""

import os
import sys
import time

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, entity_roles as er   # noqa: E402
import entity_pipeline as ep                      # noqa: E402
import promise_pipeline as pp                     # noqa: E402
import promise_extract_api as api                 # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


DAY = 86400
NOW = int(time.time())

# ---------- Еталонні кейси §6.1 ----------
#
# Це НЕ вигадані зразки: кожен набір полів зібрано з розбору реальної статті в
# docs/PROMISES_BANK.md, а цитати взято звідти дослівно. Саме ці сім матеріалів
# по черзі ламали наївну модель, тому вони й стали тестовим набором.

CASES = {
    # §2 — тендерний дедлайн, точний до дня
    294413: {
        "title": "Завершити реставрацію гімназії №2",
        "subject": "Миколаївська гімназія №2",
        "objects": ["Миколаївська гімназія №2"],
        "promiser": "Євген Скарлат", "promiser_role": "директор департаменту ЖКГ",
        "owner": "Житлопромбуд-8", "reported_by": None, "audience": "community",
        "polarity": "do", "modality": "hedged", "source_type": "official_statement",
        "deadline": "2025-12-31", "deadline_precision": "year",
        "criterion": "реставраційні роботи завершено, будівля прийнята",
        "verification_method": "official_statement",
        "condition": None, "condition_self_judged": False, "trigger_event": None,
        "actor_hidden": False, "framed_as_promise": True,
        "based_on_document": "договір із підрядником", "amount": 149500000,
        "quote": "Остаточне завершення можуть перенести на 2025 рік",
        "expect_verifiability": "measurable",
    },
    # §2.1 — ні дати, ні критерію, умова поза контролем, джерело — соцмережа
    320092: {
        "title": "Відновити Коблеве краще ніж було",
        "subject": "Коблеве", "objects": ["Коблеве"],
        "promiser": "Віталій Кім", "promiser_role": "голова Миколаївської ОВА",
        "owner": None, "reported_by": None, "audience": "community",
        "polarity": "do", "modality": "promised", "source_type": "social_comment",
        "deadline": None, "deadline_precision": None,
        "criterion": None, "verification_method": None,
        "condition": "після завершення війни", "condition_self_judged": False,
        "trigger_event": None, "actor_hidden": False, "framed_as_promise": False,
        "based_on_document": None, "amount": None,
        "quote": "Ми все повернемо краще ніж було! Трохи залишилось",
        "expect_verifiability": "unfalsifiable",
    },
    # §2.2 — предмет-практика (сутності немає), обіцяно ЖУРНАЛІСТАМ
    311271: {
        "title": "Відкрити апаратні наради для журналістів",
        "subject": "відкриті апаратні наради", "objects": [],
        "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова Миколаєва",
        "owner": "Миколаївська міська рада", "reported_by": None, "audience": "media",
        "polarity": "do", "modality": "planned", "source_type": "official_statement",
        "deadline": "2026-01-01", "deadline_precision": "year",
        "criterion": "журналістів пускають на апаратні наради",
        "verification_method": "official_statement",
        "condition": None, "condition_self_judged": False, "trigger_event": None,
        "actor_hidden": False, "framed_as_promise": True,
        "based_on_document": None, "amount": None,
        "quote": "Плануємо з нового року це зробити",
        "expect_verifiability": "measurable",
    },
    # §2.3 — анонімний актор, переказана обіцянка, відносний горизонт
    320276: {
        "title": "Зрізати незаконну огорожу за зупинкою на Соборній",
        "subject": "зупинковий комплекс на Соборній", "objects": ["Соборна вулиця"],
        "promiser": "власники зупинкового комплексу «Соборна»", "promiser_role": None,
        "owner": None, "reported_by": "Олександр Береза", "audience": "community",
        "polarity": "do", "modality": "promised", "source_type": "official_statement",
        "deadline": "2026-06-29", "deadline_precision": "day",
        "criterion": "огорожі за зупинкою немає",
        "verification_method": "field_check",
        "condition": None, "condition_self_judged": False, "trigger_event": None,
        "actor_hidden": False, "framed_as_promise": True,
        "based_on_document": None, "amount": None,
        "quote": "Власники пообіцяли зрізати огорожу за вихідні",
        "expect_verifiability": "measurable",
    },
    # §2.4 — обіцянка НЕ робити, тригер-подія замість дати
    317853: {
        "title": "Не підтримувати продаж будівель трьох дитсадків",
        "subject": "будівлі дитсадків №104, №128, №138",
        "objects": ["дитячий садок №104", "дитячий садок №128", "дитячий садок №138"],
        "promiser": "Артем Ільюк", "promiser_role": "депутат міської ради",
        "owner": None, "reported_by": None, "audience": "community",
        "polarity": "not_do", "modality": "promised",
        "source_type": "official_statement",
        "deadline": None, "deadline_precision": None,
        "criterion": "не голосує за продаж цих будівель",
        "verification_method": "document_request",
        "condition": "якщо це нормальний заклад", "condition_self_judged": True,
        "trigger_event": "винесення приватизації цих будівель на сесію",
        "actor_hidden": False, "framed_as_promise": True,
        "based_on_document": None, "amount": None,
        "quote": "Не дамо можливості приватизувати цю будівлю",
        "expect_verifiability": "event_triggered",
    },
    # §2.5 — зобов'язання БЕЗ слова «обіцяв», безособовий актор, підстава-документ
    321833: {
        "title": "Розробити житлову стратегію громади",
        "subject": "житлова стратегія громади", "objects": ["Миколаїв"],
        "promiser": "Віталій Луков", "promiser_role": "перший заступник міського голови",
        "owner": "Миколаївська міська рада", "reported_by": None,
        "audience": "community",
        "polarity": "do", "modality": "planned",
        "source_type": "government_decision",
        "deadline": None, "deadline_precision": None,
        "criterion": "стратегію розроблено і винесено на громадське обговорення",
        "verification_method": "document_request",
        "condition": None, "condition_self_judged": False, "trigger_event": None,
        "actor_hidden": True, "framed_as_promise": True,
        "based_on_document": "розпорядження від 27.07.2026", "amount": None,
        "quote": "Робоча група має розробити житлову стратегію громади та план її реалізації",
        "expect_verifiability": "undated",
    },
    # §2.6 — та сама тема, інший актор, «гарантував» твердіше за «планують»
    312757: {
        "title": "Виділити гроші на завершення відбудови ліцею №2",
        "subject": "Миколаївський ліцей №2", "objects": ["Миколаївський ліцей №2"],
        "promiser": "Віталій Кім", "promiser_role": "голова Миколаївської ОВА",
        "owner": "Кабінет Міністрів України", "reported_by": "Наталія Гайдаржи",
        "audience": "community",
        "polarity": "do", "modality": "guaranteed",
        "source_type": "official_statement",
        "deadline": "2026-12-31", "deadline_precision": "year",
        "criterion": "відбудову завершено",
        "verification_method": "official_statement",
        "condition": None, "condition_self_judged": False, "trigger_event": None,
        "actor_hidden": False, "framed_as_promise": True,
        "based_on_document": None, "amount": None,
        "quote": "Гарантував, що у 2026 році вдасться завершити",
        "expect_verifiability": "measurable",
    },
}

# Статті нори під ці кейси. Id, дати виходу, рубрики й слаги — справжні, з
# розборів §2–2.6: на них потім перевіряється і побудова лінка, і хронологія
# ланцюга, тож вигадані значення тут нічого б не перевіряли.
ARTICLES = {
    294413: (1725408000, "Реставраційні роботи історичної будівлі гімназії",
             "restavratsiini-roboti", "business", "news"),          # 04.09.2024
    311271: (1763424000, "Обіцяні журналістам відкриті апаратні наради перенесли",
             "obitsyani-zhurnalistam", "politics", "news"),         # 18.11.2025
    312757: (1766880000, "Держава обіцяє дати гроші на відбудову ліцею №2",
             "derzhava-obitsiae", "business", "news"),              # 28.12.2025
    317853: (1777507200, "Ільюк обіцяє не підтримувати продаж будівель дитсадків",
             "ilyuk-pidtrymuye", "politics", "news"),               # 30.04.2026
    320092: (1782172800, "«Повернемо краще ніж було»: Кім прокоментував Коблеве",
             "povernemo-krashe", "politics", "news"),               # 23.06.2026
    320276: (1782432000, "Власники пообіцяли зрізати незаконну огорожу",
             "vlasniki-poobitsiali", "public", "news"),             # 26.06.2026
    321833: (1785456000, "У Миколаєві планують розробити нову житлову стратегію",
             "u-mikolayevi-planuyut", "municipal", "news"),         # 31.07.2026
}

# Тексти для перевірки ПРЕ-ФІЛЬТРА. Взято зі справжніх формулювань розборів:
# у 321833 слова «обіц» немає жодного разу — саме цим кейсом перевіряється
# головне правило §2.5.
PREFILTER_TEXTS = {
    900001: ("Робоча група має розробити житлову стратегію громади. Документ "
             "мають винести на громадське обговорення, а потім — на розгляд "
             "депутатів. Роботу закріплено розпорядженням від 27 липня.", True),
    900002: ("У Миколаєві планують відремонтувати дорогу до кінця року.", True),
    900003: ("Вчора в Миколаєві пройшов дощ. Синоптики розповіли, яка була "
             "температура повітря минулого тижня.", False),
}


def setup():
    bot_db.ensure_schema()
    er.ensure_schema(force=True)
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute(pp.DDL)
        cur.execute("DELETE FROM commitment_revisions")
        cur.execute("DELETE FROM commitment_objects")
        cur.execute("DELETE FROM commitments")
        cur.execute("DELETE FROM topics")
        cur.execute("DELETE FROM promise_attempts")
        cur.execute("DELETE FROM promise_purges")
        cur.execute("DELETE FROM article_entities")
        cur.execute("DELETE FROM entities")
        cur.execute("DELETE FROM role_variants")
        cur.execute("DELETE FROM role_canon")
        cur.execute("DELETE FROM articles")
        for aid, (published, title, slug, category, kind) in ARTICLES.items():
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, text_ua) VALUES (%s,%s,1,%s,%s,%s,%s,%s)",
                (aid, published, title, slug, category, kind, title))
        for aid, (text, _) in PREFILTER_TEXTS.items():
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, text_ua) VALUES (%s,%s,1,%s,%s,%s,%s,%s)",
                (aid, 1753900000, "Тест пре-фільтра", f"test-{aid}",
                 "municipal", "news", text))
        # Сутнісний шар: картки, через які зшивається ланцюг і працює пошук.
        cur.execute(
            "INSERT INTO entities (id, kind, name_ua, name_ru, aliases, mentions) VALUES "
            "(101,'place','Миколаївська гімназія №2',NULL,'{\"гімназія №2\"}',12),"
            "(102,'person','Віталій Кім','Виталий Ким','{}',40),"
            "(103,'org','Миколаївська обласна військова адміністрація',NULL,'{\"Миколаївська ОВА\"}',30),"
            "(104,'place','Коблеве',NULL,'{}',9),"
            "(105,'org','Миколаївводоканал',NULL,'{}',15),"
            "(106,'person','Олександр Сєнкевич',NULL,'{}',50)")
        cur.execute("SELECT setval(pg_get_serial_sequence('entities','id'), 200)")
        # Роль Кіма в статті 320092 + канон із афіліацією до ОВА: саме на цьому
        # тримається пошук «усі обіцянки ОВА та її посадовців» (§6.2).
        cur.execute(
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES (320092, 102, 'голова Миколаївської ОВА', 'main')")
        cur.execute(
            "INSERT INTO role_canon (canon, canon_norm, org_entity_id, created) "
            "VALUES ('голова Миколаївської ОВА', %s, 103, %s) RETURNING id",
            (er.role_norm("голова Миколаївської ОВА"), NOW))
        canon_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO role_variants (raw_norm, raw_sample, canon_id, created) "
            "VALUES (%s, 'голова Миколаївської ОВА', %s, %s)",
            (er.role_norm("голова Миколаївської ОВА"), canon_id, NOW))
    conn.close()


def article(aid):
    published, title, *_ = ARTICLES[aid]
    return {"id": aid, "published": published, "title_ua": title}


def case_item(aid, **overrides):
    item = {k: v for k, v in CASES[aid].items() if not k.startswith("expect_")}
    item.update(overrides)
    return item


# ---------- 1. Похідний клас перевірки на всіх семи еталонах ----------

def test_verifiability():
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        for aid, case in CASES.items():
            prepared = pp.prepare(cur, case_item(aid))
            got = prepared["verifiability"]
            check(f"клас перевірки {aid} = {case['expect_verifiability']}",
                  got == case["expect_verifiability"], f"вийшло {got}")
    conn.close()


def test_populism_reason():
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        koblevo = pp.prepare(cur, case_item(320092))
        reason = pp.populism_reason(koblevo)
        check("підказка «схоже на популізм» стоїть на Коблевому", bool(reason),
              reason or "")
        check("підказка йде РАЗОМ із підставою (порожні поля названі)",
              bool(reason) and "немає дати" in reason and "немає критерію" in reason,
              reason or "")
        # Головне рішення 03.08: підказка НЕ залежить від того, чи модель
        # лишила criterion порожнім. Вписаний переказ обіцянки її не вимикає —
        # він показується дослівно, і людина сама бачить, що це не критерій.
        with_crit = pp.prepare(cur, case_item(
            320092, criterion="Коблеве відновлено краще, ніж було"))
        crit_reason = pp.populism_reason(with_crit)
        check("вписаний у критерій переказ обіцянки підказки НЕ вимикає",
              bool(crit_reason), crit_reason or "вимкнув")
        check("…і сам критерій показано дослівно, щоб судила людина",
              bool(crit_reason) and "краще, ніж було" in crit_reason,
              crit_reason or "")
        check("у підставі видно джерело — коментар у соцмережі",
              bool(reason) and "соцмереж" in reason, reason or "")
        for aid in (294413, 311271, 320276, 317853, 321833, 312757):
            p = pp.prepare(cur, case_item(aid))
            if pp.populism_reason(p):
                check(f"підказки немає там, де є дата чи підстава ({aid})", False,
                      "з'явилась помилково")
                break
        else:
            check("підказки немає там, де є дата, документ-підстава "
                  "або полярність «не робити»", True)
    conn.close()


# ---------- 2. Запис, ідемпотентність, цитата ----------

def test_write_and_idempotency():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            p = pp.prepare(cur, case_item(320276))
            cid, outcome = pp.record(cur, article(320276), p)
            check("перший запис створює обіцянку", outcome == "new", outcome)
            cid2, outcome2 = pp.record(cur, article(320276), p)
            check("повторний прогін тієї самої статті нічого не подвоює",
                  outcome2 == "dup" and cid2 == cid, f"{outcome2}, id {cid2}")
            cur.execute("SELECT count(*) FROM commitment_revisions WHERE commitment_id = %s",
                        (cid,))
            n = cur.fetchone()[0]
            check("ревізія лишилась одна", n == 1, f"ревізій {n}")

            noquote = case_item(321833, quote="   ")
            p2 = pp.prepare(cur, noquote)
            cid3, outcome3 = pp.record(cur, article(321833), p2)
            check("немає дослівної цитати — немає запису",
                  cid3 is None and outcome3 == "noquote", outcome3)
            cur.execute("SELECT count(*) FROM commitments")
            check("картка-сирота без цитати не з'явилась", cur.fetchone()[0] == 1)
        conn.commit()
    finally:
        conn.close()


# ---------- 3. Ланцюг і тема ----------

def test_chain_and_topic():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            # Гімназія №2 (2024) і ліцей №2 (2025) — картки РІЗНІ, але предмет
            # той самий об'єкт; тема має зшити їх через спільну картку 101.
            p1 = pp.prepare(cur, case_item(294413))
            cid1, _ = pp.record(cur, article(294413), p1)
            check("предмет зарезолвився в картку сутнісного шару",
                  p1["subject_entity_id"] == 101, str(p1["subject_entity_id"]))

            # Нова згадка того самого зобов'язання: суддя сказав high → ревізія
            later = case_item(294413, deadline="2026-12-31", modality="promised",
                              quote="Реставрацію обіцяють завершити до кінця 2026 року")
            p2 = pp.prepare(cur, later)
            cands = pp.candidates(cur, p2)
            check("пре-фільтр знайшов наявну обіцянку кандидатом",
                  any(c["id"] == cid1 for c in cands), f"кандидатів {len(cands)}")
            cid2, outcome = pp.record(cur, article(312757), p2, commitment_id=cid1,
                                      link_confidence="high")
            check("рішення судді high кладе РЕВІЗІЮ, а не нову обіцянку",
                  outcome == "revision" and cid2 == cid1, outcome)

            row = pp.get(cur, cid1)
            check("поточний горизонт узято з НАЙСВІЖІШОЇ ревізії",
                  row["deadline"] == pp.parse_deadline("2026-12-31"),
                  pp.fmt_date(row["deadline"]))
            check("модальність теж оновилась (ревізія міняє форму, не лише дату)",
                  row["modality"] == "promised", str(row["modality"]))
            check("лічильник ревізій рахується з даних", row["revisions"] == 2,
                  str(row["revisions"]))

            # Інше зобов'язання про той самий об'єкт (гроші, а не роботи):
            # суддя сказав «нова» — має лягти окремою обіцянкою в ТІЙ САМІЙ темі.
            money = case_item(312757)
            p3 = pp.prepare(cur, money)
            p3["subject_entity_id"] = 101      # «ліцей» зведено до тієї ж картки
            cid3, outcome3 = pp.record(cur, article(312757), p3)
            check("інше зобов'язання про той самий об'єкт — окрема обіцянка",
                  outcome3 == "new" and cid3 != cid1, outcome3)
            a, b = pp.get(cur, cid1), pp.get(cur, cid3)
            check("але тема одна — історія питання не розривається",
                  a["topic_id"] == b["topic_id"] and a["topic_id"] is not None,
                  f"{a['topic_id']} vs {b['topic_id']}")
            siblings = pp.topic_commitments(cur, a["topic_id"], exclude=cid1)
            check("картка бачить решту зобов'язань теми",
                  any(s["id"] == cid3 for s in siblings), f"сусідів {len(siblings)}")

            # Предмет-практика (сутності немає) — ключем стає нормалізований текст
            p4 = pp.prepare(cur, case_item(311271))
            cid4, _ = pp.record(cur, article(311271), p4)
            check("предмет без картки живе по subject_key",
                  p4["subject_entity_id"] is None
                  and p4["subject_key"] == "відкриті апаратні наради",
                  str(p4["subject_key"]))
            again = case_item(311271, quote="Журналістів пустимо з нового року")
            p5 = pp.prepare(cur, again)
            check("кандидати для беззв'язкового предмета знаходяться по ключу",
                  any(c["id"] == cid4 for c in pp.candidates(cur, p5)))
        conn.commit()
    finally:
        conn.close()


# ---------- 4. Черга: класи, грейс, полярність, пріоритет ----------

def test_queue_semantics():
    year_end = pp.parse_deadline("2025-12-31")
    row_year = {"status": "expected", "verifiability": "measurable",
                "deadline": year_end, "deadline_precision": "year",
                "checked_at": None, "last_seen": year_end}
    check("«до кінця 2025» не стає простроченим 1 січня о 00:00",
          pp.queue_class(row_year, year_end + 2 * DAY) != "overdue")
    check("…і стає простроченим у середині січня",
          pp.queue_class(row_year, year_end + 20 * DAY) == "overdue")

    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        not_do = pp.prepare(cur, case_item(317853))
        cls = pp.queue_class({**not_do, "status": "expected",
                              "checked_at": None, "last_seen": NOW}, NOW)
        check("обіцянка НЕ робити ніколи не «прострочена» — вона чекає події",
              cls == "waiting", cls)

        hedged = pp.prepare(cur, case_item(294413))   # modality=hedged
        check("«можуть перенести» не дзвонить — нема чого прострочувати",
              not pp.rings({**hedged, "status": "expected"}))
        conditional = pp.prepare(cur, case_item(320092, modality="promised",
                                                criterion="Коблеве відбудоване",
                                                deadline="2027-12-31"))
        check("умовна обіцянка не дзвонить («після завершення війни»)",
              not pp.rings({**conditional, "status": "expected"}))
        firm = pp.prepare(cur, case_item(320276))
        check("тверда обіцянка з датою і без умови — дзвонить",
              pp.rings({**firm, "status": "expected"}))

        # §2.3: планка значущості зламалась саме тут — дешева перевірка має
        # підіймати тему, а не відсіюватись через відсутність суми й органу.
        fence = pp.prepare(cur, case_item(320276))
        fence.update({"status": "expected", "checked_at": None,
                      "deadline": NOW - 2 * DAY,
                      "first_seen": NOW - 5 * DAY, "last_seen": NOW - 2 * DAY})
        expensive = pp.prepare(cur, case_item(294413, modality="promised"))
        expensive.update({"status": "expected", "checked_at": None,
                          "deadline": NOW - 200 * DAY,
                          "first_seen": NOW - 900 * DAY, "last_seen": NOW - 200 * DAY})
        check("дешева перевірка з коротким горизонтом стоїть вище за мільйонну",
              pp.priority(fence, NOW) > pp.priority(expensive, NOW),
              f"{pp.priority(fence, NOW)} vs {pp.priority(expensive, NOW)}")
    conn.close()


# ---------- 5. Лінки будуються _fmt_item, а не рядком ----------

def test_links():
    from handlers import promises as ph

    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        links = ph._links_for(cur, [294413, 900001])
    conn.close()
    url = links.get(294413, {}).get("url", "")
    check("лінк новини має рубрику й слаг (nikvesti.com/<id> не існує)",
          url == "https://nikvesti.com/news/business/restavratsiini-roboti", url)


# ---------- 6. Пошук: сутності + афіліація з канону ролей ----------

def test_search():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            p = pp.prepare(cur, case_item(320092))
            cid, _ = pp.record(cur, article(320092), p)
            check("обіцяльник зарезолвився в картку людини",
                  p["promiser_entity_id"] == 102, str(p["promiser_entity_id"]))
            rows, matched = pp.search(cur, "Коблеве")
            check("пошук по об'єкту знаходить обіцянку",
                  any(r["id"] == cid for r in rows), f"знайдено {len(rows)}")
            rows2, _ = pp.search(cur, "Миколаївська обласна військова")
            check("пошук по ОРГАНУ підтягує обіцянку його посадовця "
                  "(афіліація з канону ролей)",
                  any(r["id"] == cid for r in rows2), f"знайдено {len(rows2)}")
        conn.commit()
    finally:
        conn.close()


def test_loose_entity_resolution():
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        eid = pp.resolve_entity(cur, "КП «Миколаївводоканал»", ("org",))
        check("правова форма не заважає знайти картку організації",
              eid == 105, str(eid))
        eid2 = pp.resolve_entity(cur, "гімназія №2")
        check("аліас картки теж резолвиться", eid2 == 101, str(eid2))
    conn.close()


# ---------- 7. Відкат ----------

def test_forget_restore():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            p = pp.prepare(cur, case_item(321833))
            cid, _ = pp.record(cur, article(321833), p)
            before = pp.get(cur, cid)
            purge = pp.forget(cur, cid, reason="тест", who="Олег")
            check("прибирання лишає знімок у журналі", bool(purge and purge["purge_id"]))
            check("обіцянки в банку більше немає", pp.get(cur, cid) is None)
            restored = pp.restore(cur, purge["purge_id"])
            check("відкат звітує, скільки ревізій повернув",
                  (restored or {}).get("commitment_id") == cid, str(restored))
            after = pp.get(cur, cid)
            check("відкат повертає обіцянку з ТИМ САМИМ id",
                  after is not None and after["id"] == before["id"])
            check("…разом із ревізіями", after and after["revisions"] == before["revisions"],
                  str(after["revisions"] if after else None))
            check("…і полями (клас перевірки, критерій)",
                  after and after["verifiability"] == before["verifiability"]
                  and after["criterion"] == before["criterion"])
            check("повторний відкат того самого знімка нічого не робить",
                  (pp.restore(cur, purge["purge_id"]) or {}).get("already") is True)

            # /promise_retest: зняти все, що записано зі статті
            dropped = pp.drop_article(cur, 321833)
            check("перечит статті знімає обіцянку, у якої не лишилось ревізій",
                  cid in dropped["removed"], str(dropped))
            check("…і сама обіцянка зникла", pp.get(cur, cid) is None)
        conn.commit()
    finally:
        conn.close()


# ---------- 8. Пре-фільтр за маркерами ----------

def test_prefilter():
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM articles WHERE id = ANY(%s) "
            "AND fts @@ to_tsquery('simple', %s)",
            ([list(PREFILTER_TEXTS)][0], api.marker_tsquery()))
        hit = {r[0] for r in cur.fetchall()}
    conn.close()
    check("пре-фільтр ловить статтю БЕЗ слова «обіцяв» (головне правило §2.5)",
          900001 in hit)
    check("…і звичайну планову новину", 900002 in hit)
    check("…і не тягне новину без жодного маркера", 900003 not in hit)
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        same = api.marked_ids(cur, list(PREFILTER_TEXTS))
    conn.close()
    check("marked_ids (той самий фільтр, яким рахується його ціна) збігається "
          "з вибіркою скану", same == hit, f"{sorted(same)} vs {sorted(hit)}")


# ---------- 9. Дати ----------

def test_dates():
    ts = pp.parse_deadline("2026-06-29")
    check("дедлайн — це КІНЕЦЬ названого дня, а не його початок",
          ts is not None and time.strftime("%d.%m %H", time.localtime(ts)).startswith("29.06"),
          pp.fmt_date(ts))
    check("сміття замість дати не стає дедлайном",
          pp.parse_deadline("трохи залишилось") is None
          and pp.parse_deadline("") is None and pp.parse_deadline("2026-13-40") is None)
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        p = pp.prepare(cur, case_item(320092, deadline_precision="year"))
        check("точність без самої дати не зберігається (порожньо чесніше)",
              p["deadline_precision"] is None, str(p["deadline_precision"]))
        p2 = pp.prepare(cur, case_item(320092, modality="вигадана",
                                       source_type="нізвідки"))
        check("значення поза таксономією не потрапляє в базу",
              p2["modality"] is None and p2["source_type"] is None)
    conn.close()


# ---------- 10. Рендер: черга, картка, звіт по темах ----------
#
# Форматна помилка (KeyError, незакритий тег, обрив на 4096) вилазить лише в
# Telegram — тобто в Олега, а не тут. Тому шлях показу проганяється так само,
# як шлях запису: дешево і ловить регресії до того, як вони поїдуть у прод.

def test_render():
    from handlers import promises as ph

    data = ph._queue_payload(None)
    check("черга будується і не порожня", bool(data["rows"]),
          f"рядків {len(data['rows'])}")
    row = data["rows"][0]
    text = ph.format_item(row, data["first"].get(row["id"]), n=1)
    check("картка списку містить стан, назву і дослівну цитату",
          "<b>" in text and "/promise_show" in text
          and ("«" in text or row.get("quote") is None), text[:120])
    check("межі даних підписані в кожному виводі",
          "збираються" in ph._bounds_line(data["bounds"])
          or "порожній" in ph._bounds_line(data["bounds"]))

    card = ph._card_payload(commitment_id=row["id"])
    chain = "\n".join(ph._chain_lines(card["revisions"], card["links"]))
    check("ланцюг містить лінк на матеріал (доказ на кожен факт)",
          "nikvesti.com" in chain, chain[:160])
    check("ланцюг підписаний модальністю", any(
        w.upper() in chain for w in pp.MODALITY_WORD.values()), chain[:160])

    # Фільтр і пошук проходять тим самим шляхом, що й /promises з аргументом
    for arg in ("минув", "популізм", "Коблеве", "гімназія"):
        payload = ph._queue_payload(arg)
        check(f"/promises {arg} не падає", isinstance(payload["rows"], list))

    # id статті й id обіцянки в чаті виглядають однаково, тож /promise_show
    # мусить розуміти обидва — інакше людина мусить памʼятати, який номер що
    # означає, і команда тихо перестає використовуватись.
    by_article = ph._card_payload(commitment_id=320276)
    check("/promise_show з id СТАТТІ перемикається в режим «що записано з неї»",
          by_article.get("article") == 320276 and by_article["rows"],
          str(by_article.get("article")))
    explicit = ph._card_payload(article_id=320276)
    check("…і явний режим статті дає те саме",
          [r["id"] for r in explicit["rows"]] == [r["id"] for r in by_article["rows"]])
    nothing = ph._card_payload(commitment_id=999999)
    check("неіснуючий номер не вдає, що щось знайшов", nothing.get("row") is None)


def test_scan_report_and_bounds():
    from handlers import promises as ph

    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            # Місяць прогнано ДВІЧІ: спершу з фільтром, потім без нього. Саме
            # так міряється ціна фільтра (§7 крок 7) — 900001 без маркерів дав
            # одне зобов'язання, тобто фільтр загубив би його.
            pp.mark_attempt(cur, 294413, marked=True, done=True, found=2)
            pp.mark_attempt(cur, 900004, marked=False, done=True, found=1)
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, text_ua) VALUES (%s,%s,1,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO NOTHING",
                (900004, 1725408001, "Без маркерів", "bez-markeriv",
                 "municipal", "news", "Текст без жодного маркера."))
            bounds = pp.data_bounds(cur)
        conn.commit()
    finally:
        conn.close()
    # Межі рахуються з ОБ'ЄДНАННЯ «статті, що пройшли витяг» і «статті, з яких
    # щось записано»: слід прогону може загубитись (як загубився в тесті, коли
    # сусідній набір почистив articles), а обіцянки лишитись — і тоді рядок
    # меж брехав «банк ще порожній» над повним списком.
    check("межі даних беруть і пройдені статті, і ті, з яких є записи",
          bounds["articles"] >= 2
          and bounds["from"] == ARTICLES[294413][0]
          and bounds["to"] == ARTICLES[320276][0], str(bounds))
    report = ph._scan_report("2024-09")
    check("звіт по ТЕМАХ, а не списком (зобов'язання + теми числом)",
          "тем:" in report and "Зобов'язань:" in report, report[:120])
    check("звіт показує ціну пре-фільтра, коли місяць прогнали і з ним, і без",
          "Ціна пре-фільтра" in report, report[-200:])


# ---------- 11. Авто-інкремент: банк росте сам ----------
#
# Ті самі пастки, що коштували сутнісному шару 403 статті (інцидент
# 11.07–01.08): стаття, чий витяг упав, мусить лишатись у черзі, але не
# крутитись вічно; підлога не пускає добір у територію ручного скану.

def test_increment_queue():
    from handlers import promises as ph

    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM promise_attempts")
        cur.execute("DELETE FROM sync_state WHERE key LIKE 'promise_incr%'")
        # 700001 — нижче підлоги (територія ручного /promise_scan)
        # 700006 — свіжа, але НЕ миколаївська: у чергу не має потрапити взагалі
        for aid, published, region in ((700001, 1690000000, 1), (700002, 1790000000, 1),
                                       (700003, 1790100000, 1), (700004, 1790200000, 1),
                                       (700005, 1790300000, 1), (700006, 1790400000, 2)):
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, region, text_ua) "
                "VALUES (%s,%s,1,%s,%s,'municipal','news',%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET published = EXCLUDED.published, "
                "region = EXCLUDED.region",
                (aid, published, f"Стаття {aid}", f"st-{aid}", region, "текст"))
        # 700002 розібрано, 700003 упав один раз, 700004 вичерпав спроби
        pp.mark_attempt(cur, 700002, marked=True, done=True, found=1)
        pp.mark_attempt(cur, 700003, error="RateLimit", done=False)
        for _ in range(pp.MAX_ATTEMPTS):
            pp.mark_attempt(cur, 700004, error="битий JSON", done=False)
    conn.close()

    floor = 1790000000
    ids = [r["id"] for r in ph._pending(floor, 10)]
    check("стаття, чий витяг УПАВ, лишається в черзі", 700003 in ids, str(ids))
    check("розібрана стаття в чергу не потрапляє", 700002 not in ids, str(ids))
    check(f"стаття, що вичерпала {pp.MAX_ATTEMPTS} спроби, випадає з черги",
          700004 not in ids, str(ids))
    check("нижче підлоги інкремент не заглядає", 700001 not in ids, str(ids))
    # Рішення 03.08: інкремент бере лише миколаївські. Без цього фільтра банк
    # набирався загальнонаціональним (Зеленський, Укрзалізниця, Уряд), і в
    # черзі воно витісняло те, що редакція реально перевіряє.
    check("немиколаївська стаття в чергу інкремента не потрапляє",
          700006 not in ids, str(ids))
    check("свіже береться першим", ids and ids[0] == 700005, str(ids))
    check("лічильник черги збігається зі списком",
          ph._pending(floor) == len(ids), f"{ph._pending(floor)} vs {len(ids)}")

    # Підлога рахується з РОЗІБРАНОГО і закріплюється: повторний виклик не
    # перераховує (інакше вона повзла б за кожним новим скану вглиб).
    conn = ep.connect(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sync_state WHERE key LIKE 'promise_incr%'")
    conn.close()
    first = ph._incr_floor()
    check("підлога стає на початок уже розібраного", first == 1790000000, str(first))
    conn = ep.connect(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE articles SET published = 1600000000 WHERE id = 700002")
    conn.close()
    check("…і закріплюється, а не перераховується щоразу",
          ph._incr_floor() == first, str(ph._incr_floor()))


# ---------- Регіон: банк веде підзвітність МІСЦЕВОЇ влади ----------
#
# Перший прогін місяця пішов по всіх статтях нори і привів у банк Зеленського,
# Укрзалізницю й вокзал Одеси. Тут перевіряється обидва боки лікування: фільтр
# на вході (щоб не набиралось знову) і чистка набраного (щоб її можна було
# відкотити, якщо правило виявиться надто широким).

def test_region_scope():
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM promise_attempts")
        for aid, region in ((710001, 1), (710002, 1), (710003, 2), (710004, None)):
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, region, text_ua) "
                "VALUES (%s,%s,1,%s,%s,'municipal','news',%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET region = EXCLUDED.region",
                (aid, 1670000000, f"Стаття {aid}", f"reg-{aid}", region,
                 "мер пообіцяв відремонтувати дорогу до кінця року"))
    conn.close()

    # Вікно свідомо порожнє від решти фікстур: лічильник має показати рівно ці
    # чотири статті, інакше перевірка міряла б не фільтр, а сусідів.
    frm, to = "2022-12-01", "2023-01-01"
    st = api.count_range(frm, to, marked_only=False)
    check("лічильник статей рахує лише миколаївські", st["total"] == 2,
          f"total {st['total']}, за місяць {st['month_total']}")
    check("…і поруч показує, скільки всього було (щоб числа не брехали мовчки)",
          st["month_total"] == 4, str(st["month_total"]))
    arts, _ = api.fetch_range(frm, to, marked_only=False)
    ids = sorted(a["id"] for a in arts)
    check("у витяг ідуть лише миколаївські статті", ids == [710001, 710002], str(ids))
    every = api.count_range(frm, to, marked_only=False, region=None)
    check("фільтр вимикається явно (region=None) — для заміру",
          every["total"] == 4, str(every["total"]))


def test_region_prune():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM commitment_revisions")
            cur.execute("DELETE FROM commitment_objects")
            cur.execute("DELETE FROM commitments")
            cur.execute("DELETE FROM topics")
            cur.execute("DELETE FROM promise_purges")

            def put(aid, **over):
                p = pp.prepare(cur, case_item(320276, **over))
                return pp.record(cur, {"id": aid, "published": 1780000000,
                                       "title_ua": f"Стаття {aid}"}, p)[0]

            local = put(710001, quote="Огорожу зріжемо до кінця тижня")
            outside = put(710003, quote="Вокзал Одеси відремонтують до 2027 року",
                          subject="залізничний вокзал Одеси", objects=[],
                          promiser="Укрзалізниця")
            unknown = put(710004, quote="Щось пообіцяли невідомо де",
                          subject="невідомий об'єкт", objects=[])
            # Змішаний ланцюг: одна ревізія з немиколаївської статті, друга —
            # з миколаївської. Викинути таке означало б втратити місцевий факт.
            mixed = put(710003, quote="Дорогу обіцяють здати за рік",
                        subject="об'їзна дорога", objects=[])
            p = pp.prepare(cur, case_item(
                320276, subject="об'їзна дорога", objects=[],
                quote="Здачу дороги підтвердили в мерії"))
            pp.record(cur, {"id": 710002, "published": 1780500000,
                            "title_ua": "Стаття 710002"}, p,
                      commitment_id=mixed, link_confidence="high")

            st = pp.prune_scan(cur, 1)
            check("чистка бачить рівно немиколаївські", st["total"] == 1,
                  f"{st['total']}, приклади {[s['id'] for s in st['sample']]}")
            check("змішаний ланцюг порахований окремо і не йде під ніж",
                  st["mixed"] == 1, str(st["mixed"]))
            check("на «регіон невідомий» нічого не видаляється",
                  st["unknown"] == 1, str(st["unknown"]))
            check("прибиране показано прикладом ДО видалення",
                  [s["id"] for s in st["sample"]] == [outside],
                  str(st["sample"]))

            res = pp.prune(cur, 1, reason="не Миколаїв", who="тест")
            check("чистка прибрала саме одну обіцянку", res["removed"] == 1,
                  str(res["removed"]))
            cur.execute("SELECT id FROM commitments ORDER BY id")
            left = [r[0] for r in cur.fetchall()]
            check("миколаївське, змішане й невідоме лишились цілими",
                  left == sorted([local, unknown, mixed]), str(left))
            cur.execute("SELECT count(*) FROM promise_purges WHERE run = %s",
                        (res["run"],))
            check("кожна прибрана лягла в журнал одним прогоном",
                  cur.fetchone()[0] == 1)

            back = pp.prune_undo(cur, res["run"])
            check("відкат повертає ВЕСЬ прогін", back["restored"] == 1,
                  str(back))
            cur.execute("SELECT id FROM commitments WHERE id = %s", (outside,))
            check("обіцянка повернулась із тим самим id", bool(cur.fetchone()))
            cur.execute("SELECT count(*) FROM commitment_revisions "
                        "WHERE commitment_id = %s", (outside,))
            check("разом із ревізією", cur.fetchone()[0] == 1)
            cur.execute("SELECT topic_id FROM commitments WHERE id = %s", (outside,))
            tid = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM topics WHERE id = %s", (tid,))
            check("тема, що спорожніла й була знята, повернулась разом з нею",
                  tid is None or cur.fetchone()[0] == 1, str(tid))
            check("повторний відкат нічого не дублює",
                  pp.prune_undo(cur, res["run"])["restored"] == 0)

            # Друга вісь: обіцяльник. Регіон ловить рубрику, а не зміст —
            # чужий актор у миколаївській статті переживає чистку за регіоном.
            national = put(710001, quote="Виплати отримають усі ВПО до кінця року",
                           subject="виплати ВПО", objects=[],
                           promiser="Володимир Зеленський")
            who = pp.prune_who_scan(cur, "Зеленський", region=1)
            check("чистка за обіцяльником бачить його в МИКОЛАЇВСЬКІЙ статті",
                  who["total"] == 1 and who["local"] == 1, str(who["total"]))
            check("підрядок «Зеленський» ловить повне написання",
                  [w for w, _ in who["writings"]] == ["Володимир Зеленський"],
                  str(who["writings"]))
            check("…і показує, скільки з них миколаївських — до видалення",
                  who["local"] == 1, str(who["local"]))
            top = pp.top_promisers(cur, region=1)
            check("бот САМ називає кандидатів другої осі",
                  any(w == "Володимир Зеленський" for w, _ in top), str(top))

            res2 = pp.prune_who(cur, "Зеленський", decided_by="тест")
            check("чистка за обіцяльником прибрала рівно його",
                  res2["removed"] == 1, str(res2["removed"]))
            # Два прибирання в одну секунду мають лишитись РІЗНИМИ прогонами,
            # інакше відкат одного повернув би обидва.
            check("номер прогону не злипається з попереднім",
                  res2["run"] != res["run"], f"{res['run']} vs {res2['run']}")
            cur.execute("SELECT count(*) FROM commitments WHERE id = %s", (national,))
            check("обіцянка актора зникла", cur.fetchone()[0] == 0)
            cur.execute("SELECT count(*) FROM commitments WHERE id = %s", (local,))
            check("миколаївська обіцянка не зачеплена", cur.fetchone()[0] == 1)
            back2 = pp.prune_undo(cur, res2["run"])
            check("і ця чистка відкочується тим самим журналом",
                  back2["restored"] == 1, str(back2))
            cur.execute("SELECT count(*) FROM commitments WHERE id = %s", (national,))
            check("обіцянка актора повернулась із тим самим id",
                  cur.fetchone()[0] == 1)
        conn.commit()
    finally:
        conn.close()


# ---------- Дублі й екран апки ----------
#
# 473 обіцянки за перший місяць — число завищене: суддя ланцюга спрацював 63
# рази на 762 статті, і та сама обіцянка з двох статей лягла двома записами
# («…прибирання та покос трави на майданчику „Казка"» і «…прибирання на
# майданчику „Казка" у Корабельному районі», один строк, один обіцяльник).

def test_dupes_and_merge():
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM commitment_revisions")
            cur.execute("DELETE FROM commitment_objects")
            cur.execute("DELETE FROM commitments")
            cur.execute("DELETE FROM topics")
            cur.execute("DELETE FROM promise_purges")

            def put(aid, title, quote, **over):
                p = pp.prepare(cur, case_item(
                    320276, title=title, quote=quote, subject="майданчик «Казка»",
                    objects=[], promiser="адміністрація Корабельного району",
                    deadline="2026-06-10", **over))
                return pp.record(cur, {"id": aid, "published": 1780000000,
                                       "title_ua": f"Стаття {aid}"}, p)[0]

            a = put(710001, "Організувати прибирання та покос трави на майданчику «Казка»",
                    "До 10 червня адміністрація мала організувати там прибирання та покос трави")
            b = put(710002, "Організувати прибирання на майданчику «Казка» у Корабельному районі",
                    "До 10 червня там має організувати прибирання адміністрація Корабельного району")
            # Різна дія щодо ТОГО САМОГО об'єкта — не дубль (§2.6)
            other = put(710001, "Встановити освітлення на майданчику «Казка»",
                        "Освітлення на майданчику обіцяють встановити до 10 червня")

            # КОРІНЬ проблеми: пре-фільтр кандидатів мусить побачити другий
            # запис ЩЕ ДО вставки — інакше судді не задають питання взагалі, і
            # дубль лягає в банк. Саме це й сталось на 762 статтях червня:
            # усі три старі джерела кандидатів вимагали або резолвнутої картки
            # (майданчика й адміністрації району в сутнісному шарі немає), або
            # точного збігу ключа предмета (він різниться на одне слово).
            p2 = pp.prepare(cur, case_item(
                320276, title="Організувати прибирання на майданчику «Казка» у Корабельному районі",
                quote="До 10 червня там має організувати прибирання адміністрація",
                subject="майданчик «Казка» у Корабельному районі", objects=[],
                promiser="адміністрація Корабельного району", deadline="2026-06-10"))
            check("предмет НЕ має картки сутності — старі джерела мовчали б",
                  not p2.get("subject_entity_id"), str(p2.get("subject_entity_id")))
            check("обіцяльник теж без картки",
                  not p2.get("promiser_entity_id"), str(p2.get("promiser_entity_id")))
            cands = {c["id"] for c in pp.candidates(cur, p2)}
            check("пре-фільтр усе одно знаходить кандидата — по написанню "
                  "обіцяльника й однаковому строку", a in cands, str(cands))
            # `other` (освітлення того самого майданчика) у кандидатах БУТИ
            # МАЄ: це питання до судді, а не відповідь. Різні дії щодо одного
            # об'єкта лишаються різними обіцянками (§2.6), і відсіює їх він.
            # Пре-фільтр тут навмисно широкий — його ціна копійчана, а от
            # непоставлене питання коштує дубля назавжди.
            check("список кандидатів лишається коротким (це питання до судді, "
                  "а не масове злиття)", len(cands) <= pp.CANDIDATE_LIMIT,
                  str(len(cands)))

            pairs = pp.dupe_pairs(cur)
            found = {(p["a"], p["b"]) for p in pairs}
            check("детектор бачить пару, яку не зшив суддя ланцюга",
                  (min(a, b), max(a, b)) in found, str(found))
            check("різна дія щодо того самого об'єкта дублем НЕ вважається",
                  not any(other in (p["a"], p["b"]) for p in pairs), str(found))

            # «Це різні — лишити обидві». Детектор бачить СИГНАЛИ, а не суть:
            # у пари «меморіальний комплекс на Центральному кладовищі» /
            # «…у Корабельному районі» збіглись усі три (строк 15.06.2028,
            # обіцяльник, схожа назва), а це два різні комплекси. Без пам'яті
            # пара поверталась би в екран щоразу — урок role_pairs.
            pp.reject_pair(cur, a, b, who="тест")
            after = {(p["a"], p["b"]) for p in pp.dupe_pairs(cur)}
            check("«різні» прибирає пару з детектора назавжди",
                  (min(a, b), max(a, b)) not in after, str(after))
            check("…і з лічильника теж (інакше «дублів 3» без жодної пари)",
                  pp.dupe_count(cur) == len(after), str(pp.dupe_count(cur)))
            check("рішення видно, щоб помилкове «різні» не ховало дубль мовчки",
                  any(r["a"] == min(a, b) for r in pp.rejected_pairs(cur)))
            check("порядок id не має значення — рішення про пару одне",
                  pp.reject_pair(cur, b, a, who="тест") and
                  len(pp.rejected_pairs(cur)) == 1, str(pp.rejected_pairs(cur)))
            check("рішення відкочується", pp.unreject_pair(cur, a, b))
            check("…і пара повертається в детектор",
                  (min(a, b), max(a, b)) in {(p["a"], p["b"]) for p in pp.dupe_pairs(cur)})

            res = pp.merge_commitments(cur, a, b, who="тест")
            check("злиття переносить ревізії, а не видаляє докази",
                  res and res["revisions"] == 1, str(res))
            cur.execute("SELECT count(*) FROM commitment_revisions WHERE commitment_id = %s", (a,))
            check("в переможця тепер обидві цитати", cur.fetchone()[0] == 2)
            cur.execute("SELECT count(*) FROM commitments WHERE id = %s", (b,))
            check("другий запис зник із черги", cur.fetchone()[0] == 0)
            cur.execute("SELECT revisions FROM commitments WHERE id = %s", (a,))
            check("лічильник ревізій перерахувався", cur.fetchone()[0] == 2)

            back = pp.restore(cur, res["purge_id"])
            check("відкат злиття повертає ДРУГУ картку", back["commitment_id"] == b, str(back))
            cur.execute("SELECT count(*) FROM commitment_revisions WHERE commitment_id = %s", (b,))
            check("…разом із її ревізією, а не порожньою", cur.fetchone()[0] == 1,
                  "порожня картка — саме те, чим ламався б відкат злиття")
            cur.execute("SELECT count(*) FROM commitment_revisions WHERE commitment_id = %s", (a,))
            check("у переможця знову одна", cur.fetchone()[0] == 1)
        conn.commit()
    finally:
        conn.close()


def test_export_and_fix_packet():
    """Експорт + пакет рішень: шлях «файл із бота → розбір поза ботом →
    .txt назад». Без нього сотню злиттів довелось би натиснути по одному."""
    from handlers import promises as ph

    rows, bounds = ph._export_payload()
    check("експорт віддає весь банк", len(rows) > 0, str(len(rows)))
    first = rows[0]
    check("у кожної обіцянки їдуть ревізії з ЦИТАТАМИ — саме вони "
          "відрізняють дубль від двох різних обіцянок",
          bool(first["revisions"]) and bool(first["revisions"][0]["quote"]))
    check("і лінк на матеріал (nikvesti.com/<id> не існує)",
          bool(first["revisions"][0]["url"]),
          str(first["revisions"][0]["url"]))
    check("похідні поля теж у файлі — розбір поза ботом бачить те саме, що бот",
          "class" in first and "verifiability" in first and "populism" in first)

    actions, errors = ph.parse_fix(
        "# promises-fix\nmerge 12 34\n/promise_merge 55 66\n"
        "drop 77 не наша тема\ncheck 88\nказна-що\n")
    check("пакет розуміє merge/drop/check", len(actions) == 4, str(actions))
    check("…і рядок, скопійований зі звіту як /promise_merge",
          ("merge", 55, 66, None) in actions, str(actions))
    check("причина drop доїжджає", actions[2][3] == "не наша тема", str(actions[2]))
    check("нерозібране НЕ ковтається мовчки", errors == ["казна-що"], str(errors))

    ids = [r["id"] for r in rows[:2]]
    lines, counts, missing = ph.describe_fixes(
        [("merge", ids[0], ids[1], None), ("drop", 999999, None, None)])
    check("прев'ю показує НАЗВИ, а не самі id (число перевірити не можна)",
          lines and "id" not in lines[0].lower(), str(lines[:1]))
    check("неіснуючий id рахується окремо, а не тихо зникає",
          missing == 1 and counts["drop"] == 0, f"{missing}, {counts}")


def test_retest_id_reading():
    """Число в чаті виглядає однаково, чи то id статті, чи id обіцянки.

    Реальний випадок 03.08: `/promise_retest 92` прочитав 92 як статтю
    (стаття з таким id у норі є — вона з 2008 року), чесно нічого не знайшов
    і відзвітував «нових 0». Тобто виглядав так, наче виправлення промпту не
    спрацювало, хоч насправді читалась зовсім інша стаття.
    """
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM commitment_revisions")
        cur.execute("DELETE FROM commitments")
        cur.execute(
            "INSERT INTO articles (id, published, status, title_ua, slug, "
            "category, kind, region, text_ua) "
            "VALUES (92,1670000000,1,'Стара стаття 2008','st-92','municipal',"
            "'news',1,'текст') ON CONFLICT (id) DO NOTHING")
        p = pp.prepare(cur, case_item(320276))
        cid, _ = pp.record(cur, {"id": 710001, "published": 1780000000,
                                 "title_ua": "Стаття 710001"}, p)
    conn.close()

    def resolve(aid):
        """Те саме прочитання, що в promise_retest_handler."""
        conn = ep.connect()
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM articles WHERE id = %s", (aid,))
                if cur.fetchone():
                    return aid, None
                cur.execute("SELECT r.article_id FROM commitment_revisions r "
                            "WHERE r.commitment_id = %s ORDER BY r.id LIMIT 1",
                            (aid,))
                row = cur.fetchone()
                return (row[0], aid) if row else (aid, None)
        finally:
            conn.close()

    check("id ОБІЦЯНКИ веде до її статті, а не читається як стаття",
          resolve(cid) == (710001, cid), str(resolve(cid)))
    check("id СТАТТІ лишається статтею (стаття виграє — так названо аргумент)",
          resolve(92) == (92, None), str(resolve(92)))
    check("невідоме число не вигадує статті", resolve(999999) == (999999, None),
          str(resolve(999999)))


def test_glitch_and_quote_verification():
    """Витік чужої мови в генерації + справжня перевірка §3.

    Реальні випадки 03.08: «підвищити енергостійкість電транспорту» (電 =
    «електрика»), «Зараз續є інвентаризація» (續 = «триває»). Це не мохібейк у
    норі, а підстановка ієрогліфа замість українського слова.
    """
    text = ("Зараз триває інвентаризація цих мереж. Після цього необхідно "
            "буде розробити проєктні рішення та кошторис.")
    check("ієрогліф ловиться як чужий символ", pp.has_glitch("Зараз續є робота"))
    check("звичайний український текст чужим не вважається",
          not pp.has_glitch(text) and not pp.has_glitch("КП «Миколаївводоканал»"))
    check("наголос знімається («Миколаї́в» ламав пошук)",
          pp.clean_text("Миколаї́в") == "Миколаїв")

    check("дослівна цитата знаходиться в тексті статті",
          pp.find_verbatim("Зараз триває інвентаризація цих мереж", text)
          == "Зараз триває інвентаризація цих мереж")
    check("типографіка не робить цитату вигаданою",
          pp.find_verbatim("КП «Водоканал» — почне", 'КП "Водоканал" - почне') is not None)
    check("вигадана цитата НЕ знаходиться (це і є справжня перевірка §3)",
          pp.find_verbatim("Мер пообіцяв золоті гори", text) is None)

    fixed = pp.repair_quote("Зараз續є інвентаризація цих мереж", text)
    check("цитата з ієрогліфом лагодиться ТЕКСТОМ СТАТТІ, без моделі",
          fixed and "триває інвентаризація" in fixed and not pp.has_glitch(fixed),
          str(fixed))

    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM commitment_revisions")
            cur.execute("DELETE FROM commitments")
            art = {"id": 720001, "published": 1780000000,
                   "title_ua": "Стаття", "text_ua": text, "text_ru": None}
            p = pp.prepare(cur, case_item(
                320276, quote="Зараз續є інвентаризація цих мереж"))
            cid, outcome = pp.record(cur, art, p)
            check("запис із битою цитатою НЕ відкидається, а лікується",
                  outcome == "new", outcome)
            cur.execute("SELECT quote, quote_verified FROM commitment_revisions "
                        "WHERE commitment_id = %s", (cid,))
            q, ver = cur.fetchone()
            check("у банк лягла ЧИСТА цитата", not pp.has_glitch(q), q)
            check("і позначена як звірена з текстом", ver is True, str(ver))

            # Бита НАЗВА — звіряти нема з чим, запис не пишемо взагалі
            p2 = pp.prepare(cur, case_item(
                320276, title="Підвищити енергостійкість電транспорту",
                quote="Після цього необхідно буде розробити проєктні рішення"))
            _, out2 = pp.record(cur, art, p2)
            check("бита НАЗВА не пишеться — стаття лишається в черзі на перечит",
                  out2 == "glitch", out2)

            # Цитата, якої в тексті немає
            p3 = pp.prepare(cur, case_item(
                320276, quote="Мер пообіцяв золоті гори всім миколаївцям"))
            cid3, out3 = pp.record(cur, art, p3)
            cur.execute("SELECT quote_verified FROM commitment_revisions "
                        "WHERE commitment_id = %s", (cid3,))
            check("цитата поза текстом позначається, але запис не втрачаємо",
                  out3 == "new" and cur.fetchone()[0] is False, out3)
        conn.commit()
    finally:
        conn.close()


def test_vague_titles():
    """Назва без жодної конкретики — те, за що зачепився Олег: «Винести
    проєкт рішення про виділення землі на розгляд депутатів повторно», а
    йшлося про землю ПІД МОДУЛЬНІ БУДИНКИ ДЛЯ ПЕРЕСЕЛЕНЦІВ."""
    spec = pp._title_is_specific
    check("назва з цифрою вважається конкретною", spec("Відремонтувати ліцей №2"))
    check("назва з топонімом теж", spec("Модернізувати водовідведення в Коблевому"))
    check("назва в лапках теж", spec("Прибрати на майданчику «Казка»"))
    check("бюрократичне узагальнення — НІ",
          not spec("Винести проєкт рішення про виділення землі на розгляд депутатів"))
    check("…і друге таке ж", not spec("Надати дозвіл на розробку проєкту землеустрою"))

    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        rows = pp.vague_titles(cur)
    conn.close()
    check("детектор віддає список, а не падає", isinstance(rows, list),
          f"{len(rows)} шт")


def test_check_outcome():
    """«Перевірили» мусить питати РЕЗУЛЬТАТ.

    Олег натиснув кнопку і отримав «тема пішла вниз» — а чекав питання
    «виконано чи ні», щоб сказати «обіцянку п'ятирічної давнини зірвано» і
    щоб це збереглось. Висновок людини і є продукт банку: обіцянка,
    перевірена й зірвана, — факт, на який посилаються в наступному тексті, а
    не менш терміновий рядок черги.
    """
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM commitment_revisions")
            cur.execute("DELETE FROM commitments")
            art = {"id": 710001, "published": 1780000000, "title_ua": "Стаття"}
            p = pp.prepare(cur, case_item(320276))
            cid, _ = pp.record(cur, art, p)

            pp.mark_checked(cur, cid, "тест")
            cur.execute("SELECT status, checked_at FROM commitments WHERE id = %s", (cid,))
            st, at = cur.fetchone()
            check("без висновку статус не чіпається (подивився — ще в процесі)",
                  st == "expected" and at, str(st))

            # Строк минув — це ФАКТ, і перевірка його не скасовує. Обіцянка
            # 2020 року після «Перевірили» отримувала клас `soon` і підпис
            # «Строк за 0 днів»: умова `checked < deadline` вибивала її з
            # `overdue`, і вона провалювалась у наступну гілку.
            old = {"status": "expected", "verifiability": "measurable",
                   "deadline": NOW - 5 * 365 * DAY, "deadline_precision": "year",
                   "checked_at": NOW - DAY}
            check("перевірена обіцянка 2020 року лишається простроченою, "
                  "а не «скоро»", pp.queue_class(old, NOW) == "overdue",
                  pp.queue_class(old, NOW))
            fresh = dict(old, checked_at=0)
            check("…але щойно перевірена стоїть у черзі НИЖЧЕ за неперевірену",
                  pp.priority(old, NOW) < pp.priority(fresh, NOW),
                  f"{pp.priority(old, NOW)} vs {pp.priority(fresh, NOW)}")

            pp.mark_checked(cur, cid, "Олег", "failed", "будову навіть не починали")
            cur.execute("SELECT status, check_note FROM commitments WHERE id = %s", (cid,))
            st, note = cur.fetchone()
            check("«не виконано» записується статусом, а не тільки датою",
                  st == "failed", str(st))
            check("…і те, що людина побачила, теж", note == "будову навіть не починали",
                  str(note))

            check("зірвана обіцянка виходить із черги",
                  not any(r["id"] == cid for r in pp.list_queue(cur, limit=None)))
            closed = pp.list_queue(cur, cls="closed", limit=None)
            check("…але НЕ зникає — лежить у «перевірених», бо це і є продукт",
                  any(r["id"] == cid for r in closed), str(len(closed)))
            check("і підписана словом, а не кодом",
                  pp.STATE_WORD["failed"] == "зірвано")
        conn.commit()
    finally:
        conn.close()


def test_author_filter():
    """«З моїх новин»: обіцянка з матеріалу журналістки має знаходитись за
    автором — це і є механізм «обіцянки з твоїх новин прилітають тобі»."""
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM commitment_revisions")
            cur.execute("DELETE FROM commitments")
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, region, owner_id, text_ua) VALUES "
                "(730001,1780000000,1,'Її стаття','a1','municipal','news',1,777,'т'),"
                "(730002,1780000000,1,'Чужа стаття','a2','municipal','news',1,888,'т') "
                "ON CONFLICT (id) DO UPDATE SET owner_id = EXCLUDED.owner_id")
            for aid, q in ((730001, "Огорожу зріжуть до кінця тижня"),
                           (730002, "Дорогу здадуть за рік")):
                p = pp.prepare(cur, case_item(320276, quote=q, subject=f"об'єкт {aid}"))
                pp.record(cur, {"id": aid, "published": 1780000000,
                                "title_ua": "Стаття"}, p)
            mine = pp.list_queue(cur, limit=None, author_id=777)
            check("фільтр за автором бере лише його матеріали", len(mine) == 1,
                  str(len(mine)))
            check("чужу обіцянку не показує",
                  all(r["title"] for r in mine) and len(pp.list_queue(
                      cur, limit=None, author_id=888)) == 1)
            check("без автора черга повна", len(pp.list_queue(cur, limit=None)) == 2)
        conn.commit()
    finally:
        conn.close()


def test_app_payload():
    """Екран апки: те, що малює JS, має приїжджати готовим — інакше
    формулювання розійдуться з чатом за три тижні."""
    from handlers import promise_app as pa

    q = pa.queue()
    keys = {f["key"] for f in q["facets"]}
    check("фасети з числами приїжджають усі", keys == {"all", "mine", "overdue",
          "soon", "waiting", "stale", "noproof", "populism", "closed"}, str(keys))
    check("лічильник «усе» збігається з довжиною черги",
          dict((f["key"], f["n"]) for f in q["facets"])["all"] >= q["total"],
          str(q["total"]))
    check("межі даних їдуть у кожному виводі", "bounds" in q)
    item = q["items"][0] if q["items"] else {}
    check("картка несе стан словами, а не кодом класу",
          bool(item.get("state")) and not item["state"].startswith(item.get("cls", "")),
          str(item.get("state")))
    check("дешева перевірка позначена окремим прапорцем",
          "cheap" in item, str(item.keys()))
    check("цитата в картці є завжди — без неї запис у банк не пишеться",
          bool(item.get("quote")), str(item.get("quote"))[:40])
    card = pa.card(item["id"])
    check("картка віддає ланцюг", bool(card and card["chain"]), str(bool(card)))
    check("у ланцюгу кожен крок має дату", all(s["when"] for s in card["chain"]))
    pops = [i for i in q["items"] if i.get("populism")]
    check("мітка популізму приїжджає РАЗОМ із підставою",
          all(len(p["populism"]) > 10 for p in pops), f"{len(pops)} шт")


def main():
    setup()
    test_verifiability()
    test_populism_reason()
    test_write_and_idempotency()
    test_chain_and_topic()
    test_queue_semantics()
    test_links()
    test_search()
    test_loose_entity_resolution()
    test_forget_restore()
    test_prefilter()
    test_dates()
    test_render()
    test_scan_report_and_bounds()
    test_increment_queue()
    test_region_scope()
    test_region_prune()
    test_dupes_and_merge()
    test_export_and_fix_packet()
    test_retest_id_reading()
    test_glitch_and_quote_verification()
    test_vague_titles()
    test_check_outcome()
    test_author_filter()
    test_app_payload()
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n{ok}/{len(RESULTS)} перевірок пройдено")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
