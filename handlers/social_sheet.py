"""
Місячний знімок аналітики соцмереж і сайту в Google Spreadsheet.

Бот сам веде таблицю (env SOCIAL_SPREADSHEET_ID): на кожен рік — окремий лист
(«2025», «2026», …), всередині — блоки по джерелах, місяці РЯДКАМИ (набір метрик
фіксований, таблиця росте вниз — бот 1-го числа просто заповнює рядок місяця,
нічого не зсуваючи):

    ряд 1        заголовок «🦊 Аналітика МикВісті — {рік}»
    3–17         🌐 САЙТ (GA4: користувачі/сеанси/перегляди + Search Console:
                 кліки Search/News/Discover; AI Overviews Google окремим типом
                 в API НЕ віддає — вони всередині web/Search)
    19–33        📘 FACEBOOK (підписники, перегляди page_media_view, взаємодії,
                 пости, топ-допис лінком)
    35–49        📷 INSTAGRAM (підписники, перегляди, охоплення, взаємодії, пости)
    51–65        ✈️ TELEGRAM (підписники, сер. охоплення поста, пости, перегляди,
                 ERR) — офіційного API статистики немає, парсимо веб-дзеркало
                 t.me/s (та сама механіка, що telegram_stats.py)
    67–81        ▶️ YOUTUBE, 83–97 🎵 TIKTOK, 99–113 💜 VIBER — повноцінні
                 блоки-каркаси (формат, дельти-формули, підсумки, спарклайни),
                 бот їх ПОКИ не заповнює: числа приїдуть міграцією зі старої
                 ручної таблиці і згодом з їх API; формули оживають самі

Кожен блок: 12 рядків місяців + рядок «Підсумок {рік}» ЖИВИМИ ФОРМУЛАМИ
(SUM/AVERAGE/LOOKUP по місяцях + порівняння з листом попереднього року через
INDIRECT — якщо листа ще немає, IFERROR лишає порожньо). Тобто підсумок року
не треба «вставляти в кінці року» — він рахується сам з першого місяця.

Дельти MoM — теж формули (створюються разом із листом): бот пише лише сирі
числа, стрілки ▲▼ і колір малює custom number format
([Color 10]▲ …;[Color 9]▼ …), у ячейці — чисте число. Спарклайн року — формула
SPARKLINE у злитій клітинці колонки «Тренд». Два вбудовані графіки (підписники
по мережах, перегляди сайту) створюються один раз при створенні листа і
оновлюються самі — діапазони покривають усі 12 місяців наперед.

ВАЖЛИВО про локаль: формули пишемо з роздільником «;» і «\\» в масивах
(SPARKLINE) — це синтаксис локалей з десятковою комою. Тому при першому
торканні таблиці примусово ставимо locale=uk_UA (інакше формули б зламались
на en_US). Таблицю створює людина і шарить на service account (як тендерну —
див. handlers/sheets.py), бот створює лише листи всередині.

Ідемпотентність: запис місяця — це update фіксованих клітинок рядка, повторний
запуск просто перепише ті самі значення. Блок, чиє джерело впало, пропускається
(існуючі числа не затираються), помилка потрапляє у звіт команди/алерт.

Бекфіл (/sheet_backfill): сайт — GA4 тримає всю історію; Search Console —
~16 місяців; Facebook insights — ~2 роки (що Meta не віддасть — пропуститься);
Instagram — пробуємо (Meta зазвичай віддає до ~2 років insights з since/until);
Telegram — дзеркало t.me зберігає всю історію, але перегляди — поточні
(накопичені), а не «на кінець того місяця». Підписники заднім числом недоступні
ніде — колонка «Підписники» заповнюється лише живими знімками вперед.
"""

import asyncio
import calendar
import time
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from handlers import analytics_store
from handlers import telegram_stats as tg_stats
from handlers.google_analytics import get_ga4_client, get_stats
from handlers.helpers import MONTHS_UA
from handlers.notifier import notify_error, ADMIN_CHAT_ID
from handlers.sheets import _get_sheets_service

KYIV_TZ = ZoneInfo("Europe/Kiev")

# Таблиця «Аналітика МикВісті» (створена Олегом 14.07.2026, розшарена на SA)
SOCIAL_SPREADSHEET_ID = os.environ.get(
    "SOCIAL_SPREADSHEET_ID", "1KNkxqN8ru4c2ez-x3nw9sEW-lXm562VdfJ3EEdbjGZk"
)
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SOCIAL_SPREADSHEET_ID}"

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# ---------- Сітка річного листа (рядки 1-індексовані, як в UI) ----------

NUM_COLS = 11  # A..K; K — «Тренд» (спарклайн)
SHEET_ROWS, SHEET_COLS = 120, 20

# Кольори мереж (бренд) і їх світлі тінти для шапок колонок
FOX, FOX_TINT = "#D9530B", "#FBEBE0"
FB, FB_TINT = "#1877F2", "#E9F1FE"
IG, IG_TINT = "#C13584", "#FBEDF5"
TG, TG_TINT = "#229ED9", "#E8F5FC"
YT, YT_TINT = "#CC0000", "#FDEAEA"
TT, TT_TINT = "#161823", "#E9EBEE"
VB, VB_TINT = "#7360F2", "#EFECFC"
ROW_TINT = "#F7F6F2"       # чергування рядків місяців
TOTAL_BG = "#EFEDE6"       # рядок підсумку року
BORDER_SOFT = "#E0DED8"
BORDER_STRONG = "#B9B6AD"

# Блоки: рядки band (кольорова шапка) / header (назви колонок) / перший місяць /
# підсумок. Рядок місяця m = m1 + m - 1. МІНЯТИ ОБЕРЕЖНО: формули підсумків і
# січневі порівняння з груднем минулого року зав'язані на ці адреси, і вони
# однакові на всіх річних листах (INDIRECT("'{рік-1}'!B32") тощо).
SITE = {"key": "site", "band": 3, "hdr": 4, "m1": 5, "total": 17,
        "color": FOX, "tint": FOX_TINT,
        "title": "🌐  САЙТ NIKVESTI.COM — GA4 + Search Console",
        "headers": ["Місяць", "Користувачі", "Δ", "Сеанси", "Перегляди", "Δ",
                    "🔍 Search", "📰 News", "💡 Discover", "Discover %", "Тренд"]}
FBB = {"key": "fb", "band": 19, "hdr": 20, "m1": 21, "total": 33,
       "color": FB, "tint": FB_TINT,
       "title": "📘  FACEBOOK — МикВісті",
       "headers": ["Місяць", "Підписники", "+ / −", "Перегляди", "Δ",
                   "Взаємодії", "Δ", "Пости", "Топ допис", "", "Тренд"]}
