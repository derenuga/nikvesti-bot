"""
Повнотекстовий пошук по дзеркалу архіву (хвиля A, ARCHIVE_INTELLIGENCE.md).

Працює поверх Postgres бота (bot_db.articles, generated tsvector-колонка fts):
- шукає по заголовках ua/ru І ПОВНОМУ ТЕКСТУ (LIKE в news_archive бачить
  тільки заголовки);
- морфологія без стемера: кожне слово запиту → префіксний лексем (основ:*),
  довгі слова автоматично обрізаються на 1-2 символи закінчення — "стадіону"
  знайде і "стадіон", і "стадіоном";
- стратифікація по роках (spread_years) — для "історії питання" беремо
  топ-N з КОЖНОГО року, а не 10 найсвіжіших: історія не тоне під свіжаком.

Результати кладуться в ту саму пам'ять пошуку, що й news_archive.search_news
(storage.news_search) → під відповіддю Лиса працюють ті самі кнопки відбору
і "Написати бек", а get_news_leads знаходить ліди по цих же id.
"""

from datetime import datetime
import re

from handlers import bot_db, news_archive

BASE_URL = "https://nikvesti.com"

SEARCH_LIMIT_MAX = 30
EXCERPT_CHARS = 900


# ---------- tsquery ----------

_WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)


def _stem(word):
    """Груба "основа" слова для префіксного пошуку: у довгих слів відкидаємо
    відмінкове закінчення. Recall важливіший за precision — ранжування
    ts_rank все одно підніме точні збіги вгору."""
    w = word.strip("'’-")
    # Тири підібрані під українські закінчення: у прикметників вони довгі
    # ("Центрального" → основа "Центральн" — мінус 3), в іменників коротші.
    # 6-7-літерні слова ріжемо лише на 1 (було 2): "Синюха" → "Синюх", а не
    # "Синю" — інакше префікс ловив і Синютку, і Синюка, і "синю сукню"
    # (реальний кейс 20.07: пошук про річку Синюху видав Стівена Кінга).
    if len(w) >= 8:
        return w[:-3]
    if len(w) >= 5:
        return w[:-1]
    return w


def _build_tsquery(text):
    """Рядок для to_tsquery('simple', …): 'основ:* & друг:*'.
    Повертає None, якщо в запиті немає жодного слова."""
    words = _WORD_RE.findall(text or "")
    lexemes = []
    for w in words:
        s = _stem(w)
        if len(s) < 2:
            continue
        # Лапки всередині лексеми ламають синтаксис tsquery — прибираємо
        s = s.replace("'", "").replace("’", "")
        if s:
            lexemes.append(f"{s}:*")
    if not lexemes:
        return None
    return " & ".join(lexemes)


# ---------- Пошук ----------

def _fmt_item(n, row):
    title = (row.get("title_ua") or row.get("title_ru") or "").strip()
    slug = (row.get("slug") or "").strip()
    category = (row.get("category") or "").strip()
    # Старі матеріали бувають без слага — тоді хвіст URL це id. Рубрику
    # (category) все одно вставляємо: /news/politics/269222, а не /news/269222,
    # інакше двіжок редиректить і в беку плодяться голі лінки без рубрики.
    tail = slug or str(row["id"])
    if category:
        url = f"{BASE_URL}/news/{category}/{tail}"
    else:
        url = f"{BASE_URL}/news/{tail}"
    published = int(row["published"]) if row.get("published") else 0
    date = datetime.fromtimestamp(published).strftime("%d.%m.%Y") if published else "—"
    item = {"n": n, "id": row["id"], "published": published, "date": date, "title": title, "url": url}
    if "own_material" in row:
        item["own"] = bool(row.get("own_material"))
    return item


