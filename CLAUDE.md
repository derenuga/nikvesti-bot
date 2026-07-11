# Лис Микита — бот редакції МикВісті

Внутрішній Telegram-бот редакції новинного медіа МикВісті (nikvesti.com, Миколаїв).
Моніторить документи влади, тендери, конкурентів, правоохоронців, збирає аналітику, надсилає ранкові зведення.

**Репозиторій:** github.com/derenuga/nikvesti-bot  
**Хостинг:** Railway (проект `remarkable-stillness`)  
**Стек:** Python, python-telegram-bot 21.9, APScheduler, BeautifulSoup, Google Analytics Data API, Google Search Console API, Anthropic Claude (tool use), matplotlib  
**Бот:** @nikvesti_desk_bot  
**Чат редакції:** -1001857099475  
**Канал "🦊 Микита винюхав":** -1004322862າ (тендери + документи влади + правоохоронці)

---

## Деплой

Railway автодеплоїть з гілки `main`. **Щоб зміна пішла в прод — вона має бути в `main`.**
Робота у веб-сесії ведеться на feature-гілці з PR, але це проміжний крок, а не фініш:
**після того як зміни готові й запушені — одразу мерджити PR (гілку) у `main`**, щоб
задеплоїлось. Не лишати готову роботу висіти в PR і не чекати окремого прохання
«закинь на main» — це стандартне дозволене завершення задачі. Виняток — якщо явно
попросили лишити на review.

---

## Правила роботи з кодом

1. **Повні файли завжди.** Ніколи не давати патчі, інструкції "додай після рядка X" або часткові фрагменти. Тільки повний готовий файл для заміни.
2. **Не зупинятись на питаннях.** Якщо є неясність — уточнити одне питання і продовжувати, не чекати підтвердження кожного кроку.
3. **Тестувати парсери на реальному HTML.** Не писати власні зразки HTML, не тестувати на вигаданих даних.
4. **Baseline моніторингу з тестовою відправкою.** При першому запуску модуля моніторингу зберегти всі ID крім N останніх як бачені, N останніх одразу відправити — щоб перевірити що відправка і формат працюють. N вказувати явно в коментарі з поясненням.
5. **Emoji напряму в коді**, не як Unicode escape sequences (`🦊`, не `\U0001F98A`).
6. **Перевіряти факти** — особливо структуру HTML і API перед написанням парсерів.

---

## Структура коду

