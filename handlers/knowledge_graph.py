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
    # languages — ПОВТОРЮВАНИЙ параметр (languages=uk&languages=ru), а не через
    # кому: кома валить запит у 400. requests сам повторює ключ зі списку.
    langs = [x.strip() for x in languages.split(",") if x.strip()]
    params = {"key": KG_API_KEY, "limit": limit}
    if langs:
        params["languages"] = langs
    a = arg.strip()
    if _is_id(a):
        # ids приймає «сирий» mid без префікса kg:
        params["ids"] = a[3:] if a.startswith("kg:") else a
    else:
        params["query"] = a
    # (connect, read) окремо. УВАГА: жоден із них не покриває DNS-резолвінг —
    # якщо резолвер Railway залипає, це ловить лише asyncio.wait_for у хендлері.
    print(f"/kg lookup: {'ids' if _is_id(a) else 'query'}={a!r} langs={langs}")
    r = requests.get(KG_API, params=params, timeout=(8, 12))
    print(f"/kg lookup: HTTP {r.status_code}")
    if not r.ok:
        # Витягуємо зрозуміле повідомлення Google ({"error":{"message":...}}),
        # інакше HTTPError без тіла нічого не пояснює.
        detail = ""
        try:
            detail = (r.json().get("error", {}) or {}).get("message", "")
        except Exception:
            detail = (r.text or "")[:300]
        raise RuntimeError(f"KG API {r.status_code}: {detail or 'без деталей'}")
    elements = r.json().get("itemListElement", [])
    print(f"/kg lookup: {len(elements)} результат(ів)")
    return elements


def _plain(text):
    """Груба деескейпизація HTML-картки у plain text — фолбек, якщо Telegram
    не приймає розмітку (щоб повідомлення не «висіло» вічно)."""
    return (text.replace("<b>", "").replace("</b>", "")
                .replace("<i>", "").replace("</i>", "")
                .replace("<code>", "").replace("</code>", "")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))


def _lang_value(v, langs=("uk", "ru", "en")):
    """KG-поле при запиті КІЛЬКОХ мов приходить не рядком, а списком мовних
    варіантів ([{"@language":"en","@value":"BBC"}, …]) або {"@value":…}.
    Витягуємо один рядок, віддаючи перевагу порядку langs. Це й був баг:
    escape_html(список) падав, хендлер помирав, повідомлення «висіло»."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get("@value") or "")
    if isinstance(v, list):
        by_lang, plain = {}, []
        for item in v:
            if isinstance(item, dict):
                val = str(item.get("@value") or "")
                lang = item.get("@language")
                if lang:
                    by_lang[lang] = val
                elif val:
                    plain.append(val)
            elif isinstance(item, str):
                plain.append(item)
        for l in langs:
            if by_lang.get(l):
                return by_lang[l]
        if by_lang:
            return next(iter(by_lang.values()))
        return plain[0] if plain else ""
    return str(v)


def _first_obj(v):
    """detailedDescription/image теж бувають списком (по мові) — беремо перший
    словник, інакше сам словник, інакше порожній."""
    if isinstance(v, list):
        return next((x for x in v if isinstance(x, dict)), {})
    return v if isinstance(v, dict) else {}


def _format_element(el):
    """Один результат KG → HTML-блок для Telegram."""
    res = el.get("result", {})
    score = el.get("resultScore")
    name = _lang_value(res.get("name")) or "(без назви)"
    types = res.get("@type") or []
    if isinstance(types, str):
        types = [types]
    # @type містить і технічний 'Thing' — лишаємо все, це саме те, чим Google
    # вважає сутність.
    types_str = ", ".join(str(t) for t in types) if types else "—"
    kg_id = res.get("@id", "")
    lines = [f"<b>{escape_html(name)}</b>"]
    if score is not None:
        try:
            lines[0] += f"  <i>(score {float(score):.1f})</i>"
        except (TypeError, ValueError):
            pass
    lines.append(f"@type: <code>{escape_html(types_str)}</code>")
    if kg_id:
        lines.append(f"KG ID: <code>{escape_html(str(kg_id))}</code>")
    desc = _lang_value(res.get("description"))
    if desc:
        lines.append(f"Ярлик: {escape_html(desc)}")
    detailed = _first_obj(res.get("detailedDescription"))
    body = _lang_value(detailed.get("articleBody"))
    if body:
        body = body[:400] + ("…" if len(body) > 400 else "")
        lines.append(f"Опис (Вікіпедія): {escape_html(body)}")
    if detailed.get("url"):
        lines.append(f"Вікі: {escape_html(str(detailed['url']))}")
    if res.get("url"):
        lines.append(f"Сайт (за версією Google): {escape_html(str(res['url']))}")
    image = _first_obj(res.get("image"))
    if image.get("contentUrl"):
        lines.append(f"Зображення: {escape_html(str(image['contentUrl']))}")
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
    # Маркер версії у першому повідомленні — щоб очима бачити, що працює саме
    # новий код (з wait_for), а не старий деплой без нього.
    msg = await update.message.reply_text("🦊 Питаю граф Google (v3)…")
    try:
        # Жорсткий загальний таймаут: таймаут requests не покриває DNS-резолвінг,
        # тому wait_for — єдиний захист від вічного «Питаю граф…».
        elements = await asyncio.wait_for(asyncio.to_thread(kg_lookup, arg), timeout=20)
    except asyncio.TimeoutError:
        await msg.edit_text(
            "🦊 Google не відповів за 20 с — схоже, залип DNS/мережа Railway "
            "(таймаут requests DNS не покриває). Спробуй ще раз; якщо повторюється — "
            "у логах Railway буде видно, на якому кроці затик."
        )
        return
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
            "Або сутності немає в пошуковій підмножині KG API, або ID/запит невірний."
        )
        return
    # Форматування + відправка в одному захисті: будь-яка несподіванка в даних
    # KG не має лишати повідомлення «висіти» — редагуємо його хоч чимось.
    try:
        blocks = [_format_element(el) for el in elements[:5]]
        text = "🦊 <b>Knowledge Graph</b>\n\n" + "\n\n".join(blocks)
        if len(text) > 4000:
            text = text[:4000].rsplit("\n", 1)[0] + "\n…(обрізано)"
        try:
            await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            # Битий HTML від Google-описів → plain text (лінки/текст лишаються).
            print(f"/kg: HTML edit не вдався ({e}) — фолбек plain")
            await msg.edit_text(_plain(text), disable_web_page_preview=True)
    except Exception as e:
        print(f"/kg: рендер {len(elements)} результатів упав — {type(e).__name__}: {e}")
        await msg.edit_text(
            f"❌ Є {len(elements)} результат(ів), але рендер упав: "
            f"{escape_html(f'{type(e).__name__}: {e}')}",
            parse_mode="HTML",
        )
