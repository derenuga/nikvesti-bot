# Природномовні запити до Лиса Микити (Agentic Query Layer)

**Статус:** Реалізовано і в проді. GA4 + Google Search Console контур (Meta і пошук по сайту — ще ні, див. "Беклог" нижче).

**Модуль:** `handlers/query_router.py`

---

## Як це працює

```
Приватне повідомлення боту АБО reply на повідомлення бота в чаті редакції
        │
        ▼
ALLOWED_USER_IDS? ──ні──▶ ігнорується (приват: "⛔ Доступ заборонено", група: мовчки)
        │так
        ▼
handle_natural_language_query() в query_router.py
        │
        ▼
Надсилається плейсхолдер: "🦊 Розбираюсь з вашим питанням, шефе..."
        │
        ▼
Цикл tool use (максимум MAX_TOOL_ITERATIONS = 4 ітерації):
  Claude API виклик (system = QUERY_ROUTER_SYSTEM_PROMPT + tools=TOOLS)
        │
        ▼
  stop_reason == "tool_use"? ──так──▶ виконати Python-функцію(ї) з TOOL_FUNCTIONS,
        │                              повернути tool_result, повторити цикл
        │ні (фінальна відповідь)
        ▼
Плейсхолдер редагується (edit_text) на фінальний текст
        │
        ▼
Якщо викликався render_chart — окремим повідомленням надсилається PNG (reply_photo)
```

Якщо ліміт ітерацій вичерпано — плейсхолдер редагується на повідомлення про надто складне питання.
Якщо стався виняток — плейсхолдер редагується на `❌ Помилка: {e}`.

---

## Тригери

1. **Приватне повідомлення боту** (`filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND` в `bot.py`) — основний канал.
2. **Reply на повідомлення бота в чаті редакції** (`group_query_trigger` в `bot.py`) — той самий Intent Router, але тільки якщо `msg.reply_to_message.from_user.id == bot.id` і автор reply входить в `ALLOWED_USER_IDS`.
3. **Згадка `@<юзернейм бота>` будь-де в тексті повідомлення** в чаті редакції (без reply) — та сама `group_query_trigger`. Юзернейм береться динамічно з `context.bot.username` (не хардкодиться в коді), сама згадка вирізається з тексту перед тим як питання йде в `handle_natural_language_query(update, context, question=...)`.

Тригери 2 і 3 оброблені в одній функції `group_query_trigger` (а не в двох окремих `MessageHandler`), щоб reply на чуже повідомлення зі згадкою бота теж спрацював — окремі handlers з різними фільтрами конкурували б за пріоритет обробки апдейту в python-telegram-bot, і другий випадок міг би загубитись.

Усі три шляхи ведуть в `handle_natural_language_query`.

### Виняток: reply на автоматичну розсилку

`group_query_trigger` мовчки ігнорує (не запускає Intent Router) reply на повідомлення, яке бот надіслав як автоматичну розсилку — ранкове привітання (`send_morning_message`), привітання з днем народження, нагадування про непрочитану пошту чи про мовчання каналу @nikvesti. Такі пости не містять аналітичних даних, і reply на них зазвичай коментар до контенту ("в календарі знову пусто"), а не питання — раніше Claude намагався відповісти через GA4/Search Console tools, хоча питання до статистики не мало жодного стосунку. Це обмеження стосується тільки reply — згадка `@бот` в новому повідомленні (навіть якщо воно теж є reply на розсилку) все одно спрацює, бо трактується як окреме питання.

Реалізовано через `handlers/broadcast_tracker.py`: кожна з цих функцій відмічає `message_id` надісланого повідомлення (`mark_broadcast`) в in-memory реєстр (без персистентності — забувається після рестарту бота), `group_query_trigger` звіряється з ним (`is_broadcast`) перед викликом `handle_natural_language_query`.

Звіти зі статистикою (`send_daily_report`, тижневі IG/FB звіти, місячний EN-звіт) в цей реєстр свідомо НЕ потрапляють — reply на них цілком може бути реальним аналітичним питанням ("чому впали перегляди?") і має йти в Intent Router як і раніше.

## Whitelist