```
bot.py                    — реєстрація handlers, команди, TypeHandler middleware
handlers/
  scheduler.py            — APScheduler, розклад всіх автозавдань (Europe/Kiev)
  storage.py              — JSON-стан на Railway Volume (/data/prozorro_state.json)
  db.py                   — тонкий read-only адаптер до MySQL-БД сайту (SELECT only, SSL), /dbtest, /dbquery
  bot_db.py               — власна Postgres-БД бота (Railway): дзеркало архіву, tsvector FTS, sync_state, daily_stats (історія трафіку GA4), social_stats (тижневі зрізи соцмереж)
  archive_mirror.py       — синк дзеркала архіву з БД сайту: /archive_backfill (разово), інкремент щогодини :50
  archive_search.py       — повнотекстовий пошук по дзеркалу (17 років, заголовки+текст), NLQ-tool search_archive_fulltext
  dossier.py              — /dossier <тема>: історія питання з архіву, таймлайн по роках з лінками
  entity_layer.py         — сутнісний шар нори (entities/article_entities, крок C ENTITY_LAYER_PLAN): бэкфіл через Batch API з бота — /entity_estimate (безкоштовна оцінка), /entity_backfill (платно, Haiku 4.5 −50%, полінг у фоні, стан у sync_state переживає редеплой), /entity_status, /entity_resume; злиття — entity_pipeline.write_results (корінь репо), промпт витягу — entity_extract_prompt.md
  builder_monitor.py      — монітор оновлення білдера головної (options + nodes з БД), /builder, /builder_test
  ai_messages.py          — AI-шар (Anthropic Claude), FOX_SYSTEM_PROMPT, TEAM словник
  morning.py              — ранкове повідомлення: погода + події міськради + AI текст
  events.py               — парсинг календаря подій mkrada.gov.ua/calendar/
  prozorro.py             — моніторинг тендерів Prozorro ≥1 млн грн
  documents.py            — моніторинг документів органів влади (міськрада, ОВА, облрада)
  law_enforcement.py      — моніторинг новин правоохоронних органів (прокуратура тощо)
  competitors.py          — моніторинг новин конкурентів (news.pn тощо)
  google_analytics.py     — GA4 щоденна аналітика, /analytics, /report
  analytics_store.py      — пам'ять щоденної аналітики GA4 у Postgres (daily_stats): тихий щоденний захват (capture_yesterday), /analytics_backfill, серія для NLQ-tool get_traffic_history
  weekly_digest.py        — «Тижневик Лиса»: понеділковий дайджест тижня сайту з порівнянням тиждень-до-тижня (заміна щоденного 09:00-звіту), /weekly
  traffic_spikes.py       — детектор сплесків трафіку (GA4 Realtime, самонавчальний профіль), /traffic
  stat.py                 — /stat <url>: статистика матеріалу (Facebook + Telegram + GA4)
  telegram_stats.py       — перегляди постів каналу @nikvesti (індекс + парсинг t.me/s)
  english_report.py       — місячний звіт EN-версії (GA4 + Search Console + AI коментар)
  instagram.py            — тижнева статистика Instagram
  facebook.py             — тижнева статистика Facebook
  social_store.py         — пам'ять тижневих зрізів соцмереж у Postgres (social_stats): знімок піггібеком на недільні звіти FB/IG, /social_capture, історія для NLQ-tool get_social_history
  gmail.py                — перевірка Gmail
  sheets.py               — запис у Google Sheets (Prozorro)
  reactions.py            — обробка реакцій на повідомлення про тендери
  energy_outage.py        — графік відключень електроенергії (off.energy.mk.ua): /outage, /outage_probe, /outage_export (CSV вулиць із чергами)
  notifier.py             — сповіщення про збої scheduled-задач у приват адміну (слухач EVENT_JOB_ERROR + прямий notify_error у модулях з власним try/except)
  ai_usage.py             — звіт про вартість AI-шару: /aicost за місяць + авто-звіт Олегу 1-го числа; оцінка за прайсом моделей із токенів у storage (record_ai_usage)
  helpers.py              — спільні утиліти (парсинг місяців, get_author_from_url)
  query_router.py         — Intent Router: природномовні запити до Лиса (GA4 + Search Console, tool use)
  news_archive.py         — архів новин сайту (БД): пошук "що ми писали про X", ліди, генерація беку, кнопка "Написати бек"
  news_stats.py           — підрахунки по БД сайту (nodes): NLQ-tool count_news — скільки матеріалів за період / власних (own) / по рубриках / по авторах (owner_id→users) / мовою (ua/ru/en, EN-колонку визначає інтроспекцією); metric=views сумує перегляди (лічильник сайту, за весь час) — «хто з журналістів набрав більше переглядів». Джерело істини, свіже, рахує й англ. версію (якої немає в норі)
  tags_wikidata.py        — прив'язка тегів сайту до Q-сутностей Wikidata для schema.org about: /tags_export (топ-N тегів у CSV), /tags_wiki (топ-N → Wikidata wbsearchentities → Claude добирає QID+@type → CSV на ревʼю + готовий ALTER/UPDATE .sql, бо БД сайту read-only)
  knowledge_graph.py      — розвідка Google Knowledge Graph Search API: /kg <KG ID або запит> дістає канонічну картку сутності (name, @type, опис, Вікіпедія, сайт, resultScore), перевірка як Google бачить бренд/тег; простий API key GOOGLE_KG_API_KEY
```

---

## Команди бота

