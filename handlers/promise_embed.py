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

Тому набір фіксований і складений з РЕАЛЬНИХ записів банку, з двома видами
пар:

- `same` — одна справа, різні слова. Мусять стояти БЛИЗЬКО;
- `apart` — рівно ті пастки, які назвав Олег: різні обіцянки ОДНІЄЇ людини
  (Сєнкевич) і різні роботи на ОДНОМУ обʼєкті (центр «Відновлення»). Мусять
  стояти ДАЛЕКО.

Головне число на виході — не середня схожість, а чи існує ПОРІГ, який
розділяє два списки. Немає порога — ембединги задачу не вирішують, і платити
за них нема за що.

**Провайдер не обраний навмисно.** Працює з тим ключем, який є: `OPENAI_API_KEY`
(text-embedding-3-small) або `GEMINI_API_KEY` (gemini-embedding-001). Є обидва —
рахує обидва й показує поруч. Своїх SDK не тягне: обидва API це один POST.
"""

import json
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
PROVIDERS = {
    "OpenAI · text-embedding-3-small": {
        "env": ["OPENAI_KEY", "OPENAI_API_KEY"],
        "url": "https://api.openai.com/v1/embeddings",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
        "body": lambda texts: {"model": "text-embedding-3-small", "input": texts},
        "parse": lambda d: [row["embedding"] for row in d["data"]],
        "batched": True,
    },
    "Voyage · voyage-4": {
        "env": ["VOYAGE_KEY", "VOYAGE_API_KEY"],
        "url": "https://api.voyageai.com/v1/embeddings",
        "auth": lambda k: {"Authorization": f"Bearer {k}"},
        # input_type=document — саме той режим, у якому провайдер радить
        # ембедити те, що потім шукають (у нас це записи банку).
        "body": lambda texts: {"model": "voyage-4", "input": texts,
                               "input_type": "document"},
        "parse": lambda d: [row["embedding"] for row in d["data"]],
        "batched": True,
    },
    "Google · gemini-embedding-001": {
        "env": ["GEMINI_KEY", "GEMINI_API_KEY"],
        "url": ("https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-embedding-001:embedContent"),
        "auth": lambda k: {"x-goog-api-key": k},
        # Gemini бере по одному тексту на запит — тож batched=False, і
        # обгортка кличе його в циклі.
        "body": lambda text: {"content": {"parts": [{"text": text}]}},
        "parse": lambda d: [d["embedding"]["values"]],
        "batched": False,
    },
}


def _key(name):
    """Ключ провайдера — з першої змінної, яка справді заповнена."""
    for env in PROVIDERS[name]["env"]:
        val = (os.environ.get(env) or "").strip()
        if val:
            return val
    return None


def available():
    """Провайдери, ключ яких справді лежить у середовищі."""
    return [n for n in PROVIDERS if _key(n)]


def _texts(cur, ids):
    """Те, що йде в ембединг: назва + предмет. Саме ця пара несе смисл
    обіцянки; цитата тягне за собою слова журналіста й зашумила б порівняння."""
    cur.execute("SELECT id, coalesce(title, ''), coalesce(subject, '') "
                "FROM commitments WHERE id = ANY(%s)", (list(ids),))
    return {r[0]: (r[1] + ". " + r[2]).strip(". ") for r in cur.fetchall()}


def _embed(name, texts):
    import requests

    p = PROVIDERS[name]
    key = _key(name)
    headers = {"Content-Type": "application/json", **p["auth"](key)}
    if p["batched"]:
        r = requests.post(p["url"], headers=headers,
                          json=p["body"](texts), timeout=60)
        r.raise_for_status()
        return p["parse"](r.json())
    out = []
    for t in texts:
        r = requests.post(p["url"], headers=headers,
                          json=p["body"](t), timeout=60)
        r.raise_for_status()
        out.extend(p["parse"](r.json()))
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
    """Порахувати обидва списки. Нічого не пише — це замір."""
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
    """Чи існує поріг, який розділяє списки. Це і є відповідь на питання
    «чи вирішують ембединги задачу», і вона або так, або ні — середні
    значення тут нічого не варті."""
    same = [r["sim"] for r in rows if r["kind"] == "same"]
    apart = [r["sim"] for r in rows if r["kind"] == "apart"]
    if not same or not apart:
        return None
    lo, hi = min(same), max(apart)
    return {"min_same": lo, "max_apart": hi, "gap": lo - hi, "separates": lo > hi}