`ALLOWED_USER_IDS` (env, comma-separated ID) — глобальний middleware `check_allowed` в `bot.py` (`TypeHandler`, `group=-1`) блокує приватні повідомлення від людей поза списком з відповіддю "⛔ Доступ заборонено." і `ApplicationHandlerStop`. Якщо змінна не задана — дозволено всім (дефолт, для зворотної сумісності).

Поточний склад (`ALLOWED_USER_IDS=56631818,56424866,386403807`): Олег, Катя, Ліза.

---

## Доступні tools (`handlers/query_router.py`)

| Tool | Призначення |
|---|---|
| `get_ga4_metric(metric, period, start_date?, end_date?)` | Одна метрика за період: `activeUsers`, `screenPageViews`, `screenPageViewsPerSession`, `newUsers`, `returningUsers` (рахується як `activeUsers - newUsers`) |
| `get_ga4_top_articles(period, limit?, start_date?, end_date?)` | Топ статей за переглядами з атрибуцією автора |
| `get_ga4_geo_breakdown(period, dimension?, limit?, ...)` | Географія аудиторії по Україні: `region` або `city` |
| `get_ga4_hourly_breakdown(period, ...)` | Активність по годинах доби (0-23) — найкращий час публікації |
| `get_ga4_custom_report(dimensions, metrics, period, limit?, page_path_contains?, filter_dimension?, filter_value_contains?, ...)` | Запасний інструмент для довільних GA4 dimensions/metrics (пристрої, браузери, джерела трафіку, день тижня тощо). `page_path_contains` звужує до конкретної статті (ID з URL). `filter_dimension`+`filter_value_contains` — довільний CONTAINS-фільтр по будь-якій dimension (наприклад `sessionSource` містить `derstandard.de`), щоб деталізувати трафік з конкретного джерела навіть при малій кількості сесій |
| `get_ga4_article_stats(url)` | Перегляди конкретної статті за всю історію, по мовних версіях (ua/ru/en) |
| `get_search_console_report(period, dimensions?, page_url?, search_type?, limit?, ...)` | Google Search Console: пошукові запити, сторінки, країни, кліки/покази/CTR/позиція. `search_type`: `web` (дефолт), `discover` (Google Discover), `googleNews`, `image`, `video`. `page_url` звужує до конкретної статті — **матчиться по ID статті через CONTAINS, не по точному URL** (equals виявився крихким — трейлінг слеші, протокол тощо ламали фільтр і давали хибний "нуль трафіку") |
| `render_chart(labels, values, chart_type?, title?, ylabel?)` | Малює bar/line графік (matplotlib) з даних, які Claude вже отримав з інших tools, зберігає PNG; надсилається окремим повідомленням після тексту |

### Авторизація Search Console

Той самий service account, що й для GA4 (`GA4_CREDENTIALS`), має додатковий scope `https://www.googleapis.com/auth/webmasters.readonly` (вже використовувався в `english_report.py` для EN-звіту). Окремих Railway-змінних не потрібно. Сайт: `sc-domain:nikvesti.com`.

---

## Формат відповіді

- **Без Markdown-таблиць** — простий текст у кілька рядків (редакція просила, дані достатньо прості).
- **Динамічна підпис джерела даних** в кінці відповіді — формується з реально викликаних tools за час діалогу:
  - тільки GA4-tools → `📊 Джерело даних: Google Analytics 4 (nikvesti.com)`
  - тільки `get_search_console_report` → `📊 Джерело даних: Google Search Console (nikvesti.com)`
  - обидва → `📊 Джерело даних: Google Analytics 4 + Google Search Console (nikvesti.com)`
  - якщо жоден tool не викликався (загальна відповідь без даних) — підпис не додається
- **Графіки:** якщо Claude вирішив що дані — розподіл/часовий ряд (по годинах, регіонах, днях), він викликає `render_chart`. У тексті відповіді заборонено згадувати шлях до файлу чи вставляти markdown `![]()` — є і інструкція в системному промпті, і regex-запобіжник `re.sub(r'!\[[^\]]*\]\([^)]*\)', '', final_text)` в коді на випадок якщо Claude все одно це вставить.

---

## Реалізовано (хронологія ключових ітерацій)

