"""
Пошук відео TikTok про матеріал nikvesti.com для /stat — той самий семантичний
підхід, що для Instagram (handlers/stat_instagram.py).

TikTok у нас — дзеркало Instagram: у відео той самий контент і той самий підпис,
що в допису інсти, а він майже дослівно повторює лід статті. URL статті у TikTok,
як і в інсті, немає — тому зіставляємо ПО СМИСЛУ: сигнатура статті (заголовок +
лід) проти опису відео (video_description + title). Уся механіка (нормалізація,
скоринг max(coverage, bigram-Jaccard), пороги, Claude-суддя в сірій зоні,
сигнатура статті) переиспользується з stat_instagram — тут лише джерело
кандидатів (TikTok Display API) і пакування метрик відео.

Метрики відео (Display API /v2/video/list/): перегляди/лайки/коментарі/поширення.
Охоплення й збереження TikTok в API не віддає (лише перегляди). Потрібен OAuth
(tiktok_analytics.is_configured) — без нього блок TikTok у /stat не показуємо.
"""

import asyncio
from datetime import datetime, timedelta

from handlers import tiktok_analytics
from handlers.stat_instagram import (
    _norm_tokens, _score, get_article_signature, _judge,
    ACCEPT, NEAR, JUDGE_MIN, JUDGE_TOPK, MAX_MATCHES, FORWARD_DAYS,
)


def _fmt_date(create_time):
    """TikTok create_time (unix) → 'DD.MM.YYYY HH:MM' (Київ = сервер+3, як у /stat)."""
    try:
        dt = datetime.fromtimestamp(int(create_time)) + timedelta(hours=3)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def _caption(video):
    """Текст для зіставлення: опис відео + заголовок (обидва можуть бути)."""
    return " ".join(x for x in (video.get("video_description"), video.get("title")) if x)


def _pack(video, method):
    return {
        "id": str(video.get("id") or ""),  # ключ швидкого шляху /stat (article_stats)
        "permalink": video.get("share_url", ""),
        "date": _fmt_date(video.get("create_time")),
        "views": video.get("view_count"),
        "likes": video.get("like_count"),
        "comments": video.get("comment_count"),
        "shares": video.get("share_count"),
        "method": method,  # 'lexical' | 'ai' — як знайшли (для діагностики)
    }


async def get_tiktok_stat_by_ids(stored_items):
    """Швидкий шлях /stat: video_id відомі з індексу (article_stats) — минаємо
    листинг і матчинг, одразу тягнемо свіжі метрики (/v2/video/query/). Кидає
    виняток при порожній відповіді — виклик фолбекне на снімок з Нори."""
    ids = [it.get("id") for it in stored_items if it.get("id")]
    videos = await asyncio.to_thread(tiktok_analytics.get_videos_by_ids, ids)
    if not videos:
        raise RuntimeError("video/query нічого не повернув")
    return [_pack(v, "index") for v in videos]


async def get_tiktok_stat(article_url, pub_date=None, sig=None):
    """Знаходить відео TikTok про матеріал і збирає метрики. Повертає:
    - None, якщо TikTok OAuth не налаштовано (тоді блок TikTok у /stat ховаємо);
    - [] — налаштовано, але збігу немає;
    - list[dict] — знайдені відео (як get_instagram_stat).
    sig — готова сигнатура статті {title,lead}; None → тягнемо сторінку самі."""
    if not tiktok_analytics.is_configured():
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
            tiktok_analytics.get_videos_in_window,
            int(since_dt.timestamp()), int(until_dt.timestamp()),
        )
    except Exception as e:
        # Помилка API ≠ «відео немає»: ховаємо блок (None), а не показуємо
        # оманливе «не знайдено»
        print(f"stat_tiktok: помилка списку відео — {e}")
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
        # _judge читає підпис із ключа 'caption' — даємо синтетичний, паралельний top
        cands = [({"caption": _caption(v)}, s) for v, s in top]
        idx = await _judge(sig, cands, platform="TikTok")
        chosen = [(top[idx][0], "ai")] if idx is not None else []
    else:
        chosen = []

    return [_pack(v, method) for v, method in chosen]
