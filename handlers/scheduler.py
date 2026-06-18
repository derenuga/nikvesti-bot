import os
import random
import requests
import anthropic
from datetime import datetime

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Фіксовані координати Миколаєва (НЕ змінювати на q="Mykolaiv,UA" — назва міста
# через геокодер OpenWeatherMap не завжди резолвиться в потрібне місто і може
# давати випадкові/застарілі дані).
MYKOLAIV_LAT = 46.9750
MYKOLAIV_LON = 31.9946


def get_mykolaiv_weather():
    """
    Прогноз погоди на СЬОГОДНІ (а не "поточна погода о моменту запиту").
    Використовує безкоштовний endpoint /data/2.5/forecast (крок 3 години,
    5 днів наперед) і агрегує всі точки, що припадають на сьогоднішню дату
    за місцевим часом, в один денний підсумок: мін/макс температура,
    переважний опис погоди, чи очікується дощ/гроза, вологість і вітер
    беруться як середні по дню.
    """
    try:
        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            "lat": MYKOLAIV_LAT,
            "lon": MYKOLAIV_LON,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "uk",
        }
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        today = datetime.now().strftime("%Y-%m-%d")
        today_points = [
            p for p in data.get("list", [])
            if p.get("dt_txt", "").startswith(today)
        ]

        # Якщо на сьогодні лишилось мало/нема точок (наприклад вже вечір),
        # беремо найближчі доступні точки, щоб не лишитись без даних.
        if not today_points:
            today_points = data.get("list", [])[:4]

        if not today_points:
            return None

        temps = [p["main"]["temp"] for p in today_points]
        feels = [p["main"]["feels_like"] for p in today_points]
        humidity = [p["main"]["humidity"] for p in today_points]
        wind = [p["wind"]["speed"] for p in today_points]

        # Переважний опис погоди — беремо найчастіший серед денних точок
        descriptions = [p["weather"][0]["description"] for p in today_points]
        main_description = max(set(descriptions), key=descriptions.count)

        # Чи очікується дощ/гроза/снігопад протягом дня
        rain_codes = {"rain", "thunderstorm", "snow", "drizzle"}
        will_rain = any(p["weather"][0]["main"].lower() in rain_codes for p in today_points)

        return {
            "temp_min": round(min(temps)),
            "temp_max": round(max(temps)),
            "feels_like": round(sum(feels) / len(feels)),
            "description": main_description,
            "humidity": round(sum(humidity) / len(humidity)),
            "wind": round(sum(wind) / len(wind)),
            "will_rain": will_rain,
        }
    except Exception:
        return None


# Набір "фокусів" для ранкового повідомлення. Щодня обирається випадкова
# підмножина (2-3 з них), а не всі завжди — щоб повідомлення не виглядали
# як шаблон, що просто переписується іншими словами щоранку.
MESSAGE_FOCUS_OPTIONS = [
    "коротке практичне нагадування про дедлайни на сьогодні",
    "загальне нагадування не забувати про поточні задачі і соцмережі",
    "мотиваційна думка чи цитата, не обов'язково про журналістику",
    "жарт пов'язаний з журналістикою або українською політикою",
    "коротка цікава дрібничка чи факт (можна про Миколаїв, можна геть не пов'язане з роботою)",
    "просто тепле особисте звернення без конкретних завдань — день відпочинку від нагадувань",
    "питання до редакції (риторичне чи реальне) замість порад",
]


async def generate_morning_message(weather):
    if weather:
        if weather["temp_min"] == weather["temp_max"]:
            temp_text = f"{weather['temp_min']}°C"
        else:
            temp_text = f"від {weather['temp_min']}°C до {weather['temp_max']}°C"

        rain_note = "очікується дощ" if weather["will_rain"] else "без дощу"

        weather_text = (
            f"Сьогодні протягом дня температура {temp_text} "
            f"(відчувається як {weather['feels_like']}°C), {weather['description']}, "
            f"{rain_note}, вологість {weather['humidity']}%, вітер {weather['wind']} м/с."
        )
    else:
        weather_text = "погода невідома"

    # Обираємо 2-3 випадкові фокуси замість фіксованого списку з 6 пунктів щоразу
    chosen_focus = random.sample(MESSAGE_FOCUS_OPTIONS, k=random.choice([2, 3]))
    focus_text = "\n".join(f"- {f}" for f in chosen_focus)

    prompt = f"""Ти — Лис Микита, бот редакції новинного сайту МикВісті (Миколаїв, Україна).

Напиши ранкове привітання для редакції. Сьогодні {datetime.now().strftime('%A, %d.%m.%Y')}.

Прогноз погоди в Миколаєві на сьогодні: {weather_text}

Сьогодні повідомлення зроби НЕ за шаблоном — оберіть довільний тон і структуру.
Обов'язково включи:
1. Привітання (можна іносказально, але десь має прозвучати "дорога редакція")
2. Погоду на сьогодні

Додатково (вибери щось із цього, не обов'язково все і не в одному порядку щодня):
{focus_text}

Вимоги:
- Українська мова
- Неформальний живий тон, кожного разу інша структура і інші слова — уникай повторення фраз і конструкцій з попередніх днів
- 4-7 речень
- 1-3 емодзі за смаком (не обов'язково розкидані по кожному абзацу)
- НЕ пиши кожного разу однакові фрази типу "не забувайте стежити за задачами" — це вже набридло, придумай по-новому або взагалі пропусти
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
