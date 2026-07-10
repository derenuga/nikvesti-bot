#!/usr/bin/env python3
"""Водопровід сутнісного шару над «лисячою норою» (крок C, шлях Б — §3.3.1
docs/ENTITY_LAYER_PLAN.md).

БЕЗ жодного виклику LLM — тільки робота з нОрою (Postgres бота) через psycopg2.
Сам витяг сутностей робить оркестратор (сесія Claude Code під Max) Haiku-суб-
агентами: цей скрипт лише тягне статті пачками (`fetch`) і зливає готовий JSON
у entities/article_entities (`write`). Так токени витрачаються ЛИШЕ на Haiku
(підписка Max), а не на API.

URL нори береться з env NORA_URL (щоб пароль не лежав у репозиторії):
    export NORA_URL="postgresql://postgres:...@reseau.proxy.rlwy.net:46884/railway"

Команди:
    python3 entity_pipeline.py schema
        застосувати DDL (entities + article_entities + курсор entity_last_id).

    python3 entity_pipeline.py fetch 100 batch.json
        [ТЕСТ] вивантажити N свіжих опублікованих статей 2024–2026 у JSON
        [{id, published, title_ua, title_ru, text_ua, text_ru}].
        БЕЗ курсора — разова тестова вибірка (крок 2 §3.3.1).

    python3 entity_pipeline.py next 10000 batch.json
        [ПРОДАКШН, фазовий прогін] взяти наступні N необроблених статей,
        йдучи по id ВНИЗ від курсора entity_last_id (id ≈ хронологічний, тож
        це newest→oldest: 2026→…→2009, ровно фазування §3.3). Курсор НЕ рухає
        (щоб обрив до write не пропустив пачку) — рухає його write. Пише
        {"cursor_from": …, "articles": [...]}. Коли статей нижче курсора нема —
        друкує "прогін завершено".

    python3 entity_pipeline.py write results.json [batch.json]
        злити результат витягу. Формат results.json:
        [{"article_id": 320651,
          "entities": [
            {"kind":"person","subtype":null,
             "name_ua":"Олександр Сєнкевич","name_ru":"Александр Сенкевич",
             "role":"міський голова","salience":"mentioned"}, ...]}, ...]
        Злиття: точний збіг нормалізованого імені в межах kind (однофамільців
        НЕ зливаємо). mentions/first_seen/last_seen/role_last перераховуються з
        даних (ідемпотентно — повторний write безпечний).
        Якщо передано batch.json (продакшн-цикл) — курсор entity_last_id
        опускається до мінімального id пачки (весь діапазон позначається
        обробленим, навіть статті без сутностей), і друкується покриття.

    python3 entity_pipeline.py stats
        зведення: скільки сутностей по kind, топ за згадками, к-сть зв'язків.

    python3 entity_pipeline.py sample 50 qa.txt
        вибірка N випадкових статей з їх сутностями + врізка тексту — для
        ручної перевірки якості (§3.6: точність ≥90%, вигадані ролі ≤2%).

    python3 entity_pipeline.py reset
        ОЧИСТИТИ entities + article_entities і скинути курсор entity_last_id=0.
        Для чистого перегону тесту (щоб v2 не злився поверх v1-даних). Схему
        (таблиці) не чіпає. Питає підтвердження.
"""

import sys
import os
import re
import json
import random

ALLOWED_KINDS = {"person", "org", "place", "document", "event"}
ALLOWED_SALIENCE = {"main", "mentioned"}

# Витяг тексту, який віддаємо суб-агенту, обмежуємо, щоб контекст пачки був
# керованим (~1.2к токенів/стаття за планом; 8000 симв. ≈ з запасом).
TEXT_CAP = 8000

DDL = r"""
CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    kind TEXT NOT NULL,
    subtype TEXT,
    name_ua TEXT,
    name_ru TEXT,
    aliases TEXT[] DEFAULT '{}',
    role_last TEXT,
    first_seen BIGINT,
    last_seen BIGINT,
    mentions INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entities_kind        ON entities (kind);
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameua ON entities (kind, lower(name_ua));
CREATE INDEX IF NOT EXISTS idx_entities_kind_nameru ON entities (kind, lower(name_ru));
CREATE TABLE IF NOT EXISTS article_entities (
    article_id BIGINT NOT NULL,
    entity_id BIGINT NOT NULL,
    role_at_time TEXT,
    salience TEXT,
    PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities (entity_id);
INSERT INTO sync_state (key, value) VALUES ('entity_last_id', '0')
ON CONFLICT (key) DO NOTHING;
"""


