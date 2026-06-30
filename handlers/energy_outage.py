import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://off.energy.mk.ua/api"

OUTAGE_TYPE_NAMES = {
    1: "ГПВ",
    2: "ГАВ",
    3: "СГАВ",
}

# Час по Києву = UTC+3
KYIV_TZ = timezone(timedelta(hours=3))

# 48 тридцятихвилинних слотів: id=1 → 00:00–00:30, id=N → (N-1)*30хв
def _slot_to_time(slot_id: int) -> tuple[str, str]:
    start_minutes = (slot_id - 1) * 30
    end_minutes = slot_id * 30
    h_s, m_s = divmod(start_minutes, 60)
    h_e, m_e = divmod(end_minutes % (24 * 60), 60)
    return f"{h_s:02d}:{m_s:02d}", f"{h_e:02d}:{m_e:02d}"


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> list | dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_outage_data() -> dict:
    """Повертає словник з усіма потрібними даними для формування повідомлення."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NikvestiBot/1.0)",
        "Referer": "https://off.energy.mk.ua/",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        queues_by_type, active = await asyncio.gather(
            _fetch_all_queues(session),
            _fetch_json(session, f"{BASE_URL}/v2/schedule/active"),
        )
    return {"queues": queues_by_type, "active": active}


async def _fetch_all_queues(session: aiohttp.ClientSession) -> dict:
    """Повертає {queue_id: {name, type_id, type_name, enabled}}."""
    results = await asyncio.gather(*[
        _fetch_json(session, f"{BASE_URL}/outage-queue/by-type/{t}")
        for t in (1, 2, 3)
    ])
    lookup = {}
    for type_id, queues in enumerate(results, start=1):
        for q in queues:
            lookup[q["id"]] = {
                "name": q["name"],
                "type_id": type_id,
                "type_name": OUTAGE_TYPE_NAMES[type_id],
                "enabled": q.get("enabled", 0),
            }
    return lookup


def _find_current_schedule(active: list) -> dict | None:
    """Знаходить розклад що діє зараз (за UTC)."""
    now_utc = datetime.now(timezone.utc)
    for schedule in active:
        frm = datetime.fromisoformat(schedule["from"].replace("Z", "+00:00"))
        to = datetime.fromisoformat(schedule["to"].replace("Z", "+00:00"))
        if frm <= now_utc <= to:
            return schedule
    # Якщо поточного нема — перший майбутній
    future = [s for s in active
              if datetime.fromisoformat(s["from"].replace("Z", "+00:00")) > now_utc]
    return future[0] if future else None


def _merge_slots(slot_ids: list[int]) -> list[tuple[str, str]]:
    """Зливає послідовні слоти в часові інтервали."""
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


def build_message(data: dict) -> str:
    queues = data["queues"]
    schedule = _find_current_schedule(data["active"])

    if not schedule:
        return "⚡ Дані про відключення наразі відсутні.\n\n📋 Дані Миколаївобленерго"

    now_kyiv = datetime.now(KYIV_TZ)
    date_str = now_kyiv.strftime("%-d %B %Y").lower()

    # Збираємо per-queue: OFF-слоти і PROBABLY_OFF-слоти
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
        return "⚡ На сьогодні графік відключень порожній.\n\n📋 Дані Миколаївобленерго"

    # Групуємо по типу відключення
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

    lines = [f"⚡ Оновлено графік відключень на {date_str}\n"]

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
    """Команда /outage — формує і надсилає повідомлення про відключення."""
    try:
        data = await fetch_outage_data()
        text = build_message(data)
    except Exception as e:
        logger.error(f"energy_outage error: {e}", exc_info=True)
        text = f"⚠️ Не вдалося отримати дані про відключення: {e}"
    await update.message.reply_text(text)
