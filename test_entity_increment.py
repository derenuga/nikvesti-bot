"""
Тест самозалікування авто-інкремента сутностей на ЖИВОМУ Postgres
(handlers/entity_layer.py).

Не мок: піднімає реальну схему нори, кладе статті й перевіряє саме те, на чому
модуль зламався в проді 11.07–01.08.2026.

**Що сталось у проді.** Інкремент відбирав статті курсором (`id > cursor`) і
рухав курсор за ВСЮ пачку — включно з тими, чий витяг упав. Повернутись до
впалої статті він не міг ніколи. Сторож масового збою стоїть на 50%, фактична
частка сбоїв була 48% — тож він не взвівся жодного разу, і за три тижні тихо
втрачено 403 статті (майже половина виходу редакції). Виявили лише за
розбіжністю `count(articles)` і `count(distinct article_entities.article_id)`
по днях.

**Що перевіряємо тут (кожен пункт — та сама пастка):**

- стаття, чий витяг УПАВ, лишається в черзі й береться наступного прогону
  (головна регресія: саме цього курсорна схема не робила);
- спроби рахуються, і після INCR_MAX_ATTEMPTS стаття випадає з черги —
  щоб одна вічно бита нода не крутилась у циклі назавжди;
- успішний витяг БЕЗ сутностей (стаття без імен) позначається done і більше
  не береться — інакше виглядав би як необроблений і крутився б щогодини;
- стаття, що вже має зв'язки, у чергу не потрапляє (ідемпотентність прогону);
- підлога поважається: нижче неї — територія бэкфілу, інкремент туди не лізе;
- свіже береться ПЕРШИМ (ORDER BY id DESC), щоб добір старих дірок не
  відсував поточний вихід редакції;
- підлога обчислюється як початок уже покритого діапазону і закріплюється в
  sync_state (повторний виклик не перераховує).

Запуск (потрібен Postgres; за замовчуванням локальний тестовий):
    BOT_DATABASE_URL=postgresql://... python3 test_entity_increment.py
"""

import asyncio
import os
import sys

os.environ.setdefault(
    "BOT_DATABASE_URL", "postgresql://nora@/nora?host=/tmp&port=55432"
)

from handlers import bot_db, entity_layer as el   # noqa: E402
import entity_pipeline as ep                      # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def setup():
    bot_db.ensure_schema()
    conn = ep.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(ep.DDL)
        cur.execute("DELETE FROM article_entities")
        cur.execute("DELETE FROM entities")
        cur.execute("DELETE FROM articles")
        cur.execute("DELETE FROM sync_state WHERE key LIKE 'entity_incr%'")
        cur.execute("DROP TABLE IF EXISTS entity_attempts")
    conn.close()
    el._ensure_attempts_table()

    # 100..105 — «покритий» діапазон; 90 — нижче підлоги (територія бэкфілу).
    rows = [(i, f"Стаття {i}") for i in (90, 100, 101, 102, 103, 104, 105)]
    for aid, title in rows:
        bot_db.execute(
            "INSERT INTO articles (id, published, title_ua, text_ua) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (aid, 1780000000 + aid, title, f"Текст статті {aid}"))
    # Сутність і зв'язки: 100 і 90 вважаються розібраними (їх закрив бэкфіл).
    bot_db.execute(
        "INSERT INTO entities (id, kind, name_ua) VALUES (1, 'person', 'Тест') "
        "ON CONFLICT (id) DO NOTHING")
    for aid in (90, 100):
        bot_db.execute(
            "INSERT INTO article_entities (article_id, entity_id, salience) "
            "VALUES (%s, 1, 'mentioned') ON CONFLICT DO NOTHING", (aid,))


async def run():
    floor = await asyncio.to_thread(el._incr_floor)
    check("підлога = початок покритого діапазону", floor == 90, f"floor={floor}")

    stored = await asyncio.to_thread(bot_db.get_state, el.INCR_FLOOR_KEY)
    check("підлога закріплена в sync_state", int(stored) == 90, f"stored={stored}")

    async def pending():
        rows = await bot_db.aquery(
            el.PENDING_SQL, (floor, el.INCR_MAX_ATTEMPTS, el.INCR_MAX_PER_RUN))
        return [r["id"] for r in rows]

    ids = await pending()
    check("розібрані статті в чергу не потрапляють",
          100 not in ids and 90 not in ids, f"черга={ids}")
    check("нерозібрані потрапляють усі",
          set(ids) == {101, 102, 103, 104, 105}, f"черга={ids}")
    check("свіже першим (ORDER BY id DESC)",
          ids == sorted(ids, reverse=True), f"черга={ids}")

    # --- головна регресія: збій витягу НЕ втрачає статтю назавжди ---
    await asyncio.to_thread(el._mark_attempt, 103, "APIError: overloaded")
    ids = await pending()
    check("стаття після ЗБОЮ лишається в черзі (регресія курсора)",
          103 in ids, f"черга={ids}")

    row = await bot_db.aquery(
        "SELECT attempts, last_error FROM entity_attempts WHERE article_id = 103")
    check("причина збою збережена, а не проглочена",
          row and row[0]["attempts"] == 1 and "overloaded" in (row[0]["last_error"] or ""),
          f"{row}")

    # --- вичерпання спроб: вічно бита нода не крутиться назавжди ---
    for _ in range(el.INCR_MAX_ATTEMPTS - 1):
        await asyncio.to_thread(el._mark_attempt, 103, "APIError: overloaded")
    ids = await pending()
    check(f"після {el.INCR_MAX_ATTEMPTS} спроб стаття випадає з черги",
          103 not in ids, f"черга={ids}")

    # --- успіх без сутностей: done, більше не береться ---
    await asyncio.to_thread(el._mark_attempt, 104, done=True)
    ids = await pending()
    check("стаття без сутностей позначена done і не крутиться щогодини",
          104 not in ids, f"черга={ids}")

    # --- зв'язки з'явились → з черги зникає (ідемпотентність) ---
    await asyncio.to_thread(
        bot_db.execute,
        "INSERT INTO article_entities (article_id, entity_id, salience) "
        "VALUES (105, 1, 'main') ON CONFLICT DO NOTHING")
    ids = await pending()
    check("щойно розібрана стаття зникає з черги",
          105 not in ids, f"черга={ids}")

    n = await el._pending_count(floor)
    check("лічильник черги збігається з вибіркою", n == len(ids), f"{n} vs {len(ids)}")

    check("у черзі лишились саме недороблені", set(ids) == {101, 102}, f"черга={ids}")

    # --- амністія: причина була в нас (малий кап виводу), не в статті ---
    await asyncio.to_thread(
        bot_db.execute, "DELETE FROM sync_state WHERE key = %s", (el.ATTEMPTS_AMNESTY_KEY,))
    await asyncio.to_thread(el._ensure_attempts_table)
    ids = await pending()
    check("амністія повертає в чергу тих, хто вичерпав спроби через наш баг",
          103 in ids, f"черга={ids}")
    check("амністія НЕ чіпає done (стаття без сутностей лишається закритою)",
          104 not in ids, f"черга={ids}")

    await asyncio.to_thread(el._ensure_attempts_table)
    again = await pending()
    check("амністія разова — повторний виклик нічого не скидає",
          sorted(again) == sorted(ids), f"{again} vs {ids}")


def main():
    setup()
    asyncio.run(run())
    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} перевірок пройдено")
    if bad:
        print("ПРОВАЛЕНО: " + ", ".join(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
