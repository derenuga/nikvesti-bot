"""
Тест разового бекфілу історичних статей у нору (/articles_backfill).

Живих БД не треба: підміняємо `db` (MySQL сайту) і `bot_db` (Postgres нори)
на фейки в пам'яті. Стереже те, що зламало б дзеркало і на очі не видно:

- вибірка йде СУВОРО по type='article' (status=1, published у минулому,
  id > курсор) — новини вдруге не тягнемо;
- курсор ЛИШЕ свій (articles_backfill_last_id): backfill_last_id,
  mirror_cursor і backfill_done_at не пишуться НІКОЛИ — їх зсув зламав би
  головний бекфіл та інкремент;
- resumable: порція з limit зберігає курсор, наступний запуск продовжує
  з місця і не переливає вже залите;
- /archive_stop (спільний прапорець) зупиняє після поточної пачки,
  курсор збережено;
- одне з'єднання з БД сайту на весь прогін (ліміт 1000 конектів/год),
  закривається в кінці;
- kind='article' доїжджає до upsert-кортежу (15-та колонка);
- порожня заглушка (без тексту) не осідає в норі, але курсор повз неї їде;
- маркерна стаття 311397 (з якої дірку знайшли) — у залитому.

Запуск:
    python test_articles_backfill.py
"""

import asyncio
import sys

from handlers import archive_mirror

FAILS = []


