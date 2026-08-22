"""Перевірка гіпотези: чи бачить СМИСЛ те, чого не бачать букви.

**Навіщо.** Детектор дублів банку відбирає кандидатів за схожістю НАЗВ (trgm),
і тому цілий клас дублів для нього не існує. Живий приклад, знайдений 22.08 у
даних: вулиця 6 Слобідська — ТРИ записи про один ремонт на 23,8 млн
(«провести капітальний ремонт» · «розпочати основний етап» · «розпочати
укладання першого шару асфальту»), схожість назв 0.23–0.41, три різні теми.
Жодна пара не проходить жодного порога.

Оцінка Олега: «Ну ми журналістикою зайняті, звісно назви не ідентичні. Кожен
заголовок різний. Потрібен спосіб апці розуміти, що це одне й те саме
обіцяння, окрім підрахунку слів і літер».

**Що це за файл.** НЕ продукт, а замір: чи справді ембединги розводять те, що
треба розвести, і зводять те, що треба звести — САМЕ НА НАШИХ українських
формулюваннях, а не на англомовних бенчмарках (українських бенчмарків
ембедингів публічно не існує в жодного провайдера, перевірено 22.08).

**Замір перший (`run`) — пари.** Фіксований набір з РЕАЛЬНИХ записів банку:

- `same` — одна справа, різні слова. Мусять стояти БЛИЗЬКО;
- `apart` — рівно ті пастки, які назвав Олег: різні обіцянки ОДНІЄЇ людини
  (Сєнкевич) і різні роботи на ОДНОМУ обʼєкті (центр «Відновлення»). Мусять
  стояти ДАЛЕКО.

Відповідь 22.08: **глобального порога немає в жодного провайдера.** OpenAI —
найгірше «одне» 0.36 проти найгіршого «різного» 0.76; Gemini — 0.71 проти
0.85. Ламає списки та сама пара: інклюзивний корпус і соціальне містечко в
центрі «Відновлення» — один заклад, одна програма, дві РІЗНІ будівлі. Ембединг
міряє, ПРО ЩО текст, а не чи це той самий обʼєкт; відповісти на друге питання
може лише суддя, і саме воно стоїть у нього першим від 18.08.

**Замір другий (`sweep`) — обсяг.** Звідси й випливає правильне питання: не
«яким порогом різати», а «скільки пар доведеться показати судді, щоб жодна
справжня пара не загубилась». Тобто ембединг не ВИРІШУЄ, а лише ЗВУЖУЄ — рівно
там, де сьогодні стоїть trgm-індекс. `sweep` рахує це на всьому банку: скільки
пар дає кожен поріг, скільки дає відбір «K найближчих сусідів кожного запису»,
на якому МІСЦІ в сусідах стоїть кожна з еталонних пар (звідси й береться
потрібне K) і скільки пар віддає нинішній детектор для порівняння.

**Провайдер не обраний навмисно.** Працює з тими ключами, які лежать у
середовищі; є кілька — рахує всі й показує поруч. Своїх SDK не тягне: усі три
API це один POST.
"""

import math
import os

import entity_pipeline as ep
import promise_pipeline as pp

# Пари з реальних записів. Ліворуч — те, що МУСИТЬ зійтись, праворуч — те, що
# мусить лишитись нарізно. Кожен рядок підписаний, бо через півроку «(483,
# 2316)» нікому нічого не скаже.
SAME = [
    ((483, 1830), "вулиця 6 Слобідська: капремонт / основний етап"),
    ((483, 2316), "вулиця 6 Слобідська: капремонт / перший шар асфальту"),
    ((1830, 2316), "вулиця 6 Слобідська: основний етап / перший шар"),
    ((1987, 2117), "харчування школярів: субвенція / організувати"),
    ((1772, 2079), "колектор на Бедзая: замінити / завершити реконструкцію"),
    ((2137, 2138), "кухня ліцею №3: обрати підрядника / виконати"),
    ((2063, 2074), "МАСЦО Первомайськ: реалізувати проєкт / створити систему"),
    ((1961, 1999), "НЕФКО Південноукраїнськ: підписати угоду / реконструювати"),
    ((114, 777), "дорога до Матвіївки: відремонтувати / звернутись до Служби"),
]

