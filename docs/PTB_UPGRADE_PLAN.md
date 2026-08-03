# Оновлення python-telegram-bot і Rich Messages

**Мета Олега (03.08.2026):** «щоб окрема сесія потім взяла і всі повідомлення
бота переформатувала красиво» — Telegram улітку 2026 додав структуроване
оформлення постів (заголовки, таблиці, картинки всередині, згортні блоки), і
бот має ним користуватись.

Цей файл — розвідка, зроблена ДО того, як щось міняти, щоб сесія оновлення не
починала з нуля і не пішла хибним шляхом.

---

## Головне, що треба знати до початку

**Оновлення PTB САМЕ ПО СОБІ Rich Messages не дасть.** Це не здогад, а звірка
дат:

| що | коли | Bot API |
|---|---|---|
| наш `python-telegram-bot==21.9` | грудень 2024 | **8.1** |
| найсвіжіший PTB **22.8** | 12.06.2026 | **9.6 / 10.0** |
| Bot API **10.1** — Rich Messages | 11.06.2026 | ← на день раніше за 22.8 |
| Bot API **10.2** — `InputRichMessageMedia` | 14.07.2026 | ← після 22.8 |

Тобто станом на 03.08.2026 **жодна випущена версія PTB не вміє
`sendRichMessage`**. Перевірено в коді, а не за документацією:

```python
>>> import telegram; telegram.__version__
'21.9'
>>> hasattr(telegram.Bot, "send_rich_message")
False
>>> telegram.constants.BOT_API_VERSION
'8.1'
```

Отже сесія оновлення має розділити дві задачі, які легко переплутати:

1. **Оновити PTB 21.9 → 22.8.** Дає Bot API 8.1 → 10.0, тобто півтора року
   змін, і прибирає борг. Rich Messages НЕ дає.
2. **Rich Messages — окремо**, поки PTB їх не додасть: прямий HTTP-виклик
   `sendRichMessage` в обхід бібліотеки, локально в тих місцях, де це справді
   потрібно.

Робити (2) без (1) можна. Робити (1) заради (2) — марно.

---

## Наша поверхня використання PTB (заміряно, не вгадано)

Це головна причина вважати оновлення посильним: бот величезний, а від
бібліотеки бере мало.

**Імпорти — усе:**

```
telegram: InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
telegram.constants: ParseMode
telegram.ext: ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler,
              CommandHandler, MessageHandler, MessageReactionHandler,
              TypeHandler, filters
```

**Методи API:**

| виклик | разів |
|---|---|
| `.reply_text()` | 458 |
| `.edit_text()` | 215 |
| `.edit_message_text()` | 77 |
| `bot.send_message()` | 49 |
| `.answer()` (callback) | 55 |
| `.reply_document()` | 16 |
| `.delete()` | 16 |
| `bot.get_file()` | 5 |
| `.reply_photo()` | 1 |

**Фільтри:** `COMMAND, CONTACT, REPLY, TEXT, Regex, CaptionRegex,
ChatType.PRIVATE, ChatType.CHANNEL, Document.ALL, Document.FileExtension`.

**Чого ми НЕ використовуємо** (а саме там і лежать усі задокументовані
ламальні зміни 22.x): `JobQueue` (у нас свій APScheduler), `ConversationHandler`,
`Bot.get_business_account_gifts`, `UniqueGift`, `BusinessConnection`,
`ChatFullInfo.can_send_gift`, `constants.StarTransactions`, чеклісти,
`ReplyParameters`.

**Python:** 3.11 на Railway. PTB 22.6 викинув 3.9 — нас не зачіпає.

---

## Ламальні зміни 22.x, звірені з нашим кодом

| версія | що прибрано | нас чіпає |
|---|---|---|
| 22.3 | `BusinessConnection.can_reply`, `ChatFullInfo.can_send_gift`, `constants.StarTransactions`, `StarTransactionsLimit.NANOSTAR_*` | ні |
| 22.5 | `ReplyParameters.checklist_task_id` переїхав у кінець | ні |
| 22.6 | прибрано Python 3.9 | ні (у нас 3.11) |
| 22.7 | `UniqueGiftInfo.last_resale_star_count`, `Bot.get_business_account_gifts.exclude_limited`, `UniqueGift.gift_id` став позиційним | ні |
| 22.8 | без прибирань | ні |