IGB = {"key": "ig", "band": 35, "hdr": 36, "m1": 37, "total": 49,
       "color": IG, "tint": IG_TINT,
       "title": "📷  INSTAGRAM — @nikvesti",
       "headers": ["Місяць", "Підписники", "+ / −", "Перегляди", "Δ",
                   "Охоплення", "Взаємодії", "Δ", "Пости", "", "Тренд"]}
TGB = {"key": "tg", "band": 51, "hdr": 52, "m1": 53, "total": 65,
       "color": TG, "tint": TG_TINT,
       "title": "✈️  TELEGRAM — @nikvesti",
       "headers": ["Місяць", "Підписники", "+ / −", "Сер. охоплення поста", "Δ",
                   "Пости", "Перегляди за місяць", "ERR", "", "", "Тренд"]}
# Блоки-каркаси: бот їх не заповнює (API ще не підключені), числа приїдуть
# міграцією зі старої ручної таблиці і згодом з API — формули оживають самі.
# Набір колонок — за метриками старої таблиці редакції.
YTB = {"key": "yt", "band": 67, "hdr": 68, "m1": 69, "total": 81,
       "color": YT, "tint": YT_TINT,
       "title": "▶️  YOUTUBE — МикВісті   (дані вручну — API ще не підключено)",
       "headers": ["Місяць", "Підписники", "+ / −", "Перегляди відео", "Δ",
                   "Час перегляду, год", "Контент", "CTR", "", "", "Тренд"]}
TTB = {"key": "tt", "band": 83, "hdr": 84, "m1": 85, "total": 97,
       "color": TT, "tint": TT_TINT,
       "title": "🎵  TIKTOK — @nikvesti   (дані вручну — API ще не підключено)",
       "headers": ["Місяць", "Підписники", "+ / −", "Перегляди відео", "Δ",
                   "Охоплення", "Вподобайки", "Поширення", "Коментарі", "", "Тренд"]}
VBB = {"key": "vb", "band": 99, "hdr": 100, "m1": 101, "total": 113,
       "color": VB, "tint": VB_TINT,
       "title": "💜  VIBER — МикВісті   (дані вручну — API ще не підключено)",
       "headers": ["Місяць", "Підписники", "+ / −", "Активні користувачі", "Δ",
                   "Надіслано повідомлень", "", "", "", "", "Тренд"]}
BLOCKS = [SITE, FBB, IGB, TGB, YTB, TTB, VBB]
MANUAL_BLOCKS = [YTB, TTB, VBB]  # каркаси без авто-заповнення

# Формати чисел. Патерни канонічні (крапка/кома), відображення локалізує Sheets.
# [Color 10] — темно-зелений, [Color 9] — темно-червоний з індексованої палітри.
FMT_NUM = {"type": "NUMBER", "pattern": "#,##0"}
FMT_NUM1 = {"type": "NUMBER", "pattern": "#,##0.0"}   # години перегляду YT
FMT_PCT = {"type": "NUMBER", "pattern": "0.0%"}
FMT_PCT_DELTA = {"type": "NUMBER", "pattern": "[Color 10]▲ 0.0%;[Color 9]▼ 0.0%;0.0%"}
FMT_ABS_DELTA = {"type": "NUMBER", "pattern": "[Color 10]▲ #,##0;[Color 9]▼ #,##0;0"}

# 0-базовані колонки → формат, для рядків місяців і підсумку кожного блоку
COL_FORMATS = {
    "site": {1: FMT_NUM, 2: FMT_PCT_DELTA, 3: FMT_NUM, 4: FMT_NUM, 5: FMT_PCT_DELTA,
             6: FMT_NUM, 7: FMT_NUM, 8: FMT_NUM, 9: FMT_PCT},
    "fb":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM, 6: FMT_PCT_DELTA, 7: FMT_NUM},
    "ig":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM, 6: FMT_NUM, 7: FMT_PCT_DELTA, 8: FMT_NUM},
    "tg":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM, 6: FMT_NUM, 7: FMT_PCT},
    "yt":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM1, 6: FMT_NUM, 7: FMT_PCT},
    "tt":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM, 6: FMT_NUM, 7: FMT_NUM, 8: FMT_NUM},
    "vb":   {1: FMT_NUM, 2: FMT_ABS_DELTA, 3: FMT_NUM, 4: FMT_PCT_DELTA,
             5: FMT_NUM},
}