| Команда | Що робить |
|---|---|
| /start | Привітання зі списком команд |
| /status | Перевірка що бот живий |
| /analytics | GA4 статистика за вчора + топ-5 статей |
| /analytics_backfill \[N\] | Залити N днів історії трафіку з GA4 у daily_stats (дефолт 90; можна роками — GA4 тримає стандартні звіти весь час, ллється чанками по року); далі наповнюється сам тихим захватом о 09:00 |
| /weekly | Тижневик Лиса вручну в чат редакції |
| /social_capture | Зняти зріз FB+IG зараз у social_stats (засів першої точки; далі знімок сам щонеділі) |
| /social_backfill_fb \[міс\] | Спроба залити історію FB (перегляди/взаємодії по тижнях) за N місяців (дефолт 24) через since/until; вставляє лише відсутні тижні. IG історію Meta не віддає |
| /report | GA4 звіт за вчора в чат редакції (щоденний авто-пост прибрано, лишилась ручна команда) |
| /checkmail | Перевірити Gmail |
| /instagram | Тижнева статистика Instagram |
| /igreport | Тижневий Instagram звіт з AI в чат |
| /facebook | Тижнева статистика Facebook |
| /fbreport | Тижневий Facebook звіт з AI в чат |
| /morning | Ранкове повідомлення вручну |
| /documents | Перевірити нові документи органів влади |
| /documents_test | Тестовий пост з першого документа кожного джерела |
| /documents_rebaseline | Зберегти поточні сторінки як бачені БЕЗ відправки (гасіння спаму) |
| /competitors | Перевірити новини конкурентів |
| /law | Перевірити новини правоохоронних органів |
| /stat \<url\> | Статистика матеріалу (Facebook + Telegram + GA4) за URL nikvesti.com |
| /english | Місячний звіт EN-версії сайту (GA4 + Search Console) |
| /prozorro | Перевірити тендери Prozorro |
| /prozorro_test_jump \[N\] | Діагностика зсуву офсету за N днів (дефолт 14) |
| /prozorro_confirm_jump \[N\] | Підтвердити скидання офсету |
| /prozorro_reset_tender \<id\> | Розблокувати тендер для повторної реакції |
| /traffic | Хто зараз на сайті (GA4 Realtime) + типовий трафік для цієї години |
| /reset | Забути контекст розмови з Лисом (пам'ять діалогу NLQ) |
| /aicost \[YYYY-MM\] | Витрати AI-шару за місяць (дефолт — поточний) |
| /stat_backfill \[N\] | Разовий бэкфіл індексу постів каналу для /stat (N місяців, дефолт уся історія) |
| /outage | Графік відключень електроенергії (off.energy.mk.ua) |
| /outage_probe \<path\> \[arg\] | Службова розвідка API з Railway (тимчасово) |
| /outage_export \[idfilial\] | CSV вулиць із чергами, дефолт 15 (Миколаїв), ~7–15 хв |
| /outage_geocode \[idfilial\] | Геокодує вулиці по мікрорайонах (дефолт 15, Миколаїв) |
| /myip | Вихідний IP Railway + чи він у whitelist БД сайту (діагностика конекту) |
| /dbtest | Перевірка з'єднання з MySQL-БД сайту (версія, база, к-сть таблиць, час відповіді) |
| /dbquery \<SELECT…\> | Службова розвідка схеми БД з Railway (read-only, тимчасово) |
| /builder | Діагностика монітора білдера: коли оновлювався, скільки власних новин відтоді |
| /builder_test | Надіслати зразок нагадування про білдер у чат редакції (в обхід порогів) |
| /dossier \<тема\> | Історія питання з 17-річного архіву: таймлайн по роках з лінками на матеріали |
| /tags_export \[N\] | Топ-N тегів сайту за ужитком у CSV (дефолт 300), з розмерджуванням redirect_tag_id |
| /tags_wiki \[N\] | Зіставити топ-N тегів (дефолт 100) із Wikidata: QID + @type + посилання на Вікіпедію (uk→ru→en) + Google KG ID (P2671, безкоштовно з Wikidata). CSV на ревʼю + готовий ALTER/UPDATE .sql (4 колонки) для PHPMyAdmin (БД сайту read-only). Кешується за tag_id — повторний прогін на більший N чіпає лише нові теги, файли віддає на весь N |
| /tags_wiki_reset | Скинути кеш зіставлення тегів (наступний /tags_wiki перерахує все з нуля) |
| /kg \<KG ID або запит\> | Картка сутності з Google Knowledge Graph Search API: name, @type, опис, Вікіпедія, сайт, score. По ID (/g/…, /m/…) або за назвою. Потребує GOOGLE_KG_API_KEY |
| /archive_sample | Залити кілька старих+нових статей і показати збережений текст (перевірка чистки/розділення мов) |
| /archive_backfill \[N\] | Заливка архіву в дзеркало; N — порція за запуск (фазування), без N — усе; resumable |
| /archive_stop | М'яко зупинити поточний бекфіл (після поточної пачки); resumable — повторний /archive_backfill продовжить з місця зупинки |
| /archive_status | Стан дзеркала архіву: скільки статей, діапазон дат, курсори синку |
| /archive_report | Здоровкове зведення нори для нагляду за бекфілом: розподіл по роках, мови, теги, рубрики, регіони, середня довжина тексту по роках (детектор проблем чистки) |
| /nora_sql \<SELECT…\> | Read-only запит до нори (Postgres бота) для ad-hoc нагляду; той самий підхід, що /dbquery для БД сайту |
| /entity_estimate \[з\] \[по\] | Оцінка бэкфіла сутнісного шару за діапазон дат: к-сть статей + вартість Batch API (read-only, безкоштовно; дефолт 2022-01-01..2027-01-01) |
| /entity_backfill \<з\> \<по\> | ПЛАТНО: витяг сутностей із статей діапазону через Batch API (Haiku 4.5, −50%, ~1$/1000 статей), полінг у фоні, по готовності — злиття в entities/article_entities і звіт у чат |
| /entity_status | Сутнісний шар: скільки сутностей по kind, зв'язків, топ за згадками + стан активного прогону батчів |
| /entity_resume | Переприв'язати полінг батчів після редеплою (стан у sync_state) або повторити збій ingest (ідемпотентно) |
| /entity_recover | Коли стан прогону загублено: знайти батчі на боці Anthropic (живуть 29 днів), пересобрати стан і довести до ingest; якщо батчів немає — гроші не витрачались |

---

## Природномовні запити (Intent Router)

Приватне повідомлення боту (від `ALLOWED_USER_IDS`), або reply на повідомлення бота в чаті редакції — йде в `handle_natural_language_query` (`handlers/query_router.py`), Claude сам обирає tool через tool use (GA4, історія трафіку з daily_stats через `get_traffic_history` — тренди й порівняння період-до-періоду дешево з локальної БД, Search Console, архів тендерів Prozorro, Facebook/Instagram поточні + історія соцмереж через `get_social_history` з social_stats, архів новин сайту, підрахунки матеріалів через `count_news` з `handlers/news_stats.py` — скільки вийшло за період / власних / по рубриках / по авторах / мовою ua/ru/en) і відповідає живою мовою. Питання "що ми писали про X?" шукає по заголовках новин у БД сайту (`handlers/news_archive.py`) і відповідає списком "дата — заголовок (лінк)" з кнопками відбору: номерні чекбокси ✅ + кнопка "🦊 Бек з усіх цих новин"/"Бек з новин 1+3"; бек ("Нагадаємо, раніше…") складається з лідів вибраних новин, лінки — анкорами (≤3 слова) всередині речень. Стан пошуку/вибору — в storage (`news_search`), переживає редеплой. Лис пам'ятає останні 6 обмінів протягом 30 хв (follow-up'и "а за минулий місяць?" працюють), `/reset` скидає. Деталі — [`docs/NATURAL_LANGUAGE_QUERIES_MODULE.md`](docs/NATURAL_LANGUAGE_QUERIES_MODULE.md).

