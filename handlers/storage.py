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
from zoneinfo import ZoneInfo

_KYIV_TZ = ZoneInfo("Europe/Kiev")

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


def delete_tg_post(article_id):
    """Прибирає матеріал з індексу постів каналу (/stat_forget) — наступний
    /stat шукатиме пост по t.me/s наживо. True, якщо запис був."""
    with _lock:
        state = _read_state()
        posts = state.get("tg_posts", {})
        if str(article_id) not in posts:
            return False
        del posts[str(article_id)]
        _write_state(state)
        return True


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


def _add_model_usage(models, model, delta):
    """Домержити токени одного виклику в {model: rec} (спільний агрегат,
    розріз по людях і денний зріз пишуться однією лінійкою)."""
    rec = models.setdefault(
        model, {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    )
    for key, value in delta.items():
        rec[key] = rec.get(key, 0) + value


def record_ai_usage(model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0,
                    user_id=None, user_name=None, feature=None):
    """Акумулює токени AI-виклику в місячний агрегат по моделях (REVIEW в.5).
    Викликається раз на запит (для NLQ — сумарно за весь tool-use цикл).

    user_id/user_name — коли виклик ініціювала конкретна людина (NLQ-питання,
    бек із кнопки, /dossier): токени додатково лягають у місячний розріз по
    людях (ai_usage_users → «хто скільки коштує» в /aicost) і в сьогоднішній
    запис людини в bot_usage (→ вартість дня в /usage). Без user_id — лише
    спільний агрегат (автоматика: ранкове, звіти, судді, батчі, витяги).

    feature — НА ЩО пішли гроші («carousel», «dossier»…). Третя вісь поруч із
    моделлю і людиною, і питання в неї своє: «хто напитав» не відповідає на
    «скільки з'їв конкретний інструмент», бо той самий Лис у тієї самої
    людини і відповідає на питання, і малює каруселі. Позначку ставить сам
    виклик; непозначені лягають у «решта» — див. ai_usage.format_month_report."""
    month = datetime.now().strftime("%Y-%m")
    delta = {
        "requests": 1,
        "input": input_tokens or 0,
        "output": output_tokens or 0,
        "cache_read": cache_read or 0,
        "cache_creation": cache_creation or 0,
    }
    with _lock:
        state = _read_state()
        usage = state.setdefault("ai_usage", {})
        _add_model_usage(usage.setdefault(month, {}), model, delta)
        if len(usage) > AI_USAGE_MAX_MONTHS:
            for old in sorted(usage.keys())[:len(usage) - AI_USAGE_MAX_MONTHS]:
                del usage[old]
        if feature:
            by_feature = state.setdefault("ai_usage_features", {})
            month_features = by_feature.setdefault(month, {})
            _add_model_usage(month_features.setdefault(feature, {}), model, delta)
            if len(by_feature) > AI_USAGE_MAX_MONTHS:
                for old in sorted(by_feature.keys())[:len(by_feature) - AI_USAGE_MAX_MONTHS]:
                    del by_feature[old]
        if user_id is not None:
            by_user = state.setdefault("ai_usage_users", {})
            month_users = by_user.setdefault(month, {})
            user_rec = month_users.setdefault(str(user_id), {"name": "", "models": {}})
            if user_name:
                user_rec["name"] = user_name
            _add_model_usage(user_rec["models"], model, delta)
            if len(by_user) > AI_USAGE_MAX_MONTHS:
                for old in sorted(by_user.keys())[:len(by_user) - AI_USAGE_MAX_MONTHS]:
                    del by_user[old]
            # І в сьогоднішній запис користування: щоденний звіт показує
            # вартість людини за день поруч із її питаннями й беками.
            day_rec = _usage_day_rec(state, user_id, user_name)
            _add_model_usage(day_rec.setdefault("ai", {}), model, delta)
        _write_state(state)


def get_ai_usage(month=None):
    """Витрати AI: {model: rec} за місяць, або {month: {model: rec}} за всі."""
    with _lock:
        usage = _read_state().get("ai_usage", {})
        return dict(usage.get(month, {})) if month else dict(usage)


def get_ai_usage_users(month):
    """Розріз витрат AI по людях за місяць: {user_id(str): {name, models}}."""
    with _lock:
        return dict(_read_state().get("ai_usage_users", {}).get(month, {}))


def get_ai_usage_features(month):
    """Розріз витрат AI по інструментах за місяць: {feature: {model: rec}}."""
    with _lock:
        return dict(_read_state().get("ai_usage_features", {}).get(month, {}))


def record_viber_post():
    """Лічильник постів, задзеркалених у Viber, по місяцях (для Viber-блоку
    таблиці аналітики — «Надіслано повідомлень»). Викликається на кожен
    успішний пост дзеркала Telegram→Viber. Viber API історії постів не дає —
    рахуємо самі, від моменту запуску дзеркала."""
    month = datetime.now().strftime("%Y-%m")
    with _lock:
        state = _read_state()
        counts = state.setdefault("viber_posts", {})
        counts[month] = counts.get(month, 0) + 1
        if len(counts) > 60:  # ~5 років місяців, з запасом
            for old in sorted(counts.keys())[:len(counts) - 60]:
                del counts[old]
        _write_state(state)


def get_viber_post_count(month):
    """К-сть постів, задзеркалених у Viber за місяць 'YYYY-MM' (None, якщо не було)."""
    with _lock:
        return _read_state().get("viber_posts", {}).get(month)


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


def get_fb_missing_state():
    """Стан монітора миколаївських новин без Facebook-публікації (fb_missing.py):
    {"alerted": [node_id, ...], "baseline_done": bool, "baseline_ver": int}.
    alerted — новини, про які вже нагадали (нагадуємо РІВНО раз); baseline_ver —
    версія тихого baseline: перший запуск після зміни фільтра вибірки позначає
    вікно баченим без розсилки (щоб не завалити чат добовою історією);
    baseline_done — легасі-флаг перших версій, більше не читається.
    Порожній dict = перший запуск."""
    with _lock:
        state = _read_state()
        return state.get("fb_missing", {})


def save_fb_missing_state(fb_missing_state):
    with _lock:
        state = _read_state()
        state["fb_missing"] = fb_missing_state
        _write_state(state)


def get_token_state(name):
    """Стан сторожа токена Meta (fb_token.py) — name: "fb" | "ig".
    {"down_since": iso, "last_alert_at": iso, "warned_expiry": unix}.
    down_since — токен зараз мертвий і про це алертили (щоб помітити одужання);
    last_alert_at — дедуп нагадувань (раз на добу); warned_expiry — за який
    саме expires_at уже попереджали заздалегідь. Порожній dict = усе гаразд.

    Ключ у стані — f"{name}_token", тож "fb" читає й пише той самий "fb_token",
    що й до появи інсти: сторож розширився, а накопичений стан не загубився."""
    with _lock:
        state = _read_state()
        return state.get(f"{name}_token", {})


def save_token_state(name, token_state):
    """Записати стан сторожа токена (див. get_token_state)."""
    with _lock:
        state = _read_state()
        state[f"{name}_token"] = token_state
        _write_state(state)


def get_fb_token_state():
    """Сумісність: те саме, що get_token_state("fb")."""
    return get_token_state("fb")


def save_fb_token_state(fb_token_state):
    """Сумісність: те саме, що save_token_state("fb", …)."""
    save_token_state("fb", fb_token_state)


def get_tiktok_oauth():
    """OAuth-стан TikTok: {"refresh_token", "access_token", "access_expires_at"}.
    TikTok РОТУЄ refresh token на кожному оновленні (старий протухає), тому
    новий треба зберігати тут, а не покладатись на env-сід. Порожній dict —
    ще не оновлювали (візьметься env TIKTOK_REFRESH_TOKEN як сід)."""
    with _lock:
        state = _read_state()
        return state.get("tiktok_oauth", {})


def save_tiktok_oauth(oauth):
    with _lock:
        state = _read_state()
        state["tiktok_oauth"] = oauth
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


# Відбір пар у екрані дублів банку тем (/promise_dupes → кнопки «злити
# обрані»). Той самий підхід, що news_search: стан живе на томі, бо кнопки
# мусять пережити редеплой — інакше вибір посеред розбору мовчки помирає, а
# людина цього не бачить, поки не тапне.
PROMISE_DUPES_MAX_ENTRIES = 6


def get_promise_dupes(dialog_id):
    """Останній відбір пар-дублів для розмови dialog_id ("chat:user")."""
    with _lock:
        return _read_state().get("promise_dupes", {}).get(dialog_id)


def save_promise_dupes(dialog_id, entry):
    """Зберігає {"pairs": [[keep, drop], ...], "off": [...], "at": iso}."""
    with _lock:
        state = _read_state()
        picks = state.setdefault("promise_dupes", {})
        picks[dialog_id] = entry
        if len(picks) > PROMISE_DUPES_MAX_ENTRIES:
            oldest = sorted(picks, key=lambda k: picks[k].get("at", ""))
            for key in oldest[:len(picks) - PROMISE_DUPES_MAX_ENTRIES]:
                del picks[key]
        _write_state(state)


# Сторінка завантаження відео (video_download): токен доступу і куки.
#
# Куки лежать тут, а не у змінній середовища, з двох причин. Перша: вони
# помирають тижнями, а заміна через Railway означає редеплой бота заради
# рядка тексту. Друга: покласти їх має ЛЮДИНА зі свого браузера — файл
# приходить у приват боту, де вже стоїть whitelist ALLOWED_USER_IDS.
# Токен доступу — тому, що з чужими куками сторінка качає ВІД ІМЕНІ людини,
# і відкритою в інтернет їй бути не можна.


def get_video_access():
    """Токен сторінки /video або None (ще не видавали)."""
    with _lock:
        return _read_state().get("video_access")


def save_video_access(token):
    with _lock:
        state = _read_state()
        state["video_access"] = token
        _write_state(state)


def get_video_cookies():
    """{"text": ..., "at": iso, "by": "Олег", "domains": [...]} або None."""
    with _lock:
        return _read_state().get("video_cookies")


def save_video_cookies(entry):
    """Кладе куки (або None — прибрати)."""
    with _lock:
        state = _read_state()
        if entry is None:
            state.pop("video_cookies", None)
        else:
            state["video_cookies"] = entry
        _write_state(state)


# Беки, віддані на сторінку /back для копіювання з лінками (back_export).
# Живуть рівно доти, доки текст не вставили в статтю; кап — щоб стан не ріс.
BACK_EXPORTS_MAX = 40


def get_back_export(token):
    """Збережений бек за токеном лінка або None (застарів/не той токен)."""
    with _lock:
        return _read_state().get("back_exports", {}).get(token)


def save_back_export(token, entry):
    """Зберігає {"html": ..., "topic": ..., "at": iso} під токеном лінка."""
    with _lock:
        state = _read_state()
        exports = state.setdefault("back_exports", {})
        exports[token] = entry
        if len(exports) > BACK_EXPORTS_MAX:
            oldest = sorted(exports, key=lambda k: exports[k].get("at", ""))
            for key in oldest[:len(exports) - BACK_EXPORTS_MAX]:
                del exports[key]
        _write_state(state)


# Генератор каруселей (carousel.py): персональні токени сторінки і кеш планів.
#
# Токен тут, а не в норі, з тієї ж причини, що й у сторінки відео: сторінку
# відкривають звичайним браузером, де initData Telegram узяти нізвідки, а
# генерація плану коштує грошей — значить, вхід має бути іменний.
# Кеш планів — щоб повторне відкриття тієї самої новини не платило вдруге.
CAROUSEL_TOKENS_MAX = 30
CAROUSEL_PLANS_MAX = 30
# Чернетки каруселей. Зберігаємо ЛИШЕ слайди й лінк — сама стаття тягнеться
# заново при відкритті, тож запис лишається кілька кілобайтів. Класти в
# чернетку ще й текст статті означало б роздути файл стану, який
# перечитується й переписується на КОЖНУ операцію.
CAROUSEL_DRAFTS_MAX = 30
# Підписи, скопійовані в Instagram, — друга сигнатура для матчингу в /stat.
# Кап більший за решту каруселей: запис це кілька сотень байтів, а живе він
# рівно доти, доки по матеріалу можуть спитати статистику. 200 підписів це
# близько двох місяців роботи СММ.
CAROUSEL_CAPTIONS_MAX = 200


def get_carousel_tokens():
    """{token: {"person", "tg_id", "at"}} — видані входи на сторінку каруселей."""
    with _lock:
        return dict(_read_state().get("carousel_tokens", {}))


def save_carousel_tokens(tokens):
    """Перезаписує весь реєстр токенів (виклик уже обрізав протухлі)."""
    with _lock:
        state = _read_state()
        if len(tokens) > CAROUSEL_TOKENS_MAX:
            oldest = sorted(tokens, key=lambda k: tokens[k].get("at", ""))
            for key in oldest[:len(tokens) - CAROUSEL_TOKENS_MAX]:
                del tokens[key]
        state["carousel_tokens"] = tokens
        _write_state(state)


def get_carousel_plan(article_id):
    """Збережений план каруселі за id новини або None."""
    with _lock:
        return _read_state().get("carousel_plans", {}).get(str(article_id))


def save_carousel_plan(article_id, entry):
    """Кладе {"plan": ..., "at": iso, ...} під id новини (кап найстарішими)."""
    with _lock:
        state = _read_state()
        plans = state.setdefault("carousel_plans", {})
        plans[str(article_id)] = entry
        if len(plans) > CAROUSEL_PLANS_MAX:
            oldest = sorted(plans, key=lambda k: plans[k].get("at", ""))
            for key in oldest[:len(plans) - CAROUSEL_PLANS_MAX]:
                del plans[key]
        _write_state(state)


def get_carousel_caption(article_id):
    """Підпис, який СММ забрала з генератора каруселей для цієї новини.
    /stat дає його матчеру другою сигнатурою: у стрічці інсти посилання на
    статтю немає, і зіставляти доводиться по смислу — а тут ми знаємо сам
    текст, що поїхав у допис, тому здогадка стає звіркою."""
    with _lock:
        entry = _read_state().get("carousel_captions", {}).get(str(article_id))
        return (entry or {}).get("text") or ""


def save_carousel_caption(article_id, text, person=""):
    """Записує підпис у мить копіювання — саме тоді відомо, що цей текст іде
    в Instagram. Повторне копіювання перезаписує: в допис поїде остання
    версія, а не перша."""
    with _lock:
        state = _read_state()
        caps = state.setdefault("carousel_captions", {})
        caps[str(article_id)] = {"text": (text or "").strip(),
                                 "by": person,
                                 "at": datetime.now().isoformat(timespec="seconds")}
        if len(caps) > CAROUSEL_CAPTIONS_MAX:
            oldest = sorted(caps, key=lambda k: caps[k].get("at", ""))
            for key in oldest[:len(caps) - CAROUSEL_CAPTIONS_MAX]:
                del caps[key]
        _write_state(state)


def get_carousel_drafts():
    """{ключ: чернетка} — усі збережені чернетки каруселей."""
    with _lock:
        return dict(_read_state().get("carousel_drafts", {}))


def save_carousel_draft(key, entry):
    """Кладе чернетку під ключем «людина:новина» (повторне збереження тієї
    самої новини перезаписує, а не плодить копії)."""
    with _lock:
        state = _read_state()
        drafts = state.setdefault("carousel_drafts", {})
        drafts[key] = entry
        if len(drafts) > CAROUSEL_DRAFTS_MAX:
            oldest = sorted(drafts, key=lambda k: drafts[k].get("at", ""))
            for old in oldest[:len(drafts) - CAROUSEL_DRAFTS_MAX]:
                del drafts[old]
        _write_state(state)


def delete_carousel_draft(key):
    with _lock:
        state = _read_state()
        if state.get("carousel_drafts", {}).pop(key, None) is not None:
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


# ---------- Облік користування ботом (щоденний звіт адміну) ----------
#
# Хто зі співробітників що робив з ботом за день: команди, NLQ-питання з
# використаними tools, складені беки з темами. Ключ дня — за Києвом (сервер
# Railway працює в UTC, «вчора» у звіті має збігатись із людським «вчора»).
# Структура:
# "bot_usage": {
#   "2026-07-21": {
#     "56424866": {
#       "name": "Катерина Середа (@sereda_ka)",
#       "commands": {"stat": 2},
#       "nlq": 3,
#       "questions": [{"q": "скільки трафіку за тиждень", "len": 26}, ...],
#       "tools": {"get_traffic_history": 2},
#       "backs": [{"topic": "Сєнкевич марафон", "len": 16, "items": 3}]
#     }
#   }
# }
# len — довжина ПОВНОГО тексту до обрізки USAGE_TEXT_MAX (детектор «стін
# тексту» в NLQ: сам обрізаний текст не каже, було там 250 символів чи 5000).
# Старі записи questions — прості рядки без len, звіт розуміє обидва формати.

USAGE_MAX_DAYS = 30        # тримаємо місяць історії
USAGE_QUESTIONS_MAX = 40   # питань на користувача на день (захист від роздування)
USAGE_BACKS_MAX = 20       # беків на користувача на день
USAGE_TEXT_MAX = 200       # обрізка збережених питань/тем


def _usage_day_rec(state, user_id, user_name):
    """Запис користувача за СЬОГОДНІ (Київ) + прюнінг старих днів."""
    day = datetime.now(_KYIV_TZ).strftime("%Y-%m-%d")
    usage = state.setdefault("bot_usage", {})
    if len(usage) > USAGE_MAX_DAYS:
        for old in sorted(usage.keys())[:len(usage) - USAGE_MAX_DAYS]:
            del usage[old]
    day_rec = usage.setdefault(day, {})
    rec = day_rec.setdefault(str(user_id), {
        "name": "", "commands": {}, "nlq": 0, "questions": [], "tools": {}, "backs": [],
    })
    if user_name:
        rec["name"] = user_name  # ім'я освіжаємо щоразу (могли змінити username)
    return rec


def _usage_clip(text):
    text = " ".join((text or "").split())
    return text[:USAGE_TEXT_MAX] + "…" if len(text) > USAGE_TEXT_MAX else text


def record_usage_command(user_id, user_name, command):
    """Залічити виклик команди (/stat, /weekly, …) — без слеша й аргументів."""
    with _lock:
        state = _read_state()
        rec = _usage_day_rec(state, user_id, user_name)
        rec["commands"][command] = rec["commands"].get(command, 0) + 1
        _write_state(state)


def record_usage_nlq(user_id, user_name, question, tools=None):
    """Залічити природномовне питання до Лиса + tools, які воно задіяло."""
    with _lock:
        state = _read_state()
        rec = _usage_day_rec(state, user_id, user_name)
        rec["nlq"] = rec.get("nlq", 0) + 1
        q_full = " ".join((question or "").split())
        if q_full and len(rec["questions"]) < USAGE_QUESTIONS_MAX:
            # Зберігаємо і реальну довжину: обрізаний текст не дає відрізнити
            # довге питання від вставленої стіни тексту на тисячі символів
            rec["questions"].append({"q": _usage_clip(q_full), "len": len(q_full)})
        for t in tools or []:
            rec["tools"][t] = rec["tools"].get(t, 0) + 1
        _write_state(state)


def record_usage_back(user_id, user_name, topic, items_count=None):
    """Залічити складений бек: тема (пошуковий запит/питання) + к-сть новин."""
    with _lock:
        state = _read_state()
        rec = _usage_day_rec(state, user_id, user_name)
        if len(rec["backs"]) < USAGE_BACKS_MAX:
            t_full = " ".join((topic or "").split())
            rec["backs"].append({
                "topic": _usage_clip(t_full), "len": len(t_full), "items": items_count,
            })
        _write_state(state)


def get_usage_day(day):
    """Зріз користування за день 'YYYY-MM-DD': {user_id(str): rec}. Порожній dict — тиша."""
    with _lock:
        return dict(_read_state().get("bot_usage", {}).get(day, {}))


def get_usage_all():
    """Уся збережена історія користування: {day: {user_id(str): rec}} —
    для зведення по одній людині (/usage <імʼя>). Живе USAGE_MAX_DAYS днів."""
    with _lock:
        return {day: dict(users) for day, users in _read_state().get("bot_usage", {}).items()}


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

