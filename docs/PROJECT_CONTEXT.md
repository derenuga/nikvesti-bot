---

**ПРОЕКТ: Лис Микита — бот редакції МикВісті**

**Репозиторій:** github.com/derenuga/nikvesti-bot

**Хостинг:** Railway (проект remarkable-stillness)

**Стек:** Python, python-telegram-bot 21.9, Railway, GitHub

**Бот:** @mykvisti_bot (токен в Railway як BOT_TOKEN)

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
- `TEAM_APP_MODULE.md` — Mini App «Команда»: creative tasks, грантові проєкти з тематиками і звітністю, KPI редакції (норми, дашборд, помісячна динаміка)
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

**БЕКЛОГ переїхав.** Єдиний список ідей і напрямків — [`BACKLOG.md`](BACKLOG.md).
Там усе згруповано за напрямками, з позначками стану і тим, що чим заблоковано.
Тримати список в одному місці: раніше він жив тут, вперемішку з довідкою, і
паралельно ще в шести файлах — читати це було неможливо.

Викреслені зроблені пункти лишились в історії git (до 02.08.2026).

---