---

## Розклад (Europe/Kiev)

| Час | Що запускається |
|---|---|
| 08:15 щодня | Ранкове повідомлення в чат редакції |
| 09:00 щодня | Тихий захват вчорашньої аналітики в daily_stats (users/sessions/pageviews + топ сторінок), БЕЗ поста — щоденний звіт у чат прибрано на користь тижневика |
| Понеділок 09:30 | Тижневик Лиса: тиждень сайту до тижня (users/sessions/pageviews) + топ-5 матеріалів + винюхані тендери + соцмережі тиждень-до-тижня (з social_stats) + AI-підводка |
| 10:00 щодня | Перевірка правоохоронних органів |
| 13:00 щодня | Перевірка Gmail + правоохоронні органи |
| 16:00 щодня | Перевірка правоохоронних органів |
| 16:50 щодня | Перевірка Gmail |
| 15:00 щонеділі | Facebook тижневий звіт з AI (заодно знімок у social_stats) |
| 18:00 щонеділі | Instagram тижневий звіт з AI (заодно знімок у social_stats) |
| Щогодини :00 | Тендери Prozorro |
| Кожні 3 год :15 (01,04,07,10,13,16,19,22) | Новини конкурентів (00:00–07:00 — у нічний буфер, дайджест о 07:15) |
| Щогодини :30 | Документи органів влади |
| Кожні 30 хв (10–18, пн–пт) | Перевірка мовчання каналу @nikvesti |
| Щогодини :05 і :35 | Детектор сплесків трафіку (GA4 Realtime, алерт при ≥2× від типового) |
| :10 і :40 (9–21) | Монітор білдера головної: >2 год без оновлення + ≥2 нові власні новини → нагадування |
| Щогодини :50 | Інкрементальний sync дзеркала архіву (тихо пропускається без BOT_DATABASE_URL) |
| Останній день місяця 19:00 | Місячний EN-звіт |
| 1-го числа 10:00 | Звіт про вартість AI за попередній місяць (в приват Олегу) |

