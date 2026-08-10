"""
Детектор сплесків трафіку (проактивний Лис) — REVIEW_2026_07.md, п. а.4.

Як працює:
1. Кожні 30 хвилин знімаємо кількість активних користувачів на сайті
   (GA4 Realtime API — вікно останніх ~30 хвилин).
2. Замір кладемо в профіль "типового трафіку" у слот (день тижня, година)
   за Києвом. Типовість слота = медіана останніх PROFILE_MAX_SAMPLES
   замірів — медіана стійка до самих сплесків, тому вони не завищують
   базу. Профіль будується сам: перші дні модуль тільки вчиться і мовчить
   (потрібно мінімум MIN_SAMPLES_FOR_ALERT замірів у слоті).
3. Сплеск = поточне значення >= SPIKE_RATIO × медіани слота
   І >= SPIKE_MIN_USERS (абсолютний поріг, щоб вночі 30→70 читачів
   не будили редакцію).
4. При сплеску: топ матеріалів просто зараз (Realtime, заголовки сторінок)
   + best-effort розбивка джерел трафіку за сьогодні (стандартний
   intraday-звіт GA4 — може відставати на годину-дві, тому як довідка)
   → повідомлення в чат редакції з короткою AI-підводкою Лиса.
5. Кулдаун ALERT_COOLDOWN_HOURS годин між алертами — один довгий сплеск
   не спамить щопівгодини.
6. Дедуп за ЗМІСТОМ: бот пам'ятає, які матеріали були в блоці «Що читають»
   останнього алерту. Якщо лідер нового сплеску вже був у тому списку —
   це та сама історія, і алерт не повторюється (кейс серпня 2026: вербовий
   ліс на дні Каховки спамив два дні поспіль, мінялось тільки число
   читачів). Порівнюємо з УСІМ списком, а не лише з лідером, бо той самий
   матеріал живе двома мовними версіями з різними заголовками (рос. і укр.
   версії — окремі рядки Realtime-топу), і зміна мови лідера — не новина.
   Бот озветься про ту саму історію знову лише якщо:
   — хвиля ВИРОСЛА: поточне значення ≥ ESCALATION_RATIO × числа з минулого
     алерту (це нова інформація, а не повтор), або
   — минуло SAME_TOP_MEMORY_DAYS днів (нова хвиля через тиждень — окрема
     подія: нові репости, інший привід).

/traffic — ручна діагностика: поточне значення, типове для слота,
скільки замірів зібрано, топ сторінок зараз. Працює одразу, без warmup.

Стан — у /data/prozorro_state.json, ключ "traffic_spikes":
{"profile": {"0_14": [312, 298, ...], ...}, "last_alert_at": "2026-07-02T14:35:00",
 "last_alert_top": ["заголовок 1", ...], "last_alert_users": 969}
"""

import asyncio
import os
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo

from google.analytics.data_v1beta.types import (
    RunRealtimeReportRequest, RunReportRequest, DateRange, Metric, Dimension, OrderBy,
)

from handlers import storage
from handlers.google_analytics import get_ga4_client, GA4_PROPERTY_ID
from handlers.ai_messages import fox_generate, FOX_MODEL_FAST
from handlers.helpers import escape_html

KYIV_TZ = ZoneInfo("Europe/Kiev")
CHAT_ID = os.environ.get("CHAT_ID")

SPIKE_RATIO = 2.0            # у скільки разів поточне має перевищити медіану слота
SPIKE_MIN_USERS = 150        # абсолютний поріг — нижче цього сплеск не рахуємо
MIN_SAMPLES_FOR_ALERT = 3    # мінімум замірів у слоті, щоб довіряти медіані
PROFILE_MAX_SAMPLES = 8      # скільки останніх замірів тримаємо на слот (~4 тижні історії)
ALERT_COOLDOWN_HOURS = 3     # пауза між алертами
SAME_TOP_MEMORY_DAYS = 7     # той самий матеріал-лідер не повторюється тиждень…
ESCALATION_RATIO = 1.5       # …хіба що хвиля виросла у півтора раза від минулого алерту


# ---------- Синхронні GA4-запити (викликаються через asyncio.to_thread) ----------

