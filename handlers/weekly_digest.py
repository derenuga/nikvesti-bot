"""
«Тижневик Лиса» — понеділковий редакційний дайджест (заміна щоденного 09:00-звіту).

Тиждень сайту в цифрах одним постом, з порівнянням тиждень-до-тижня:
users/sessions/pageviews за минулий тиждень проти позаминулого, топ-5 матеріалів
тижня, скільки тендерів винюхано, і AI-підводка Лиса.

Порівняння тиждень-до-тижня стало можливим завдяки пам'яті аналітики
(handlers/analytics_store.py, таблиця daily_stats): якщо обидва тижні повністю є
в локальній БД — беремо звідти (дешево, без GA4); інакше фолбек на прямий GA4,
щоб дайджест працював і поки історія ще не накопичилась.

Розклад: понеділок 09:30 (scheduler). Ручний запуск — /weekly.
"""

import asyncio
from datetime import datetime, timedelta

from handlers import analytics_store, social_store, storage
from handlers.google_analytics import get_ga4_client, get_stats, get_top_pages, BASE_URL
from handlers.ai_messages import generate_weekly_digest_comment
from handlers.helpers import escape_html
from handlers.notifier import notify_error

WEEK_DAYS = 7


def _totals_from_series(series):
    return {
        "users": sum(r["users"] or 0 for r in series),
        "sessions": sum(r["sessions"] or 0 for r in series),
        "pageviews": sum(r["pageviews"] or 0 for r in series),
    }


def _week_totals_pair(cur_start, cur_end, prev_start, prev_end):
    """Підсумки двох тижнів (users/sessions/pageviews) + джерело даних.
    Обидва тижні беремо з ОДНОГО джерела — інакше порівняння криве (числа GA4
    «доусідають» ~48 год, а в пам'яті вже стабільні). Пам'ять — лише коли обидва
    тижні повні (7 днів); інакше обидва з GA4."""
    cur = analytics_store.get_daily_series(cur_start, cur_end)
    prev = analytics_store.get_daily_series(prev_start, prev_end)
    if len(cur) >= WEEK_DAYS and len(prev) >= WEEK_DAYS:
        return _totals_from_series(cur), _totals_from_series(prev), "пам'ять бота"
    # Фолбек: прямий GA4 (2 запити на тиждень — дешево)
    client = get_ga4_client()
    cu, cs, cp = get_stats(client, cur_start, cur_end)
    pu, ps, pp = get_stats(client, prev_start, prev_end)
    return (
        {"users": cu, "sessions": cs, "pageviews": cp},
        {"users": pu, "sessions": ps, "pageviews": pp},
        "GA4",
    )


def _week_tenders(cur_start_date, cur_end_date):
    """Скільки тендерів бот винюхав за тиждень + сума + скільки взято в роботу.
    Рахуємо по sent_at з архіву бота (storage)."""
    count = total = taken = 0
    for t in storage.get_all_tenders().values():
        try:
            sent = datetime.fromisoformat(t.get("sent_at", "")).date()
        except (ValueError, TypeError):
            continue
        if cur_start_date <= sent <= cur_end_date:
            count += 1
            total += t.get("amount") or 0
            if t.get("taken_by"):
                taken += 1
    return count, total, taken


def _fmt_wow(cur, prev):
    """Порівняння тиждень-до-тижня: '(+1 234, +5%)' або '(−320, −2%)'."""
    diff = cur - prev
    sign = "+" if diff >= 0 else "−"
    body = f"{sign}{abs(diff):,}".replace(",", " ")
    if prev:
        pct = round(diff / prev * 100)
        psign = "+" if pct >= 0 else "−"
        return f"({body}, {psign}{abs(pct)}%)"
    return f"({body})"


def _num(n):
    return f"{int(n or 0):,}".replace(",", " ")


def _absdiff(cur, prev):
    """' (+120)' / ' (−30)' — абсолютний приріст; порожньо, якщо нема з чим порівняти."""
    if cur is None or prev is None:
        return ""
    d = cur - prev
    sign = "+" if d >= 0 else "−"
    return f" ({sign}{_num(abs(d))})"


def _pctdiff(cur, prev):
    """' (+12%)' / ' (−5%)' — відносний приріст; порожньо, якщо нема з чим порівняти."""
    if cur is None or not prev:
        return ""
    d = round((cur - prev) / prev * 100)
    sign = "+" if d >= 0 else "−"
    return f" ({sign}{abs(d)}%)"


