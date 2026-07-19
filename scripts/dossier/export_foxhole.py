#!/usr/bin/env python3
"""Вигрузка статей із «лисячої нори» (Postgres бота) по темі — крок 1
глибокого досьє (скіл deep-dossier). Запускається на GitHub-раннері, бо
з пісочниці Claude Code прямого TCP до Railway немає (egress-проксі глушить).

env:
  FOXHOLE_DB_URL — connection string Railway Postgres (публічний)
  TOPIC_TSQUERY  — вираз to_tsquery('simple', ...), напр. "Кім:* | Ким:*"
  OUT_PREFIX     — префікс вихідних файлів (дефолт dossier_export/topic)
"""
import gzip
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DB_URL = os.environ["FOXHOLE_DB_URL"]
TSQUERY = os.environ.get("TOPIC_TSQUERY", "")
OUT_PREFIX = os.environ.get("OUT_PREFIX", "dossier_export/topic")
# для map-фази вистачає ~12к симв.: згадка майже завжди в перших екранах
TEXT_CAP = int(os.environ.get("TEXT_CAP", "12000"))


def main():
    if not TSQUERY.strip():
        print("TOPIC_TSQUERY порожній", file=sys.stderr)
        return 2
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT count(*) AS n FROM articles")
    total = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM articles WHERE fts @@ to_tsquery('simple', %s)",
                (TSQUERY,))
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
            d = datetime.fromtimestamp(row["published"], tz=timezone.utc)
            title = (row["title_ua"] or row["title_ru"] or "").replace("\t", " ")
            slug = (row["slug"] or "").strip()
            # канонічний URL сайту — з категорією: /news/{category}/{slug|id}
            cat = (row.get("category") or "").strip()
            tail = slug or str(row["id"])
            url = f"https://nikvesti.com/news/{cat}/{tail}" if cat else f"https://nikvesti.com/news/{tail}"
            fi.write(f"{row['id']}\t{d:%Y-%m-%d}\t{title}\t{url}\n")
            n += 1
    print(f"exported {n} articles -> {ndjson_path}, {index_path}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