def _filter_conditions(own_material=None, category=None, region=None, tag=None):
    """Спільні SQL-умови фільтрів (own_material/category/region/tag) + params.
    Кожна умова написана для аліаса таблиці `a`."""
    conds, params = [], []
    if own_material:
        conds.append("a.own_material = 1")
    if category:
        conds.append("a.category = %s")
        params.append(str(category))
    if region is not None:
        conds.append("a.region = %s")
        params.append(int(region))
    if tag:
        # Точний тег (без урахування регістру) по канонічній назві ua/ru.
        conds.append(
            "EXISTS (SELECT 1 FROM article_tags at JOIN tags t ON t.id = at.tag_id "
            "WHERE at.article_id = a.id AND (t.name_ua ILIKE %s OR t.name_ru ILIKE %s))"
        )
        params.extend([tag, tag])
    return conds, params


def search_items(query, limit=10, year_from=None, year_to=None,
                 spread_years=False, per_year=3,
                 own_material=None, category=None, region=None, tag=None):
    """Ядро пошуку: повертає list[dict] (n/id/date/title/url/own) без побічних
    ефектів. Використовується і NLQ-tool'ом (з пам'яттю), і /dossier (без).
    Фільтри: own_material (тільки власні), category (слаг), region (код), tag (назва)."""
    tsquery = _build_tsquery(query)
    if not tsquery:
        return []
    limit = min(int(limit or 10), SEARCH_LIMIT_MAX)
    now = int(datetime.now().timestamp())

    conds = ["a.fts @@ q.query", "a.status = 1", "a.published > 0", "a.published <= %s"]
    params = [tsquery, now]
    if year_from:
        conds.append("a.published >= %s")
        params.append(int(datetime(int(year_from), 1, 1).timestamp()))
    if year_to:
        conds.append("a.published < %s")
        params.append(int(datetime(int(year_to) + 1, 1, 1).timestamp()))
    fconds, fparams = _filter_conditions(own_material, category, region, tag)
    conds += fconds
    params += fparams
    where = " AND ".join(conds)

    if spread_years:
        sql = f"""
            WITH q AS (SELECT to_tsquery('simple', %s) AS query),
            ranked AS (
                SELECT a.id, a.published, a.title_ua, a.title_ru, a.slug, a.category, a.own_material,
                       ts_rank(a.fts, q.query) AS rank,
                       EXTRACT(YEAR FROM to_timestamp(a.published))::int AS yr,
                       ROW_NUMBER() OVER (
                           PARTITION BY EXTRACT(YEAR FROM to_timestamp(a.published))
                           ORDER BY ts_rank(a.fts, q.query) DESC, a.published DESC
                       ) AS rn
                FROM articles a, q
                WHERE {where}
            )
            SELECT id, published, title_ua, title_ru, slug, category, own_material
            FROM ranked WHERE rn <= %s
            ORDER BY yr ASC, rank DESC
            LIMIT %s
        """
        params.extend([max(1, int(per_year or 3)), limit])
    else:
        sql = f"""
            WITH q AS (SELECT to_tsquery('simple', %s) AS query)
            SELECT a.id, a.published, a.title_ua, a.title_ru, a.slug, a.category, a.own_material
            FROM articles a, q
            WHERE {where}
            ORDER BY ts_rank(a.fts, q.query) DESC, a.published DESC
            LIMIT %s
        """
        params.append(limit)

    rows = bot_db.query(sql, tuple(params))
    # Релевантністю (ts_rank) вибираємо, ЯКІ новини показати (топ-N), але
    # користувачу віддаємо їх хронологічно — найсвіжіше зверху. Інакше дати
    # в списку скачуть (05.07 після 21.06 після 23.03…), і номери кнопок не
    # читаються як стрічка. spread_years має власний таймлайн по роках
    # (yr ASC) — його не чіпаємо.
    if not spread_years:
        rows = sorted(rows, key=lambda r: int(r["published"] or 0), reverse=True)
    return [_fmt_item(i, row) for i, row in enumerate(rows, start=1)]


