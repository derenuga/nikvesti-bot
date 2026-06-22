import os
import random
import requests
import anthropic
from datetime import datetime
from handlers.events import get_today_events, format_events_for_prompt, format_events_html
from handlers.ai_messages import clean_ai_text

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


# Набір "фокусів" — додаткові теми, які МОЖУТЬ з'явитись у повідомленні.
# Кожного разу обирається випадкова підмножина (0-2), і модель явно
# попереджена, що 0 — теж нормальний варіант.
MESSAGE_FOCUS_OPTIONS = [
    "коротке практичне нагадування про дедлайни на сьогодні",
    "нагадування не забувати про поточні задачі і соцмережі",
    "мотиваційна думка чи цитата, не обов'язково про журналістику",
    "жарт пов'язаний з журналістикою або українською політикою",
    "коротка цікава дрібничка чи факт (можна про Миколаїв, можна геть не пов'язане з роботою)",
    "питання до редакції (риторичне чи реальне) замість порад",
]

# Набір форматів — визначає ФОРМУ повідомлення, не тільки зміст.
# Це головний інструмент проти одноманітності: без цього модель за
# замовчуванням завжди тяжіє до одного й того самого шаблону
# "привітання + погода + мотивація + побажання настрою + жарт",
# навіть якщо зміст рандомізований.
MESSAGE_FORMATS = [
    {
        "name": "максимально коротко",
        "instruction": "Напиши МАКСИМУМ 2-3 речення загалом, одним суцільним повідомленням без абзаців. Без жарту. Без фінального побажання настрою. Просто привітання і погода, і якщо влізе — одна додаткова думка.",
    },
    {
        "name": "одне довге речення",
        "instruction": "Спробуй вмістити привітання, погоду і одну думку в 1-2 довгих речення (можна з тире, дужками, переліком через кому) — без розбивки на абзаци.",
    },
    {
        "name": "звичний розгорнутий формат",
        "instruction": "Можеш написати в звичному розгорнутому форматі з кількома короткими абзацами (4-6 речень загалом), якщо це природно лягає сьогодні.",
    },
    {
        "name": "без жарту і без мотивації",
        "instruction": "НЕ додавай жарт і НЕ додавай мотиваційне побажання чи побажання настрою — тільки привітання, погода і, за бажанням, одна з додаткових тем нижче. Закінчи природно, без формального фіналу.",
    },
    {
        "name": "жарт або цікавинка на першому місці",
        "instruction": "Почни НЕ з привітання, а одразу з жарту, питання або цікавинки, і лише потім — привітання і погода в довільному місці тексту.",
    },
    {
        "name": "тільки практика, без емоцій",
        "instruction": "Пиши сухіше і прямолінійніше, без емоційних побажань настрою чи натхнення — як швидка нотатка-нагадування, а не урочисте привітання.",
    },
]


# Пул варіантів коментаря про ПОРОЖНІЙ календар міськради.
# Python обирає один випадково і передає в промпт як конкретну
# інструкцію — щоб модель не генерувала щоразу схожі фрази
# з тих самих "запасів" своєї уяви.
EMPTY_CALENDAR_VARIANTS = [
    "скажи щось на кшталт: пресслужба міськради знову взяла вихідний раніше за всіх",
    "зауваж, що календар міськради сьогодні чистіший за совість депутата перед виборами",
    "поскаржся (іронічно) що пресслужба, схоже, анонсує події заднім числом — або не анонсує взагалі",
    "відмітьте що в календарі міськради знову тиша — мабуть всі засідання відбуваються таємно",
    "зауваж що пресслужба міськради настільки засекречена, що навіть від самої міськради",
    "скажи що сьогодні в календарі міськради — порожнеча, як і обіцянки на сесії",
    "поіронізуй що пресслужба, мабуть, теж чекає на офіційний анонс перед тим як щось анонсувати",
    "зауваж що відсутність подій у календарі — це теж інформація, просто не та, яку хотілося б мати",
]


