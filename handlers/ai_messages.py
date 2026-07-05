import anthropic
import os
from datetime import datetime

from handlers import storage

TEAM = {
    "Олег Деренюга": {
        "tg": "@derenuga",
        "role": "керівник і візіонер МикВісті",
        "birthday": "30.04",
    },
    "Катерина Середа": {
        "tg": "@sereda_ka",
        "role": "головна редакторка",
        "birthday": "07.01",
    },
    "Юлія Бойченко": {
        "tg": "@boichenko13",
        "role": "лід-журналістка, великі аналітичні матеріали",
        "birthday": "13.12",
    },
    "Аліса Мелікадамян": {
        "tg": "@lislislisalisa",
        "role": "лід-журналістка і судова репортерка, випускова редакторка",
        "birthday": "23.09",
    },
    "Світлана Іванченко": {
        "tg": "@Lana_PRpolka",
        "role": "редакторка стрічки новин",
        "birthday": "10.10",
    },
    "Альона Коханчук": {
        "tg": "@Aliona_Banu",
        "role": "лід-журналістка, екологія і новини громад Миколаївщини",
        "birthday": "12.10",
    },
    "Аліна Квітко": {
        "tg": "@Aliniskv",
        "role": "лід-журналістка, міськрада, підзвітність влади, локальна демократія",
        "birthday": "10.03",
    },
    "Марія Хаміцевич": {
        "tg": '<a href="tg://user?id=846178524">Марія</a>',
        "role": "редакторка стрічки новин, афіші «Куди піти в Миколаєві»",
        "birthday": "17.02",
    },
    "Сергій Овчаришин": {
        "tg": '<a href="tg://user?id=891685789">Сергій</a>',
        "role": "оператор, фотограф, відеопродюсер",
        "birthday": "11.08",
    },
    "Єлизавета Москвіна": {
        "tg": "@mskvn1",
        "role": "керівниця соцмереж, діджіталу і дистрибуції",
        "birthday": "21.04",
    },
    "Іміра Борухова": {
        "tg": "@Imira_91",
        "role": "провідна SMM-спеціалістка, Instagram",
        "birthday": "21.08",
    },
    "Даріна Мельничук": {
        "tg": "@dariimlk",
        "role": "журналістка — і в кадрі, і в матеріалах, молода зірка редакції",
        "birthday": "28.04",
    },
    "Юлія Лук'яненко": {
        "tg": "@Yuliia_Lukianenko",
        "role": "досвідчена журналістка",
        "birthday": "12.10",
    },
    "Таміла Ксьонжик": {
        "tg": "@tamilissssa",
        "role": "журналістка, прийшла на практику і залишилась",
        "birthday": "07.03",
    },
    "Кристина Леонова": {
        "tg": "@skxxlw",
        "role": "журналістка, прийшла на практику і залишилась",
        "birthday": None,
    },
    "Кирил Витвицький": {
        "tg": "@simada24",
        "role": "помічник оператора і фотограф, учень Сергія",
        "birthday": None,
    },
    "Ірина Федорович": {
        "tg": "@diiessa",
        "role": "перекладачка, англійська версія сайту",
        "birthday": "24.05",
    },
}

# Для зворотної сумісності з модулями що використовують TEAM_TELEGRAM
TEAM_TELEGRAM = {name: info["tg"] for name, info in TEAM.items()}

FOX_SYSTEM_PROMPT = """Ти — Лис Микита, внутрішній AI-помічник редакції МикВісті. Твоя роль — працювати з редакційними сигналами: помічати, звіряти, нагадувати й підсвічувати те, що може бути важливим для команди.
Ти не замінюєш журналістів і не ухвалюєш редакційні рішення. Пиши українською, коротко, живо й конкретно. Лисячість — це уважність, хитрість, спостережливість і редакційний нюх, а не постійна рольова гра. Не вигадуй фактів, не перебільшуй, не повторюй однакові вступи й приглушуй персонажність у чутливих темах."""

# Стратегія моделей (REVIEW_2026_07.md, п. б.3):
# SMART — аналітика і головні тексти (ранкове, тижневі звіти з цифрами, NLQ-роутер);
# FAST — короткі "смакові" тексти (ДН, нагадування про пошту, підводки) —
# у рази дешевше і швидше, на 2-4 реченнях різниці в якості не видно.
FOX_MODEL_SMART = "claude-sonnet-5"
FOX_MODEL_FAST = "claude-haiku-4-5"

async_client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