def search_archive(dialog_key, query, limit=10, year_from=None, year_to=None,
                   spread_years=False, per_year=3,
                   own_material=None, category=None, region=None, tag=None,
                   turn_id=None):
    """Повнотекстовий пошук по «лисячій норі» (заголовки + теги + текст, 17 років) —
    tool для NLQ-роутера.

    query — фраза або ключові слова (бажано базові форми, але закінчення
    прощаються — див. _stem). spread_years=True — режим "історія питання":
    до per_year результатів з кожного року, від давніх до свіжих.
    Фільтри: own_material — тільки власні матеріали; category — слаг рубрики;
    region — код регіону; tag — точна назва тегу.

    turn_id — маркер одного NLQ-запиту: кілька пошуків у межах запиту
    (напр. «що писали про A та B» — окремо по A і по B) зливаються в ОДИН
    список з наскрізною нумерацією, відсортований свіжіше→давніше (дублі по
    id відкидаються). Інакше другий пошук затирав перший, і кнопки (одна
    нумерація) не збігалися з двома списками в тексті.
    Результати запам'ятовуються для кнопок відбору і беку
    (та сама пам'ять, що в search_news_archive)."""
    if not bot_db.is_configured():
        return {"error": (
            "Лисяча нора ще не налаштована (BOT_DATABASE_URL). "
            "Скористайся search_news_archive (пошук по заголовках напряму в БД сайту)."
        )}
    try:
        items = search_items(
            query, limit=limit, year_from=year_from, year_to=year_to,
            spread_years=spread_years, per_year=per_year,
            own_material=own_material, category=category, region=region, tag=tag,
        )
    except Exception as e:
        return {"error": f"Пошук по норі не вдався: {e}"}
    if not items and not _build_tsquery(query):
        return {"error": "Порожній пошуковий запит."}

    # Той самий NLQ-запит уже шукав (turn_id) — доклеюємо до наявного списку
    # з наскрізною нумерацією, а не затираємо. spread_years має власний
    # таймлайн по роках (yr ASC) — його не мержимо, щоб не ламати порядок.
    prev_items = []
    if turn_id and not spread_years:
        entry = news_archive._get_entry(dialog_key)
        if entry and entry.get("turn_id") == turn_id:
            prev_items = entry["items"]
    seen_ids = {it["id"] for it in prev_items}
    all_items = prev_items + [it for it in items if it["id"] not in seen_ids]
    if prev_items:
        all_items.sort(key=lambda it: it.get("published", 0), reverse=True)
        for i, it in enumerate(all_items, start=1):
            it["n"] = i
    news_archive.remember_results(dialog_key, all_items, turn_id=turn_id)
    note = (
        "Повнотекстовий пошук по «лисячій норі» — архіву nikvesti.com "
        "(заголовки + теги + текст, зважене ранжування, вся історія). "
        + ("Режим історії питання: до кількох результатів з кожного року, від давніх до свіжих. "
           if spread_years else "")
        + "У items — ПОВНИЙ накопичений список цього запиту (кілька пошуків одного "
          "запиту зливаються в один список з наскрізною нумерацією). Показуй усі "
          "items рівно під цими номерами n, ОДНИМ наскрізним списком. "
        + "Якщо результатів мало — спробуй синоніми або російське написання (старі матеріали російською)."
    )
    return {"query": query, "found": len(items), "note": note, "items": all_items}


_MONTHS_UK = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер",
              "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]