def _realtime_snapshot():
    """Повертає (загальна кількість активних користувачів, топ-5 сторінок [(title, users)])."""
    client = get_ga4_client()

    total_resp = client.run_realtime_report(RunRealtimeReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        metrics=[Metric(name="activeUsers")],
    ))
    total = int(total_resp.rows[0].metric_values[0].value) if total_resp.rows else 0

    # Realtime API не має pagePath — тільки unifiedScreenName (заголовок сторінки)
    top_resp = client.run_realtime_report(RunRealtimeReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="unifiedScreenName")],
        metrics=[Metric(name="activeUsers")],
        limit=8,
    ))
    top_pages = []
    for row in top_resp.rows:
        title = row.dimension_values[0].value
        users = int(row.metric_values[0].value)
        if not title or title.lower() in ("(not set)",):
            continue
        top_pages.append((title, users))
        if len(top_pages) == 5:
            break

    return total, top_pages


def _today_sources():
    """Best-effort розбивка джерел трафіку за сьогодні (intraday-дані GA4
    можуть відставати на годину-дві — тому лише як довідка в алерті)."""
    client = get_ga4_client()
    resp = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        date_ranges=[DateRange(start_date="today", end_date="today")],
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[Metric(name="activeUsers")],
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="activeUsers"), desc=True)],
        limit=5,
    ))
    return [(row.dimension_values[0].value, int(row.metric_values[0].value)) for row in resp.rows]


# ---------- Профіль ----------

def _slot_key(now_kyiv):
    return f"{now_kyiv.weekday()}_{now_kyiv.hour}"


def _get_state():
    return storage.get_traffic_spikes_state()


def _update_profile(state, slot, value):
    profile = state.setdefault("profile", {})
    samples = profile.setdefault(slot, [])
    samples.append(value)
    if len(samples) > PROFILE_MAX_SAMPLES:
        del samples[:-PROFILE_MAX_SAMPLES]


# ---------- Основна перевірка (планувальник, кожні 30 хв) ----------

async def check_traffic_spikes(bot):
    if not CHAT_ID:
        return
    try:
        now_kyiv = datetime.now(KYIV_TZ)
        slot = _slot_key(now_kyiv)

        current, top_pages = await asyncio.to_thread(_realtime_snapshot)

        state = await asyncio.to_thread(_get_state)
        samples = list(state.get("profile", {}).get(slot, []))

        # Спершу вирішуємо про сплеск на СТАРИХ замірах, потім додаємо поточний
        is_spike = False
        typical = None
        if len(samples) >= MIN_SAMPLES_FOR_ALERT:
            typical = median(samples)
            if typical > 0 and current >= SPIKE_RATIO * typical and current >= SPIKE_MIN_USERS:
                is_spike = True

        _update_profile(state, slot, current)

        if is_spike:
            last_alert = state.get("last_alert_at")
            cooled_down = True
            hours_since = None
            if last_alert:
                hours_since = (now_kyiv - datetime.fromisoformat(last_alert)).total_seconds() / 3600
                cooled_down = hours_since >= ALERT_COOLDOWN_HOURS
            if cooled_down:
                # Дедуп за змістом: лідер сплеску вже був у списку минулого
                # алерту → та сама історія, повторювати нема чого. Виняток —
                # хвиля виросла (нова інформація) або минув тиждень (нова хвиля).
                prev_titles = state.get("last_alert_top") or []
                prev_users = state.get("last_alert_users") or 0
                same_story = (
                    top_pages
                    and top_pages[0][0] in prev_titles
                    and hours_since is not None
                    and hours_since < SAME_TOP_MEMORY_DAYS * 24
                )
                escalated = same_story and prev_users and current >= ESCALATION_RATIO * prev_users
                if not same_story or escalated:
                    await _send_spike_alert(bot, current, typical, top_pages, escalated=bool(escalated))
                    state["last_alert_at"] = now_kyiv.isoformat()
                    state["last_alert_top"] = [title for title, _ in top_pages]
                    state["last_alert_users"] = current

        await asyncio.to_thread(storage.save_traffic_spikes_state, state)
    except Exception as e:
        print(f"Сплески трафіку: помилка — {e}")
        from handlers.notifier import notify_error
        await notify_error(bot, "детектор сплесків трафіку", e)


