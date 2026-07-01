import csv
import io
import json
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://off.energy.mk.ua/api"
SITE_ROOT = "https://off.energy.mk.ua"

_LABELS_PATH = Path(__file__).parent / "energy_outage_labels.json"
_LABELS: dict[str, list[str]] = json.loads(_LABELS_PATH.read_text(encoding="utf-8")) if _LABELS_PATH.exists() else {}

OUTAGE_TYPE_NAMES = {
    1: "ГАВ",
    2: "СГАВ",
    3: "ГПВ",
}

KYIV_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NikvestiBot/1.0)",
    "Referer": "https://off.energy.mk.ua/",
}

NOMINATIM_HEADERS = {
    "User-Agent": "nikvesti-outage-bot/1.0 (contact: derenuga@gmail.com)",
}

DATA_DIR = Path(os.environ.get("STATE_PATH", "/data/prozorro_state.json")).parent


def _slot_to_time(slot_id: int) -> tuple[str, str]:
    start_minutes = (slot_id - 1) * 30
    end_minutes = slot_id * 30
    h_s, m_s = divmod(start_minutes, 60)
    h_e, m_e = divmod(end_minutes % (24 * 60), 60)
    return f"{h_s:02d}:{m_s:02d}", f"{h_e:02d}:{m_e:02d}"


def _fetch_json(url: str) -> list | dict:
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_queues() -> dict:
    lookup = {}
    for type_id in (1, 2, 3):
        queues = _fetch_json(f"{BASE_URL}/outage-queue/by-type/{type_id}")
        for q in queues:
            lookup[q["id"]] = {
                "name": q["name"],
                "type_id": type_id,
                "type_name": OUTAGE_TYPE_NAMES[type_id],
                "enabled": q.get("enabled", 0),
            }
    return lookup


def _find_current_schedule(active: list) -> dict | None:
    now_utc = datetime.now(timezone.utc)
    for schedule in active:
        frm = datetime.fromisoformat(schedule["from"].replace("Z", "+00:00"))
        to = datetime.fromisoformat(schedule["to"].replace("Z", "+00:00"))
        if frm <= now_utc <= to:
            return schedule
    future = [s for s in active
              if datetime.fromisoformat(s["from"].replace("Z", "+00:00")) > now_utc]
    return future[0] if future else None


def _merge_slots(slot_ids: list[int]) -> list[tuple[str, str]]:
    if not slot_ids:
        return []
    sorted_ids = sorted(slot_ids)
    ranges = []
    run_start = sorted_ids[0]
    run_end = sorted_ids[0]
    for sid in sorted_ids[1:]:
        if sid == run_end + 1:
            run_end = sid
        else:
            ranges.append((run_start, run_end))
            run_start = run_end = sid
    ranges.append((run_start, run_end))
    result = []
    for start_id, end_id in ranges:
        t_start, _ = _slot_to_time(start_id)
        _, t_end = _slot_to_time(end_id)
        result.append((t_start, t_end))
    return result


OUTAGE_STATE_PATH = DATA_DIR / "outage_monitor_state.json"

MONTH_UA = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
}


def fetch_outage_data() -> dict:
    queues = _fetch_all_queues()
    active = _fetch_json(f"{BASE_URL}/v2/schedule/active")
    return {"queues": queues, "active": active}


def _parse_schedule(data: dict) -> tuple[dict | None, dict[str, dict]]:
    """Повертає (schedule, queue_map) де queue_map: name → {off, prob}."""
    queues = data["queues"]
    schedule = _find_current_schedule(data["active"])
    if not schedule:
        return None, {}

    queue_off: dict[int, list[int]] = {}
    queue_prob: dict[int, list[int]] = {}
    for entry in schedule["series"]:
        qid = entry["outage_queue_id"]
        tsid = entry["time_series_id"]
        t = entry["type"]
        if t in ("OFF", "SURE_OFF"):
            queue_off.setdefault(qid, []).append(tsid)
        elif t == "PROBABLY_OFF":
            queue_prob.setdefault(qid, []).append(tsid)

    queue_map: dict[str, dict] = {}
    for qid in set(queue_off) | set(queue_prob):
        q = queues.get(qid)
        if not q:
            continue
        queue_map[q["name"]] = {
            "off": sorted(queue_off.get(qid, [])),
            "prob": sorted(queue_prob.get(qid, [])),
            "type_id": q["type_id"],
            "type_name": q["type_name"],
        }
    return schedule, queue_map


