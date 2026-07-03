"""
Тонкий адаптер до production-БД сайту nikvesti.com (MySQL, read-only).

Крок A переходу на архітектуру «сервер + БД» (LONG_TERM_VISION.md §2.4):
поки що це лише під'єднання і /dbtest для перевірки. Жодного бізнес-коду —
модулі (NLQ, /stat, контроль заголовків) ляжуть поверх у наступних кроках.

Доступ, який дав KEY4 (02–03.07.2026):
- окремий користувач `nikvesti_bot`, ТІЛЬКИ SELECT на `nikvesti.*`
- SSL обов'язковий (ssl-mode=REQUIRED)
- ліміти сервера: 5 одночасних з'єднань, 10000 запитів/год, 1000 з'єднань/год,
  0 UPDATE/год (записати фізично неможливо — це production-БД сайту)

Через ліміт на одночасні з'єднання і low query volume бота обрано найпростішу
thread-safe модель: **з'єднання на запит** (відкрив → прочитав → закрив).
Пул тут зайвий — запити рідкі, а окреме з'єднання на виклик не ділиться між
потоками (PyMySQL-конекшн не thread-safe).

PyMySQL — блокуючий driver, тому всі публічні хелпери синхронні; в async-коді
викликати через `asyncio.to_thread` (як решта модулів моніторингу).

Конфіг з env (Railway):
    DB_HOST, DB_PORT (3306), DB_NAME (nikvesti), DB_USER, DB_PASSWORD
    DB_SSL_CA           — опційно, шлях до CA-сертифіката сервера. Якщо не заданий,
                          з'єднання все одно шифрується, але без перевірки cert
                          (CA сайту нам не передали — шифрування є, MITM-перевірки нема).
    DB_CONNECT_TIMEOUT  — опційно, сек (дефолт 10)
    DB_READ_TIMEOUT     — опційно, сек (дефолт 30)
"""

import asyncio
import os
import ssl as _ssl
import time

import pymysql
import pymysql.cursors

from handlers.helpers import escape_html

