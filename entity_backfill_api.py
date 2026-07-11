#!/usr/bin/env python3
"""Разовий бэкфіл сутнісного шару через Anthropic Batch API (шлях А, §3.3.1).

Канал А з плану: коли Max-канал занадто повільний для обсягу, женемо Haiku 4.5
через Message Batches API (−50% до ціни). Пише в ту саму нору тим самим
злиттям (entity_pipeline.write) — тобто це просто ІНШИЙ спосіб отримати
results.json, далі все як з Max-каналом.

Дизайн (вища якість, ніж Max-пачки по 25):
- ОДНА стаття на запит → фізична ізоляція, контекст-блид неможливий.
- Структурований вивід (json_schema) → строгий JSON без парс-помилок.
- Промпт-правила беруться з entity_extract_prompt.md (єдине джерело таксономії),
  системний блок кешується (prompt caching) — дешевше на батчі.
- Модель — claude-haiku-4-5 (та сама, що піде в прод-інкремент).

Env:
    NORA_URL           — Postgres нори (як у entity_pipeline)
    ANTHROPIC_API_KEY  — ключ з поповненим балансом

Команди:
    python3 entity_backfill_api.py count 2022-01-01 2027-01-01
        порахувати статті в діапазоні + оцінка вартості батчем (перед оплатою).

    python3 entity_backfill_api.py run 2022-01-01 2027-01-01 results.json
        вивантажити статті діапазону, прогнати через Batch API, зібрати
        results.json. Далі: python3 entity_pipeline.py write results.json
        (без batch-аргументу — курсор id-walk тут не рухаємо, це діапазонний
        бэкфіл; злиття тегів/дублів — як завжди, ідемпотентне).

Ціна Haiku 4.5 батчем: $0.50/1M вх, $2.50/1M вих (−50% від $1.00/$5.00).
"""

import sys
import os
import re
import json
import time

import entity_pipeline as ep  # спільні: connect(), TEXT_CAP, ALLOWED_*

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 2000
# Ліміти Batch API: 100k запитів АБО 256 МБ тіла на батч. Вузьке місце — байти
# (кожен запит несе системний промпт + json_schema + текст статті), тому чанкуємо
# за розміром із запасом.
BATCH_MAX_REQUESTS = 90000
BATCH_MAX_BYTES = 150 * 1024 * 1024
REQ_OVERHEAD = 2500  # json_schema + обгортка запиту, байт
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "entity_extract_prompt.md")

# Оцінка токенів (для прогнозу вартості): ~1.2к вх (текст) + ~0.2к вих на статтю.
EST_IN_PER_ART = 1200
EST_OUT_PER_ART = 200
PRICE_IN = 0.50 / 1_000_000   # батч
PRICE_OUT = 2.50 / 1_000_000

# Строга схема виводу (json_schema) — Haiku 4.5 підтримує structured outputs.
ENTITY_ITEM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(ep.ALLOWED_KINDS)},
        "subtype": {"type": ["string", "null"]},
        "name_ua": {"type": ["string", "null"]},
        "name_ru": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"]},
        "salience": {"type": "string", "enum": sorted(ep.ALLOWED_SALIENCE)},
    },
    "required": ["kind", "subtype", "name_ua", "name_ru", "role", "salience"],
    "additionalProperties": False,
}
ARTICLE_OUT = {
    "type": "object",
    "properties": {
        "article_id": {"type": "integer"},
        "entities": {"type": "array", "items": ENTITY_ITEM},
    },
    "required": ["article_id", "entities"],
    "additionalProperties": False,
}


def get_system_prompt():
    """Лінива ініціалізація системного промпту (кешується в глобалі) —
    щоб модуль можна було імпортувати з бота без виклику main()."""
    global SYSTEM_PROMPT
    if SYSTEM_PROMPT is None:
        SYSTEM_PROMPT = load_system_prompt()
    return SYSTEM_PROMPT


def load_system_prompt():
    """Правила таксономії з entity_extract_prompt.md (єдине джерело), очищені
    від markdown-цитатних '> ', + примітка про режим однієї статті."""
    with open(PROMPT_FILE, encoding="utf-8") as f:
        raw = f.read()
    # беремо тіло промпту (рядки-цитати '> …'), знімаємо маркер
    lines = [re.sub(r"^> ?", "", ln) for ln in raw.splitlines() if ln.startswith(">")]
    body = "\n".join(lines).strip()
    body += ("\n\nРЕЖИМ ЦЬОГО ЗАПИТУ: вхід — РІВНО ОДНА стаття (об'єкт з "
             "id/title_ua/title_ru/text_ua/text_ru). Поверни JSON-об'єкт "
             "{\"article_id\": <id>, \"entities\": [...]} лише для неї. "
             "Правило ізоляції виконується автоматично — інших статей у запиті немає.")
    return body