---

## Змінні середовища (Railway)

```
BOT_TOKEN
CHAT_ID = -1001857099475
PROZORRO_CHAT_ID = -1004322862192
DOCUMENTS_CHAT_ID = -1004322862192
GA4_PROPERTY_ID = 321381722
GA4_CREDENTIALS              # JSON сервісного акаунту Google
GMAIL_USER
GMAIL_PASSWORD               # App Password
INSTAGRAM_TOKEN
INSTAGRAM_USER_ID = 17841400860799899
FACEBOOK_PAGE_TOKEN
FACEBOOK_PAGE_ID = 301719373180657
ANTHROPIC_API_KEY
OPENWEATHER_API_KEY
GOOGLE_KG_API_KEY            # опційно, простий API key (не сервісний акаунт) для /kg — Google Knowledge Graph Search API
SPREADSHEET_ID = 1bsKzGRsQ7O1aa4TpxmzqEfIjRM1A0dso7zueYvCXB1I
ALLOWED_USER_IDS = 56631818,56424866,386403807   # Олег, Катя, Ліза — whitelist приватних повідомлень і NLQ
STATE_PATH                   # опційно, дефолт /data/prozorro_state.json
MISE_PYTHON_GITHUB_ATTESTATIONS = false

# БД бота (Postgres на Railway) — handlers/bot_db.py, дзеркало архіву + /dossier
BOT_DATABASE_URL             # референс на DATABASE_URL Postgres-плагіна (фолбек: DATABASE_URL)

# БД сайту (MySQL, read-only, SELECT only на nikvesti.*, SSL обов'язковий) — handlers/db.py
DB_HOST                      # 185.149.41.55
DB_PORT = 3306
DB_NAME = nikvesti
DB_USER                      # nikvesti_bot
DB_PASSWORD
DB_SSL_CA                    # опційно, шлях до CA сервера; без нього — SSL без перевірки cert
DB_CONNECT_TIMEOUT           # опційно, сек (дефолт 10)
DB_READ_TIMEOUT             # опційно, сек (дефолт 30)
```

**Доступ до БД сайту (KEY4):** whitelist за вихідними IP Railway; ліміти сервера —
5 одночасних з'єднань, 10000 запитів/год, 1000 з'єднань/год, 0 UPDATE. Модель у `db.py` —
з'єднання на запит (thread-safe), тільки читання. Бот стартує і без DB_* (модуль опційний).

**Railway Volume:** mount path `/data`.  
**Стан:** єдиний файл `/data/prozorro_state.json` для всіх модулів моніторингу.

---

## Storage — структура JSON

```json
{
  "document_ids": {
    "mayor_orders": ["51982", ...],
    "ova_orders": ["18165", ...],
    "oblrada_decisions": ["md5hash", ...],
    "prokuratura": ["424328", ...]
  },
  "competitor_ids": {
    "news_pn": ["345266", ...]
  },
  "tg_posts": {
    "320362": {"message_id": 82005}
  },
  "traffic_spikes": {
    "profile": {"2_14": [312, 298, ...]},
    "last_alert_at": "2026-07-02T14:35:00+03:00"
  },
  "builder_monitor": {
    "last_alert_at": 1783093933
  },
  "news_search": {
    "56631818:56631818": {"items": [{"n": 1, "id": 320651, "date": "03.07.2026", "title": "...", "url": "..."}], "selected": [1, 3], "at": "2026-07-03T23:51:00"}
  },
  "ai_usage": {
    "2026-07": {"claude-sonnet-5": {"requests": 42, "input": 168000, "output": 9000, "cache_read": 140000, "cache_creation": 12000}}
  },
  "prozorro": { ... }
}
```

