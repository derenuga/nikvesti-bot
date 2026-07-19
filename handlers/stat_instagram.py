"""
Пошук допису Instagram про матеріал nikvesti.com для /stat.

На відміну від Facebook/Telegram, у стрічці Instagram НЕМАЄ посилання на статтю
(діляться візуалом, а не URL), тому зіставити пост із матеріалом по URL нічим.
Натомість підпис допису в інсті — це майже дослівно лід статті (перевірено на
живих парах: підпис збігається з <meta name="description"> сторінки). Тому
зіставляємо ПО СМИСЛУ: сигнатура статті (заголовок + лід) проти підписів
дописів у вузькому вікні дат.

Схема (двоступенева, дешева):
  1. Кандидати за датою — дописи інсти у вікні [дата−1, дата+FORWARD_DAYS].
     Інста постить у той самий день, вікно вузьке → кілька-десяток кандидатів.
  2. Лексична схожість підпису до сигнатури: coverage (частка значущих слів
     статті, наявних у підписі) — головний сигнал, бо підпис переписує лід;
     плюс bigram-Jaccard на близькість формулювань. Беремо максимум.
       - score ≥ ACCEPT → впевнений збіг без AI (кілька збігів = пост+рілз);
       - ACCEPT > score ≥ JUDGE_MIN → «сіра зона», віддаємо топ-кандидатів
         Claude-судді (Haiku) — стійко до сильного перепису/зміни мови;
       - score < JUDGE_MIN → допису немає.

Пороги відкалібровані на реальних парах: вірні збіги 0.76/0.79, будь-який чужий
підпис ≤0.18 — величезний зазор, тож ACCEPT свідомо високий (0.5), а не «ловимо
все». Метрики знайденого допису (перегляди/охоплення) — get_media_insights.
"""

import asyncio
import re
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from handlers import instagram

# Вікно пошуку вперед від дати публікації (інсту постять у ці дні)
FORWARD_DAYS = 5
# Пороги схожості (калібровані на живих парах, див. док-стрінг)
ACCEPT = 0.50      # ≥ — впевнений збіг без AI
NEAR = 0.10        # інші кандидати в межах ACCEPT..(best−NEAR) теж рахуємо збігом (пост+рілз)
JUDGE_MIN = 0.28   # сіра зона [JUDGE_MIN, ACCEPT) → Claude-суддя
JUDGE_TOPK = 4     # скільки топ-кандидатів показуємо судді
MAX_MATCHES = 3    # запобіжник на кількість показаних дописів

_WORD_RE = re.compile(r"[0-9a-zA-Zа-яА-ЯіїєґІЇЄҐ']+", re.UNICODE)
# Короткі службові слова (укр/рос) — шумлять збіг, прибираємо
_STOP = set("""
та і й у в з за на до від про що як але щоб чи не ні так теж також для по при
без через між над під це цей ця ці той те або чи є бути був була було
""".split())


def _norm_tokens(text):
    if not text:
        return []
    text = text.lower().replace("ё", "е")
    return [t for t in _WORD_RE.findall(text) if len(t) >= 3 and t not in _STOP]


def _score(sig_tokens, caption):
    """Схожість сигнатури статті (передобчислені токени) до підпису допису.
    Три сигнали, беремо максимум:
    - coverage: частка значущих слів СТАТТІ, що є в підписі — головний для
      довгих підписів (інста переписує лід статті);
    - bigram-Jaccard: близькість формулювань;
    - reverse coverage: частка слів ПІДПИСУ, що є в статті — для КОРОТКИХ
      підписів (заголовок YouTube-шортса — витяжка з заголовка статті: пряма
      coverage структурно низька, бо 7 слів не покриють 25-слівну сигнатуру,
      а reverse = 1.0). Дисконт len/12 і поріг ≥5 токенів — щоб коротка
      генерик-підпись («новини Миколаєва сьогодні») не матчила все підряд:
      5-6 слів навіть із повним збігом потрапляють лише в сіру зону (суддя),
      авто-ACCEPT — від ~7 повністю «статейних» слів."""
    ct = _norm_tokens(caption)
    if not sig_tokens or not ct:
        return 0.0
    st_set, ct_set = set(sig_tokens), set(ct)
    inter = len(st_set & ct_set)
    coverage = inter / len(st_set)
    bg_s = set(zip(sig_tokens, sig_tokens[1:]))
    bg_c = set(zip(ct, ct[1:]))
    jacc = len(bg_s & bg_c) / len(bg_s | bg_c) if (bg_s or bg_c) else 0.0
    score = max(coverage, jacc)
    if len(ct_set) >= 5:
        reverse = inter / len(ct_set)
        score = max(score, reverse * min(1.0, len(ct_set) / 12))
    return score


# ---------- Сигнатура статті ----------