APART = [
    ((36, 322), "Сєнкевич: автобусний маршрут / парк «Юність»"),
    ((331, 754), "Сєнкевич: укриття / винагороди спортсменам"),
    ((795, 817), "Сєнкевич: рятувальні пости / книги в бібліотеки"),
    ((1000, 335), "Сєнкевич: дитсадок на Північному / скейтпарк"),
    ((1985, 1986), "центр «Відновлення»: інклюзивний корпус / соціальне містечко"),
    ((2059, 2256), "14 млн: ямковий ремонт доріг / сонячні електростанції"),
]

# Імена змінних — ТІ, ЯКІ ПОКЛАВ ОЛЕГ (`OPENAI_KEY`, `VOYAGE_KEY`), а не
# канонічні з документації провайдерів. Кожне поле — список, бо друге ім'я
# лишається запасним: перейменувати змінну в Railway дорожче, ніж прочитати
# два ключі.
#
# `chunk` — скільки текстів іде одним запитом. Різне не з примхи: OpenAI бере
# до 2048 рядків, Voyage сотні, а Gemini має ОКРЕМИЙ batch-endpoint і жорсткіші
# ліміти. Весь банк це 858 записів, тобто чотири запити в OpenAI і вісімнадцять
# у Gemini — обидва вкладаються у безкоштовний тариф.
PROVIDERS = {
    "OpenAI · text-embedding-3-small": {
        "env": ["OPENAI_KEY", "OPENAI_API_KEY"],
        "url": "https://api.openai.com/v1/embeddings",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
        # У OpenAI типу задачі немає взагалі — одна модель на всі випадки.
        "body": lambda ts: {"model": "text-embedding-3-small", "input": list(ts)},
        "parse": lambda d: [row["embedding"] for row in d["data"]],
        "chunk": 256,
    },
    "Voyage · voyage-4 (симетрично)": {
        "env": ["VOYAGE_KEY", "VOYAGE_API_KEY"],
        "url": "https://api.voyageai.com/v1/embeddings",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
        # input_type НЕ задається свідомо. «document» — режим АСИМЕТРИЧНОГО
        # пошуку (короткий запит проти довгого тексту), а в нас задача
        # симетрична: два однакові за природою записи банку. Саме так
        # провайдер і радить робити для similarity та кластеризації.
        #
        # Модель — voyage-4, як назвав Олег із власного кабінету. Перший
        # прогін упав на 403, і саме тому помилка тепер несе ТІЛО відповіді:
        # ключ, права й назва моделі дають різні тексти, а голий код статусу
        # їх не розрізняє.
        "body": lambda ts: {"model": "voyage-4", "input": list(ts)},
        "parse": lambda d: [row["embedding"] for row in d["data"]],
        "chunk": 128,
    },
    # У Gemini тип задачі задається ЯВНО і міняє самі вектори. SEMANTIC_
    # SIMILARITY — рівно наш випадок: «чи про одне й те саме ці два тексти».
    # Друга картка нижче лишена НАВМИСНО: саме в тому режимі (дефолтний
    # RETRIEVAL_DOCUMENT) зроблено перший замір 22.08, і без нього не видно,
    # скільки з результату дала модель, а скільки — недоглянутий параметр.
    "Google · gemini-embedding-001 (similarity)": {
        "env": ["GEMINI_KEY", "GEMINI_API_KEY"],
        "url": ("https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-embedding-001:batchEmbedContents"),
        "auth": lambda k: {"x-goog-api-key": k},
        "body": lambda ts: {"requests": [
            {"model": "models/gemini-embedding-001",
             "taskType": "SEMANTIC_SIMILARITY",
             "content": {"parts": [{"text": t}]}} for t in ts]},
        "parse": lambda d: [e["values"] for e in d["embeddings"]],
        "chunk": 50,
    },
    "Google · gemini-embedding-001 (retrieval, як міряли вперше)": {
        "env": ["GEMINI_KEY", "GEMINI_API_KEY"],
        "url": ("https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-embedding-001:batchEmbedContents"),
        "auth": lambda k: {"x-goog-api-key": k},
        "body": lambda ts: {"requests": [
            {"model": "models/gemini-embedding-001",
             "taskType": "RETRIEVAL_DOCUMENT",
             "content": {"parts": [{"text": t}]}} for t in ts]},
        "parse": lambda d: [e["values"] for e in d["embeddings"]],
        "chunk": 50,
        "extra": True,
    },
}


def _key(name):
    """Ключ провайдера — з першої змінної, яка справді заповнена."""
    for env in PROVIDERS[name]["env"]:
        val = (os.environ.get(env) or "").strip()
        if val:
            return val
    return None