`None` (ключ відсутній) ≠ `[]` (ініціалізовано але порожньо). `None` = перший запуск, потрібен baseline.

**Обмеження росту (storage.py):** `competitor_ids` капляться на 1000/джерело (стрічка новин ~10-50 останніх, справжній bloat). `document_ids` — БЕЗ капу: сторінки проєктів рішень містять повну історію (тисячі записів), кап відрізав би її й старі документи щоразу виглядали б "новими" (був баг: спам за 2021). Ріст документів обмежений темпом публікацій. Тендери старші 120 днів прюняться разом з `message_to_tender` (у `bulk_save`). `tg_posts` — кап 20000. `traffic_spikes` — 8 замірів на слот. Запобіжник: `>DOCS_SANITY_MAX` (20) "нових" документів за прогон → ре-baseline без спаму + алерт (`/documents_rebaseline` — ручна чистка).

---

## Детальна документація модулів

- [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) — загальний контекст, беклог
- [`docs/DOCUMENTS_MODULE.md`](docs/DOCUMENTS_MODULE.md) — моніторинг документів влади
- [`docs/LAW_ENFORCEMENT_MODULE.md`](docs/LAW_ENFORCEMENT_MODULE.md) — моніторинг правоохоронців
- [`docs/COMPETITORS_MODULE.md`](docs/COMPETITORS_MODULE.md) — моніторинг конкурентів
- Тендери Prozorro — окремого дока немає, детальний опис у док-стрінгу [`handlers/prozorro.py`](handlers/prozorro.py)
- [`docs/BUILDER_MONITOR_MODULE.md`](docs/BUILDER_MONITOR_MODULE.md) — монітор білдера головної + схема БД сайту (options/nodes/users/logs), розвідана 03.07
- [`docs/STAT_MODULE.md`](docs/STAT_MODULE.md) — статистика матеріалів (/stat)
- [`docs/ENGLISH_REPORT_MODULE.md`](docs/ENGLISH_REPORT_MODULE.md) — EN-звіт
- [`docs/FOX_LORE.md`](docs/FOX_LORE.md) — identity frame персонажа Лиса Микити
- [`docs/NATURAL_LANGUAGE_QUERIES_MODULE.md`](docs/NATURAL_LANGUAGE_QUERIES_MODULE.md) — природномовні запити (Agentic Query Layer): GA4 + Search Console, tool use, whitelist
- [`docs/ENERGY_OUTAGE_MODULE.md`](docs/ENERGY_OUTAGE_MODULE.md) — графік відключень: API off.energy.mk.ua, адресний каскад, CSV-експорт, наступні кроки
- [`docs/LONG_TERM_VISION.md`](docs/LONG_TERM_VISION.md) — довгострокові ідеї: підписки, перехід на власний сервер+MySQL, AI-доступ до 17-річного архіву новин
- [`docs/ARCHIVE_INTELLIGENCE.md`](docs/ARCHIVE_INTELLIGENCE.md) — стратегія інституційної пам'яті: рівні, вартість, дорожня карта, продукти
- [`docs/ARCHIVE_MIRROR_MODULE.md`](docs/ARCHIVE_MIRROR_MODULE.md) — дзеркало архіву в Postgres бота, FTS-пошук, /dossier (хвиля A)
- [`docs/ENTITY_LAYER_PLAN.md`](docs/ENTITY_LAYER_PLAN.md) — план Досьє v2 (індекс-файл, /dossier_deep) і сутнісного шару (хвиля D); бриф для нової сесії, «кодь» не давався
- [`docs/REVIEW_2026_07.md`](docs/REVIEW_2026_07.md) — ревізія липня 2026: апрувнутий план оптимізацій і розвитку (хвилі впровадження)

---

## Команда редакції (TEAM в ai_messages.py)

| Хто | TG | Роль |
|---|---|---|
| Олег Деренюга | @derenuga | Керівник |
| Катерина Середа | @sereda_ka | Головред |
| Юлія Бойченко | — | Лід-журналістка |
| Аліса Мелікадамян | — | Лід-журналістка, судова репортерка |
| Світлана Іванченко | — | Редакторка стрічки |
| Альона Коханчук | — | Лід-журналістка, екологія |
| Іра Федорович | @diiessa | Перекладачка (EN-версія) |