def _rgb(hexcode):
    h = hexcode.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def _grid(sheet_id, r1, r2, c1=0, c2=NUM_COLS):
    """GridRange: рядки 1-індексовані включно (як в UI), колонки 0-базовані."""
    return {"sheetId": sheet_id, "startRowIndex": r1 - 1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


# ---------- Формули річного листа (локаль uk_UA: «;» і «\\» в масивах) ----------

def _prev_dec(year, col, block):
    """Адреса грудня попереднього року в тій самій колонці блока (для січня)."""
    return f"INDIRECT(\"'{year - 1}'!{col}{block['m1'] + 11}\")"


def _delta_pct_formula(year, block, col, row_idx):
    """Δ% місяць-до-місяця; для січня — до грудня минулорічного листа."""
    r = block["m1"] + row_idx
    cell = f"{col}{r}"
    if row_idx == 0:
        prev = _prev_dec(year, col, block)
        return f'=IFERROR(IF({cell}="";"";({cell}-{prev})/{prev});"")'
    prev = f"{col}{r - 1}"
    return f'=IF(OR({prev}="";{cell}="");"";({cell}-{prev})/{prev})'


def _delta_abs_formula(year, block, col, row_idx):
    """± абсолют (підписники) місяць-до-місяця; січень — до минулого грудня."""
    r = block["m1"] + row_idx
    cell = f"{col}{r}"
    if row_idx == 0:
        prev = _prev_dec(year, col, block)
        return f'=IFERROR(IF({cell}="";"";{cell}-{prev});"")'
    prev = f"{col}{r - 1}"
    return f'=IF(OR({prev}="";{cell}="");"";{cell}-{prev})'


def _sum_formula(block, col):
    rng = f"{col}{block['m1']}:{col}{block['m1'] + 11}"
    return f'=IF(COUNT({rng})=0;"";SUM({rng}))'


def _avg_formula(block, col):
    rng = f"{col}{block['m1']}:{col}{block['m1'] + 11}"
    return f'=IF(COUNT({rng})=0;"";ROUND(AVERAGE({rng})))'


def _last_value_formula(block, col):
    """Останнє непорожнє значення колонки (підписники на кінець року)."""
    rng = f"{col}{block['m1']}:{col}{block['m1'] + 11}"
    return f'=IFERROR(LOOKUP(2;1/({rng}<>"");{rng});"")'


def _yoy_formula(year, block, col):
    """Δ% підсумку до підсумку попереднього року (порожньо, якщо листа немає)."""
    cell = f"{col}{block['total']}"
    prev = f"INDIRECT(\"'{year - 1}'!{col}{block['total']}\")"
    return f'=IFERROR(IF({cell}="";"";({cell}-{prev})/{prev});"")'


def _yoy_abs_formula(year, block, col):
    cell = f"{col}{block['total']}"
    prev = f"INDIRECT(\"'{year - 1}'!{col}{block['total']}\")"
    return f'=IFERROR(IF({cell}="";"";{cell}-{prev});"")'


def _spark_formula(block, col, color):
    """SPARKLINE року в злитій клітинці K (масив опцій: «\\» — роздільник
    колонок у локалях з десятковою комою)."""
    rng = f"{col}{block['m1']}:{col}{block['m1'] + 11}"
    opts = ('{"charttype"\\"line";"color"\\"' + color +
            '";"linewidth"\\2;"empty"\\"ignore"}')
    return f'=IFERROR(SPARKLINE({rng};{opts});"")'


# ---------- Створення/оформлення річного листа ----------

def _ensure_locale(service):
    """Локаль/таймзона таблиці: формули нижче писані під uk_UA («;», «\\»).
    Ідемпотентно, викликається перед кожним записом — дешево."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"requests": [{"updateSpreadsheetProperties": {
            "properties": {"locale": "uk_UA", "timeZone": "Europe/Kiev"},
            "fields": "locale,timeZone",
        }}]},
    ).execute()


def _avg_pct_formula(block, col):
    """Середнє відсоткової колонки без округлення (CTR тощо)."""
    rng = f"{col}{block['m1']}:{col}{block['m1'] + 11}"
    return f'=IF(COUNT({rng})=0;"";AVERAGE({rng}))'


def _block_static_values(year, b):
    """Статика одного блоку: шапка, назви місяців, формули дельт/підсумків/
    спарклайна. Використовується і при створенні листа, і при апгрейді
    старих листів новими блоками."""
    y = str(year)
    data = [
        {"range": f"'{y}'!A{b['band']}", "values": [[b["title"]]]},
        {"range": f"'{y}'!A{b['hdr']}:K{b['hdr']}", "values": [b["headers"]]},
        {"range": f"'{y}'!A{b['m1']}:A{b['m1'] + 11}",
         "values": [[MONTHS_UA[m]] for m in range(1, 13)]},
        {"range": f"'{y}'!A{b['total']}", "values": [[f"Підсумок {year}"]]},
    ]

    # Дельти по місяцях: формула живе у formula_col, порівнює колонку data_col
    def col_formulas(formula_col, data_col, kind):
        fn = _delta_pct_formula if kind == "pct" else _delta_abs_formula
        return {"range": f"'{y}'!{formula_col}{b['m1']}:{formula_col}{b['m1'] + 11}",
                "values": [[fn(year, b, data_col, i)] for i in range(12)]}

    t = b["total"]
    key = b["key"]
    if key == "site":
        data.append(col_formulas("C", "B", "pct"))   # Δ користувачів
        data.append(col_formulas("F", "E", "pct"))   # Δ переглядів
        data.append({"range": f"'{y}'!J{b['m1']}:J{b['m1'] + 11}",  # частка Discover
                     "values": [[f'=IF(OR(I{r}="";G{r}+H{r}+I{r}=0);"";I{r}/(G{r}+H{r}+I{r}))']
                                for r in range(b["m1"], b["m1"] + 12)]})
        data.append({"range": f"'{y}'!B{t}:J{t}", "values": [[
            _sum_formula(b, "B"), _yoy_formula(year, b, "B"),
            _sum_formula(b, "D"), _sum_formula(b, "E"), _yoy_formula(year, b, "E"),
            _sum_formula(b, "G"), _sum_formula(b, "H"), _sum_formula(b, "I"),
            f'=IF(OR(I{t}="";G{t}+H{t}+I{t}=0);"";I{t}/(G{t}+H{t}+I{t}))',
        ]]})
        spark_col, spark_color = "E", FOX
    elif key == "fb":
        data.append(col_formulas("C", "B", "abs"))   # ± підписники
        data.append(col_formulas("E", "D", "pct"))   # Δ переглядів
        data.append(col_formulas("G", "F", "pct"))   # Δ взаємодій
        data.append({"range": f"'{y}'!B{t}:H{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _sum_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"), _yoy_formula(year, b, "F"),
            _sum_formula(b, "H"),
        ]]})
        spark_col, spark_color = "D", FB
    elif key == "ig":
        data.append(col_formulas("C", "B", "abs"))
        data.append(col_formulas("E", "D", "pct"))
        data.append(col_formulas("H", "G", "pct"))   # Δ взаємодій
        data.append({"range": f"'{y}'!B{t}:I{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _sum_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"),
            _sum_formula(b, "G"), _yoy_formula(year, b, "G"),
            _sum_formula(b, "I"),
        ]]})
        spark_col, spark_color = "D", IG
    elif key == "tg":
        data.append(col_formulas("C", "B", "abs"))
        data.append(col_formulas("E", "D", "pct"))   # Δ сер. охоплення
        data.append({"range": f"'{y}'!H{b['m1']}:H{b['m1'] + 11}",  # ERR
                     "values": [[f'=IF(OR(B{r}="";D{r}="");"";D{r}/B{r})']
                                for r in range(b["m1"], b["m1"] + 12)]})
        data.append({"range": f"'{y}'!B{t}:H{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _avg_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"), _sum_formula(b, "G"),
            f'=IF(OR(B{t}="";D{t}="");"";D{t}/B{t})',
        ]]})
        spark_col, spark_color = "B", TG
    elif key == "yt":
        data.append(col_formulas("C", "B", "abs"))
        data.append(col_formulas("E", "D", "pct"))   # Δ переглядів відео
        data.append({"range": f"'{y}'!B{t}:H{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _sum_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"), _sum_formula(b, "G"),
            _avg_pct_formula(b, "H"),
        ]]})
        spark_col, spark_color = "D", YT
    elif key == "tt":
        data.append(col_formulas("C", "B", "abs"))
        data.append(col_formulas("E", "D", "pct"))
        data.append({"range": f"'{y}'!B{t}:I{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _sum_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"), _sum_formula(b, "G"),
            _sum_formula(b, "H"), _sum_formula(b, "I"),
        ]]})
        spark_col, spark_color = "D", TT
    else:  # vb
        data.append(col_formulas("C", "B", "abs"))
        data.append(col_formulas("E", "D", "pct"))   # Δ активних користувачів
        data.append({"range": f"'{y}'!B{t}:F{t}", "values": [[
            _last_value_formula(b, "B"), _yoy_abs_formula(year, b, "B"),
            _avg_formula(b, "D"), _yoy_formula(year, b, "D"),
            _sum_formula(b, "F"),
        ]]})
        spark_col, spark_color = "B", VB

    data.append({"range": f"'{y}'!K{b['m1']}",
                 "values": [[_spark_formula(b, spark_col, spark_color)]]})
    return data


def _year_static_values(year, blocks=BLOCKS):
    """Всі статичні значення листа: тексти, назви місяців, формули дельт,
    підсумків і спарклайнів. Пише бот один раз при створенні листа."""
    y = str(year)
    data = [
        {"range": f"'{y}'!A1", "values": [[f"🦊 Аналітика МикВісті — {year}"]]},
        {"range": f"'{y}'!A2",
         "values": [["Веде бот: рядок місяця заповнюється 1-го числа наступного. "
                     "Підсумок року і стрілки Δ рахуються самі (формули). "
                     "YouTube/TikTok/Viber — поки вручну, до підключення API."]]},
    ]
    for b in blocks:
        data.extend(_block_static_values(year, b))
    return data


def _block_format_requests(sheet_id, blocks):
    """batchUpdate-запити оформлення блоків (шапки з кольорами мереж, формати
    чисел зі стрілками, чергування рядків, межі, злиті клітинки)."""
    req = []
    for b in blocks:
        # Кольорова шапка блоку
        req.append({"mergeCells": {"range": _grid(sheet_id, b["band"], b["band"]),
                                   "mergeType": "MERGE_ALL"}})
        req.append({"repeatCell": {"range": _grid(sheet_id, b["band"], b["band"]),
                    "cell": {"userEnteredFormat": {
                        "backgroundColorStyle": {"rgbColor": _rgb(b["color"])},
                        "textFormat": {"bold": True, "fontSize": 11,
                                       "foregroundColorStyle": {"rgbColor": _rgb("#FFFFFF")}},
                        "verticalAlignment": "MIDDLE",
                    }},
                    "fields": "userEnteredFormat(backgroundColorStyle,textFormat,verticalAlignment)"}})
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": b["band"] - 1, "endIndex": b["band"]},
            "properties": {"pixelSize": 30}, "fields": "pixelSize"}})

        # Рядок назв колонок
        req.append({"repeatCell": {"range": _grid(sheet_id, b["hdr"], b["hdr"]),
                    "cell": {"userEnteredFormat": {
                        "backgroundColorStyle": {"rgbColor": _rgb(b["tint"])},
                        "textFormat": {"bold": True, "fontSize": 9},
                        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "WRAP",
                    }},
                    "fields": "userEnteredFormat(backgroundColorStyle,textFormat,"
                              "horizontalAlignment,verticalAlignment,wrapStrategy)"}})
        req.append({"repeatCell": {"range": _grid(sheet_id, b["hdr"], b["hdr"], 0, 1),
                    "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                    "fields": "userEnteredFormat.horizontalAlignment"}})
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS",
                      "startIndex": b["hdr"] - 1, "endIndex": b["hdr"]},
            "properties": {"pixelSize": 34}, "fields": "pixelSize"}})

        # Назви місяців — напівжирні, сірі
        req.append({"repeatCell": {"range": _grid(sheet_id, b["m1"], b["m1"] + 11, 0, 1),
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True,
                                       "foregroundColorStyle": {"rgbColor": _rgb("#6B6963")}}}},
                    "fields": "userEnteredFormat.textFormat"}})

        # Формати чисел колонок (місяці + підсумок)
        for col, fmt in COL_FORMATS[b["key"]].items():
            req.append({"repeatCell": {
                "range": _grid(sheet_id, b["m1"], b["total"], col, col + 1),
                "cell": {"userEnteredFormat": {"numberFormat": fmt}},
                "fields": "userEnteredFormat.numberFormat"}})

        # Чергування рядків місяців (кожен другий — легкий тінт)
        for i in range(1, 12, 2):
            r = b["m1"] + i
            req.append({"repeatCell": {"range": _grid(sheet_id, r, r),
                        "cell": {"userEnteredFormat": {
                            "backgroundColorStyle": {"rgbColor": _rgb(ROW_TINT)}}},
                        "fields": "userEnteredFormat.backgroundColorStyle"}})

        # Спарклайн: злита клітинка K на всі 12 місяців
        req.append({"mergeCells": {"range": _grid(sheet_id, b["m1"], b["m1"] + 11, 10, 11),
                                   "mergeType": "MERGE_ALL"}})

        # Підсумок року
        req.append({"repeatCell": {"range": _grid(sheet_id, b["total"], b["total"]),
                    "cell": {"userEnteredFormat": {
                        "backgroundColorStyle": {"rgbColor": _rgb(TOTAL_BG)},
                        "textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat(backgroundColorStyle,textFormat)"}})

        # Межі блоку: зовнішня помітніша, внутрішні — тонкі
        req.append({"updateBorders": {
            "range": _grid(sheet_id, b["band"], b["total"]),
            "top": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": _rgb(BORDER_STRONG)}},
            "bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": _rgb(BORDER_STRONG)}},
            "left": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": _rgb(BORDER_STRONG)}},
            "right": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": _rgb(BORDER_STRONG)}},
            "innerHorizontal": {"style": "SOLID", "colorStyle": {"rgbColor": _rgb(BORDER_SOFT)}},
            "innerVertical": {"style": "SOLID", "colorStyle": {"rgbColor": _rgb(BORDER_SOFT)}},
        }})
    return req


def _year_format_requests(sheet_id):
    """Повне оформлення нового листа: ширини колонок, заголовок, усі блоки."""
    req = []

    # Ширини колонок: A — місяць, B..J — числа, K — тренд
    for c1, c2, px in [(0, 1, 118), (1, 10, 116), (10, 11, 150)]:
        req.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": c1, "endIndex": c2},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}})

    # Заголовок листа
    req.append({"mergeCells": {"range": _grid(sheet_id, 1, 1), "mergeType": "MERGE_ALL"}})
    req.append({"repeatCell": {"range": _grid(sheet_id, 1, 1), "cell": {"userEnteredFormat": {
        "textFormat": {"bold": True, "fontSize": 14, "foregroundColorStyle": {"rgbColor": _rgb(FOX)}},
    }}, "fields": "userEnteredFormat.textFormat"}})
    req.append({"mergeCells": {"range": _grid(sheet_id, 2, 2), "mergeType": "MERGE_ALL"}})
    req.append({"repeatCell": {"range": _grid(sheet_id, 2, 2), "cell": {"userEnteredFormat": {
        "textFormat": {"italic": True, "fontSize": 9,
                       "foregroundColorStyle": {"rgbColor": _rgb("#8A8880")}},
    }}, "fields": "userEnteredFormat.textFormat"}})

    return req + _block_format_requests(sheet_id, BLOCKS)


def _year_chart_requests(sheet_id):
    """Два вбудовані графіки праворуч від блоків. Діапазони покривають усі
    12 місяців — графіки оновлюються самі в міру заповнення рядків."""
    def src(r1, r2, c):
        return {"sources": [{"sheetId": sheet_id, "startRowIndex": r1 - 1,
                             "endRowIndex": r2, "startColumnIndex": c,
                             "endColumnIndex": c + 1}]}

    months = src(SITE["m1"], SITE["m1"] + 11, 0)
    followers_chart = {"addChart": {"chart": {
        "spec": {
            "title": "Підписники по мережах",
            "subtitle": "Facebook — синій · Instagram — рожевий · Telegram — блакитний",
            "basicChart": {
                "chartType": "LINE", "legendPosition": "NO_LEGEND", "headerCount": 0,
                "domains": [{"domain": {"sourceRange": months}}],
                "series": [
                    {"series": {"sourceRange": src(FBB["m1"], FBB["m1"] + 11, 1)},
                     "targetAxis": "LEFT_AXIS", "colorStyle": {"rgbColor": _rgb(FB)}},
                    {"series": {"sourceRange": src(IGB["m1"], IGB["m1"] + 11, 1)},
                     "targetAxis": "LEFT_AXIS", "colorStyle": {"rgbColor": _rgb(IG)}},
                    {"series": {"sourceRange": src(TGB["m1"], TGB["m1"] + 11, 1)},
                     "targetAxis": "LEFT_AXIS", "colorStyle": {"rgbColor": _rgb(TG)}},
                ],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": 2, "columnIndex": 12},
            "widthPixels": 620, "heightPixels": 360}},
    }}}
    pageviews_chart = {"addChart": {"chart": {
        "spec": {
            "title": "Перегляди сайту по місяцях",
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "NO_LEGEND", "headerCount": 0,
                "domains": [{"domain": {"sourceRange": months}}],
                "series": [
                    {"series": {"sourceRange": src(SITE["m1"], SITE["m1"] + 11, 4)},
                     "targetAxis": "LEFT_AXIS", "colorStyle": {"rgbColor": _rgb(FOX)}},
                ],
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": 21, "columnIndex": 12},
            "widthPixels": 620, "heightPixels": 360}},
    }}}
    return [followers_chart, pageviews_chart]


def _upgrade_year_sheet(service, p, year):
    """Добудова старого листа (створеного до появи блоків YouTube/TikTok/Viber,
    коли на рядках 67–69 були злиті рядки-заглушки): додати рядків до
    SHEET_ROWS, зняти злиття/формат заглушок, вписати нові блоки-каркаси.
    Дані місяців (рядки до 65) не чіпає. Ідемпотентно — маркер A68 == «Місяць»."""
    sheet_id = p["sheetId"]
    got = service.spreadsheets().values().get(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        range=f"'{year}'!A{YTB['hdr']}",
    ).execute()
    vals = got.get("values")
    if vals and vals[0] and vals[0][0] == "Місяць":
        return  # нові блоки вже на місці

    req = []
    rows = p.get("gridProperties", {}).get("rowCount", 0)
    if rows < SHEET_ROWS:
        req.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS",
                                        "length": SHEET_ROWS - rows}})
    # Старі заглушки: зняти злиття A:K і повністю зачистити значення/формат
    req.append({"unmergeCells": {"range": _grid(sheet_id, 67, 69)}})
    req.append({"updateCells": {"range": _grid(sheet_id, 67, 69),
                                "fields": "userEnteredValue,userEnteredFormat"}})
    service.spreadsheets().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID, body={"requests": req},
    ).execute()

    data = []
    for b in MANUAL_BLOCKS:
        data.extend(_block_static_values(year, b))
    data.append({"range": f"'{year}'!A2",
                 "values": [["Веде бот: рядок місяця заповнюється 1-го числа наступного. "
                             "Підсумок року і стрілки Δ рахуються самі (формули). "
                             "YouTube/TikTok/Viber — поки вручну, до підключення API."]]})
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"requests": _block_format_requests(sheet_id, MANUAL_BLOCKS)},
    ).execute()
    print(f"social_sheet: лист {year} добудовано блоками YouTube/TikTok/Viber")


def _ensure_year_sheet(service, year):
    """Лист року: якщо є — повертає sheetId (добудувавши новими блоками, якщо
    лист старої розмітки), якщо немає — створює й оформлює (статичні тексти,
    формули, формати, графіки). Викликається перед кожним записом."""
    meta = service.spreadsheets().get(
        spreadsheetId=SOCIAL_SPREADSHEET_ID, fields="sheets.properties",
    ).execute()
    props = [s["properties"] for s in meta.get("sheets", [])]
    titles = {p["title"]: p["sheetId"] for p in props}
    # Локаль тримаємо примусово завжди (не лише при створенні): щомісячний
    # HYPERLINK топ-допису теж писаний під «;-локаль»
    _ensure_locale(service)
    if str(year) in titles:
        p = next(p for p in props if p["title"] == str(year))
        _upgrade_year_sheet(service, p, year)
        return titles[str(year)]
    reply = service.spreadsheets().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {
            "title": str(year), "index": 0,
            "tabColorStyle": {"rgbColor": _rgb(FOX)},
            "gridProperties": {"rowCount": SHEET_ROWS, "columnCount": SHEET_COLS,
                               "frozenColumnCount": 1},
        }}}]},
    ).execute()
    sheet_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": _year_static_values(year)},
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=SOCIAL_SPREADSHEET_ID,
        body={"requests": _year_format_requests(sheet_id) + _year_chart_requests(sheet_id)},
    ).execute()

    # Порожній дефолтний лист нової таблиці (Sheet1/Аркуш1) більше не потрібен.
    # Видаляємо тихо: якщо там раптом є дані або він один — API/логіка не дасть.
    for junk in ("Sheet1", "Аркуш1", "Лист1"):
        if junk in titles and len(titles) >= 1:
            try:
                got = service.spreadsheets().values().get(
                    spreadsheetId=SOCIAL_SPREADSHEET_ID, range=f"'{junk}'!A1:C5",
                ).execute()
                if not got.get("values"):
                    service.spreadsheets().batchUpdate(
                        spreadsheetId=SOCIAL_SPREADSHEET_ID,
                        body={"requests": [{"deleteSheet": {"sheetId": titles[junk]}}]},
                    ).execute()
            except Exception as e:
                print(f"social_sheet: не вдалось прибрати '{junk}' — {e}")
    return sheet_id


# ---------- Збір даних за місяць ----------

def _month_bounds(year, month):
    """('YYYY-MM-01', 'YYYY-MM-DD', since_ts, until_ts) — межі місяця за Києвом;
    unix-межі для Meta API (until — виключно, перша секунда наступного місяця)."""
    last = calendar.monthrange(year, month)[1]
    start_dt = datetime(year, month, 1, tzinfo=KYIV_TZ)
    end_dt = (datetime(year, month, last, tzinfo=KYIV_TZ) + timedelta(days=1))
    return (f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}",
            int(start_dt.timestamp()), int(end_dt.timestamp()))


def _collect_site(year, month):
    """GA4 напряму (тримає всю історію, один дешевий запит) + Search Console
    тоталі по типах (історія ~16 міс; чого нема — блок пише без SC-колонок)."""
    start, end, _, _ = _month_bounds(year, month)
    client = get_ga4_client()
    users, sessions, pageviews = get_stats(client, start, end)
    sc = {}
    try:
        sc_client = analytics_store._sc_client()
        for st in analytics_store.SC_SEARCH_TYPES:
            body = {"startDate": start, "endDate": end, "type": st, "rowLimit": 1}
            try:
                resp = sc_client.searchanalytics().query(
                    siteUrl=analytics_store.SC_SITE_URL, body=body,
                ).execute()
            except Exception as e:
                print(f"social_sheet: SC type={st} за {start} пропущено — {e}")
                continue
            rows = resp.get("rows", [])
            if rows:
                sc[st] = int(round(rows[0].get("clicks", 0)))
    except Exception as e:
        print(f"social_sheet: Search Console за {start} недоступний — {e}")
    return {"users": users, "sessions": sessions, "pageviews": pageviews, "sc": sc}


FB_MONTH_METRICS = ("page_media_view", "page_post_engagements")


def _collect_facebook(year, month, with_followers):
    """Місячні суми insights (period=day, місяць вкладається у вікно ~93 днів
    Meta) + пости за місяць з топ-дописом (пагінація /posts). Підписники —
    лише живий знімок (історії Meta не має)."""
    page_id = os.environ.get("FACEBOOK_PAGE_ID")
    token = os.environ.get("FACEBOOK_PAGE_TOKEN")
    if not page_id or not token:
        raise RuntimeError("FACEBOOK_PAGE_TOKEN/FACEBOOK_PAGE_ID не задано")
    _, _, since_ts, until_ts = _month_bounds(year, month)

    out = {"followers": None, "views": None, "engagement": None,
           "posts": None, "top": None}
    if with_followers:
        from handlers.facebook import get_page_followers
        page = get_page_followers()
        out["followers"] = page.get("followers_count") or page.get("fan_count")

    for metric in FB_MONTH_METRICS:
        data = requests.get(
            f"https://graph.facebook.com/v19.0/{page_id}/insights",
            params={"metric": metric, "period": "day",
                    "since": since_ts, "until": until_ts, "access_token": token},
            timeout=30,
        ).json()
        if "error" in data:
            print(f"social_sheet: FB {metric} — {data['error'].get('message')}")
            continue
        total, seen = 0, False
        for item in data.get("data", []):
            for v in item.get("values", []):
                if isinstance(v.get("value"), (int, float)):
                    total += v["value"]
                    seen = True
        if seen:
            key = "views" if metric == "page_media_view" else "engagement"
            out[key] = int(total)

    # Пости місяця: рахуємо всі публікації сторінки, топ — за реакціями+
    # коментарями+шерами (у тижневих звітах «пости» — лише з лінком на сайт,
    # тут — усі: це число публікацій за місяць)
    url = f"https://graph.facebook.com/v19.0/{page_id}/posts"
    params = {
        "fields": "message,permalink_url,created_time,shares,"
                  "reactions.summary(true),comments.summary(true)",
        "since": since_ts, "until": until_ts, "limit": 100, "access_token": token,
    }
    count, top = 0, None
    for _ in range(12):  # до 1200 постів на місяць — з великим запасом
        data = requests.get(url, params=params, timeout=30).json()
        if "error" in data:
            print(f"social_sheet: FB /posts — {data['error'].get('message')}")
            break
        for p in data.get("data", []):
            count += 1
            eng = (p.get("reactions", {}).get("summary", {}).get("total_count", 0)
                   + p.get("comments", {}).get("summary", {}).get("total_count", 0)
                   + p.get("shares", {}).get("count", 0))
            if top is None or eng > top["engagement"]:
                top = {"engagement": eng,
                       "permalink": p.get("permalink_url", ""),
                       "message": p.get("message", "")}
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, None
    if count:
        out["posts"] = count
        out["top"] = top
    return out


def _collect_instagram(year, month, with_followers):
    """Місячні insights IG (metric_type=total_value + since/until — API віддає
    тотал за діапазон; якщо повний місяць відхилено, сумуємо дві половини) +
    кількість публікацій (пагінація /media). Підписники — лише живий знімок."""
    token = os.environ.get("INSTAGRAM_TOKEN")
    if not token:
        raise RuntimeError("INSTAGRAM_TOKEN не задано")
    from handlers.instagram import INSTAGRAM_USER_ID, get_instagram_profile
    _, _, since_ts, until_ts = _month_bounds(year, month)

    def insights_range(since, until):
        data = requests.get(
            f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/insights",
            params={"metric": "reach,views,total_interactions", "period": "day",
                    "metric_type": "total_value", "since": since, "until": until,
                    "access_token": token},
            timeout=30,
        ).json()
        if "error" in data:
            raise RuntimeError(data["error"].get("message"))
        return {item["name"]: item.get("total_value", {}).get("value", 0)
                for item in data.get("data", [])}

    out = {"followers": None, "views": None, "reach": None,
           "interactions": None, "posts": None}
    if with_followers:
        profile = get_instagram_profile()
        out["followers"] = profile.get("followers_count")

    try:
        stats = insights_range(since_ts, until_ts)
    except RuntimeError:
        # 31-денний місяць може не влізти у вікно API — дві половини.
        # reach половин сумується з невеликим завищенням (унікальність), ок.
        mid = (since_ts + until_ts) // 2
        a = insights_range(since_ts, mid)
        b = insights_range(mid, until_ts)
        stats = {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in set(a) | set(b)}
    out["views"] = stats.get("views")
    out["reach"] = stats.get("reach")
    out["interactions"] = stats.get("total_interactions")

    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_USER_ID}/media"
    params = {"fields": "id", "since": since_ts, "until": until_ts,
              "limit": 100, "access_token": token}
    count = 0
    for _ in range(10):
        data = requests.get(url, params=params, timeout=30).json()
        if "error" in data:
            print(f"social_sheet: IG /media — {data['error'].get('message')}")
            break
        count += len(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, None
    if count:
        out["posts"] = count
    return out


_TG_SUBS_RE = re.compile(r"([\d\s ]+)\s*(?:subscribers|підписник)")


def _tg_subscribers():
    """Підписники каналу з веб-прев'ю t.me/{channel} («41 012 subscribers»);
    фолбек — округлений лічильник зі стрічки /s («41K»)."""
    try:
        html = tg_stats._fetch_html(f"/{tg_stats.CHANNEL}")
        soup = BeautifulSoup(html, "html.parser")
        extra = soup.find("div", class_="tgme_page_extra")
        if extra:
            m = _TG_SUBS_RE.search(extra.get_text(" ", strip=True))
            if m:
                return int(re.sub(r"\D", "", m.group(1)))
    except Exception as e:
        print(f"social_sheet: прев'ю t.me/{tg_stats.CHANNEL} — {e}")
    html = tg_stats._fetch_html(f"/s/{tg_stats.CHANNEL}")
    soup = BeautifulSoup(html, "html.parser")
    for counter in soup.find_all("div", class_="tgme_channel_info_counter"):
        type_span = counter.find("span", class_="counter_type")
        value_span = counter.find("span", class_="counter_value")
        if type_span and value_span and "subscriber" in type_span.get_text().lower():
            return tg_stats._parse_views_text(value_span.get_text(strip=True))
    return None


def _collect_telegram(year, month, with_followers):
    """Пости місяця зі стрічки t.me/s: старт — оцінка message_id початку
    наступного місяця по якорях (+ запас), гортаємо вниз до виходу за початок
    місяця. Перегляди — поточні накопичені (t.me округлює: 12.3K), для
    минулого місяця це майже фінальні числа. Підписники — лише живий знімок."""
    m_start = datetime(year, month, 1, tzinfo=KYIV_TZ)
    last = calendar.monthrange(year, month)[1]
    m_end = datetime(year, month, last, tzinfo=KYIV_TZ) + timedelta(days=1)

    out = {"subscribers": None, "posts": None, "views_total": None, "avg_views": None}
    if with_followers:
        out["subscribers"] = _tg_subscribers()

    before = tg_stats.estimate_message_id(m_end) + 250  # запас на похибку інтерполяції
    posts, views_sum, views_n = 0, 0, 0
    for _ in range(80):  # ~1600 постів — з запасом на найактивніший місяць
        html = tg_stats._fetch_html(f"/s/{tg_stats.CHANNEL}",
                                    params={"before": before})
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            break
        page_min_id, oldest_dt = None, None
        for block in blocks:
            msg_id, views, _hrefs, dt = tg_stats._parse_message_block(block)
            if msg_id is not None and (page_min_id is None or msg_id < page_min_id):
                page_min_id = msg_id
            if dt is None:
                continue
            if oldest_dt is None or dt < oldest_dt:
                oldest_dt = dt
            if m_start <= dt < m_end:
                posts += 1
                if views is not None:
                    views_sum += views
                    views_n += 1
        if page_min_id is None or page_min_id <= 1:
            break
        if oldest_dt is not None and oldest_dt < m_start:
            break
        before = page_min_id
        time.sleep(0.3)

    if posts:
        out["posts"] = posts
        out["views_total"] = views_sum if views_n else None
        out["avg_views"] = round(views_sum / views_n) if views_n else None
    return out


# ---------- Запис місяця в лист ----------

def _hyperlink(url, label):
    label = (label or "").replace('"', "'").strip()
    return f'=HYPERLINK("{url}";"{label}")'


def _month_value_ranges(year, month, site, fb, ig, tg):
    """values.batchUpdate-діапазони рядка місяця. Пишемо ЛИШЕ сирі числа у
    «свої» клітинки (дельти/частки/спарклайни — формули, їх не чіпаємо);
    блок без даних пропускається повністю (не затираємо існуюче)."""
    y = str(year)
    data = []

    def row(block):
        return block["m1"] + month - 1

    if site:
        r = row(SITE)
        data.append({"range": f"'{y}'!B{r}", "values": [[site["users"]]]})
        data.append({"range": f"'{y}'!D{r}:E{r}",
                     "values": [[site["sessions"], site["pageviews"]]]})
        sc = site.get("sc") or {}
        if sc:
            data.append({"range": f"'{y}'!G{r}:I{r}", "values": [[
                sc.get("web", 0), sc.get("googleNews", 0), sc.get("discover", 0)]]})
    if fb:
        r = row(FBB)
        if fb.get("followers") is not None:
            data.append({"range": f"'{y}'!B{r}", "values": [[fb["followers"]]]})
        if fb.get("views") is not None:
            data.append({"range": f"'{y}'!D{r}", "values": [[fb["views"]]]})
        if fb.get("engagement") is not None:
            data.append({"range": f"'{y}'!F{r}", "values": [[fb["engagement"]]]})
        if fb.get("posts") is not None:
            data.append({"range": f"'{y}'!H{r}", "values": [[fb["posts"]]]})
        top = fb.get("top")
        if top and top.get("permalink"):
            words = " ".join((top.get("message") or "").split()[:4]) or "допис"
            label = f"🔥 {top['engagement']} · {words}…"
            data.append({"range": f"'{y}'!I{r}",
                         "values": [[_hyperlink(top["permalink"], label)]]})
    if ig:
        r = row(IGB)
        if ig.get("followers") is not None:
            data.append({"range": f"'{y}'!B{r}", "values": [[ig["followers"]]]})
        if ig.get("views") is not None:
            data.append({"range": f"'{y}'!D{r}", "values": [[ig["views"]]]})
        # окремими клітинками: None у values.batchUpdate затер би сусідню
        if ig.get("reach") is not None:
            data.append({"range": f"'{y}'!F{r}", "values": [[ig["reach"]]]})
        if ig.get("interactions") is not None:
            data.append({"range": f"'{y}'!G{r}", "values": [[ig["interactions"]]]})
        if ig.get("posts") is not None:
            data.append({"range": f"'{y}'!I{r}", "values": [[ig["posts"]]]})
    if tg:
        r = row(TGB)
        if tg.get("subscribers") is not None:
            data.append({"range": f"'{y}'!B{r}", "values": [[tg["subscribers"]]]})
        if tg.get("avg_views") is not None:
            data.append({"range": f"'{y}'!D{r}", "values": [[tg["avg_views"]]]})
        if tg.get("posts") is not None:
            data.append({"range": f"'{y}'!F{r}", "values": [[tg["posts"]]]})
        if tg.get("views_total") is not None:
            data.append({"range": f"'{y}'!G{r}", "values": [[tg["views_total"]]]})
    return data


BLOCK_LABELS = {"site": "🌐 Сайт", "fb": "📘 Facebook", "ig": "📷 Instagram",
                "tg": "✈️ Telegram"}


async def capture_month(year, month, blocks=("site", "fb", "ig", "tg"),
                        with_followers=True):
    """Знімок одного місяця: збирає джерела, гарантує лист року, пише рядок.
    Повертає {block: "✅ …"/"⛔ помилка"} — часткові збої не валять решту."""
    service = await asyncio.to_thread(_get_sheets_service)
    await asyncio.to_thread(_ensure_year_sheet, service, year)

    results = {}
    site = fb = ig = tg = None
    if "site" in blocks:
        try:
            site = await asyncio.to_thread(_collect_site, year, month)
            sc_note = "+SC" if site.get("sc") else "без SC"
            results["site"] = f"✅ {site['pageviews']:,} переглядів ({sc_note})".replace(",", " ")
        except Exception as e:
            results["site"] = f"⛔ {e}"
    if "fb" in blocks:
        try:
            fb = await asyncio.to_thread(_collect_facebook, year, month, with_followers)
            if not any(v is not None for v in fb.values()):
                fb, results["fb"] = None, "⛔ Meta нічого не віддала за цей місяць"
            else:
                results["fb"] = f"✅ перегляди {fb.get('views')}, пости {fb.get('posts')}"
        except Exception as e:
            results["fb"] = f"⛔ {e}"
    if "ig" in blocks:
        try:
            ig = await asyncio.to_thread(_collect_instagram, year, month, with_followers)
            if not any(v is not None for v in ig.values()):
                ig, results["ig"] = None, "⛔ Meta нічого не віддала за цей місяць"
            else:
                results["ig"] = f"✅ перегляди {ig.get('views')}, пости {ig.get('posts')}"
        except Exception as e:
            results["ig"] = f"⛔ {e}"
    if "tg" in blocks:
        try:
            tg = await asyncio.to_thread(_collect_telegram, year, month, with_followers)
            if not any(v is not None for v in tg.values()):
                tg, results["tg"] = None, "⛔ стрічка t.me не віддала постів місяця"
            else:
                results["tg"] = (f"✅ пости {tg.get('posts')}, "
                                 f"сер. охоплення {tg.get('avg_views')}")
        except Exception as e:
            results["tg"] = f"⛔ {e}"

    data = _month_value_ranges(year, month, site, fb, ig, tg)
    if data:
        await asyncio.to_thread(
            lambda: service.spreadsheets().values().batchUpdate(
                spreadsheetId=SOCIAL_SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
        )
    return results


def _prev_month(today=None):
    today = today or datetime.now(KYIV_TZ)
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return prev.year, prev.month


def _results_text(year, month, results):
    lines = [f"🦊 Таблиця аналітики: {MONTHS_UA[month]} {year}"]
    lines += [f"{BLOCK_LABELS[k]}: {v}" for k, v in results.items()]
    lines.append(SPREADSHEET_URL)
    return "\n".join(lines)


async def run_monthly_snapshot(bot):
    """Автозадача 1-го числа: знімок попереднього місяця в таблицю, короткий
    звіт Олегу в приват (раз на місяць — не шум)."""
    year, month = _prev_month()
    try:
        results = await capture_month(year, month)
        await bot.send_message(chat_id=ADMIN_CHAT_ID,
                               text=_results_text(year, month, results),
                               disable_web_page_preview=True)
    except Exception as e:
        print(f"social_sheet: місячний знімок упав — {e}")
        await notify_error(bot, "місячний знімок таблиці аналітики", e)


# ---------- Команди ----------

async def sheet_snapshot_handler(update, context):
    """/sheet_snapshot [YYYY-MM] — знімок місяця в таблицю вручну (дефолт —
    попередній місяць). Ідемпотентно: рядок місяця просто перезаписується."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    year, month = _prev_month()
    if context.args:
        m = re.fullmatch(r"(\d{4})-(\d{2})", context.args[0])
        if not m:
            await update.message.reply_text("Формат: /sheet_snapshot 2026-06")
            return
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            await update.message.reply_text("Місяць 01–12.")
            return
    now = datetime.now(KYIV_TZ)
    current = (year == now.year and month == now.month)
    msg = await update.message.reply_text(
        f"🦊 Знімаю {MONTHS_UA[month]} {year} у таблицю…"
        + ("\n⚠️ Місяць ще не скінчився — числа неповні, перезапишуться 1-го числа." if current else "")
    )
    try:
        results = await capture_month(year, month, with_followers=(current or (year, month) == _prev_month(now)))
        await msg.edit_text(_results_text(year, month, results),
                            disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")


async def sheet_backfill_handler(update, context):
    """/sheet_backfill [місяців] — залити історію в таблицю (дефолт 36,
    від давніх до свіжих). Сайт — уся глибина (GA4), Search Console ~16 міс,
    FB/IG — скільки віддасть Meta (~2 роки), Telegram — по стрічці t.me
    (перегляди — поточні накопичені). Підписники заднім числом недоступні —
    колонки «Підписники» бекфіл не чіпає."""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    months = 36
    if context.args:
        try:
            months = min(max(1, int(context.args[0])), 120)
        except ValueError:
            pass
    year, month = _prev_month()
    todo = []
    y, m = year, month
    for _ in range(months):
        todo.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    todo.reverse()

    msg = await update.message.reply_text(
        f"🦊 Бекфіл {months} міс ({todo[0][0]}-{todo[0][1]:02d} → "
        f"{todo[-1][0]}-{todo[-1][1]:02d})… Це кілька хвилин: Meta пагінація "
        f"+ гортання стрічки t.me по місяцях."
    )
    ok, partial, failed = 0, 0, 0
    for i, (y, m) in enumerate(todo, 1):
        try:
            results = await capture_month(y, m, with_followers=False)
            bad = sum(1 for v in results.values() if v.startswith("⛔"))
            if bad == 0:
                ok += 1
            elif bad < len(results):
                partial += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"social_sheet: бекфіл {y}-{m:02d} упав — {e}")
        if i % 6 == 0 or i == len(todo):
            try:
                await msg.edit_text(
                    f"🦊 Бекфіл: {i}/{len(todo)} міс "
                    f"(повних {ok}, часткових {partial}, порожніх {failed})…"
                )
            except Exception:
                pass  # текст не змінився — Telegram таке не любить
    await msg.edit_text(
        f"✅ Бекфіл готовий: {len(todo)} міс — повних {ok}, часткових {partial}, "
        f"порожніх {failed}.\nЧасткові/порожні — це нормально для давніх місяців: "
        f"SC тримає ~16 міс, Meta ~2 роки, підписники заднім числом недоступні "
        f"ніде (переносяться зі старої таблиці окремо).\n{SPREADSHEET_URL}"
    )
