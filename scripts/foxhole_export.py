#!/usr/bin/env python3
"""Разова вигрузка статей із «лисячої нори» (Postgres бота) по темі — для
прототипу глибокого досьє (ENTITY_LAYER_PLAN §2.2, «верстат для Олега»).

Запускається з GitHub Actions (звідти є прямий TCP до Railway Postgres).
Читає БД тільки SELECT-ами, результат пише в gzip-NDJSON + індекс TSV.

env:
  FOXHOLE_DB_URL — публічний connection string Railway Postgres
  TOPIC_TSQUERY  — вираз to_tsquery('simple', ...), напр. "Воронов:*"
  OUT_PREFIX     — префікс вихідних файлів (дефолт dossier_export)
"""
import gzip
import json
import os
import sys

import psycopg2
import psycopg2.extras

DB_URL = os.environ["FOXHOLE_DB_URL"]
TSQUERY = os.environ.get("TOPIC_TSQUERY", "Воронов:*")
OUT_PREFIX = os.environ.get("OUT_PREFIX", "dossier_export")

# Повний текст може сягати 60к симв.; для map-фази досьє вистачає 12к —
# довші тексти (лонгріди) ріжемо, згадка майже завжди в перших екранах.
TEXT_CAP = 12000


def main():
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT count(*) AS n FROM articles")
    total = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT count(*) AS n FROM articles
        WHERE fts @@ to_tsquery('simple', %s)
        """,
        (TSQUERY,),
    )
    matched = cur.fetchone()["n"]
    print(f"articles total={total} matched({TSQUERY})={matched}", flush=True)

    cur.execute(
        """
        SELECT id, published, own_material, category, region, tags_text,
               title_ua, title_ru, slug,
               left(text_ua, %s) AS text_ua,
               left(text_ru, %s) AS text_ru,
               ts_rank(fts, to_tsquery('simple', %s)) AS rank
        FROM articles
        WHERE fts @@ to_tsquery('simple', %s)
        ORDER BY published ASC
        """,
        (TEXT_CAP, TEXT_CAP, TSQUERY, TSQUERY),
    )

    ndjson_path = f"{OUT_PREFIX}.ndjson.gz"
    index_path = f"{OUT_PREFIX}_index.tsv"
    n = 0
    with gzip.open(ndjson_path, "wt", encoding="utf-8") as fj, open(
        index_path, "w", encoding="utf-8"
    ) as fi:
        fi.write("id\tdate\ttitle\turl\n")
        for row in cur:
            row = dict(row)
            row["rank"] = float(row["rank"])
            fj.write(json.dumps(row, ensure_ascii=False) + "\n")
            from datetime import datetime, timezone

            d = datetime.fromtimestamp(row["published"], tz=timezone.utc)
            title = (row["title_ua"] or row["title_ru"] or "").replace("\t", " ")
            fi.write(
                f"{row['id']}\t{d:%Y-%m-%d}\t{title}\t"
                f"https://nikvesti.com/news/{row['slug']}\n"
            )
            n += 1
    print(f"exported {n} articles -> {ndjson_path}, {index_path}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
