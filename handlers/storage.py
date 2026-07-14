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
from datetime import datetime, timedelta

STATE_PATH = os.environ.get("STATE_PATH", "/data/prozorro_state.json")

_lock = threading.Lock()

# Обмеження росту стану (REVIEW п. б.4).
# Seen-списки — кап на джерело: свіжі ID у хвості, обрізаємо початок.
# 1000 >> будь-якої сторінки фіду (~10-50), тому фід ніколи не "забувається".
# Кап seen-списків КОНКУРЕНТІВ: їх сторінка — стрічка (~10-50 останніх),
# 1000 з запасом. ДОКУМЕНТИ не капляться: їх сторінки (проєкти рішень) —
# повний історичний список на тисячі записів; кап відрізав би історію,
# і старі документи щоразу виглядали б "новими" (баг б.4, спам за 2021).
SEEN_COMPETITOR_IDS_MAX = 1000
# Тендери старші за це прюняться (звільняє tenders + message_to_tender).
# 120 днів > this_quarter (~90) — щоб NLQ-запити по кварталу не втрачали дані.
TENDER_RETENTION_DAYS = 120

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


def get_seen_tender_ids():
    """Повертає set усіх вже відісланих tender_id одним читанням файлу."""
    with _lock:
        state = _read_state()
        return set(state["tenders"].keys())


def _prune_old_tenders(state, days=TENDER_RETENTION_DAYS):
    """Прибирає тендери старші за `days` (за sent_at) разом з їх
    message_to_tender. Реакції на тендери актуальні днями, не місяцями,
    тому старі можна забувати без шкоди. Повертає кількість видалених."""
    cutoff = datetime.now() - timedelta(days=days)
    tenders = state.get("tenders", {})
    m2t = state.get("message_to_tender", {})
    to_delete = []
    for tid, t in tenders.items():
        sent = t.get("sent_at")
        if not sent:
            continue
        try:
            if datetime.fromisoformat(sent) < cutoff:
                to_delete.append(tid)
        except (ValueError, TypeError):
            continue  # нерозпарсована дата — лишаємо про всяк випадок
    for tid in to_delete:
        mid = tenders[tid].get("message_id")
        del tenders[tid]
        if mid is not None:
            m2t.pop(str(mid), None)
    return len(to_delete)


def bulk_save(new_tenders, new_offset=None):
    """
    Зберігає одразу кілька нових тендерів і offset ОДНИМ записом файлу.
    new_tenders: список dict {tender_id, message_id, title, amount, buyer, sent_at}
    Заодно прюнить старі тендери (прогон Prozorro щогодини — чистка теж).
    """
    with _lock:
        state = _read_state()
        for t in new_tenders:
            tender_id = t["tender_id"]
            state["tenders"][tender_id] = {
                "message_id": t["message_id"],
                "sent_at": t["sent_at"],
                "title": t["title"],
                "amount": t["amount"],
                "buyer": t["buyer"],
                "taken_by": None,
                "taken_at": None,
            }
            state["message_to_tender"][str(t["message_id"])] = tender_id
        if new_offset:
            state["offset"] = new_offset
        _prune_old_tenders(state)
        _write_state(state)


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


def reset_tender_taken(tender_id):
    """Скидає taken_by/taken_at назад на None — для розблокування після помилки запису в Sheets."""
    with _lock:
        state = _read_state()
        tender = state["tenders"].get(tender_id)
        if not tender:
            return False
        tender["taken_by"] = None
        tender["taken_at"] = None
        _write_state(state)
        return True
        
def get_seen_document_ids(source_id):
    """
    Повертає список вже бачених ID для конкретного джерела документів.
    Повертає None якщо джерело ще ніколи не перевірялось (перший запуск) —
    це важливо, бо [] і None мають різний сенс: [] = є записи але порожньо,
    None = ще не ініціалізовано (потрібен baseline-запуск без відправки).
    """
    with _lock:
        state = _read_state()
        doc_ids = state.get("document_ids", {})
        if source_id not in doc_ids:
            return None
        return doc_ids[source_id]


