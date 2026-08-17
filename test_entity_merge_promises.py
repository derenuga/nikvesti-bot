"""
Злиття карток НЕ РВЕ банк тем (знайдено 16.08.2026).

Що це було. Злиття перевішує на переможця зв'язки зі статтями, правила
зіставлення й орган канону ролей — усе, що існувало на момент, коли злиття
писали. Банк тем зʼявився пізніше, і його ніхто не догнав, хоча він тримає
ТРИ посилання на картки:

    commitments.subject_entity_id — предмет обіцянки
    commitments.owner_entity_id   — обіцяльник
    commitment_objects.entity_id  — обʼєкти обіцянки

Програшна картка при злитті ВИДАЛЯЄТЬСЯ. Тобто обіцянка лишалась із посиланням
у нікуди: зникала з пошуку /promises по сутності, а детектор виконання
переставав знаходити для неї новини — його пре-фільтр шукає саме за СПІЛЬНОЮ
КАРТКОЮ предмета. Мовчки, без жодної помилки на екрані: обіцянка просто
переставала перевірятись, і ніхто б не сказав, коли саме це почалось.

Що стереже:
- після злиття посилання банку показують на ПЕРЕМОЖЦЯ, а не в нікуди;
- обʼєкт, яким були ОБИДВІ картки, не падає на конфлікті ключа і не
  роздвоюється;
- /entity_unmerge повертає посилання рівно на воскреслу картку — і НЕ знімає
  з переможця те, що було його й до злиття;
- видалення сміттєвої картки (/entity_junk) знімає посилання, а відкат
  прогону їх повертає.

Запуск: потрібен Postgres. Локально:
    initdb -D /tmp/pg -U postgres -A trust && pg_ctl -D /tmp/pg -o '-p 55432' start
    BOT_DATABASE_URL=postgresql://postgres@localhost:55432/postgres \\
        python test_entity_merge_promises.py
"""
import os
import sys

if not os.environ.get("BOT_DATABASE_URL") and not os.environ.get("DATABASE_URL"):
    print("❌ Потрібен BOT_DATABASE_URL (Postgres)")
    sys.exit(1)

os.environ.setdefault("BOT_TOKEN", "1:test")

from handlers import bot_db          # noqa: E402
from handlers import entity_merge as em  # noqa: E402
import entity_pipeline as ep  # noqa: E402


def _one(sql, params=None):
    """Один рядок: у bot_db є лише query() зі списком."""
    rows = bot_db.query(sql, params)
    return rows[0] if rows else {}

SCHEMA = """
DROP TABLE IF EXISTS commitment_objects, commitments, article_entities,
                     entity_merges, entity_merge_rules, entities CASCADE;
CREATE TABLE entities (
    id BIGSERIAL PRIMARY KEY, kind TEXT, subtype TEXT, name_ua TEXT, name_ru TEXT,
    aliases TEXT[] DEFAULT '{}', role_last TEXT,
    first_seen BIGINT, last_seen BIGINT, mentions INT DEFAULT 0);
CREATE TABLE article_entities (
    article_id BIGINT, entity_id BIGINT, role_at_time TEXT, salience INT,
    PRIMARY KEY (article_id, entity_id));
CREATE TABLE commitments (
    id BIGSERIAL PRIMARY KEY, title TEXT,
    subject_entity_id BIGINT, owner_entity_id BIGINT);
CREATE TABLE commitment_objects (
    commitment_id BIGINT, entity_id BIGINT, PRIMARY KEY (commitment_id, entity_id));
"""

def setup():
    # Спершу даємо боту створити СВОЮ схему (articles з усіма колонками й
    # індексами), і лише потім кладемо поверх свої тестові таблиці — інакше
    # спрощена articles ламає індекси ensure_schema
    bot_db.query("SELECT 1")
    bot_db.execute(SCHEMA)
    bot_db.execute(em.MERGES_DDL)
    bot_db.execute("DELETE FROM articles WHERE id IN (1, 2, 3)")
    bot_db.execute("INSERT INTO articles (id, published) VALUES (1, 1750000000), "
                   "(2, 1750100000), (3, 1750200000)")
    # Дві картки одного об'єкта: «сквер Юних Героїв» і той самий сквер іншим
    # написанням. Переможець — перша (більше згадок).
    win = _one(
        "INSERT INTO entities (kind, name_ua, mentions) "
        "VALUES ('place', 'сквер Юних Героїв', 5) RETURNING id")["id"]
    lose = _one(
        "INSERT INTO entities (kind, name_ua, mentions) "
        "VALUES ('place', 'сквер юних героїв', 2) RETURNING id")["id"]
    other = _one(
        "INSERT INTO entities (kind, name_ua, mentions) "
        "VALUES ('org', 'Миколаївська міська рада', 9) RETURNING id")["id"]
    bot_db.execute("INSERT INTO article_entities (article_id, entity_id) "
                   "VALUES (1, %s), (2, %s), (3, %s)", (win, lose, lose))
    # Обіцянка про сквер: предмет і обіцяльник — на програшній картці
    c1 = _one(
        "INSERT INTO commitments (title, subject_entity_id, owner_entity_id) "
        "VALUES ('Відновити сквер', %s, %s) RETURNING id", (lose, other))["id"]
    # Друга обіцянка: обʼєктами стоять ОБИДВІ картки — перевірка конфлікту ключа
    c2 = _one(
        "INSERT INTO commitments (title, subject_entity_id) "
        "VALUES ('Освітити сквер', %s) RETURNING id", (other,))["id"]
    bot_db.execute("INSERT INTO commitment_objects (commitment_id, entity_id) "
                   "VALUES (%s, %s), (%s, %s), (%s, %s)",
                   (c1, lose, c2, lose, c2, win))
    return win, lose, other, c1, c2


