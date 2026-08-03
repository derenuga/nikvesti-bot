"""
Банк тем для Mini App «Команда» — дані екрана (docs/PROMISES_BANK.md §6).

Чому окремий модуль, а не ще один шматок `handlers/promises.py`: там усе
зверстано під Telegram (HTML-теги, ліміт повідомлення, `/promise_show 81`
рядком). Апці потрібні ті самі факти, але СТРУКТУРОЮ — інакше JS довелося б
розбирати рядки, зверстані для чату.

Формулювання при цьому лишаються ТУТ, а не в JS: «строк минув 2 місяці тому»
рахує `pp.human_gap` з українським відмінюванням, і другої, майже такої самої
реалізації в браузері бути не повинно — вона розійдеться з першою тижнів за
три.

Екран народився з першого місяця на обсязі: 473 обіцянки після чистки, і
чергою в чаті по вісім штук їх не переглянути. Тому три речі, яких у
`/promises` немає й не буде:

- **фасети з числами** одразу видно, скільки чого (а не «показано 8 з 473»);
- **картка з ланцюгом** — уся історія питання з лінком на кожен факт;
- **дублі окремим списком**: 473 — завищене число, бо суддя ланцюга спрацював
  63 рази на 762 статті, і та сама обіцянка з двох статей лягла двома
  записами. Скорочувати банк починають звідси, а не з видалення живих тем.
"""

import time

import entity_pipeline as ep
import promise_pipeline as pp
from handlers import team_notifications

# Скільки карток віддаємо за раз. Гортання нескінченне (offset), але перша
# порція має малюватись миттєво — на телефоні 30 карток це вже довгий екран.
PAGE = 30
QUOTE_CAP = 260

FACETS = [
    ("all", "Усе"),
    ("overdue", "Строк минув"),
    ("soon", "Скоро"),
    ("waiting", "Чекає події"),
    ("stale", "Давно не питали"),
    ("noproof", "Перевірити нічим"),
    ("populism", "Популізм"),
]


def _conn():
    conn = ep.connect()
    conn.autocommit = True
    return conn


def _state(row, now):
    """Підпис стану — той самий текст, що в чаті, але без емодзі: в апці клас
    кодує смуга кольору, і емодзі поруч із нею була б другим носієм того
    самого сигналу."""
    cls = row["class"]
    if cls == "overdue":
        return f"Строк минув {pp.human_gap(now - int(row['deadline']))} тому"
    if cls == "soon":
        return f"Строк за {pp.human_gap(int(row['deadline']) - now)}"
    if cls == "waiting":
        return "Чекає події"
    if cls == "stale":
        silence = max(row.get("checked_at") or 0, row.get("last_seen") or 0)
        return f"Не питали {pp.human_gap(now - silence)}"
    if cls == "noproof":
        return "Перевірити нічим"
    if cls == "closed":
        return pp.STATE_WORD.get(row.get("status"), "закрито")
    if row.get("deadline"):
        return f"Строк {pp.fmt_date(row['deadline'])}"
    return "Строку не дали"


def _amount(amount):
    if not amount:
        return None
    if amount >= 1_000_000:
        s = f"{amount / 1_000_000:.1f}".replace(".", ",").rstrip("0").rstrip(",")
        return f"{s} млн грн"
    if amount >= 1000:
        return f"{amount / 1000:.0f} тис грн"
    return f"{amount:.0f} грн"


def _meta(row):
    """Нижній рядок картки. Порядок сталий: строк · чим будиться · скільки
    ревізій · гроші · кому обіцяно."""
    meta = []
    if row.get("deadline"):
        meta.append(f"Строк {pp.fmt_date(row['deadline'])}")
    if row.get("trigger_event"):
        meta.append(f"Розбудить: {row['trigger_event'][:70]}")
    elif row.get("polarity") == "not_do":
        meta.append("Перевіряється дією")
    n = row.get("revisions") or 0
    if n:
        meta.append(f"{n} {pp.plural(n, 'ревізія', 'ревізії', 'ревізій')}")
    money = _amount(row.get("amount"))
    if money:
        meta.append(money)
    if row.get("audience") == "media":
        meta.append("Обіцяно журналістам")
    if row.get("condition"):
        meta.append("Умовна — не прострочується")
    return meta


def _who(row, rev=None):
    """Хто обіцяв — структурою, а не реченням: у макеті ім'я жирне, роль ні,
    і склеєний рядок довелося б різати регуляркою в браузері."""
    rev = rev or {}
    return {
        "promiser": rev.get("promiser_text") or row.get("owner_text"),
        "role": rev.get("promiser_role"),
        "reported": rev.get("reported_by_text"),
        "owner": (row.get("owner_text")
                  if row.get("owner_text") != (rev.get("promiser_text")
                                               or row.get("owner_text"))
                  else None),
        "hidden": bool(row.get("actor_hidden")) and not (
            rev.get("promiser_text") or row.get("owner_text")),
    }