def get_article_signature(article_url):
    """Заголовок + лід статті зі сторінки. Лід — <meta name="description">
    (= перше речення матеріалу, саме його інста бере у підпис). Заголовок —
    og:title / <title> / <h1>. None, якщо сторінку не вдалося прочитати."""
    try:
        resp = requests.get(
            article_url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NikVesti-Bot/1.0)"},
        )
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
        elif soup.title and soup.title.string:
            title = soup.title.string.strip()
        elif soup.find("h1"):
            title = soup.find("h1").get_text(strip=True)

        lead = ""
        desc = soup.find("meta", attrs={"name": "description"})
        if not desc:
            desc = soup.find("meta", property="og:description")
        if desc and desc.get("content"):
            lead = desc["content"].strip()

        if not title and not lead:
            return None
        return {"title": title, "lead": lead}
    except Exception as e:
        print(f"stat_instagram: не вдалося зчитати сигнатуру — {e}")
        return None


# ---------- Claude-суддя (сіра зона) ----------

async def _judge(sig, candidates, platform="Instagram"):
    """Повертає індекс допису-збігу в candidates або None. Викликаємо лише в
    сірій зоні лексики. Модель — FAST (Haiku), облік вартості — у fox_generate.
    platform — назва мережі в промпті (Instagram/TikTok); кандидати завжди
    несуть підпис у ключі 'caption'."""
    from handlers.ai_messages import fox_generate

    lines = []
    for i, (media, _s) in enumerate(candidates, 1):
        cap = (media.get("caption") or "").replace("\n", " ").strip()[:300]
        lines.append(f"{i}. {cap}")
    listed = "\n".join(lines)
    prompt = (
        f"Ти зіставляєш новину сайту з дописом у {platform}.\n\n"
        f"Новина:\nЗаголовок: {sig.get('title', '')}\nЛід: {sig.get('lead', '')}\n\n"
        f"Підписи дописів {platform}:\n{listed}\n\n"
        "Який допис розповідає про ЦЮ САМУ новину? Відповідай ЛИШЕ числом — "
        "номером допису зі списку. Якщо жоден не про цю новину — відповідай 0. "
        "Тільки число, без пояснень."
    )
    try:
        raw = await fox_generate(prompt, system=None, max_tokens=5)
        m = re.search(r"\d+", raw or "")
        if not m:
            return None
        idx = int(m.group()) - 1
        return idx if 0 <= idx < len(candidates) else None
    except Exception as e:
        print(f"stat_instagram: суддя не спрацював — {e}")
        return None


# ---------- Оркестрація ----------

def _fmt_date(ts):
    """Instagram timestamp '2026-07-16T18:20:00+0000' → 'DD.MM.YYYY HH:MM' (Київ)."""
    try:
        dt = datetime.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S")
        return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return (ts or "")[:10]


def _pack(media, method):
    ins = instagram.get_media_insights(media.get("id"), media.get("media_type"))
    return {
        "permalink": media.get("permalink", ""),
        "date": _fmt_date(media.get("timestamp")),
        "media_type": media.get("media_type"),
        "views": ins.get("views"),
        "reach": ins.get("reach"),
        "likes": media.get("like_count", 0),
        "comments": media.get("comments_count", 0),
        # Поширення (✈️) і збереження (🔖) — лише з insights (у полях медіа їх немає)
        "shares": ins.get("shares"),
        "saved": ins.get("saved"),
        "method": method,  # 'lexical' | 'ai' — як знайшли (для діагностики)
    }


async def get_instagram_stat(article_url, pub_date=None, sig=None):
    """Знаходить допис(и) Instagram про матеріал і збирає їхні метрики.
    Повертає list[dict] (як get_fb_stats — може бути кілька: пост + рілз) або
    порожній список, якщо збігу немає / інста не налаштована. sig — готова
    сигнатура статті {title,lead}; якщо None, тягнемо сторінку самі (щоб /stat
    не фетчив сторінку кілька разів, він передає спільну сигнатуру)."""
    if not instagram.INSTAGRAM_TOKEN:
        return []

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

    media = await asyncio.to_thread(
        instagram.get_media_in_window, int(since_dt.timestamp()), int(until_dt.timestamp())
    )
    if not media:
        return []

    scored = sorted(
        ((m, _score(sig_tokens, m.get("caption"))) for m in media),
        key=lambda x: x[1], reverse=True,
    )
    best_s = scored[0][1]

    # Впевнена зона: усі дописи з високим збігом близько до топа (пост+рілз
    # однієї новини мають однаковий підпис → однаковий score)
    if best_s >= ACCEPT:
        strong = [m for m, s in scored if s >= ACCEPT and s >= best_s - NEAR]
        chosen = [(m, "lexical") for m in strong[:MAX_MATCHES]]
    # Сіра зона: лексика вагається — питаємо суддю по топ-K
    elif best_s >= JUDGE_MIN:
        top = scored[:JUDGE_TOPK]
        idx = await _judge(sig, top)
        chosen = [(top[idx][0], "ai")] if idx is not None else []
    else:
        chosen = []

    # Метрики знайдених дописів (мережеві виклики) — окремим потоком
    return await asyncio.to_thread(
        lambda: [_pack(m, method) for m, method in chosen]
    )