def _queue_map_to_entries(queue_map: dict[str, dict]) -> dict[int, list]:
    by_type: dict[int, list] = {}
    for name, q in queue_map.items():
        off_ranges = _merge_slots(q["off"])
        prob_ranges = _merge_slots(q["prob"])
        by_type.setdefault(q["type_id"], []).append({
            "name": name,
            "type_name": q["type_name"],
            "off": off_ranges,
            "prob": prob_ranges,
        })
    return by_type


def build_message(data: dict, changes: list[str] | None = None) -> str:
    schedule, queue_map = _parse_schedule(data)

    if not schedule:
        return "⚡ Дані про відключення наразі відсутні."
    if not queue_map:
        return "⚡ На сьогодні графік відключень порожній."

    now_kyiv = datetime.now(KYIV_TZ)
    date_str = f"{now_kyiv.day} {MONTH_UA[now_kyiv.month]} {now_kyiv.year}"

    updated_raw = schedule.get("updated_at") or schedule.get("from", "")
    try:
        updated_dt = datetime.fromisoformat(updated_raw.replace("Z", "+00:00")).astimezone(KYIV_TZ)
        updated_str = updated_dt.strftime("%H:%M")
    except Exception:
        updated_str = None

    header = f"⚡ Графік відключень на {date_str}"
    if updated_str:
        header += f" (оновлено {updated_str})"

    lines = [header, ""]

    if changes:
        lines.append("🔄 <b>Зміни у графіку:</b>")
        for ch in changes:
            lines.append(f"  {ch}")
        lines.append("")

    by_type = _queue_map_to_entries(queue_map)

    for type_id in sorted(by_type):
        type_name = OUTAGE_TYPE_NAMES[type_id]
        entries = sorted(by_type[type_id], key=lambda x: x["name"])
        lines.append(f"📋 {type_name}:")
        for e in entries:
            if not e["off"] and not e["prob"]:
                continue

            off_min = sum(
                (int(en.split(":")[0]) * 60 + int(en.split(":")[1])) -
                (int(s.split(":")[0]) * 60 + int(s.split(":")[1]))
                for s, en in e["off"]
            )
            h, m = divmod(off_min, 60)
            hours_str = f"{h} год" + (f" {m} хв" if m else "")

            schedule_parts = []
            if e["off"]:
                off_str = " / ".join(f"{s}–{en}" for s, en in e["off"])
                schedule_parts.append(off_str)
            if e["prob"]:
                prob_str = " / ".join(f"{s}–{en}" for s, en in e["prob"])
                schedule_parts.append(f"можливо — {prob_str}")

            lines.append(f"<b>Черга {e['name']}: немає світла {hours_str}</b>")
            lines.append(f"🕐 {' / '.join(schedule_parts)}")

            neighborhoods = _LABELS.get(e["name"], [])
            if neighborhoods:
                lines.append(f"<blockquote expandable>{', '.join(neighborhoods)}</blockquote>")

            lines.append("")

    return "\n".join(lines).rstrip()


