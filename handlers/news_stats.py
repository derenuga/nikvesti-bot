"""
Підрахунки по архіву новин напряму з БД сайту (nodes) — джерело істини.

Відповідає на «скільки?»-питання редакції через NLQ-tool count_news:
скільки матеріалів вийшло за місяць, скільки власних (own_material), скільки
по кожній рубриці, скільки написав конкретний автор, скільки англійською/
українською/російською версією. На відміну від нори (дзеркало в Postgres, лише
ua/ru, лаг до години), тут читаємо nodes напряму: завжди свіжо, включно з
англійською локалізацією, якої в норі немає.

Чому COUNT напряму в production-БД — це ок (на відміну від важких LIKE по
longtext, заради яких і будували нору): це рідкі агрегатні запити (людина
питає бота), по одній таблиці з фільтром type/status/published — дешево,
далеко в межах лімітів KEY4 (10000 запитів/год).

Схема (розвідано 03.07, docs/BUILDER_MONITOR_MODULE.md):
- nodes: type='news', status=1 (опубл.), published (unix, буває в майбутньому —
  гейтимо <=now), own_material (1=власний), category (слаг), region (код),
  owner_id → users.id (автор; колонка author порожня, НЕ використовувати),
  title_ua/title (заголовок ua/ru), content_ua/content (тіло ua/ru).
- Мовні версії — по НАЯВНОСТІ непорожнього тіла: ua=content_ua, ru=content
  (без суфікса = рос.), en=content_en (назву EN-колонки визначаємо інтроспекцією
  SHOW COLUMNS, бо в репозиторії її явно не бачили — не гадаємо).
- users: id, first_name/last_name (+_ru/_en), username (логін, не імʼя).

EN-застереження: рахуємо матеріали з непорожнім EN-тілом за датою публікації
САМОГО вузла (окремої дати публікації перекладу в схемі немає). Якщо переклад
залили пізніше за оригінал — він все одно рахується в місяці публікації оригіналу.
"""

from datetime import datetime

from handlers import db

# Кеш інтроспекції колонок (одна перевірка на процес): SHOW COLUMNS дешевий,
# але і його зайвий раз ганяти нема потреби.
_columns_cache = {}


def _table_columns(table):
    """Множина назв колонок таблиці (кешовано). Порожня множина при помилці —
    тоді покладаємось на базові, точно наявні колонки."""
    if table in _columns_cache:
        return _columns_cache[table]
    try:
        rows = db.query(f"SHOW COLUMNS FROM {table}")
        cols = {r.get("Field") for r in rows if r.get("Field")}
    except Exception as e:
        print(f"news_stats: не вдалось прочитати колонки {table} — {e}")
        cols = set()
    _columns_cache[table] = cols
    return cols


def _en_content_column():
    """Назва EN-колонки тіла в nodes (content_en / text_en / …) або None.
    Визначаємо інтроспекцією, щоб не гадати назву й не писати парсер наосліп."""
    cols = _table_columns("nodes")
    for cand in ("content_en", "text_en", "body_en", "fulltext_en"):
        if cand in cols:
            return cand
    # Останній шанс: будь-яка колонка, схожа на англійське тіло.
    for c in cols:
        cl = c.lower()
        if cl.endswith("_en") and any(k in cl for k in ("content", "text", "body")):
            return c
    return None


def _views_column():
    """Назва колонки лічильника переглядів у nodes (hits/views/…), або None.
    Визначаємо інтроспекцією — точну назву в репозиторії ніде не використовують,
    тож не гадаємо: пробуємо однозначні кандидати за пріоритетом. 'rating' та
    подібні НЕ беремо — вони можуть означати редакційну оцінку, а не перегляди."""
    cols = {c.lower(): c for c in _table_columns("nodes")}
    for cand in ("counter", "views", "hits", "view_count", "count_views", "count_view",
                 "viewed", "count_show", "show_count", "shows", "read_count"):
        if cand in cols:
            return cols[cand]
    return None


# Непорожнє тіло певної мови: ua=content_ua, ru=content (без суфікса = рос.).
_LANG_BASE_COLUMN = {"ua": "content_ua", "ru": "content"}


def _nonempty(col):
    return f"(n.{col} IS NOT NULL AND n.{col} <> '')"


def _lang_condition(language):
    """SQL-умова наявності тексту мовної версії, або ('ERROR', msg)."""
    if language in _LANG_BASE_COLUMN:
        return _nonempty(_LANG_BASE_COLUMN[language]), None
    if language == "en":
        col = _en_content_column()
        if not col:
            return None, (
                "EN-колонку тіла в nodes не знайдено (SHOW COLUMNS не показав "
                "content_en/text_en). Або англійські локалізації зберігаються "
                "інакше, або їх немає. Перевір: /dbquery DESCRIBE nodes"
            )
        return f"(n.{col} IS NOT NULL AND n.{col} <> '')", None
    return None, f"Невідома мова '{language}'. Доступно: ua, ru, en."


