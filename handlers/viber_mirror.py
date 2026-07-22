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

Медіа: фото → picture (найбільший розмір ≤1 МБ), відео → video (≤20 МБ через
getFile Telegram, ≤180 с; крупніше/довше — текстом). media = публічний URL,
який віддає bot.get_file (Telegram сам хостить). Будь-який збій медіа →
фолбек на текст, щоб пост не губився. Підпис до відео — окремим текстом
(Viber video підпису не має).

Альбом (кілька фото) TG шле окремими постами з одним media_group_id (у
довільному порядку, підпис лише на одному). Viber альбомів не має — тому
збираємо всю групу в буфер (ALBUM_DEBOUNCE) і публікуємо разом: перше фото з
підписом+лінком, решту фото слідом. Так у Viber «кілька фото + опис», а не
одиноке фото без тексту.

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

import asyncio
import os
from collections import deque

import requests

POST_URL = "https://chatapi.viber.com/pa/post"
ACCOUNT_INFO_URL = "https://chatapi.viber.com/pa/get_account_info"
SET_WEBHOOK_URL = "https://chatapi.viber.com/pa/set_webhook"

TEXT_LIMIT = 7000
PIC_TEXT_LIMIT = 768          # ліміт підпису у picture-повідомленні Viber
PIC_MAX_BYTES = 1_000_000     # Viber: картинка JPEG ≤1 МБ
VIDEO_MAX_BYTES = 20_000_000  # Telegram getFile тягне лише ≤20 МБ (Viber ліміт 50)
VIDEO_MAX_DURATION = 180      # Viber: відео ≤180 с

_sender_cache = None
# Альбоми (кілька фото) приходять з TG окремими постами з одним media_group_id,
# у довільному порядку, підпис — лише на одному з них. Viber альбомів не має,
# тож збираємо всю групу в буфер і публікуємо разом: ПЕРШЕ фото з підписом,
# решту фото слідом (несколько фото + опис). _seen_albums — щоб пізні одинаки
# тієї ж групи (після флашу) не дублювались.
_album_buffer = {}            # media_group_id -> {"msgs": [msg, ...]}
_seen_albums = deque(maxlen=256)
ALBUM_DEBOUNCE = 3.0          # с чекаємо, поки долетять усі фото альбому


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


def post_picture(text, media_url):
    """Картинка у канал. media_url — публічний JPEG ≤1 МБ; text — підпис ≤768."""
    body = {
        "auth_token": _token(),
        "from": _sender_id(),
        "type": "picture",
        "text": (text or "")[:PIC_TEXT_LIMIT],
        "media": media_url,
    }
    resp = requests.post(POST_URL, json=body, timeout=30).json()
    if resp.get("status") != 0:
        raise RuntimeError(resp.get("status_message") or resp)
    return resp


def post_video(media_url, size, duration=0):
    """Відео у канал. media_url — публічний MP4/H264 ≤50 МБ; size — байти
    (обов'язково); підпису video не має — його шлемо окремим текстом."""
    body = {
        "auth_token": _token(),
        "from": _sender_id(),
        "type": "video",
        "media": media_url,
        "size": int(size),
    }
    if duration:
        body["duration"] = min(int(duration), VIDEO_MAX_DURATION)
    resp = requests.post(POST_URL, json=body, timeout=60).json()
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
    """Чи дзеркалити цей пост: не репост, не службовий, є що постити —
    текст/підпис АБО фото/відео (медіа без підпису теж дзеркалимо)."""
    if msg is None or _is_repost(msg):
        return False
    return bool(msg.text or msg.caption or msg.photo or msg.video)


def build_text(msg):
    """Текст поста для Viber: текст або підпис як є (у ньому вже є лінки на
    nikvesti.com)."""
    return (msg.text or msg.caption or "").strip()