def _item(row, rev, now):
    quote = (rev or {}).get("quote") or ""
    return {
        "id": row["id"],
        "cls": row["class"],
        "state": _state(row, now),
        "how": pp.METHOD_WORD.get(row.get("verification_method")),
        # «Піти й подивитись» підсвічене зеленим: дешева перевірка ПІДІЙМАЄ
        # тему, а не відсіює її (§2.3) — це не декор, а те саме правило, що
        # в сортуванні черги.
        "cheap": row.get("verification_method") == "field_check",
        "title": row.get("title") or "—",
        "who": _who(row, rev),
        "quote": quote if len(quote) <= QUOTE_CAP else quote[:QUOTE_CAP] + "…",
        "populism": row.get("populism"),
        "meta": _meta(row),
        "topic_id": row.get("topic_id"),
    }


def _first_revisions(cur, ids):
    """Перша ревізія кожної обіцянки — вона несе цитату й обіцяльника."""
    if not ids:
        return {}
    revs = pp.revisions(cur, list(ids))
    out = {}
    for r in revs:
        out.setdefault(r["commitment_id"], r)
    return out


def queue(cls=None, q=None, offset=0, limit=PAGE, now=None):
    """Черга банку: фасети з числами + сторінка карток + межі даних."""
    now = now or int(time.time())
    conn = _conn()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            matched = []
            if q and len(q.strip()) > 3:
                # Пошук іде через сутнісний шар (аліаси + афіліація посадовців
                # з канону ролей), тому це не «фільтр списку», а окремий
                # запит — і фасети рахуються вже по знайденому.
                rows, matched = pp.search(cur, q, limit=200)
            else:
                rows = pp.list_queue(cur, limit=None, now=now)
            counts = pp.facet_counts(rows)
            counts["populism"] = sum(1 for r in rows if r.get("populism"))
            counts["all"] = len(rows)
            if cls == "populism":
                rows = [r for r in rows if r.get("populism")]
            elif cls and cls != "all":
                rows = [r for r in rows if r["class"] == cls]
            total = len(rows)
            page = rows[offset:offset + limit]
            revs = _first_revisions(cur, [r["id"] for r in page])
            items = [_item(r, revs.get(r["id"]), now) for r in page]
            bounds = pp.data_bounds(cur)
            dupes = pp.dupe_count(cur)
    finally:
        conn.close()
    return {
        "items": items, "total": total, "offset": offset,
        "facets": [{"key": k, "label": lbl, "n": counts.get(k, 0)}
                   for k, lbl in FACETS],
        "bounds": bounds, "dupes": dupes, "query": q or "",
        "matched": [{"name": m["name"], "kind": m["kind"]} for m in matched],
    }


# ---------- Картка: історія питання ----------

def _step_kind(rev, prev):
    """Крок ланцюга: тверде зобов'язання · зрив/перенос · решта.

    «Зрив» ставиться не за словами, а за ФАКТОМ: строк у цій ревізії пізніший
    за попередній. Саме перенос і є подія, заради якої банк існує, і бачити
    його треба в ланцюгу, а не вираховувати очима з двох дат.
    """
    a = rev.get("stated_deadline")
    b = (prev or {}).get("stated_deadline")
    if a and b and int(a) > int(b):
        return "broken"
    if rev.get("modality") in ("guaranteed", "promised") or \
            rev.get("source_type") in ("tender", "government_decision"):
        return "firm"
    return ""


def card(commitment_id):
    """Одна обіцянка з усім ланцюгом і сусідами по темі."""
    conn = _conn()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            row = pp.get(cur, int(commitment_id))
            if not row:
                return None
            siblings = pp.topic_commitments(cur, row["topic_id"], exclude=row["id"])
            revs = pp.revisions(cur, [row["id"]])
            from handlers.promises import _links_for
            links = _links_for(cur, {r["article_id"] for r in revs})
            now = int(time.time())
            steps, prev = [], None
            for r in revs:
                link = links.get(r["article_id"]) or {}
                steps.append({
                    "when": pp.fmt_date(r.get("published")),
                    "kind": _step_kind(r, prev),
                    "modality": pp.MODALITY_WORD.get(r.get("modality"), ""),
                    "source": pp.SOURCE_WORD.get(r.get("source_type"), ""),
                    "quote": r.get("quote"),
                    "deadline": (pp.fmt_date(r["stated_deadline"])
                                 if r.get("stated_deadline") else None),
                    "promiser": r.get("promiser_text"),
                    "url": link.get("url"),
                    "article_title": link.get("title"),
                })
                prev = r
            head = _item(row, revs[0] if revs else None, now)
            head["tags"] = _tags(row, now)
            head["criterion"] = row.get("criterion")
            head["based_on"] = row.get("based_on_document")
            head["condition"] = row.get("condition")
            head["checked_at"] = row.get("checked_at")
            return {
                "commitment": head, "chain": steps,
                "siblings": [{"id": s["id"], "title": s["title"]}
                             for s in siblings],
                "bounds": pp.data_bounds(cur),
            }
    finally:
        conn.close()


