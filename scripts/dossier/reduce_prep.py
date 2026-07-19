#!/usr/bin/env python3
"""Крок 4 глибокого досьє: зводить мікрофакти map-фази, лишає тільки статті
про фігуранта (person=="subject") і адаптивно ділить їх на періоди для
reduce-фази (пакує суміжні роки в <=5 кошиків приблизно рівного розміру).

env:
  FACTSDIR — де лежать facts/batch_NN.json (вихід map-агентів)
  WORKDIR  — робоча тека (сюди пишуться subject_all.json, all_classified.json,
             periods/pN.json, periods_meta.json)
  MAXP     — максимум періодів (дефолт 5)
"""
import glob
import json
import os
from collections import Counter, defaultdict

FACTSDIR = os.environ["FACTSDIR"]
WORKDIR = os.environ["WORKDIR"]
MAXP = int(os.environ.get("MAXP", "5"))

rows = []
for f in sorted(glob.glob(f"{FACTSDIR}/batch_*.json")):
    for r in json.load(open(f)):
        rows.append(r)

# дедуп по id
seen = {}
for r in rows:
    seen[r["id"]] = r
rows = list(seen.values())
rows.sort(key=lambda r: r["date"])


def year(r):
    return r["date"][:4]


subj = [r for r in rows if r.get("person") == "subject"]
person = Counter(r.get("person", "?") for r in rows)

# адаптивні періоди: суміжні роки в <=MAXP кошиків ~рівного розміру
by_year = defaultdict(list)
for r in subj:
    by_year[year(r)].append(r)
yrs = sorted(by_year)
total = len(subj)
NP = min(MAXP, max(1, len(yrs)))
target = max(1, total / NP) if NP else total

periods, cur, cur_n = [], [], 0
for y in yrs:
    cur.append(y)
    cur_n += len(by_year[y])
    if cur_n >= target and len(periods) < NP - 1:
        periods.append(cur)
        cur, cur_n = [], 0
if cur:
    periods.append(cur)

os.makedirs(f"{WORKDIR}/periods", exist_ok=True)
os.makedirs(f"{WORKDIR}/narrative", exist_ok=True)
meta = []
for idx, yl in enumerate(periods, 1):
    sub = [r for y in yl for r in by_year[y]]
    name = f"{yl[0]}–{yl[-1]}" if yl[0] != yl[-1] else yl[0]
    json.dump(sub, open(f"{WORKDIR}/periods/p{idx}.json", "w"), ensure_ascii=False)
    meta.append({"idx": idx, "name": name, "n": len(sub)})

json.dump(subj, open(f"{WORKDIR}/subject_all.json", "w"), ensure_ascii=False)
json.dump(rows, open(f"{WORKDIR}/all_classified.json", "w"), ensure_ascii=False)
json.dump(meta, open(f"{WORKDIR}/periods_meta.json", "w"), ensure_ascii=False)

print(f"total={len(rows)} person={dict(person)}")
print(f"subject={len(subj)} periods={len(periods)}")
for p in meta:
    print(f"  p{p['idx']} {p['name']}: {p['n']} статей")
