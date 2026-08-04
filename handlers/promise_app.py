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
# Скільки днів обіцянка вважається «щойно знайденою». Тиждень, бо саме таким
# кроком редакція переглядає зроблене, і бо щоденний інкремент дає десятки
# записів — за день їх мало, за місяць уже не переглянеш.
FRESH_DAYS = 7

FACETS = [
    ("all", "Усе"),
    # «Нові» окремим входом, бо решта черги сортована за ТЕРМІНОВІСТЮ, і
    # свіжознайдене в ній тоне: обіцянка, знайдена сьогодні, зі строком у
    # грудні стоїть нижче за прострочену торішню. Це правильно для роботи й
    # непридатно для перегляду «що бот приніс».
    ("fresh", "Нові"),
    ("mine", "З моїх новин"),
    ("overdue", "Строк минув"),
    ("soon", "Скоро"),
    ("waiting", "Чекає події"),
    ("stale", "Давно не питали"),
    ("noproof", "Перевірити нічим"),
    ("populism", "Популізм"),
    # Ознаки виконання, у яких бот не був упевнений: він знайшов пізнішу
    # новину про той самий об'єкт, але закрити сам не наважився.
    ("mayclose", "Схоже, виконано"),
    # Перевірене з банку не зникає — це і є продукт. Окремий кошик, а не
    # видалення: на «зірвано» посилаються в наступному тексті.
    ("closed", "Перевірені"),
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


def _found_ago(row, now):
    """«Знайдено сьогодні / вчора / N днів тому» — підпис, без якого фасет
    «Нові» сортує за невидимим полем."""
    seen = row.get("first_seen") or 0
    if not seen:
        return None
    days = max(0, (now - int(seen)) // 86400)
    if days == 0:
        return "Знайдено сьогодні"
    if days == 1:
        return "Знайдено вчора"
    return f"Знайдено {days} {pp.plural(days, 'день', 'дні', 'днів')} тому"


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
        "found": _found_ago(row, now),
        "fresh": (now - (row.get("first_seen") or 0)) < FRESH_DAYS * 86400,
        "topic_id": row.get("topic_id"),
    }


# ---------- Автор новини ----------
#
# Нора зберігає `articles.owner_id` — той самий, за яким рахується факт KPI.
# Показуємо автора в ланцюгу з простої причини: обіцянку в його матеріалі
# природно перевіряти саме йому, він уже в темі й знає, кому дзвонити. Це ж
# дає «мої обіцянки» на дверях журналістки.
#
# Резолв через ростер, а не окремим запитом імен: у `users` бувають дублі
# акаунтів (звільнена + нинішня), і ПІБ звідти інколи розходиться з тим, як
# людину звуть в апці. Пін `team_user_link` це вже лікує — беремо його.

_authors = {"at": 0.0, "map": {}}


def author_map():
    """{users.id: ПІБ} для людей ростера. Кеш 10 хв — стільки ж, скільки в
    самого резолвера KPI, тож зайвих походів у БД сайту немає."""
    import time as _t
    if _t.monotonic() - _authors["at"] < 600 and _authors["map"]:
        return _authors["map"]
    try:
        from handlers import team_kpi, team_roster
        links = team_kpi.get_user_links()
        names = team_kpi._user_id_map()
        out = {}
        for person in team_roster.ROSTER:
            uid = team_kpi.resolve_site_user_id(person, links, names)
            if uid:
                out.setdefault(int(uid), person)
    except Exception as e:
        print(f"promise_app: автори недоступні — {e}")
        return _authors["map"]
    _authors["at"], _authors["map"] = _t.monotonic(), out
    return out


def _authors_for(cur, article_ids):
    """{article_id: ПІБ} — по одному запиту в нору й нуль у БД сайту."""
    ids = [int(i) for i in article_ids if i]
    if not ids:
        return {}
    by_id = author_map()
    cur.execute("SELECT id, owner_id FROM articles WHERE id = ANY(%s)", (ids,))
    return {aid: by_id[owner] for aid, owner in cur.fetchall()
            if owner and int(owner) in by_id}


# ---------- Ілюстрація ----------
#
# Питання Олега: «чи сильно вантажить?». Вантажить рівно тоді, коли тягнути
# на кожен показ — тридцять карток черги дали б тридцять мережевих походів.
# Тому кеш у норі (`articles.og_image`): один запит на статтю за все життя.
#
# Черга показує ЛИШЕ вже закешоване й нічого не тягне. Картка, відкрита
# вперше, доганяє своє зображення сама — це один похід і він однаково
# швидший за прогортування.

def _images_for(cur, article_ids):
    ids = [int(i) for i in article_ids if i]
    if not ids:
        return {}
    cur.execute("SELECT id, og_image FROM articles "
                "WHERE id = ANY(%s) AND og_image IS NOT NULL", (ids,))
    return dict(cur.fetchall())


def _fetch_image(cur, article_id, url):
    """Дотягнути og:image і запам'ятати. Збій — просто без картинки."""
    if not url:
        return None
    try:
        from handlers.team_matching import fetch_card_meta
        _, image = fetch_card_meta(url)
    except Exception as e:
        print(f"promise_app: не дістав зображення {article_id} — {e}")
        return None
    if image:
        cur.execute("UPDATE articles SET og_image = %s WHERE id = %s",
                    (image, int(article_id)))
    return image


def _first_revisions(cur, ids):
    """Перша ревізія кожної обіцянки — вона несе цитату й обіцяльника."""
    if not ids:
        return {}
    revs = pp.revisions(cur, list(ids))
    out = {}
    for r in revs:
        out.setdefault(r["commitment_id"], r)
    return out


def queue(cls=None, q=None, offset=0, limit=PAGE, now=None, author_id=None):
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
            elif cls == "closed":
                rows = pp.list_queue(cur, cls="closed", limit=None, now=now)
            else:
                rows = pp.list_queue(cur, limit=None, now=now)
            # Згортаємо в ТЕМИ до підрахунку фасетів: числа мусять означати
            # те саме, що показує список, інакше фасет каже «52», а на екрані
            # 30 рядків, і довіри до чисел більше немає.
            rows = pp.group_by_topic(rows)
            counts = pp.facet_counts(rows)
            counts["populism"] = sum(1 for r in rows if r.get("populism"))
            counts["all"] = len(rows)
            # «З моїх новин» і «Перевірені» рахуються окремими запитами: перше
            # звужує вибірку іншою умовою, друге бере зовсім інший статус.
            if author_id:
                counts["mine"] = len(pp.group_by_topic(pp.list_queue(
                    cur, limit=None, now=now, author_id=author_id)))
            counts["closed"] = len(pp.group_by_topic(
                pp.list_queue(cur, cls="closed", limit=None, now=now)))
            may = pp.closure_ids(cur)
            counts["mayclose"] = sum(1 for r in rows if r["id"] in may)
            fresh_since = now - FRESH_DAYS * 86400
            counts["fresh"] = sum(1 for r in rows
                                  if (r.get("first_seen") or 0) >= fresh_since)
            if cls == "populism":
                rows = [r for r in rows if r.get("populism")]
            elif cls == "fresh":
                # За ДАТОЮ ВИЯВЛЕННЯ, а не за терміновістю: тут питання не
                # «що робити», а «що бот приніс, поки я не дивився».
                rows = sorted((r for r in rows
                               if (r.get("first_seen") or 0) >= fresh_since),
                              key=lambda r: -(r.get("first_seen") or 0))
            elif cls == "mayclose":
                rows = [r for r in rows if r["id"] in may]
            elif cls == "mine" and author_id:
                rows = pp.group_by_topic(
                    pp.list_queue(cur, limit=None, now=now, author_id=author_id))
            elif cls and cls not in ("all", "closed"):
                rows = [r for r in rows if r["class"] == cls]
            total = len(rows)
            page = rows[offset:offset + limit]
            revs = _first_revisions(cur, [r["id"] for r in page])
            art_ids = [(revs.get(r["id"]) or {}).get("article_id") for r in page]
            authors = _authors_for(cur, art_ids)
            images = _images_for(cur, art_ids)
            items = []
            for r in page:
                rev = revs.get(r["id"])
                aid = (rev or {}).get("article_id")
                it = _item(r, rev, now)
                it["author"] = authors.get(aid)
                it["image"] = images.get(aid)
                # Скільки ще зобов'язань у цій темі — щоб рядок чесно казав,
                # що за ним стоїть історія, а не одна заява.
                if (r.get("topic_size") or 1) > 1:
                    it["more"] = r["topic_size"] - 1
                items.append(it)
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


def mine_count(author_id, now=None):
    """Скільки обіцянок із МОЇХ матеріалів — число на двері журналістки.

    Двері без числа не відкривають (правило апки), а саме тут воно й вирішує:
    «Банк тем» звучить як чужа адміністративна штука, «Банк тем · 3
    прострочені» — як особиста справа. Це і є та «доставка обіцянок автору»,
    про яку йшлося: не сповіщення, а видима причина зайти.
    """
    if not author_id:
        return {"total": 0, "overdue": 0}
    now = now or int(time.time())
    conn = _conn()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            rows = pp.list_queue(cur, limit=None, now=now, author_id=author_id)
    finally:
        conn.close()
    return {"total": len(rows),
            "overdue": sum(1 for r in rows if r["class"] == "overdue")}


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
            # Ланцюг — по ВСІЙ ТЕМІ, а не по одному запису. Олег, 04.08:
            # «бачу купу посилань [у чаті], чому їх не видно на фронті?» —
            # у чаті картка давно збирає історію питання цілком, а апка
            # показувала ревізії лише цієї обіцянки. У 2084 своя ревізія одна,
            # тож із трьох лінків історії лишався один.
            sib_titles = {s["id"]: s.get("title") for s in siblings}
            revs = pp.revisions(cur, [row["id"]] + list(sib_titles))
            from handlers.promises import _links_for
            links = _links_for(cur, {r["article_id"] for r in revs})
            authors = _authors_for(cur, {r["article_id"] for r in revs})
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
                    # Автор матеріалу: обіцянку в його тексті природно
                    # перевіряти саме йому — він уже в темі й знає, кому
                    # дзвонити.
                    "author": authors.get(r["article_id"]),
                    # Крок сусіда по темі підписаний і клікабельний: історія
                    # спільна, але видно, чиє саме це зобов'язання.
                    "other": (None if r["commitment_id"] == row["id"]
                              else {"id": r["commitment_id"],
                                    "title": sib_titles.get(r["commitment_id"])}),
                })
                prev = r
            # Ілюстрація й автор — із ПЕРШОЇ ревізії САМОЇ обіцянки, а не
            # теми: картка про неї, і чуже фото збивало б з пантелику.
            own = [r for r in revs if r["commitment_id"] == row["id"]]
            first_art = own[0]["article_id"] if own else None
            images = _images_for(cur, [first_art])
            image = images.get(first_art)
            if not image and first_art:
                image = _fetch_image(cur, first_art,
                                     (links.get(first_art) or {}).get("url"))
            head = _item(row, own[0] if own else None, now)
            head["image"] = image
            head["author"] = authors.get(first_art)
            head["tags"] = _tags(row, now)
            head["criterion"] = row.get("criterion")
            head["based_on"] = row.get("based_on_document")
            head["condition"] = row.get("condition")
            head["checked_at"] = row.get("checked_at")
            head["check_note"] = row.get("check_note")
            head["status"] = row.get("status")
            return {
                "commitment": head, "chain": steps,
                # `open` потрібен фронту, щоб «Перевірили» могло чесно
                # написати, скільки зобов'язань закриє відповідь на всю тему.
                "siblings": [{"id": s["id"], "title": s["title"],
                              "open": s.get("status") == "expected"}
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

def check(commitment_id, who, outcome=None, note=None, scope="one"):
    """«Перевірили» — і ЧИМ це скінчилось.

    Перша версія лише відсувала тему вниз, і висновок людини нікуди не
    записувався. А він і є продукт: обіцянка, перевірена й зірвана, — не
    «менш терміновий рядок черги», а факт, на який посилаються в наступному
    тексті. `outcome=None` лишає стару поведінку («подивився, ще в процесі»).

    `scope="topic"` закриває всю справу разом (див. pp.mark_checked):
    черга шикується темами, тож і відповідь мусить уміти лягати на тему.
    """
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        with conn.cursor() as cur:
            n = pp.mark_checked(cur, int(commitment_id), who, outcome, note,
                                scope=scope)
        conn.commit()
        return n
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


def _dupe_side(row, revs, links, now):
    """Половина пари: звична картка + лінк на статтю, з якої її взято."""
    rev = revs.get(row["id"])
    item = _item(row, rev, now)
    link = links.get((rev or {}).get("article_id")) or {}
    if link.get("url"):
        item["link"] = {"url": link["url"], "title": link.get("title"),
                        "date": link.get("date")}
    return item


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
            # Лінк на статтю прямо в парі: Олег, 03.08 — «виводь мені
            # посилання, щоб я міг без переходу в картку перевірити». Рішення
            # «одне це чи двоє» часто впирається саме в текст статті, і зайвий
            # захід у картку на кожну пару — це і є та вартість, через яку
            # черга дублів не розбирається ніколи.
            from handlers.promises import _links_for
            links = _links_for(cur, {r.get("article_id") for r in revs.values()})
            verdicts = pp.load_verdicts(cur, pairs)
            now = int(time.time())
            out = []
            for p in pairs:
                a, b = rows.get(p["a"]), rows.get(p["b"])
                if not a or not b:
                    continue
                # Кого лишаємо — рахуємо ТУТ, а не питаємо. Людське питання
                # рівно одне: це одне й те саме чи ні.
                keep, _drop = pp.merge_winner(a, b)
                v = verdicts.get(tuple(sorted((a["id"], b["id"])))) or {}
                out.append({
                    "sim": p["sim"],
                    "keep": keep["id"],
                    # Що сказав суддя. Пари, які він упевнено назвав різними,
                    # сюди взагалі не доїжджають (їх ріже детектор), тож тут
                    # лишається його «схоже, одне» — як підказка, а не вирок.
                    "why": v.get("why"),
                    "sure": v.get("same") and v.get("confidence") == "high",
                    "a": _dupe_side(a, revs, links, now),
                    "b": _dupe_side(b, revs, links, now),
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
