"""
Пошук відео YouTube про матеріал nikvesti.com для /stat — той самий
семантичний підхід, що для Instagram/TikTok (handlers/stat_instagram.py).

URL статті у відео на YouTube немає, тож зіставляємо ПО СМИСЛУ: сигнатура статті
(заголовок + лід) проти тексту відео (title + description). Уся механіка
(нормалізація, скоринг max(coverage, bigram-Jaccard), сигнатура статті,
Claude-суддя в сірій зоні) переиспользується з stat_instagram — тут лише джерело
кандидатів (YouTube Data API, get_videos_in_window) і пакування метрик.

Метрики відео (Data API videos.list statistics): перегляди/лайки/коментарі.
Поширень і охоплення на рівні відео Data API не дає. Потрібен OAuth
(youtube_analytics.is_configured) — без нього блок YouTube у /stat не показуємо.

Застереження: опис відео на YT часто містить багато сталого тексту (лінки на
соцмережі, підписка) — але coverage рахує частку слів СТАТТІ в описі, тож
сталий текст збіг не роздуває; впливає лише реальний перетин.
"""

import asyncio
from datetime import datetime, timedelta

from handlers import youtube_analytics
from handlers.stat_instagram import (
    _norm_tokens, _score, get_article_signature, _judge,
    ACCEPT, NEAR, JUDGE_MIN, JUDGE_TOPK, MAX_MATCHES, FORWARD_DAYS,
)


def _fmt_date(published_at):
    """YouTube publishedAt ('2026-07-16T15:30:00Z', UTC) → 'DD.MM.YYYY HH:MM'
    за Києвом (UTC+3)."""
    try:
        dt = datetime.strptime(str(published_at)[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=3)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(published_at)[:10]


def _caption(video):
    return " ".join(x for x in (video.get("title"), video.get("description")) if x)


def _pack(video, method):
    return {
        "id": str(video.get("id") or ""),  # ключ швидкого шляху /stat (article_stats)
        "permalink": video.get("url", ""),
        "date": _fmt_date(video.get("published_at")),
        "views": video.get("views"),
        "likes": video.get("likes"),
        "comments": video.get("comments"),
        "method": method,  # 'lexical' | 'ai' — як знайшли (для діагностики)
    }


async def get_youtube_stat_by_ids(stored_items):
    """Швидкий шлях /stat: video_id відомі з індексу (article_stats) — минаємо
    гортання плейлиста і матчинг, одразу videos.list по id. Кидає виняток при
    порожній відповіді — виклик фолбекне на снімок з Нори."""
    ids = [it.get("id") for it in stored_items if it.get("id")]
    videos = await asyncio.to_thread(youtube_analytics.get_videos_by_ids, ids)
    if not videos:
        raise RuntimeError("videos.list нічого не повернув")
    return [_pack(v, "index") for v in videos]


async def get_youtube_stat(article_url, pub_date=None, sig=None):
    """Знаходить відео YouTube про матеріал і збирає метрики. Повертає:
    - None, якщо YouTube OAuth не налаштовано (тоді блок YouTube у /stat ховаємо);
    - [] — налаштовано, але збігу немає;
    - list[dict] — знайдені відео (як get_instagram_stat).
    sig — готова сигнатура статті {title,lead}; None → тягнемо сторінку самі."""
    if not youtube_analytics.is_configured():
        return None

    if sig is None:
        sig = await asyncio.to_thread(get_article_signature, article_url)
    if not sig:
        return []
    sig_tokens = _norm_tokens(f"{sig.get('title', '')} {sig.get('lead', '')}")
    if not sig_tokens:
        return []

    now = datetime.now()
    if pub_date:
        since_dt = pub_date.replace(tzinfo=None) - timedelta(days=1)
        until_dt = min(pub_date.replace(tzinfo=None) + timedelta(days=FORWARD_DAYS), now)
    else:
        until_dt = now
        since_dt = until_dt - timedelta(days=14)

    try:
        videos = await asyncio.to_thread(
            youtube_analytics.get_videos_in_window,
            int(since_dt.timestamp()), int(until_dt.timestamp()),
        )
    except Exception as e:
        # Помилка API ≠ «відео немає»: ховаємо блок (None), а не показуємо
        # оманливе «не знайдено» (напр. 403 по scope токена)
        print(f"stat_youtube: помилка списку відео — {e}")
        return None
    if not videos:
        return []

    scored = sorted(
        ((v, _score(sig_tokens, _caption(v))) for v in videos),
        key=lambda x: x[1], reverse=True,
    )
    best_s = scored[0][1]

    if best_s >= ACCEPT:
        strong = [v for v, s in scored if s >= ACCEPT and s >= best_s - NEAR]
        chosen = [(v, "lexical") for v in strong[:MAX_MATCHES]]
    elif best_s >= JUDGE_MIN:
        top = scored[:JUDGE_TOPK]
        cands = [({"caption": _caption(v)}, s) for v, s in top]
        idx = await _judge(sig, cands, platform="YouTube")
        chosen = [(top[idx][0], "ai")] if idx is not None else []
    else:
        chosen = []

    return [_pack(v, method) for v, method in chosen]
