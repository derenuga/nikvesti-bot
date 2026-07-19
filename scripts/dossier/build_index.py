#!/usr/bin/env python3
"""Крок 6a: повний індекс-файл усіх матеріалів про фігуранта (§2.1 плану:
100% покриття, 0 токенів). Markdown, хронологічно по роках.

env:
  WORKDIR — тека з subject_all.json / all_classified.json
  SUBJECT — ім'я фігуранта (для заголовка)
  OUT     — шлях вихідного .md
"""
import html
import json
import os
from collections import defaultdict

WORKDIR = os.environ["WORKDIR"]
SUBJECT = os.environ["SUBJECT"]
OUT = os.environ["OUT"]

subj = json.load(open(f"{WORKDIR}/subject_all.json"))
allc = json.load(open(f"{WORKDIR}/all_classified.json"))
subj.sort(key=lambda r: r["date"])

by_year = defaultdict(list)
for r in subj:
    by_year[r["date"][:4]].append(r)

lines = [f"# Індекс матеріалів МикВісті про {SUBJECT}\n"]
lines.append(f"Повний хронологічний перелік. Матеріалів про {SUBJECT}: "
             f"**{len(subj)}** (з {len(allc)} згадок за пошуковим запитом; "
             f"решта — однофамільці, тезки та збіги). ⭐ — ключові матеріали.\n")
for y in sorted(by_year):
    items = by_year[y]
    lines.append(f"\n## {y} ({len(items)})\n")
    for r in items:
        star = " ⭐" if r.get("significance", 0) >= 4 else ""
        title = html.unescape((r["title"] or "(без заголовка)").strip())
        lines.append(f"- {r['date']} — [{title}]({r['url']}){star}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"index {len(subj)} матеріалів -> {OUT}")
