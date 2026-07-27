"""
Telegram-пости за дисклеймером — зарахування виконання поза сайтом.

Метод Олега: у `partner_project` є `disclaimer_ua` («Матеріал підготовлено за
підтримки…»). Якщо текст поста каналу містить дисклеймер проєкту — пост точно
цього проєкту. Жодного AI тут не треба: це буквальний збіг рядка.

Чого метод НЕ дає — автора. У пості каналу його немає ніде, тож такий матч
ЗАВЖДИ йде в чергу Каті (`pending`), навіть коли проєкт визначено однозначно:
вирішити треба і кому зарахувати, і в яку тематику.

Джерело постів — веб-версія каналу (t.me/s/nikvesti), той самий шлях і ті самі
дзеркала, що в telegram_stats: індекс storage.tg_posts тримає лише
message_id ↔ стаття, тексту постів у ньому немає.

Дешевизна прогону тримається на двох речах:
  • нічого, крім збігів, не записуємо — інакше в таблиці осідали б тисячі
    звичайних постів каналу;
  • уже вирішені (source='telegram') відсікаються через known_refs, тож ті
    самі сторінки можна перечитувати щогодини.
"""

import re

from bs4 import BeautifulSoup

from handlers import db, team_matches
from handlers.telegram_stats import CHANNEL, _fetch_html, _parse_message_block

# Скільки сторінок стрічки дивимось за прогін. Сторінка t.me/s — ~20 постів,
# дві покривають кілька годин каналу з запасом на нічний простій.
SCAN_PAGES = 2

# Дисклеймер мусить бути осмисленої довжини: короткий рядок збігся б з
# половиною каналу і зарахував би що завгодно.
MIN_DISCLAIMER_LEN = 40

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

# Лапки і тире в CMS і в Telegram різні («ялинки», &laquo;, типографське тире),
# а дисклеймер у БД ще й із HTML-розмітки — тому порівнюємо нормалізовані рядки.
_QUOTES = {"«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "‟": '"',
           "‘": "'", "’": "'", "–": "-", "—": "-", " ": " "}


def normalize(text):
    """HTML → плаский рядок у нижньому регістрі, з уніфікованими лапками й
    пробілами. Саме в такому вигляді порівнюємо дисклеймер і текст поста."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&laquo;", '"').replace("&raquo;", '"')
    text = text.replace("&quot;", '"').replace("&amp;", "&")
    for src, dst in _QUOTES.items():
        text = text.replace(src, dst)
    return _SPACE_RE.sub(" ", text).strip().lower()


def project_disclaimers():
    """{project_id: нормалізований дисклеймер} з БД сайту. Порожні й надто
    короткі відкидаємо — див. MIN_DISCLAIMER_LEN."""
    if not db.is_configured():
        return {}
    rows = db.query(
        "SELECT id, disclaimer_ua FROM partner_project "
        "WHERE disclaimer_ua IS NOT NULL AND disclaimer_ua <> ''"
    )
    out = {}
    for r in rows:
        text = normalize(r["disclaimer_ua"])
        if len(text) >= MIN_DISCLAIMER_LEN:
            out[int(r["id"])] = text
    return out


_BG_URL_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)")


def _post_image(block):
    """Картинка поста зі стрічки t.me: вона віддається не тегом <img>, а
    style="background-image:url(...)" на обгортці прев'ю. Окремого запиту не
    треба — HTML уже в руках."""
    wrap = block.find("a", class_="tgme_widget_message_photo_wrap")
    if not wrap:
        return None
    m = _BG_URL_RE.search(wrap.get("style", "") or "")
    return m.group(1) if m else None


def fetch_recent_posts(pages=SCAN_PAGES):
    """Останні пости каналу з ТЕКСТОМ: [{message_id, url, text, dt}]."""
    posts = []
    before = None
    for _ in range(pages):
        html = _fetch_html(f"/s/{CHANNEL}", params={"before": before} if before else None)
        soup = BeautifulSoup(html, "html.parser")
        blocks = soup.find_all("div", class_="tgme_widget_message")
        if not blocks:
            break
        page_min = None
        for block in blocks:
            msg_id, _views, _hrefs, dt = _parse_message_block(block)
            if msg_id is None:
                continue
            page_min = msg_id if page_min is None else min(page_min, msg_id)
            body = block.find("div", class_="tgme_widget_message_text")
            posts.append({
                "message_id": msg_id,
                "url": f"https://t.me/{CHANNEL}/{msg_id}",
                "text": body.get_text(" ", strip=True) if body else "",
                "image": _post_image(block),
                "dt": dt,
            })
        if page_min is None or page_min <= 1:
            break
        before = page_min
    return posts


def match_by_disclaimer(posts, disclaimers):
    """[(пост, project_id)] — пости, у тексті яких є дисклеймер проєкту."""
    out = []
    for post in posts:
        text = normalize(post["text"])
        if not text:
            continue
        for project_id, disclaimer in disclaimers.items():
            if disclaimer in text:
                out.append((post, project_id))
                break
    return out


def post_title(post):
    """Перший рядок поста як заголовок картки в черзі."""
    text = _SPACE_RE.sub(" ", (post["text"] or "").strip())
    return text[:120] + ("…" if len(text) > 120 else "") or f"Пост {post['message_id']}"


def post_description(post):
    """Опис для картки — те, що йде після заголовкового рядка поста."""
    text = _SPACE_RE.sub(" ", (post["text"] or "").strip())
    tail = text[120:] if len(text) > 120 else ""
    return (tail[:300] + ("…" if len(tail) > 300 else "")) or None


def scan_posts(pages=SCAN_PAGES):
    """Прогін: знайти проєктні пости й покласти їх у чергу Каті.
    Повертає (скільки постів переглянули, скільки нових пішло в чергу)."""
    disclaimers = project_disclaimers()
    if not disclaimers:
        return 0, 0
    posts = fetch_recent_posts(pages)
    hits = match_by_disclaimer(posts, disclaimers)
    if not hits:
        return len(posts), 0
    seen = team_matches.known_refs("telegram", [p["message_id"] for p, _ in hits])
    queued = 0
    for post, project_id in hits:
        if str(post["message_id"]) in seen:
            continue
        # person=None НАВМИСНО: автора поста не знає ніхто, тож рішення
        # «кому зарахувати» лишається за Катею
        match = team_matches.record_match(
            "telegram", post["message_id"], "pending",
            project_id=project_id, person=None, confidence=None,
            reasoning="Пост каналу з дисклеймером цього проєкту — "
                      "автора Telegram не показує, тож кому зарахувати, "
                      "вирішує редакторка.",
            title=post_title(post), url=post["url"],
            # Опис і картинка для картки — з самого поста: текст уже маємо,
            # прев'ю лежить у тому ж HTML, тож жодного зайвого запиту
            description=post_description(post), image=post.get("image"),
            published=int(post["dt"].timestamp()) if post["dt"] else None,
            node_type="post")
        if match:
            queued += 1
    return len(posts), queued