1. Базовий контур: `get_ga4_metric`, `get_ga4_top_articles`, `get_ga4_article_stats`, tool use цикл, whitelist через `ALLOWED_USER_IDS`.
2. Прибрано Markdown-таблиці з відповідей.
3. Додано плейсхолдер "Розбираюсь з вашим питанням, шефе..." з подальшим `edit_text`.
4. Додано `get_ga4_geo_breakdown` (регіони/міста України).
5. Додано тригер через reply на бота в груповому чаті редакції (не тільки приват).
6. Додано `get_ga4_hourly_breakdown` (найкращий час публікації).
7. Додано `get_ga4_custom_report` як запасний tool для довільних GA4 dimensions/metrics, щоб Claude не впирався у відсутність спеціалізованого інструменту.
8. Додано детермінований footer з джерелом даних.
9. Додано `render_chart` (matplotlib) + виправлено витік markdown-синтаксису зображення в текст відповіді.
10. Додано `page_path_contains` до `get_ga4_custom_report` — можливість розбити трафік конкретної статті по джерелах (`sessionDefaultChannelGroup`/`sessionSource`) без знання дати публікації.
11. Додано `get_search_console_report`, включно з `search_type='discover'` — тепер Лис бачить трафік з Google Discover, пошукові запити Google, Google News.
12. Footer джерела даних зроблено динамічним (GA4 / Search Console / обидва — залежно від реально викликаних tools).
13. Виправлено фільтр `page_url` в Search Console: був `equals` (крихке точне співпадіння URL, давало хибні "0 трафіку"), став `contains` по ID статті — як і в GA4-tools.
14. Прибрано фільтр виключення трафіку з Сінгапуру (`_no_singapore_filter` та аналог в `english_report.py`) — після впровадження капчі на сайті бот-трафіку немає.
15. `/english` і місячний автозвіт тепер коректно звітують за поточний місяць, коли запускаються в його останній день (раніше завжди брали попередній місяць).
16. Додано `filter_dimension`/`filter_value_contains` до `get_ga4_custom_report` — довільний CONTAINS-фільтр по будь-якій GA4 dimension (не тільки `pagePath`). Дозволяє деталізувати трафік з конкретного джерела/реферера (наприклад `sessionSource` містить `derstandard.de`) навіть якщо воно дало лише кілька сесій і не потрапляє в загальний топ; разом з dimension `pageReferrer` показує точні сторінки-донори.

---

## Відомі проблеми / спостереження з продакшну

- **Group-chat reply leak (не пріоритет, відкладено):** зафіксований випадок, коли бот відповів у чаті редакції людині поза `ALLOWED_USER_IDS`, яка зробила reply на повідомлення лиса. Очікувана поведінка — `group_query_trigger` в `bot.py` має мовчки ігнорувати таких користувачів (`if ALLOWED_USER_IDS and update.effective_user.id not in ALLOWED_USER_IDS: return`). Причина не підтверджена, не досліджено — за явним проханням залишити на потім.

---

## Беклог (не реалізовано)

### Meta (Facebook + Instagram)

Основа є в `facebook.py`, `instagram.py`, але tool-обгорток для NLQ ще немає:
- `get_facebook_post_stats(period | post_url)`
- `get_instagram_stats(period)`
- `get_facebook_top_posts(period, limit?)`

### Пошук по сайту (MySQL)

Найскладніший і найцінніший інструмент — питання типу "що останнє ми писали про Сєнкевича?". Залежить від доступу до БД сайту (KEY4-міграція або REST wrapper — див. `PROJECT_CONTEXT.md`).
- `search_site_articles(query, limit?, date_from?, date_to?)`
- `get_article_by_url(url)`

### Комбіновані запити

Питання що вимагають кількох джерел одночасно (сайт + соцмережі) — не окремий tool, а природний наслідок того, що Claude сам ланцюжком викликає кілька tools в межах одного діалогу tool use, коли з'являться Meta- і site-search tools.

---

## Залежності від іншого беклогу (PROJECT_CONTEXT.md)

- П. 10-11 (БД сайту, графіки) — пряма залежність для інструменту пошуку по сайту
- П. 16 (міграція на KEY4) — впливає на спосіб доступу до БД (прямий доступ замість REST wrapper)
