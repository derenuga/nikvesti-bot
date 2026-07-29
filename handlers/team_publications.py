"""
«Мої публікації» для Mini App «Команда».

Олег, 29.07: «у нас уже є команда /stat у бота… дати можливість журналістці в
своєму інтерфейсі бачити кнопку "Мої публікації"».

Що це і чого НЕ це:

- це перелік того, що вийшло під її іменем за період, із власним лічильником
  переглядів сайту. Джерело — `nodes` БД сайту, той самий, що рахує факт KPI,
  тож перелік і кружечок норми не можуть розійтися;
- це НЕ /stat. /stat ходить у Facebook, Instagram, TikTok, YouTube і GA4 по
  кожному матеріалу — секунди й зовнішні ліміти на КОЖЕН рядок. Для екрана,
  який відкривають щодня, це неприйнятно, тож соцмереж тут немає взагалі.
  Знімки /stat у Норі (article_stats) лежать по одному матеріалу на запит, і
  сорок запитів на відкриття екрана — та сама пастка з іншого боку; коли
  дійде черга, це буде один пакетний SELECT, а не цикл.

Лічильник переглядів у `nodes` — накопичувальний за весь час, а не за період.
Для свіжих матеріалів це майже те саме, для старих — ні, тому в інтерфейсі він
підписаний «переглядів на сайті», без слова «за місяць». Чесніше показати
всечасне число з правильним підписом, ніж вигадати періодне з GA4 по 40
сторінках на кожне відкриття екрана.
"""

from datetime import datetime

from handlers import db, team_kpi
from handlers.news_stats import KYIV_TZ, _views_column

MAX_ITEMS = 60


def _period_ts(offset=0):
    """Межі місяця зі зсувом (0 — поточний), у unix-секундах Києва."""
    return team_kpi._period_ts_range("month", offset)


def my_publications(person, offset=0, limit=MAX_ITEMS):
    """Перелік публікацій людини за місяць (offset — зсув назад).

    Повертає {"items": [...], "total": N, "views": N, "own": N, "label": …}
    або {"error": …}. Помилка — це рядок для екрана, а не виняток: БД сайту
    лягає регулярно, і екран має сказати про це людською мовою."""
    if not db.is_configured():
        return {"error": "БД сайту недоступна — перелік тимчасово не зібрати."}
    uid = team_kpi.resolve_site_user_id(person)
    if not uid:
        return {"error": "Не знайшли твій акаунт на сайті — скажи Олегу, це лікується."}

    start_ts, end_ts = _period_ts(offset)
    now = int(datetime.now().timestamp())
    vcol = _views_column()
    vsel = f", n.{vcol} AS views" if vcol else ""

    from handlers.news_archive import _news_url

    try:
        rows = db.query(
            "SELECT n.id, n.published, n.title_ua, n.title, n.slug_ua, n.slug, "
            f"n.category, n.own_material, n.type{vsel} "
            "FROM nodes n WHERE n.owner_id = %s AND n.status = 1 "
            "AND n.published >= %s AND n.published < %s AND n.published <= %s "
            f"ORDER BY n.published DESC LIMIT {int(limit)}",
            (uid, start_ts, end_ts, now),
        )
    except Exception as e:
        return {"error": f"БД сайту не відповіла: {e}"}

    items = []
    for r in rows:
        dt = datetime.fromtimestamp(int(r["published"]), KYIV_TZ)
        items.append({
            "id": r["id"],
            "title": (r.get("title_ua") or r.get("title") or "").strip(),
            "url": _news_url(r),
            "date": dt.strftime("%d.%m"),
            "time": dt.strftime("%H:%M"),
            "ts": int(r["published"]),
            "own": int(r.get("own_material") or 0) == 1,
            "kind": r.get("type") or "news",
            "views": int(r.get("views") or 0) if vcol else None,
        })

    views = sum(i["views"] or 0 for i in items) if vcol else None
    return {
        "items": items,
        "total": len(items),
        "own": sum(1 for i in items if i["own"]),
        "articles": sum(1 for i in items if i["kind"] == "article"),
        "views": views,
        "capped": len(items) >= limit,
        "label": team_kpi.period_label("month", offset),
    }
