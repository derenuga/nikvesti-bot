"""
Тест місячної пам'яті соцмереж: імпорт із Google-таблиці + запис знімка (Нора).

Живих БД і Google API не треба: підміняємо читання таблиці та `bot_db`.
Стереже саме те, що на очі не перевіриш — ЗБІГ КОЛОНОК таблиці з метриками
Нори. Помилка на одну колонку тут не падає, а тихо кладе «взаємодії» в
«перегляди», і потім місяцями вводить в оману.

Що стереже:
- розкладка колонок кожного блоку (fb/ig/tg/yt/tt/vb) читається з тих самих
  констант social_sheet, і жодна метрика не з'їжджає на сусідню;
- місяць визначається позицією рядка в блоці (m1 + m − 1);
- порожній місяць не осідає в Норі порожнім рядком;
- «#N/A», текст і порожня клітинка — це «немає даних», а не нуль;
- специфічні метрики (години перегляду YouTube, лайки TikTok, сер. охоплення
  Telegram) лягають в extra, а не губляться;
- місячний знімок (record_month) розкладає результати збирачів по тих самих
  колонках, що імпорт із таблиці — тобто джерела не розійдуться.

Запуск:
    python test_social_import.py
"""

import asyncio
import json
import sys

from handlers import social_import, social_sheet, social_store

FAILS = []


def check(name, cond, extra=""):
    print(f"{'✅' if cond else '❌'} {name}{'' if cond else ' — ' + str(extra)}")
    if not cond:
        FAILS.append(name)


def _row_dict(row):
    """Кортеж upsert-рядка → зрозумілий словник."""
    platform, month, followers, reach, views, engagement, posts, extra, source = row
    return {"platform": platform, "month": month, "followers": followers,
            "reach": reach, "views": views, "engagement": engagement,
            "posts": posts, "extra": json.loads(extra) if extra else {},
            "source": source}


def test_column_map():
    """Кожна колонка з COLUMN_MAP має існувати в headers блоку, а індекс —
    відповідати назві. Це страховка від зсуву, коли в таблиці додасться колонка."""
    by_key = {b["key"]: b for b in social_sheet.BLOCKS}
    expect = {
        ("fb", "followers"): "Підписники", ("fb", "views"): "Перегляди",
        ("fb", "engagement"): "Взаємодії", ("fb", "posts"): "Пости",
        ("ig", "views"): "Перегляди", ("ig", "reach"): "Охоплення",
        ("ig", "engagement"): "Взаємодії", ("ig", "posts"): "Пости",
        ("tg", "views"): "Перегляди за місяць", ("tg", "posts"): "Пости",
        ("yt", "views"): "Перегляди відео",
        ("tt", "views"): "Перегляди відео", ("tt", "reach"): "Охоплення",
        ("vb", "posts"): "Постів у канал",
    }
    ok = True
    for (key, metric), header in expect.items():
        idx = social_import.COLUMN_MAP[key][metric]
        actual = by_key[key]["headers"][idx]
        if actual != header:
            ok = False
            print(f"   {key}.{metric} → колонка {idx} = «{actual}», а мало бути «{header}»")
    check("колонки таблиці збігаються з метриками Нори", ok)
    # Специфічні метрики — в extra, з правильних колонок
    check("години перегляду YouTube — з колонки «Час перегляду, год»",
          by_key["yt"]["headers"][social_import.COLUMN_MAP["yt"]["extra"]["watch_hours"]]
          == "Час перегляду, год")
    check("сер. охоплення поста Telegram — в extra",
          by_key["tg"]["headers"][social_import.COLUMN_MAP["tg"]["extra"]["avg_views"]]
          == "Сер. охоплення поста")
    check("лайки TikTok — в extra",
          by_key["tt"]["headers"][social_import.COLUMN_MAP["tt"]["extra"]["likes"]]
          == "Вподобайки")


def test_num():
    check("порожня клітинка — немає даних", social_import._num("") is None)
    check("помилка формули — немає даних", social_import._num("#N/A") is None)
    check("текст — немає даних", social_import._num("—") is None)
    check("число з пробілами-розрядами читається",
          social_import._num("41 012") == 41012, social_import._num("41 012"))
    check("дробове округляється", social_import._num(1234.6) == 1235)
    check("нуль трактуємо як «не знімали», а не як нуль",
          social_import._num(0) is None)


