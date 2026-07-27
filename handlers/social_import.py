"""
Імпорт історії соцмереж із Google-таблиці «Аналітика МикВісті» в Нору.

Навіщо: до 27.07.2026 бот збирав місячні числа соцмереж і писав їх ЛИШЕ в
таблицю — у власній БД лишались тільки тижневі зрізи Facebook та Instagram.
Тобто вся історія Telegram / YouTube / TikTok / Viber (а з нею й перенесена
стара ручна таблиця редакції з лютого 2024) жила поза ботом: NLQ її не бачив,
дашборд Mini App не мав чого показати. Ця команда закриває розрив назад —
уперед те саме пише `social_store.record_month` на кожному місячному знімку.

Читаємо ті самі клітинки, у які пише `social_sheet`: розкладка блоків (рядок
першого місяця + колонки) береться з його ж констант, тож імпорт не може
розійтися з експортом. Значення — `UNFORMATTED_VALUE`: числа приходять
числами, а не рядками «41 012», і формули дельт віддають уже порахований
результат (ми їх усе одно не читаємо — у Норі дельти рахуються з даних).

Ідемпотентно: `upsert_social_monthly` зливає рядки по (platform, month), а
COALESCE не дає порожній клітинці затерти вже відоме число. Тому ганяти можна
скілько завгодно і в будь-якому порядку з живими знімками.

Команда: /social_import_sheet [рік|all]  (дефолт — усі річні листи)
"""

import asyncio
import os
import re

from handlers import bot_db, social_sheet, social_store
from handlers.notifier import notify_error
from handlers.sheets import _get_sheets_service

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# Що з якої колонки блоку брати. Індекси — від A (0), рівно як у headers
# блоків social_sheet; колонки дельт (Δ / ±) свідомо пропущені — це формули.
#
# fb:  A міс · B підписники · C ± · D перегляди · E Δ · F взаємодії · G Δ · H пости
# ig:  A міс · B підписники · C ± · D перегляди · E Δ · F охоплення · G взаємодії · H Δ · I пости
# tg:  A міс · B підписники · C ± · D сер. охоплення поста · E Δ · F пости · G перегляди за місяць
# yt:  A міс · B підписники · C ± · D перегляди відео · E Δ · F години перегляду
# tt:  A міс · B підписники · C ± · D перегляди відео · E Δ · F охоплення · G лайки · H поширення · I коментарі
# vb:  A міс · B підписники · C ± · D активні · E Δ · F постів у канал
COLUMN_MAP = {
    "fb": {"followers": 1, "views": 3, "engagement": 5, "posts": 7},
    "ig": {"followers": 1, "views": 3, "reach": 5, "engagement": 6, "posts": 8},
    "tg": {"followers": 1, "views": 6, "posts": 5, "extra": {"avg_views": 3}},
    "yt": {"followers": 1, "views": 3, "extra": {"watch_hours": 5}},
    "tt": {"followers": 1, "views": 3, "reach": 5,
           "extra": {"likes": 6, "shares": 7, "comments": 8}},
    "vb": {"followers": 1, "posts": 5, "extra": {"active_users": 3}},
}

_YEAR_RE = re.compile(r"^(20\d{2})$")