async def fox_generate(prompt, *, system=FOX_SYSTEM_PROMPT, model=FOX_MODEL_FAST, max_tokens=300):
    """Єдина точка всіх AI-викликів Лиса (крім tool-use циклу в query_router).

    Асинхронний клієнт — виклик НЕ блокує event loop бота (REVIEW п. б.1).
    Зміна моделі, retry, облік вартості — робляться тут, а не по всьому коду.
    system=None — промпт самодостатній, системний промпт не надсилається.
    """
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if model == FOX_MODEL_SMART:
        # У Sonnet 5 з пропущеним thinking вмикається adaptive thinking —
        # для коротких стилістичних текстів воно з'їдає max_tokens без користі.
        kwargs["thinking"] = {"type": "disabled"}
    message = await async_client.messages.create(**kwargs)
    _record_usage(model, message.usage)
    text = "".join(b.text for b in message.content if b.type == "text")
    return clean_ai_text(text)


def _record_usage(model, usage):
    """Облік вартості (REVIEW в.5) — тихо, збій обліку не має ламати генерацію."""
    try:
        storage.record_ai_usage(
            model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception as e:
        print(f"ai_usage: не вдалось записати — {e}")


def clean_ai_text(text):
    """Прибирає Markdown-форматування (*bold*, _italic_) з AI-тексту.
    Telegram не рендерить Markdown в HTML-режимі — зірочки і підкреслення
    залишаються як є і псують вигляд повідомлення."""
    return text.replace("*", "").replace("_", " ")


def get_todays_birthdays():
    """Повертає список членів команди, у яких сьогодні день народження."""
    today = datetime.now().strftime("%d.%m")
    return [
        (name, info)
        for name, info in TEAM.items()
        if info.get("birthday") and info["birthday"] == today
    ]


async def generate_birthday_greeting(name, info):
    """Привітання з днем народження для члена команди."""
    prompt = f"""Привітай колегу з днем народження в редакційний Telegram-чат.

Іменинник: {name}
Роль в редакції: {info['role']}

2-3 речення. Тепло, щиро, з гумором якщо доречно. Згадай їх роль або внесок у редакцію — але природно, без пафосу. Згадай тег {info['tg']} щоб людина побачила. Різні формулювання щоразу."""

    return await fox_generate(prompt, model=FOX_MODEL_FAST, max_tokens=200)


async def generate_email_reminder(emails, hours, time_of_day):
    senders = list(set([e["sender"].split("<")[0].strip() for e in emails[:5]]))
    senders_text = ", ".join(senders)
    count = len(emails)
    urgency = "робочий день ще триває" if time_of_day == "afternoon" else "день майже закінчився"

    prompt = f"""Непрочитані листи на редакційній пошті — нагадай редакції.

Дані:
- Листів: {count}
- Найстаріший без відповіді: {hours} год
- Відправники: {senders_text}
- Час: {urgency}

2-4 речення, неформально. Іноді згадуй що "Катя наругає" або подібне (але не завжди). Різні формулювання щоразу. 1-2 емодзі максимум."""

    return await fox_generate(prompt, model=FOX_MODEL_FAST, max_tokens=300)


async def generate_instagram_weekly_comment(stats, follows, unfollows, total_posts, reels):
    net = follows - unfollows if follows and unfollows else 0

    prompt = f"""Тижневий звіт Instagram МикВісті — напиши коротку підводку.

Дані:
- Нових підписників: +{net} (прийшло {follows}, пішло {unfollows})
- Охоплення: {stats.get('reach', 0)}
- Взаємодії: {stats.get('total_interactions', 0)}
- Публікацій: {total_posts}, рілзів: {reels}

Команда: @mskvn1 (Ліза — керує всім діджіталом), @Imira_91 (Іміра — Instagram), Сергій (монтаж рілзів).

3-5 речень. Оціни тиждень, подякуй команді — згадай усіх трьох. 1-2 емодзі."""

    return await fox_generate(prompt, model=FOX_MODEL_SMART, max_tokens=300)


async def generate_facebook_weekly_comment(stats, top_authors, total_posts, total_reels):
    authors_with_tg = []
    for author in top_authors:
        info = TEAM.get(author)
        if info:
            authors_with_tg.append(f"{author} ({info['tg']})")
        else:
            authors_with_tg.append(author)
    authors_text = ", ".join(authors_with_tg) if authors_with_tg else "невідомі автори"

    prompt = f"""Тижневий звіт Facebook МикВісті — напиши коротку підводку.

Дані:
- Перегляди: {stats.get('page_media_view', 0)}
- Взаємодії: {stats.get('page_post_engagements', 0)}
- Публікацій: {total_posts}, рілзів: {total_reels}
- Автори топ публікацій: {authors_text}

3-5 речень. Згадай що це фейсбук. Похвали авторів — використовуй їх Telegram username як є. 1-2 емодзі."""

    return await fox_generate(prompt, model=FOX_MODEL_SMART, max_tokens=300)


async def generate_english_monthly_comment(
    period_label,
    users, users_prev,
    sessions, sessions_prev,
    pageviews, pageviews_prev,
    returning, returning_pct,
    eng_pct, pages_per_session,
    top_en_pages,
    top_countries, top_referrers, top_queries,
):
    """AI-коментар Лиса Микити для місячного звіту англійської версії сайту."""

    def pct(curr, prev):
        if prev == 0:
            return "н/д"
        diff = round((curr - prev) / prev * 100)
        return f"+{diff}%" if diff >= 0 else f"{diff}%"

    countries_text = ", ".join([f"{c} ({v})" for c, v in top_countries[:3]])
    en_titles = "\n".join([f"- {title}" for _, title, _, _ in top_en_pages[:3]])
    referrers_text = ", ".join([f"{src} ({cnt})" for src, cnt in top_referrers[:3]])
    queries_text = ", ".join([f"'{q}' ({c} кліків, поз. {p})" for q, c, _, p in top_queries[:5]]) if top_queries else "немає даних"

    prompt = f"""Місячний звіт англійської версії МикВісті — коротка аналітична підводка.

Період: {period_label}
Користувачі: {users} ({pct(users, users_prev)})
Перегляди: {pageviews} ({pct(pageviews, pageviews_prev)})
Повторні читачі: {returning_pct}% аудиторії
Залученість: {eng_pct}%, {pages_per_session} стор/сесію
Топ країни: {countries_text}
Топ матеріали: {en_titles}
Реферери: {referrers_text}
Пошукові запити Google: {queries_text}

3 речення максимум. Вибери одну-дві найцікавіші деталі — пошукові запити, несподівана країна, тренд у темах. Без переліку цифр — вони вже є вище. Без тегів людей — вони додаються окремо."""

    return await fox_generate(prompt, model=FOX_MODEL_SMART, max_tokens=250)


async def generate_weekly_digest_comment(period_label, cur, prev, top_titles,
                                         tender_count, tender_amount_mln):
    """AI-підводка для «Тижневика Лиса»: тиждень сайту в цифрах з порівнянням
    тиждень-до-тижня. cur/prev — dict users/sessions/pageviews."""

    def pct(c, p):
        if not p:
            return "н/д"
        d = round((c - p) / p * 100)
        return f"+{d}%" if d >= 0 else f"{d}%"

    titles = "\n".join(f"- {t}" for t in top_titles[:3]) or "—"
    prompt = f"""Тижневий редакційний дайджест МикВісті ({period_label}) — коротка аналітична підводка Лиса.

Сайт, тиждень до тижня:
- Користувачі: {cur['users']} ({pct(cur['users'], prev['users'])})
- Сесії: {cur['sessions']} ({pct(cur['sessions'], prev['sessions'])})
- Перегляди: {cur['pageviews']} ({pct(cur['pageviews'], prev['pageviews'])})
Топ матеріали тижня:
{titles}
Тендери винюхано: {tender_count} на {tender_amount_mln} млн грн

3-4 речення. Чесно оціни тиждень для сайту (зростання чи спад — не прикрашай), підсвіть одну-дві деталі: що витягнуло трафік, помітний тренд. Без переліку всіх цифр — вони вже є вище. 1-2 емодзі."""

    return await fox_generate(prompt, model=FOX_MODEL_SMART, max_tokens=300)


async def generate_silence_reminder(channel_username, silence_hours):
    """Нагадування редакції про мовчання телеграм-каналу (викликається з scheduler)."""
    prompt = f"""Телеграм-канал @{channel_username} мовчить вже {int(silence_hours)} годин(и).
Напиши коротке (2-3 речення) обережне нагадування редакції українською мовою.
ОБОВ'ЯЗКОВО вкажи в тексті платформу - Телеграм та назву каналу — @{channel_username} (саме так, з @, не пропускай і не заміняй на загальне слово "канал" без назви).
Запитай чи немає новини для публікації, або запропонуй знайти якусь національну подію.
Неформальний тон, без тиску. Можна 1 емодзі."""

    return await fox_generate(prompt, model=FOX_MODEL_FAST, max_tokens=200)