def _load_outage_state() -> dict:
    if OUTAGE_STATE_PATH.exists():
        try:
            return json.loads(OUTAGE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_outage_state(state: dict) -> None:
    OUTAGE_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_changes(old_map: dict[str, dict], new_map: dict[str, dict]) -> list[str]:
    changes = []
    all_queues = sorted(set(old_map) | set(new_map))
    for name in all_queues:
        if name not in old_map:
            changes.append(f"➕ Черга {name} з'явилась у графіку")
        elif name not in new_map:
            changes.append(f"➖ Черга {name} зникла з графіку")
        else:
            old, new = old_map[name], new_map[name]
            if old["off"] != new["off"] or old["prob"] != new["prob"]:
                off_ranges = _merge_slots(new["off"])
                off_str = " / ".join(f"{s}–{en}" for s, en in off_ranges) if off_ranges else "—"
                changes.append(f"✏️ Черга {name}: новий графік — {off_str}")
    return changes


async def check_outage_changes(bot, debug_chat_id: int) -> None:
    try:
        data = fetch_outage_data()
        schedule, new_map = _parse_schedule(data)

        if schedule is None:
            return

        state = _load_outage_state()
        old_map: dict[str, dict] = state.get("queue_map", {})

        if not old_map:
            # перший запуск — зберігаємо baseline без надсилання
            _save_outage_state({"queue_map": new_map})
            return

        changes = _build_changes(old_map, new_map)
        if changes:
            text = build_message(data, changes=changes)
            await bot.send_message(chat_id=debug_chat_id, text=text, parse_mode="HTML")

        _save_outage_state({"queue_map": new_map})

    except Exception as e:
        logger.error(f"check_outage_changes error: {e}", exc_info=True)


async def outage_handler(update, context):
    try:
        data = fetch_outage_data()
        text = build_message(data)
    except Exception as e:
        logger.error(f"energy_outage error: {e}", exc_info=True)
        text = f"⚠️ Не вдалося отримати дані про відключення: {e}"
    await update.message.reply_text(text, parse_mode="HTML")


# ---------- Адресний каскад ----------

def _fetch_streets_data(idfilial: int) -> list[dict]:
    """Обходить каскад filii→ns→street→dom→outage-queue, повертає список
    {street, sample_dom, queues: tuple} для кожної вулиці.
    Результат кешується у /data/outage_streets_{idfilial}.json.
    """
    cache_path = DATA_DIR / f"outage_streets_{idfilial}.json"
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        # queues зберігаємо як list у JSON, конвертуємо назад у tuple
        for r in rows:
            r["queues"] = tuple(r["queues"])
        logger.info(f"outage_streets: loaded {len(rows)} rows from cache {cache_path}")
        return rows

    filii = {f["idfilial"]: f["fullname"] for f in _fetch_json(f"{BASE_URL}/addr/filii")}
    _ = filii.get(idfilial, str(idfilial))

    rows = []
    ns_list = _fetch_json(f"{BASE_URL}/addr/filii/{idfilial}/ns")
    for ns in ns_list:
        ns_id = ns["idnaspunkt"]
        streets = _fetch_json(f"{BASE_URL}/addr/ns/{ns_id}/street")
        for street in streets:
            street_id = street["idstreet"]
            street_name = street["nazstreet"]
            try:
                doms = _fetch_json(f"{BASE_URL}/addr/street/{street_id}/dom")
            except Exception:
                continue
            if not doms:
                continue
            sample = doms[0]
            dom_id = sample["iddom"]
            dom_name = sample["nazdom"]
            try:
                outage_queue = _fetch_json(f"{BASE_URL}/addr/dom/{dom_id}/outage-queue")
                queues = tuple(sorted(set(q["outage"]["name"] for q in outage_queue)))
            except Exception:
                queues = tuple()
            rows.append({"street": street_name, "sample_dom": dom_name, "queues": queues})

    # зберігаємо в кеш (queues як list для JSON-серіалізації)
    serializable = [{"street": r["street"], "sample_dom": r["sample_dom"], "queues": list(r["queues"])} for r in rows]
    cache_path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"outage_streets: fetched {len(rows)} rows, saved to {cache_path}")
    return rows


def _export_streets_csv(idfilial: int) -> io.StringIO:
    """Будує CSV з даних адресного каскаду (з кешу або свіжий запит)."""
    rows = _fetch_streets_data(idfilial)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "filiya_id", "filiya_name", "naspunkt_id", "naspunkt_name",
        "street_id", "street_name", "sample_dom_id", "sample_dom_name",
        "outage_queue_raw",
    ])

    # для CSV потрібні повні поля — доводиться повторно пройти каскад якщо немає
    # Але у нас вже є street+sample_dom+queues — цього достатньо для геокодування.
    # CSV формуємо зі спрощеними полями (без street_id/ns_id — вони не потрібні далі).
    for r in rows:
        writer.writerow([
            idfilial, "", "", "",
            "", r["street"], "", r["sample_dom"],
            json.dumps([{"outage": {"name": q}} for q in r["queues"]], ensure_ascii=False),
        ])

    buf.seek(0)
    return buf


