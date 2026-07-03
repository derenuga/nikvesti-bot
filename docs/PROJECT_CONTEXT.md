---

**ПРОЕКТ: Лис Микита — бот редакції МикВісті**

**Репозиторій:** github.com/derenuga/nikvesti-bot

**Хостинг:** Railway (проект remarkable-stillness)

**Стек:** Python, python-telegram-bot 21.9, Railway, GitHub

**Бот:** @nikvesti_desk_bot (токен в Railway як BOT_TOKEN)

**Чат редакції ID:** -1001857099475

**Канал "🦊 Микита винюхав" ID:** -1004322862192 (тендери Прозорро + документи органів влади + правоохоронці)

---

**Окремі файли з деталями модулів (читати при роботі з відповідним модулем):**
- `PROZORRO_MODULE.md` — моніторинг тендерів Прозорро: API, офсети, продуктивність, Google Sheets, реакції
- `DOCUMENTS_MODULE.md` — моніторинг документів органів влади: mkrada.gov.ua, mk.gov.ua, mk-oblrada.gov.ua
- `LAW_ENFORCEMENT_MODULE.md` — моніторинг новин правоохоронних органів: прокуратура тощо
- `COMPETITORS_MODULE.md` — моніторинг новин конкурентів: news.pn, словник локальних ключових слів
- `STAT_MODULE.md` — команда /stat: статистика конкретного матеріалу (Facebook + GA4)
- `ENGLISH_REPORT_MODULE.md` — місячний звіт EN-версії сайту (GA4 + Search Console + AI)
- `FOX_LORE.md` — повна lore bible персонажа Лиса Микити (для розробників)

---

**Змінні в Railway:**
- BOT_TOKEN
- GA4_PROPERTY_ID = 321381722
- GA4_CREDENTIALS (JSON сервісного акаунту Google; той самий акаунт для Google Sheets і Search Console)
- GMAIL_USER, GMAIL_PASSWORD (App Password)
- INSTAGRAM_TOKEN, INSTAGRAM_USER_ID = 17841400860799899
- FACEBOOK_PAGE_TOKEN, FACEBOOK_PAGE_ID = 301719373180657
- ANTHROPIC_API_KEY
- CHAT_ID = -1001857099475
- OPENWEATHER_API_KEY (прогноз погоди, endpoint /data/2.5/forecast, координати Миколаєва 46.9750/31.9946)
- MISE_PYTHON_GITHUB_ATTESTATIONS = false
- PROZORRO_CHAT_ID = -1004322862192
- DOCUMENTS_CHAT_ID = -1004322862192
- SPREADSHEET_ID = 1bsKzGRsQ7O1aa4TpxmzqEfIjRM1A0dso7zueYvCXB1I
- ALLOWED_USER_IDS = 56631818
- STATE_PATH (опційно, дефолт /data/prozorro_state.json)

---

**Railway Volume:** mount path `/data`. Файл `/data/prozorro_state.json` — єдине сховище стану для модулів Прозорро, Документи, Правоохоронці і Конкуренти.

---

**Структура коду:**
```
bot.py — головний файл, реєстрація handlers і команд, TypeHandler middleware (ALLOWED_USER_IDS)
handlers/
  google_analytics.py — GA4 аналітика (/analytics, /report)
  gmail.py — перевірка пошти
  instagram.py — Instagram статистика
  facebook.py — Facebook статистика
  ai_messages.py — AI підводки (Anthropic); містить FOX_SYSTEM_PROMPT і mode-prompts, TEAM словник
  morning.py — ранкове повідомлення (погода + події міськради + AI текст)
  events.py — парсинг календаря подій mkrada.gov.ua (/calendar/)
  scheduler.py — розклад автозвітів
  prozorro.py — моніторинг тендерів Прозорро (деталі — PROZORRO_MODULE.md)
  documents.py — моніторинг документів органів влади (деталі — DOCUMENTS_MODULE.md)
  law_enforcement.py — моніторинг новин правоохоронців (деталі — LAW_ENFORCEMENT_MODULE.md)
  competitors.py — моніторинг новин конкурентів (деталі — COMPETITORS_MODULE.md)
  stat.py — /stat <url>: статистика матеріалу Facebook + GA4 (деталі — STAT_MODULE.md)
  english_report.py — місячний звіт EN-версії (деталі — ENGLISH_REPORT_MODULE.md)
  storage.py — шар абстракції над станом (JSON на Railway Volume)
  sheets.py — запис у Google Sheets
  reactions.py — обробка реакцій на повідомлення про тендери
  helpers.py — спільні утиліти (парсинг місяців, get_author_from_url)
```

---

**Команди бота:**
- /start — привітання зі списком команд
- /status — перевірка що бот живий
- /analytics — GA4 статистика за вчора з топ-5 статей
- /report — надіслати GA4 звіт в групу
- /checkmail — перевірити Gmail
- /instagram — тижнева статистика Instagram
- /igreport — тижневий Instagram звіт з AI в групу
- /facebook — тижнева статистика Facebook
- /fbreport — тижневий Facebook звіт з AI в групу
- /morning — згенерувати ранкове повідомлення вручну
- /documents — перевірити нові документи органів влади вручну
- /documents_test — тестовий пост з першого документа кожного джерела в канал
- /competitors — перевірити новини конкурентів вручну
- /law — перевірити новини правоохоронних органів вручну
- /stat <url> — статистика матеріалу nikvesti.com (Facebook перегляди/реакції + GA4 по мовах)
- /english — місячний звіт EN-версії сайту (GA4 + Search Console + AI коментар)
- /prozorro, /prozorro_test_jump, /prozorro_confirm_jump, /prozorro_reset_tender — див. PROZORRO_MODULE.md