def test_rows_for_year():
    # Рядки блоку: по 12 на місяць, колонки A..K. Заповнюємо лютий і березень.
    def block_rows(values_by_month):
        rows = []
        for m in range(1, 13):
            rows.append(values_by_month.get(m, [""] * 11))
        return rows

    fb_feb = ["Лютий", 39000, "", 500000, "", 12000, "", 210, "", "", ""]
    tg_mar = ["Березень", 41000, "", 5200, "", 300, 1500000, "", "", "", ""]
    yt_mar = ["Березень", 3100, "", 42000, "", 260.5, "", "", "", "", ""]
    tt_mar = ["Березень", 8200, "", 90000, "", "", 3000, 400, 120, "", ""]
    by_block = {
        "fb": block_rows({2: fb_feb}),
        "tg": block_rows({3: tg_mar}),
        "yt": block_rows({3: yt_mar}),
        "tt": block_rows({3: tt_mar}),
    }
    rows = [_row_dict(r) for r in social_import._rows_for_year(2026, by_block)]
    check("порожні місяці не осідають у Норі", len(rows) == 4, len(rows))
    fb = next(r for r in rows if r["platform"] == "facebook")
    check("місяць — за позицією рядка в блоці", fb["month"] == "2026-02-01", fb)
    check("FB: перегляди й взаємодії не переплутані",
          (fb["views"], fb["engagement"], fb["posts"]) == (500000, 12000, 210), fb)
    check("джерело помічено як таблиця", fb["source"] == "sheet", fb)
    tg = next(r for r in rows if r["platform"] == "telegram")
    check("TG: перегляди за місяць, а не сер. охоплення поста",
          tg["views"] == 1500000 and tg["extra"]["avg_views"] == 5200, tg)
    check("TG: пости на місці", tg["posts"] == 300, tg)
    yt = next(r for r in rows if r["platform"] == "youtube")
    # 260.5 → 260: round() у Python банкірський, і для годин перегляду
    # (довідкова метрика в extra) втрата половини години нічого не значить
    check("YouTube: години перегляду — в extra",
          yt["views"] == 42000 and yt["extra"]["watch_hours"] == 260, yt)
    tt = next(r for r in rows if r["platform"] == "tiktok")
    check("TikTok: лайки/поширення/коментарі — в extra",
          tt["extra"] == {"likes": 3000, "shares": 400, "comments": 120}, tt["extra"])
    check("TikTok: охоплення порожнє (API його не дає)", tt["reach"] is None, tt)


def test_record_month():
    """Живий знімок місяця має розкластись у ті самі колонки, що імпорт."""
    saved = []
    social_store.bot_db.upsert_social_monthly = lambda rows: saved.extend(rows) or len(rows)
    social_store.bot_db.is_configured = lambda: True
    blocks = {
        "fb": {"followers": 41500, "views": 520000, "engagement": 13000, "posts": 220},
        "ig": {"followers": 20100, "views": 300000, "reach": 250000,
               "interactions": 9000, "posts": 90},
        "tg": {"subscribers": 41012, "posts": 300, "views_total": 1500000,
               "avg_views": 5000},
        "tt": {"followers": 8200, "views": 90000, "likes": 3000, "shares": 400,
               "comments": 120},
        "yt": {"followers": 3100, "views": 42000, "watch_hours": 260},
        "vb": {"subscribers": 1500, "posts": 280},
    }
    asyncio.run(social_store.record_month(2026, 7, blocks))
    rows = {r[0]: _row_dict(r) for r in saved}
    check("усі шість мереж записались", len(rows) == 6, list(rows))
    check("місяць — перше число", rows["facebook"]["month"] == "2026-07-01", rows["facebook"])
    check("IG: interactions → engagement, reach окремо",
          (rows["instagram"]["engagement"], rows["instagram"]["reach"]) == (9000, 250000),
          rows["instagram"])
    check("TG: subscribers → followers, views_total → views",
          (rows["telegram"]["followers"], rows["telegram"]["views"]) == (41012, 1500000),
          rows["telegram"])
    check("TikTok: взаємодії = лайки + коментарі + поширення",
          rows["tiktok"]["engagement"] == 3520, rows["tiktok"])
    check("YouTube: години перегляду в extra",
          rows["youtube"]["extra"]["watch_hours"] == 260, rows["youtube"])
    check("Viber: підписники й пости, решти немає",
          (rows["viber"]["followers"], rows["viber"]["posts"]) == (1500, 280),
          rows["viber"])
    check("живий знімок помічений як api", rows["facebook"]["source"] == "api",
          rows["facebook"])


if __name__ == "__main__":
    test_column_map()
    test_num()
    test_rows_for_year()
    test_record_month()
    print()
    if FAILS:
        print(f"❌ Провалено {len(FAILS)}: {', '.join(FAILS)}")
        sys.exit(1)
    print("✅ Усе зелене")
