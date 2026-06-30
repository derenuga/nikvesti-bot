
---

## МОДУЛЬ EN-ЗВІТ (місячний звіт англійської версії сайту) — детальний опис

**Належить до проекту:** Лис Микита — бот редакції МикВісті (див. PROJECT_CONTEXT.md).

### Що робить
В останній день кожного місяця о 19:00 формує і надсилає в чат редакції місячний звіт англійської версії nikvesti.com: трафік, топ-статті, аудиторія за країнами, реферери, пошукові запити з Search Console, порівняння з попереднім місяцем, AI-коментар.

### Файли коду
```
handlers/english_report.py — вся логіка: GA4, Search Console, форматування, AI коментар
handlers/ai_messages.py — generate_english_monthly_comment (AI підводка від Лиса Микити)
```

### Команди бота
- `/english` — ручний запуск звіту (відправляє в чат де викликано)

### Env-змінні
- GA4_PROPERTY_ID = 321381722
- GA4_CREDENTIALS (JSON сервісного акаунту; він же для Search Console)
- ANTHROPIC_API_KEY (для AI коментаря)

### Розклад
Автоматично — останній день місяця о 19:00 (Europe/Kiev). APScheduler: `day="last"`.

---

### Технічні деталі

**Клієнти:**
- GA4: `google.analytics.data_v1beta.BetaAnalyticsDataClient`
- Search Console: `googleapiclient.discovery.build("searchconsole", "v1")`
- Сервісний акаунт: `nikvesti-bot@speedy-actor-355616.iam.gserviceaccount.com`
- Search Console site: `sc-domain:nikvesti.com`

**GA4 фільтр EN-трафіку:**
```python
pagePath BEGINS_WITH "/en/"
```
Раніше тут також виключався Сінгапур (бот-трафік), але після впровадження капчі на сайті бот-трафіку немає — фільтр прибрано.

**Що збирається:**
- `get_en_summary` — загальна кількість: users, sessions, pageviews, повернення (activeUsers - newUsers), переглядів/сесію
- `get_en_top_pages` — топ-5 EN-статей за переглядами
- `get_ua_top_pages` — топ-5 UA-статей (для порівняння / контексту)
- `get_en_top_countries` — топ-5 країн за users
- `get_en_top_referrers` — топ-8 реферерів за sessions
- `get_sc_top_queries` — топ-8 пошукових запитів з Search Console (clicks, impressions, CTR, позиція)

**Порівняння з попереднім місяцем:**
- `get_prev_month_range` — розраховує діапазон попереднього місяця
- `format_diff` — форматує різницю зі знаком (+/-)

**AI коментар:**
- Викликає `generate_english_monthly_comment` з ai_messages.py
- Claude Sonnet отримує числові дані і генерує короткий аналіз у стилі Лиса Микити
- Коментар додається в кінець звіту

**Метрика повернення:**
`returningUsers` відсутня в GA4 Data API → `returning = activeUsers - newUsers`

---

### Формат звіту (приблизний)

```
🇬🇧 EN-версія МикВісті — Травень 2026

👥 Користувачі: 1 234 (+12% vs квітень)
🔄 Сесії: 1 456 (+8%)
📄 Перегляди: 2 100 (+15%)
🔁 Повторні: 234

📰 Топ-5 EN статей:
1. заголовок — 320 переглядів
...

🌍 Топ країн:
1. США — 456 users
...

🔍 Топ запити (Search Console):
1. "mykolaiv news" — 120 кліків, поз. 3.2
...

🔗 Топ реферери:
1. google.com — 540 сесій
...

🦊 [AI коментар від Лиса Микити]
```

---
