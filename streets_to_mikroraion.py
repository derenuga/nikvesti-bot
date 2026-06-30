#!/usr/bin/env python3
"""
Групування вулиць Миколаєва по мікрорайонах для Telegram-бота МикВісті.

ІДЕЯ: 735 вулиць у CSV розбиваються на ~50 унікальних комбінацій черг
відключення (outage_queue_raw). Жодна вулиця не зустрічається у двох
різних комбінаціях одночасно — отже комбінація черг = надійний
географічний кластер (бо черги прив'язані до фідерів/підстанцій, які
фізично обслуговують конкретну територію міста).

Замість геокодування 735 вулиць ми геокодуємо 1-3 "представники" з
кожного кластера (за допомогою Nominatim, з house-номером для точності),
визначаємо мікрорайон кластера і проставляємо його ВСІМ вулицям кластера.//
Це ~100-150 запитів до API замість 735.

ВАЖЛИВО: запускати цей скрипт потрібно з власного сервера/машини, а не
з пісочниці Claude — Nominatim блокує автоматизовані інструменти за
robots.txt, тому Claude не міг сам протестувати запити наживо.

Перед запуском встанови requests:
    pip install requests
"""

import csv
import json
import time
import sys
from collections import defaultdict, Counter
from pathlib import Path

import requests

# ---------- Налаштування ----------

CSV_PATH = "outage_streets_filial_15.csv"
CACHE_PATH = "nominatim_cache.json"
OUTPUT_STREETS = "streets_with_mikroraion.json"
OUTPUT_CLUSTERS = "queue_clusters_with_mikroraion.json"

# Скільки представників геокодувати з кожного кластера (1 — швидко,
# 3 — надійніше, бо мажоритарне голосування згладжує помилки геокодера)
SAMPLES_PER_CLUSTER = 3

# Nominatim вимагає коректний User-Agent з контактом (правило usage policy)
HEADERS = {
    "User-Agent": "nikvesti-outage-bot/1.0 (contact: your-email@nikvesti.com)"
}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Список основних мікрорайонів Миколаєва — для довідки/перевірки
KNOWN_MIKRORAIONS = [
    "Центр", "Намив", "Соляні", "Варварівка", "Ліски", "Матвіївка",
    "ПТЗ", "Ракетне Урочище", "Темвод", "Новий Водопій", "Старий Водопій",
    "Північний", "Сухий Фонтан", "Корабельний район", "Інгульський район",
    "Заводський район", "Тернівка", "Широка Балка", "Балабанівка",
    "Селище Горького", "Велика Корениха", "Мала Корениха", "Кульбакине",
    "Слобідка", "Ялти",
]


# ---------- Крок 1: парсинг CSV ----------

def parse_csv(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            street = row["street_name"].strip()
            sample_dom = (row.get("sample_dom_name") or "").strip()
            if not street or street.upper() == "TEST":
                continue  # відкидаємо сміттєві рядки
            try:
                raw = json.loads(row["outage_queue_raw"])
                queues = tuple(sorted(set(q["outage"]["name"] for q in raw)))
            except Exception:
                queues = tuple()
            rows.append({"street": street, "sample_dom": sample_dom, "queues": queues})
    return rows


def cluster_by_queue(rows):
    by_q = defaultdict(list)
    for r in rows:
        by_q[r["queues"]].append(r)
    # сортуємо кластери від найбільшого до найменшого
    return dict(sorted(by_q.items(), key=lambda kv: -len(kv[1])))


# ---------- Крок 2: геокодування представників кластера ----------

def load_cache():
    if Path(CACHE_PATH).exists():
        return json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    Path(CACHE_PATH).write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def geocode_street(street, sample_dom, cache):
    """Повертає (suburb_or_neighbourhood, raw_address_dict) або (None, None)."""
    query_variants = []
    if sample_dom:
        query_variants.append(f"{street}, {sample_dom}, Миколаїв, Україна")
    query_variants.append(f"{street}, Миколаїв, Україна")

    for query in query_variants:
        if query in cache:
            cached = cache[query]
            if cached is not None:
                return cached.get("suburb"), cached
            continue

        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "addressdetails": 1,
                    "countrycodes": "ua",
                    "limit": 1,
                },
                headers=HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
        except Exception as e:
            print(f"  [помилка геокодування] {query}: {e}", file=sys.stderr)
            cache[query] = None
            time.sleep(1)
            continue

        time.sleep(1)  # Nominatim usage policy: максимум 1 запит/сек

        if not results:
            cache[query] = None
            continue

        address = results[0].get("address", {})
        # У OSM мікрорайон може лежати в різних полях залежно від того,
        # як саме його розмітили мапери — перевіряємо по черзі.
        # ВАЖЛИВО: city_district у Миколаєві — це один із 4 офіційних
        # адмінрайонів (Центральний/Заводський/Корабельний/Інгульський),
        # а не мікрорайон — він занадто широкий (наприклад, Матвіївка і
        # Соляні теж формально лежать у Центральному районі). Тому це
        # окремий, явно позначений фолбек, а не рівноцінна заміна suburb.
        precise = (
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("quarter")
        )
        city_district = address.get("city_district")
        if precise:
            suburb = precise
            granularity = "мікрорайон"
        elif city_district == "Центральний район":
            # історичне старе місто зазвичай не має окремого тегу
            # мікрорайону в OSM — це і є неформальний "Центр"
            suburb = "Центр"
            granularity = "приблизно (історичний центр, за замовчуванням)"
        elif city_district:
            suburb = city_district
            granularity = "район (грубо, не мікрорайон!)"
        else:
            suburb = None
            granularity = "немає даних"
        entry = {
            "suburb": suburb,
            "granularity": granularity,
            "address": address,
            "display_name": results[0].get("display_name"),
        }
        cache[query] = entry
        return suburb, entry

    return None, None