def merge(win, lose):
    """Злиття тим самим шляхом, що /entity_merge: журнал → перевішування →
    видалення програшної."""
    conn = ep.connect()
    cur = conn.cursor()
    mid = em.record_merge(cur, win, lose, decided_by="test", run="t1")
    cur.execute("UPDATE article_entities ae SET entity_id = %s WHERE entity_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM article_entities x "
                "WHERE x.article_id = ae.article_id AND x.entity_id = %s)",
                (win, lose, win))
    cur.execute("DELETE FROM article_entities WHERE entity_id = %s", (lose,))
    cur.execute("DELETE FROM entities WHERE id = %s", (lose,))
    conn.commit()
    cur.close()
    conn.close()
    return mid


def main():
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(f"  {'✅' if cond else '❌'} {name}{(' — ' + extra) if extra else ''}")

    win, lose, other, c1, c2 = setup()
    print("Злиття карток і банк тем:")

    mid = merge(win, lose)

    row = _one("SELECT subject_entity_id, owner_entity_id "
                           "FROM commitments WHERE id = %s", (c1,))
    check("предмет обіцянки переїхав на переможця", row["subject_entity_id"] == win,
          f"було {lose}, стало {row['subject_entity_id']}")
    check("обіцяльник не зачеплений (він на іншій картці)",
          row["owner_entity_id"] == other)

    objs = [r["entity_id"] for r in bot_db.query(
        "SELECT entity_id FROM commitment_objects WHERE commitment_id = %s "
        "ORDER BY entity_id", (c1,))]
    check("обʼєкт обіцянки показує на переможця", objs == [win], str(objs))

    objs2 = [r["entity_id"] for r in bot_db.query(
        "SELECT entity_id FROM commitment_objects WHERE commitment_id = %s", (c2,))]
    check("обидві картки як обʼєкти — злились в одну, без дубля", objs2 == [win],
          str(objs2))

    dangling = _one(
        "SELECT count(*) AS n FROM commitments c "
        "WHERE c.subject_entity_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM entities e WHERE e.id = c.subject_entity_id)")["n"]
    check("посилань у нікуди не лишилось", dangling == 0, f"висить {dangling}")

    # --- відкат ---
    print("\nВідкат злиття:")
    em.restore_merge(mid)

    row = _one("SELECT subject_entity_id FROM commitments WHERE id = %s",
                           (c1,))
    check("предмет повернувся на воскреслу картку", row["subject_entity_id"] == lose,
          str(row["subject_entity_id"]))
    objs = sorted(r["entity_id"] for r in bot_db.query(
        "SELECT entity_id FROM commitment_objects WHERE commitment_id = %s", (c1,)))
    check("обʼєкт повернувся", objs == [lose], str(objs))
    objs2 = sorted(r["entity_id"] for r in bot_db.query(
        "SELECT entity_id FROM commitment_objects WHERE commitment_id = %s", (c2,)))
    check("а те, що було в переможця ДО злиття, лишилось у нього",
          objs2 == sorted([win, lose]), str(objs2))

    # --- видалення сміттєвої картки і відкат прогону ---
    print("\nВидалення картки (/entity_junk) і відкат:")
    conn = ep.connect()
    cur = conn.cursor()
    pid = em.record_purge(cur, lose, "тест", "run-junk", "test")
    cur.execute("DELETE FROM article_entities WHERE entity_id = %s", (lose,))
    cur.execute("DELETE FROM entities WHERE id = %s", (lose,))
    conn.commit()
    cur.close()
    conn.close()

    row = _one("SELECT subject_entity_id FROM commitments WHERE id = %s",
                           (c1,))
    check("посилання знято, а не лишилось у нікуди", row["subject_entity_id"] is None)
    em.restore_merge(pid)
    row = _one("SELECT subject_entity_id FROM commitments WHERE id = %s",
                           (c1,))
    check("відкат повернув посилання на картку", row["subject_entity_id"] == lose)

    # ПРИБИРАЄМО ЗА СОБОЮ. Тест кладе СВОЇ обрізані `commitments`,
    # `entities` тощо — рівно ті колонки, які йому потрібні. Лишена на місці,
    # така заглушка мовчки ламає наступний тест: `pp.DDL` створює таблиці
    # через IF NOT EXISTS, бачить наявну і падає на індексі по колонці, якої
    # в заглушці немає («column subject_key does not exist»). Помилка при
    # цьому вказує не на винуватця, а на потерпілого.
    try:
        bot_db.execute(
            "DROP TABLE IF EXISTS commitment_objects, commitments, "
            "article_entities, entity_merges, entity_merge_rules, "
            "entities CASCADE")
    except Exception as e:
        print(f"прибирання заглушок: {type(e).__name__}: {e}")

    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} перевірок пройдено")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