def _author_name_columns():
    """Наявні колонки імен у users для матчингу автора (ua/ru/en варіанти)."""
    cols = _table_columns("users")
    cands = [
        "first_name", "last_name", "middle_name",
        "first_name_ua", "last_name_ua", "first_name_ru", "last_name_ru",
        "first_name_en", "last_name_en",
    ]
    present = [c for c in cands if c in cols]
    # Фолбек на базові, якщо інтроспекція нічого не дала.
    return present or ["first_name", "last_name"]


def _period_conds(year=None, month=None, year_from=None, year_to=None):
    """Умови періоду по published (unix) + params. year+month → місяць;
    year → рік; інакше year_from/year_to; порожньо → вся історія."""
    conds, params = [], []
    if year and month:
        y, m = int(year), int(month)
        start = int(datetime(y, m, 1).timestamp())
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = int(datetime(ny, nm, 1).timestamp())
        conds += ["n.published >= %s", "n.published < %s"]
        params += [start, end]
        return conds, params
    if year:
        y = int(year)
        conds += ["n.published >= %s", "n.published < %s"]
        params += [int(datetime(y, 1, 1).timestamp()),
                   int(datetime(y + 1, 1, 1).timestamp())]
        return conds, params
    if year_from:
        conds.append("n.published >= %s")
        params.append(int(datetime(int(year_from), 1, 1).timestamp()))
    if year_to:
        conds.append("n.published < %s")
        params.append(int(datetime(int(year_to) + 1, 1, 1).timestamp()))
    return conds, params


# Вісь розбивки → (SQL-вираз групування, чи хронологічне сортування).
def _group_expr(group_by):
    if group_by == "category":
        return "COALESCE(NULLIF(n.category, ''), '—')", False
    if group_by == "region":
        return "COALESCE(CAST(n.region AS CHAR), '—')", False
    if group_by == "year":
        return "YEAR(FROM_UNIXTIME(n.published))", True
    if group_by == "month":
        return "DATE_FORMAT(FROM_UNIXTIME(n.published), '%Y-%m')", True
    if group_by == "own_material":
        return "CASE WHEN n.own_material = 1 THEN 'власні' ELSE 'рерайт/агентські' END", False
    return None, False


_NOTE = (
    "Джерело — БД сайту (nodes) напряму: опубліковані (status=1), тип news, "
    "published<=зараз. Свіже, включно з англійською версією. Автор — за owner_id "
    "(колонка author на сайті порожня). УВАГА: own_material проставлений лише з "
    "певного року — старіші власні матеріали можуть недораховуватись. Мова "
    "рахується по наявності непорожнього тіла версії (ua/ru/en); один матеріал "
    "може мати кілька версій, тож суми по мовах можуть перевищувати загальну."
)


_VIEWS_NOTE = (
    "metric=views: сумуємо ВЛАСНИЙ лічильник переглядів сайту (nodes) — це "
    "накопичена за весь час кількість переглядів матеріалу, НЕ GA4 і НЕ за період "
    "(період фільтрує лише за датою публікації матеріалу, самі перегляди — "
    "довічні). Для питання 'хто з журналістів набрав більше переглядів' — саме те. "
    "У breakdown: views (сума переглядів) і materials (скільки матеріалів). "
    "Автор — за owner_id (колонка author порожня)."
)


