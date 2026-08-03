"""
/fbdebug <url статті> [id або лінк поста ФБ] — діагностика пошуку публікації у
Facebook для /stat.

Навіщо. Коли /stat каже «Публікацій не знайдено», а пост у стрічці ФБ очевидно
є, причин рівно три, і на вигляд вони однакові:
  1) поста не було на момент виклику (SMM запостив пізніше);
  2) пост є, але Graph API не віддав його на тому ребрі, яке ми питаємо
     (`/posts` не завжди показує відео-публікації — для них є `/video_reels`
     і `/videos`);
  3) пост у вибірці БУВ, але не збігся: лінк на статтю лежить не в message, а
     в описі вкладення (класика відео-поста, де message взагалі порожній).
Розрізнити їх можна тільки подивившись, що саме віддав API — саме це й показує
команда: вікно пошуку, ЩО повернуло кожне ребро і де в об'єкті трапився ID
статті. Один виклик замість години здогадок (інцидент 03.08.2026, матеріал
321935 — відео-пост знайшовся, лише коли в матчинг додали описи вкладень).

Нічого не пише і не зберігає — тільки читає Graph API.
"""

import asyncio
import html
import json
import os
from datetime import datetime

import requests

from handlers.helpers import extract_article_id
from handlers.stat import (
    KYIV_TZ,
    POST_FIELDS,
    _clean_url,
    _matches_article,
    fb_search_window,
    get_article_published_date,
    match_source,
)

FACEBOOK_PAGE_TOKEN = os.environ.get("FACEBOOK_PAGE_TOKEN")
FACEBOOK_PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID")

GRAPH = "https://graph.facebook.com/v25.0"
MAX_LISTED = 20  # скільки постів вікна показуємо рядками (решта — числом)


