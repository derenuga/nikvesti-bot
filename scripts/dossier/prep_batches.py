#!/usr/bin/env python3
"""Крок 2 глибокого досьє: з вигрузки нори (ndjson.gz) робить пачки статей
для map-фази. Кожна пачка — JSON-масив {id, date, title, url, text}.

env:
  SRC      — шлях до <prefix>.ndjson.gz
  OUTDIR   — куди класти batch_NN.json
  BATCH    — статей у пачці (дефолт 25)
  TEXT_CAP — обрізати текст статті до N симв. (дефолт 7000)
"""
import gzip
import json
import os
from collections import Counter
from datetime import datetime, timezone

SRC = os.environ["SRC"]
OUTDIR = os.environ["OUTDIR"]
BATCH = int(os.environ.get("BATCH", "25"))
TEXT_CAP = int(os.environ.get("TEXT_CAP", "7000"))
os.makedirs(OUTDIR, exist_ok=True)

arts = []
years = Counter()
for line in gzip.open(SRC, "rt", encoding="utf-8"):
    r = json.loads(line)
    d = datetime.fromtimestamp(r["published"], tz=timezone.utc)
    text = r.get("text_ua") or r.get("text_ru") or ""
    title = r.get("title_ua") or r.get("title_ru") or ""
    slug = (r.get("slug") or "").strip()
    # канонічний URL сайту — з категорією: /news/{category}/{slug|id}
    cat = (r.get("category") or "").strip()
    tail = slug or str(r["id"])
    url = f"https://nikvesti.com/news/{cat}/{tail}" if cat else f"https://nikvesti.com/news/{tail}"
    years[d.year] += 1
    arts.append({"id": r["id"], "date": f"{d:%Y-%m-%d}", "title": title,
                 "url": url, "text": text[:TEXT_CAP]})

arts.sort(key=lambda a: a["date"])
nb = 0
for i in range(0, len(arts), BATCH):
    nb += 1
    json.dump(arts[i:i + BATCH], open(f"{OUTDIR}/batch_{nb:02d}.json", "w"),
              ensure_ascii=False)

print(f"articles={len(arts)} batches={nb}")
print("by_year=" + json.dumps(dict(sorted(years.items()))))