def available(extras=True):
    """Провайдери, ключ яких справді лежить у середовищі.

    `extras=False` прибирає контрольні картки (той самий провайдер у другому
    режимі): на парах вони коштують копійки й показують ціну недогляду, а
    ганяти ними ВЕСЬ банк — платити двічі за те, що вже виміряно.
    """
    return [n for n in PROVIDERS
            if _key(n) and (extras or not PROVIDERS[n].get("extra"))]


def _text(title, subject):
    """Те, що йде в ембединг: назва + предмет. Саме ця пара несе смисл
    обіцянки; цитата тягне за собою слова журналіста й зашумила б порівняння."""
    return ((title or "") + ". " + (subject or "")).strip(". ")


def _texts(cur, ids):
    cur.execute("SELECT id, coalesce(title, ''), coalesce(subject, '') "
                "FROM commitments WHERE id = ANY(%s)", (list(ids),))
    return {r[0]: _text(r[1], r[2]) for r in cur.fetchall()}


def corpus(cur):
    """Усі відкриті записи — рівно ті, серед яких детектор шукає дублі."""
    cur.execute("SELECT id, coalesce(title, ''), coalesce(subject, '') "
                "FROM commitments WHERE status = 'expected' ORDER BY id")
    rows = cur.fetchall()
    return [r[0] for r in rows], [_text(r[1], r[2]) for r in rows]


def _embed(name, texts):
    import requests

    p = PROVIDERS[name]
    headers = {"Content-Type": "application/json", **p["auth"](_key(name))}
    out, step = [], p["chunk"]
    for i in range(0, len(texts), step):
        r = requests.post(p["url"], headers=headers,
                          json=p["body"](texts[i:i + step]), timeout=180)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        out.extend(p["parse"](r.json()))
    if len(out) != len(texts):
        raise RuntimeError(f"провайдер віддав {len(out)} векторів на "
                           f"{len(texts)} текстів")
    return out


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _trgm(cur, a, b):
    """Скільки та сама пара має СЬОГОДНІ — щоб різниця була видима."""
    cur.execute("SELECT similarity((SELECT coalesce(title,'') FROM commitments WHERE id=%s),"
                "                  (SELECT coalesce(title,'') FROM commitments WHERE id=%s))",
                (a, b))
    return float(cur.fetchone()[0] or 0)


def run(provider):
    """Замір перший: обидва списки пар. Нічого не пише."""
    ids = {i for pair, _ in SAME + APART for i in pair}
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            texts = _texts(cur, ids)
            missing = [i for i in ids if i not in texts]
            order = sorted(texts)
            vecs = dict(zip(order, _embed(provider, [texts[i] for i in order])))
            rows = []
            for kind, pairs in (("same", SAME), ("apart", APART)):
                for (a, b), label in pairs:
                    if a not in vecs or b not in vecs:
                        continue
                    rows.append({"kind": kind, "label": label,
                                 "sim": _cos(vecs[a], vecs[b]),
                                 "trgm": _trgm(cur, a, b)})
    finally:
        conn.close()
    return rows, missing


def verdict(rows):
    """Що саме показали пари.

    Питання «чи існує поріг» дає відповідь «так/ні», і 22.08 вона виявилась
    надто грубою: списки перетинає ОДНА пара з пʼятнадцяти, і бінарне «ні»
    ховає це так само надійно, як бінарне «так» сховало б провал. Тому крім
    відповіді рахується САМИЙ ДОБРИЙ поріг — найнижча справжня пара, тобто
    той, що не губить жодної, — і скільки пасток він при цьому пропускає.
    Саме ця пара чисел і каже, годяться ембединги у ВІДБІР кандидатів.
    """
    same = [r for r in rows if r["kind"] == "same"]
    apart = [r for r in rows if r["kind"] == "apart"]
    if not same or not apart:
        return None
    lo = min(r["sim"] for r in same)
    hi = max(r["sim"] for r in apart)
    leaked = [r["label"] for r in apart if r["sim"] >= lo]
    return {"min_same": lo, "max_apart": hi, "gap": lo - hi,
            "separates": lo > hi, "cut": lo, "leaked": leaked,
            "same_n": len(same), "apart_n": len(apart)}


# ---------- замір другий: обсяг роботи для судді ----------

CUTS = (0.90, 0.85, 0.80, 0.75, 0.70, 0.65)
KS = (3, 5, 10, 20)


