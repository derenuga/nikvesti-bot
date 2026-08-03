#!/usr/bin/env python3
"""Витяг ЗОБОВ'ЯЗАНЬ із нори через Anthropic API (банк тем, docs/PROMISES_BANK.md §7).

Той самий дизайн, що дав якість у сутнісному шарі (`entity_backfill_api.py`):
ОДНА стаття на запит (фізична ізоляція, контекст-блид неможливий), строгий
структурований вивід (json_schema), правила таксономії — з одного файлу
`promise_extract_prompt.md`, системний блок кешується.

**Окремий виклик, а не «заодно з сутностями».** Розмивання задачі вимірювано
псує витяг (`ENTITY_LAYER_PLAN.md` §3.3.1), тому промпт тут свій і модель
читає статтю вдруге. Це свідома плата.

**Пре-фільтр за маркерами.** Прямий прогін 28к статей 2024–2026 коштує ~$170,
тож перед прогоном відсіюємо статті, у яких немає жодного мовленнєвого маркера
майбутньої дії. Список маркерів навмисно ШИРОКИЙ (recall важливіший за
precision: пропущена обіцянка втрачена назавжди, зайва стаття коштує пів
цента), і його ціна вимірюється, а не вгадується — див. `MARKERS` нижче і
крок 7 §7 плану.

Ціна Haiku 4.5 батчем: $0.50/1M вх, $2.50/1M вих (−50% від звичайних).
"""

import json
import os
import re
from datetime import datetime

import entity_pipeline as ep          # connect(), TEXT_CAP
import promise_pipeline as pp         # схема банку тем
from entity_backfill_api import chunk_articles   # ті самі ліміти Batch API

MODEL = "claude-haiku-4-5"
# Кап виводу з тим самим запасом, що у витягу сутностей: платимо за фактичний
# вихід, а обрив на капі дає непарсибельний JSON — двічі наступали на це в
# сутнісному шарі (див. коментар до MAX_TOKENS в entity_backfill_api).
MAX_TOKENS = 8000

PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "promise_extract_prompt.md")

# Оцінка токенів — НАВМИСНО ЗАВИЩЕНА, поки немає заміру.
#
# Вхід = текст статті (~3.0к, як у витягу сутностей) + системний промпт, а він
# тут утричі більший за сутнісний (19 КБ проти 5.5 КБ: сім розборів дали сім
# правил, і кожне коштує рядків). Кешування системного блоку зробить реальну
# цифру помітно нижчою, але ЦЕ ЧИСЛО Олег бачить перед тим, як натиснути
# «запустити», тож помилятись воно має в бік дорожче, а не дешевше.
# Уточнити після першого місяця: фактична вартість друкується у звіті
# /promise_scan.
EST_IN_PER_ART = 6000
EST_OUT_PER_ART = 400
PRICE_IN_BATCH = 0.50 / 1_000_000
PRICE_OUT_BATCH = 2.50 / 1_000_000
PRICE_IN = 1.00 / 1_000_000     # звичайний API (/promise_test, /promise_retest)
PRICE_OUT = 5.00 / 1_000_000

# ---------- Пре-фільтр: маркери мовленнєвого акту ----------
#
# Це ПРЕФІКСИ лексем для to_tsquery('simple', 'обіця:* | плану:* | …') — нора
# індексується без стемера, морфологія закривається префіксом (як у
# archive_search._stem).
#
# Список широкий свідомо. Головне правило витягу — «лови мовленнєвий акт, а не
# слово "обіцяв"» (§2.5), і фільтр не має права бути вужчим за нього: у
# еталонній статті 321833 слова «обіц» немає жодного разу, а зобов'язань там
# чотири — ловиться вона по «розроб», «плану», «розпоряд».
#
# Російські форми потрібні не менше за українські: до ~2023 сайт писав
# російською, тобто ВЕСЬ старий архів — а саме там лежать обіцянки, строк яких
# уже минув.
MARKERS = [
    # українська: зобов'язання прямо
    "обіця", "пообіця", "обіцян", "гаранту", "гарантува", "зобов", "запевн",
    # українська: планова й безособова форма — саме так влада вуалює
    "плану", "заплан", "передбач", "розпоряд", "доруч", "намір",
    # українська: дія, про яку зазвичай і йдеться
    "розроб", "заверш", "закінч", "перенес", "перенос", "введ", "відкри",
    "запуст", "розпочн", "розпочат", "збуду", "побуду", "відбуду", "будува",
    "збудова", "відремонт", "ремонтув", "капремонт", "реконструкц",
    "відновл", "створ", "виділ", "профінанс", "здійсн", "запровад",
    "модерніз", "експлуатац",
    # російська (архів до ~2023)
    "обеща", "пообеща", "гаранти", "обязу", "обязал", "планиру", "предусм",
    "разработ", "восстанов", "постро", "отремонт", "откро", "откры",
    "созда", "выдел", "намер", "приступ", "эксплуатац", "заверши", "перенос",
]


