"""
Сторож токенів Meta: FACEBOOK_PAGE_TOKEN і INSTAGRAM_TOKEN.

12.08.2026 токен протух о 17:21 за Києвом — і бот ПРОМОВЧАВ майже добу:
щогодинний fb_missing чесно трактує будь-яку помилку Graph API як «unknown»
(помилка API ≠ «поста немає») і не алертить, тижневий ФБ-звіт ковтає виняток
у print, а редакція дізналась про протухання лише зі /stat наступного дня —
з порадою «спробуйте ще раз за хвилину, це тимчасово», хоча протухлий токен
сам не оживає ніколи. Тому окремий сторож, за зразком check_tiktok_health:

1. Щогодинна перевірка живості токена (один копійчаний запит /{PAGE_ID}):
   протух → приватний алерт Олегу з інструкцією оновлення. Поки не
   полагоджено — нагадування раз на ДОБУ, не щогодини: лагодження вимагає
   людини, і 24 однакові повідомлення на день не пришвидшать його ні на хвилину.
2. Той самий алерт смикається реактивно зі /stat (щоб не чекати до години),
   а /stat більше не називає протухлий токен «тимчасовим».
3. Після заміни токена сторож сам каже «знову живий» — щоб було видно, що
   нова змінна на Railway підхопилась і можна не перевіряти руками.
4. Best-effort попередження ЗАЗДАЛЕГІДЬ: debug_token віддає expires_at
   (коли віддає — залежить від типу токена), і за 7 днів до кінця бот
   попереджає один раз. Вічний токен (expires_at=0) нічого не шле.

Інста приїхала сюди 21.08.2026 і рівно з тієї ж причини. Її токен протух
12 серпня — і про це не сказав НІХТО дев'ять днів: тижневий IG-звіт ковтає
виняток, а нова команда /post чесно спитала інсту, дістала «Session has
expired» і переказала це людині як «схоже, пост давніший». Тобто урок
12.08 був записаний у код лише для Facebook, хоч ціна помилки та сама.
Другого модуля не заводимо: перевірка, дедуп, одужання й попередження — одні
й ті самі, різняться лише адреса запиту, назва змінної та інструкція заміни
(SPECS нижче).

Стан — storage.{fb,ig}_token, переживає редеплой.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import requests

from handlers import storage
from handlers.notifier import ADMIN_CHAT_ID, KYIV_TZ

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")
INSTAGRAM_TOKEN = os.environ.get("INSTAGRAM_TOKEN")
INSTAGRAM_USER_ID = os.environ.get("INSTAGRAM_USER_ID") or "17841400860799899"

# Нагадувати про мертвий токен не частіше ніж раз на добу
REALERT_HOURS = 24
# Попереджати заздалегідь, коли до кінця дії лишилось менше
EXPIRY_WARN_DAYS = 7

# Маркери помилок токена (родина OAuthException code 190). Звіряємо по тексту
# повідомлення, бо саме він доступний усюди, де помилка вже перетворена на
# рядок (graph_get, get_fb_stats); "(#190)" — на випадок, коли Facebook
# лишив код у тексті. Ліміти (#4/#17/#32) і таймаути сюди НЕ входять — вони
# справді тимчасові.
_TOKEN_ERROR_MARKERS = (
    "error validating access token",
    "session has expired",
    "session has been invalidated",
    "invalid oauth access token",
    "access token could not be decrypted",
    "malformed access token",
    "has not authorized application",
    "(#190)",
)


def is_token_error(error_text):
    """Чи каже цей текст помилки Graph API про мертвий/протухлий токен."""
    if not error_text:
        return False
    low = str(error_text).lower()
    return any(marker in low for marker in _TOKEN_ERROR_MARKERS)


RENEW_TEXT = (
    "Як оновити (~5 хв):\n"
    "1. developers.facebook.com/tools/explorer — обрати додаток бота, "
    "залогінитись від акаунта з правами на сторінку МикВісті.\n"
    "2. У «User or Page» обрати Page: МикВісті → Generate Access Token "
    "(права ті самі: pages_show_list, read_insights, pages_read_engagement, "
    "pages_read_user_content).\n"
    "3. Продовжити до довгоживучого: ⓘ біля токена → «Open in Access Token "
    "Tool» → кнопка «Extend Access Token» (інакше протухне знову за ~2 години).\n"
    "4. Railway → сервіс бота → Variables → FACEBOOK_PAGE_TOKEN → вставити "
    "новий → редеплой.\n"
    "Перевірка: /stat будь-якого свіжого матеріалу — блок ФБ має віддати числа; "
    "сторож сам напише, коли побачить живий токен."
)


IG_RENEW_TEXT = (
    "Як оновити (~5 хв):\n"
    "1. developers.facebook.com/tools/explorer — обрати додаток бота, "
    "залогінитись від акаунта, що адмініструє сторінку МикВісті.\n"
    "2. Права: instagram_basic, instagram_manage_insights, pages_show_list, "
    "pages_read_engagement → Generate Access Token.\n"
    "3. Продовжити до довгоживучого: ⓘ біля токена → «Open in Access Token "
    "Tool» → «Extend Access Token» (дає ~60 днів; без цього протухне за години).\n"
    "4. Railway → сервіс бота → Variables → INSTAGRAM_TOKEN → вставити новий "
    "→ редеплой.\n"
    "Перевірка: /instagram має віддати числа; сторож сам напише, коли "
    "побачить живий токен."
)

# Що саме про кожен токен знає сторож. Різняться тільки ці чотири рядки —
# уся механіка (перевірка, дедуп, одужання, попередження) спільна.
SPECS = {
    "fb": {
        "title": "Facebook",
        "var": "FACEBOOK_PAGE_TOKEN",
        "silent": ("блок ФБ у /stat і /post, монітор новин без ФБ, недільний "
                   "ФБ-звіт, тижневик, лінки ФБ у контент-звітах і "
                   "ФБ-стовпчики таблиці аналітики"),
        "alive": "Токен Facebook знову живий — /stat, монітори і звіти працюють.",
    },
    "ig": {
        "title": "Instagram",
        "var": "INSTAGRAM_TOKEN",
        "silent": ("блок інсти у /stat, /post по дописах інсти, /instagram, "
                   "недільний IG-звіт і IG-стовпчики таблиці аналітики"),
        "alive": "Токен Instagram знову живий — /instagram і блок інсти у /stat працюють.",
    },
}


def renew_text(name):
    return RENEW_TEXT if name == "fb" else IG_RENEW_TEXT


def _dead_alert_text(name, error_text):
    spec = SPECS[name]
    return (
        f"🔴 <b>Протух токен {spec['title']}</b> ({spec['var']})\n\n"
        f"{str(error_text)[:300]}\n\n"
        f"Поки він мертвий, мовчать: {spec['silent']}.\n\n"
        + renew_text(name)
        + f"\n\n(Нагадуватиму раз на {REALERT_HOURS} год, поки не полагоджено.)"
    )


# ---------- Перевірки (sync, ходять у мережу — кликати через to_thread) ----------

def probe_token():
    """Живість токена Facebook одним найдешевшим запитом.
    → ("ok", None) | ("token", <текст помилки 190>) | ("other", <текст>) |
      ("network", <текст>). Мережеві збої і не-токенові помилки Graph
    сторож НЕ алертить — це територія тимчасового."""
    return _probe("https://graph.facebook.com/v25.0/" + str(FACEBOOK_PAGE_ID),
                  FACEBOOK_PAGE_TOKEN)


def probe_instagram_token():
    """Те саме для INSTAGRAM_TOKEN. Адреса інша (graph.instagram.com), а
    родина помилок та сама — «Session has expired» саме звідти й прийшло."""
    return _probe("https://graph.instagram.com/v21.0/" + str(INSTAGRAM_USER_ID),
                  INSTAGRAM_TOKEN)


def _probe(url, token):
    try:
        data = requests.get(url, params={"fields": "id", "access_token": token},
                            timeout=15).json()
    except Exception as e:
        return "network", str(e)
    if isinstance(data, dict) and "error" in data:
        msg = data["error"].get("message") or "невідома помилка Graph API"
        return ("token", msg) if is_token_error(msg) else ("other", msg)
    return "ok", None


def token_expires_at(token=None):
    """Unix-час кінця дії токена з debug_token, best-effort: 0 = вічний,
    None = не дізнатись (debug_token доступний не кожному типу токена —
    тоді лишається реактивна гілка сторожа)."""
    token = token or FACEBOOK_PAGE_TOKEN
    try:
        data = requests.get(
            "https://graph.facebook.com/v25.0/debug_token",
            params={"input_token": token, "access_token": FACEBOOK_PAGE_TOKEN},
            timeout=15,
        ).json()
        expires = data.get("data", {}).get("expires_at")
        return expires if isinstance(expires, int) else None
    except Exception:
        return None


# ---------- Алерти ----------

async def alert_dead(bot, name, error_text, source=""):
    """Алерт «токен мертвий» з дедупом раз на REALERT_HOURS. Кликати звідусіль,
    де в руках і bot, і помилка 190: сторож — щогодини, /stat і /post — у
    момент команди. Тихо ковтає власні збої: алерт не має ламати виклик, що і
    так уже впав."""
    try:
        state = storage.get_token_state(name)
        now = datetime.now(KYIV_TZ)
        last = state.get("last_alert_at")
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=REALERT_HOURS):
                    # у стані лишаємо ознаку «мертвий», щоб одужання помітилось
                    if not state.get("down_since"):
                        state["down_since"] = now.isoformat()
                        storage.save_token_state(name, state)
                    return
            except ValueError:
                pass
        state["down_since"] = state.get("down_since") or now.isoformat()
        state["last_alert_at"] = now.isoformat()
        storage.save_token_state(name, state)
        suffix = f"\n\nПомітив: {source}" if source else ""
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=_dead_alert_text(name, error_text) + suffix,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"fb_token: не вдалось надіслати алерт {name} — {e}")


async def alert_token_dead(bot, error_text, source=""):
    """Facebook — стара назва, лишається заради наявних викликів зі /stat."""
    await alert_dead(bot, "fb", error_text, source)


async def alert_ig_token_dead(bot, error_text, source=""):
    await alert_dead(bot, "ig", error_text, source)


async def check_meta_tokens(bot):
    """Щогодинний сторож обох токенів. Тихий, поки вони живі й не добігають
    кінця. Токени перевіряються НЕЗАЛЕЖНО: мертва інста не має ховати
    здоровий Facebook і навпаки."""
    if FACEBOOK_PAGE_TOKEN and FACEBOOK_PAGE_ID:
        await _check_one(bot, "fb", probe_token, FACEBOOK_PAGE_TOKEN)
    if INSTAGRAM_TOKEN:
        await _check_one(bot, "ig", probe_instagram_token, INSTAGRAM_TOKEN)


# Стара назва — на випадок зовнішніх викликів; розклад кличе check_meta_tokens
check_fb_token = check_meta_tokens


async def _check_one(bot, name, probe, token):
    status, err = await asyncio.to_thread(probe)

    if status == "token":
        await alert_dead(bot, name, err, source="щогодинна перевірка")
        return
    if status != "ok":
        # мережа лягла або Graph чудить не токеном — не наша тривога
        if err:
            print(f"fb_token: перевірка {name} не вдалась ({status}) — {err}")
        return

    state = storage.get_token_state(name)
    changed = False

    # Одужання: до цього алертили про мертвий → скажемо, що знову живий,
    # і скинемо інцидент (наступне протухання алертне знову одразу)
    if state.get("down_since"):
        state.pop("down_since", None)
        state.pop("last_alert_at", None)
        changed = True
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID,
                                   text="✅ " + SPECS[name]["alive"])
        except Exception as e:
            print(f"fb_token: не вдалось надіслати «знову живий» {name} — {e}")

    # Попередження заздалегідь (best-effort): раз на конкретний expires_at,
    # щоб заміна токена (новий expires_at) знову мала своє попередження
    expires = await asyncio.to_thread(token_expires_at, token)
    if expires:  # 0/None = вічний або не дізнатись — мовчимо
        left = datetime.fromtimestamp(expires, tz=timezone.utc) - datetime.now(timezone.utc)
        if left <= timedelta(days=EXPIRY_WARN_DAYS) and state.get("warned_expiry") != expires:
            state["warned_expiry"] = expires
            changed = True
            when = datetime.fromtimestamp(expires, tz=KYIV_TZ).strftime("%d.%m.%Y %H:%M")
            days = max(left.days, 0)
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(f"🟠 <b>Токен {SPECS[name]['title']} протухне {when}</b> "
                          f"(лишилось ~{days} дн.)\n\n"
                          "Краще замінити заздалегідь — інакше на день-два "
                          f"замовкнуть {SPECS[name]['silent']}.\n\n"
                          + renew_text(name)),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print(f"fb_token: не вдалось надіслати попередження {name} — {e}")

    if changed:
        storage.save_token_state(name, state)
