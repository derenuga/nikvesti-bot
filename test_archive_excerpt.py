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

**Дані у фікстурах ВИГАДАНІ, і імена в них навмисно неіснуючі.** Перевіряємо
механіку SQL (де рахується ts_headline, чи знаходить пара, чи чистий фрагмент),
а для неї байдуже, чиє прізвище в тексті — потрібна лише жива українська
морфологія. Реальних людей у фікстурах не називаємо: вигадану цитату, вкладену
в уста справжньої людини, наступний читач тесту може прийняти за факт з нори
(так і сталось 12.08 — у першій версії цього файлу стояло справжнє прізвище з
питання Аліни й вигаданою посадою «депутат», якої ця людина не обіймає).
Справжня перевірка «чи є така цитата в архіві» — це той самий запит по живій
норі, а не тест.
"""

import sys

from handlers import archive_search, bot_db

FAILS = []
TEST_IDS = (990317249, 990316578)


def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# Прізвище й підприємство вигадані (див. шапку файлу). Форма тексту — справжня:
# заява людини в СЕРЕДИНІ статті, а не в заголовку й не в ліді. Саме ця форма і
# провалювалась, бо пошук віддавав лише заголовок.
PERSON = "Пилип Кущенко"
ORG = "Тестозеленбуд"
ARTICLE_WITH_QUOTE = (
    f"Комунальне підприємство «{ORG}» подало заявку на знесення 12 тополь "
    "на приміському бульварі, назвавши їх аварійними. Проти виступила державна екологічна "
    f"інспекція. Голова громадської ради {PERSON} розкритикував роботу "
    "комунального підприємства: «Роками не доглядають за деревами, а потім приходять "
    "і кажуть, що дерево аварійне і його треба зрізати», — заявив він на засіданні "
    "профільної комісії. Він додав, що підприємство не веде інвентаризації насаджень."
)
ARTICLE_PLAIN = (
    f"У комунальному підприємстві «{ORG}» повідомили, що фінансування на "
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
         "Хочуть знести тополі на приміському бульварі: екоінспекція вимагає експертизу",
         "test-topoli-bulvar", ARTICLE_WITH_QUOTE, f"екологія, {ORG}"))
    bot_db.execute(
        """INSERT INTO articles (id, published, status, title_ua, slug, category, text_ua, tags_text)
           VALUES (%s, %s, 1, %s, %s, 'public', %s, %s)
           ON CONFLICT (id) DO UPDATE SET text_ua = EXCLUDED.text_ua""",
        (TEST_IDS[1], 1774000000,
         f"«{ORG}» заявив, що грошей на висадку дерев цієї весни немає",
         "test-zelenbud-finansuvannia", ARTICLE_PLAIN, f"бюджет, {ORG}"))


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
        items = archive_search.search_items(f"{PERSON.split()[1]} {ORG}", limit=10,
                                            with_context=True)
        mine = [it for it in items if it["id"] == TEST_IDS[0]]
        check("стаття знайшлась по парі «прізвище + об'єкт»", bool(mine),
              f"знайдено {len(items)} шт., потрібної немає")
        if mine:
            exc = mine[0].get("excerpt") or ""
            check("excerpt є", bool(exc))
            check("у excerpt видно, ХТО критикує", PERSON.split()[1] in exc, exc[:120])
            check("у excerpt видно, ЩО саме сказано",
                  "розкритикував" in exc or "не доглядають" in exc, exc[:120])
            check("прізвища в заголовку немає (перевіряємо саме той випадок)",
                  PERSON.split()[1] not in mine[0]["title"])
            check("excerpt без маркерів виділення",
                  "<b>" not in exc and "</b>" not in exc and "<" not in exc, exc[:80])

        print("\n2. Пошук лише по об'єкту (як зробив Лис) персони не гарантує")
        obj_items = archive_search.search_items(ORG, limit=10, with_context=True)
        check("статті про об'єкт знайшлись", len(obj_items) >= 2, f"знайдено {len(obj_items)}")
        # Заголовки — це РІВНО те, що Лис бачив до цієї зміни. Прізвища там немає
        # в жодному, тому висновок «не згадується» він зробив на порожньому місці.
        # (Витяг по запиту про об'єкт іноді ЗАЧЕПИТЬ і потрібне речення — але це
        # везіння з вибору фрагмента, а не метод: питати треба парою.)
        titles = " ".join(it["title"] for it in obj_items if it["id"] in TEST_IDS)
        check("у заголовках видачі по об'єкту прізвища немає — саме тому потрібен "
              "крок «спершу пара»", PERSON.split()[1] not in titles)

        print("\n3. Режим «історія питання» (інша гілка SQL)")
        spread = archive_search.search_items(ORG, limit=10, spread_years=True, with_context=True)
        got = [it for it in spread if it["id"] in TEST_IDS]
        check("spread_years віддає результати", bool(got))
        check("spread_years теж віддає excerpt",
              all(it.get("excerpt") for it in got),
              str([it.get("excerpt") for it in got])[:120])

        print("\n4. Без with_context (шлях /dossier) — нічого не змінилось")
        plain = archive_search.search_items(ORG, limit=5)
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
        archive_search.search_items(ORG, limit=5, with_context=True)
    finally:
        bot_db.query = real_query
    return captured["sql"]


if __name__ == "__main__":
    sys.exit(main())
