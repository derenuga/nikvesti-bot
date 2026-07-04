# Дзеркало архіву + повнотекстовий пошук + /dossier

Хвиля A плану [`ARCHIVE_INTELLIGENCE.md`](ARCHIVE_INTELLIGENCE.md) — інституційна
пам'ять редакції поверх 17-річного архіву. Апрув Олега: «Кодь», 03.07.2026.

## Модулі

| Файл | Що робить |
|---|---|
| `handlers/bot_db.py` | Адаптер власної Postgres-БД бота: схема (articles + generated tsvector, sync_state), upsert, курсори, `ping()` |
| `handlers/archive_mirror.py` | Синхронізація дзеркала: `/archive_backfill` (разова заливка, resumable), інкрементальний sync щогодини о :50, `/archive_status` |
| `handlers/archive_search.py` | Повнотекстовий пошук: tsquery з префіксними лексемами, стратифікація по роках, excerpts; NLQ-tool `search_archive_fulltext` |
| `handlers/dossier.py` | `/dossier <тема>`: Haiku генерує ua/ru варіанти пошуку → пошук по роках → Sonnet складає таймлайн з лінками |

## Архітектура

```
MySQL сайту (read-only, ліміти KEY4)
        │  бекфіл (разово, порціями) + інкремент (щогодини :50)
        ▼
Postgres бота (Railway) — articles: id, дати, title_ua/ru, slug, text_plain,
                          fts (generated tsvector, GIN)
        │
        ├── search_archive_fulltext (NLQ-tool Лиса; кнопки беку працюють)
        └── /dossier — історія питання по роках з лінками
```

**Чому дзеркало:** production-БД read-only (індекс не створиш), 10 000 запитів/год,
5 з'єднань; кожен LIKE по longtext — ризик для сайту. HTML → чистий текст
конвертується один раз при синку.

## Налаштування (Railway)

1. Додати **PostgreSQL** у проект `remarkable-stillness`.
2. У сервісі бота задати `BOT_DATABASE_URL` = референс на `DATABASE_URL`
   Postgres-плагіна (бот також бачить і просто `DATABASE_URL` — фолбек).
3. Задеплоїти, перевірити `/archive_status` (покаже «дзеркало порожнє»).
4. Разово: `/archive_backfill` — заливає весь архів (~1–2 год, порціями по
   150 з паузою 1 с — далеко в межах лімітів KEY4). Resumable: обрив →
   повторний виклик продовжує з курсора `backfill_last_id`.
5. Після завершення дзеркало оновлюється само (щогодини о :50).

Без `BOT_DATABASE_URL` бот працює як раніше: `/dossier` і full-text пошук чемно
відмовляють, NLQ-роутер відкочується на `search_news_archive` (LIKE по заголовках).

## Схема Postgres

```sql
articles (
  id BIGINT PK,                 -- = nodes.id сайту
  published, updated BIGINT,    -- unix, published буває в майбутньому (відкладені)
  status SMALLINT,              -- 1 = опубліковано; пошук фільтрує status=1 AND published<=now
  own_material SMALLINT, owner_id BIGINT,
  title_ua TEXT, title_ru TEXT, -- title_ru = nodes.title (без суфікса — російська!)
  slug TEXT,                    -- slug_ua || slug; URL = nikvesti.com/news/{slug}
  text_plain TEXT,              -- чистий текст, кап 60 000 симв. (захист tsvector 1МБ)
  fts tsvector GENERATED ALWAYS AS (to_tsvector('simple', titles || text)) STORED
)
sync_state (key PK, value)      -- backfill_last_id, backfill_done_at, mirror_cursor
```

Індекси: GIN(fts), btree(published DESC), опційно pg_trgm по заголовках
(якщо інстанс дозволяє CREATE EXTENSION; без нього все працює).

## Пошук: як влаштована морфологія без стемера

Українського стемера в Postgres немає → конфіг `'simple'` + **префіксні лексеми**:
кожне слово запиту обрізається до грубої основи (`≥8 симв. → -3`, `≥6 → -2`,
`≥5 → -1`) і шукається як `основ:*`. «Стадіону» → `стаді:*` → знаходить
стадіон/стадіону/стадіоном. Ранжування `ts_rank` піднімає точні збіги.

**Відоме обмеження (перевірено тестом):** ua-запит НЕ знаходить російські тексти
(«стадіону» ≠ «стадиона», бо і/и). Закрито на рівні вище: `/dossier` генерує
ua+ru варіанти через Haiku, NLQ-роутеру наказано повторювати пошук російським
написанням. Семантично це закриє хвиля B (embeddings).

`spread_years=true` — режим «історія питання»: `ROW_NUMBER() OVER (PARTITION BY
рік ORDER BY ts_rank)` ≤ per_year, від давніх до свіжих — історія не тоне під
свіжими новинами.

## /dossier

`/dossier стадіон Центральний` →
1. Haiku (`FOX_MODEL_FAST`): 3–5 пошукових варіантів (ua/ru написання, синоніми).
2. Кожен варіант → пошук зі stratify по роках; злиття: ≤3/рік, ≤22 всього.
3. `get_excerpts` — по 700 симв. тексту на статтю з дзеркала.
4. Sonnet (`FOX_MODEL_SMART`): таймлайн по періодах, фігуранти, відкриті питання.
   Правило чесності беків: тільки факти з наших текстів, кожен факт — з
   HTML-лінком анкором 1–3 слова.

Результат лягає в пам'ять діалогу NLQ → follow-up'и («а що по 2016?») працюють.
Вартість одного досьє ≈ $0.05–0.15 (Haiku варіанти + Sonnet синтез ~20К вх. токенів).

## Команди

| Команда | Дія |
|---|---|
| `/archive_backfill` | Разова заливка архіву в дзеркало (фонова, прогрес редагуванням повідомлення, resumable) |
| `/archive_status` | Стан дзеркала: скільки статей, діапазон дат, курсори, чи йде бекфіл |
| `/dossier <тема>` | Історія питання з архіву: таймлайн по роках з лінками |

## Розклад

`:50` щогодини — інкрементальний sync: `WHERE GREATEST(updated, published) >= cursor-120`,
ORDER BY зміною, LIMIT 500 (аномальний сплеск добереться наступними запусками).
Курсор — `mirror_cursor` у sync_state. Тихо пропускається без env або до бекфілу.
Знімає з публікації теж: інкремент бере рядки без фільтра status, пошук фільтрує сам.

## Тестування (03.07.2026)

- Інтеграційно на реальному PostgreSQL 16: схема з generated-колонкою, ідемпотентний
  upsert, ua/ru пошук, пошук по тілу (слова немає в заголовку), spread_years,
  фільтри років, excerpts, злиття варіантів досьє (таймлайн 2014→2026), sync_state.
- `html_to_text` — на структурі content_ua, задокументованій з реальної БД
  (imgbox-блок → абзаци → script/figure).
- ⚠️ НЕ тестовано на живих БД (з dev-середовища немає доступу: whitelist по IP
  Railway). **Після деплою**: `/archive_backfill`, дочекатись, `/archive_status`,
  спот-чек — `/dossier` по знайомій темі і звірити лінки/дати очима.

## Стійкість і ліміти

- Обидві БД недоступні / не налаштовані → всі функції чемно відмовляють, бот живе.
- Збій інкрементального sync → `notify_error` Олегу (як у решти моніторів).
- Бекфіл і інкремент не працюють одночасно (`_backfill_running`).
- Розмір: сотні тисяч статей × ≤60К симв. ≈ одиниці ГБ; Railway Postgres тягне.
