import logging
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://off.energy.mk.ua/api"

OUTAGE_TYPE_NAMES = {
    1: "ГПВ",
    2: "ГАВ",
    3: "СГАВ",
}

KYIV_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NikvestiBot/1.0)",
    "Referer": "https://off.energy.mk.ua/",
}


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


def fetch_outage_data() -> dict:
    queues = _fetch_all_queues()
    active = _fetch_json(f"{BASE_URL}/v2/schedule/active")
    return {"queues": queues, "active": active}


def build_message(data: dict) -> str:
    queues = data["queues"]
    schedule = _find_current_schedule(data["active"])

    if not schedule:
        return "⚡ Дані про відключення наразі відсутні.\n\nДані Миколаївобленерго"

    now_kyiv = datetime.now(KYIV_TZ)
    date_str = now_kyiv.strftime("%-d.%m.%Y")

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

    all_queue_ids = set(queue_off) | set(queue_prob)
    if not all_queue_ids:
        return "⚡ На сьогодні графік відключень порожній.\n\nДані Миколаївобленерго"

    by_type: dict[int, list] = {}
    for qid in all_queue_ids:
        q = queues.get(qid)
        if not q:
            continue
        type_id = q["type_id"]
        off_ranges = _merge_slots(queue_off.get(qid, []))
        prob_ranges = _merge_slots(queue_prob.get(qid, []))
        by_type.setdefault(type_id, []).append({
            "name": q["name"],
            "type_name": q["type_name"],
            "off": off_ranges,
            "prob": prob_ranges,
        })

    MONTH_UA = {
        1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
        5: "травня", 6: "червня", 7: "липня", 8: "серпня",
        9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня",
    }
    date_str = f"{now_kyiv.day} {MONTH_UA[now_kyiv.month]} {now_kyiv.year}"

    lines = [f"⚡ Графік відключень на {date_str}\n"]

    for type_id in sorted(by_type):
        type_name = OUTAGE_TYPE_NAMES[type_id]
        entries = sorted(by_type[type_id], key=lambda x: x["name"])
        lines.append(f"📋 {type_name}:")
        for e in entries:
            queue_lines = []
            if e["off"]:
                ranges_str = ", ".join(f"{s}–{en}" for s, en in e["off"])
                queue_lines.append(f"немає світла {ranges_str}")
            if e["prob"]:
                ranges_str = ", ".join(f"{s}–{en}" for s, en in e["prob"])
                queue_lines.append(f"можливе відключення {ranges_str}")
            if queue_lines:
                lines.append(f"  Черга {e['name']}: {'; '.join(queue_lines)}")
        lines.append("")

    lines.append("Дані Миколаївобленерго")
    return "\n".join(lines)


async def outage_handler(update, context):
    try:
        data = fetch_outage_data()
        text = build_message(data)
    except Exception as e:
        logger.error(f"energy_outage error: {e}", exc_info=True)
        text = f"⚠️ Не вдалося отримати дані про відключення: {e}"
    await update.message.reply_text(text)
