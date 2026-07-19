"""
Снімки /stat у Нору (article_stats, bot_db) — «останній стан» метрик матеріалу
по каналах + object_id знайдених постів/відео.

Дві ролі (узгоджено з Олегом 19.07.2026):
1. ШВИДКИЙ ШЛЯХ: object_id з індексу дозволяє повторному /stat минути весь
   пошук (вікна дат, листинги, семантичний скоринг, суддю) і одразу тягнути
   свіжі метрики конкретних об'єктів — перший /stat ~8-9с, повторні ~2-3с.
2. ФОЛБЕК «з Нори»: коли живе джерело впало (ліміт FB, збій API) — показуємо
   останній збережений снімок із поміткою дати.

Семантика збереження — upsert «останній стан», БЕЗ історії (рішення Олега).
Порожній свіжий результат канал НЕ затирає (збій пошуку не має стирати робочий
індекс). Модуль тихий: без BOT_DATABASE_URL нічого не робить, /stat працює
як раніше.
"""

import json

from handlers import bot_db

# Канали снімка. site — GA4 по мовах; telegram — url+views; решта — списки
# знайдених об'єктів з їхніми object_id.
SOCIAL_CHANNELS = ("facebook", "instagram", "tiktok", "youtube")


def load_index(article_id):
    """Останній снімок матеріалу: {channel: {"items": [item,...],
    "captured_at": datetime}}. {} — якщо Нора не налаштована/порожньо/збій."""
    if not bot_db.is_configured():
        return {}
    try:
        rows = bot_db.get_article_stats(int(article_id))
    except Exception as e:
        print(f"stat_store: не вдалось прочитати індекс — {e}")
        return {}
    out = {}
    for r in rows:
        item = r.get("item")
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if not isinstance(item, dict):
            continue
        entry = out.setdefault(r["channel"], {"items": [], "captured_at": r.get("captured_at")})
        entry["items"].append(item)
    return out


def save_snapshot(article_id, per_channel):
    """Зберігає снімок каналів (лише СВІЖІ дані — фолбеки з Нори сюди не
    передавати). per_channel: {channel: list[item] | dict}; dict загортається
    в один рядок (site: by_lang, telegram: url+views). object_id — item['id']
    (де є). Помилки ковтаються по каналу — снімок не має ламати /stat."""
    if not bot_db.is_configured():
        return
    for channel, items in per_channel.items():
        if not items:
            continue
        if isinstance(items, dict):
            items = [items]
        rows = []
        for it in items:
            oid = str(it.get("id") or "") if isinstance(it, dict) else ""
            rows.append((oid, json.dumps(it, ensure_ascii=False, default=str)))
        try:
            bot_db.replace_channel_stats(int(article_id), channel, rows)
        except Exception as e:
            print(f"stat_store: не вдалось зберегти снімок {channel} — {e}")


def mark_nora(entry):
    """Items снімка з поміткою «з Нори» для фолбек-показу. entry — елемент
    load_index ({"items", "captured_at"})."""
    try:
        stamp = entry["captured_at"].strftime("%d.%m %H:%M")
    except Exception:
        stamp = "?"
    return [{**it, "nora": stamp} for it in entry["items"]]