def get_url():
    url = os.environ.get("NORA_URL")
    if not url:
        sys.exit("NORA_URL не заданий. Спершу: export NORA_URL='postgresql://…'")
    return url


def connect():
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 не встановлено. pip install psycopg2-binary")
    return psycopg2.connect(get_url(), connect_timeout=10)


def norm(s):
    """Нормалізоване ім'я для точного злиття: trim + collapse spaces + lower."""
    if not s:
        return None
    return re.sub(r"\s+", " ", s.strip()).lower() or None


# Курований словник канону ключових локацій (домен: Миколаївщина + топ-згадувані).
# Зводить відмінки/рос. написання до називного, щоб «Миколаєв»/«Миколаєві»/
# «Миколаївщина» не плодили окремих сутностей. Це НЕ морфологія — лише
# страхувальна сітка над інструкцією суб-агенту віддавати називний відмінок
# (промпт гасить решту; на всьому архіві можливі інші міста в непрямих формах).
# Ключ — нормалізоване (lower) написання варіанта; значення — канон (name_ua, name_ru).
CANON_PLACE = {
    "миколаєв": ("Миколаїв", "Николаев"),
    "миколаєві": ("Миколаїв", "Николаев"),
    "миколаєва": ("Миколаїв", "Николаев"),
    "николаев": ("Миколаїв", "Николаев"),
    "nikolaev": ("Миколаїв", "Николаев"),
    "миколаївщина": ("Миколаївська область", "Николаевская область"),
    "миколаївщині": ("Миколаївська область", "Николаевская область"),
    "миколаївській області": ("Миколаївська область", "Николаевская область"),
    "миколаївської області": ("Миколаївська область", "Николаевская область"),
    "николаевщина": ("Миколаївська область", "Николаевская область"),
    "одесі": ("Одеса", "Одесса"),
    "одеси": ("Одеса", "Одесса"),
    "одесу": ("Одеса", "Одесса"),
    "одеській області": ("Одеська область", "Одесская область"),
    "херсонщина": ("Херсонська область", "Херсонская область"),
    "херсонщині": ("Херсонська область", "Херсонская область"),
    "херсоні": ("Херсон", "Херсон"),
    "києві": ("Київ", "Киев"),
    "києва": ("Київ", "Киев"),
    "україні": ("Україна", "Украина"),
    "україни": ("Україна", "Украина"),
    "вишневому": ("Вишневе", "Вишневое"),
}


def canon_place(name_ua, name_ru):
    """Звести відомий варіант локації до називного канону; інакше — без змін."""
    for v in (norm(name_ua), norm(name_ru)):
        if v in CANON_PLACE:
            return CANON_PLACE[v]
    return name_ua, name_ru


def get_state(cur, key, default=None):
    cur.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_state(cur, key, value):
    cur.execute(
        "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )


# ---------- schema ----------

def cmd_schema():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("SELECT to_regclass('entities'), to_regclass('article_entities')")
    print("schema:", cur.fetchone())
    cur.execute("SELECT value FROM sync_state WHERE key='entity_last_id'")
    print("cursor:", cur.fetchone())
    cur.execute("SELECT count(*) FROM articles")
    print("articles:", cur.fetchone()[0])
    cur.close()
    conn.close()
    print("OK")


# ---------- fetch ----------

def cmd_fetch(n, outpath):
    conn = connect()
    cur = conn.cursor()
    # Свіжі опубліковані 2024–2026 (де щільність сутностей вища). Нора вже
    # містить лише status=1 та published у минулому — додатковий фільтр не треба.
    cur.execute(
        """
        SELECT id, published, title_ua, title_ru, text_ua, text_ru
        FROM articles
        WHERE published >= extract(epoch FROM date '2024-01-01')
          AND published <  extract(epoch FROM date '2027-01-01')
        ORDER BY published DESC
        LIMIT %s
        """,
        (n,),
    )
    out = []
    for aid, pub, tua, tru, xua, xru in cur.fetchall():
        out.append({
            "id": aid,
            "published": int(pub) if pub is not None else None,
            "title_ua": tua,
            "title_ru": tru,
            "text_ua": (xua or "")[:TEXT_CAP] or None,
            "text_ru": (xru or "")[:TEXT_CAP] or None,
        })
    cur.close()
    conn.close()
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"fetched {len(out)} статей → {outpath}")
    if out:
        print(f"діапазон дат (unix): {out[-1]['published']} … {out[0]['published']}")


