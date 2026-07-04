"""
Власна БД бота — Postgres на Railway (хвиля A, docs/ARCHIVE_INTELLIGENCE.md).

«Лисяча нора» (foxhole) — збагачений корпус 17-річного архіву новин сайту:
таблиця articles (текст обома мовами, рубрика, регіон, теги) з повнотекстовим
зваженим пошуком, плюс довідники tags / article_tags. Згодом сюди переїде і
стан модулів моніторингу з /data/prozorro_state.json (окремим кроком).

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
# Збагачення (за запитом Олега, 04.07): own_material (власне/рерайт), category
# (слаг рубрики), region (код регіону), tags_text (назви тегів статті) + окремі
# довідники tags / article_tags. tags_text денормалізовано в рядок статті САМЕ
# щоб вкласти теги в пошуковий індекс — тег стає частиною пошуку («нерухомість»
# знайде статтю з таким тегом, навіть якщо слова немає в тексті).
#
# articles.fts — generated column, ЗВАЖЕНИЙ: заголовок (A) > теги (B) > текст (C).
# ts_rank за замовчуванням дає A=1.0, B=0.4, C=0.2 — збіг у заголовку/тезі
# ранжується вище за випадкову згадку в тілі. Postgres перераховує вектор сам
# при кожному upsert. Конфіг 'simple' (без стемінгу): українського стемера
# немає, морфологію закриваємо префіксним пошуком (слово:*) в archive_search.

# Версія схеми: піднімати при зміні forma таблиці, що потребує міграції.
# 1 = базова нора (text_ua/text_ru); 2 = теги + рубрика/регіон + зважений fts.
SCHEMA_VERSION = 2

# Вираз пошукового вектора — ОДИН на CREATE і на перебудову, щоб не розійшлись.
# Зважений: заголовок (A) > теги (B) > текст (C).
_FTS_EXPR = (
    "setweight(to_tsvector('simple', "
    "coalesce(title_ua,'') || ' ' || coalesce(title_ru,'')), 'A') || "
    "setweight(to_tsvector('simple', coalesce(tags_text,'')), 'B') || "
    "setweight(to_tsvector('simple', "
    "coalesce(text_ua,'') || ' ' || coalesce(text_ru,'')), 'C')"
)

# Базові CREATE ... IF NOT EXISTS — цільова схема для чистої БД. Індекси
# рубрики/регіону і сам fts-індекс сюди НЕ входять: вони залежать від колонок,
# яких у старій таблиці ще немає, тож створюються в міграціях (нижче) вже після
# ADD COLUMN — інакше на старій нopі CREATE INDEX впав би з "column does not exist".
_SCHEMA_STATEMENTS = [
    (
        f"""
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
            category     TEXT,
            region       INTEGER,
            tags_text    TEXT,
            synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            fts          tsvector GENERATED ALWAYS AS ({_FTS_EXPR}) STORED
        )
        """,
        True,  # обов'язковий statement — без нього модуль не працює
    ),
    ("CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published DESC)", True),
    (
        "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)",
        True,
    ),
    # Довідник тегів: канонічні (розмерджені зведені до цільового id у sync).
    # iptc = google_category сайту (IPTC Media Topics — міжнародний стандарт).
    (
        """
        CREATE TABLE IF NOT EXISTS tags (
            id          BIGINT PRIMARY KEY,
            name_ua     TEXT,
            name_ru     TEXT,
            name_en     TEXT,
            iptc        TEXT,
            description TEXT
        )
        """,
        True,
    ),
    # Звʼязок стаття↔тег (many-to-many). Для «усі матеріали з тегом X»,
    # спільних тегів, майбутньої аналітики за IPTC.
    (
        """
        CREATE TABLE IF NOT EXISTS article_tags (
            article_id BIGINT,
            tag_id     BIGINT,
            PRIMARY KEY (article_id, tag_id)
        )
        """,
        True,
    ),
    ("CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags (tag_id)", True),
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

# Ідемпотентні міграції — виконуються ЗАВЖДИ після базових CREATE. CREATE TABLE
# IF NOT EXISTS не додає колонок до вже наявної таблиці, тому нові колонки й
# залежні від них індекси доводимо тут (ADD COLUMN IF NOT EXISTS безпечний і на
# чистій БД, і на старій).
_MIGRATIONS = [
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS category TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS region INTEGER",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS tags_text TEXT",
    "CREATE INDEX IF NOT EXISTS idx_articles_category ON articles (category)",
    "CREATE INDEX IF NOT EXISTS idx_articles_region ON articles (region)",
]

# Версійна перебудова fts: у старої таблиці fts був без tags_text і без ваг.
# Вираз generated-колонки не змінити ALTER-ом — тільки drop+add. На порожній
# норі миттєво; на заповненій (апгрейд) — одноразовий перерахунок вектора.
# Виконується лише коли збережена версія схеми < SCHEMA_VERSION.
_FTS_REBUILD = [
    "ALTER TABLE articles DROP COLUMN IF EXISTS fts",
    f"ALTER TABLE articles ADD COLUMN fts tsvector GENERATED ALWAYS AS ({_FTS_EXPR}) STORED",
    "CREATE INDEX IF NOT EXISTS idx_articles_fts ON articles USING gin (fts)",
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
                # Ідемпотентні міграції (доводять стару таблицю до поточної схеми)
                for sql in _MIGRATIONS:
                    cur.execute(sql)
                # Версійна перебудова fts — лише коли схема застаріла.
                # Читаємо/пишемо sync_state сирим SQL (не через query/get_state —
                # ті самі викликали б ensure_schema і зациклили б).
                cur.execute("SELECT value FROM sync_state WHERE key = 'schema_version'")
                row = cur.fetchone()
                version = int(row[0]) if row and row[0] else 0
                if version < SCHEMA_VERSION:
                    for sql in _FTS_REBUILD:
                        cur.execute(sql)
                    cur.execute(
                        "INSERT INTO sync_state (key, value) VALUES ('schema_version', %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (str(SCHEMA_VERSION),),
                    )
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
     title_ua, title_ru, slug, text_ua, text_ru, category, region, tags_text, synced_at)
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
    category = EXCLUDED.category,
    region = EXCLUDED.region,
    tags_text = EXCLUDED.tags_text,
    synced_at = now()
"""

