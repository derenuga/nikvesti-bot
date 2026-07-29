"""
Телефонна база редакції — модуль Mini App «Команда».

Проблема, з якої виросло (Олег, 29.07): «у кого є телефон віцемера Лукова?» —
хтось кидає в чат, а через пів року питають знову. Редакція щоразу відновлює
те, що вже знала.

Два входи, обидва ручні (рішення Олега — чат бот НЕ сканує):
1. **Форма в апці** — додати або поправити картку.
2. **Переслати контакт Лису в приват** — Telegram шле картку контакту як
   звичайне повідомлення, бот кладе її в базу «як є». Далі ПІБ і посаду все
   одно правлять руками: у людей у телефонах записано хто як («Яблучко ЖЕК»,
   «Степан Микол.»), і жодна автоматика цього не причеше.

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
    # Кілька номерів на людину (Олег, 29.07): у чиновника зазвичай мобільний,
    # приймальня і прессекретар. Масив вільних рядків, а не окремі поля з
    # мітками: люди все одно пишуть по-своєму («0512… приймальня»), і
    # вигадувати їм словник міток означає змусити воювати з формою.
    "ALTER TABLE team_contacts ADD COLUMN IF NOT EXISTS phones TEXT[] DEFAULT '{}'",
    # Разова міграція наявних карток: старий phone стає першим у списку
    "UPDATE team_contacts SET phones = ARRAY[phone] "
    "WHERE phone IS NOT NULL AND (phones IS NULL OR cardinality(phones) = 0)",
    # Плюс до вже збережених міжнародних номерів: Telegram віддає
    # phone_number БЕЗ нього, і перші картки лягли як «380501954887».
    # Ідемпотентно й за тим самим правилом, що normalize_phone: чіпаємо лише
    # суцільні цифри 11–15 не з нуля — місцевий «0501112233» лишається як є.
    """
    UPDATE team_contacts SET phones = ARRAY(
        SELECT CASE WHEN p ~ '^[1-9][0-9]{10,14}$' THEN '+' || p ELSE p END
        FROM unnest(phones) AS p)
    WHERE EXISTS (SELECT 1 FROM unnest(phones) AS p WHERE p ~ '^[1-9][0-9]{10,14}$')
    """,
    "UPDATE team_contacts SET phone = '+' || phone "
    "WHERE phone ~ '^[1-9][0-9]{10,14}$'",
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
    phones = [p for p in (r.get("phones") or []) if p]
    if not phones and r.get("phone"):
        phones = [r["phone"]]
    return {
        "id": r["id"], "name": r["name"], "role": r["role"],
        # phone лишається — це ПЕРШИЙ номер: на ньому тримається і пошук, і
        # кнопка дзвінка в списку, і сумісність зі старими картками
        "phone": phones[0] if phones else None, "phones": phones,
        "telegram": r["telegram"], "email": r["email"],
        "tags": r["tags"], "note": r["note"],
        "added_by": r["added_by"], "updated_by": r["updated_by"],
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


SEARCH_FIELDS = ("name", "role", "tags", "phone", "note")
# Шукати треба по ВСІХ номерах, а не лише по першому — інакше картка з трьома
# номерами знаходилась би тільки за одним із них
_SEARCH_EXTRA = "lower(coalesce(array_to_string(phones, ' '), ''))"


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
    parts = [f"lower(coalesce({f}, '')) LIKE %s" for f in SEARCH_FIELDS]
    parts.append(f"{_SEARCH_EXTRA} LIKE %s")
    return [_row(r) for r in bot_db.query(
        f"SELECT * FROM team_contacts WHERE {' OR '.join(parts)} "
        f"ORDER BY lower(name) LIMIT %s",
        tuple([like] * len(parts) + [limit]),
    )]


def _clean(v):
    v = (v or "").strip()
    return v or None


def normalize_phone(value):
    """«380501954887» → «+380501954887».

    Telegram віддає phone_number БЕЗ плюса, і в картці це виглядало як набір
    цифр, а tel:-посилання з такого номера набирає внутрішній, а не
    міжнародний.

    Плюс ставимо ЛИШЕ там, де номер уже міжнародний: самі цифри, довжина
    11–15 і не з нуля. Місцевий «0501112233» так і лишається місцевим —
    домислювати йому код країни ми не маємо права (номер може бути й не
    український), а «+0501112233» був би просто зламаним."""
    v = (value or "").strip()
    if v.isdigit() and 11 <= len(v) <= 15 and not v.startswith("0"):
        return "+" + v
    return v


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
            "SELECT * FROM team_contacts "
            "WHERE phone IS NOT NULL OR cardinality(coalesce(phones, '{}')) > 0"):
        row = _row(r)
        for p in row["phones"]:
            other = "".join(c for c in p if c.isdigit())
            if other and other[-9:] == tail:
                return row
    return None


def add_contact(actor, name, role=None, phone=None, telegram=None,
                email=None, tags=None, note=None, phones=None):
    ensure_contacts_schema()
    name = (name or "").strip()
    if not name:
        raise ValueError("Без імені картка не має сенсу")
    nums = _phones(phones) if phones is not None else _phones(phone) or []
    rows = bot_db.query(
        "INSERT INTO team_contacts (name, role, phone, phones, telegram, email, "
        "tags, note, added_by, updated_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (name, _clean(role), nums[0] if nums else None, nums,
         _clean(telegram), _clean(email), _clean(tags), _clean(note),
         actor, actor),
    )
    return _row(rows[0])


_EDITABLE = ("name", "role", "phone", "telegram", "email", "tags", "note")


def _phones(value):
    """Список номерів із того, що прийшло: масив або один рядок. Порожні
    рядки відкидаємо — інакше «+ ще номер» без тексту лишав би дірку."""
    if value is None:
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(p).strip() for p in items if str(p or "").strip()]


def update_contact(contact_id, actor, **fields):
    ensure_contacts_schema()
    sets, params = [], []
    if "phones" in fields:
        # phone тримаємо синхронним із першим номером: на ньому пошук,
        # кнопка дзвінка в списку і старі картки
        nums = _phones(fields["phones"]) or []
        sets += ["phones = %s", "phone = %s"]
        params += [nums, nums[0] if nums else None]
    for key in _EDITABLE:
        if key in fields and key != "phone":
            sets.append(f"{key} = %s")
            params.append(_clean(fields[key]) if key != "name"
                          else (fields[key] or "").strip() or None)
        elif key == "phone" and "phone" in fields and "phones" not in fields:
            nums = _phones(fields["phone"]) or []
            sets += ["phones = %s", "phone = %s"]
            params += [nums, nums[0] if nums else None]
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


def save_shared_contact(actor, first_name, last_name, phone, vcard=None):
    """Контакт, ПЕРЕСЛАНИЙ Лису в приват.

    Працює і для тих, кого немає в Telegram: у скріпці «Контакт» віддає
    будь-кого з адресної книги телефона, просто без user_id.

    Із vCard дістаємо решту номерів, організацію і посаду — тобто частину
    того, що інакше довелось би вбивати руками. ПІБ усе одно лишається як є:
    у телефонах записано хто як («Яблучко ЖЕК»), і причісувати це автоматом —
    гірше, ніж лишити людині.

    Якщо такий номер уже є, доповнюємо картку, а не плодимо другу."""
    name = " ".join(p for p in ((first_name or "").strip(),
                                (last_name or "").strip()) if p) or "Без імені"
    card = parse_vcard(vcard)
    numbers = []
    for num in [phone] + card["phones"]:
        num = normalize_phone(num)
        if num and num not in numbers:
            numbers.append(num)
    role = " · ".join(p for p in (card["title"], card["org"]) if p) or None

    found = find_by_phone(phone)
    if found:
        patch = {}
        merged = list(found["phones"])
        for num in numbers:
            tail = "".join(c for c in num if c.isdigit())[-9:]
            if not any("".join(c for c in m if c.isdigit())[-9:] == tail
                       for m in merged):
                merged.append(num)
        if merged != found["phones"]:
            patch["phones"] = merged
        if role and not found.get("role"):
            patch["role"] = role
        if card["email"] and not found.get("email"):
            patch["email"] = card["email"]
        if patch:
            updated = update_contact(found["id"], actor, **patch)
            if updated:
                return updated, False
        return found, False
    # Нотатку «переслано в Лиса» не пишемо: вона займала єдине вільне поле
    # і не казала нічого корисного. Хто додав — видно з added_by (Олег, 29.07).
    created = add_contact(actor, name, role=role, phones=numbers,
                          email=card["email"])
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


def parse_vcard(vcard):
    """Витяг корисного з vCard, яку Telegram шле разом із контактом.

    Навіщо: у самої картки контакту Telegram є лише імʼя й ОДИН номер, а у
    vCard з адресної книги телефона часто лежать усі номери, організація і
    посада. Тобто половина того, що ми просимо дозаповнити руками, уже
    приїхала — просто в іншому полі.

    Розбираємо вручну і терпимо: vCard буває у різних кодуваннях і з
    параметрами (TEL;TYPE=CELL;VALUE=uri:tel:+380…), а тягнути залежність
    заради трьох рядків не варто. Чого не зрозуміли — просто не беремо."""
    out = {"phones": [], "org": None, "title": None, "email": None}
    if not vcard:
        return out
    for raw in str(vcard).replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name = head.split(";")[0].upper()
        value = value.strip()
        if not value:
            continue
        if name == "TEL":
            num = value.replace("tel:", "").strip()
            if num and num not in out["phones"]:
                out["phones"].append(num)
        elif name == "ORG" and not out["org"]:
            out["org"] = value.replace(";", " ").strip()
        elif name == "TITLE" and not out["title"]:
            out["title"] = value
        elif name == "EMAIL" and not out["email"]:
            out["email"] = value
    return out
