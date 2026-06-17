"""
Шар абстракції над персистентним станом бота.

Зараз стан зберігається в JSON-файлі на Railway Volume (/data/prozorro_state.json).
Якщо в майбутньому проект переїде на MySQL чи іншу БД — потрібно переписати
тільки цей файл, решта коду (prozorro.py, sheets.py, bot.py) не зміниться,
бо звертається лише до функцій нижче, а не до файлу напряму.

Структура state.json:
{
    "offset": "1718600000.0",          # останній offset з Prozorro API (для інкрементального опитування)
    "spreadsheet_id": "abc123...",      # ID створеної Google Sheets таблиці (None, доки не створена)
    "tenders": {
        "UA-2026-05-28-001834-a": {
            "message_id": 1234,
            "sent_at": "2026-06-17T14:00:00",
            "title": "...",
            "amount": 1932480,
            "buyer": "...",
            "taken_by": null,           # ім'я/username того, хто взяв (None, якщо ще ніхто)
            "taken_at": null
        },
        ...
    },
    "message_to_tender": {
        "1234": "UA-2026-05-28-001834-a"
    }
}
"""

import json
import os
import threading

STATE_PATH = os.environ.get("STATE_PATH", "/data/prozorro_state.json")

_lock = threading.Lock()

_DEFAULT_STATE = {
    "offset": None,
    "spreadsheet_id": None,
    "tenders": {},
    "message_to_tender": {},
}


def _read_state():
    if not os.path.exists(STATE_PATH):
        return dict(_DEFAULT_STATE)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, value in _DEFAULT_STATE.items():
            if key not in data:
                data[key] = value if not isinstance(value, dict) else {}
        return data
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_STATE)


def _write_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_PATH)


def get_offset():
    with _lock:
        return _read_state().get("offset")


def set_offset(offset):
    with _lock:
        state = _read_state()
        state["offset"] = offset
        _write_state(state)


def is_tender_seen(tender_id):
    with _lock:
        state = _read_state()
        return tender_id in state["tenders"]


def mark_tender_sent(tender_id, message_id, title, amount, buyer, sent_at):
    with _lock:
        state = _read_state()
        state["tenders"][tender_id] = {
            "message_id": message_id,
            "sent_at": sent_at,
            "title": title,
            "amount": amount,
            "buyer": buyer,
            "taken_by": None,
            "taken_at": None,
        }
        state["message_to_tender"][str(message_id)] = tender_id
        _write_state(state)


def get_tender_by_message_id(message_id):
    with _lock:
        state = _read_state()
        tender_id = state["message_to_tender"].get(str(message_id))
        if not tender_id:
            return None
        tender = state["tenders"].get(tender_id)
        if not tender:
            return None
        return {"tender_id": tender_id, **tender}


def is_tender_taken(tender_id):
    with _lock:
        state = _read_state()
        tender = state["tenders"].get(tender_id)
        if not tender:
            return False
        return tender.get("taken_by") is not None


def mark_tender_taken(tender_id, taken_by, taken_at):
    with _lock:
        state = _read_state()
        tender = state["tenders"].get(tender_id)
        if not tender:
            return False
        if tender.get("taken_by") is not None:
            return False
        tender["taken_by"] = taken_by
        tender["taken_at"] = taken_at
        _write_state(state)
        return True


def get_spreadsheet_id():
    with _lock:
        return _read_state().get("spreadsheet_id")


def set_spreadsheet_id(spreadsheet_id):
    with _lock:
        state = _read_state()
        state["spreadsheet_id"] = spreadsheet_id
        _write_state(state)
