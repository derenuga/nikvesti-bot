"""
Структура органів (entity_links) переживає злиття й відкат.

Навіщо тест саме тут. Зв'язок між картками — четверте посилання на
`entities.id` у норі, після зв'язків зі статтями, правил зіставлення й банку
тем. Три попередні вже наступали на ті самі граблі: картка при злитті
ВИДАЛЯЄТЬСЯ, а посилання на неї лишається вказувати в нікуди — мовчки, без
жодної помилки на екрані (банк тем прожив так від свого народження до
16.08.2026, див. test_entity_merge_promises.py). Тому нове посилання
народжується одразу з тестом, а не після інциденту.

Що стереже:
- зв'язок, що вказував на програшну картку, після злиття показує на переможця;
- зв'язок між ДВОМА картками, які злили одну в одну, не лишається сам на себе
  (це був би вузол «прокуратура — підрозділ самої себе»);
- дубль, який після перевішування вперся б у UNIQUE, не валить злиття;
- /entity_unmerge повертає структуру точно в те, чим вона була;
- напрямок не можна поставити двічі назустріч (типова помилка — переплутати
  старшу й молодшу картку);
- картка, вписана в структуру, захищена від прибирання як сміття.

Запуск: потрібен Postgres. Локально:
    initdb -D /tmp/pg -U postgres -A trust && pg_ctl -D /tmp/pg -o '-p 55432' start
    BOT_DATABASE_URL=postgresql://postgres@localhost:55432/postgres \\
        python test_entity_links.py
"""
import os
import sys

if not os.environ.get("BOT_DATABASE_URL") and not os.environ.get("DATABASE_URL"):
    print("❌ Потрібен BOT_DATABASE_URL (Postgres)")
    sys.exit(1)

os.environ.setdefault("BOT_TOKEN", "1:test")

from handlers import bot_db              # noqa: E402
from handlers import entity_links as el  # noqa: E402
from handlers import entity_merge as em  # noqa: E402
import entity_pipeline as ep             # noqa: E402


def _one(sql, params=None):
    rows = bot_db.query(sql, params)
    return rows[0] if rows else {}


SCHEMA = """
DROP TABLE IF EXISTS entity_links, article_entities, entity_merges,
                     entity_merge_rules, entities CASCADE;
CREATE TABLE entities (
    id BIGSERIAL PRIMARY KEY, kind TEXT, subtype TEXT, name_ua TEXT, name_ru TEXT,
    aliases TEXT[] DEFAULT '{}', role_last TEXT,
    first_seen BIGINT, last_seen BIGINT, mentions INT DEFAULT 0);
CREATE TABLE article_entities (
    article_id BIGINT, entity_id BIGINT, role_at_time TEXT, salience INT,
    PRIMARY KEY (article_id, entity_id));
"""


def setup():
    bot_db.query("SELECT 1")
    bot_db.execute(SCHEMA)
    bot_db.execute(em.MERGES_DDL)
    el.ensure_schema(force=True)
    bot_db.execute("DELETE FROM articles WHERE id IN (1, 2, 3)")
    bot_db.execute("INSERT INTO articles (id, published) VALUES (1, 1750000000), "
                   "(2, 1750100000), (3, 1750200000)")
    # Жива фактура: обласна прокуратура, її міська окружна і друге написання
    # тієї ж міської — рівно те, що розбиралось 16.08.
    obl = _one("INSERT INTO entities (kind, name_ua, mentions) VALUES "
               "('org', 'Миколаївська обласна прокуратура', 486) RETURNING id")["id"]
    misto = _one("INSERT INTO entities (kind, name_ua, mentions) VALUES "
                 "('org', 'Окружна прокуратура міста Миколаєва', 23) "
                 "RETURNING id")["id"]
    dubl = _one("INSERT INTO entities (kind, name_ua, mentions) VALUES "
                "('org', 'Окружна прокуратура Миколаєва', 16) RETURNING id")["id"]
    bot_db.execute("INSERT INTO article_entities (article_id, entity_id) "
                   "VALUES (1, %s), (2, %s), (3, %s)", (obl, misto, dubl))
    return obl, misto, dubl


def merge(win, lose):
    """Той самий шлях, що /entity_merge: журнал → перевішування → видалення."""
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

    obl, misto, dubl = setup()

    print("\nЗапис і читання:")
    el.add_link(obl, dubl, "підрозділ", "розбір 16.08", "тест")
    rows = el.links_of(dubl)
    check("зв'язок читається з боку молодшої картки", len(rows) == 1)
    check("речення пишеться правильним боком",
          "входить до" in el.sentence(rows[0], dubl), el.sentence(rows[0], dubl))
    try:
        el.add_link(dubl, obl, "підрозділ")
        check("зустрічний напрямок відхилено", False)
    except ValueError as e:
        check("зустрічний напрямок відхилено з поясненням", "зворотний" in str(e))
    try:
        el.add_link(obl, obl)
        check("картку не можна пов'язати саму з собою", False)
    except ValueError:
        check("картку не можна пов'язати саму з собою", True)

    print("\nЗахист від прибирання як сміття:")
    from handlers import entity_junk as ej
    protected = bot_db.query(
        f"SELECT e.id FROM entities e WHERE e.id = %s AND {ej.PROTECTED_SQL}",
        (dubl,))
    check("картка в структурі вважається захищеною", len(protected) == 1)

    print("\nЗлиття перевішує структуру на переможця:")
    mid = merge(misto, dubl)
    rows = el.links_of(misto)
    check("зв'язок переїхав на переможця", len(rows) == 1)
    check("старша картка лишилась старшою", rows[0]["parent_id"] == obl)
    check("посилання на зниклу картку не лишилось",
          not bot_db.query("SELECT 1 FROM entity_links WHERE parent_id = %s "
                           "OR child_id = %s", (dubl, dubl)))

    print("\nВідкат повертає структуру:")
    em.restore_merge(mid)
    rows = el.links_of(dubl)
    check("зв'язок повернувся на воскреслу картку", len(rows) == 1)
    check("з переможця його знято",
          not bot_db.query("SELECT 1 FROM entity_links WHERE child_id = %s",
                           (misto,)))

    print("\nЗлиття ДВОХ пов'язаних карток не лишає вузол сам на себе:")
    el.add_link(misto, dubl, "підрозділ")   # тимчасово: міська «має» дубль
    mid2 = merge(misto, dubl)
    self_loop = bot_db.query(
        "SELECT 1 FROM entity_links WHERE parent_id = child_id")
    check("вузла «сам на себе» не з'явилось", not self_loop)
    check("зв'язок з обласною при цьому вцілів",
          len(el.links_of(misto)) == 1)
    em.restore_merge(mid2)
    check("відкат повернув обидва зв'язки", len(el.links_of(dubl)) == 2,
          str([el.sentence(r, dubl) for r in el.links_of(dubl)]))

    print("\nДубль зв'язку не валить злиття:")
    bot_db.execute("DELETE FROM entity_links")
    el.add_link(obl, misto, "підрозділ")
    el.add_link(obl, dubl, "підрозділ")     # той самий факт, але іншою карткою
    mid3 = merge(misto, dubl)
    rows = el.links_of(misto)
    check("лишився рівно один зв'язок, без конфлікту ключа", len(rows) == 1)
    em.restore_merge(mid3)
    check("відкат повернув другий зв'язок", len(el.links_of(dubl)) == 1)

    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} перевірок пройдено")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