async def generate_morning_message(weather, events_text=None):
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

    # Випадковий формат — визначає ФОРМУ (довжину, наявність жарту/мотивації,
    # структуру), а не тільки тему.
    chosen_format = random.choice(MESSAGE_FORMATS)

    # 0, 1 або 2 додаткові теми — явно дозволяємо 0, інакше модель завжди
    # тягне додати хоч щось.
    focus_count = random.choice([0, 0, 1, 1, 2])
    chosen_focus = random.sample(MESSAGE_FOCUS_OPTIONS, k=focus_count)
    if chosen_focus:
        focus_text = "Можеш (не обов'язково) торкнутися однієї з цих тем:\n" + "\n".join(f"- {f}" for f in chosen_focus)
    else:
        focus_text = "Сьогодні не додавай жодних додаткових тем — тримайся тільки привітання і погоди."

    is_weekend = datetime.now().weekday() >= 5  # 5=субота, 6=неділя

    if events_text:
        events_block = (
            f"\nКалендар анонсів міської ради на сьогодні НЕ порожній — там є подія(ї). "
            f"Сам список подій буде доданий до повідомлення окремо, ПІСЛЯ твого тексту, "
            f"тому НЕ переказуй і не перелічуй самі події, не вигадуй деталей про них.\n"
            f"ОБОВ'ЯЗКОВО (не пропускай цього разу) одним реченням з легким здивованим "
            f"сарказмом відмітьте що пресслужба міськради таки щось внесла в календар — "
            f"наче це рідкість. Формулювання щоразу інше, не повторюй однакові жарти."
        )
    elif is_weekend or random.random() < 0.3:
        # У вихідні — завжди мовчимо. У будні — мовчимо з імовірністю 30%
        events_block = ""
    else:
        chosen_calendar_quip = random.choice(EMPTY_CALENDAR_VARIANTS)
        events_block = (
            f"\nКалендар анонсів міської ради на сьогодні ПОРОЖНІЙ.\n"
            f"Обов'язково додай одне коротке іронічне речення про це — саме в такому дусі: "
            f"{chosen_calendar_quip}. "
            f"Перефразуй своїми словами, не цитуй дослівно. Легка журналістська іронія, без грубощів."
        )

    prompt = f"""Ти — Лис Микита, бот редакції новинного сайту МикВісті (Миколаїв, Україна).

Напиши ранкове повідомлення для редакції. Сьогодні {datetime.now().strftime('%A, %d.%m.%Y')}.

Прогноз погоди в Миколаєві на сьогодні: {weather_text}
{events_block}

ФОРМАТ СЬОГОДНІ: {chosen_format['instruction']}

Обов'язково має прозвучати десь у тексті звертання "дорога редакція" (можна не на початку) і інформація про погоду на сьогодні.

{focus_text}

Вимоги:
- Українська мова
- Сьогоднішній формат — {chosen_format['name']}. Дотримайся його навіть якщо це означає дуже короткий чи незвичний текст.
- Кожного разу інші слова і інша структура — НЕ копіюй фрази і конструкції типу "нехай дедлайни не лякають", "наша аудиторія чекає", "продуктивного робочого дня" — ці фрази вже занадто часто повторювались, шукай інші формулювання або пропускай цю думку взагалі
- Емодзі необов'язкові; якщо використовуєш — 0-2, не більше
- НЕ занадто офіційно, але і не зобов'язково святково-урочисто щоразу

Напиши тільки текст повідомлення, без пояснень."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
    return clean_ai_text(message.content[0].text)

def _escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_full_message(ai_text, events):
    """Склеює AI-згенерований текст з готовим HTML-списком подій (якщо є)."""
    events_html = format_events_html(events)
    safe_ai_text = _escape_html(ai_text.strip())
    if events_html:
        return f"{safe_ai_text}\n\n{events_html}"
    return safe_ai_text


async def send_morning_message(bot, chat_id):
    try:
        weather = get_mykolaiv_weather()
        events = get_today_events()
        events_text = format_events_for_prompt(events)
        ai_text = await generate_morning_message(weather, events_text)
        full_text = _build_full_message(ai_text, events)
        await bot.send_message(
            chat_id=chat_id,
            text=full_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print("Помилка ранкового повідомлення: " + str(e))

    # Привітання з днем народження — окреме повідомлення після ранкового
    try:
        from handlers.ai_messages import get_todays_birthdays, generate_birthday_greeting
        birthdays = get_todays_birthdays()
        for name, info in birthdays:
            greeting = await generate_birthday_greeting(name, info)
            await bot.send_message(chat_id=chat_id, text=greeting, parse_mode="HTML")
    except Exception as e:
        print("Помилка привітання з ДН: " + str(e))

async def morning_handler(update, context):
    try:
        weather = get_mykolaiv_weather()
        events = get_today_events()
        events_text = format_events_for_prompt(events)
        ai_text = await generate_morning_message(weather, events_text)
        full_text = _build_full_message(ai_text, events)
        await update.message.reply_text(
            full_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.message.reply_text("Помилка: " + str(e))