def count_news(title_contains=None, year=None, month=None, year_from=None, year_to=None,
               own_material=None, category=None, region=None, author=None,
               language=None, group_by=None, metric="count"):
    """Підрахунок опублікованих матеріалів у nodes за фільтрами. metric='count'
    (кількість матеріалів, дефолт) або 'views' (сума переглядів з лічильника
    сайту). Одне число або розбивка (group_by). Див. докстрінг модуля і опис
    NLQ-tool count_news."""
    if not db.is_configured():
        return {"error": "БД сайту не налаштована (DB_* env) — підрахунок недоступний."}

    if metric not in ("count", "views"):
        return {"error": f"Невідома метрика '{metric}'. Доступно: count, views."}

    valid_groups = {"category", "author", "year", "month", "own_material", "language", "region"}
    if group_by and group_by not in valid_groups:
        return {"error": f"Невідома вісь group_by '{group_by}'. Доступно: {', '.join(sorted(valid_groups))}."}
    if metric == "views" and group_by == "language":
        return {"error": "Розбивка по мові підтримується лише для metric=count."}

    now = int(datetime.now().timestamp())
    conds = ["n.type = 'news'", "n.status = 1", "n.published > 0", "n.published <= %s"]
    params = [now]

    pconds, pparams = _period_conds(year, month, year_from, year_to)
    conds += pconds
    params += pparams

    if own_material:
        conds.append("n.own_material = 1")
    if category:
        conds.append("n.category = %s")
        params.append(str(category))
    if region is not None:
        conds.append("n.region = %s")
        params.append(int(region))
    if title_contains:
        conds.append("(n.title_ua LIKE %s OR n.title LIKE %s)")
        like = f"%{title_contains}%"
        params += [like, like]
    if language:
        cond, err = _lang_condition(language)
        if err:
            return {"error": err}
        conds.append(cond)
    if author:
        name_cols = _author_name_columns()
        like = f"%{author.strip()}%"
        ors = " OR ".join(f"u.{c} LIKE %s" for c in name_cols)
        conds.append(f"n.owner_id IN (SELECT u.id FROM users u WHERE {ors})")
        params += [like] * len(name_cols)

    where = " AND ".join(conds)

    try:
        if metric == "views":
            vcol = _views_column()
            if not vcol:
                return {"error": (
                    "Колонку переглядів у nodes не знайдено (не видно "
                    "views/hits/view_count…). Перевір назву: /dbquery DESCRIBE nodes"
                )}
            vsum = f"SUM(COALESCE(n.{vcol}, 0))"
            if group_by == "author":
                sql = (
                    "SELECT n.owner_id AS oid, "
                    "TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS name, "
                    f"COUNT(*) AS materials, {vsum} AS views "
                    "FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
                    f"WHERE {where} GROUP BY n.owner_id ORDER BY views DESC LIMIT 60"
                )
                rows = db.query(sql, tuple(params))
                breakdown = [
                    {"key": (r["name"].strip() if r.get("name") and r["name"].strip()
                             else f"id {r['oid']}"),
                     "owner_id": r["oid"], "views": int(r["views"] or 0),
                     "materials": int(r["materials"])}
                    for r in rows
                ]
            elif group_by:
                gexpr, chrono = _group_expr(group_by)
                order = "grp ASC" if chrono else "views DESC, grp ASC"
                sql = (f"SELECT {gexpr} AS grp, COUNT(*) AS materials, {vsum} AS views "
                       f"FROM nodes n WHERE {where} GROUP BY grp ORDER BY {order} LIMIT 60")
                rows = db.query(sql, tuple(params))
                breakdown = [{"key": r["grp"], "views": int(r["views"] or 0),
                              "materials": int(r["materials"])} for r in rows]
            else:
                r = db.query(
                    f"SELECT COUNT(*) AS materials, {vsum} AS views FROM nodes n WHERE {where}",
                    tuple(params),
                )[0]
                return {"total_views": int(r["views"] or 0), "materials": int(r["materials"]),
                        "metric": "views", "note": _VIEWS_NOTE}
            return {"total_views": sum(b["views"] for b in breakdown),
                    "materials": sum(b["materials"] for b in breakdown),
                    "metric": "views", "group_by": group_by,
                    "breakdown": breakdown, "note": _VIEWS_NOTE}

        if group_by == "language":
            en_col = _en_content_column()
            en_expr = (f"SUM({_nonempty(en_col)})" if en_col else "NULL")
            sql = (
                "SELECT COUNT(*) AS total, "
                f"SUM({_nonempty('content_ua')}) AS ua, "
                f"SUM({_nonempty('content')}) AS ru, "
                f"{en_expr} AS en "
                f"FROM nodes n WHERE {where}"
            )
            r = db.query(sql, tuple(params))[0]
            breakdown = [
                {"key": "українською", "count": int(r["ua"] or 0)},
                {"key": "російською", "count": int(r["ru"] or 0)},
                {"key": "англійською", "count": (int(r["en"]) if r["en"] is not None else None)},
            ]
            return {"total": int(r["total"] or 0), "group_by": "language",
                    "breakdown": breakdown, "note": _NOTE}

        if group_by == "author":
            sql = (
                "SELECT n.owner_id AS oid, "
                "TRIM(CONCAT(COALESCE(u.first_name,''), ' ', COALESCE(u.last_name,''))) AS name, "
                "COUNT(*) AS c FROM nodes n LEFT JOIN users u ON u.id = n.owner_id "
                f"WHERE {where} GROUP BY n.owner_id ORDER BY c DESC LIMIT 60"
            )
            rows = db.query(sql, tuple(params))
            breakdown = [
                {"key": (r["name"].strip() if r.get("name") and r["name"].strip()
                         else f"id {r['oid']}"),
                 "owner_id": r["oid"], "count": int(r["c"])}
                for r in rows
            ]
            return {"total": sum(b["count"] for b in breakdown), "group_by": "author",
                    "breakdown": breakdown, "note": _NOTE}

        if group_by:
            gexpr, chrono = _group_expr(group_by)
            order = "grp ASC" if chrono else "c DESC, grp ASC"
            sql = (f"SELECT {gexpr} AS grp, COUNT(*) AS c FROM nodes n "
                   f"WHERE {where} GROUP BY grp ORDER BY {order} LIMIT 60")
            rows = db.query(sql, tuple(params))
            breakdown = [{"key": r["grp"], "count": int(r["c"])} for r in rows]
            return {"total": sum(b["count"] for b in breakdown), "group_by": group_by,
                    "breakdown": breakdown, "note": _NOTE}

        sql = f"SELECT COUNT(*) AS c FROM nodes n WHERE {where}"
        total = int(db.query(sql, tuple(params))[0]["c"])
        return {"total": total, "note": _NOTE}
    except Exception as e:
        return {"error": f"Підрахунок по БД сайту не вдався: {e}"}
