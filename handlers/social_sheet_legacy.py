"""
Разова міграція історії зі старої ручної таблиці «МикВісті SMM» у таблицю
аналітики (social_sheet.py).

Звідки дані: стара таблиця SMM-редакторки (Google Sheets «МикВісті SMM»,
1MDov5UBfTz1Lvnt4purUW4Zkz6qcW0uzsxCiDOgyQrc) розпарсена офлайн Claude-сесією
14.07.2026 у data/legacy_smm.json: 29 місяців (2024-02 … 2026-06), 6 мереж.
Особливості парсингу, вже враховані в JSON:
- місяці липень–грудень на листі «Статистика 2026» були несвіжими копіями
  2025-го (лист дублювали) — викинуті;
- числа виду «148к», «7,3 тис.», «2.8 млн», нерозривні пробіли — нормалізовані;
- CTR YouTube зберігався то як «5,3 %», то як частка «0.056» — приведено до
  частки; спот-чек берёзня 2026 звірено з живим скріном таблиці.

Куди пишеться (рядок місяця у річному листі, лише сирі числа — дельти/підсумки
рахують формули листа):
    ig: B підписники, D перегляди, F охоплення, G взаємодії
    fb: B підписники, D перегляди, F взаємодії
    tg: B підписники, D сер. охоплення поста
    vb: B підписники, D активні користувачі, F повідомлення
    tt: B підписники, D перегляди, F охоплення, G вподобайки, H поширення, I коментарі
    yt: B підписники, D перегляди, F час перегляду (год), G контент, H CTR

Чого в старій таблиці немає, а бот пише з API: пости/топ-допис FB, пости IG,
пости/перегляди-за-місяць TG, весь блок «Сайт». Стик методологій: до 2026-06
включно — числа старої таблиці (методологія SMM/Business Suite), з 2026-07 —
числа бота з API; Δ на стику липня буде «кривою» один раз — це очікувано.

Ідемпотентно: перезаписує ті самі клітинки; можна ганяти повторно.
"""

import asyncio
import json
import os

from handlers.sheets import _get_sheets_service
from handlers.social_sheet import (
    SOCIAL_SPREADSHEET_ID, SPREADSHEET_URL, _ensure_year_sheet,
    FBB, IGB, TGB, VBB, TTB, YTB, _ALLOWED_USER_IDS,
)

LEGACY_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "legacy_smm.json")

# мережа → (блок, {поле JSON → колонка рядка місяця})
COLMAP = {
    "ig": (IGB, {"followers": "B", "views": "D", "reach": "F", "interactions": "G"}),
    "fb": (FBB, {"followers": "B", "views": "D", "interactions": "F"}),
    "tg": (TGB, {"followers": "B", "avg_reach": "D"}),
    "vb": (VBB, {"followers": "B", "active": "D", "messages": "F"}),
    "tt": (TTB, {"followers": "B", "views": "D", "reach": "F",
                 "likes": "G", "shares": "H", "comments": "I"}),
    "yt": (YTB, {"followers": "B", "views": "D", "watch_hours": "F",
                 "content": "G", "ctr": "H"}),
}


def _load_legacy():
    with open(LEGACY_JSON, encoding="utf-8") as f:
        return json.load(f)


def _month_ranges(year, month, nets):
    """values.batchUpdate-діапазони одного місяця зі старих даних."""
    data = []
    for net, fields in nets.items():
        block, colmap = COLMAP.get(net, (None, None))
        if block is None:
            continue
        r = block["m1"] + month - 1
        for field, col in colmap.items():
            v = fields.get(field)
            if v is not None:
                data.append({"range": f"'{year}'!{col}{r}", "values": [[v]]})
    return data


def _run_migration():
    """Синхронно: гарантує річні листи, пише всі місяці. Повертає
    (місяців, клітинок, роки)."""
    legacy = _load_legacy()
    service = _get_sheets_service()
    years = sorted({int(k[:4]) for k in legacy})
    for y in years:
        _ensure_year_sheet(service, y)
    total_cells = 0
    for y in years:
        data = []
        for key, nets in legacy.items():
            ky, km = int(key[:4]), int(key[5:7])
            if ky == y:
                data.extend(_month_ranges(ky, km, nets))
        if data:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=SOCIAL_SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
            total_cells += len(data)
    return len(legacy), total_cells, years


async def sheet_migrate_legacy_handler(update, context):
    """/sheet_migrate_legacy — разово перенести історію зі старої таблиці
    «МикВісті SMM» (2024-02 … 2026-06) у таблицю аналітики: підписники всіх
    мереж (їх API заднім числом не віддають) + YouTube/TikTok/Viber цілком.
    Ідемпотентно, дані бота за нові місяці не чіпає."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    msg = await update.message.reply_text(
        "🦊 Переношу історію зі старої таблиці «МикВісті SMM»…"
    )
    try:
        months, cells, years = await asyncio.to_thread(_run_migration)
        await msg.edit_text(
            f"✅ Перенесено {months} місяців ({cells} значень) у листи "
            f"{', '.join(map(str, years))}: підписники всіх мереж, IG/FB/TG "
            f"метрики + YouTube/TikTok/Viber цілком. Дельти й підсумки роки "
            f"порахували формули листів.\n"
            f"Стик методологій: до 2026-06 — числа старої таблиці, "
            f"з 2026-07 — числа бота з API.\n{SPREADSHEET_URL}",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")