---

**Розклад (Europe/Kiev):**
- 08:15 щодня — ранкове повідомлення в чат редакції
- 09:00 щодня — GA4 звіт в групу
- 10:00 щодня — перевірка правоохоронних органів
- 13:00 щодня — перевірка Gmail + перевірка правоохоронних органів
- 16:00 щодня — перевірка правоохоронних органів
- 16:50 щодня — перевірка Gmail
- 15:00 щонеділі — Facebook тижневий звіт з AI в групу
- 18:00 щонеділі — Instagram тижневий звіт з AI в групу
- щогодини (хвилина 0) — перевірка тендерів Прозорро
- щогодини (хвилина 15) — перевірка новин конкурентів
- щогодини (хвилина 30) — перевірка нових документів органів влади
- кожні 30 хв (10:00–18:00, пн–пт) — перевірка мовчання каналу @nikvesti
- останній день місяця о 19:00 — місячний EN-звіт

---

**Ранкове повідомлення (handlers/morning.py + handlers/events.py):**
- Погода: прогноз на день через OpenWeatherMap /data/2.5/forecast, координати Миколаєва
- Події: парсинг mkrada.gov.ua/calendar/ на поточний день
- AI-текст: Claude Sonnet, 6 форматів повідомлення рандомізовані, 0-2 теми за раз
- Порожній календар у будні: іронія з 40% шансом (60% — мовчить); у вихідні — завжди мовчить
- Список подій форматується в Python (HTML з посиланням на трансляцію якщо є)
- Зірочки з AI-тексту прибираються перед відправкою (Telegram не рендерить Markdown в HTML режимі)

---

**Команда Instagram МикВісті:**
- @mskvn1 (Ліза) — керує всім СММ
- @Imira_91 (Іміра) — розвиває Instagram
- Сергій Овчаришин (TG ID: 891685789) — монтажер рілзів

---

**AI-шар (handlers/ai_messages.py):**
Використовує Anthropic Claude Sonnet. Містить:
- `FOX_SYSTEM_PROMPT` — runtime ядро особистості (~700 символів)
- `TEAM` — словник команди редакції з TG-тегами і днями народження
- Mode-prompts для кожної задачі (пошта, соцмережі, конкуренти, ранок, EN-звіт тощо)
- `FOX_LORE.md` — повна lore bible для розробників (не йде в API)

---

**БЕКЛОГ:**

1. **AI рефакторинг** — винести FOX_SYSTEM_PROMPT з prompt caching, переписати всі функції в ai_messages.py з mode-prompts. Деталі — FOX_LORE.md.

2. **Конкуренти — підводка AI** — generate_competitors_intro в ai_messages.py, підводка перед списком новин конкурентів у стилі Лиса Микити.

3. **Конкуренти — novosti-n.org** — додати як друге джерело в competitors.py після відпрацювання news.pn.

4. **Словник локальних слів** — продовжувати навчати на реальних заголовках. Поточний словник: Миколає|Миколаїв|миколаїв|миколаївськ|Інгул|Намив|Парутин|Слобідськ|Галицинів|Снігурівк|Вознесенськ|Баштанськ|Первомайськ|Очак|Южноукраїнськ|Кім|Сєнкевич|Куцуруб|Новоодеськ|Мертвовод|Корабельн

5. **Документи — виправити /documents відповідь** — замість хардкоду "Перевірив документи!" динамічно перелічувати перевірені джерела.

6. **Документи — текст ОВА** — обрізати службовий текст "Зареєстровано в Одеському міжрегіональному управлінні..." з кінця назви розпорядження.

7. **Документи — проєкти рішень міської ради** (c=1) — є PDF на окремих сторінках документів, потрібен додатковий запит для посилання.

8. **Документи — проєкти рішень виконкому** (c=5) — аналогічно п.7.

9. **Правоохоронці — розширення джерел** — додати поліцію (ГУНП), СБУ, ДСНС після відпрацювання прокуратури.

10. **admin_creator FB** — отримати автора поста на Facebook. App Review в процесі.

11. **БД сайту** — підключення до БД МикВісті (Laminas/Zend, сервер KEY4). /stat потребує TG post ID з БД.

12. **Графіки** — після підключення БД зберігати метрики і малювати графіки.

13. **Звіти по співробітниках** — скільки новин опублікував кожен автор.

14. **Перевірка якості** — нагадування якщо журналіст не додав картинку або не локалізував новину.

15. **Перевірка орфографії** — бот помічає орфографічні помилки на сайті.

16. **Аватарка бота** — намалювати Лиса Микиту (Midjourney/DALL-E).

17. **Перенести бота на KEY4** — зараз на Railway. Вихідний IP Railway: `152.55.180.241` (перевірено 2026-07-03). Для whitelist зовнішніх БД/сервісів використовувати саме цей IP. Також у whitelist можуть бути `162.220.234.241`, `162.220.234.242` (інші вузли Railway). Увага: IP може змінитись після рестарту деплою.

18. **Facebook App Review** — завершити для permissions: pages_read_user_content, business_management, read_insights, pages_read_engagement.

19. **Facebook System User token** — автоматичне оновлення (зараз 60-денний токен, ручне оновлення). Залежить від App Review.

20. **Прозорро — розширення критеріїв і інші регіони** — деталі в PROZORRO_MODULE.md.

21. **Іра Федорович в TEAM** — додати `{"tg": "@diiessa", "role": "перекладачка, англійська версія сайту", "birthday": "24.05"}` в словник TEAM у ai_messages.py.

---