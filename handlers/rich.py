"""Rich Messages (Bot API 10.1/10.2) прямим HTTP-викликом.

**Чому не через бібліотеку.** PTB 22.8 зупинився на Bot API 10.0 —
`sendRichMessage` у ньому немає, і це не здогад за датами:
`hasattr(telegram.Bot, "send_rich_message")` → `False`. Тому виклик іде сирим
HTTP тим самим токеном. Уся домовленість зібрана в ОДНІЙ функції, щоб замінити
її на нативний виклик одним рядком, коли PTB додасть підтримку.

**Головне зі схеми, заради чого це варте заходу.** `InputRichMessage` приймає
готовий HTML-рядок (поле `html`) — будувати дерево з двадцяти класів
`InputRichBlock` не треба, і верстка лишається читабельною в коді. Набір тегів
ширший за звичайний `sendMessage`:

    <h1>…<h6>  <p>  <hr/>  <ul>/<ol>/<li>  <blockquote>…<cite>
    <details><summary>  <figure><img><figcaption>  <table>  <footer>

Ліміти: 32768 символів (замість 4096), 500 блоків, 50 медіа, 20 колонок
таблиці. Картинки — ТІЛЬКИ HTTP(S)-URL і тільки окремими медіа-блоками
(`<img>` усередині `<p>` не працює).

**Фолбек обов'язковий.** Rich Messages молодші за два місяці, і клієнт, який
їх не малює, або відмова API не мають з'їдати повідомлення — тому кожен виклик
несе з собою звичайний HTML-варіант. Якщо rich не пройшов, летить він, а
причина пишеться в лог один раз (не щоразу — інакше лог заллє).
"""

import json

import aiohttp

# Причина першої відмови. Тримаємо, щоб не сипати в лог те саме щодня і щоб
# /promise_remind міг показати людині, ЧОМУ повідомлення прийшло звичайним.
_last_error = None


def last_error():
    return _last_error


async def send_rich(bot, chat_id, html, *, fallback, reply_markup=None,
                    disable_notification=False, timeout=30):
    """Надіслати rich-повідомлення. Повертає True, якщо пішло саме rich.

    `fallback` — звичайний HTML на випадок відмови. Обов'язковий аргумент
    свідомо: повідомлення, яке може тихо зникнути, гірше за негарне.
    """
    global _last_error

    payload = {
        "chat_id": str(chat_id),
        "rich_message": json.dumps({"html": html}, ensure_ascii=False),
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup.to_json()
    if disable_notification:
        payload["disable_notification"] = "true"

    url = f"https://api.telegram.org/bot{bot.token}/sendRichMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    url, data=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = await resp.json(content_type=None)
        if data.get("ok"):
            return True
        reason = f"{data.get('error_code')}: {data.get('description')}"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"

    if reason != _last_error:
        _last_error = reason
        print(f"rich: sendRichMessage не пройшов — {reason}; іду звичайним")

    await bot.send_message(chat_id, fallback[:4000], parse_mode="HTML",
                           disable_web_page_preview=True,
                           reply_markup=reply_markup,
                           disable_notification=disable_notification)
    return False
