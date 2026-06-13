import anthropic
import os

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
