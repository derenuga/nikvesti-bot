"""
Витяг матеріалу з «лисячої нори» текстовим файлом — рятунок, коли на сайті
матеріал затерли, видалили або зіпсували.

Нора (дзеркало архіву, `articles` у Postgres бота) тримає ЧИСТИЙ текст статті
станом на останній синк. Сайт може вже віддавати 404 або чужий текст — нора
пам'ятає свій. Тому команда і потрібна: дістати збережену копію в один клік,
не через ad-hoc /nora_sql (він ріже довгий текст у <pre>).

/nora_article <id | URL | шматок заголовка>

- ID чи URL nikvesti.com → точний матеріал (з URL id дістає extract_article_id);
- будь-який інший текст → пошук по заголовках (ILIKE, укр і рос), свіжі згори;
  один збіг — одразу файл, кілька — список id, з яким повторити команду.

Віддає .txt: шапка (id, дата виходу, дата останнього синку, автор, рубрика,
теги, URL) + текст. Якщо в норі є обидві мовні версії — обидві в одному файлі.
"""

import os
import re
from datetime import datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from handlers import bot_db
from handlers.helpers import escape_html, extract_article_id

KYIV_TZ = ZoneInfo("Europe/Kiev")
BASE_URL = "https://nikvesti.com"

# Скільки варіантів показувати, коли пошук за заголовком дав кілька збігів.
TITLE_SEARCH_LIMIT = 10

_COLUMNS = (
    "id, published, updated, status, own_material, owner_id, "
    "title_ua, title_ru, slug, text_ua, text_ru, category, region, tags_text, synced_at"
)


def _allowed_ids():
    return {int(u) for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u.strip()}


def _fmt_ts(value):
    """Unix-час нори → людський рядок за Києвом."""
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone(KYIV_TZ).strftime(
            "%d.%m.%Y %H:%M"
        )
    except (TypeError, ValueError, OSError):
        return "—"


def article_url(row):
    """Канонічний URL матеріалу: /news/{рубрика}/{slug або id}."""
    tail = (row.get("slug") or "").strip() or str(row["id"])
    cat = (row.get("category") or "").strip()
    return f"{BASE_URL}/news/{cat}/{tail}" if cat else f"{BASE_URL}/news/{tail}"


def render_txt(row, author=None):
    """Файл-рятунок: шапка з метаданими + текст усіх мовних версій, що є в норі."""
    title = row.get("title_ua") or row.get("title_ru") or "(без заголовка)"
    lines = [
        title,
        "",
        f"ID: {row['id']}",
        f"Опубліковано: {_fmt_ts(row.get('published'))}",
        f"Оновлено на сайті: {_fmt_ts(row.get('updated'))}",
        f"Знімок у норі: {row['synced_at'].astimezone(KYIV_TZ):%d.%m.%Y %H:%M}"
        if row.get("synced_at")
        else "Знімок у норі: —",
        f"Автор: {author or '—'}",
        f"Рубрика: {row.get('category') or '—'}",
        f"Теги: {row.get('tags_text') or '—'}",
        f"Власний матеріал: {'так' if row.get('own_material') else 'ні'}",
        f"URL: {article_url(row)}",
        "",
        "=" * 60,
        "",
    ]
    for lang, t_key, x_key in (("Українська", "title_ua", "text_ua"), ("Російська", "title_ru", "text_ru")):
        text = (row.get(x_key) or "").strip()
        if not text:
            continue
        lines += [f"[{lang}]", "", (row.get(t_key) or "").strip(), "", text, "", "-" * 60, ""]
    return "\n".join(lines)


def _filename(row):
    """Ім'я файлу — id + слаг, щоб у теці Олега файли не зливались."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (row.get("slug") or "").strip())[:60].strip("-")
    return f"nora_{row['id']}{'_' + slug if slug else ''}.txt"


async def _author_name(owner_id):
    """ПІБ автора з БД сайту — best-effort: нора owner_id зберігає, імена — ні."""
    if not owner_id:
        return None
    try:
        from handlers import db
        if not db.is_configured():
            return None
        rows = await db.aquery(
            "SELECT first_name, last_name FROM users WHERE id = %s", (int(owner_id),)
        )
        if not rows:
            return None
        full = f"{rows[0].get('first_name') or ''} {rows[0].get('last_name') or ''}".strip()
        return full or None
    except Exception:
        return None


async def find_articles(arg):
    """Матеріали нори за аргументом: точний id (з URL теж) або пошук по заголовках."""
    ident = arg.strip()
    art_id = extract_article_id(ident) if "/" in ident else (ident if ident.isdigit() else None)
    if art_id:
        return await bot_db.aquery(f"SELECT {_COLUMNS} FROM articles WHERE id = %s", (int(art_id),))
    return await bot_db.aquery(
        f"SELECT {_COLUMNS} FROM articles "
        "WHERE title_ua ILIKE %s OR title_ru ILIKE %s "
        "ORDER BY published DESC LIMIT %s",
        (f"%{ident}%", f"%{ident}%", TITLE_SEARCH_LIMIT),
    )


async def nora_article_handler(update, context):
    """/nora_article <id|URL|заголовок> — витягти матеріал із нори текстовим файлом."""
    allowed = _allowed_ids()
    if allowed and update.effective_user.id not in allowed:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not bot_db.is_configured():
        await update.message.reply_text("🦊 Нора не налаштована (BOT_DATABASE_URL).")
        return

    arg = update.message.text.partition(" ")[2].strip()
    if not arg:
        await update.message.reply_text(
            "Використання: /nora_article <id | URL | шматок заголовка>\n\n"
            "Напр.:\n"
            "/nora_article 321354\n"
            "/nora_article https://nikvesti.com/news/justice/321354-...\n"
            "/nora_article стягувати майже ₴200 тис. за землю"
        )
        return

    msg = await update.message.reply_text("🦊 Риюсь у норі…")
    try:
        rows = await find_articles(arg)
    except Exception as e:
        await msg.edit_text(f"❌ Нора не відповіла: {type(e).__name__}: {e}")
        return

    if not rows:
        await msg.edit_text("🦊 У норі такого немає. Спробуй інший шматок заголовка або id.")
        return

    if len(rows) > 1:
        lines = ["🦊 Знайшов кілька — повтори з потрібним id:", ""]
        for r in rows:
            title = (r.get("title_ua") or r.get("title_ru") or "")[:90]
            lines.append(
                f"<code>/nora_article {r['id']}</code> — "
                f"{_fmt_ts(r.get('published'))} — {escape_html(title)}"
            )
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
        return

    row = rows[0]
    author = await _author_name(row.get("owner_id"))
    payload = BytesIO(render_txt(row, author).encode("utf-8"))
    payload.name = _filename(row)
    title = row.get("title_ua") or row.get("title_ru") or ""
    text_len = len((row.get("text_ua") or "").strip()) + len((row.get("text_ru") or "").strip())
    snap = (
        f"\nЗнімок у норі: {row['synced_at'].astimezone(KYIV_TZ):%d.%m.%Y %H:%M}"
        if row.get("synced_at")
        else ""
    )
    await update.message.reply_document(
        document=payload,
        filename=_filename(row),
        caption=(
            f"🦊 <b>{escape_html(title[:200])}</b>\n"
            f"id {row['id']} · {_fmt_ts(row.get('published'))} · {text_len} символів{snap}"
        ),
        parse_mode="HTML",
    )
    await msg.delete()
