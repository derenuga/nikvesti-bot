"""
Генератор каруселей для Instagram — сторінка /carousel на домені Mini App.

Старший брат генератора карток (/card, handlers/card_maker.py): там одна
картинка з заголовком, тут — 4–5 слайдів, які гортають. Флоу: СММ кидає лінк
на новину → агент читає СТАТТЮ і пропонує зміст слайдів → людина в редакторі
править усе (тексти, фото, порядок, типи) → «Завантажити» віддає готові JPG
1080×1440 для ручного постингу.

Агент тут — ЧЕРНЕТКА, а не продукт. Продукт — те, що затвердила людина.

**Точність цитат гарантує КОД, а не промпт.** Головний ризик карусельного
агента — красива цитата, якої ніхто не казав: підпис «Іванов, депутат» під
переказом перетворює переказ на пряму мову. Тому агент не пише текст цитати
ніколи. Він повертає лише НОМЕР цитати в масиві blockquote статті, а текст
підставляє сервер із самої статті (apply_plan_quotes нижче). Скорочувати
довгу цитату агент має право — але тільки ВИКИДАЮЧИ слова: fit_excerpt звіряє
кожне слово фрагмента з оригіналом і його порядком, тож дописати чи
переставити неможливо, а заперечення («не») викидати заборонено окремо.
Не збіглось → на слайд іде повна цитата, і в відповіді лежить позначка. Стаття без
blockquote (322389) слайдів quote не отримує взагалі — просто нема з чого.

**Авторизація.** Сторінка коштує грошей (виклик Sonnet на статтю), тож
відкритою бути не може, як /card чи /back. Вхід — персональний токен, який
видає команда бота /carousel: резолв людини через team_roster, токен у
storage (TTL 30 днів), кнопка з лінком. Сторінка кладе токен у localStorage
і шле в кожен запит. Проксі картинок і сам скрап лишаються відкритими
ендпоінтами card_maker — вони read-only і нічого не коштують.

**Гроші.** Кожен план записується в /aicost і /usage РАЗОМ З ЛЮДИНОЮ
(record_ai_usage(user_id=…)), інакше «хто скільки напитав» показувало б
генерації каруселей як автоматику Лиса. План кешується за id новини: вдруге
відкрита та сама новина не платить, а кнопка «Запропонувати інакше» шле
force=true і переписує кеш.
"""

import asyncio
import json
import os
import re
import secrets
from datetime import datetime, timedelta

from handlers import card_maker, storage
from handlers.helpers import normalize_https_url

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — веб-шар неактивний
    web = None

WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))

_STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp")

_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "carousel_plan_prompt.md")

# Модель: карусель — редакційний текст під іменем видання, тут не місце
# економії на розумі. Sonnet уже є в ai_usage.PRICING.
PLAN_MODEL = "claude-sonnet-5"

# Скільки слайдів просимо в агента. Стеля Instagram при ручному постингу —
# 20 (через API 10), але це межа платформи, а не редакційна: карусель гортають,
# доки цікаво, і на п'ятому екрані більшість уже вийшла.
#
# Було 4–7, потім 3–5, стало 4–5 (Олег, 17.08). Нижню межу підняли назад до
# чотирьох, бо карусель, коротша за це, майже не набирає свайпів, за якими
# стрічка вирішує показувати її ширше. Плюс шостим іде авторський слайд-заклик
# — його редакція вмикає сама, і в ці числа він не входить.
MIN_SLIDES, MAX_SLIDES = 4, 5
# Жорстка стеля редактора разом із фінальним CTA: більше не даємо додати.
HARD_MAX_SLIDES = 8

# Ліміти довжини — ті самі числа, що в промпті агента і в лічильниках UI.
#
# Різко скорочені 17.08 після живих каруселей (Олег): «нам не потрібні стіни
# тексту, нас читають діти 18–19 років в інстаграмі». Попередні 280 знаків на
# абзац агент сприймав як ЦІЛЬ і чесно набивав слайд канцеляритом із
# «влаштуванням відкритої заїзної кишені». Карусель — не переказ статті, а
# лійка до неї: легка адаптація, після якої йдуть читати повний текст.
LIMITS = {"title": 55, "kicker": 20, "body": 150, "quote": 170, "caption": 90}

# Підпис під постом. Instagram ховає все після ~125 знаків за «…ще», тож
# гачок мусить бути в першому рядку, а не в кінці. Стеля з запасом на
# 3–4 короткі абзаци; хештегів у підписі немає свідомо — рішення Олега
# 17.08: «вони не мають сенсу».
POST_CAPTION_MAX = 600

# Текст статті для агента. 12k символів ≈ 4k токенів: довші лонгріди
# ріжемо і чесно про це кажемо в промпті — краще план за початком статті,
# ніж відмова.
MAX_TEXT_CHARS = 12000

SLIDE_TYPES = ("cover", "text", "quote", "photo", "cta")

# Версія логіки плану. План кешується ГОТОВИМ (уже з підставленими цитатами),
# тож зміна правил розбору сама собою старий кеш не лікує: 17.08 хвіст
# «— зазначила вона» лишався на слайді ще довго після фіксу, бо новину вже
# відкривали раніше. Міняти це число щоразу, коли міняється те, ЯК план
# складається; записи іншої версії просто ігноруються.
PLAN_VERSION = 5

# Токен живе довго: СММ відкриває сторінку з закладки, і щоденне ходіння в
# бота по свіжий лінк перетворило б інструмент на квест.
TOKEN_TTL_DAYS = 30

# Заклики фінального слайда. Текст редагований, це лише заготовки.
CTA_PRESETS = {
    "subscribe": {
        "label": "Підписка",
        "text": "Сподобався матеріал? Підпишіться на МикВісті в Instagram — "
                "щодня розповідаємо, що насправді відбувається в Миколаєві.",
    },
    "club": {
        "label": "Клуб МикВісті",
        "text": "Ця робота існує завдяки читачам. Приєднуйтесь до Клубу "
                "МикВісті — і підтримайте незалежну журналістику Миколаєва.",
    },
    "site": {
        "label": "Повний текст",
        "text": "Це лише головне. Повний текст — на nikvesti.com",
    },
}
DEFAULT_CTA = "subscribe"


# ---------- Токени доступу ----------

def _fresh(entry):
    """Чи токен ще живий (виданий не давніше за TTL)."""
    try:
        return datetime.fromisoformat(entry["at"]) > datetime.now() - timedelta(
            days=TOKEN_TTL_DAYS)
    except (KeyError, TypeError, ValueError):
        return False


