import anthropic
import os
import random
from datetime import datetime

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

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


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

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)


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

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)


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

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)


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
- Охоплення: {stats.get('page_impressions_unique', 0)}
- Взаємодії: {stats.get('page_post_engagements', 0)}
- Публікацій: {total_posts}, рілзів: {total_reels}
- Автори топ публікацій: {authors_text}

3-5 речень. Згадай що це фейсбук. Похвали авторів — використовуй їх Telegram username як є. 1-2 емодзі."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)


async def generate_competitors_intro(sources_with_items):
    """Підводка перед списком новин конкурентів."""
    lines = []
    for source_name, items in sources_with_items:
        titles = [i["title"] for i in items[:3]]
        lines.append(f"{source_name}: {', '.join(titles)}")
    news_summary = "\n".join(lines)

    tones = [
        "коротко і нейтрально — просто сигнал що є нові новини",
        "з легкою іронією — лис підглянув що пишуть сусіди",
        "по-редакційному сухо — без коментарів, просто факт",
        "трохи колегіально — може ви вже бачили, але на всяк випадок",
        "спостережливо — відмітити що тема може перетинатись з нашою",
    ]
    chosen_tone = random.choice(tones)

    prompt = f"""Перед списком новин інших миколаївських медіа напиши коротку підводку (1-3 речення).

Новини які зараз будуть показані:
{news_summary}

Тон сьогодні: {chosen_tone}

Не переказуй заголовки — вони будуть нижче. Не називай себе лисом прямо. Не починай з "Я". Різні формулювання щоразу."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)


async def generate_english_monthly_comment(period_label, users, users_prev, sessions, sessions_prev, top_pages, top_countries, top_referrers):
    """AI-коментар Лиса Микити для місячного звіту англійської версії сайту."""

    def pct(curr, prev):
        if prev == 0:
            return "н/д"
        diff = round((curr - prev) / prev * 100)
        return f"+{diff}%" if diff >= 0 else f"{diff}%"

    countries_text = ", ".join([f"{c} ({v})" for c, v in top_countries[:3]])
    referrers_text = ", ".join([f"{src} ({cnt} сесій)" for src, cnt in top_referrers[:3]])
    pages_text = "\n".join([f"- {title}" for _, title, _ in top_pages[:3]])

    prompt = f"""Місячний звіт англійської версії МикВісті — напиши коротку аналітичну підводку.

Період: {period_label}
Користувачі: {users} ({pct(users, users_prev)} до попереднього місяця)
Сесії: {sessions} ({pct(sessions, sessions_prev)} до попереднього місяця)
Топ країни (без Сінгапуру — там були боти): {countries_text}
Топ матеріали:
{pages_text}
Реферери: {referrers_text}

3-5 речень. Зверни увагу на щось цікаве або несподіване в даних — країни, тренд, популярні теми. Можеш зробити припущення чому так. Звернись до Іри (@diiessa) — вона перекладачка і їй буде цікаво. Не перелічуй всі цифри ще раз — вони вже є у звіті вище."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=350,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)