# ---------- next (продакшн-цикл по курсору, newest→oldest) ----------

def cmd_next(n, outpath):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT max(id) FROM articles")
    maxid = cur.fetchone()[0] or 0
    stored = int(get_state(cur, "entity_last_id", "0") or "0")
    # 0 = ще не починали → стеля вище за max(id); інакше йдемо нижче курсора.
    ceiling = (maxid + 1) if stored == 0 else stored
    cur.execute(
        """
        SELECT id, published, title_ua, title_ru, text_ua, text_ru
        FROM articles
        WHERE id < %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (ceiling, n),
    )
    arts = []
    for aid, pub, tua, tru, xua, xru in cur.fetchall():
        arts.append({
            "id": aid,
            "published": int(pub) if pub is not None else None,
            "title_ua": tua,
            "title_ru": tru,
            "text_ua": (xua or "")[:TEXT_CAP] or None,
            "text_ru": (xru or "")[:TEXT_CAP] or None,
        })
    cur.close()
    conn.close()
    if not arts:
        print("прогін завершено: статей нижче курсора немає "
              f"(курсор entity_last_id = {stored})")
        return
    payload = {"cursor_from": ceiling, "articles": arts}
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    low, high = arts[-1]["id"], arts[0]["id"]
    print(f"взято {len(arts)} статей id {high}…{low} → {outpath}")
    print(f"курсор поки НЕ рухаю (рухне write). Після write буде: {low}")


# ---------- write ----------

def cmd_write(path, batch_path=None):
    """Пакетний запис: існуючі сутності підвантажуються в пам'ять один раз,
    зіставлення (те саме — точний збіг нормалізованого імені в межах kind,
    для place через CANON_PLACE) робиться в Python, вставки йдуть execute_values
    пачкою. Семантика злиття ідентична побудовному варіанту, але замість тисяч
    round-trip'ів до нори — кілька запитів. Для place застосовується canon_place."""
    from psycopg2.extras import execute_values

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    conn = connect()
    cur = conn.cursor()

    # 1. Підвантажити наявні сутності в пам'ять (індекс за (kind, norm-ім'я)).
    cur.execute("SELECT id, kind, name_ua, name_ru, subtype, aliases, mentions FROM entities")
    recs = {}   # id -> запис (мутабельний)
    index = {}  # (kind, norm) -> id (за найбільшою кількістю згадок)
    for eid, kind, nua, nru, sub, aliases, mentions in cur.fetchall():
        rec = {"id": eid, "kind": kind, "name_ua": nua, "name_ru": nru,
               "subtype": sub, "aliases": set(aliases or []),
               "mentions": mentions or 0, "dirty": False, "new": False}
        recs[eid] = rec
        for nm in (nua, nru):
            k = (kind, norm(nm))
            if k[1] is None:
                continue
            best = index.get(k)
            if best is None or recs[best]["mentions"] < rec["mentions"]:
                index[k] = eid

    new_recs = []       # записи на INSERT
    tmp_seq = [-1]      # тимчасові від'ємні id для нових (мапляться після вставки)

    def find_or_stage(kind, subtype, name_ua, name_ru):
        if kind == "place":
            name_ua, name_ru = canon_place(name_ua, name_ru)
        nu, nr = norm(name_ua), norm(name_ru)
        if not nu and not nr:
            return None
        hit = None
        for k in ((kind, nu), (kind, nr)):
            if k[1] and k in index:
                hit = index[k]
                break
        if hit is not None:
            rec = recs[hit]
            if not rec["name_ua"] and name_ua:
                rec["name_ua"] = name_ua; rec["dirty"] = True
            if not rec["name_ru"] and name_ru:
                rec["name_ru"] = name_ru; rec["dirty"] = True
            if not rec["subtype"] and subtype:
                rec["subtype"] = subtype; rec["dirty"] = True
            canon_norms = {norm(rec["name_ua"]), norm(rec["name_ru"])}
            for nm in (name_ua, name_ru):
                if nm and norm(nm) not in canon_norms and nm not in rec["aliases"]:
                    rec["aliases"].add(nm); rec["dirty"] = True
            for nm in (rec["name_ua"], rec["name_ru"]):
                kk = (kind, norm(nm))
                if kk[1] and kk not in index:
                    index[kk] = rec["id"]
            return rec["id"]
        tid = tmp_seq[0]
        tmp_seq[0] -= 1
        rec = {"id": tid, "kind": kind, "name_ua": name_ua, "name_ru": name_ru,
               "subtype": subtype, "aliases": set(), "mentions": 0,
               "dirty": True, "new": True}
        recs[tid] = rec
        new_recs.append(rec)
        for nm in (name_ua, name_ru):
            kk = (kind, norm(nm))
            if kk[1]:
                index.setdefault(kk, tid)
        return tid

    # 2. Пройти результат, зібрати зв'язки (з тимчасовими id для нових сутностей).
    links = []   # [article_id, eid(may be temp), role, salience]
    n_articles = n_skipped = 0
    got_ids = set()
    for art in data:
        aid = art.get("article_id") or art.get("id")
        if aid is None:
            continue
        n_articles += 1
        got_ids.add(aid)
        for e in art.get("entities", []):
            kind = (e.get("kind") or "").strip().lower()
            sal = (e.get("salience") or "").strip().lower()
            if kind not in ALLOWED_KINDS or sal not in ALLOWED_SALIENCE:
                n_skipped += 1
                continue
            eid = find_or_stage(kind, e.get("subtype"),
                                e.get("name_ua"), e.get("name_ru"))
            if eid is None:
                n_skipped += 1
                continue
            role = e.get("role") or e.get("role_at_time") or None
            links.append([aid, eid, role, sal])

    # 3. Вставити нові сутності пачкою, змапити тимчасові id -> реальні.
    idmap = {}
    if new_recs:
        rows = [(r["kind"], r["subtype"], r["name_ua"], r["name_ru"],
                 sorted(r["aliases"])) for r in new_recs]
        inserted = execute_values(
            cur,
            "INSERT INTO entities (kind, subtype, name_ua, name_ru, aliases) "
            "VALUES %s RETURNING id",
            rows, fetch=True,
        )
        for r, row in zip(new_recs, inserted):
            idmap[r["id"]] = row[0]

    # 4. Оновити наявні сутності, що набули імені/алiаса/subtype цієї пачки.
    for rec in recs.values():
        if rec["new"] or not rec["dirty"]:
            continue
        cur.execute(
            "UPDATE entities SET name_ua = %s, name_ru = %s, subtype = %s, aliases = %s "
            "WHERE id = %s",
            (rec["name_ua"], rec["name_ru"], rec["subtype"],
             sorted(rec["aliases"]), rec["id"]),
        )

    # 5. Вставити зв'язки пачкою (тимчасові id -> реальні).
    # Дедуп (article_id, entity_id) last-wins ПЕРЕД вставкою: дві сутності
    # однієї статті можуть звестись до одного entity_id (напр. Миколаїв+Миколаєв
    # після canon_place). execute_values з ON CONFLICT не терпить дубль ключа в
    # одній пачці (CardinalityViolation) — тож згортаємо тут, як робив ON CONFLICT
    # у побудовному варіанті.
    touched = set()
    dedup = {}
    for aid, eid, role, sal in links:
        rid = idmap.get(eid, eid)
        dedup[(aid, rid)] = (role, sal)   # останній перемагає
        touched.add(rid)
    resolved = [(aid, rid, role, sal) for (aid, rid), (role, sal) in dedup.items()]
    if resolved:
        execute_values(
            cur,
            "INSERT INTO article_entities (article_id, entity_id, role_at_time, salience) "
            "VALUES %s "
            "ON CONFLICT (article_id, entity_id) DO UPDATE SET "
            "role_at_time = EXCLUDED.role_at_time, salience = EXCLUDED.salience",
            resolved,
        )
    n_links = len(resolved)

    # 6. Перерахунок агрегатів із даних — ідемпотентно, не залежить від порядку.
    cur.execute(
        """
        UPDATE entities e SET
            mentions   = s.cnt,
            first_seen = s.fmin,
            last_seen  = s.fmax
        FROM (
            SELECT ae.entity_id, count(*) AS cnt,
                   min(a.published) AS fmin, max(a.published) AS fmax
            FROM article_entities ae JOIN articles a ON a.id = ae.article_id
            GROUP BY ae.entity_id
        ) s
        WHERE e.id = s.entity_id
        """
    )
    # role_last = роль у найсвіжішій статті сутності (де роль текстуально є).
    cur.execute(
        """
        UPDATE entities e SET role_last = sub.role
        FROM (
            SELECT DISTINCT ON (ae.entity_id) ae.entity_id,
                   ae.role_at_time AS role
            FROM article_entities ae JOIN articles a ON a.id = ae.article_id
            WHERE ae.role_at_time IS NOT NULL AND ae.role_at_time <> ''
            ORDER BY ae.entity_id, a.published DESC
        ) sub
        WHERE e.id = sub.entity_id
        """
    )
    # Продакшн-цикл: якщо передано пачку fetch/next — опустити курсор до її
    # мінімального id (весь діапазон оброблено, включно зі статтями без
    # сутностей) і звірити покриття.
    if batch_path:
        with open(batch_path, encoding="utf-8") as f:
            batch = json.load(f)
        batch_arts = batch["articles"] if isinstance(batch, dict) else batch
        batch_ids = [a.get("id") for a in batch_arts if a.get("id") is not None]
        if batch_ids:
            new_cur = min(batch_ids)
            set_state(cur, "entity_last_id", new_cur)
            covered = len(got_ids & set(batch_ids))
            print(f"курсор entity_last_id → {new_cur} "
                  f"(оброблено діапазон до цього id включно)")
            print(f"покриття пачки: у пачці {len(batch_ids)}, "
                  f"є результат по {covered}")
            if covered < len(batch_ids):
                print(f"  увага: {len(batch_ids) - covered} статей без результату "
                      f"(нуль сутностей або суб-агент їх не повернув) — теж позначені обробленими")
    conn.commit()
    cur.close()
    conn.close()
    print(f"статей оброблено: {n_articles}, зв'язків: {n_links}, "
          f"сутностей торкнулись: {len(touched)}, пропущено (невалідні): {n_skipped}")


# ---------- stats ----------

def cmd_stats():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities")
    print("усього сутностей:", cur.fetchone()[0])
    cur.execute("SELECT count(*) FROM article_entities")
    print("усього зв'язків:", cur.fetchone()[0])
    print("\nпо kind:")
    cur.execute("SELECT kind, count(*) FROM entities GROUP BY kind ORDER BY count(*) DESC")
    for kind, c in cur.fetchall():
        print(f"  {kind:9} {c}")
    print("\nтоп-15 за згадками:")
    cur.execute(
        "SELECT kind, coalesce(name_ua, name_ru), role_last, mentions "
        "FROM entities ORDER BY mentions DESC LIMIT 15"
    )
    for kind, name, role, m in cur.fetchall():
        print(f"  [{kind}] {name} — {role or '—'} ({m})")
    cur.close()
    conn.close()


# ---------- sample (ручна перевірка якості §3.6) ----------

def cmd_sample(n, outpath):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT article_id FROM article_entities")
    ids = [r[0] for r in cur.fetchall()]
    random.shuffle(ids)
    ids = ids[:n]
    lines = []
    for aid in ids:
        cur.execute(
            "SELECT title_ua, title_ru, coalesce(text_ua, text_ru) FROM articles WHERE id = %s",
            (aid,),
        )
        r = cur.fetchone()
        title = (r[0] or r[1] or "—") if r else "—"
        body = (r[2] or "")[:600] if r else ""
        lines.append(f"===== article {aid}: {title}")
        lines.append(body)
        cur.execute(
            "SELECT e.kind, coalesce(e.name_ua, e.name_ru), ae.role_at_time, ae.salience "
            "FROM article_entities ae JOIN entities e ON e.id = ae.entity_id "
            "WHERE ae.article_id = %s ORDER BY ae.salience",
            (aid,),
        )
        for kind, name, role, sal in cur.fetchall():
            lines.append(f"    [{kind}/{sal}] {name} — {role or '—'}")
        lines.append("")
    cur.close()
    conn.close()
    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"вибірка {len(ids)} статей → {outpath} (звірити очима: точність ≥90%, вигадані ролі ≤2%)")


def cmd_reset():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM entities")
    ne = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM article_entities")
    nl = cur.fetchone()[0]
    ans = input(f"Очистити entities ({ne}) та article_entities ({nl}) "
                f"і скинути курсор? Введи 'yes': ")
    if ans.strip().lower() != "yes":
        print("скасовано")
        return
    cur.execute("TRUNCATE entities RESTART IDENTITY")
    cur.execute("TRUNCATE article_entities")
    set_state(cur, "entity_last_id", "0")
    print("очищено: entities, article_entities; курсор entity_last_id=0")
    cur.close()
    conn.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "schema":
        cmd_schema()
    elif cmd == "reset":
        cmd_reset()
    elif cmd == "fetch":
        cmd_fetch(int(sys.argv[2]), sys.argv[3])
    elif cmd == "next":
        cmd_next(int(sys.argv[2]), sys.argv[3])
    elif cmd == "write":
        cmd_write(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "sample":
        cmd_sample(int(sys.argv[2]), sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