async def outage_export_handler(update, context):
    """Розвідувальна команда: /outage_export [idfilial] — обходить весь
    каскад адрес для однієї дільниці і присилає CSV для подальшого
    групування вулиць по мікрорайонах. За замовчуванням idfilial=15
    (Миколаївський РЕМ — місто Миколаїв). Результат кешується на /data.
    """
    idfilial = 15
    if context.args:
        try:
            idfilial = int(context.args[0])
        except ValueError:
            await update.message.reply_text("idfilial має бути числом, напр. /outage_export 15")
            return

    cache_path = DATA_DIR / f"outage_streets_{idfilial}.json"
    if cache_path.exists():
        await update.message.reply_text(f"Беру дані з кешу ({cache_path.name})...")
    else:
        await update.message.reply_text(f"Збираю дані для дільниці {idfilial}... це займе ~10 хвилин.")

    try:
        buf = _export_streets_csv(idfilial)
        data = buf.getvalue().encode("utf-8")
        await update.message.reply_document(
            document=io.BytesIO(data),
            filename=f"outage_streets_filial_{idfilial}.csv",
        )
    except Exception as e:
        logger.error(f"energy_outage export error: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Помилка експорту: {e}")


# ---------- Геокодування мікрорайонів ----------

def _geocode_street(street: str, sample_dom: str, cache: dict) -> str | None:
    """Визначає мікрорайон вулиці через Nominatim. Повертає назву або None."""
    queries = []
    if sample_dom:
        queries.append(f"{street}, {sample_dom}, Миколаїв, Україна")
    queries.append(f"{street}, Миколаїв, Україна")

    for query in queries:
        if query in cache:
            return cache[query]

        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "addressdetails": 1,
                        "countrycodes": "ua", "limit": 1},
                headers=NOMINATIM_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json()
        except Exception as e:
            logger.warning(f"nominatim error for '{query}': {e}")
            time.sleep(1)
            continue
        finally:
            time.sleep(1)  # Nominatim: max 1 req/s

        if not results:
            cache[query] = None
            continue

        addr = results[0].get("address", {})
        suburb = (
            addr.get("suburb")
            or addr.get("neighbourhood")
            or addr.get("quarter")
        )
        if not suburb:
            city_district = addr.get("city_district", "")
            suburb = "Центр" if city_district == "Центральний район" else city_district or None
        cache[query] = suburb
        return suburb

    return None


def _run_geocoding(idfilial: int) -> list[dict]:
    """Кластеризує вулиці по підписах черг і геокодує по 3 представники
    кожного кластера. Повертає список кластерів з мікрорайонами.
    """
    rows = _fetch_streets_data(idfilial)

    # кластеризація по підпису черг
    by_queues: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_queues[r["queues"]].append(r)

    # кеш Nominatim — зберігаємо між запусками
    nom_cache_path = DATA_DIR / "nominatim_cache.json"
    cache: dict = {}
    if nom_cache_path.exists():
        cache = json.loads(nom_cache_path.read_text(encoding="utf-8"))

    SAMPLES = 3
    clusters = []
    for queues, items in sorted(by_queues.items(), key=lambda kv: -len(kv[1])):
        votes: Counter = Counter()
        samples_checked = []
        for item in items[:SAMPLES]:
            suburb = _geocode_street(item["street"], item["sample_dom"], cache)
            samples_checked.append({"street": item["street"], "suburb": suburb})
            if suburb:
                votes[suburb] += 1

        best = votes.most_common(1)[0][0] if votes else None
        top_count = votes.most_common(1)[0][1] if votes else 0
        confidence = "висока" if top_count == SAMPLES else ("середня" if votes else "немає даних")

        clusters.append({
            "queue_signature": list(queues),
            "count": len(items),
            "mikroraion_guess": best,
            "confidence": confidence,
            "votes": dict(votes),
            "samples_checked": samples_checked,
            "all_streets": [it["street"] for it in items],
        })

    nom_cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return clusters