def _kyiv(created_time):
    """created_time Graph API → 'DD.MM HH:MM' за Києвом ('?' якщо не розпарсити)."""
    try:
        dt = datetime.strptime(created_time, "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(KYIV_TZ).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return "?"


def _fetch_edge(edge, fields, since=None, until=None, pages=3):
    """Сторінки ребра з пагінацією. Повертає (елементи, помилка або None)."""
    url = f"{GRAPH}/{FACEBOOK_PAGE_ID}/{edge}"
    params = {"fields": fields, "limit": 100, "access_token": FACEBOOK_PAGE_TOKEN}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    items, error = [], None
    for _ in range(pages):
        try:
            data = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            error = str(e)
            break
        if "error" in data:
            error = data["error"].get("message", "?")
            break
        items.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url, params = next_url, None
    return items, error


def _text_of(item):
    """Увесь текст елемента відео-ребра (там підпис лежить у description/title)."""
    return " ".join(str(item.get(k) or "") for k in ("description", "title", "message"))


def _edge_report(title, items, error, clean, article_id, text_match):
    """Рядок звіту по одному ребру: скільки віддало і чи є там наша стаття."""
    if error:
        return f"<b>{title}</b>: ⚠️ {html.escape(error[:120])}"
    hits = [it for it in items if text_match(it)]
    line = f"<b>{title}</b>: {len(items)}"
    if hits:
        ids = ", ".join(str(h.get("id"))[:40] for h in hits[:3])
        times = ", ".join(_kyiv(h.get("created_time")) for h in hits[:3])
        line += f" → ✅ збіг: {ids} ({times})"
    else:
        line += " → збігів немає"
    return line


def _probe_object(obj_id, clean, article_id):
    """Прямий запит конкретного об'єкта: що саме віддає API і де в ньому ID."""
    lines = []
    tried = [obj_id]
    if "_" not in obj_id and FACEBOOK_PAGE_ID:
        tried.append(f"{FACEBOOK_PAGE_ID}_{obj_id}")
    for oid in tried:
        try:
            data = requests.get(
                f"{GRAPH}/{oid}",
                params={"fields": POST_FIELDS, "access_token": FACEBOOK_PAGE_TOKEN},
                timeout=30,
            ).json()
        except Exception as e:
            lines.append(f"  {oid}: ⚠️ {html.escape(str(e)[:100])}")
            continue
        if "error" in data:
            lines.append(f"  {oid}: ⚠️ {html.escape(data['error'].get('message', '?')[:100])}")
            continue
        raw = json.dumps(data, ensure_ascii=False)
        src = match_source(data, clean, article_id)
        msg = (data.get("message") or "").strip()
        lines.append(
            f"  <code>{html.escape(oid)}</code> від {_kyiv(data.get('created_time'))}\n"
            f"  message: {'є (' + str(len(msg)) + ' симв.)' if msg else '❌ порожній'}\n"
            f"  «{article_id}» в об'єкті: {'так' if article_id in raw else 'ні'}\n"
            f"  матч /stat: {src or '❌ не збігається'}"
        )
        break
    return lines


def _diagnose(article_url, obj_id=None):
    """Синхронна частина: усі запити до Graph API і збірка тексту звіту."""
    clean = _clean_url(article_url)
    article_id = extract_article_id(article_url)
    pub_date = get_article_published_date(article_url)
    since_ts, until_ts = fb_search_window(pub_date)

    def ts_kyiv(ts):
        return datetime.fromtimestamp(ts, KYIV_TZ).strftime("%d.%m %H:%M")

    lines = [
        f"🔎 <b>FB-діагностика {article_id}</b>",
        f'<a href="{clean}">{clean}</a>',
        "",
        "Дата статті: " + (
            pub_date.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
            if pub_date and pub_date.tzinfo
            else (pub_date.strftime("%d.%m.%Y %H:%M") if pub_date else "❌ не визначилась")
        ),
        f"Вікно пошуку: {ts_kyiv(since_ts)} → {ts_kyiv(until_ts)} (Київ)",
        "",
    ]

    # 1. Стрічка постів у вікні — те саме ребро і поля, що в /stat
    posts, posts_err = _fetch_edge("posts", POST_FIELDS, since_ts, until_ts, pages=15)
    if posts_err:
        lines.append(f"<b>/posts у вікні</b>: ⚠️ {html.escape(posts_err[:150])}")
    else:
        lines.append(f"<b>/posts у вікні</b>: {len(posts)} постів")
        matched = [(p, match_source(p, clean, article_id)) for p in posts]
        hits = [(p, s) for p, s in matched if s]
        # пост, у якому ID статті десь є, але матч не спрацював — головна підказка
        raw_only = [
            p for p, s in matched
            if not s and article_id in json.dumps(p, ensure_ascii=False)
        ]
        for p, s in hits:
            lines.append(f"  ✅ {p.get('id')} від {_kyiv(p.get('created_time'))} — поле: {s}")
        for p in raw_only:
            lines.append(
                f"  ⚠️ {p.get('id')} від {_kyiv(p.get('created_time'))} — "
                f"«{article_id}» в об'єкті Є, але матч не спрацював"
            )
        if not hits and not raw_only:
            lines.append("  ❌ статті немає в жодному пості вікна")
            for p in posts[:MAX_LISTED]:
                msg = (p.get("message") or "").strip().replace("\n", " ")
                media = ""
                att = (p.get("attachments", {}) or {}).get("data", [])
                if att:
                    media = att[0].get("media_type", "") or ""
                lines.append(
                    f"  · {_kyiv(p.get('created_time'))} {media or '—'} "
                    f"{'msg:' + html.escape(msg[:40]) if msg else 'БЕЗ message'}"
                )
            if len(posts) > MAX_LISTED:
                lines.append(f"  … і ще {len(posts) - MAX_LISTED}")

    # 2. Останні пости БЕЗ фільтра часу — чи не в самому вікні справа
    recent, recent_err = _fetch_edge("posts", POST_FIELDS, pages=1)
    lines.append(_edge_report(
        "/posts без вікна (100 останніх)", recent, recent_err, clean, article_id,
        lambda p: match_source(p, clean, article_id) is not None,
    ))

    # 3-4. Відео-ребра: рілзи і звичайні відео-публікації
    reels, reels_err = _fetch_edge(
        "video_reels", "id,description,permalink_url,created_time", pages=2)
    lines.append(_edge_report(
        "/video_reels (200 останніх)", reels, reels_err, clean, article_id,
        lambda r: _matches_article(_text_of(r), clean, article_id),
    ))

    videos, videos_err = _fetch_edge(
        "videos", "id,description,title,permalink_url,created_time",
        since_ts, until_ts, pages=2)
    lines.append(_edge_report(
        "/videos у вікні", videos, videos_err, clean, article_id,
        lambda v: _matches_article(_text_of(v), clean, article_id),
    ))

    # 5. Прямий запит конкретного поста, якщо його id передали другим аргументом
    if obj_id:
        lines.append("")
        lines.append(f"<b>Прямий запит об'єкта {html.escape(obj_id)}</b>")
        lines.extend(_probe_object(obj_id, clean, article_id))

    return "\n".join(lines)


def _obj_id_from(arg):
    """id поста з аргументу: чисте число, page_post, або лінк .../posts/<id>/."""
    arg = arg.strip()
    if arg.isdigit() or ("_" in arg and arg.replace("_", "").isdigit()):
        return arg
    import re
    m = re.search(r"/(?:posts|videos|reel)/(\d+)", arg)
    if m:
        return m.group(1)
    nums = re.findall(r"\d{8,}", arg)
    return nums[-1] if nums else None


async def fbdebug_handler(update, context):
    if not context.args:
        await update.message.reply_text(
            "Використання: /fbdebug <url матеріалу> [id або лінк поста ФБ]\n\n"
            "Показує вікно пошуку і що саме віддав Graph API на кожному ребрі "
            "(/posts, /video_reels, /videos) — щоб не гадати, чому /stat не "
            "знайшов публікацію."
        )
        return

    article_url = context.args[0]
    if "nikvesti.com" not in article_url:
        await update.message.reply_text("Вкажіть посилання на матеріал nikvesti.com")
        return
    if not FACEBOOK_PAGE_TOKEN or not FACEBOOK_PAGE_ID:
        await update.message.reply_text("Не налаштовано FACEBOOK_PAGE_TOKEN / FACEBOOK_PAGE_ID")
        return

    obj_id = _obj_id_from(context.args[1]) if len(context.args) > 1 else None
    msg = await update.message.reply_text("⏳ Питаю Graph API…")
    try:
        text = await asyncio.to_thread(_diagnose, article_url, obj_id)
    except Exception as e:
        await msg.edit_text(f"Збій діагностики: {html.escape(str(e)[:300])}")
        return

    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