def save_seen_document_ids(source_id, ids):
    """Зберігає список ID для конкретного джерела документів — БЕЗ капу:
    сторінка проєктів рішень містить повну історію (тисячі записів), кап
    відрізав би її й ті записи щоразу виглядали б 'новими'. Ріст обмежений
    темпом публікацій, а не бот-трафіком, тому безпечно тримати все."""
    with _lock:
        state = _read_state()
        if "document_ids" not in state:
            state["document_ids"] = {}
        state["document_ids"][source_id] = list(ids)
        _write_state(state)


def get_competitor_night_buffer():
    """Нічний буфер новин конкурентів (00:00–07:00) — шлються ранковим дайджестом."""
    with _lock:
        return list(_read_state().get("competitor_night_buffer", []))


def append_competitor_night_buffer(items):
    with _lock:
        state = _read_state()
        state.setdefault("competitor_night_buffer", []).extend(items)
        _write_state(state)


def clear_competitor_night_buffer():
    with _lock:
        state = _read_state()
        state["competitor_night_buffer"] = []
        _write_state(state)


TG_POSTS_MAX_ENTRIES = 20000  # вистачає на кілька років історії каналу (бэкфіл)


def get_tg_post(article_id):
    """Індекс постів каналу @nikvesti: article_id → {"message_id": ...}. None якщо немає."""
    with _lock:
        return _read_state().get("tg_posts", {}).get(str(article_id))


def _trim_tg_posts(posts):
    if len(posts) > TG_POSTS_MAX_ENTRIES:
        for key in list(posts.keys())[:len(posts) - TG_POSTS_MAX_ENTRIES]:
            del posts[key]


def save_tg_post(article_id, message_id):
    with _lock:
        state = _read_state()
        posts = state.setdefault("tg_posts", {})
        posts[str(article_id)] = {"message_id": message_id}
        _trim_tg_posts(posts)
        _write_state(state)


def bulk_save_tg_posts(mapping):
    """Записує багато article_id→message_id ОДНИМ записом файлу (для бэкфілу)."""
    with _lock:
        state = _read_state()
        posts = state.setdefault("tg_posts", {})
        for article_id, message_id in mapping.items():
            posts[str(article_id)] = {"message_id": message_id}
        _trim_tg_posts(posts)
        _write_state(state)


def get_all_tenders():
    """Повний архів відісланих тендерів (read-only копія) — для NLQ-tools
    'що там по тендерах за тиждень?'. Ключ — tender_id, значення — dict
    з title/amount/buyer/sent_at/taken_by/taken_at."""
    with _lock:
        state = _read_state()
        return dict(state["tenders"])


AI_USAGE_MAX_MONTHS = 13  # тримаємо ~рік історії витрат


