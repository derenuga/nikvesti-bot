"""
Телефонна база редакції — модуль Mini App «Команда».

Проблема, з якої виросло (Олег, 29.07): «у кого є телефон віцемера Лукова?» —
хтось кидає в чат, а через пів року питають знову. Редакція щоразу відновлює
те, що вже знала.

Два входи, обидва ручні (рішення Олега — чат бот НЕ сканує):
1. **Форма в апці** — додати або поправити картку.
2. **Переслати контакт Лису в приват** — Telegram шле картку контакту як
   звичайне повідомлення, бот кладе її в базу «як є». Далі ПІБ і посаду все
   одно правлять руками: у людей у телефонах записано хто як («Луков мер»,
   «Ігор Микол.»), і жодна автоматика цього не причеше.

Пошук — і за іменем, і за ТЕМОЮ: реальне питання в редакції частіше «хто в нас
по енергетиці?», ніж «дай Лукова». Тому теги — звичайний текстовий рядок через
кому, який шукається нарівні з іменем і посадою.

Таблиця в Норі: team_contacts. Без Нори модуль тихо спить (апка показує
порожньо, а не падає).
"""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from handlers import bot_db

KYIV_TZ = ZoneInfo("Europe/Kiev")

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS team_contacts (
        id         BIGSERIAL PRIMARY KEY,
        name       TEXT NOT NULL,
        role       TEXT,
        phone      TEXT,
        telegram   TEXT,
        email      TEXT,
        tags       TEXT,
        note       TEXT,
        added_by   TEXT,
        updated_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Пошук іде по кількох полях одразу — індекс на нижній регістр імені
    # покриває найчастіший випадок («дай Лукова»), решта добирається сканом:
    # база редакції — це сотні рядків, а не мільйони.
    "CREATE INDEX IF NOT EXISTS idx_team_contacts_name "
    "ON team_contacts (lower(name))",
]

_schema_lock = threading.Lock()
_schema_done = False


def ensure_contacts_schema():
    global _schema_done
    if _schema_done:
        return
    with _schema_lock:
        if _schema_done:
            return
        with bot_db.session():
            for sql in _SCHEMA_STATEMENTS:
                bot_db.execute(sql)
        _schema_done = True


