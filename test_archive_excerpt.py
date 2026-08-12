"""
Тест витягу тексту навколо збігу в повнотекстовому пошуку по норі
(handlers/archive_search.py) — на ЖИВОМУ Postgres, не на моках.

Запуск:
    BOT_DATABASE_URL=postgresql://... python3 test_archive_excerpt.py

**Що сталось у проді (12.08.2026).** Аліна попросила «збери нагадування про
критику від Дмитра Рябченка на роботу КП „Миколаївські парки"». Лис знайшов
десяток новин про саме КП, прочитав їхні ліди й відповів, що прямого
підтвердження немає: «його ім'я в жодному з лідів цих новин не фігурує —
можливо, він згадується десь у тілі тексту, але переконатись у контексті без
повного тексту я не можу». Щодо себе він мав рацію: `search_archive_fulltext`
ШУКАВ по повному тексту, а віддавав лише заголовок, дату й URL. Тобто будь-яке
питання «хто кого критикував / хто що заявив» упиралось у стіну, хоча цитата
лежала в норі. Плюс він шукав по об'єкту («Миколаївські парки»), а питання
було про ПЕРЕТИН персони й об'єкта.

**Що перевіряємо тут (кожен пункт — та сама пастка):**

- пошук по ПАРІ «прізвище + об'єкт» знаходить статтю, у якій прізвище є лише
  в тілі тексту, а в заголовку його немає (головна регресія);
- excerpt приносить саме те речення, де людина критикує, — тобто відповідь на
  питання видно без читання повного тексту;
- пошук лише по об'єкту (як робив Лис) прізвища не знаходить — це не баг
  пошуку, а неправильний запит, і саме тому в промпті стоїть крок «спершу пара»;
- excerpt чистий: без маркерів виділення (Лис цитує його майже дослівно, і
  будь-які <b>/«» поїхали б у текст відповіді чи в HTML-розмітку);
- режим «історія питання» (spread_years) теж віддає excerpt — це ІНША гілка
  SQL, і зламати її окремо легко;
- без with_context ключа excerpt немає взагалі (шлях /dossier не змінився і
  не платить за ts_headline);
- витяг рахується ЛИШЕ для відібраних рядків: план запиту не має містити
  ts_headline під сканом усіх збігів (на «Миколаїв» це десятки тисяч статей).

Дані свої (дві статті), бо перевіряємо механіку SQL: у чужій норі під рукою
немає гарантованої статті з прізвищем лише в тілі тексту.
"""

import os
import sys

from handlers import archive_search, bot_db

FAILS = []
TEST_IDS = (990317249, 990316578)


def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


ARTICLE_WITH_QUOTE = (
    "Комунальне підприємство «Миколаївські парки» подало заявку на знесення 12 тополь "
    "на Флотському бульварі, назвавши їх аварійними. Проти виступила державна екологічна "
    "інспекція. Депутат Миколаївської міської ради Дмитро Рябченко розкритикував роботу "
    "комунального підприємства: «Парки роками не доглядають за деревами, а потім приходять "
    "і кажуть, що дерево аварійне і його треба зрізати», — заявив депутат на засіданні "
    "профільної комісії. Він додав, що підприємство не веде інвентаризації насаджень."
)
ARTICLE_PLAIN = (
    "У комунальному підприємстві «Миколаївські парки» повідомили, що фінансування на "
    "висадку дерев навесні 2026 року не передбачено. Директор пояснив це секвестром бюджету."
)


def seed():
    bot_db.execute(
        """INSERT INTO articles (id, published, status, title_ua, slug, category, text_ua, tags_text)
           VALUES (%s, %s, 1, %s, %s, 'public', %s, %s)
           ON CONFLICT (id) DO UPDATE SET text_ua = EXCLUDED.text_ua,
               title_ua = EXCLUDED.title_ua, published = EXCLUDED.published""",
        (TEST_IDS[0], 1776200000,
         # Прізвища в заголовку НЕМАЄ свідомо — саме цей випадок і провалився.
         "У Миколаєві хочуть знести тополі на Флотському бульварі: екоінспекція вимагає експертизу",
         "test-topoli-flotskyi", ARTICLE_WITH_QUOTE, "екологія, Миколаївські парки"))
    bot_db.execute(
        """INSERT INTO articles (id, published, status, title_ua, slug, category, text_ua, tags_text)
           VALUES (%s, %s, 1, %s, %s, 'public', %s, %s)
           ON CONFLICT (id) DO UPDATE SET text_ua = EXCLUDED.text_ua""",
        (TEST_IDS[1], 1774000000,
         "«Миколаївські парки» заявили, що грошей на висадку дерев цієї весни немає",
         "test-parky-finansuvannia", ARTICLE_PLAIN, "бюджет, Миколаївські парки"))