def _num(value):
    """Клітинка → int або None. Порожньо, текст, помилка формули (#N/A),
    нуль-як-заглушка — усе це «немає даних», а не нуль: у таблиці порожня
    клітинка означає «не знімали», і записати туди 0 було б брехнею."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) or None
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    text = text.replace(" ", "").replace(" ", "").replace(" ", "")
    text = text.replace(",", ".")
    try:
        return int(round(float(text))) or None
    except ValueError:
        return None


def year_sheets(service):
    """Назви річних листів таблиці («2024», «2025», «2026»), за зростанням."""
    meta = service.spreadsheets().get(
        spreadsheetId=social_sheet.SOCIAL_SPREADSHEET_ID,
        fields="sheets.properties.title",
    ).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    return sorted(t for t in titles if _YEAR_RE.match(t))


def _read_year(service, year):
    """Значення блоків одного річного листа: {ключ блоку: [12 рядків]}."""
    blocks = [b for b in social_sheet.BLOCKS if b["key"] in COLUMN_MAP]
    ranges = [f"'{year}'!A{b['m1']}:K{b['m1'] + 11}" for b in blocks]
    resp = service.spreadsheets().values().batchGet(
        spreadsheetId=social_sheet.SOCIAL_SPREADSHEET_ID,
        ranges=ranges, valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    out = {}
    for block, value_range in zip(blocks, resp.get("valueRanges", [])):
        out[block["key"]] = value_range.get("values", [])
    return out


def _rows_for_year(year, by_block):
    """Рядки для social_monthly з прочитаного листа. Місяць = позиція рядка в
    блоці (рядок m1 + m − 1), як і при записі. Порожній місяць пропускаємо
    цілком — інакше в Норі осіли б дванадцять пустих рядків на кожен рік."""
    rows = []
    for key, values in by_block.items():
        platform = social_store.SHEET_BLOCK_PLATFORM[key]
        spec = COLUMN_MAP[key]
        for i in range(12):
            row = values[i] if i < len(values) else []
            get = lambda idx: _num(row[idx]) if idx < len(row) else None
            metrics = {name: get(idx) for name, idx in spec.items()
                       if name != "extra"}
            extra = {name: get(idx) for name, idx in (spec.get("extra") or {}).items()}
            if not any(v is not None for v in list(metrics.values()) + list(extra.values())):
                continue
            rows.append(social_store._month_row(
                platform, f"{year}-{i + 1:02d}-01", source="sheet",
                extra=extra, **metrics))
    return rows


async def import_year(year):
    """Імпорт одного річного листа. Повертає кількість залитих рядків
    (платформа × місяць)."""
    service = await asyncio.to_thread(_get_sheets_service)
    by_block = await asyncio.to_thread(_read_year, service, year)
    rows = _rows_for_year(int(year), by_block)
    if not rows:
        return 0
    return await asyncio.to_thread(bot_db.upsert_social_monthly, rows)


async def import_all():
    """Імпорт усіх річних листів. Повертає (усього рядків, {рік: рядків})."""
    service = await asyncio.to_thread(_get_sheets_service)
    years = await asyncio.to_thread(year_sheets, service)
    total, per_year = 0, {}
    for year in years:
        by_block = await asyncio.to_thread(_read_year, service, year)
        rows = _rows_for_year(int(year), by_block)
        if rows:
            await asyncio.to_thread(bot_db.upsert_social_monthly, rows)
        per_year[year] = len(rows)
        total += len(rows)
    return total, per_year


def coverage_text():
    """Що тепер лежить у Норі — рядок на платформу, для звіту команди."""
    rows = social_store.month_coverage()
    if not rows:
        return "Місячна історія поки порожня."
    lines = []
    for r in rows:
        title = social_store.PLATFORM_TITLES.get(r["platform"], r["platform"])
        lines.append(f"  {title}: {r['months']} міс ({r['oldest']} — {r['newest']})")
    return "\n".join(lines)


async def social_import_sheet_handler(update, context):
    """/social_import_sheet [рік|all] — залити місячну історію соцмереж із
    Google-таблиці в Нору. Ідемпотентно; дефолт — усі річні листи."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not social_store.is_ready():
        await update.message.reply_text(
            "🦊 БД бота не налаштована (BOT_DATABASE_URL) — нема куди заливати."
        )
        return
    arg = (context.args[0] if context.args else "all").strip().lower()
    msg = await update.message.reply_text(
        f"🦊 Читаю таблицю аналітики ({'усі роки' if arg == 'all' else arg}) "
        "і складаю історію соцмереж у нору…"
    )
    try:
        if arg == "all":
            total, per_year = await import_all()
            detail = ", ".join(f"{y}: {n}" for y, n in sorted(per_year.items()))
        else:
            if not _YEAR_RE.match(arg):
                await msg.edit_text("🦊 Формат: /social_import_sheet 2025 або all")
                return
            total = await import_year(arg)
            detail = f"{arg}: {total}"
        await msg.edit_text(
            f"✅ Залито {total} рядків (мережа × місяць).\n{detail}\n\n"
            f"У норі тепер:\n{coverage_text()}\n\n"
            "Далі кожен місячний знімок пише і в таблицю, і в нору сам."
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
        await notify_error(context.bot, "імпорт історії соцмереж із таблиці", e)