def _row(r):
    return {
        "id": r["id"], "name": r["name"], "role": r["role"],
        "phone": r["phone"], "telegram": r["telegram"], "email": r["email"],
        "tags": r["tags"], "note": r["note"],
        "added_by": r["added_by"], "updated_by": r["updated_by"],
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


SEARCH_FIELDS = ("name", "role", "tags", "phone", "note")


def list_contacts(query=None, limit=200):
    """Пошук по імені, посаді, темах, телефону і нотатці одразу.

    Одним запитом, бо «хто в нас по енергетиці» і «дай Лукова» — це те саме
    питання з різного боку, і змушувати вибирати режим пошуку немає сенсу."""
    ensure_contacts_schema()
    q = (query or "").strip()
    if not q:
        return [_row(r) for r in bot_db.query(
            "SELECT * FROM team_contacts ORDER BY lower(name) LIMIT %s", (limit,))]
    like = f"%{q.lower()}%"
    where = " OR ".join(f"lower(coalesce({f}, '')) LIKE %s" for f in SEARCH_FIELDS)
    return [_row(r) for r in bot_db.query(
        f"SELECT * FROM team_contacts WHERE {where} ORDER BY lower(name) LIMIT %s",
        tuple([like] * len(SEARCH_FIELDS) + [limit]),
    )]


def _clean(v):
    v = (v or "").strip()
    return v or None


def find_by_phone(phone):
    """Той самий номер двічі в базі не потрібен — картку доповнюємо, а не
    дублюємо. Порівнюємо лише цифри: один і той самий номер пишуть і як
    +380…, і як 0…, і з пробілами."""
    ensure_contacts_schema()
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 7:
        return None
    tail = digits[-9:]      # національна частина без коду країни
    for r in bot_db.query(
            "SELECT * FROM team_contacts WHERE phone IS NOT NULL"):
        other = "".join(c for c in (r["phone"] or "") if c.isdigit())
        if other and other[-9:] == tail:
            return _row(r)
    return None


def add_contact(actor, name, role=None, phone=None, telegram=None,
                email=None, tags=None, note=None):
    ensure_contacts_schema()
    name = (name or "").strip()
    if not name:
        raise ValueError("Без імені картка не має сенсу")
    rows = bot_db.query(
        "INSERT INTO team_contacts (name, role, phone, telegram, email, tags, "
        "note, added_by, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING *",
        (name, _clean(role), _clean(phone), _clean(telegram), _clean(email),
         _clean(tags), _clean(note), actor, actor),
    )
    return _row(rows[0])


_EDITABLE = ("name", "role", "phone", "telegram", "email", "tags", "note")


def update_contact(contact_id, actor, **fields):
    ensure_contacts_schema()
    sets, params = [], []
    for key in _EDITABLE:
        if key in fields:
            sets.append(f"{key} = %s")
            params.append(_clean(fields[key]) if key != "name"
                          else (fields[key] or "").strip() or None)
    if not sets:
        return None
    sets.append("updated_by = %s")
    params.append(actor)
    sets.append("updated_at = now()")
    params.append(int(contact_id))
    rows = bot_db.query(
        f"UPDATE team_contacts SET {', '.join(sets)} WHERE id = %s RETURNING *",
        tuple(params),
    )
    return _row(rows[0]) if rows else None


def delete_contact(contact_id):
    ensure_contacts_schema()
    rows = bot_db.query(
        "DELETE FROM team_contacts WHERE id = %s RETURNING id", (int(contact_id),))
    return bool(rows)


def contributions(person):
    """Скільки карток людина завела і скільки поправила.

    Це для подяки на її екрані (Олег, 29.07: «на сторінці журналіста десь
    писати, що ви поповнили базу контактів на стільки-то, пасибочки»), а НЕ
    для KPI: щойно за контакти почнуть давати очки, база наповниться сміттям
    заради лічильника."""
    ensure_contacts_schema()
    rows = bot_db.query(
        "SELECT count(*) FILTER (WHERE added_by = %s) AS added, "
        "       count(*) FILTER (WHERE updated_by = %s AND added_by <> %s) AS fixed, "
        "       count(*) AS total FROM team_contacts",
        (person, person, person),
    )
    r = rows[0] if rows else {}
    return {"added": int(r.get("added") or 0),
            "fixed": int(r.get("fixed") or 0),
            "total": int(r.get("total") or 0)}


def save_shared_contact(actor, first_name, last_name, phone, username=None):
    """Контакт, ПЕРЕСЛАНИЙ Лису в приват. Кладемо як є — правити ПІБ і посаду
    все одно доведеться руками (у телефонах записано хто як). Якщо такий номер
    уже є, доповнюємо картку, а не плодимо другу."""
    name = " ".join(p for p in ((first_name or "").strip(),
                                (last_name or "").strip()) if p) or "Без імені"
    found = find_by_phone(phone)
    if found:
        patch = {}
        if username and not found.get("telegram"):
            patch["telegram"] = f"@{username.lstrip('@')}"
        if patch:
            update_contact(found["id"], actor, **patch)
            found.update(patch)
        return found, False
    created = add_contact(
        actor, name, phone=phone,
        telegram=f"@{username.lstrip('@')}" if username else None,
        note="переслано в Лиса",
    )
    return created, True


# ---------- Підтягнути з архіву (сутнісний шар) ----------
#
# Олег, 29.07: «щоб я міг натиснути на сірого Миколу Логвинова — і чик-чик,
# йому підтяглася сутність, що він директор Миколаївобтеплоенерго».
#
# Джерело — таблиця entities сутнісного шару нори (17 років архіву, витяг
# Haiku): там уже лежать імена, останні відомі посади (role_last), аліаси й
# кількість згадок. Тобто редакція вже знає посаду — просто знання лежало не
# там, де його питають.
#
# Чесна межа: сутності залито приблизно за останні 2–3 роки, тож знайдеться
# не кожен. Порожню відповідь так і кажемо, а не вдаємо, що людини не існує.

def lookup_entity(name, limit=5):
    """Кандидати з архіву за іменем. Порядок слів не має значення: шукаємо
    ВСІ токени в спільному сіні з name_ua + name_ru + аліасів — «Логвінов
    Микола» і «Микола Логвінов» мають знаходитись однаково, і російське
    написання теж (у сутності є обидві мови)."""
    tokens = [t.lower() for t in (name or "").replace(".", " ").split()
              if len(t) >= 3]
    if not tokens:
        return []
    haystack = ("lower(coalesce(name_ua, '') || ' ' || coalesce(name_ru, '') "
                "|| ' ' || coalesce(array_to_string(aliases, ' '), ''))")
    where = " AND ".join([f"{haystack} LIKE %s"] * len(tokens))
    try:
        rows = bot_db.query(
            f"SELECT id, kind, name_ua, name_ru, role_last, mentions, last_seen "
            f"FROM entities WHERE {where} "
            # спершу люди й ті, про кого писали більше: у сірої картки
            # найімовірніший кандидат — той, хто частіше в новинах
            f"ORDER BY (kind = 'person') DESC, mentions DESC NULLS LAST LIMIT %s",
            tuple([f"%{t}%" for t in tokens] + [limit]),
        )
    except Exception as e:
        # Сутнісного шару може не бути взагалі (порожня нора) — це не помилка
        print(f"team_contacts: сутності недоступні — {e}")
        return []
    out = []
    for r in rows:
        year = None
        if r.get("last_seen"):
            try:
                year = datetime.fromtimestamp(r["last_seen"], KYIV_TZ).year
            except (ValueError, OSError, TypeError):
                year = None
        out.append({
            "id": r["id"], "kind": r["kind"],
            "name": r.get("name_ua") or r.get("name_ru"),
            "role": r.get("role_last"),
            "mentions": r.get("mentions") or 0,
            "last_year": year,
        })
    return out
