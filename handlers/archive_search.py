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
    if len(w) >= 8:
        return w[:-3]
    if len(w) >= 6:
        return w[:-2]
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
    url = f"{BASE_URL}/news/{slug}" if slug else f"{BASE_URL}/news/{row['id']}"
    date = datetime.fromtimestamp(int(row["published"])).strftime("%d.%m.%Y") if row.get("published") else "—"
    return {"n": n, "id": row["id"], "date": date, "title": title, "url": url}


def search_items(query, limit=10, year_from=None, year_to=None,
                 spread_years=False, per_year=3):
    """Ядро пошуку: повертає list[dict] (n/id/date/title/url) без побічних
    ефектів. Використовується і NLQ-tool'ом (з пам'яттю), і /dossier (без)."""
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
    where = " AND ".join(conds)

    if spread_years:
        sql = f"""
            WITH q AS (SELECT to_tsquery('simple', %s) AS query),
            ranked AS (
                SELECT a.id, a.published, a.title_ua, a.title_ru, a.slug,
                       ts_rank(a.fts, q.query) AS rank,
                       EXTRACT(YEAR FROM to_timestamp(a.published))::int AS yr,
                       ROW_NUMBER() OVER (
                           PARTITION BY EXTRACT(YEAR FROM to_timestamp(a.published))
                           ORDER BY ts_rank(a.fts, q.query) DESC, a.published DESC
                       ) AS rn
                FROM articles a, q
                WHERE {where}
            )
            SELECT id, published, title_ua, title_ru, slug
            FROM ranked WHERE rn <= %s
            ORDER BY yr ASC, rank DESC
            LIMIT %s
        """
        params.extend([max(1, int(per_year or 3)), limit])
    else:
        sql = f"""
            WITH q AS (SELECT to_tsquery('simple', %s) AS query)
            SELECT a.id, a.published, a.title_ua, a.title_ru, a.slug
            FROM articles a, q
            WHERE {where}
            ORDER BY ts_rank(a.fts, q.query) DESC, a.published DESC
            LIMIT %s
        """
        params.append(limit)

    rows = bot_db.query(sql, tuple(params))
    return [_fmt_item(i, row) for i, row in enumerate(rows, start=1)]


def search_archive(dialog_key, query, limit=10, year_from=None, year_to=None,
                   spread_years=False, per_year=3):
    """Повнотекстовий пошук по дзеркалу архіву (заголовки + текст, 17 років) —
    tool для NLQ-роутера.

    query — фраза або ключові слова (бажано базові форми, але закінчення
    прощаються — див. _stem). spread_years=True — режим "історія питання":
    до per_year результатів з кожного року, від давніх до свіжих.
    Результати запам'ятовуються для кнопок відбору і беку
    (та сама пам'ять, що в search_news_archive)."""
    if not bot_db.is_configured():
        return {"error": (
            "Дзеркало архіву ще не налаштоване (BOT_DATABASE_URL). "
            "Скористайся search_news_archive (пошук по заголовках напряму в БД сайту)."
        )}
    try:
        items = search_items(query, limit=limit, year_from=year_from,
                             year_to=year_to, spread_years=spread_years, per_year=per_year)
    except Exception as e:
        return {"error": f"Пошук по дзеркалу не вдався: {e}"}
    if not items and not _build_tsquery(query):
        return {"error": "Порожній пошуковий запит."}
    news_archive.remember_results(dialog_key, items)
    note = (
        "Повнотекстовий пошук по дзеркалу архіву (заголовки + текст, вся історія). "
        + ("Режим історії питання: до кількох результатів з кожного року, від давніх до свіжих. "
           if spread_years else "")
        + "Якщо результатів мало — спробуй синоніми або російське написання (старі матеріали російською)."
    )
    return {"query": query, "found": len(items), "note": note, "items": items}


def get_excerpts(ids, max_chars=EXCERPT_CHARS):
    """Початки текстів статей з дзеркала (для складання досьє).
    ids — список node id. Повертає list[dict] з excerpt."""
    if not ids:
        return []
    rows = bot_db.query(
        "SELECT id, published, title_ua, title_ru, slug, "
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
