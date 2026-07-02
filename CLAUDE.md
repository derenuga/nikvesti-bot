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
  ai_messages.py          — AI-шар (Anthropic Claude), FOX_SYSTEM_PROMPT, TEAM словник
  morning.py              — ранкове повідомлення: погода + події міськради + AI текст
  events.py               — парсинг календаря подій mkrada.gov.ua/calendar/
  prozorro.py             — моніторинг тендерів Prozorro ≥1 млн грн
  documents.py            — моніторинг документів органів влади (міськрада, ОВА, облрада)
  law_enforcement.py      — моніторинг новин правоохоронних органів (прокуратура тощо)
  competitors.py          — моніторинг новин конкурентів (news.pn тощо)
  google_analytics.py     — GA4 щоденна аналітика, /analytics, /report
  traffic_spikes.py       — детектор сплесків трафіку (GA4 Realtime, самонавчальний профіль), /traffic
  stat.py                 — /stat <url>: статистика матеріалу (Facebook + Telegram + GA4)
  telegram_stats.py       — перегляди постів каналу @nikvesti (індекс + парсинг t.me/s)
  english_report.py       — місячний звіт EN-версії (GA4 + Search Console + AI коментар)
  instagram.py            — тижнева статистика Instagram
  facebook.py             — тижнева статистика Facebook
  gmail.py                — перевірка Gmail
  sheets.py               — запис у Google Sheets (Prozorro)
  reactions.py            — обробка реакцій на повідомлення про тендери
  helpers.py              — спільні утиліти (парсинг місяців, get_author_from_url)
  query_router.py         — Intent Router: природномовні запити до Лиса (GA4 + Search Console, tool use)
```

---

## Команди бота

| Команда | Що робить |
|---|---|
| /start | Привітання зі списком команд |
| /status | Перевірка що бот живий |
| /analytics | GA4 статистика за вчора + топ-5 статей |
| /report | GA4 звіт в чат редакції |
| /checkmail | Перевірити Gmail |
| /instagram | Тижнева статистика Instagram |
| /igreport | Тижневий Instagram звіт з AI в чат |
| /facebook | Тижнева статистика Facebook |
| /fbreport | Тижневий Facebook звіт з AI в чат |
| /morning | Ранкове повідомлення вручну |
| /documents | Перевірити нові документи органів влади |
| /documents_test | Тестовий пост з першого документа кожного джерела |
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
| /stat_backfill \[N\] | Разовий бэкфіл індексу постів каналу для /stat (N місяців, дефолт уся історія) |
| /outage | Графік відключень електроенергії (off.energy.mk.ua) |
| /outage_probe \<path\> \[arg\] | Службова розвідка API з Railway (тимчасово) |
| /outage_export \[idfilial\] | CSV вулиць із чергами, дефолт 15 (Миколаїв), ~7–15 хв |

---

## Природномовні запити (Intent Router)

Приватне повідомлення боту (від `ALLOWED_USER_IDS`), або reply на повідомлення бота в чаті редакції — йде в `handle_natural_language_query` (`handlers/query_router.py`), Claude сам обирає tool через tool use (GA4, Search Console, архів тендерів Prozorro, Facebook/Instagram) і відповідає живою мовою. Лис пам'ятає останні 6 обмінів протягом 30 хв (follow-up'и "а за минулий місяць?" працюють), `/reset` скидає. Деталі — [`docs/NATURAL_LANGUAGE_QUERIES_MODULE.md`](docs/NATURAL_LANGUAGE_QUERIES_MODULE.md).

---

## Розклад (Europe/Kiev)

| Час | Що запускається |
|---|---|
| 08:15 щодня | Ранкове повідомлення в чат редакції |
| 09:00 щодня | GA4 звіт за вчора |
| 10:00 щодня | Перевірка правоохоронних органів |
| 13:00 щодня | Перевірка Gmail + правоохоронні органи |
| 16:00 щодня | Перевірка правоохоронних органів |
| 16:50 щодня | Перевірка Gmail |
| 15:00 щонеділі | Facebook тижневий звіт з AI |
| 18:00 щонеділі | Instagram тижневий звіт з AI |
| Щогодини :00 | Тендери Prozorro |
| Щогодини :15 | Новини конкурентів (00:00–07:00 — у нічний буфер, дайджест о 07:15) |
| Щогодини :30 | Документи органів влади |
| Кожні 30 хв (10–18, пн–пт) | Перевірка мовчання каналу @nikvesti |
| Щогодини :05 і :35 | Детектор сплесків трафіку (GA4 Realtime, алерт при ≥2× від типового) |
| Останній день місяця 19:00 | Місячний EN-звіт |

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
SPREADSHEET_ID = 1bsKzGRsQ7O1aa4TpxmzqEfIjRM1A0dso7zueYvCXB1I
ALLOWED_USER_IDS = 56631818,56424866,386403807   # Олег, Катя, Ліза — whitelist приватних повідомлень і NLQ
STATE_PATH                   # опційно, дефолт /data/prozorro_state.json
MISE_PYTHON_GITHUB_ATTESTATIONS = false
```

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
  "prozorro": { ... }
}
```

`None` (ключ відсутній) ≠ `[]` (ініціалізовано але порожньо). `None` = перший запуск, потрібен baseline.

---

## Детальна документація модулів

- [`docs/PROJECT_CONTEXT.md`](docs/PROJECT_CONTEXT.md) — загальний контекст, беклог
- [`docs/DOCUMENTS_MODULE.md`](docs/DOCUMENTS_MODULE.md) — моніторинг документів влади
- [`docs/LAW_ENFORCEMENT_MODULE.md`](docs/LAW_ENFORCEMENT_MODULE.md) — моніторинг правоохоронців
- [`docs/COMPETITORS_MODULE.md`](docs/COMPETITORS_MODULE.md) — моніторинг конкурентів
- Тендери Prozorro — окремого дока немає, детальний опис у док-стрінгу [`handlers/prozorro.py`](handlers/prozorro.py)
- [`docs/STAT_MODULE.md`](docs/STAT_MODULE.md) — статистика матеріалів (/stat)
- [`docs/ENGLISH_REPORT_MODULE.md`](docs/ENGLISH_REPORT_MODULE.md) — EN-звіт
- [`docs/FOX_LORE.md`](docs/FOX_LORE.md) — identity frame персонажа Лиса Микити
- [`docs/NATURAL_LANGUAGE_QUERIES_MODULE.md`](docs/NATURAL_LANGUAGE_QUERIES_MODULE.md) — природномовні запити (Agentic Query Layer): GA4 + Search Console, tool use, whitelist
- [`docs/ENERGY_OUTAGE_MODULE.md`](docs/ENERGY_OUTAGE_MODULE.md) — графік відключень: API off.energy.mk.ua, адресний каскад, CSV-експорт, наступні кроки
- [`docs/LONG_TERM_VISION.md`](docs/LONG_TERM_VISION.md) — довгострокові ідеї: підписки, перехід на власний сервер+MySQL, AI-доступ до 17-річного архіву новин
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