async def _mirror_photo(bot, msg, caption):
    """Фото → Viber picture (JPEG ≤1 МБ). Беремо найбільший розмір ≤1 МБ,
    публічний URL Telegram віддає get_file. Довгий підпис (>768) — окремим
    текстом, щоб не різати (лінк зазвичай у кінці). Повертає True, якщо
    відправлено; False — хай іде текстовий фолбек."""
    import asyncio
    fitting = [p for p in msg.photo if (p.file_size or 0) <= PIC_MAX_BYTES]
    size = fitting[-1] if fitting else None  # найбільший, що влазить у 1 МБ
    if size is None:
        return False
    f = await bot.get_file(size.file_id)
    url = f.file_path  # повний https-URL Telegram
    if caption and len(caption) > PIC_TEXT_LIMIT:
        await asyncio.to_thread(post_picture, "", url)
        await asyncio.to_thread(post_text, caption)
    else:
        await asyncio.to_thread(post_picture, caption or "", url)
    return True


async def _mirror_video(bot, msg, caption):
    """Відео → Viber video (MP4 ≤50 МБ, ≤180 с). Обмежені getFile'ом Telegram
    (≤20 МБ) — крупніше/довше піде текстом. Підпис — окремим повідомленням
    (video підпису не має). Повертає True, якщо відправлено."""
    import asyncio
    v = msg.video
    if not v or not v.file_size or v.file_size > VIDEO_MAX_BYTES:
        return False
    if v.duration and v.duration > VIDEO_MAX_DURATION:
        return False
    f = await bot.get_file(v.file_id)
    url = f.file_path
    await asyncio.to_thread(post_video, url, v.file_size, v.duration or 0)
    if caption:
        await asyncio.to_thread(post_text, caption)
    return True


async def _send_one(bot, msg, caption):
    """Публікує один пост/елемент альбому: фото → picture, відео → video,
    інакше (або збій медіа) → текст. True, якщо щось відправлено."""
    posted = False
    try:
        if msg.photo:
            posted = await _mirror_photo(bot, msg, caption)
        elif msg.video:
            posted = await _mirror_video(bot, msg, caption)
    except Exception as e:
        print(f"viber mirror: медіа не пішло ({e}) — фолбек на текст")
        posted = False
    if not posted and caption:
        await asyncio.to_thread(post_text, caption)
        posted = True
    return posted


async def _flush_album(bot, gid):
    """Публікує зібраний альбом одним блоком: перше фото з підписом, решта
    фото слідом (Viber альбомів не має). Викликається з затримкою, щоб
    долетіли всі фото групи."""
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE)
    except asyncio.CancelledError:
        return
    entry = _album_buffer.pop(gid, None)
    _seen_albums.append(gid)  # пізні одинаки цієї групи — ігнор
    if not entry:
        return
    msgs = sorted(entry["msgs"], key=lambda m: m.message_id)
    # Підпис альбому лежить лише на одному повідомленні — беремо перший непорожній
    caption = ""
    for m in msgs:
        c = build_text(m)
        if c:
            caption = c
            break
    posted_any = False
    caption_done = False
    for m in msgs:
        cap = "" if caption_done else caption
        try:
            ok = await _send_one(bot, m, cap)
        except Exception as e:
            print(f"viber mirror album: {e}")
            ok = False
        if ok:
            posted_any = True
            if cap:
                caption_done = True  # підпис пішов з першим відправленим фото
    if not posted_any and caption:
        await asyncio.to_thread(post_text, caption)
        posted_any = True
    if posted_any:
        from handlers import storage
        await asyncio.to_thread(storage.record_viber_post)  # альбом = один пост


async def mirror_channel_post(bot, msg):
    """Дублює пост каналу у Viber: фото/відео з підписом, інакше текст. Альбом
    (media_group_id) буферизується і публікується разом (перше фото з описом,
    решта слідом). Збій медіа → фолбек на текст. Тихо виходить, якщо вимкнено
    або пост не підлягає дзеркаленню."""
    if not is_enabled() or not should_mirror(msg):
        return None
    gid = getattr(msg, "media_group_id", None)
    if gid:
        if gid in _seen_albums:
            return None
        entry = _album_buffer.get(gid)
        if entry is None:
            _album_buffer[gid] = {"msgs": [msg]}
            asyncio.create_task(_flush_album(bot, gid))
        else:
            entry["msgs"].append(msg)
        return None
    caption = build_text(msg)
    if not await _send_one(bot, msg, caption):
        return None
    from handlers import storage
    await asyncio.to_thread(storage.record_viber_post)
    return True


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