**Але цього мало для спокою.** Перелік вище — те, що PTB назвав ламальним у
своєму changelog. Між 21.9 і 22.0 є ще межа мажорної версії, і її треба
прочитати окремо: сесія оновлення мусить пройти changelog 22.0–22.2, якого в
цій розвідці немає.

---

## План для сесії оновлення

1. **Прочитати changelog 22.0, 22.1, 22.2** — єдина біла пляма цієї розвідки.
2. **Підняти версію в `requirements.txt`, запустити всі тести**:
   `test_promises.py`, `test_promise_eval.py`, `test_entity_increment.py`,
   `test_entity_backfill_filter.py`. Вони не про Telegram, але імпортують
   `handlers/*`, тобто впіймають зламані імпорти.
3. **`python -c "import bot"`** — найдешевша перевірка: bot.py імпортує
   майже всі 40 модулів, і будь-яка зникла назва вилізе одразу.
4. **Прогнати вручну по одній команді з кожного класу**: `/status` (текст),
   `/promises` (HTML + довге), `/promise_export` (документ), `/team` (WebApp-
   кнопка), будь-яка з інлайн-кнопками (`/entity_find`), реакції на пост
   каналу (MessageReactionHandler), пересланий контакт (filters.CONTACT),
   .txt-пакет у приват (filters.Document.FileExtension).
5. **Окремо перевірити Viber-дзеркало** (`viber_mirror.py`): воно смикає
   `bot.get_file()` і працює з `media_group_id` — найтонше місце.

**Деплой:** Railway автодеплоїть із `main`, тож оновлення requirements.txt
одразу піде в прод. Відкату «в один клік» немає — тому спершу переконатись,
що `import bot` і тести проходять локально.

---

## Rich Messages: що це і чому вони НЕ в цій задачі

**Що дає Bot API 10.1/10.2** (`sendRichMessage`, `InputRichMessage` з полем
`blocks`): заголовки секцій, таблиці, списки, цитати й пул-цитати, колажі,
слайдшоу, роздільники, згортні блоки `details`, формули, карти, футери. Плюс
`sendRichMessageDraft` — стрімінг відповіді частинами.

**Які повідомлення бота переверстувати — питання відкрите і вирішується
окремо, з Олегом.** Спокуса розписати це заздалегідь велика, але список
«ось це в таблицю, а це не чіпати» без погляду на реальний вигляд на
телефоні — здогад. Тендери, документи влади, сплески трафіку, бюджетні
розбори, картка обіцянки, тижневик — усе це кандидати, і жоден не
відсіяний. Тому редизайн повідомлень — **третя сесія**, зі своїм промтом,
і починається вона з розмови, а не з коду.

Ця розвідка потрібна їй лише одним фактом: **оновлення PTB саме по собі
Rich Messages не відкриє**, бо бібліотека зупинилась на Bot API 10.0.
Коли до цього дійде — виклик доведеться робити прямим HTTP-запитом до
`sendRichMessage`, обгорнувши його однією функцією, щоб потім замінити на
нативний метод одним рядком.

---

## Готовий промт для сесії оновлення

> **Задача: оновити python-telegram-bot 21.9 → 22.8. Тільки це.**
>
> Прочитай `docs/PTB_UPGRADE_PLAN.md` — там розвідка, зроблена заздалегідь:
> заміряна поверхня використання PTB і звірка ламальних змін 22.x із нашим
> кодом. Rich Messages у цій задачі НЕ робимо: PTB 22.8 зупинився на Bot
> API 10.0, а Rich Messages це 10.1/10.2, тобто оновлення їх усе одно не
> відкриє. Редизайн повідомлень — окрема сесія пізніше.
>
> Порядок:
> 1. Прочитати changelog PTB 22.0, 22.1, 22.2 — єдина біла пляма розвідки
>    (22.3–22.8 звірені, нас не чіпають).
> 2. `python-telegram-bot==21.9` → `22.8` у `requirements.txt`.
> 3. `python -c "import bot"` — bot.py тягне майже всі 40 модулів.
> 4. Тести: `test_promises.py`, `test_promise_eval.py`,
>    `test_entity_increment.py`, `test_entity_backfill_filter.py`.
> 5. Ручний прогін по одній команді з кожного класу — список у §«План».
> 6. Окремо `viber_mirror.py`: `get_file` + альбоми, найтонше місце.
>
> Якщо щось у 22.0–22.2 таки зачіпає наш код — не обходь мовчки, покажи
> Олегу, що саме зламалось і як лагодиш.
>
> Правила проєкту — `CLAUDE.md`. Деплой із `main`, Railway підхоплює сам.