_UPSERT_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"


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


# ---------- Теги ----------

_TAGS_UPSERT_SQL = """
INSERT INTO tags (id, name_ua, name_ru, name_en, iptc, description)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    name_ua = EXCLUDED.name_ua,
    name_ru = EXCLUDED.name_ru,
    name_en = EXCLUDED.name_en,
    iptc = EXCLUDED.iptc,
    description = EXCLUDED.description
"""


def upsert_tags(rows):
    """Батчевий upsert довідника тегів. rows — list[(id, name_ua, name_ru,
    name_en, iptc, description)]. Повертає кількість."""
    if not rows:
        return 0
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, _TAGS_UPSERT_SQL, rows, page_size=500)
        return len(rows)
    finally:
        conn.close()


def delete_articles(ids):
    """Прибирає статті з нори (і їхні звʼязки з тегами) — коли матеріал зняли
    з публікації або він більше не проходить у корпус. Ідемпотентно."""
    if not ids:
        return 0
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM article_tags WHERE article_id = ANY(%s)", (list(ids),))
            cur.execute("DELETE FROM articles WHERE id = ANY(%s)", (list(ids),))
            return cur.rowcount
    finally:
        conn.close()


def replace_article_tags(article_ids, pairs):
    """Перезаписує звʼязки стаття↔тег для пачки статей: видаляє старі звʼязки
    цих статей і вставляє нові. DELETE-then-INSERT — щоб зняті теги теж
    зникали при ре-синку. pairs — list[(article_id, tag_id)]."""
    if not article_ids:
        return
    ensure_schema()
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM article_tags WHERE article_id = ANY(%s)",
                (list(article_ids),),
            )
            if pairs:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO article_tags (article_id, tag_id) VALUES %s "
                    "ON CONFLICT DO NOTHING",
                    pairs,
                    page_size=500,
                )
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
    tags_total = query("SELECT count(*) AS c FROM tags")[0]["c"]
    tagged = query("SELECT count(DISTINCT article_id) AS c FROM article_tags")[0]["c"]
    cursors = {r["key"]: r["value"] for r in query("SELECT key, value FROM sync_state")}
    return {
        "version": version,
        "articles": stats["total"],
        "oldest_published": stats["oldest"],
        "newest_published": stats["newest"],
        "tags": tags_total,
        "tagged_articles": tagged,
        "sync_state": cursors,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
    }