DB_HOST = os.environ.get("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "nikvesti")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_SSL_CA = os.environ.get("DB_SSL_CA")
CONNECT_TIMEOUT = int(os.environ.get("DB_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = int(os.environ.get("DB_READ_TIMEOUT", "30"))

# Захист від випадкового запису: сервер і так дає тільки SELECT, але тонкий
# guard на рівні коду ловить помилку раніше й зрозуміліше.
_READ_ONLY_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")


def is_configured():
    """Чи задані обов'язкові env для під'єднання. Дозволяє боту стартувати
    без БД (модуль опційний, доки не всі env виставлені на Railway)."""
    return bool(DB_HOST and DB_USER and DB_PASSWORD)


def _ssl_context():
    if DB_SSL_CA:
        return _ssl.create_default_context(cafile=DB_SSL_CA)
    # CA сайту не передали — шифруємо з'єднання, але не перевіряємо сертифікат.
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


def _connect():
    if not is_configured():
        raise RuntimeError(
            "БД сайту не налаштована: задайте DB_HOST, DB_USER, DB_PASSWORD"
        )
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        ssl=_ssl_context(),
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        autocommit=True,
    )


def query(sql, params=None):
    """Прочитати рядки з БД. Повертає list[dict] (DictCursor).

    Параметри — через `params` (плейсхолдери %s), не конкатенацією рядків:
        db.query("SELECT * FROM articles WHERE id = %s", (article_id,))

    Дозволені тільки читальні запити (SELECT/SHOW/DESCRIBE/EXPLAIN/WITH) —
    решта відхиляється до звернення в БД."""
    stripped = sql.lstrip().lower()
    if not stripped.startswith(_READ_ONLY_PREFIXES):
        raise ValueError("db.query дозволяє тільки читання (SELECT/SHOW/...)")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


async def aquery(sql, params=None):
    """Async-обгортка над query — щоб не блокувати event loop бота."""
    return await asyncio.to_thread(query, sql, params)


def ping():
    """Діагностика з'єднання для /dbtest: версія MySQL, поточна БД, користувач,
    к-сть таблиць (і їх список) та час відповіді. Один конекшн, кілька SELECT."""
    start = time.monotonic()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT VERSION() AS version, DATABASE() AS db, CURRENT_USER() AS user"
            )
            info = cur.fetchone()
            cur.execute("SHOW TABLES")
            tables = [next(iter(row.values())) for row in cur.fetchall()]
    finally:
        conn.close()
    return {
        "version": info.get("version"),
        "db": info.get("db"),
        "user": info.get("user"),
        "table_count": len(tables),
        "tables": tables,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
    }


_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


async def dbtest_handler(update, context):
    """/dbtest — перевірка з'єднання з БД сайту (тільки для редакції)."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_configured():
        await update.message.reply_text(
            "🦊 БД сайту ще не налаштована.\n"
            "Потрібні env на Railway: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD."
        )
        return
    msg = await update.message.reply_text("🦊 Стукаю в базу сайту…")
    try:
        info = await asyncio.to_thread(ping)
    except Exception as e:
        await msg.edit_text(
            "❌ Не вдалось під'єднатись до БД сайту:\n"
            f"<code>{escape_html(f'{type(e).__name__}: {e}')}</code>",
            parse_mode="HTML",
        )
        return
    tables = info["tables"]
    preview = ", ".join(tables[:25])
    if len(tables) > 25:
        preview += f" … (+{len(tables) - 25})"
    lines = [
        "✅ БД сайту на зв'язку.",
        f"MySQL: <b>{escape_html(str(info['version']))}</b>",
        f"База: <b>{escape_html(str(info['db']))}</b>",
        f"Користувач: <code>{escape_html(str(info['user']))}</code>",
        f"Таблиць: <b>{info['table_count']}</b>",
        f"Відповідь: {info['elapsed_ms']} мс",
    ]
    if tables:
        lines.append(f"\n<i>{escape_html(preview)}</i>")
    await msg.edit_text("\n".join(lines), parse_mode="HTML")


def _format_rows(rows, max_rows=40, max_chars=3500):
    """Компактний вивід результату для /dbquery: рядок = 'col=val | col=val'."""
    if not rows:
        return "(0 рядків)"
    lines = []
    for row in rows[:max_rows]:
        lines.append(" | ".join(f"{k}={v}" for k, v in row.items()))
    text = "\n".join(lines)
    if len(rows) > max_rows:
        text += f"\n… (+{len(rows) - max_rows} рядків)"
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


async def dbquery_handler(update, context):
    """/dbquery <SELECT...> — службова розвідка схеми БД з Railway (тимчасово).

    Тільки для редакції, тільки читання (guard у query). Потрібна, щоб побачити
    реальну структуру таблиць (options, nodes тощо) перед написанням модулів —
    з локального оточення БД недоступна (whitelist за IP Railway). Той самий
    підхід, що /outage_probe. Прибрати, коли схема зафіксована в модулях."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_configured():
        await update.message.reply_text("🦊 БД сайту ще не налаштована (DB_* env).")
        return
    # Беремо весь текст після команди, щоб зберегти SQL з комами й пробілами.
    sql = update.message.text.partition(" ")[2].strip()
    if not sql:
        await update.message.reply_text(
            "Використання: /dbquery <SELECT…>\n"
            "Напр.: /dbquery SHOW TABLES\n"
            "/dbquery DESCRIBE options\n"
            "/dbquery SELECT * FROM options LIMIT 5"
        )
        return
    msg = await update.message.reply_text("🦊 Виконую…")
    try:
        rows = await aquery(sql)
    except Exception as e:
        await msg.edit_text(
            f"❌ <code>{escape_html(f'{type(e).__name__}: {e}')}</code>",
            parse_mode="HTML",
        )
        return
    body = _format_rows(rows)
    await msg.edit_text(
        f"<b>{len(rows)} рядк(ів):</b>\n<pre>{escape_html(body)}</pre>",
        parse_mode="HTML",
    )