def cleanup():
    bot_db.execute("DELETE FROM articles WHERE id IN %s", (TEST_IDS,))


def main():
    if not bot_db.is_configured():
        print("❌ Потрібен BOT_DATABASE_URL (Postgres нори)")
        return 1
    bot_db.ensure_schema()
    seed()
    try:
        print("\n1. Питання на ПЕРЕТИН: прізвище лише в тілі тексту")
        items = archive_search.search_items("Рябченко Миколаївські парки", limit=10,
                                            with_context=True)
        mine = [it for it in items if it["id"] == TEST_IDS[0]]
        check("стаття знайшлась по парі «прізвище + об'єкт»", bool(mine),
              f"знайдено {len(items)} шт., потрібної немає")
        if mine:
            exc = mine[0].get("excerpt") or ""
            check("excerpt є", bool(exc))
            check("у excerpt видно, ХТО критикує", "Рябченко" in exc, exc[:120])
            check("у excerpt видно, ЩО саме сказано",
                  "розкритикував" in exc or "не доглядають" in exc, exc[:120])
            check("прізвища в заголовку немає (перевіряємо саме той випадок)",
                  "Рябченко" not in mine[0]["title"])
            check("excerpt без маркерів виділення",
                  "<b>" not in exc and "</b>" not in exc and "<" not in exc, exc[:80])

        print("\n2. Пошук лише по об'єкту (як зробив Лис) персону не знаходить")
        obj_items = archive_search.search_items("Миколаївські парки", limit=10,
                                                with_context=True)
        check("статті про КП знайшлись", len(obj_items) >= 2, f"знайдено {len(obj_items)}")
        joined = " ".join((it.get("excerpt") or "") + it["title"] for it in obj_items
                          if it["id"] in TEST_IDS)
        check("прізвища у видачі по об'єкту немає — тому й потрібен крок «спершу пара»",
              "Рябченко" not in joined)

        print("\n3. Режим «історія питання» (інша гілка SQL)")
        spread = archive_search.search_items("Миколаївські парки", limit=10,
                                             spread_years=True, with_context=True)
        got = [it for it in spread if it["id"] in TEST_IDS]
        check("spread_years віддає результати", bool(got))
        check("spread_years теж віддає excerpt",
              all(it.get("excerpt") for it in got),
              str([it.get("excerpt") for it in got])[:120])

        print("\n4. Без with_context (шлях /dossier) — нічого не змінилось")
        plain = archive_search.search_items("Миколаївські парки", limit=5)
        check("ключа excerpt немає взагалі", all("excerpt" not in it for it in plain))

        print("\n5. Витяг рахується лише для відібраних рядків, не для всіх збігів")
        sql = _capture_sql()
        # ts_headline перечитує тіло статті. Якщо він опиниться всередині CTE
        # matches (усі збіги — на «Миколаїв» це десятки тисяч статей), кожен
        # пошук ставав би повільним і дорогим. Місце виклику й перевіряємо.
        check("ts_headline у запиті один", sql.count("ts_headline") == 1,
              f"знайдено {sql.count('ts_headline')}")
        check("ts_headline стоїть ПІСЛЯ добору рядків (у фінальному select)",
              sql.index("ts_headline") > sql.index("top_ranked AS ("))
    finally:
        cleanup()

    print("\n" + ("❌ ВПАЛО: " + ", ".join(FAILS) if FAILS else "✅ Усі перевірки пройдено"))
    return 1 if FAILS else 0


def _capture_sql():
    """SQL, який search_items реально відправляє в нору (з витягом)."""
    captured = {}
    real_query = bot_db.query

    def spy(sql, params=None):
        captured.setdefault("sql", sql)
        return real_query(sql, params)

    bot_db.query = spy
    try:
        archive_search.search_items("Миколаївські парки", limit=5, with_context=True)
    finally:
        bot_db.query = real_query
    return captured["sql"]


if __name__ == "__main__":
    sys.exit(main())
