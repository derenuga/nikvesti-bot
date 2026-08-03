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
        for aid, published in ((700001, 1690000000), (700002, 1790000000),
                               (700003, 1790100000), (700004, 1790200000),
                               (700005, 1790300000)):
            cur.execute(
                "INSERT INTO articles (id, published, status, title_ua, slug, "
                "category, kind, text_ua) VALUES (%s,%s,1,%s,%s,'municipal','news',%s) "
                "ON CONFLICT (id) DO UPDATE SET published = EXCLUDED.published",
                (aid, published, f"Стаття {aid}", f"st-{aid}", "текст"))
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
    ok = sum(1 for _, o, _ in RESULTS if o)
    print(f"\n{ok}/{len(RESULTS)} перевірок пройдено")
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
