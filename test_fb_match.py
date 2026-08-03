"""
Тест матчингу публікації ФБ до статті (handlers/stat.match_source).

Фактура — реальний випадок 03.08.2026: матеріал 321935 («У Миколаєві триває
ліквідація наслідків російського удару по ліцею») пішов у ФБ ВІДЕО-постом
(og:type=video.other, той самий ролик, що в Telegram), і /stat його не знайшов,
хоча переглянув 14 постів вікна. У відео-поста Graph API кладе підпис не в
message, а в description вкладення — стара перевірка (message / story / URL
вкладення) до нього не діставала.

Офлайн-частина ганяє матчер на формах об'єкта, які віддає Graph API.
З FACEBOOK_PAGE_TOKEN — ще й живий прогін /stat по тому самому матеріалу.

Запуск: python test_fb_match.py (живу частину — в консолі Railway)
"""

import os

from handlers.stat import _clean_url, match_source, get_fb_stats

ARTICLE_URL = ("https://nikvesti.com/news/incidents/321935-u-mikolaievi-trivaye-"
               "likvidatsiia-naslidkiv-rosiiskoho-udaru-po-litseiu")
ARTICLE_ID = "321935"
CLEAN = _clean_url(ARTICLE_URL)

# Реальний підпис поста МикВісті (facebook.com/share/197akWTcEC)
CAPTION = (
    "У Миколаєві комунальні служби продовжують ліквідовувати наслідки російського "
    "удару по одному з навчальних закладів міста.\n\n"
    "На території школи прибирають уламки та закривають вибиті вікна, а міська "
    "влада вже готує звернення до міжнародних партнерів щодо допомоги у "
    "відновленні будівлі.\n\n"
    f"🔗 {ARTICLE_URL}"
)

CASES = [
    (
        "відео-пост: підпис лише в описі вкладення, message порожній",
        {
            "id": "301719373180657_1710649447730196",
            "created_time": "2026-08-03T12:53:00+0000",
            "attachments": {"data": [{
                "media_type": "video",
                "description": CAPTION,
                "target": {"id": "1760654562049169",
                           "url": "https://www.facebook.com/nikvesti/videos/1760654562049169/"},
                "url": "https://www.facebook.com/nikvesti/videos/1760654562049169/",
            }]},
        },
        "attachments.description",
    ),
    (
        "звичайний пост: лінк у message",
        {"id": "1_2", "message": CAPTION},
        "message",
    ),
    (
        "лінк лише у вкладенні, ще й shimmed",
        {"id": "1_3", "message": "Читайте на сайті", "attachments": {"data": [{
            "media_type": "share",
            "url": "https://l.facebook.com/l.php?u=" + ARTICLE_URL.replace(":", "%3A").replace("/", "%2F"),
        }]}},
        "attachments.url",
    ),
    (
        "чужий матеріал із схожим ID (1321935) — не наш",
        {"id": "1_4", "message": "https://nikvesti.com/news/incidents/1321935-inshyi-material"},
        None,
    ),
    (
        "пост без тексту і без лінка",
        {"id": "1_5", "attachments": {"data": [{"media_type": "photo",
                                                "url": "https://www.facebook.com/photo/?fbid=1"}]}},
        None,
    ),
]


def main():
    failed = 0
    print("=== матчинг поста до статті ===")
    for name, post, expected in CASES:
        got = match_source(post, CLEAN, ARTICLE_ID)
        ok = got == expected
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {name}\n     очікували {expected!r}, отримали {got!r}")

    if os.environ.get("FACEBOOK_PAGE_TOKEN"):
        print("\n=== живий прогін get_fb_stats ===")
        items, scanned, error = get_fb_stats(ARTICLE_URL, ARTICLE_ID)
        print(f"переглянуто постів: {scanned}, помилка: {error}")
        for it in items:
            print(f"  {it['type']} {it['id']} від {it['date']} — {it['permalink']}")
        if not items:
            print("  ❌ публікацій не знайдено — далі /fbdebug по цьому ж URL")
            failed += 1
    else:
        print("\n(живий прогін пропущено — немає FACEBOOK_PAGE_TOKEN)")

    print(f"\nПідсумок: {'усе пройшло' if not failed else str(failed) + ' провалів'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