def _tags(row, now):
    tags = [{"text": _state(row, now), "danger": row["class"] == "overdue"}]
    money = _amount(row.get("amount"))
    if money:
        tags.append({"text": money})
    how = pp.METHOD_WORD.get(row.get("verification_method"))
    if how:
        tags.append({"text": how.capitalize()})
    n = row.get("revisions") or 0
    if n > 1:
        tags.append({"text": f"{n} {pp.plural(n, 'ревізія', 'ревізії', 'ревізій')}"})
    return tags


# ---------- Дії ----------

def check(commitment_id, who):
    """«Перевірили»: тема йде вниз черги, але з банку не зникає — обіцянку
    можна перевірити й не писати про неї (§6)."""
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            pp.mark_checked(cur, int(commitment_id), who)
            ok = cur.rowcount > 0
        conn.commit()
        return ok
    finally:
        conn.close()


def drop(commitment_id, who, reason=None):
    """«Не наша тема» — зі знімком у журнал, як /promise_forget."""
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            result = pp.forget(cur, int(commitment_id), reason=reason, who=who)
        conn.commit()
        return result
    finally:
        conn.close()


def dupes(limit=40):
    """Список пар-кандидатів + повні картки обох, щоб екран показував ЦИТАТИ.
    Без цитат злиття вслiпу: дві схожі назви бувають у двох сусідніх
    дитсадків, і відрізняє їх саме текст обіцянки."""
    conn = _conn()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            pairs = pp.dupe_pairs(cur, limit=limit)
            ids = {p["a"] for p in pairs} | {p["b"] for p in pairs}
            if not ids:
                return {"pairs": [], "total": 0}
            cur.execute(
                f"SELECT {pp.COMMITMENT_COLS} FROM commitments c "
                "WHERE c.id = ANY(%s)", (list(ids),))
            rows = {r["id"]: r for r in pp._decorate(pp._rows(cur))}
            revs = _first_revisions(cur, ids)
            now = int(time.time())
            out = []
            for p in pairs:
                a, b = rows.get(p["a"]), rows.get(p["b"])
                if not a or not b:
                    continue
                out.append({
                    "sim": p["sim"],
                    "a": _item(a, revs.get(a["id"]), now),
                    "b": _item(b, revs.get(b["id"]), now),
                })
            return {"pairs": out, "total": pp.dupe_count(cur)}
    finally:
        conn.close()


def not_dupe(a, b, who):
    """«Різні» — рішення людини, яке пам'ятається назавжди.

    Потрібне тому, що детектор працює на сигналах, а не на знанні: у пари
    «меморіальний комплекс на Центральному кладовищі» / «…у Корабельному
    районі» збіглись усі три (строк 15.06.2028, обіцяльник, схожа назва), а це
    два різні комплекси. Без пам'яті пара поверталась би в екран щоразу.
    """
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            ok = pp.reject_pair(cur, a, b, who)
        conn.commit()
        return ok
    finally:
        conn.close()


def merge(keep_id, dup_id, who):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            result = pp.merge_commitments(cur, keep_id, dup_id, who=who)
        conn.commit()
        return result
    finally:
        conn.close()


def take(commitment_id, person):
    """«Взяти в роботу» журналісткою — не мовчки: іде менеджеру на погодження,
    бо донора, проєкт і тематику проставляє він (та сама логіка, що в
    team_tasks: тему може принести будь-хто, прив'язку робить менеджер).

    Обіцянка при цьому НЕ помічається перевіреною: людина ще нічого не
    перевірила, вона лише зголосилась.
    """
    conn = _conn()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            row = pp.get(cur, int(commitment_id))
    finally:
        conn.close()
    if not row:
        return None
    team_notifications.notify_safe(
        "task_assigned",
        f"{person} бере тему з банку",
        audience=team_notifications.AUDIENCE_MANAGERS,
        body=row.get("title"),
        object_type="promise", object_id=row["id"],
        dedup_key=f"promise_take:{row['id']}:{person}",
        meta={"promise_id": row["id"], "person": person,
              "state": _state(row, int(time.time()))})
    return {"ok": True, "title": row.get("title")}