def label_clusters(clusters, cache):
    labeled = {}
    for queues, items in clusters.items():
        votes = Counter()
        details = []
        # беремо до SAMPLES_PER_CLUSTER представників (бажано з різними назвами)
        samples = items[:SAMPLES_PER_CLUSTER]
        for item in samples:
            suburb, info = geocode_street(item["street"], item["sample_dom"], cache)
            details.append({"street": item["street"], "suburb": suburb})
            if suburb:
                votes[suburb] += 1
            print(f"  {item['street']} -> {suburb}")

        best = votes.most_common(1)[0][0] if votes else None
        confidence = "висока" if votes and votes.most_common(1)[0][1] == len(samples) else (
            "середня" if votes else "немає даних — потрібна ручна перевірка"
        )
        labeled[queues] = {
            "mikroraion_guess": best,
            "confidence": confidence,
            "votes": dict(votes),
            "samples_checked": details,
            "count": len(items),
        }
    return labeled


# ---------- Main ----------

def main():
    print(f"Читаю {CSV_PATH}...")
    rows = parse_csv(CSV_PATH)
    print(f"Знайдено {len(rows)} вулиць (без сміттєвих рядків)")

    clusters = cluster_by_queue(rows)
    print(f"Згруповано у {len(clusters)} кластерів за чергами\n")

    cache = load_cache()
    print("Геокодую представників кожного кластера через Nominatim...")
    labeled = label_clusters(clusters, cache)
    save_cache(cache)

    # Збираємо фінальний маппінг вулиця -> мікрорайон
    streets_out = []
    for queues, items in clusters.items():
        label_info = labeled[queues]
        for item in items:
            streets_out.append({
                "street": item["street"],
                "mikroraion": label_info["mikroraion_guess"],
                "confidence": label_info["confidence"],
                "queue_signature": list(queues),
            })

    Path(OUTPUT_STREETS).write_text(
        json.dumps(streets_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    clusters_out = [
        {
            "queue_signature": list(q),
            "count": labeled[q]["count"],
            "mikroraion_guess": labeled[q]["mikroraion_guess"],
            "confidence": labeled[q]["confidence"],
            "votes": labeled[q]["votes"],
            "samples_checked": labeled[q]["samples_checked"],
            "all_streets": [it["street"] for it in clusters[q]],
        }
        for q in clusters
    ]
    Path(OUTPUT_CLUSTERS).write_text(
        json.dumps(clusters_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nГотово!")
    print(f"  {OUTPUT_STREETS} — маппінг вулиця -> мікрорайон (для бота)")
    print(f"  {OUTPUT_CLUSTERS} — кластери з деталями геокодування (для ручної перевірки)")
    print(f"\nПЕРЕВІР вручну кластери з 'confidence': 'середня' або 'немає даних' — ")
    print(f"саме там Nominatim міг помилитися або не мати даних про мікрорайон.")


if __name__ == "__main__":
    main()