def check(name, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {name}{'' if cond else ' — ' + str(extra)}")
    if not cond:
        FAILS.append(name)


# ---------- Фейкові БД ----------

# Реальний HTML сайту (зразок структури статті з фото-контейнером старого
# движка) — щоб пачка проходила справжню чистку html_to_text.
_HTML = (
    '<p>Миколаїв має шанс змінити підхід до захисту історичної забудови.</p>'
    '<div id="imgbox-1" class="photo right"><span class="title">Підпис фото</span></div>'
    '<p>Експерти пропонують створити реєстр памʼяток місцевого значення.</p>'
)

NOW_SAFE = 1700000000  # published свідомо в минулому відносно реального now


def _node(nid, ntype="article", published=NOW_SAFE, status=1, content=_HTML):
    return {
        "id": nid, "published": published, "updated": published, "status": status,
        "own_material": 1, "owner_id": 7, "type": ntype,
        "title_ua": f"Стаття {nid}", "title": None,
        "slug_ua": f"slug-{nid}", "slug": None,
        "content_ua": content, "content": None,
        "category": "society", "region": 15,
    }


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSiteDB:
    """Підміна handlers.db: aquery + open_bulk_connection над списком nodes."""

    def __init__(self, nodes):
        self.nodes = sorted(nodes, key=lambda r: r["id"])
        self.node_queries = []   # (sql, params, conn)
        self.conns = []

    def open_bulk_connection(self):
        conn = FakeConn()
        self.conns.append(conn)
        return conn

    async def aquery(self, sql, params=None, conn=None):
        if "FROM tag" in sql:
            return []  # довідник тегів порожній — синк іде без тегів
        if "FROM node_tag" in sql:
            return []
        if "FROM nodes" in sql:
            self.node_queries.append((sql, params, conn))
            now_ts, last_id, batch = params
            out = [r for r in self.nodes
                   if r["type"] == "article" and r["status"] == 1
                   and 0 < r["published"] <= now_ts and r["id"] > last_id]
            return out[:batch]
        raise AssertionError(f"неочікуваний SQL: {sql}")


class FakeBotDB:
    """Підміна handlers.bot_db: sync_state у dict + збір upsert-кортежів."""

    def __init__(self):
        self.state = {}
        self.upserted = []       # усі кортежі upsert_articles за всі виклики
        self.tag_pairs = []

    def ensure_schema(self):
        pass

    def get_state(self, key, default=None):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = str(value)

    def upsert_tags(self, rows):
        pass

    def upsert_articles(self, tuples):
        self.upserted.extend(tuples)

    def replace_article_tags(self, ids, pairs):
        self.tag_pairs.extend(pairs)


def _patch(site, nora):
    """Ставить фейки в archive_mirror (модульні посилання db/bot_db лишаються
    справжніми модулями — підміняємо атрибути точково через простий проксі)."""

    class Proxy:
        def __init__(self, target):
            self._t = target

        def __getattr__(self, name):
            return getattr(self._t, name)

    db_proxy = Proxy(site)
    bot_proxy = Proxy(nora)
    # html_to_text бере bot_db.TEXT_CAP — віддаємо реальний кап
    bot_proxy.TEXT_CAP = archive_mirror.bot_db.TEXT_CAP
    archive_mirror.db = db_proxy
    archive_mirror.bot_db = bot_proxy


_REAL_DB = archive_mirror.db
_REAL_BOT_DB = archive_mirror.bot_db


def _unpatch():
    archive_mirror.db = _REAL_DB
    archive_mirror.bot_db = _REAL_BOT_DB


NODES = [
    _node(10),
    _node(15, ntype="news"),                      # новина — НЕ має потрапити
    _node(20),
    _node(300000, content=""),                     # порожня заглушка — не в корпус
    _node(311397),                                 # маркерна стаття з брифу
    _node(350000, status=0),                       # чернетка — не беремо
    _node(380000, published=9999999999),           # відкладена в майбутнє — не беремо
    _node(400001),
]


def test_full_run():
    """Повний прогін до кінця: фільтр, курсор, kind, заглушки, з'єднання."""
    site, nora = FakeSiteDB(NODES), FakeBotDB()
    _patch(site, nora)
    archive_mirror.BACKFILL_BATCH, real_batch = 2, archive_mirror.BACKFILL_BATCH
    archive_mirror.BACKFILL_PAUSE, real_pause = 0, archive_mirror.BACKFILL_PAUSE
    try:
        done, reached_end, first_id, last_id = asyncio.run(
            archive_mirror.run_articles_backfill()
        )
    finally:
        archive_mirror.BACKFILL_BATCH = real_batch
        archive_mirror.BACKFILL_PAUSE = real_pause
        _unpatch()

    check("дійшов до кінця (reached_end)", reached_end)
    check("оброблено 5 статей (без новин/чернеток/майбутніх)", done == 5, done)
    check("діапазон id у підсумку", (first_id, last_id) == (10, 400001),
          (first_id, last_id))

    ids = [t[0] for t in nora.upserted]
    check("залито 4 (заглушка 300000 не в корпусі)", ids == [10, 20, 311397, 400001], ids)
    check("маркерна стаття 311397 у норі", 311397 in ids)
    check("новина 15 не потрапила", 15 not in ids)
    check("kind='article' у кожному кортежі",
          all(t[14] == "article" for t in nora.upserted),
          [t[14] for t in nora.upserted])
    # Чистка HTML: фото-контейнер з підписом вирізано, текст обох абзаців є
    text_ua = nora.upserted[0][9]
    check("html_to_text: підпис фото вирізано", "Підпис фото" not in text_ua, text_ua)
    check("html_to_text: текст статті на місці",
          "історичної забудови" in text_ua and "реєстр памʼяток" in text_ua, text_ua)

    check("курсор статей на максимумі", nora.state.get("articles_backfill_last_id") == "400001",
          nora.state)
    for key in ("backfill_last_id", "mirror_cursor", "backfill_done_at"):
        check(f"основний курсор {key} НЕ зачеплено", key not in nora.state, nora.state)

    check("фільтр type='article' у кожному запиті nodes",
          all("type = 'article'" in q[0] for q in site.node_queries))
    check("одне bulk-з'єднання на весь прогін", len(site.conns) == 1, len(site.conns))
    check("усі запити nodes через спільне з'єднання",
          all(q[2] is site.conns[0] for q in site.node_queries))
    check("з'єднання закрито в кінці", site.conns[0].closed)


def test_resumable_limit():
    """Порція з limit → курсор зберігся; другий запуск продовжує, не переливає."""
    site, nora = FakeSiteDB(NODES), FakeBotDB()
    _patch(site, nora)
    archive_mirror.BACKFILL_PAUSE, real_pause = 0, archive_mirror.BACKFILL_PAUSE
    try:
        done1, end1, _, last1 = asyncio.run(archive_mirror.run_articles_backfill(limit=2))
        first_batch_ids = [t[0] for t in nora.upserted]
        done2, end2, first2, last2 = asyncio.run(archive_mirror.run_articles_backfill())
    finally:
        archive_mirror.BACKFILL_PAUSE = real_pause
        _unpatch()

    check("порція: 2 статті, не кінець", (done1, end1, last1) == (2, False, 20),
          (done1, end1, last1))
    check("порція залила перші дві", first_batch_ids == [10, 20], first_batch_ids)
    # Перша оброблена після курсора 20 — заглушка 300000 (вона проходить
    # повз курсор, хоч і не осідає в корпусі)
    check("другий запуск продовжив з курсора", first2 == 300000, first2)
    check("другий запуск дійшов до кінця", (done2, end2, last2) == (3, True, 400001),
          (done2, end2, last2))
    ids = [t[0] for t in nora.upserted]
    check("нічого не перелито двічі", ids == [10, 20, 311397, 400001], ids)


def test_stop_flag():
    """Спільний з /archive_stop прапорець зупиняє після поточної пачки."""
    site, nora = FakeSiteDB(NODES), FakeBotDB()
    _patch(site, nora)
    archive_mirror.BACKFILL_BATCH, real_batch = 2, archive_mirror.BACKFILL_BATCH
    archive_mirror.BACKFILL_PAUSE, real_pause = 0, archive_mirror.BACKFILL_PAUSE

    async def stopper(done, last_id):
        archive_mirror._backfill_stop["flag"] = True  # як зробив би /archive_stop

    try:
        done, reached_end, _, last_id = asyncio.run(
            archive_mirror.run_articles_backfill(progress_cb=stopper)
        )
    finally:
        archive_mirror.BACKFILL_BATCH = real_batch
        archive_mirror.BACKFILL_PAUSE = real_pause
        archive_mirror._backfill_stop["flag"] = False
        _unpatch()

    check("стоп після першої пачки", done == 2, done)
    check("стоп ≠ завершення", not reached_end)
    check("курсор збережено для продовження",
          nora.state.get("articles_backfill_last_id") == "20", nora.state)


def test_no_parallel_run():
    """Другий прогін паралельно (і з головним бекфілом теж) — відмова."""
    site, nora = FakeSiteDB(NODES), FakeBotDB()
    _patch(site, nora)
    archive_mirror._backfill_running["flag"] = True
    try:
        try:
            asyncio.run(archive_mirror.run_articles_backfill())
            check("паралельний запуск заборонено", False, "RuntimeError не піднявся")
        except RuntimeError:
            check("паралельний запуск заборонено", True)
    finally:
        archive_mirror._backfill_running["flag"] = False
        _unpatch()


if __name__ == "__main__":
    test_full_run()
    test_resumable_limit()
    test_stop_flag()
    test_no_parallel_run()
    print()
    if FAILS:
        print(f"❌ Провалено: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("✅ Всі перевірки пройдено")
