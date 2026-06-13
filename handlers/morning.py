import os
import requests
import anthropic
from datetime import datetime

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_mykolaiv_weather():
    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": "Mykolaiv,UA",
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "uk"
        }
        response = requests.get(url, params=params)
        data = response.json()
        temp = round(data["main"]["temp"])
        feels_like = round(data["main"]["feels_like"])
        description = data["weather"][0]["description"]
        humidity = data["main"]["humidity"]
        wind = round(data["wind"]["speed"])
        return {
            "temp": temp,
            "feels_like": feels_like,
            "description": description,
            "humidity": humidity,
            "wind": wind
        }
    except:
        return None

async def generate_morning_message(weather):
    weather_text = ""
    if weather:
        weather_text = (
            f"Температура: {weather['temp']}°C (відчувається як {weather['feels_like']}°C), "
            f"{weather['description']}, вологість {weather['humidity']}%, "
            f"вітер {weather['wind']} м/с."
        )
    else:
        weather_text = "погода невідома"

    prompt = f"""Ти — Лис Микита, бот редакції новинного сайту МикВісті (Миколаїв, Україна).

Напиши ранкове привітання для редакції. Сьогодні {datetime.now().strftime('%A, %d.%m.%Y')}.

Погода в Миколаєві зараз: {weather_text}

Повідомлення має містити:
1. Привітання "Доброго ранку, дорога редакція!" (можна іносказально але щоб обов'язково було словосполучення "дорога редакція")
2. Погода в Миколаєві
3. Побажання продуктивного робочого дня
4. Нагадування слідкувати за поточними задачами і не забувати постити матеріали у соцмережі
5. Побажання доброго настрою
6. Жарт пов'язаний з журналістикою або українською політикою (дотепний, не образливий)

Вимоги:
- Українська мова
- Неформальний живий тон
- 5-8 речень
- Між блоками (привітання, погода, задачі, жарт) роби порожній рядок
- 1-2 емодзі
- додатково 2-3 емодзі, можна використати емодзі погоди (☀️🌧️⛅🌤️❄️💨🌫️) для опису погоди
- НЕ занадто офіційно

Напиши тільки текст повідомлення."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

async def send_morning_message(bot, chat_id):
    try:
        weather = get_mykolaiv_weather()
        text = await generate_morning_message(weather)
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        print("Помилка ранкового повідомлення: " + str(e))

async def morning_handler(update, context):
    try:
        weather = get_mykolaiv_weather()
        text = await generate_morning_message(weather)
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text("Помилка: " + str(e))
