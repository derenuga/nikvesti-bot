"""
Дзеркало Telegram → Viber: автопостинг постів каналу @nikvesti у Viber-канал
(звільнити журналістів від ручного крос-постингу).

Через Viber Channels Post API (developers.viber.com/docs/tools/channels-post-api):
POST https://chatapi.viber.com/pa/post — постить прямо в канал текст/картинку/
відео/лінк. auth_token у тілі; `from` — Viber-id супер-адміна каналу (з
get_account_info). Ліміти: текст 7000, картинка JPEG ≤1 МБ, відео MP4/H264
≤50 МБ ≤180 с.

Обсяг (рішення Олега): дзеркалимо ВСЕ, крім репостів (форвардів з інших
каналів) і службових повідомлень.

ФАЗА 1 (цей файл): текст + лінк. Картинки/відео — наступним етапом (потрібна
конвертація в JPEG <1 МБ і публічний хостинг; Telegram file-URL несе токен
бота — не хочемо світити його Viber'у).

Налаштування:
1. Бути super admin Viber-каналу → інфо каналу → Developer Tools → auth_token.
2. Railway: VIBER_AUTH_TOKEN (+ VIBER_WEBHOOK_URL — будь-який HTTPS-endpoint,
   що віддає 200; Viber вимагає заданий webhook перед постингом. Підійде
   проста сторінка на nikvesti.com. /viber_setup його реєструє).
3. /viber_setup — реєструє webhook і показує акаунт/супер-адміна.
4. /viber_test — тестовий пост у канал.
Далі кожен пост каналу @nikvesti автоматично дублюється у Viber.

Тихо вимкнено, поки VIBER_AUTH_TOKEN не задано.
"""

import os

import requests

POST_URL = "https://chatapi.viber.com/pa/post"
ACCOUNT_INFO_URL = "https://chatapi.viber.com/pa/get_account_info"
SET_WEBHOOK_URL = "https://chatapi.viber.com/pa/set_webhook"

TEXT_LIMIT = 7000

_sender_cache = None


def _token():
    return os.environ.get("VIBER_AUTH_TOKEN")


def is_enabled():
    return bool(_token())


def get_account_info():
    """Дані Viber-акаунта каналу (статус, id, супер-адміни)."""
    resp = requests.post(ACCOUNT_INFO_URL, json={"auth_token": _token()}, timeout=15).json()
    if resp.get("status") != 0:
        raise RuntimeError(resp.get("status_message") or resp)
    return resp


def _sender_id():
    """Viber-id супер-адміна каналу (потрібен як `from` у pa/post — саме він
    визначає підпис автора поста). Кешуємо.

    VIBER_SENDER_ID (env) жорстко закріплює автора. Інакше — перший
    super admin (не будь-який адмін-журналіст), далі admin, далі id акаунта."""
    global _sender_cache
    if _sender_cache:
        return _sender_cache
    pinned = os.environ.get("VIBER_SENDER_ID")
    if pinned:
        _sender_cache = pinned.strip()
        return _sender_cache
    info = get_account_info()
    members = info.get("members") or []
    admin = (next((m for m in members if m.get("role") == "superadmin"), None)
             or next((m for m in members if m.get("role") == "admin"), None))
    _sender_cache = (admin or {}).get("id") or info.get("id")
    if not _sender_cache:
        raise RuntimeError("не знайдено super admin id у get_account_info")
    return _sender_cache


def set_webhook(url):
    """Разова реєстрація webhook (Viber вимагає перед постингом у канал).
    url має віддавати HTTP 200 на перевірку Viber."""
    resp = requests.post(SET_WEBHOOK_URL, json={
        "auth_token": _token(), "url": url,
    }, timeout=15).json()
    if resp.get("status") != 0:
        raise RuntimeError(resp.get("status_message") or resp)
    return resp


def post_text(text):
    """Текстовий пост у канал. Повертає JSON-відповідь Viber."""
    body = {
        "auth_token": _token(),
        "from": _sender_id(),
        "type": "text",
        "text": text[:TEXT_LIMIT],
    }
    resp = requests.post(POST_URL, json=body, timeout=20).json()
    if resp.get("status") != 0:
        raise RuntimeError(resp.get("status_message") or resp)
    return resp