def count_by_month(query, year_from=None, year_to=None,
                   own_material=None, category=None, region=None, tag=None):
    """Кількість новин за запитом, згрупована по роках і місяцях — АГРЕГАТ
    (COUNT(*) GROUP BY), а не перелік. Тому без капу на 30: рахує весь збіг.

    Для питань «скільки новин про X по місяцях», «динаміка згадувань»,
    «порівняй роки». Повертає готові дані під render_chart: labels — 12
    коротких назв місяців, series — по ряду на КОЖЕН рік (counts: 12 значень
    Січ→Гру, total: за рік). Клод малює порівняння років, не перелічуючи новини
    текстом (саме перелік раніше переповнював ліміт відповіді)."""
    if not bot_db.is_configured():
        return {"error": (
            "Лисяча нора ще не налаштована (BOT_DATABASE_URL) — підрахунок недоступний."
        )}
    tsquery = _build_tsquery(query)
    if not tsquery:
        return {"error": "Порожній пошуковий запит."}
    now = int(datetime.now().timestamp())

    conds = ["a.fts @@ q.query", "a.status = 1", "a.published > 0", "a.published <= %s"]
    params = [tsquery, now]
    if year_from:
        conds.append("a.published >= %s")
        params.append(int(datetime(int(year_from), 1, 1).timestamp()))
    if year_to:
        conds.append("a.published < %s")
        params.append(int(datetime(int(year_to) + 1, 1, 1).timestamp()))
    fconds, fparams = _filter_conditions(own_material, category, region, tag)
    conds += fconds
    params += fparams
    where = " AND ".join(conds)

    sql = f"""
        WITH q AS (SELECT to_tsquery('simple', %s) AS query)
        SELECT EXTRACT(YEAR FROM to_timestamp(a.published))::int AS yr,
               EXTRACT(MONTH FROM to_timestamp(a.published))::int AS mo,
               count(*) AS c
        FROM articles a, q
        WHERE {where}
        GROUP BY yr, mo
        ORDER BY yr, mo
    """
    try:
        rows = bot_db.query(sql, tuple(params))
    except Exception as e:
        return {"error": f"Підрахунок по норі не вдався: {e}"}

    counts = {}
    years_seen = set()
    for r in rows:
        yr, mo, c = int(r["yr"]), int(r["mo"]), int(r["c"])
        counts[(yr, mo)] = c
        years_seen.add(yr)

    if year_from and year_to:
        years = list(range(int(year_from), int(year_to) + 1))
    elif year_from:
        top = max(years_seen) if years_seen else int(year_from)
        years = list(range(int(year_from), top + 1))
    else:
        years = sorted(years_seen)

    now_dt = datetime.now()
    cur_year, cur_month = now_dt.year, now_dt.month

    series = []
    for yr in years:
        monthly = []
        for m in range(1, 13):
            # Майбутні місяці (ще не настали) — null, а не 0: інакше на графіку
            # лінія поточного року падала б у нуль до кінця року. render_chart
            # малює null як розрив (лінія обривається на поточному місяці).
            if yr > cur_year or (yr == cur_year and m > cur_month):
                monthly.append(None)
            else:
                monthly.append(counts.get((yr, m), 0))
        series.append({
            "year": yr,
            "counts": monthly,
            "total": sum(c for c in monthly if c is not None),
        })

    return {
        "query": query,
        "labels": _MONTHS_UK,
        "series": series,
        "note": (
            "Кількість новин за запитом по місяцях (агрегат по дзеркалу архіву, "
            "весь збіг, без обмеження на 30). У series — по ряду на кожен рік "
            "(counts: 12 значень Січ→Гру, total: за рік). УВАГА: майбутні місяці "
            "поточного року — null (ще не настали); передавай counts у render_chart "
            "ЯК Є разом з null, НЕ заміняй null на 0 — інакше лінія року впаде в нуль "
            "до грудня. Для графіка: render_chart з labels і series=[{name:'2026', "
            "values: counts, color:'#2e6ee8'}, {name:'2025', values: counts, "
            "color:'#e8402e'}]. Новини текстом НЕ перелічуй."
        ),
    }


def get_excerpts(ids, max_chars=EXCERPT_CHARS):
    """Початки текстів статей з дзеркала (для складання досьє).
    ids — список node id. Повертає list[dict] з excerpt."""
    if not ids:
        return []
    rows = bot_db.query(
        "SELECT id, published, title_ua, title_ru, slug, category, "
        "left(coalesce(text_ua, text_ru), %s) AS excerpt "
        "FROM articles WHERE id = ANY(%s)",
        (int(max_chars), [int(i) for i in ids]),
    )
    by_id = {r["id"]: r for r in rows}
    result = []
    for i in ids:
        row = by_id.get(int(i))
        if not row:
            continue
        item = _fmt_item(0, row)
        item.pop("n", None)
        item["excerpt"] = (row.get("excerpt") or "").strip() or "(текст відсутній)"
        result.append(item)
    return result


def count_articles():
    """Скільки статей у дзеркалі (0 — дзеркало порожнє/не налаштоване)."""
    if not bot_db.is_configured():
        return 0
    try:
        return bot_db.query("SELECT count(*) AS c FROM articles")[0]["c"]
    except Exception:
        return 0