def _social_lines():
    """Рядки соцмереж для тижневика: FB/IG підписники + охоплення/перегляди з
    порівнянням тиждень-до-тижня (два останні зрізи з social_stats). Порожньо,
    якщо зрізів ще немає. IG: основне охоплення — views (Meta перейшов з reach)."""
    out = []
    for platform, emoji, name, primary in (
        (social_store.FACEBOOK, "📘", "FB", "views"),
        (social_store.INSTAGRAM, "📱", "IG", "views"),
    ):
        rows = social_store.get_history(platform, limit=2)
        if not rows:
            continue
        cur = rows[0]
        prev = rows[1] if len(rows) > 1 else None
        bits = []

        followers = cur.get("followers")
        if followers is not None:
            bits.append(f"підписників {_num(followers)}"
                        + _absdiff(followers, prev.get("followers") if prev else None))

        # Охоплення: IG — views (фолбек reach), FB — reach
        reach_val = cur.get(primary)
        reach_label = "переглядів" if primary == "views" else "охоплення"
        if reach_val is None and primary == "views":
            reach_val, reach_label = cur.get("reach"), "охоплення"
        if reach_val is not None:
            prev_reach = None
            if prev:
                prev_reach = prev.get(primary)
                if prev_reach is None and primary == "views":
                    prev_reach = prev.get("reach")
            bits.append(f"{reach_label} {_num(reach_val)}" + _pctdiff(reach_val, prev_reach))

        if bits:
            out.append(f"{emoji} {name}: " + ", ".join(bits))
    return out


async def build_weekly_digest():
    """Складає текст тижневика. Повертає рядок (HTML)."""
    now = datetime.now()
    yesterday = now - timedelta(days=1)
    cur_start = yesterday - timedelta(days=WEEK_DAYS - 1)   # минулий тиждень (7 днів по вчора)
    cur_end = yesterday
    prev_end = cur_start - timedelta(days=1)                 # позаминулий тиждень
    prev_start = prev_end - timedelta(days=WEEK_DAYS - 1)

    cs, ce = cur_start.strftime("%Y-%m-%d"), cur_end.strftime("%Y-%m-%d")
    ps, pe = prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")
    label = f"{cur_start.strftime('%d.%m')} — {cur_end.strftime('%d.%m.%Y')}"

    cur_tot, prev_tot, source = await asyncio.to_thread(_week_totals_pair, cs, ce, ps, pe)

    client = await asyncio.to_thread(get_ga4_client)
    top_pages = await asyncio.to_thread(get_top_pages, client, cs, ce)

    tender_count, tender_total, tender_taken = await asyncio.to_thread(
        _week_tenders, cur_start.date(), cur_end.date()
    )
    tender_mln = round(tender_total / 1_000_000, 1)

    top_titles = [title for _, title, _, _ in top_pages]
    ai_comment = await generate_weekly_digest_comment(
        label, cur_tot, prev_tot, top_titles, tender_count, tender_mln
    )

    lines = [
        f"🦊 <b>Тижневик Лиса</b> ({label})",
        "",
        "📊 <b>Сайт</b> (тиждень до тижня):",
        f"👥 Користувачі: {_num(cur_tot['users'])} {_fmt_wow(cur_tot['users'], prev_tot['users'])}",
        f"🔄 Сесії: {_num(cur_tot['sessions'])} {_fmt_wow(cur_tot['sessions'], prev_tot['sessions'])}",
        f"📄 Перегляди: {_num(cur_tot['pageviews'])} {_fmt_wow(cur_tot['pageviews'], prev_tot['pageviews'])}",
    ]

    if top_pages:
        lines.append("")
        lines.append("🔥 <b>Топ-5 матеріалів тижня:</b>")
        for i, (path, title, views, author) in enumerate(top_pages, 1):
            line = f'  {i}. <a href="{BASE_URL}{path}">{escape_html(title)}</a> — {_num(views)}'
            if author:
                line += f"\n      👤 {escape_html(author)}"
            lines.append(line)

    if tender_count:
        taken_note = f", {tender_taken} взято в роботу" if tender_taken else ""
        lines.append("")
        lines.append(f"🏛 <b>Винюхано:</b> {tender_count} тендер(ів) на {tender_mln} млн грн{taken_note}")

    # Соцмережі — тиждень до тижня з накопичених зрізів (social_stats).
    # Порожньо, поки не набралось хоча б одного знімка (див. /social_capture).
    social = await asyncio.to_thread(_social_lines)
    if social:
        lines.append("")
        lines.append("📣 <b>Соцмережі</b> (тиждень до тижня):")
        lines.extend(f"  {s}" for s in social)

    lines.append("")
    lines.append(f"🦊 {ai_comment}")

    return "\n".join(lines)


async def send_weekly_digest(bot, chat_id):
    """Складає і шле тижневик у чат. Помилку — алерт Олегу, як у решти автозадач."""
    try:
        msg = await build_weekly_digest()
        await bot.send_message(
            chat_id=chat_id, text=msg,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"Помилка тижневика Лиса: {e}")
        await notify_error(bot, "тижневик Лиса", e)


async def weekly_handler(update, context):
    """/weekly — надіслати тижневик у чат редакції вручну (для перевірки)."""
    import os
    chat_id = os.environ.get("CHAT_ID")
    await update.message.reply_text("🦊 Складаю тижневик…")
    await send_weekly_digest(context.bot, chat_id)
