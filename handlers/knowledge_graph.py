"""
Google Knowledge Graph Search API — розвідка «що Google бачить як сутність».

Діагностична команда /kg: по KG ID (напр. /g/11hczwttdm — це МикВісті) або
за текстовим запитом дістає канонічну картку сутності з графа Google:
name, @type, короткий і розгорнутий (Вікіпедія) опис, лого, офіційний сайт,
resultScore. Корисно перевірити, чи Google не переплутав тип/сайт бренду, і
чи взагалі визнає сутність, яку ми привʼязуємо до тегів (тег→Wikidata).

ВАЖЛИВО про межі: цей API віддає лише СТАБ сутності (те, що зринає в підказці
пошуку) — не повну knowledge panel, не запити/ранжування. Для «за чим нас
шукають» точніший інструмент — Search Console (він у бота вже є).

Автентифікація — простий API key (НЕ сервісний акаунт GA4/Search Console):
Google Cloud → Credentials → Create → API key → Railway env GOOGLE_KG_API_KEY.
"""

import asyncio
import os

import requests

from handlers.helpers import escape_html

KG_API = "https://kgsearch.googleapis.com/v1/entities:search"
KG_API_KEY = os.environ.get("GOOGLE_KG_API_KEY")

_ALLOWED_USER_IDS = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}


def _is_id(arg):
    """KG ID виглядає як /g/11… або /m/… (machine-ID), можливо з префіксом kg:."""
    a = arg.strip()
    if a.startswith("kg:"):
        a = a[3:]
    return a.startswith("/g/") or a.startswith("/m/")


def kg_lookup(arg, languages="uk,ru,en", limit=5):
    """Запит до KG Search API. Якщо arg схожий на ID — шукаємо по ids,
    інакше — по тексту. Повертає list елементів itemListElement.
    Синхронно (requests) — викликати через to_thread."""
    params = {"key": KG_API_KEY, "languages": languages, "limit": limit}
    a = arg.strip()
    if _is_id(a):
        # ids приймає «сирий» mid без префікса kg:
        params["ids"] = a[3:] if a.startswith("kg:") else a
    else:
        params["query"] = a
    r = requests.get(KG_API, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("itemListElement", [])


def _format_element(el):
    """Один результат KG → HTML-блок для Telegram."""
    res = el.get("result", {})
    score = el.get("resultScore")
    name = res.get("name") or "(без назви)"
    types = res.get("@type") or []
    if isinstance(types, str):
        types = [types]
    # @type містить і технічний 'Thing' — лишаємо все, це саме те, чим Google
    # вважає сутність.
    types_str = ", ".join(types) if types else "—"
    kg_id = res.get("@id", "")
    lines = [f"<b>{escape_html(name)}</b>"]
    if score is not None:
        lines[0] += f"  <i>(score {score:.1f})</i>"
    lines.append(f"@type: <code>{escape_html(types_str)}</code>")
    if kg_id:
        lines.append(f"KG ID: <code>{escape_html(kg_id)}</code>")
    if res.get("description"):
        lines.append(f"Ярлик: {escape_html(res['description'])}")
    detailed = res.get("detailedDescription") or {}
    if detailed.get("articleBody"):
        body = detailed["articleBody"]
        body = body[:400] + ("…" if len(body) > 400 else "")
        lines.append(f"Опис (Вікіпедія): {escape_html(body)}")
    if detailed.get("url"):
        lines.append(f"Вікі: {escape_html(detailed['url'])}")
    if res.get("url"):
        lines.append(f"Сайт (за версією Google): {escape_html(res['url'])}")
    image = res.get("image") or {}
    if image.get("contentUrl"):
        lines.append(f"Зображення: {escape_html(image['contentUrl'])}")
    return "\n".join(lines)


async def kg_handler(update, context):
    """/kg <KG ID або запит> — картка сутності з Google Knowledge Graph.

    /kg /g/11hczwttdm — по ID (МикВісті)
    /kg МикВісті       — пошук за назвою (покаже, що Google віддає першим)"""
    if _ALLOWED_USER_IDS and update.effective_user.id not in _ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    if not KG_API_KEY:
        await update.message.reply_text(
            "🦊 Немає GOOGLE_KG_API_KEY.\n"
            "Google Cloud → Credentials → Create credentials → API key "
            "(це простий ключ, не сервісний акаунт), і поклади в Railway env "
            "GOOGLE_KG_API_KEY. Не забудь увімкнути Knowledge Graph Search API."
        )
        return
    arg = update.message.text.partition(" ")[2].strip()
    if not arg:
        await update.message.reply_text(
            "Використання: /kg <KG ID або запит>\n"
            "Напр.: /kg /g/11hczwttdm  (по ID)\n"
            "/kg МикВісті  (пошук за назвою)"
        )
        return
    msg = await update.message.reply_text("🦊 Питаю граф Google…")
    try:
        elements = await asyncio.to_thread(kg_lookup, arg)
    except Exception as e:
        await msg.edit_text(
            f"❌ <code>{escape_html(f'{type(e).__name__}: {e}')}</code>",
            parse_mode="HTML",
        )
        return
    if not elements:
        await msg.edit_text(
            "🦊 Google нічого не повернув по цьому "
            f"{'ID' if _is_id(arg) else 'запиту'}. "
            "Або сутності немає в графі, або ID/запит невірний."
        )
        return
    blocks = [_format_element(el) for el in elements[:5]]
    text = "🦊 <b>Knowledge Graph</b>\n\n" + "\n\n".join(blocks)
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n…(обрізано)"
    await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