def token_for(person, tg_id):
    """Персональний вхід людини на сторінку. Живий токен тієї самої людини
    перевикористовуємо: лінк у неї в закладках має лишатись робочим, а не
    змінюватись від кожного /carousel. Блокуючий (пише стан)."""
    tokens = {t: who for t, who in storage.get_carousel_tokens().items()
              if _fresh(who)}
    for token, who in tokens.items():
        if who.get("person") == person:
            return token
    token = secrets.token_urlsafe(16)
    tokens[token] = {"person": person, "tg_id": tg_id,
                     "at": datetime.now().isoformat(timespec="seconds")}
    storage.save_carousel_tokens(tokens)
    return token


def whois(token):
    """{person, tg_id, at} за токеном або None (немає / протух). Блокуючий."""
    if not token:
        return None
    entry = storage.get_carousel_tokens().get(token)
    return entry if entry and _fresh(entry) else None


def carousel_url(token, article=""):
    """Лінк на сторінку з готовим токеном (і одразу новиною, якщо дали)."""
    if not WEBAPP_URL or not token:
        return None
    from urllib.parse import quote
    link = f"{WEBAPP_URL}/carousel?k={quote(token)}"
    if article:
        link += "&url=" + quote(article)
    return link


# ---------- Промпт і схема ----------

_SYSTEM_PROMPT = None