def marker_tsquery():
    """OR-запит префіксних лексем для articles.fts."""
    return " | ".join(f"{m}:*" for m in sorted(set(MARKERS)))


# ---------- Схема виводу ----------

COMMITMENT_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subject": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"}},
        "promiser": {"type": ["string", "null"]},
        "promiser_role": {"type": ["string", "null"]},
        "owner": {"type": ["string", "null"]},
        "reported_by": {"type": ["string", "null"]},
        # УВАГА: `enum` стоїть лише на полях, які НЕ можуть бути null.
        #
        # Пара «"type": ["string","null"] + enum з null у списку» виглядає
        # природно і є валідним JSON Schema, але структурований вивід Anthropic
        # її відкидає: 400 invalid_request_error, «Enum value 'media' does not
        # match declared type». Прод-схема витягу сутностей (entity_backfill_api)
        # тримається того самого правила — там `subtype` теж має словник, і теж
        # без enum.
        #
        # Словник для таких полів живе у промпті й у `description` нижче, а в
        # базу його стереже pp._enum(): значення поза таксономією стає NULL, а
        # не потрапляє в реєстр мовчки.
        "audience": {"type": ["string", "null"],
                     "description": "media | community | group, або null"},
        "polarity": {"type": "string", "enum": list(pp.POLARITY)},
        "modality": {"type": "string", "enum": list(pp.MODALITY)},
        "source_type": {"type": "string", "enum": list(pp.SOURCE_TYPE)},
        "deadline": {"type": ["string", "null"],
                     "description": "YYYY-MM-DD — остання дата періоду, або null"},
        "deadline_precision": {
            "type": ["string", "null"],
            "description": "day | month | quarter | year | vague, або null"},
        "criterion": {"type": ["string", "null"]},
        "verification_method": {
            "type": ["string", "null"],
            "description": "field_check | document_request | official_statement "
                           "| data, або null"},
        "condition": {"type": ["string", "null"]},
        "condition_self_judged": {"type": "boolean"},
        "trigger_event": {"type": ["string", "null"]},
        "actor_hidden": {"type": "boolean"},
        "framed_as_promise": {"type": "boolean"},
        "based_on_document": {"type": ["string", "null"]},
        "amount": {"type": ["number", "null"]},
        "quote": {"type": "string"},
    },
    "required": ["title", "subject", "objects", "promiser", "promiser_role",
                 "owner", "reported_by", "audience", "polarity", "modality",
                 "source_type", "deadline", "deadline_precision", "criterion",
                 "verification_method", "condition", "condition_self_judged",
                 "trigger_event", "actor_hidden", "framed_as_promise",
                 "based_on_document", "amount", "quote"],
    "additionalProperties": False,
}