def fetch_range(from_date, to_date):
    conn = ep.connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, published, title_ua, title_ru, text_ua, text_ru
        FROM articles
        WHERE published >= extract(epoch FROM %s::date)
          AND published <  extract(epoch FROM %s::date)
        ORDER BY published DESC
        """,
        (from_date, to_date),
    )
    arts = []
    for aid, pub, tua, tru, xua, xru in cur.fetchall():
        arts.append({
            "id": aid,
            "title_ua": tua,
            "title_ru": tru,
            "text_ua": (xua or "")[:ep.TEXT_CAP] or None,
            "text_ru": (xru or "")[:ep.TEXT_CAP] or None,
        })
    cur.close()
    conn.close()
    return arts


def chunk_articles(arts, system_len):
    """Розбити статті на чанки під ліміти Batch API (256 МБ / 100к запитів);
    рахуємо байти: системний промпт іде В КОЖНОМУ запиті."""
    chunks, cur_chunk, cur_bytes = [], [], 0
    for a in arts:
        sz = system_len + REQ_OVERHEAD + len(
            json.dumps(a, ensure_ascii=False).encode("utf-8"))
        if cur_chunk and (cur_bytes + sz > BATCH_MAX_BYTES
                          or len(cur_chunk) >= BATCH_MAX_REQUESTS):
            chunks.append(cur_chunk)
            cur_chunk, cur_bytes = [], 0
        cur_chunk.append(a)
        cur_bytes += sz
    if cur_chunk:
        chunks.append(cur_chunk)
    return chunks


def cmd_count(from_date, to_date):
    arts = fetch_range(from_date, to_date)
    n = len(arts)
    est_in = n * EST_IN_PER_ART
    est_out = n * EST_OUT_PER_ART
    cost = est_in * PRICE_IN + est_out * PRICE_OUT
    n_chunks = len(chunk_articles(arts, len(get_system_prompt().encode("utf-8")))) if arts else 0
    print(f"статей у {from_date}..{to_date}: {n}")
    print(f"оцінка (батч Haiku 4.5): ~{est_in/1e6:.0f}M вх + ~{est_out/1e6:.0f}M вих")
    print(f"≈ ${cost:.0f} (без урахування prompt-cache — по факту дешевше)")
    print(f"батчів (ліміт 256МБ/100к): {n_chunks}")


def _make_request(client, art):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    return Request(
        custom_id=str(art["id"]),
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": get_system_prompt(),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": json.dumps(art, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": ARTICLE_OUT}},
        ),
    )


def cmd_run(from_date, to_date, outpath):
    import anthropic
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY з env
    arts = fetch_range(from_date, to_date)
    print(f"статей: {len(arts)}")
    if not arts:
        print("нема чого робити")
        return

    results = []
    n_err = 0
    # чанкуємо під ліміти батчу (256 МБ тіла / 100к запитів)
    chunks = chunk_articles(arts, len(get_system_prompt().encode("utf-8")))
    print(f"батчів: {len(chunks)}")
    for chunk in chunks:
        requests = [_make_request(client, a) for a in chunk]
        batch = client.messages.batches.create(requests=requests)
        print(f"батч {batch.id}: {len(chunk)} запитів, чекаю…")
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            rc = b.request_counts
            print(f"  {b.processing_status}: processing={rc.processing} "
                  f"succeeded={rc.succeeded} errored={rc.errored}")
            time.sleep(30)
        # збираємо результати (порядок довільний → ключ по custom_id)
        for res in client.messages.batches.results(batch.id):
            if res.result.type != "succeeded":
                n_err += 1
                continue
            msg = res.result.message
            text = next((bl.text for bl in msg.content if bl.type == "text"), None)
            if not text:
                n_err += 1
                continue
            try:
                obj = json.loads(text)
            except Exception:
                n_err += 1
                continue
            # довіряємо custom_id як джерелу article_id
            obj["article_id"] = int(res.custom_id)
            results.append({"article_id": obj["article_id"],
                            "entities": obj.get("entities", [])})
        print(f"  батч оброблено, всього зібрано {len(results)}, помилок {n_err}")

    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"готово: {len(results)} статей → {outpath} (помилок: {n_err})")
    print(f"далі: python3 entity_pipeline.py write {outpath}")


SYSTEM_PROMPT = None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "count":
        cmd_count(sys.argv[2], sys.argv[3])
    elif cmd == "run":
        cmd_run(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