def load_prompt():
    """Промпт агента — окремий файл (конвенція promise_extract_prompt.md):
    правила подачі правлять редактори, а не програміст у рядку коду."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        with open(_PROMPT_FILE, encoding="utf-8") as f:
            _SYSTEM_PROMPT = f.read()
    return _SYSTEM_PROMPT


_PLAN_TOOL = {
    "name": "carousel_plan",
    "description": "План каруселі: слайди по порядку.",
    "input_schema": {
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "description": f"Слайди по порядку, {MIN_SLIDES}–{MAX_SLIDES} штук. "
                               "Перший — завжди cover.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["cover", "text", "quote", "photo"],
                            "description": "cover — обкладинка; text — думка текстом; "
                                           "quote — пряма мова (лише за номером цитати); "
                                           "photo — сильний кадр із підписом",
                        },
                        "kicker": {
                            "type": "string",
                            "description": f"2–4 слова над заголовком, до "
                                           f"{LIMITS['kicker']} знаків (необов'язково)",
                        },
                        "title": {
                            "type": "string",
                            "description": f"Заголовок слайда, до {LIMITS['title']} знаків",
                        },
                        "body": {
                            "type": "string",
                            "description": f"Абзац тексту, до {LIMITS['body']} знаків",
                        },
                        "quote_index": {
                            "type": "integer",
                            "description": "НОМЕР цитати зі списку цитат статті "
                                           "(нумерація з 0). Текст цитати підставить "
                                           "сервер — не пиши його сам.",
                        },
                        "quote_excerpt": {
                            "type": "string",
                            "description": "Скорочена цитата: ті самі слова в тому "
                                           "самому порядку, лише без зайвих. Можна "
                                           "прибрати вставні слова й цілі речення і "
                                           "поправити пунктуацію на шві. Не можна "
                                           "додавати слова, міняти їх місцями і "
                                           "викидати «не».",
                        },
                        "attribution": {
                            "type": "string",
                            "description": "Хто сказав: ім'я і посада, як у статті",
                        },
                        "caption": {
                            "type": "string",
                            "description": f"Підпис до фото, до {LIMITS['caption']} знаків",
                        },
                        "photo_index": {
                            "type": "integer",
                            "description": "Номер фото зі списку фото статті (з 0)",
                        },
                    },
                    "required": ["type"],
                },
            },
            "post_caption": {
                "type": "string",
                "description": "Підпис під постом у стрічці. Перший рядок — "
                               "гачок (до 125 знаків, далі Instagram ховає "
                               "текст). БЕЗ хештегів.",
            },
            "cta_suggestion": {
                "type": "string",
                "enum": list(CTA_PRESETS),
                "description": "Який фінальний заклик доречніший",
            },
        },
        "required": ["slides"],
    },
}


# ---------- Валідація цитат ----------

def _norm_ws(text):
    """Нормалізація пробілів для звірки фрагмента з оригіналом: у HTML вони
    довільні, а нерозривний пробіл на око не відрізниш від звичайного."""
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


# Хвіст атрибуції в кінці прямої мови: «…, — сказала Олена Іванова.»,
# «…», — йдеться у відповіді на запит.» На слайді він зайвий двічі: поле
# «хто сказав» уже є, і воно стоїть окремим рядком (Олег, 17.08: «цитата —
# це цитата, хто сказав — це хто сказав, не мішай у цитату вставні
# конструкції»).
#
# Ріжемо ДЕТЕРМІНОВАНО і тільки ВИДАЛЯЄМО: жодного слова не додається і не
# переставляється, тож обіцянка «текст цитати — з тексту статті» лишається
# в силі. Дієслова взяті з живих цитат 322377 і 322371.
_SPEECH_VERB = (r"(?:сказа|зазнач|додав|додала|повідом|розпов|наголос|поясн|"
                r"заяв|підкресл|відповів|відповіла|йдеться|йшлося|зауваж|"
                r"резюм|підсум|прокоментув|уточн|каже|казав|казала|нагада|"
                r"звернув|запевн|констату|вважає|зізна|поскарж|запропонув|"
                r"запита|вигукн|перекон|іронізу|обурив|обурила)")

_TAIL_RE = re.compile(
    r"[\s,;:]*[—–]\s*(?P<tail>[^—–]{0,140}?" + _SPEECH_VERB + r"[^—–]{0,140})\s*$",
    re.IGNORECASE)

_LEAD_DASH_RE = re.compile(r"^\s*[—–]\s*")

# Слова, які самі по собі нікого не називають: «зазначила вона» підписом бути
# не може, а «сказав Роман Сеник» — може.
_UPPER_RE = re.compile(r"[А-ЯЄІЇҐA-Z]")


def split_speech(text):
    """Цитата → (сама цитата, підказка «хто сказав»).

    Прибирає провідне тире прямої мови, зовнішні «ялинки» і хвіст атрибуції.
    Другим значенням повертає того, кого хвіст назвав, — але лише якщо він
    справді когось назвав: із «зазначила вона» підпису не буває."""
    text = (text or "").strip()
    who = ""

    m = _TAIL_RE.search(text)
    if m:
        text = text[:m.start()].strip()
        tail = re.sub(r"^\W+", "", m.group("tail")).strip(" .")
        # з хвоста лишаємо все, крім самого дієслова мовлення
        rest = re.sub(r"^\S*" + _SPEECH_VERB + r"\S*\s*", "", tail,
                      flags=re.IGNORECASE).strip(" ,.")
        if rest and _UPPER_RE.search(rest):
            who = rest

    text = _LEAD_DASH_RE.sub("", text).strip()
    text = text.rstrip(" ,;:")

    # Зовнішні «ялинки» знімаємо лише коли вони парні й обрамляють усе:
    # усередині цитати теж бувають лапки («Зеленбуд»), і зняти їх не можна
    if (text.startswith("«") and text.endswith("»")
            and text.count("«") == text.count("»")):
        inner = text[1:-1]
        if "»" not in inner or inner.count("«") == inner.count("»"):
            text = inner.strip()

    # Зрізаний хвіст лишає фразу без крапки («…як тільки знайдемо можливість»).
    # Ставимо її: це друкарське завершення речення, а не зміна слів.
    text = text.strip()
    if text and text[-1] not in ".!?…»":
        text += "."
    return text, who


_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)

# Слова, викидання яких МІНЯЄ СЕНС на протилежний. Решту («я вважаю», «свої»,
# «але») скорочувати можна й треба, а це — ні.
_NEGATIONS = {
    "не", "ні", "ані", "без", "нема", "немає", "ніколи", "ніхто", "ніщо",
    "жоден", "жодна", "жодне", "жодних", "жодного", "жодної", "жодним",
    "неможливо", "заборонено",
}

MIN_EXCERPT_WORDS = 4


def fit_excerpt(excerpt, original):
    """Чи справді скорочення зроблене З ОРИГІНАЛУ — і його готовий текст.

    Перевірка не по підрядку, а ПО СЛОВАХ: слова фрагмента мусять іти в тому
    самому порядку, що в оригіналі, і жодного нового слова бути не може.
    Викидати ж можна будь-що. Це рішення Олега (17.08) і звичайна редакційна
    практика — «Я мер Миколаєва і я вважаю, що завжди виконуватиму обіцянки»
    чесно коротшає до «Я мер Миколаєва. Завжди виконуватиму обіцянки»: слова
    ті самі, зникли лише шумові, а крапка й велика літера на шві — це
    пунктуація, а не переписування. Трикрапка при цьому не обовʼязкова: у
    картинці вона зайва, її роль тут виконує сама верстка.

    Один запобіжник, без якого правило було б небезпечним: **заперечення
    викидати не можна**. «Не буде ремонту, поки не знайдуть кошти» → «буде
    ремонт» пройшло б будь-яку перевірку «зі слів оригіналу», а сенс дало б
    протилежний. Тому якщо викинуте «не» стояло ПЕРЕД словом, яке лишилось,
    фрагмент відхиляється. Викинути цілу фразу разом із її «не» — можна:
    тоді зникає і те, що воно заперечувало.

    Повертає текст фрагмента або None, якщо він не складається з оригіналу."""
    orig_words = _WORD_RE.findall((original or "").lower())
    frag_words = _WORD_RE.findall((excerpt or "").lower())
    if len(frag_words) < MIN_EXCERPT_WORDS or not orig_words:
        return None

    kept = set()
    j = 0
    for word in frag_words:
        while j < len(orig_words) and orig_words[j] != word:
            j += 1
        if j >= len(orig_words):
            return None      # слова немає в оригіналі або воно не в тому порядку
        kept.add(j)
        j += 1

    for i, word in enumerate(orig_words):
        if i not in kept and word in _NEGATIONS and (i + 1) in kept:
            return None      # викинули заперечення, а заперечене лишили

    return excerpt.strip()


def apply_plan_quotes(slides, quotes):
    """Підставляє в quote-слайди ТЕКСТ ІЗ СТАТТІ і карає вигадки.

    Правило одне: текст цитати береться з quotes[quote_index], і більше
    нізвідки. Агент може попросити коротший фрагмент (quote_excerpt) — тоді
    фрагмент має дослівно входити в оригінал після нормалізації пробілів.
    Не входить або номер поза списком → слайд деградує у text (з поміткою),
    бо порожній слайд чесніший за вигадану цитату.

    Повертає (slides, notes) — notes іде в відповідь, щоб людина бачила, що
    саме бот не прийняв, а не гадала, чому слайдів менше.
    """
    notes = []
    out = []
    for n, slide in enumerate(slides, 1):
        slide = dict(slide)
        if slide.get("type") != "quote":
            slide.pop("quote_index", None)
            slide.pop("quote_excerpt", None)
            out.append(slide)
            continue

        idx = slide.get("quote_index")
        if not isinstance(idx, int) or not 0 <= idx < len(quotes):
            notes.append(f"Слайд {n}: агент послався на цитату №{idx}, "
                         f"а в статті їх {len(quotes)} — зробив текстовим.")
            out.append(_degrade(slide))
            continue

        original = quotes[idx]
        excerpt = (slide.get("quote_excerpt") or "").strip()
        if excerpt:
            text = fit_excerpt(excerpt, original)
            if text is None:
                notes.append(f"Слайд {n}: агент обрізав цитату не дослівно — "
                             f"поставив повну з тексту статті.")
                text = original
        else:
            text = original

        speech, who = split_speech(text)
        slide["quote"] = speech or text
        slide["quote_index"] = idx
        # Агент часто лишає підпис порожнім, бо ім'я стоїть у хвості самої
        # цитати, який ми щойно зрізали. Підставляємо його, а не втрачаємо.
        if who and not (slide.get("attribution") or "").strip():
            slide["attribution"] = who
        slide.pop("quote_excerpt", None)
        out.append(slide)
    return out, notes


def _degrade(slide):
    """Quote-слайд без надійної цитати → звичайний текстовий."""
    slide = dict(slide)
    slide["type"] = "text"
    slide.pop("quote_index", None)
    slide.pop("quote_excerpt", None)
    slide.pop("quote", None)
    if not slide.get("body") and slide.get("attribution"):
        slide["body"] = slide["attribution"]
    slide.pop("attribution", None)
    return slide


def normalize_plan(plan, article):
    """Приводить відповідь агента до того, що вміє намалювати редактор.

    Це не «про всяк випадок»: модель охоче віддає photo_index поза списком,
    quote-слайд без цитати або десять слайдів замість семи, і кожен такий
    випадок у редакторі виглядав би поламаною сторінкою.
    """
    quotes = article.get("quotes") or []
    photos = article.get("images") or []

    raw = [s for s in (plan.get("slides") or []) if isinstance(s, dict)]
    raw = [s for s in raw if s.get("type") in ("cover", "text", "quote", "photo")]
    raw = raw[:MAX_SLIDES]

    slides, notes = apply_plan_quotes(raw, quotes)

    clean = []
    for slide in slides:
        idx = slide.get("photo_index")
        if not isinstance(idx, int) or not 0 <= idx < len(photos):
            idx = None
        # Слайд-фото без фото — це порожній слайд; хай буде текстовим
        if slide["type"] == "photo" and idx is None:
            slide = dict(slide, type="text")
            if not slide.get("body"):
                slide["body"] = slide.pop("caption", "") or ""
        slide["photo_index"] = idx
        for key, limit in (("title", "title"), ("kicker", "kicker"),
                           ("body", "body"), ("caption", "caption")):
            if slide.get(key):
                slide[key] = str(slide[key]).strip()[:LIMITS[limit] * 2]
        clean.append(slide)

    # Перший слайд — завжди обкладинка: лічильник, стрілка й пропорція
    # каруселі рахуються від нього, а «обкладинка посередині» це не варіант
    # подачі, а помилка агента.
    if clean and clean[0]["type"] != "cover":
        clean[0] = dict(clean[0], type="cover")
    if clean and not clean[0].get("title"):
        clean[0]["title"] = article.get("title") or ""
    if clean and clean[0].get("photo_index") is None and photos:
        clean[0]["photo_index"] = 0

    cta = plan.get("cta_suggestion")
    return {
        "slides": clean,
        "cta_suggestion": cta if cta in CTA_PRESETS else DEFAULT_CTA,
        "post_caption": clean_caption(plan.get("post_caption")),
        "notes": notes,
    }


# Хештеги в підписі не потрібні (рішення Олега 17.08: «вони не мають сенсу»),
# і просити про це промпт — половина заходу: модель усе одно час від часу дописує
# «#Миколаїв #новини» в кінець. Тому зрізаємо їх кодом — так само, як усе
# інше, що не має доїхати до людини.
_HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")


def clean_caption(text):
    """Підпис під пост: без хештегів, без порожніх хвостів, у межах стелі."""
    text = _HASHTAG_RE.sub(" ", str(text or ""))
    lines = [ln.strip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:POST_CAPTION_MAX]


# ---------- Агент ----------

# Підписи, за якими видно, що це не ілюстрація, а папір. Реальний привід
# (Олег, 17.08.2026, стаття 322371 про «Іскру»): з трьох фото статті два —
# «Скриншот акту обстеження будівлі у 2019 році». У тексті вони на місці, на
# весь екран у стрічці Instagram — ні. Тому такі кадри позначені і для
# агента (щоб не ставив на обкладинку), і в редакторі (щоб людина бачила, що
# треба підвантажити своє фото об'єкта).
_DOC_PHOTO_RE = re.compile(
    r"скрин|скрін|screenshot|документ|акт\b|витяг|таблиц|інфографік|"
    r"допис|пост\b|переписк|лист\b|довідк|звіт\b", re.IGNORECASE)


def better_caption(image, body):
    """Підпис фото: спершу той, що стоїть під фото в тілі статті, і лише
    потім JSON-LD.

    Порядок саме такий, бо в тілі лежить те, що бачить читач, і воно буває
    правильнішим: у 322371 підпис у JSON-LD виявився чужим текстом (помилка
    сервісу автопідписів, яку CMS зберегла), а під самим фото стояло
    «Скриншот акту обстеження будівлі у 2019 році». Фільтра «схоже на
    сміття» тут свідомо немає: агенції в підписах пишуться латиною («Reuters»,
    «AFP»), і будь-яке таке правило рано чи пізно з'їло б справжній кредит."""
    return ((body.get("photo_titles") or {}).get(
        card_maker._img_key(image.get("url") or ""))
        or (image.get("caption") or "").strip())


