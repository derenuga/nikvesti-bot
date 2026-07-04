"""
Власна БД бота — Postgres на Railway (хвиля A, docs/ARCHIVE_INTELLIGENCE.md).

Перший мешканець бази — дзеркало 17-річного архіву новин сайту (таблиця
articles) з повнотекстовим пошуком. Згодом сюди переїде і стан модулів
моніторингу з /data/prozorro_state.json (окремим кроком, без поспіху).

Чому Postgres, а не MySQL/SQLite — обґрунтування в ARCHIVE_INTELLIGENCE.md:
FTS (tsvector+GIN) + нечіткий збіг (pg_trgm) + у майбутньому вектори (pgvector)
живуть в одному движку, одна залежність, один бекап.

Конфіг з env (Railway):
    BOT_DATABASE_URL — connection string Postgres (postgresql://user:pass@host:port/db).
                       Fallback: DATABASE_URL (її Railway інжектить автоматично,
                       якщо в сервісі бота зареференсити змінну Postgres-плагіна).

Модель з'єднань — як у db.py: **з'єднання на запит** (відкрив → виконав →
закрив), thread-safe без пулу. psycopg2 — блокуючий драйвер, тому всі публічні
хелпери синхронні; в async-коді викликати через asyncio.to_thread / aquery.
Бот стартує і без BOT_DATABASE_URL — модуль опційний, як db.py.
"""

import asyncio
import os
import threading
import time

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # локальний dev без psycopg2 — модуль просто "не налаштований"
    psycopg2 = None

BOT_DATABASE_URL = os.environ.get("BOT_DATABASE_URL") or os.environ.get("DATABASE_URL")

CONNECT_TIMEOUT = int(os.environ.get("BOT_DB_CONNECT_TIMEOUT", "10"))

# Скільки символів чистого тексту статті зберігаємо НА КОЖНУ МОВУ. Кап потрібен,
# щоб generated-колонка fts (tsvector, ліміт 1 МБ) ніколи не переповнювалась;
# 60к символів покривають навіть найдовші лонгріди.
TEXT_CAP = 60_000

# ---------- Схема ----------
#
# Мовні версії зберігаються СУВОРО ОКРЕМО: title_ua/text_ua — українська,
# title_ru/text_ru — російська (nodes.title / nodes.content без суфікса — рос.).
# До ~2023 матеріали були лише російською, потім українською, бувають і обидві —
# тому в одне поле їх не звалюємо, інакше версії губляться або змішуються.
#
# articles.fts — generated column: Postgres сам перераховує tsvector при
# кожному upsert, окремого кроку індексації не існує в принципі. Індекс
# будується з УСІХ чотирьох текстових полів, тож пошук знаходить незалежно
# від того, якою мовою (чи обома) вийшов матеріал.
# Конфіг 'simple' (без стемінгу): українського стемера в Postgres немає,
# морфологію закриваємо префіксним пошуком (слово:*) на боці archive_search.

_SCHEMA_STATEMENTS = [
    (
        """
        CREATE TABLE IF NOT EXISTS articles (
            id           BIGINT PRIMARY KEY,
            published    BIGINT,
            updated      BIGINT,
            status       SMALLINT,
            own_material SMALLINT,
            owner_id     BIGINT,
            title_ua     TEXT,
            title_ru     TEXT,
            slug         TEXT,
            text_ua      TEXT,
            text_ru      TEXT,
            synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            fts          tsvector GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(title_ua, '') || ' ' ||
                    coalesce(title_ru, '') || ' ' ||
                    coalesce(text_ua, '') || ' ' ||
                    coalesce(text_ru, ''))
            ) STORED
        )
        """,
        True,  # обов'язковий statement — без нього модуль не працює
    ),
    ("CREATE INDEX IF NOT EXISTS idx_articles_fts ON articles USING gin (fts)", True),
    ("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published DESC)", True),
    (
        "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)",
        True,
    ),
    # pg_trgm — для нечіткого збігу імен (Сєнкевич/Сенкевич) у майбутньому.
    # Опційно: якщо у Postgres-інстансу немає прав на CREATE EXTENSION,
    # модуль працює без trgm (тільки FTS).
    ("CREATE EXTENSION IF NOT EXISTS pg_trgm", False),
    (
        "CREATE INDEX IF NOT EXISTS idx_articles_titles_trgm ON articles "
        "USING gin ((coalesce(title_ua,'') || ' ' || coalesce(title_ru,'')) gin_trgm_ops)",
        False,
    ),
]