def _matrix(vecs):
    """Матриця косинусів. numpy на Railway є (тягнеться за matplotlib), але
    його відсутність не має валити замір — тоді рахуємо руками."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        m = np.asarray(vecs, dtype="float32")
        m /= np.linalg.norm(m, axis=1, keepdims=True)
        s = m @ m.T
        np.fill_diagonal(s, -1.0)
        return s, np
    n = len(vecs)
    norm = []
    for v in vecs:
        d = math.sqrt(sum(x * x for x in v)) or 1.0
        norm.append([x / d for x in v])
    s = [[0.0] * n for _ in range(n)]
    for i in range(n):
        s[i][i] = -1.0
        for j in range(i + 1, n):
            val = sum(x * y for x, y in zip(norm[i], norm[j]))
            s[i][j] = s[j][i] = val
    return s, None


def sweep(provider):
    """Замір другий: скільки пар доведеться показати судді.

    Порогів тут два різні за природою. **Поріг** («усе, що вище 0.85») дає
    обсяг, який залежить від того, наскільки однорідний банк: місто щодня
    обіцяє ремонтувати дороги, тож на 0.7 в кандидати піде півбанку.
    **K найближчих** дає обсяг, ЗАДАНИЙ НАПЕРЕД (858×K/2 у стелі), і саме так
    ембединги й ставлять у відбір кандидатів. Тому головне число тут не
    схожість, а МІСЦЕ: на якому за близькістю сусіді стоїть справжня пара.
    Найгірше з дев'яти місць і є потрібне K.
    """
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            ids, texts = corpus(cur)
            pp._trgm_limit(cur)
            cur.execute(
                "SELECT count(*) FROM commitments a JOIN commitments b "
                "  ON b.id > a.id AND b.title %% a.title "
                "WHERE a.status = 'expected' AND b.status = 'expected'")
            trgm_pairs = int(cur.fetchone()[0])
    finally:
        conn.close()

    vecs = _embed(provider, texts)
    s, np = _matrix(vecs)
    pos = {pid: i for i, pid in enumerate(ids)}
    n = len(ids)

    cuts = []
    for c in CUTS:
        if np is not None:
            cnt = int((np.triu(s, 1) >= c).sum())
        else:
            cnt = sum(1 for i in range(n) for j in range(i + 1, n) if s[i][j] >= c)
        cuts.append((c, cnt))

    # Місце кожної еталонної пари в сусідах — беремо кращий з двох напрямів
    # (кандидат народжується, щойно ОДИН із записів побачив другого).
    kmax = max(KS)
    if np is not None:
        order = np.argsort(-s, axis=1)[:, :max(kmax, 60)]
        neigh = {ids[i]: [ids[j] for j in order[i]] for i in range(n)}
    else:
        neigh = {}
        for i in range(n):
            row = sorted(range(n), key=lambda j: -s[i][j])[:max(kmax, 60)]
            neigh[ids[i]] = [ids[j] for j in row]

    def rank(a, b):
        best = None
        for x, y in ((a, b), (b, a)):
            lst = neigh.get(x) or []
            if y in lst:
                r = lst.index(y) + 1
                best = r if best is None else min(best, r)
        return best

    ref = []
    for kind, pairs in (("same", SAME), ("apart", APART)):
        for (a, b), label in pairs:
            if a not in pos or b not in pos:
                continue
            ref.append({"kind": kind, "label": label, "a": a, "b": b,
                        "sim": float(s[pos[a]][pos[b]]), "rank": rank(a, b)})

    ks = []
    for k in KS:
        seen = set()
        for pid in ids:
            for other in (neigh.get(pid) or [])[:k]:
                seen.add((pid, other) if pid < other else (other, pid))
        caught = sum(1 for r in ref
                     if r["kind"] == "same" and r["rank"] and r["rank"] <= k)
        traps = sum(1 for r in ref
                    if r["kind"] == "apart" and r["rank"] and r["rank"] <= k)
        ks.append({"k": k, "pairs": len(seen), "same": caught,
                   "same_total": sum(1 for r in ref if r["kind"] == "same"),
                   "apart": traps,
                   "apart_total": sum(1 for r in ref if r["kind"] == "apart")})

    return {"n": n, "total": n * (n - 1) // 2, "trgm": trgm_pairs,
            "cuts": cuts, "ks": ks, "ref": ref}