async def outage_geocode_handler(update, context):
    """/outage_geocode [idfilial] — геокодує вулиці по мікрорайонах через
    Nominatim/OSM. Кластеризує вулиці за підписом черг (~50 кластерів),
    геокодує 3 представники кожного, повертає JSON для ручної перевірки.
    Дані вулиць беруться з кешу /data якщо є, інакше спочатку треба
    запустити /outage_export.
    """
    idfilial = 15
    if context.args:
        try:
            idfilial = int(context.args[0])
        except ValueError:
            await update.message.reply_text("idfilial має бути числом, напр. /outage_geocode 15")
            return

    cache_path = DATA_DIR / f"outage_streets_{idfilial}.json"
    if not cache_path.exists():
        await update.message.reply_text(
            f"⚠️ Кеш вулиць для дільниці {idfilial} відсутній.\n"
            f"Спочатку запусти /outage_export {idfilial} (займе ~10 хв), потім повтори."
        )
        return

    await update.message.reply_text(
        "Геокодую мікрорайони через Nominatim... ~3–5 хвилин (1 запит/сек)."
    )
    try:
        clusters = _run_geocoding(idfilial)
        result_json = json.dumps(clusters, ensure_ascii=False, indent=2).encode("utf-8")
        no_data = [c for c in clusters if c["confidence"] == "немає даних"]
        medium = [c for c in clusters if c["confidence"] == "середня"]
        summary = (
            f"✅ Геокодування завершено.\n"
            f"Кластерів: {len(clusters)}, вулиць: {sum(c['count'] for c in clusters)}\n"
            f"Висока впевненість: {len(clusters) - len(medium) - len(no_data)}\n"
            f"Середня: {len(medium)} — перевір вручну\n"
            f"Немає даних: {len(no_data)} — потрібна ручна розмітка"
        )
        await update.message.reply_document(
            document=io.BytesIO(result_json),
            filename=f"queue_clusters_mikroraion_{idfilial}.json",
            caption=summary,
        )
    except Exception as e:
        logger.error(f"outage_geocode error: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Помилка геокодування: {e}")


# ---------- Службові команди ----------

async def outage_probe_handler(update, context):
    """Тимчасова службова команда для розвідки off.energy.mk.ua.
    Локальне середовище розробки не має мережевого доступу до off.energy.mk.ua
    (блокується Cloudflare), а Railway — має. Тому ендпоінти адресного каскаду
    (ns/street/dom) підбираються наживо через цю команду в Telegram.

    /outage_probe <path> [query string | пошуковий рядок для .js файлів]
    Якщо <path> починається з "api/" — запит іде на off.energy.mk.ua/api/...,
    другий аргумент трактується як query string (filiya_id=15 тощо).
    Якщо <path> не починається з "api/" (наприклад js/app.js) — запит іде
    на off.energy.mk.ua/<path>, а другий аргумент — це підрядок для пошуку
    в тексті відповіді (бо файл мінімізований і завеликий для повного виводу).
    """
    if not context.args:
        await update.message.reply_text(
            "Використання:\n"
            "/outage_probe api/<шлях> [query string] — запит до API\n"
            "/outage_probe js/app.js <підрядок> — пошук тексту в JS-файлі сайту"
        )
        return
    path = context.args[0]
    arg2 = context.args[1] if len(context.args) > 1 else ""
    is_api = path.startswith("api/")
    url = f"{SITE_ROOT}/{path}"
    if is_api and arg2:
        url = f"{url}?{arg2}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        body = resp.text
        if is_api or not arg2:
            text = f"URL: {url}\nStatus: {resp.status_code}\n\n{body[:3500]}"
        else:
            matches = []
            start = 0
            while len(matches) < 5:
                idx = body.find(arg2, start)
                if idx == -1:
                    break
                lo = max(0, idx - 80)
                hi = min(len(body), idx + len(arg2) + 80)
                matches.append(body[lo:hi])
                start = idx + len(arg2)
            if matches:
                snippet = "\n---\n".join(matches)
                text = f"URL: {url}\nStatus: {resp.status_code}\nЗнайдено '{arg2}': {len(matches)}+\n\n{snippet}"
            else:
                text = f"URL: {url}\nStatus: {resp.status_code}\nПідрядок '{arg2}' не знайдено (довжина файлу: {len(body)})"
        await update.message.reply_text(text[:4000])
    except Exception as e:
        await update.message.reply_text(f"URL: {url}\n⚠️ Помилка: {e}")