# ---------- Логіка дзеркала ----------

def _is_repost(msg):
    """Форвард (репост з іншого каналу/користувача) — за будь-якою з ознак
    (сумісно з різними версіями python-telegram-bot)."""
    return any(getattr(msg, a, None) for a in (
        "forward_origin", "forward_from_chat", "forward_from",
        "forward_sender_name", "forward_date",
    ))


def should_mirror(msg):
    """Чи дзеркалити цей пост: не репост, не службовий, є що постити (текст/
    підпис). Фаза 1 — лише текстовий вміст (фото/відео поки пропускаємо, але
    їхній підпис + лінк дзеркалимо)."""
    if msg is None or _is_repost(msg):
        return False
    return bool(msg.text or msg.caption)


def build_text(msg):
    """Текст поста для Viber: текст або підпис як є (у ньому вже є лінки на
    nikvesti.com)."""
    return (msg.text or msg.caption or "").strip()


async def mirror_channel_post(msg):
    """Дублює один пост каналу у Viber. Тихо виходить, якщо вимкнено або
    пост не підлягає дзеркаленню. Помилку кидає нагору (лог/алерт у bot.py)."""
    import asyncio
    if not is_enabled() or not should_mirror(msg):
        return None
    text = build_text(msg)
    if not text:
        return None
    return await asyncio.to_thread(post_text, text)


# ---------- Команди ----------

_ALLOWED_USER_IDS = {
    int(uid) for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}


def _deny(update):
    return _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS


async def viber_setup_handler(update, context):
    """/viber_setup — зареєструвати webhook (Viber вимагає перед постингом) і
    показати акаунт + супер-адміна. Потрібні VIBER_AUTH_TOKEN і VIBER_WEBHOOK_URL."""
    import asyncio
    if _deny(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_enabled():
        await update.message.reply_text("🦊 Задай VIBER_AUTH_TOKEN у Railway (Developer Tools каналу).")
        return
    webhook = os.environ.get("VIBER_WEBHOOK_URL")
    msg = await update.message.reply_text("🦊 Налаштовую Viber…")
    try:
        info = await asyncio.to_thread(get_account_info)
        sender = await asyncio.to_thread(_sender_id)
        lines = [f"✅ Акаунт: {info.get('name')} (id {info.get('id')})",
                 f"Автор постів (from): {sender}", ""]
        members = info.get("members") or []
        if members:
            lines.append("Учасники (id — для VIBER_SENDER_ID, якщо хочеш іншого автора):")
            for m in members:
                mark = " ← зараз" if m.get("id") == sender else ""
                lines.append(f"• {m.get('name')} — {m.get('role')} — {m.get('id')}{mark}")
            lines.append("")
        if webhook:
            await asyncio.to_thread(set_webhook, webhook)
            lines.append(f"Webhook зареєстровано: {webhook}")
        else:
            lines.append("⚠️ VIBER_WEBHOOK_URL не задано — Viber вимагає webhook "
                         "перед постингом. Дай будь-який HTTPS-URL, що віддає 200 "
                         "(проста сторінка на nikvesti.com), і повтори /viber_setup.")
        lines.append("\nДалі /viber_test — тестовий пост.")
        await msg.edit_text("\n".join(lines), disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Не вдалось: {e}")


async def viber_test_handler(update, context):
    """/viber_test — тестовий пост у Viber-канал (перевірка постингу)."""
    import asyncio
    if _deny(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not is_enabled():
        await update.message.reply_text("🦊 Задай VIBER_AUTH_TOKEN у Railway.")
        return
    msg = await update.message.reply_text("🦊 Публікую тест у Viber…")
    try:
        await asyncio.to_thread(post_text, "🦊 Тест дзеркала МикВісті. Якщо бачите це у Viber — постинг працює.")
        await msg.edit_text("✅ Опубліковано у Viber-канал. Далі кожен пост @nikvesti дублюється сюди сам.")
    except Exception as e:
        hint = ""
        if "webhook" in str(e).lower():
            hint = "\n\nСхоже, не заданий webhook — зроби /viber_setup (потрібен VIBER_WEBHOOK_URL)."
        await msg.edit_text(f"❌ Не вдалось: {e}{hint}")
