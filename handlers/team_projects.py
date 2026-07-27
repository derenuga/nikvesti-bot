"""
Проєкти, партнери і фото людей для Mini App «Команда» — читання з БД сайту.

Концепція v2 (docs/TEAM_APP_MODULE.md): грантові проєкти ЖИВУТЬ у CMS —
partner_project (назва, строки, квоти kpi_news/kpi_articles, partner_id),
partner (назва, логотип). Апка їх читає як є і нічого не дублює.

Картинки (розвідано 27.07.2026, підтверджено Олегом по БД):
- partner.logo  — ім'я файла (php…jpg/png) → https://nikvesti.com/{size}/images/partner_logo/{stem}.webp
- users.avatar  — ім'я файла (avatar_…/php…, png/jpg упереміш) →
                  https://nikvesti.com/{size}/img/users_ava/{stem}.webp
  Ресайзер сайту віддає .webp незалежно від розширення оригіналу — тому
  розширення відкидаємо і просимо .webp.

Зіставлення фото з ростером: users.first_name/last_name українською, ростер —
теж ("Аліна Квітко"), матчимо повне ім'я без регістру (пробуємо і зворотний
порядок). Немає збігу — None, фронт малює ініціали.

Кеш: проєкти й аватарки міняються рідко, а ліміти БД сайту жорсткі
(10000 запитів/год на всю ботову активність) — тримаємо в пам'яті TTL 10 хв.
Модуль опційний: без DB_* повертає порожньо, апка деградує до ініціалів
і ручного вибору «позапроєктна».
"""

import time

from handlers import db

SITE_BASE = "https://nikvesti.com"


def is_configured():
    """Чи налаштована БД сайту (без неї модуль повертає порожньо, не падає)."""
    return db.is_configured()

CACHE_TTL = 600
_cache = {}  # key -> (expires_at, value)


def _cached(key):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _remember(key, value):
    _cache[key] = (time.monotonic() + CACHE_TTL, value)
    return value


def invalidate_cache():
    _cache.clear()


def _webp_url(filename, path, size):
    """Публічний URL картинки через ресайзер сайту (він конвертує в webp)."""
    if not filename:
        return None
    stem = filename.rsplit(".", 1)[0]
    return f"{SITE_BASE}/{size}/{path}/{stem}.webp"


def partner_logo_url(logo, size="255x255"):
    return _webp_url(logo, "images/partner_logo", size)


def avatar_url(avatar, size="255x255"):
    return _webp_url(avatar, "img/users_ava", size)


def list_projects(active_only=True):
    """Проєкти з CMS: id, назва, строки (unix), квоти, партнер з лого-URL.
    active_only — ті, що ще тривають (end_date у майбутньому або не задано);
    сортування: що раніше закінчується — те вище (як у пікері форми таски).
    Блокуючий (MySQL) — з async кликати через asyncio.to_thread."""
    if not db.is_configured():
        return []
    cache_key = f"projects:{active_only}"
    hit = _cached(cache_key)
    if hit is not None:
        return hit
    where = "WHERE (pp.end_date IS NULL OR pp.end_date = 0 OR pp.end_date >= UNIX_TIMESTAMP())" if active_only else ""
    rows = db.query(
        f"""
        SELECT pp.id, pp.name_ua AS name, pp.start_date, pp.end_date,
               pp.kpi_news, pp.kpi_articles,
               p.name_ua AS partner, p.logo AS partner_logo
        FROM partner_project pp
        LEFT JOIN partner p ON p.id = pp.partner_id
        {where}
        ORDER BY (pp.end_date IS NULL OR pp.end_date = 0), pp.end_date ASC, pp.id DESC
        """
    )
    projects = [
        {
            "id": r["id"],
            "name": (r["name"] or "").strip(),
            "start_date": r["start_date"] or None,
            "end_date": r["end_date"] or None,
            "kpi_news": r["kpi_news"] or 0,
            "kpi_articles": r["kpi_articles"] or 0,
            "partner": (r["partner"] or "").strip() or None,
            "logo": partner_logo_url(r["partner_logo"]),
        }
        for r in rows
    ]
    return _remember(cache_key, projects)


def avatar_map(names):
    """{канонічне ім'я ростера: URL фото або None} з users.avatar БД сайту.
    Матч повного імені без регістру, прямий і зворотний порядок слів."""
    result = {n: None for n in names}
    if not db.is_configured():
        return result
    hit = _cached("avatars")
    if hit is None:
        rows = db.query(
            "SELECT first_name, last_name, avatar FROM users "
            "WHERE avatar IS NOT NULL AND avatar != ''"
        )
        by_name = {}
        for r in rows:
            first = (r["first_name"] or "").strip()
            last = (r["last_name"] or "").strip()
            if not (first or last):
                continue
            by_name.setdefault(f"{first} {last}".strip().lower(), r["avatar"])
            by_name.setdefault(f"{last} {first}".strip().lower(), r["avatar"])
        hit = _remember("avatars", by_name)
    for name in names:
        file = hit.get(name.lower())
        if file:
            result[name] = avatar_url(file)
    return result
