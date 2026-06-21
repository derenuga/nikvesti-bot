import anthropic
import os
import random

TEAM_TELEGRAM = {
    "Олег Деренюга": "@derenuga",
    "Катерина Середа": "@sereda_ka",
    "Юлія Бойченко": "@boichenko13",
    "Аліса Мелікадамян": "@lislislisalisa",
    "Світлана Іванченко": "@Lana_PRpolka",
    "Альона Коханчук": "@Aliona_Banu",
    "Аліна Квітко": "@Aliniskv",
    "Марія Хаміцевич": '<a href="tg://user?id=846178524">Марія</a>',
    "Сергій Овчаришин": '<a href="tg://user?id=891685789">Сергій</a>',
    "Єлизавета Москвіна": "@mskvn1",
    "Іміра Борухова": "@Imira_91",
    "Даріна Мельничук": "@dariimlk",
    "Олена Бондаренко": '<a href="tg://user?id=191642941">Олена</a>',
    "Юлія Лук'яненко": "@Yuliia_Lukianenko",
    "Таміла Ксьонжик": "@tamilissssa",
    "Кристина Леонова": "@skxxlw",
}

# Runtime ядро особистості — йде в API як system для всіх функцій.
# Повна lore bible — у FOX_LORE.md (для розробників, не в API).
FOX_SYSTEM_PROMPT = """Ти — Лис Микита, внутрішній AI-помічник редакції МикВісті. Твоя роль — працювати з редакційними сигналами: помічати нові документи, тендери, новини конкурентів, аналітику, листи, соцмережі, календарі, старі теми й інші речі, які редакції варто побачити.

Ти не замінюєш журналістів і не ухвалюєш редакційні рішення. Ти допомагаєш не пропустити важливе: підсвічуєш факти, зв'язки, ризики, незавершені теми й потенційні зачіпки.

Пиши українською, коротко, живо й конкретно. Лисячість — це уважність, спостережливість, трохи іронії і редакційний нюх, а не постійна рольова гра. Не вигадуй фактів, не перебільшуй, не повторюй однакові вступи й приглушуй персонажність у чутливих темах."""

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


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
    return message.content[0].text


async def generate_instagram_weekly_comment(stats, follows, unfollows, total_posts, reels):
    net = follows - unfollows if follows and unfollows else 0

    prompt = f"""Тижневий звіт Instagram МикВісті — напиши коротку підводку.

Дані:
- Нових підписників: +{net} (прийшло {follows}, пішло {unfollows})
- Охоплення: {stats.get('reach', 0)}
- Взаємодії: {stats.get('total_interactions', 0)}
- Публікацій: {total_posts}, рілзів: {reels}

Команда: @mskvn1 (Ліза — керує СММ), @Imira_91 (Іміра — Instagram), Сергій (монтаж рілзів).

3-5 речень. Оціни тиждень, подякуй команді — згадай усіх трьох. 1-2 емодзі."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=FOX_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


async def generate_facebook_weekly_comment(stats, top_authors, total_posts, total_reels):
    authors_with_tg = []
    for author in top_authors:
        tg = TEAM_TELEGRAM.get(author)
        if tg:
            authors_with_tg.append(f"{author} ({tg})")
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
    return message.content[0].text


async def generate_competitors_intro(sources_with_items):
    """
    Підводка перед списком новин конкурентів.
    sources_with_items: список (source_name, [news_titles])
    """
    # Збираємо заголовки для контексту
    lines = []
    for source_name, items in sources_with_items:
        titles = [i["title"] for i in items[:3]]
        lines.append(f"{source_name}: {', '.join(titles)}")
    news_summary = "\n".join(lines)

    # Пул тональностей — Python обирає одну, щоб AI не повторювався
    tones = [
        "коротко і нейтрально — просто сигнал що є нові новини",
        "з легкою іронією — лис підглянув що пишуть сусіди",
        "по-редакційному сухо — без коментарів, просто факт",
        "трохи колегіально — може ви вже бачили, але на всяк випадок",
        "спостережливо — відмітити що тема перетинається з нашою або цікава",
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
    return message.content[0].text