def record_ai_usage(model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
    """Акумулює токени AI-виклику в місячний агрегат по моделях (REVIEW в.5).
    Викликається раз на запит (для NLQ — сумарно за весь tool-use цикл)."""
    month = datetime.now().strftime("%Y-%m")
    with _lock:
        state = _read_state()
        usage = state.setdefault("ai_usage", {})
        month_rec = usage.setdefault(month, {})
        rec = month_rec.setdefault(
            model, {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        )
        rec["requests"] += 1
        rec["input"] += input_tokens or 0
        rec["output"] += output_tokens or 0
        rec["cache_read"] += cache_read or 0
        rec["cache_creation"] += cache_creation or 0
        if len(usage) > AI_USAGE_MAX_MONTHS:
            for old in sorted(usage.keys())[:len(usage) - AI_USAGE_MAX_MONTHS]:
                del usage[old]
        _write_state(state)


def get_ai_usage(month=None):
    """Витрати AI: {model: rec} за місяць, або {month: {model: rec}} за всі."""
    with _lock:
        usage = _read_state().get("ai_usage", {})
        return dict(usage.get(month, {})) if month else dict(usage)


def get_traffic_spikes_state():
    """Стан детектора сплесків трафіку: профіль типового трафіку по слотах
    (день тижня + година) і час останнього алерту. Порожній dict = перший запуск."""
    with _lock:
        state = _read_state()
        return state.get("traffic_spikes", {})


def save_traffic_spikes_state(spikes_state):
    with _lock:
        state = _read_state()
        state["traffic_spikes"] = spikes_state
        _write_state(state)


def get_youtube_counters():
    """Знімки lifetime-лічильників YouTube-каналу (views/videos) на кінець
    місяця: {"YYYY-MM": {"views": N, "videos": K, "at": iso}}. Місячні
    перегляди/контент = дельта сусідніх знімків (YouTube Data API віддає
    лише накопичені лічильники). Порожній dict = ще не знімали."""
    with _lock:
        state = _read_state()
        return state.get("youtube_counters", {})


def save_youtube_counters(counters):
    with _lock:
        state = _read_state()
        state["youtube_counters"] = counters
        _write_state(state)


def get_builder_monitor_state():
    """Стан монітора білдера головної: {'last_alert_at': unix} для кулдауну.
    Порожній dict = ще не алертили."""
    with _lock:
        state = _read_state()
        return state.get("builder_monitor", {})


def save_builder_monitor_state(builder_state):
    with _lock:
        state = _read_state()
        state["builder_monitor"] = builder_state
        _write_state(state)


# Останні пошуки по архіву новин (news_archive) — по одному на розмову
# (ключ "chat_id:user_id"). Персистимо, щоб кнопки відбору новин для беку
# переживали редеплой/рестарт бота (інакше "Результати застаріли" одразу
# після деплою). Кап — щоб ключі покинутих розмов не накопичувались.
NEWS_SEARCH_MAX_ENTRIES = 8


def get_news_search(dialog_id):
    """Останній пошук по архіву новин для розмови dialog_id ("chat:user")."""
    with _lock:
        return _read_state().get("news_search", {}).get(dialog_id)


def save_news_search(dialog_id, entry):
    """Зберігає {"items": [...], "selected": [...], "at": iso} для розмови."""
    with _lock:
        state = _read_state()
        searches = state.setdefault("news_search", {})
        searches[dialog_id] = entry
        if len(searches) > NEWS_SEARCH_MAX_ENTRIES:
            oldest = sorted(searches, key=lambda k: searches[k].get("at", ""))
            for key in oldest[:len(searches) - NEWS_SEARCH_MAX_ENTRIES]:
                del searches[key]
        _write_state(state)


def get_seen_competitor_ids(source_id):
    """
    Повертає список вже бачених ID для конкретного джерела конкурента.
    None = ще не ініціалізовано (перший запуск).
    """
    with _lock:
        state = _read_state()
        competitor_ids = state.get("competitor_ids", {})
        if source_id not in competitor_ids:
            return None
        return competitor_ids[source_id]


def save_seen_competitor_ids(source_id, ids):
    """Зберігає список ID для конкретного джерела конкурента (кап на джерело,
    свіжі — у хвості)."""
    with _lock:
        state = _read_state()
        if "competitor_ids" not in state:
            state["competitor_ids"] = {}
        state["competitor_ids"][source_id] = list(ids)[-SEEN_COMPETITOR_IDS_MAX:]
        _write_state(state)


# ---------- Кеш зіставлення тегів із Wikidata (/tags_wiki) ----------
#
# Дороге в прогоні — пошук у Wikidata і рішення Claude. Кешуємо їх за tag_id,
# щоб повторний /tags_wiki на більший N не перепроходив уже зіставлені теги.
# Назви/ужиток НЕ кешуємо — вони беруться з БД щоразу (ужиток змінюється).

def get_tags_wikidata_cache():
    """dict tag_id(str) → {qid, type, chosen_label, confidence, reason, candidates}."""
    with _lock:
        return _read_state().get("tags_wikidata", {})


def update_tags_wikidata_cache(mapping):
    """Домержити результати прогону (dict tag_id(str) → рішення) у кеш."""
    with _lock:
        state = _read_state()
        cache = state.setdefault("tags_wikidata", {})
        for tid, decision in mapping.items():
            cache[str(tid)] = decision
        _write_state(state)


def clear_tags_wikidata_cache():
    """Скинути кеш зіставлення (для повного свіжого прогону). Повертає к-сть."""
    with _lock:
        state = _read_state()
        n = len(state.get("tags_wikidata", {}))
        state["tags_wikidata"] = {}
        _write_state(state)
        return n