ARTICLE_OUT = {
    "type": "object",
    "properties": {
        "article_id": {"type": "integer"},
        "commitments": {"type": "array", "items": COMMITMENT_ITEM},
    },
    "required": ["article_id", "commitments"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = None


def load_system_prompt():
    """Правила з promise_extract_prompt.md (єдине джерело таксономії), очищені
    від markdown-цитатних '> '."""
    with open(PROMPT_FILE, encoding="utf-8") as f:
        raw = f.read()
    lines = [re.sub(r"^> ?", "", ln) for ln in raw.splitlines() if ln.startswith(">")]
    body = "\n".join(lines).strip()
    body += ("\n\nРЕЖИМ ЦЬОГО ЗАПИТУ: вхід — РІВНО ОДНА стаття. Поверни "
             "JSON-об'єкт {\"article_id\": <id>, \"commitments\": [...]} лише "
             "для неї. Порожній список — нормальний результат.")
    return body


def get_system_prompt():
    global SYSTEM_PROMPT
    if SYSTEM_PROMPT is None:
        SYSTEM_PROMPT = load_system_prompt()
    return SYSTEM_PROMPT


# ---------- Вибірка статей ----------

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця",
            "субота", "неділя"]


def article_payload(row):
    """Рядок нори → вхід моделі.

    `published` і `weekday` передаються ОБОВ'ЯЗКОВО: без них відносний
    горизонт («за вихідні», «до кінця тижня») розв'язати неможливо, і
    найшвидша з усіх обіцянок — три дні строку — провалилась би в «без
    дати» (§2.3).

    Рядок без колонок тексту (META_COLS) дає ту саму форму без `text_*` —
    цього досить для зшивання ланцюга й судді, і не тягне в пам'ять мегабайти
    текстів, які вже нікуди не поїдуть.
    """
    aid, published, tua, tru = row[0], row[1], row[2], row[3]
    xua = row[4] if len(row) > 4 else None
    xru = row[5] if len(row) > 5 else None
    text_ua = (xua or "")[:ep.TEXT_CAP] or None
    text_ru = (xru or "")[:ep.TEXT_CAP] or None
    if text_ua and text_ru:
        text_ru = None      # одна мова достатня, як у витягу сутностей
    dt = datetime.fromtimestamp(int(published)) if published else None
    return {
        "id": aid,
        "published": dt.strftime("%Y-%m-%d") if dt else None,
        "weekday": WEEKDAYS[dt.weekday()] if dt else None,
        "title_ua": tua,
        "title_ru": tru,
        "text_ua": text_ua,
        "text_ru": text_ru,
    }


# Стаття «ще чекає витягу». Не просто «не done»: стаття, чий витяг УПАВ,
# лишається в черзі й повертається наступним скану — саме цього не робила
# курсорна схема сутностей, і дірка від збою не затягувалась ніколи. Але
# вічно крутити одну биту ноду теж не можна, звідси стеля спроб.
PENDING_COND = ("NOT EXISTS (SELECT 1 FROM promise_attempts t "
                " WHERE t.article_id = a.id AND (t.done OR t.attempts >= %s))")

SELECT_COLS = "a.id, a.published, a.title_ua, a.title_ru, a.text_ua, a.text_ru"
# Те саме без текстів: для лічильників, звітів і зшивання ланцюга сам текст не
# потрібен, а 28к статей по 8 КБ — це чверть гігабайта в пам'яті процесу бота.
META_COLS = "a.id, a.published, a.title_ua, a.title_ru"


def month_bounds(month):
    """'YYYY-MM' → ('YYYY-MM-01', 'YYYY-MM-01' наступного місяця)."""
    y, m = int(month[:4]), int(month[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"


def fetch_range(from_date, to_date, marked_only=True, only_missing=True,
                limit=None):
    """Статті діапазону до витягу зобов'язань.

    marked_only — пре-фільтр за маркерами (див. MARKERS).
    only_missing — пропускати ті, що вже пройшли витяг: повторний прогін не
    платить удруге. Свідомий перечит — це /promise_retest, не скан.

    Повертає (articles, stats), де stats — {total, marked, skipped}: саме з
    цих чисел рахується і оцінка вартості, і ціна пре-фільтра.
    """
    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(pp.DDL)
            window = ("a.published >= extract(epoch FROM %s::date) AND "
                      "a.published <  extract(epoch FROM %s::date)")
            params = [from_date, to_date]
            cur.execute(f"SELECT count(*) FROM articles a WHERE {window}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM articles a WHERE {window} "
                "AND a.fts @@ to_tsquery('simple', %s)",
                params + [marker_tsquery()])
            marked = cur.fetchone()[0]

            where = [window]
            sel_params = list(params)
            if marked_only:
                where.append("a.fts @@ to_tsquery('simple', %s)")
                sel_params.append(marker_tsquery())
            if only_missing:
                where.append(PENDING_COND)
                sel_params.append(pp.MAX_ATTEMPTS)
            sql = (f"SELECT {SELECT_COLS} FROM articles a "
                   f"WHERE {' AND '.join(where)} ORDER BY a.published")
            if limit:
                sql += " LIMIT %s"
                sel_params.append(int(limit))
            cur.execute(sql, sel_params)
            arts = [article_payload(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return arts, {"total": total, "marked": marked,
                  "skipped": (marked if marked_only else total) - len(arts)}


def count_range(from_date, to_date, marked_only=True, only_missing=True):
    """Скільки статей у діапазоні / з маркерами / чекають витягу — САМІ ЧИСЛА.

    Окремо від fetch_range саме тому, що оцінка на роках («скільки коштуватиме
    2024–2026») інакше вивантажувала б у пам'ять тексти 28 тисяч статей, щоб
    порахувати їх довжину списку.
    """
    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(pp.DDL)
            window = ("a.published >= extract(epoch FROM %s::date) AND "
                      "a.published <  extract(epoch FROM %s::date)")
            params = [from_date, to_date]
            cur.execute(f"SELECT count(*) FROM articles a WHERE {window}", params)
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM articles a WHERE {window} "
                "AND a.fts @@ to_tsquery('simple', %s)", params + [marker_tsquery()])
            marked = cur.fetchone()[0]
            where = [window]
            sel = list(params)
            if marked_only:
                where.append("a.fts @@ to_tsquery('simple', %s)")
                sel.append(marker_tsquery())
            if only_missing:
                where.append(PENDING_COND)
                sel.append(pp.MAX_ATTEMPTS)
            cur.execute(f"SELECT count(*) FROM articles a WHERE {' AND '.join(where)}", sel)
            pending = cur.fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "marked": marked, "pending": pending,
            "skipped": (marked if marked_only else total) - pending}


def fetch_ids(ids, with_text=True):
    """Конкретні статті за id — для /promise_test і /promise_retest."""
    cols = SELECT_COLS if with_text else META_COLS
    conn = ep.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM articles a "
                        "WHERE a.id = ANY(%s) ORDER BY a.published",
                        ([int(i) for i in ids],))
            return [article_payload(r) for r in cur.fetchall()]
    finally:
        conn.close()


def marked_ids(cur, ids):
    """Які з цих статей проходять пре-фільтр — ТИМ САМИМ запитом, що й скан.

    Не підрядком по тексту: фільтр працює через tsvector нори, і друга,
    «приблизна» реалізація рано чи пізно розійшлася б із першою — а від цього
    числа залежить рішення, вмикати фільтр на роках чи ні (§7 крок 7).
    """
    ids = [int(i) for i in ids if i]
    if not ids:
        return set()
    cur.execute("SELECT id FROM articles WHERE id = ANY(%s) "
                "AND fts @@ to_tsquery('simple', %s)", (ids, marker_tsquery()))
    return {r[0] for r in cur.fetchall()}


# ---------- Запити до моделі ----------

def request_params():
    """Спільне тіло запиту — однакове для батча і для звичайного виклику,
    щоб /promise_test і масовий скан не розійшлись у поведінці."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": get_system_prompt(),
                    "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": ARTICLE_OUT}},
    }


def _make_request(client, art):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=str(art["id"]),
        params=MessageCreateParamsNonStreaming(
            messages=[{"role": "user",
                       "content": json.dumps(art, ensure_ascii=False)}],
            **request_params(),
        ),
    )


def extract_one(client, art):
    """Витяг однієї статті звичайним API. Повертає (commitments, usage)."""
    resp = client.messages.create(
        messages=[{"role": "user", "content": json.dumps(art, ensure_ascii=False)}],
        **request_params(),
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(
            f"вихід обрізано на max_tokens={MAX_TOKENS} — JSON недописаний")
    text = next((bl.text for bl in resp.content if bl.type == "text"), None)
    obj = json.loads(text)
    return obj.get("commitments", []), resp.usage


def estimate(n, batch=True):
    """(вартість, вх. токени, вих. токени) для n статей."""
    tin, tout = n * EST_IN_PER_ART, n * EST_OUT_PER_ART
    pin, pout = (PRICE_IN_BATCH, PRICE_OUT_BATCH) if batch else (PRICE_IN, PRICE_OUT)
    return tin * pin + tout * pout, tin, tout


__all__ = ["MODEL", "MARKERS", "marker_tsquery", "fetch_range", "count_range",
           "fetch_ids", "marked_ids", "extract_one", "estimate", "chunk_articles",
           "get_system_prompt", "month_bounds", "ARTICLE_OUT", "_make_request"]