def looks_like_document(caption):
    """Чи підпис каже, що на фото папір, а не об'єкт зйомки."""
    return bool(_DOC_PHOTO_RE.search(caption or ""))


def build_prompt(article, force=False):
    """Матеріал для агента: заголовок, опис, ПРОНУМЕРОВАНІ фото й цитати,
    текст статті. Нумерація тут — це контракт: агент повертає номери, сервер
    за ними бере текст і картинки."""
    lines = [
        f"ЗАГОЛОВОК СТАТТІ: {article.get('title') or '—'}",
        f"ОПИС (лід): {article.get('description') or '—'}",
        "",
    ]

    photos = article.get("images") or []
    if photos:
        lines.append("ФОТО СТАТТІ (номер — те, що ставиш у photo_index):")
        for i, im in enumerate(photos):
            cap = (im.get("caption") or "").strip()
            mark = "  ⚠ схоже на скриншот документа — не для обкладинки" \
                if looks_like_document(cap) else ""
            lines.append(f"  [{i}] {cap or 'без підпису'}{mark}")
        if all(looks_like_document(im.get("caption")) for im in photos):
            lines.append("  Усі фото статті — папір. Кольорові слайди без фото "
                         "тут будуть кращі; редакція підвантажить свої кадри.")
    else:
        lines.append("ФОТО СТАТТІ: немає — усі слайди будуть кольоровими, "
                     "photo_index не став.")
    lines.append("")

    quotes = article.get("quotes") or []
    if quotes:
        lines.append("ЦИТАТИ СТАТТІ (номер — те, що ставиш у quote_index; "
                     "текст НЕ переписуй):")
        for i, q in enumerate(quotes):
            lines.append(f"  [{i}] {q}")
    else:
        lines.append("ЦИТАТ У СТАТТІ НЕМАЄ — слайдів типу quote бути не може "
                     "взагалі. Не вигадуй пряму мову.")
    lines.append("")

    text = "\n\n".join(article.get("paragraphs") or [])
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n\n[текст статті обрізано — далі йде "
        text += "продовження, якого ти не бачиш; не посилайся на нього]"
    lines += ["ТЕКСТ СТАТТІ:", text or "—"]

    if force:
        lines += [
            "",
            "УВАГА: попередній варіант плану редакцію не влаштував. Зайди з "
            "ІНШОГО КУТА — інша головна думка на обкладинці, інший порядок, "
            "інший набір слайдів. Не переставляй слова в тому самому плані.",
        ]
    return "\n".join(lines)


def clip_words(text, limit):
    """Обрізка по межі слова: «на протиаварій» замість половини слова.
    Різати посеред слова можна в лічильнику, але не в тому, що піде в кадр."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + "…"


def fake_plan(article):
    """План без моделі — для тестів (env CAROUSEL_FAKE_PLAN=1) і для випадку,
    коли ANTHROPIC_API_KEY не налаштований. Не «заглушка про всяк випадок»:
    ганяти живий Sonnet у кожному прогоні Playwright ні до чого, а без ключа
    редактор має відкриватись і працювати руками."""
    photos = article.get("images") or []
    quotes = article.get("quotes") or []
    paragraphs = article.get("paragraphs") or []

    # Скриншоти актів і таблиць у чернетку не ставимо — те саме правило, що
    # й для агента: на весь екран у стрічці папір не читається.
    usable = [i for i, im in enumerate(photos)
              if not looks_like_document(im.get("caption"))]

    slides = [{
        "type": "cover",
        "title": clip_words(article.get("title"), LIMITS["title"]),
        "photo_index": usable[0] if usable else None,
    }]
    for i, para in enumerate(paragraphs[:2]):
        slides.append({
            "type": "text",
            "body": clip_words(para, LIMITS["body"]),
            "photo_index": usable[i + 1] if i + 1 < len(usable) else None,
        })
    if quotes:
        slides.append({"type": "quote", "quote_index": 0, "attribution": ""})
    if len(paragraphs) > 2:
        slides.append({"type": "text",
                       "body": clip_words(paragraphs[2], LIMITS["body"])})
    lead = (article.get("description") or article.get("title") or "").strip()
    return {"slides": slides, "cta_suggestion": DEFAULT_CTA,
            "post_caption": clip_words(lead, 200)}


# ---------- Правка ОДНОГО слайда за підказкою людини ----------
#
# Привід (Олег, 17.08): агент написав «Гучне свято — у сусідів» про Тернопіль,
# а Тернопіль Миколаєву не сусід — він на заході, ми на півдні. Думка при
# цьому хороша, переробляти всю карусель через один невдалий рядок безглуздо.
#
# Межу тут тримає КОД, а не прохання в промпті: ендпоінт віддає лише текстові
# поля ОДНОГО слайда, і сторінка вливає рівно їх. Навіть якщо модель вирішить
# переписати всю карусель, далі цього списку її відповідь не пройде — фото,
# кроп, тип, кегль, порядок і сусідні слайди недосяжні за побудовою.
REVISABLE_FIELDS = ("kicker", "title", "body", "attribution", "caption",
                    "quote_index", "quote_excerpt")
# У режимі «додай ще один слайд» агент має право обрати ще й тип і фото —
# слайда ж не існує. На правці наявного ці поля лишаються недосяжними: там
# тип і кадр уже обрала людина.
NEW_SLIDE_FIELDS = REVISABLE_FIELDS + ("type", "photo_index")
NEW_SLIDE_TYPES = ("text", "quote", "photo")

_SLIDE_TOOL = {
    "name": "slide_fix",
    "description": "Виправлений текст ОДНОГО слайда.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kicker": {"type": "string", "description": f"до {LIMITS['kicker']} знаків"},
            "title": {"type": "string", "description": f"до {LIMITS['title']} знаків"},
            "body": {"type": "string", "description": f"до {LIMITS['body']} знаків"},
            "quote_index": {"type": "integer",
                            "description": "лише для слайда-цитати: номер цитати статті"},
            "quote_excerpt": {"type": "string",
                              "description": "скорочена цитата за тими самими "
                                             "правилами: тільки викидання слів"},
            "attribution": {"type": "string", "description": "хто сказав"},
            "caption": {"type": "string", "description": "підпис до фото"},
            "type": {"type": "string", "enum": list(NEW_SLIDE_TYPES),
                     "description": "лише для НОВОГО слайда: який він"},
            "photo_index": {"type": "integer",
                            "description": "лише для НОВОГО слайда: номер фото"},
            "post_caption": {"type": "string",
                             "description": "лише для правки підпису під постом"},
            "note": {"type": "string",
                     "description": "одне речення людині: що саме ти змінив"},
        },
        "required": ["note"],
    },
}


def build_revise_prompt(article, slide, feedback, others, mode="fix"):
    """Матеріал для правки або для нового слайда: стаття, наявні слайди і
    підказка. Наявні слайди показуються лише щоб не повторити те, що вже
    сказано на інших екранах, — міняти їх не можна."""
    lines = [
        f"СТАТТЯ: {article.get('title') or '—'}",
        f"ЛІД: {article.get('description') or '—'}",
        "",
    ]
    quotes = article.get("quotes") or []
    if quotes:
        lines.append("ЦИТАТИ СТАТТІ (номер — quote_index):")
        lines += [f"  [{i}] {q}" for i, q in enumerate(quotes)]
        lines.append("")

    text = "\n\n".join(article.get("paragraphs") or [])
    lines += ["ТЕКСТ СТАТТІ:", text[:MAX_TEXT_CHARS] or "—", ""]

    photos = article.get("images") or []
    if mode == "new" and photos:
        lines.append("ФОТО СТАТТІ (номер — photo_index):")
        for i, im in enumerate(photos):
            cap = (im.get("caption") or "").strip()
            mark = "  ⚠ схоже на скриншот документа" \
                if looks_like_document(cap) else ""
            lines.append(f"  [{i}] {cap or 'без підпису'}{mark}")
        lines.append("")

    if mode == "caption":
        lines.append("ПОТОЧНИЙ ПІДПИС ПІД ПОСТОМ:")
        lines.append((slide or {}).get("caption") or "(порожній)")
        lines.append("")
    elif slide:
        lines.append(f"СЛАЙД, ЯКИЙ ТРЕБА ВИПРАВИТИ (тип {slide.get('type')}):")
        for key in ("kicker", "title", "body", "quote", "attribution", "caption"):
            if slide.get(key):
                lines.append(f"  {key}: {slide[key]}")
        lines.append("")

    if others:
        lines.append("СЛАЙДИ, ЩО ВЖЕ Є (лише щоб не повторюватись — НЕ чіпай їх):")
        for n, other in others:
            bits = " · ".join(str(other.get(k)) for k in
                              ("title", "body", "quote", "caption") if other.get(k))
            lines.append(f"  {n}. [{other.get('type')}] {bits[:160]}")
        lines.append("")

    if feedback.strip():
        lines += ["ЩО КАЖЕ РЕДАКТОР:", feedback.strip(), ""]

    if mode == "caption":
        lines += [
            "ЗАВДАННЯ: перепиши ПІДПИС під постом (поле post_caption).",
            "Слайди не чіпай — їх ти тут не міняєш узагалі.",
            "Виклич інструмент slide_fix.",
        ]
    elif mode == "new":
        lines += [
            "ЗАВДАННЯ: додай ОДИН новий слайд до цієї каруселі.",
            "Він мусить давати те, чого на наявних слайдах ЩЕ НЕМАЄ — не",
            "переказувати вже сказане іншими словами. Обери тип сам (text,",
            "quote або photo) і напиши його поля." ,
        ]
        if not feedback.strip():
            lines.append("Редактор не сказав, про що саме — вирішуй за статтею: "
                         "візьми найсильніше з того, що ще не використано.")
        lines.append("Виклич інструмент slide_fix.")
    else:
        lines += [
            "Виправ ЛИШЕ цей слайд і лише те, про що йдеться. Тип слайда не",
            "міняй. Поля, яких правка не стосується, повертай без змін. Якщо",
            "редактор каже, що думка хороша, — збережи її, зміни формулювання.",
            "Виклич інструмент slide_fix.",
        ]
    return "\n".join(lines)


async def revise_slide(article, slide, feedback, others=(), *, mode="fix",
                       user_id=None, user_name=None):
    """Агент править один слайд за підказкою (mode="fix") або складає один
    новий (mode="new"). Повертає (поля, нотатка людині, зауваження)."""
    from handlers.ai_messages import async_client

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("агент вимкнений: немає ANTHROPIC_API_KEY")

    msg = await async_client.messages.create(
        model=PLAN_MODEL,
        max_tokens=900,
        thinking={"type": "disabled"},
        system=load_prompt(),
        tools=[_SLIDE_TOOL],
        tool_choice={"type": "tool", "name": "slide_fix"},
        messages=[{"role": "user",
                   "content": build_revise_prompt(article, slide, feedback,
                                                  others, mode)}],
    )
    _record_usage(msg.usage, user_id, user_name)
    raw = next((b.input for b in msg.content if b.type == "tool_use"), None)
    if not raw:
        raise ValueError("агент не повернув правку")

    # Беремо ЛИШЕ дозволені поля — решту відповіді ігноруємо мовчки
    if mode == "caption":
        caption = clean_caption(raw.get("post_caption"))
        return ({"post_caption": caption} if caption else {}), \
            (raw.get("note") or "").strip(), []
    allowed = NEW_SLIDE_FIELDS if mode == "new" else REVISABLE_FIELDS
    fixed = {k: raw[k] for k in allowed if k in raw}
    note = (raw.get("note") or "").strip()

    if mode == "new":
        if fixed.get("type") not in NEW_SLIDE_TYPES:
            fixed["type"] = "text"
        photos = article.get("images") or []
        idx = fixed.get("photo_index")
        if not isinstance(idx, int) or not 0 <= idx < len(photos):
            fixed["photo_index"] = None
        slide = {"type": fixed["type"]}

    notes = []
    if slide.get("type") == "quote":
        # Цитата проходить ту саму перевірку, що й у плані: текст ставить код
        merged = dict(slide, **fixed)
        if not isinstance(merged.get("quote_index"), int):
            merged["quote_index"] = slide.get("quote_index")
        checked, notes = apply_plan_quotes([merged], article.get("quotes") or [])
        for key in ("quote", "quote_index", "attribution"):
            if key in checked[0]:
                fixed[key] = checked[0][key]
        # Цитата не склалась — слайд деградував у текстовий, і в режимі
        # «новий слайд» це має доїхати до сторінки, інакше вона намалює
        # порожню цитату
        if mode == "new" and checked[0].get("type") != "quote":
            fixed["type"] = checked[0].get("type", "text")
            fixed.pop("quote", None)
    fixed.pop("quote_excerpt", None)

    for key, limit in (("title", "title"), ("kicker", "kicker"),
                       ("body", "body"), ("caption", "caption")):
        if fixed.get(key):
            fixed[key] = str(fixed[key]).strip()[:LIMITS[limit] * 2]
    return fixed, note, notes


async def make_plan(article, *, force=False, user_id=None, user_name=None):
    """Виклик агента → нормалізований план. Кидає винятки виклику назовні:
    вирішує ендпоінт (сторінка має відкритись і без плану)."""
    from handlers.ai_messages import async_client

    if os.environ.get("CAROUSEL_FAKE_PLAN") == "1":
        return normalize_plan(fake_plan(article), article), "fake"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return normalize_plan(fake_plan(article), article), "nokey"

    msg = await async_client.messages.create(
        model=PLAN_MODEL,
        max_tokens=2000,
        thinking={"type": "disabled"},
        system=load_prompt(),
        tools=[_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "carousel_plan"},
        messages=[{"role": "user", "content": build_prompt(article, force)}],
    )
    _record_usage(msg.usage, user_id, user_name)
    raw = next((b.input for b in msg.content if b.type == "tool_use"), None)
    if not raw:
        raise ValueError("агент не повернув план")
    return normalize_plan(raw, article), "live"


def _record_usage(usage, user_id, user_name):
    """Облік вартості РАЗОМ З ЛЮДИНОЮ: сторінку відкривають самі СММ, і в
    /aicost ці гроші мають лежати під їхніми іменами, а не в «автоматиці
    Лиса». Збій обліку не має ламати генерацію."""
    try:
        storage.record_ai_usage(
            PLAN_MODEL,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            user_id=user_id,
            user_name=user_name,
            feature="carousel",   # → рядок «Каруселі для Instagram» в /aicost
        )
    except Exception as e:
        print(f"carousel: облік витрат не записався — {e}")


# ---------- Скрап ----------

def scrape_article(url):
    """Дані статті для каруселі: те саме, що бачить /card, ПЛЮС повний текст,
    цитати по порядку і картка автора. Блокуючий (мережа)."""
    import requests

    resp = requests.get(url, timeout=card_maker.FETCH_TIMEOUT,
                        headers={"User-Agent": card_maker._UA})
    resp.raise_for_status()
    html_text, final_url = resp.text, resp.url

    data = card_maker.parse_article(html_text)
    body = card_maker.parse_article_body(html_text)
    data["images"] = [dict(im, caption=better_caption(im, body))
                      for im in data.get("images") or []]

    # Цитати беремо З ТІЛА статті, а не з усього документа: нумерація —
    # частина контракту з агентом, і вона мусить рахуватись від того самого
    # контейнера, з якого взято абзаци.
    data["quotes"] = body["quotes"]
    data["paragraphs"] = body["paragraphs"]
    data["author"] = body["author"]
    data["final_url"] = final_url if card_maker._host_allowed(final_url) else url
    data["article_id"] = article_id_from(data["final_url"])

    author = data.get("author")
    if author and author.get("photo"):
        # Велика аватарка для круглого фото на CTA-слайді. Перевіряємо саме
        # content-type: ресайзер на розмір, якого немає, віддає 200 з
        # порожнім тілом і text/html, тобто «успіх» за кодом брехливий.
        big = card_maker.author_photo_url(author["photo"])
        author["photo_big"] = big if card_maker.probe_image(big) else author["photo"]

    return data


def article_id_from(url):
    """id новини з URL (…/322371-slug) — ключ кеша планів і імена файлів."""
    m = re.search(r"/(\d{4,})(?:-|$)", url or "")
    return m.group(1) if m else ""


# ---------- HTTP ----------

def _token_of(request):
    return (request.headers.get("X-Carousel-Key")
            or request.query.get("k") or "").strip()


async def _require_person(request):
    """Хто прийшов. Повертає запис або кидає 403 зі зрозумілим текстом."""
    who = await asyncio.to_thread(whois, _token_of(request))
    if not who:
        raise web.HTTPForbidden(
            text=json.dumps({"error": "Лінк застарів або чужий. Попроси "
                                      "свіжий у бота командою /carousel."},
                            ensure_ascii=False),
            content_type="application/json")
    return who


_page_html = None


def render_page():
    """carousel.html із версійованим лінком на cardkit.js.

    Сторінка віддається без кеша, а ядро лежить у /static з довгим — без
    ?v=<хеш> після деплою можна було б отримати свіжу сторінку зі старим
    ядром. Той самий прийом, що для app.js в апці, лише рахований тут, щоб
    не тягнути webapp у carousel (webapp уже імпортує carousel — вийшло б
    кільце). Читаємо раз на процес."""
    global _page_html
    if _page_html is None:
        import hashlib
        with open(os.path.join(_STATIC_DIR, "carousel.html"), encoding="utf-8") as f:
            html = f.read()
        try:
            with open(os.path.join(_STATIC_DIR, "cardkit.js"), "rb") as f:
                version = hashlib.sha256(f.read()).hexdigest()[:10]
            html = html.replace("/static/cardkit.js",
                                f"/static/cardkit.js?v={version}")
        except OSError as e:
            print(f"carousel: версія cardkit.js не порахувалась — {e}")
        _page_html = html
    return _page_html


async def page(request):
    """GET /carousel — сторінка редактора. Без токена теж віддається: сама
    сторінка покаже, що треба взяти лінк у бота (білий екран нічого не
    пояснює, а людина зазвичай просто відкрила стару закладку)."""
    return web.Response(
        text=await asyncio.to_thread(render_page),
        content_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


async def api_plan(request):
    """POST /api/carousel/plan {url, force} → {article, plan, source}.

    Один запит на весь вхід: сторінці однаково потрібні і дані статті, і
    план, а скрапити ту саму сторінку двічі — марно палити ліміти сайту.
    """
    who = await _require_person(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)

    url = (body.get("url") or "").strip()
    if re.fullmatch(r"\d{4,}", url):
        url = "https://nikvesti.com/" + url   # сайт сам редіректить на слаг
    if not card_maker._host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лише посилання nikvesti.com"}, status=400)
    force = bool(body.get("force"))

    import requests
    try:
        article = await asyncio.to_thread(scrape_article, url)
    except requests.RequestException as e:
        return web.json_response(
            {"error": f"Не вдалося прочитати сторінку: {e}"}, status=502)
    if not article.get("title") and not article.get("paragraphs"):
        return web.json_response(
            {"error": "Схоже, це не сторінка матеріалу — ні заголовка, "
                      "ні тексту не знайшлось"}, status=422)

    article_id = article.get("article_id") or ""
    if article_id and not force:
        cached = await asyncio.to_thread(storage.get_carousel_plan, article_id)
        if cached and cached.get("plan") and cached.get("v") == PLAN_VERSION:
            return web.json_response({
                "article": _public_article(article),
                "plan": cached["plan"],
                "source": "cache",
                "planned_at": cached.get("at"),
                "planned_by": cached.get("person"),
            })

    try:
        plan, source = await make_plan(
            article, force=force,
            user_id=who.get("tg_id"), user_name=who.get("person"))
    except Exception as e:
        print(f"carousel: агент не склав план — {e}")
        # Сторінка має відкритись і без агента: людина збере слайди руками,
        # а не дивитиметься на червоне повідомлення замість редактора.
        return web.json_response({
            "article": _public_article(article),
            "plan": None,
            "source": "error",
            "error": f"Агент не склав план ({e}). Слайди можна зібрати руками.",
        })

    if article_id and source in ("live", "fake"):
        await asyncio.to_thread(storage.save_carousel_plan, article_id, {
            "plan": plan,
            "v": PLAN_VERSION,
            "at": datetime.now().isoformat(timespec="seconds"),
            "person": who.get("person"),
        })
    return web.json_response({
        "article": _public_article(article),
        "plan": plan,
        "source": source,
    })


# ---------- Чернетки ----------
#
# Нової бази для цього не треба: чернетка — це кілька кілобайтів JSON, і
# лежить вона там само, де токени й кеш планів (файл стану на Volume).
# Ключ «людина:новина», тож повторне збереження тієї самої каруселі
# перезаписує чернетку, а не плодить копії.
#
# У чернетці НЕМАЄ ні статті, ні картинок: стаття тягнеться заново при
# відкритті (це безкоштовно й заразом свіже), а власні фото з диска живуть
# у браузері як blob-посилання, які після перезавантаження сторінки мертві
# в принципі. Тому їх кількість записана числом — щоб сказати людині, що
# саме доведеться підвантажити ще раз, а не мовчки віддати слайд без фото.

def _draft_key(person, article_id):
    return f"{person}:{article_id}"


async def api_drafts(request):
    """GET /api/carousel/drafts — чернетки цієї людини, свіжі першими."""
    who = await _require_person(request)
    drafts = await asyncio.to_thread(storage.get_carousel_drafts)
    mine = [d for k, d in drafts.items()
            if d.get("person") == who.get("person")]
    mine.sort(key=lambda d: d.get("at", ""), reverse=True)
    return web.json_response({"drafts": mine})


async def api_draft_save(request):
    """POST /api/carousel/draft — зберегти чернетку (перезаписує свою ж)."""
    who = await _require_person(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)

    url = (body.get("url") or "").strip()
    article_id = article_id_from(url)
    if not card_maker._host_allowed(url) or not article_id:
        return web.json_response({"error": "немає лінка новини"}, status=400)

    entry = {
        "person": who.get("person"),
        "url": url,
        "article_id": article_id,
        "title": (body.get("title") or "")[:200],
        "slides": body.get("slides") or [],
        "cta": body.get("cta") or {},
        "caption": clean_caption(body.get("caption")),
        "own_photos": int(body.get("own_photos") or 0),
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    await asyncio.to_thread(
        storage.save_carousel_draft, _draft_key(who.get("person"), article_id), entry)
    return web.json_response({"ok": True, "at": entry["at"]})


async def api_draft_delete(request):
    """DELETE /api/carousel/draft?id=<article_id> — прибрати свою чернетку."""
    who = await _require_person(request)
    article_id = (request.query.get("id") or "").strip()
    if not article_id:
        return web.json_response({"error": "немає id"}, status=400)
    await asyncio.to_thread(
        storage.delete_carousel_draft, _draft_key(who.get("person"), article_id))
    return web.json_response({"ok": True})


async def api_article(request):
    """GET /api/carousel/article?url=… — дані статті БЕЗ виклику агента.

    Потрібне для відкриття чернетки: слайди в ній свої, а фото, цитати й
    текст статті беруться свіжими. Грошей не коштує."""
    await _require_person(request)
    url = (request.query.get("url") or "").strip()
    if not card_maker._host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лише посилання nikvesti.com"}, status=400)
    import requests
    try:
        article = await asyncio.to_thread(scrape_article, url)
    except requests.RequestException as e:
        return web.json_response(
            {"error": f"Не вдалося прочитати сторінку: {e}"}, status=502)
    return web.json_response({"article": _public_article(article)})


async def api_revise(request):
    """POST /api/carousel/slide {url, slide, others, feedback} → правка ОДНОГО
    слайда. Відповідь несе лише текстові поля — усе інше сторінка лишає своє."""
    who = await _require_person(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "не JSON"}, status=400)

    feedback = (body.get("feedback") or "").strip()
    mode = body.get("mode") if body.get("mode") in ("new", "caption") else "fix"
    slide = body.get("slide") or {}
    # Для нового слайда підказка НЕ обовʼязкова: «додай ще один» — теж завдання
    if mode == "fix" and not feedback:
        return web.json_response({"error": "Напиши, що саме поправити"}, status=400)
    if mode == "fix" and (not isinstance(slide, dict)
                          or slide.get("type") not in SLIDE_TYPES):
        return web.json_response({"error": "не той слайд"}, status=400)

    url = (body.get("url") or "").strip()
    if not card_maker._host_allowed(url):
        return web.json_response(
            {"error": "Приймаються лише посилання nikvesti.com"}, status=400)

    import requests
    try:
        article = await asyncio.to_thread(scrape_article, url)
    except requests.RequestException as e:
        return web.json_response(
            {"error": f"Не вдалося прочитати статтю: {e}"}, status=502)

    others = [(n, o) for n, o in enumerate(body.get("others") or [], 1)
              if isinstance(o, dict)][:8]
    try:
        fixed, note, notes = await revise_slide(
            article, slide if mode == "fix" else None, feedback, others,
            mode=mode, user_id=who.get("tg_id"), user_name=who.get("person"))
    except Exception as e:
        print(f"carousel: правка слайда не вдалась — {e}")
        return web.json_response({"error": f"Агент не впорався: {e}"}, status=502)

    return web.json_response({"slide": fixed, "note": note, "notes": notes})


def _public_article(article):
    """Те, що потрібне редактору. Абзаци теж віддаємо: людина часто хоче
    взяти в слайд саме речення зі статті, а не переказ агента."""
    return {
        "title": article.get("title") or "",
        "description": article.get("description") or "",
        "url": article.get("final_url") or "",
        "article_id": article.get("article_id") or "",
        "images": [dict(im, doc_like=looks_like_document(im.get("caption")))
                   for im in (article.get("images") or [])],
        "quotes": article.get("quotes") or [],
        # Розібрана цитата для редактора: у слайд іде speech, у поле «хто
        # сказав» — who. Розбір живе в ОДНОМУ місці (Python), інакше сторінка
        # і сервер різали б хвости по-різному.
        "quote_parts": [
            dict(zip(("speech", "who"), split_speech(q)))
            for q in (article.get("quotes") or [])
        ],
        "paragraphs": article.get("paragraphs") or [],
        "author": article.get("author"),
        "is_ad": bool(article.get("is_ad")),
    }


async def api_state(request):
    """GET /api/carousel/state — хто я і що вміє сторінка. Перше, що вона
    питає: без валідного токена показуємо підказку, а не порожній редактор."""
    who = await asyncio.to_thread(whois, _token_of(request))
    if not who:
        return web.json_response({"ok": False,
                                  "hint": "Візьми свіжий лінк у бота: /carousel"},
                                 status=403)
    return web.json_response({
        "ok": True,
        "person": who.get("person"),
        "cta_presets": CTA_PRESETS,
        "default_cta": DEFAULT_CTA,
        "limits": LIMITS,
        "max_slides": HARD_MAX_SLIDES,
    })


# ---------- Команда бота ----------

async def carousel_handler(update, context):
    """/carousel [лінк|id] — персональний вхід на сторінку каруселей."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from handlers import team_roster

    user = update.effective_user
    person = await asyncio.to_thread(
        team_roster.resolve_person, user.id, user.username)
    if not person:
        await update.message.reply_text(
            "🦊 Не впізнав тебе в ростері редакції — сторінка каруселей "
            "іменна. Напиши Олегу, і тебе додадуть.")
        return

    arg = " ".join(context.args or []).strip()
    article = card_maker._host_allowed(arg) and article_id_from(arg) or ""
    if not article and re.fullmatch(r"\d{4,}", arg):
        article = arg
    if arg and not article:
        await update.message.reply_text(
            "Це не схоже на лінк новини nikvesti.com. Приклад:\n"
            "<code>/carousel https://nikvesti.com/322371</code>",
            parse_mode="HTML")
        return

    token = await asyncio.to_thread(token_for, person, user.id)
    link = carousel_url(token, article)
    if not link:
        await update.message.reply_text(
            "Сторінка каруселей спить: не налаштований WEBAPP_URL.")
        return

    lines = [
        "🦊 <b>Карусель для Instagram</b>",
        "Кинь лінк новини — прочитаю статтю і запропоную слайди. "
        "Далі правиш усе руками й забираєш готові JPG 1080×1440.",
        "",
        "<i>Лінк особистий — він твій вхід, не пересилай його.</i>",
    ]
    if update.effective_chat.type != "private":
        # З 17.08.2026 команда працює і в приваті — приватний фільтр бота
        # пускає туди КОМАНДИ від будь-кого з ростера (bot.check_allowed), а
        # не лише трьох людей із ALLOWED_USER_IDS. Рядок лишається, бо він
        # знімає інше побоювання: у чаті лінк виглядає спільним, а він
        # особистий і працює тільки для того, хто покликав.
        lines.insert(3, "Команду можна кликати звідси, з чату — лінк однаково "
                        "особистий.")

    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🖼 Відкрити генератор", url=link)]]))


def carousel_button(article_id, text="🖼 Зробити карусель"):
    """Кнопка в генератор каруселей для інших модулів. None без WEBAPP_URL —
    і None без токена: вхід іменний, тож кнопку в нікуди не малюємо."""
    return None if not WEBAPP_URL or not article_id else text
