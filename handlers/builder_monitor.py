"""
Монітор оновлення білдера головної сторінки nikvesti.com.

Білдер — «конструктор» першого екрана сайту: журналісти вручну верстають туди
матеріали. Зберігається в таблиці `options` (name='builder', кілька namespace-
блоків: a1, a2, a3, b1, c3, t1, t2…). Усі блоки записуються одним збереженням →
`options.date` (unix-час) у всіх рядках білдера однаковий і дорівнює часу
останнього оновлення головної. Тому builder_last = MAX(date) по name='builder'.

Умова алерту (запит Олега):
  (а) білдер не оновлювався > BUILDER_STALE_HOURS (2 год);
  (б) за цей час вийшло ≥ MIN_FRESH_NEWS (2) власних новин
      (nodes.own_material=1, status=1, published у вікні (builder_last, now]).
Тоді Лис акуратно пише в чат редакції: скільки висить, хто випустив, заголовки,
«давайте оновимо». Кулдаун ALERT_COOLDOWN_HOURS, щоб не спамити; коли редактор
оновить білдер — builder_last стрибає на now і умова згасає сама.

Автор новини: nodes.owner_id → users.id, ім'я = users.first_name (порожній join
→ фолбек). `published` гейтимо по <= now: у nodes бувають відкладені пости на
майбутнє (status=1, але published у майбутньому) — їх ще нема на сайті, не рахуємо.

Часи всюди — unix epoch (UTC): і options.date/nodes.published у БД, і time.time(),
тому порівняння без прив'язки до таймзони. Розклад запуску (Києвом) — у scheduler.
"""

import asyncio
import os
import time

from handlers import db, storage
from handlers.helpers import escape_html
from handlers.notifier import notify_error

CHAT_ID = os.environ.get("CHAT_ID")

BUILDER_STALE_HOURS = 2
MIN_FRESH_NEWS = 2
ALERT_COOLDOWN_HOURS = 2
# Скільки заголовків показувати в пості (решта — «…і ще N»).
MAX_TITLES = 6


def _builder_last_updated():
    """Unix-час останнього оновлення білдера (MAX по всіх блоках) або None."""
    rows = db.query("SELECT MAX(`date`) AS last FROM options WHERE name = 'builder'")
    if not rows or rows[0].get("last") is None:
        return None
    return int(rows[0]["last"])


def _fresh_own_news(since_ts, until_ts):
    """Власні новини (own_material=1, опубліковані), published у вікні (since, until]."""
    sql = (
        "SELECT n.id, n.published, n.title_ua, n.title, u.first_name "
        "FROM nodes n "
        "LEFT JOIN users u ON u.id = n.owner_id "
        "WHERE n.own_material = 1 AND n.status = 1 "
        "AND n.published > %s AND n.published <= %s "
        "ORDER BY n.published ASC"
    )
    return db.query(sql, (since_ts, until_ts))


def _author_name(row):
    return (row.get("first_name") or "").strip() or "хтось із редакції"


def _news_title(row):
    return (row.get("title_ua") or row.get("title") or "").strip() or f"новина #{row.get('id')}"


def _format_alert(gap_hours, news):
    # Автор — до кожного заголовка (і інформативніше, і уникає узгодження
    # дієслова за родом/числом, бо автор може бути один і будь-якої статі).
    titles = [
        f"• {escape_html(_news_title(row))} — {escape_html(_author_name(row))}"
        for row in news[:MAX_TITLES]
    ]
    more = len(news) - MAX_TITLES
    if more > 0:
        titles.append(f"…і ще {more}")
    return (
        f"🦊 Шеф, головна застоялась — білдер не оновлювався вже понад {int(gap_hours)} год.\n"
        f"А на сайті за цей час вийшли нові власні матеріали ({len(news)}):\n\n"
        f"{chr(10).join(titles)}\n\n"
        f"Давайте оновимо головну? Хто підхопить?"
    )


async def check_builder_staleness(bot):
    """Планова перевірка: білдер застоявся + вийшли нові власні новини → пост у чат."""
    try:
        if not db.is_configured():
            return
        now = int(time.time())
        builder_last = await asyncio.to_thread(_builder_last_updated)
        if builder_last is None:
            return
        gap_hours = (now - builder_last) / 3600
        if gap_hours < BUILDER_STALE_HOURS:
            return
        news = await asyncio.to_thread(_fresh_own_news, builder_last, now)
        if len(news) < MIN_FRESH_NEWS:
            return
        # Кулдаун: не частіше ніж раз на ALERT_COOLDOWN_HOURS (умова згасне сама,
        # коли редактор оновить білдер — тоді builder_last стрибне вперед).
        state = storage.get_builder_monitor_state()
        last_alert = state.get("last_alert_at")
        if last_alert and (now - last_alert) < ALERT_COOLDOWN_HOURS * 3600:
            return
        text = _format_alert(gap_hours, news)
        await bot.send_message(
            chat_id=CHAT_ID, text=text, disable_web_page_preview=True
        )
        storage.save_builder_monitor_state({"last_alert_at": now})
    except Exception as e:
        print("Помилка монітора білдера: " + str(e))
        await notify_error(bot, "монітор білдера", e)


_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


async def builder_handler(update, context):
    """/builder — діагностика монітора: коли оновлювався білдер, скільки власних
    новин вийшло відтоді, і чи спрацював би алерт зараз. Тільки для редакції."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not db.is_configured():
        await update.message.reply_text("🦊 БД сайту ще не налаштована (DB_* env).")
        return
    msg = await update.message.reply_text("🦊 Дивлюсь білдер…")
    try:
        now = int(time.time())
        builder_last = await asyncio.to_thread(_builder_last_updated)
        if builder_last is None:
            await msg.edit_text("🦊 Не знайшов рядків білдера в options.")
            return
        gap_hours = (now - builder_last) / 3600
        news = await asyncio.to_thread(_fresh_own_news, builder_last, now)
    except Exception as e:
        await msg.edit_text(
            f"❌ <code>{escape_html(f'{type(e).__name__}: {e}')}</code>",
            parse_mode="HTML",
        )
        return
    would_alert = gap_hours >= BUILDER_STALE_HOURS and len(news) >= MIN_FRESH_NEWS
    verdict = "🔔 умова алерту виконана" if would_alert else "🟢 поки тихо"
    titles = "\n".join(
        f"• {escape_html(_news_title(r))} — {escape_html(_author_name(r))}"
        for r in news[:MAX_TITLES]
    )
    lines = [
        f"Білдер оновлювався {gap_hours:.1f} год тому (поріг {BUILDER_STALE_HOURS} год).",
        f"Власних новин відтоді: {len(news)} (поріг {MIN_FRESH_NEWS}).",
        verdict,
    ]
    if titles:
        lines.append("")
        lines.append(titles)
    await msg.edit_text("\n".join(lines))