async def _send_spike_alert(bot, current, typical, top_pages, escalated=False):
    ratio = current / typical if typical else 0

    top_lines = "\n".join(
        f"{i+1}. {escape_html(title)} — {users}"
        for i, (title, users) in enumerate(top_pages)
    ) or "немає даних"

    sources_line = ""
    try:
        sources = await asyncio.to_thread(_today_sources)
        if sources:
            sources_text = ", ".join(f"{name} ({users})" for name, users in sources[:4])
            sources_line = f"\n\n📡 Джерела за сьогодні: {sources_text}"
    except Exception as e:
        print(f"Сплески трафіку: джерела недоступні — {e}")

    context_line = (
        "Про цей сплеск редакція вже знає з попереднього алерту, але хвиля відтоді ПОМІТНО ВИРОСЛА — саме це і є новина, скажи про це прямо."
        if escalated
        else "Щось залетіло, варто глянути що і, можливо, підхопити тему (оновити матеріал, дотиснути в соцмережах)."
    )
    intro = ""
    try:
        intro = await fox_generate(
            f"""На сайті сплеск трафіку: зараз ~{current} активних читачів — це у {ratio:.1f} рази більше, ніж типово о цій порі (~{typical:.0f}).
Найпопулярніший матеріал зараз: "{top_pages[0][0] if top_pages else 'невідомо'}".

Напиши 1-2 короткі речення підводки для чату редакції. {context_line} Без цифр — вони будуть нижче. Без пафосу.""",
            model=FOX_MODEL_FAST,
            max_tokens=150,
        )
    except Exception as e:
        print(f"Сплески трафіку: AI-підводка не вдалась — {e}")

    text = (
        "🔥 <b>Сплеск трафіку росте!</b>\n" if escalated
        else "🔥 <b>Сплеск трафіку на сайті!</b>\n"
    )
    if intro:
        text += f"\n🦊 {escape_html(intro.strip())}\n"
    text += (
        f"\n👥 Зараз на сайті: <b>~{current}</b> активних читачів"
        f"\n📈 Типово о цій порі: ~{typical:.0f} (перевищення у {ratio:.1f}×)"
        f"\n\n📰 Що читають просто зараз:\n{top_lines}"
        f"{sources_line}"
    )

    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")


# ---------- /traffic — ручна діагностика ----------

async def traffic_handler(update, context):
    try:
        now_kyiv = datetime.now(KYIV_TZ)
        slot = _slot_key(now_kyiv)

        current, top_pages = await asyncio.to_thread(_realtime_snapshot)
        state = await asyncio.to_thread(_get_state)
        samples = state.get("profile", {}).get(slot, [])

        if samples:
            typical = median(samples)
            ratio_text = f"{current / typical:.1f}×" if typical else "н/д"
            profile_text = (
                f"📈 Типово о цій порі: ~{typical:.0f} (медіана {len(samples)} замірів, зараз {ratio_text})"
            )
        else:
            profile_text = "📈 Для цього слота (день тижня + година) ще немає замірів — профіль набирається."

        top_lines = "\n".join(
            f"{i+1}. {escape_html(title)} — {users}"
            for i, (title, users) in enumerate(top_pages)
        ) or "немає даних"

        total_samples = sum(len(v) for v in state.get("profile", {}).values())

        last_alert_text = ""
        last_alert = state.get("last_alert_at")
        if last_alert:
            last_dt = datetime.fromisoformat(last_alert)
            last_alert_text = f"\n\n🔔 Останній алерт: {last_dt.strftime('%d.%m %H:%M')}"
            prev_titles = state.get("last_alert_top") or []
            if prev_titles:
                prev_users = state.get("last_alert_users") or 0
                last_alert_text += (
                    f", лідер «{escape_html(prev_titles[0])}» (~{prev_users} читачів).\n"
                    f"Про цю історію бот повторно не скаже, поки хвиля не виросте до "
                    f"~{int(prev_users * ESCALATION_RATIO)} або лідером не стане інший матеріал"
                )

        await update.message.reply_text(
            f"👥 Зараз на сайті: <b>~{current}</b> активних читачів (останні ~30 хв)\n"
            f"{profile_text}\n\n"
            f"📰 Що читають просто зараз:\n{top_lines}\n\n"
            f"🗂 Профіль: {total_samples} замірів у {len(state.get('profile', {}))} слотах "
            f"(алерт після {MIN_SAMPLES_FOR_ALERT} замірів у слоті, поріг {SPIKE_RATIO}× і ≥{SPIKE_MIN_USERS} читачів)"
            f"{last_alert_text}",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")
