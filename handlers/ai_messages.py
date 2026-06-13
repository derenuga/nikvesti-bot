import anthropic
import os

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

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

async def generate_email_reminder(emails, hours, time_of_day):
    senders = list(set([e["sender"].split("<")[0].strip() for e in emails[:5]]))
    senders_text = ", ".join(senders)
    count = len(emails)

    if time_of_day == "afternoon":
        urgency = "помірна, робочий день ще триває"
    else:
        urgency = "висока, день майже закінчився"

    prompt = f"""Ти помічник редакції новинного сайту МикВісті. Напиши коротке неформальне повідомлення в Telegram-чат редакції про непрочитані листи на редакційній пошті.

Дані:
- Непрочитаних листів: {count}
- Найстаріший лист не читався вже: {hours} годин
- Відправники: {senders_text}
- Терміновість: {urgency}

Вимоги:
- Українська мова
- Неформальний, живий тон — як від колеги
- 2-4 речення максимум
- Іноді згадуй що "Катя наругає" або подібні жартівливі фрази (але не завжди)
- Використовуй різні фрази щоразу, не повторюйся
- Можна використати 1-2 емодзі
- НЕ використовуй шаблонні фрази типу "Шановні колеги"

Напиши тільки текст повідомлення, нічого більше."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    return message.content[0].text

async def generate_instagram_weekly_comment(stats, follows, unfollows, total_posts, reels):
    net = follows - unfollows if follows and unfollows else 0

    prompt = f"""Ти помічник редакції новинного сайту МикВісті. Напиши короткий неформальний коментар до тижневого звіту Instagram в Telegram-чат редакції.

Дані за тиждень:
- Нових підписників: +{net} (прийшло {follows}, пішло {unfollows})
- Охоплення: {stats.get('reach', 0)}
- Взаємодії: {stats.get('total_interactions', 0)}
- Публікацій: {total_posts}, з них рілзів: {reels}

Команда Instagram:
- @mskvn1 (Ліза) — керує всім СММ напрямком
- @Imira_91 (Іміра) — розвиває Instagram
- Сергій (монтажер рілзів)

Вимоги:
- Українська мова
- 3-5 речень максимум
- Неформальний живий тон
- Оціни як пройшов тиждень — добре чи є куди рости
- Подякуй команді, згадай @mskvn1, @Imira_91 і Сергія (без тегу, просто на ім'я)
- Можна 1-2 емодзі
- НЕ починай з "Шановні колеги"

Напиши тільки текст, нічого більше."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
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

    prompt = f"""Ти помічник редакції новинного сайту МикВісті. Напиши короткий неформальний коментар до тижневого звіту Facebook в Telegram-чат редакції.

Дані за тиждень:
- Охоплення: {stats.get('page_impressions_unique', 0)}
- Взаємодії: {stats.get('page_post_engagements', 0)}
- Публікацій: {total_posts}, рілзів: {total_reels}
- Автори топ публікацій: {authors_text}

Вимоги:
- Українська мова
- 3-5 речень максимум
- Неформальний живий тон
- Похвали авторів топ публікацій — використовуй їх Telegram username як є (з @ або як HTML посилання)
- Оціни як пройшов тиждень
- Можна 1-2 емодзі
- НЕ починай з "Шановні колеги"

Напиши тільки текст, нічого більше."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text