_schema_lock = threading.Lock()
_schema_ready = False


def is_configured():
    """Чи задано підключення. Дозволяє боту стартувати без Postgres."""
    return bool(BOT_DATABASE_URL and psycopg2)


def _connect():
    if not is_configured():
        raise RuntimeError(
            "БД бота не налаштована: додайте Postgres на Railway і задайте BOT_DATABASE_URL"
        )
    return psycopg2.connect(BOT_DATABASE_URL, connect_timeout=CONNECT_TIMEOUT)


def ensure_schema():
    """Створює таблиці/індекси, якщо їх ще немає. Ідемпотентно, викликається
    лениво перед першою операцією (кожен процес — один раз)."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = _connect()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for sql, required in _SCHEMA_STATEMENTS:
                    try:
                        cur.execute(sql)
                    except Exception as e:
                        if required:
                            raise
                        print(f"bot_db: опційний крок схеми пропущено — {e}")
        finally:
            conn.close()
        _schema_ready = True


def query(sql, params=None):
    """SELECT з БД бота. Повертає list[dict] (RealDictCursor)."""
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


async def aquery(sql, params=None):
    """Async-обгортка над query — щоб не блокувати event loop бота."""
    return await asyncio.to_thread(query, sql, params)


def execute(sql, params=None):
    """INSERT/UPDATE/DELETE у БД бота (це наша база — писати можна)."""
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount
    finally:
        conn.close()


# ---------- Upsert дзеркала архіву ----------

_UPSERT_SQL = """
INSERT INTO articles
    (id, published, updated, status, own_material, owner_id,
     title_ua, title_ru, slug, text_ua, text_ru, synced_at)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    published = EXCLUDED.published,
    updated = EXCLUDED.updated,
    status = EXCLUDED.status,
    own_material = EXCLUDED.own_material,
    owner_id = EXCLUDED.owner_id,
    title_ua = EXCLUDED.title_ua,
    title_ru = EXCLUDED.title_ru,
    slug = EXCLUDED.slug,
    text_ua = EXCLUDED.text_ua,
    text_ru = EXCLUDED.text_ru,
    synced_at = now()
"""

_UPSERT_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"


def upsert_articles(rows):
    """Батчевий upsert статей у дзеркало. rows — list[tuple] у порядку колонок
    _UPSERT_SQL (без synced_at). Повертає кількість рядків."""
    if not rows:
        return 0
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, _UPSERT_SQL, rows, template=_UPSERT_TEMPLATE, page_size=200
            )
        return len(rows)
    finally:
        conn.close()


# ---------- sync_state (курсори синхронізації) ----------

def get_state(key, default=None):
    rows = query("SELECT value FROM sync_state WHERE key = %s", (key,))
    return rows[0]["value"] if rows else default


def set_state(key, value):
    execute(
        "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )


# ---------- Діагностика (/dbbot) ----------

def ping():
    """Стан БД бота: версія, кількість статей у дзеркалі, межі, курсори."""
    start = time.monotonic()
    version = query("SELECT version() AS v")[0]["v"].split(" on ")[0]
    stats = query(
        "SELECT count(*) AS total, min(published) AS oldest, max(published) AS newest "
        "FROM articles"
    )[0]
    cursors = {r["key"]: r["value"] for r in query("SELECT key, value FROM sync_state")}
    return {
        "version": version,
        "articles": stats["total"],
        "oldest_published": stats["oldest"],
        "newest_published": stats["newest"],
        "sync_state": cursors,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
    }
