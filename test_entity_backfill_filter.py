"""
Тест відбору статей для платного бэкфіла сутностей на ЖИВОМУ Postgres
(entity_backfill_api.fetch_range).

Це вибірка, за якою ми ПЛАТИМО, тож помилка тут коштує грошей напряму.
Дві речі, які вона має робити і які легко зламати:

**Фільтр за типом.** `/entity_backfill` довго вмів лише діапазон дат. Але статей
(`kind='article'`) на порядок менше за новини, і саме їх треба догнати першими —
платний батч їх не бачив узагалі (нора почала дзеркалити статті лише з 30.07), а
це найвагоміші тексти й ядро імпакт-серій. Без фільтра догнати статті 2009–2023
означало б витягти заодно всі новини тих років: ~$700 замість пари доларів.

**Пропуск уже розібраного.** Раніше повторний прогін того самого діапазону
платив за все вдруге. Гірше за гроші: `write_results` лише апсертить зв'язки і
НЕ знімає старі, тож стаття тягла б за собою об'єднання двох витягів — якщо
другий прогін дав менше сутностей, зайві лишились би назавжди. Свідомий перечит
зі зняттям зв'язків — це /entity_resync, не бэкфіл.

Перевіряємо:
- kind='article' / 'news' / None відбирають рівно те, що обіцяно;
- межі діапазону дат (нижня включно, верхня виключно);
- стаття зі зв'язками в черзі не з'являється і потрапляє в лічильник skipped;
- стаття, позначена done (витяг пройшов, сутностей у тексті немає), теж
  пропускається — інакше кожен прогін платив би за неї знову;
- only_missing=False повертає все (аварійний режим, skipped=0);
- одномовність: якщо є обидві версії, у запит іде лише ua (інакше вхід удвічі).

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_entity_backfill_filter.py
"""

import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db            # noqa: E402
import entity_pipeline as ep           # noqa: E402
import entity_backfill_api as api      # noqa: E402

RESULTS = []
D2020 = 1577836800   # 2020-01-01 UTC
DAY = 86400


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def setup():
    bot_db.ensure_schema()
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute(ep.ATTEMPTS_DDL)
        cur.execute("DELETE FROM article_entities")
        cur.execute("DELETE FROM entities")
        cur.execute("DELETE FROM entity_attempts")
        cur.execute("DELETE FROM articles")
    conn.close()

    # 201,202 — новини; 301,302,303 — статті; 999 — поза діапазоном (2021).
    rows = [
        (201, D2020 + DAY, "news", "Новина 201"),
        (202, D2020 + 2 * DAY, "news", "Новина 202"),
        (301, D2020 + 3 * DAY, "article", "Стаття 301"),
        (302, D2020 + 4 * DAY, "article", "Стаття 302"),
        (303, D2020 + 5 * DAY, "article", "Стаття 303"),
        (999, D2020 + 400 * DAY, "article", "Стаття 2021 року"),
    ]
    for aid, pub, kind, title in rows:
        bot_db.execute(
            "INSERT INTO articles (id, published, kind, title_ua, text_ua) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (aid, pub, kind, title, f"Текст {aid}"))
    # 302 вже розібрана; 303 пройшла витяг і законно порожня (done).
    bot_db.execute("INSERT INTO entities (id, kind, name_ua) VALUES (1,'person','Тест') "
                   "ON CONFLICT (id) DO NOTHING")
    bot_db.execute("INSERT INTO article_entities (article_id, entity_id, salience) "
                   "VALUES (302, 1, 'main') ON CONFLICT DO NOTHING")
    bot_db.execute("INSERT INTO entity_attempts (article_id, attempts, done) "
                   "VALUES (303, 1, true) ON CONFLICT (article_id) DO NOTHING")
    # Двомовна — перевіряємо, що в запит іде одна мова.
    bot_db.execute("UPDATE articles SET text_ru = %s WHERE id = 301", ("Текст 301 ru",))


def run():
    def ids(arts):
        return sorted(a["id"] for a in arts)

    arts, skipped = api.fetch_range("2020-01-01", "2020-02-01", "article")
    check("kind=article бере лише статті", ids(arts) == [301],
          f"{ids(arts)}, пропущено {skipped}")
    check("розібрана (302) і порожня-done (303) у лічильнику skipped", skipped == 2,
          f"skipped={skipped}")

    arts, skipped = api.fetch_range("2020-01-01", "2020-02-01", "news")
    check("kind=news бере лише новини", ids(arts) == [201, 202], f"{ids(arts)}")

    arts, skipped = api.fetch_range("2020-01-01", "2020-02-01", None)
    check("без фільтра — і новини, і статті", ids(arts) == [201, 202, 301],
          f"{ids(arts)}")

    check("стаття поза діапазоном (2021) не потрапила", 999 not in ids(arts))

    # Дати: 201=02.01, 202=03.01, 301=04.01, 302=05.01, 303=06.01.
    # Вікно 03.01…05.01 має взяти 202 (рівно нижня межа, включно) і 301,
    # а 302 (рівно верхня межа) — відсікти.
    arts, _ = api.fetch_range("2020-01-03", "2020-01-05", None)
    check("нижня межа включно, верхня виключно", ids(arts) == [202, 301],
          f"{ids(arts)} — 202 на нижній межі взято, 302 на верхній відсічено")

    arts, skipped = api.fetch_range("2020-01-01", "2020-02-01", "article",
                                    only_missing=False)
    check("only_missing=False повертає все", ids(arts) == [301, 302, 303],
          f"{ids(arts)}")
    check("у режимі «все» skipped=0", skipped == 0, f"skipped={skipped}")

    arts, _ = api.fetch_range("2020-01-01", "2020-02-01", "article")
    a301 = next(a for a in arts if a["id"] == 301)
    check("двомовна стаття йде однією мовою (вхід не подвоюється)",
          a301["text_ua"] and a301["text_ru"] is None,
          f"ua={bool(a301['text_ua'])} ru={a301['text_ru']!r}")


def main():
    setup()
    run()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} перевірок пройдено")
    if bad:
        print("ПРОВАЛЕНО: " + ", ".join(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
