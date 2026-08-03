"""
Тест: пошук поста у ФБ не залежить від важких полів листингу.

Інцидент 03.08.2026. Матеріал 321935 вийшов о 16:06, пост у ФБ — о 16:21, а о
19:20 монітор fb_missing сказав редакції «цієї новини досі немає у Facebook» і
дав чернетку поста, який уже три години висів у стрічці.

Заміряно на живому Graph API — те саме вікно, той самий токен, різниця тільки
в полях:
    id,message,created_time                       → 18 постів, наш є
    + attachments{...}                            → 18 постів, наш є
    + reactions.summary,comments.summary,shares   → 17 постів, НАШОГО НЕМАЄ
    повний набір бота                             → 17 постів, НАШОГО НЕМАЄ
Facebook мовчки викидає елемент зі списку, коли на нього просять важку
залученість: помилки немає, просто на один пост менше. Бот бачив 17 і чесно
доповідав «переглянув 17 постів» — рівно стільки, скільки віддав API.

Тому листинг тепер легкий, а реакції/коменти/шери тягнуться окремим запитом
для ЗНАЙДЕНОГО поста. Тест стереже обидві половини правила.

Запуск: python test_fb_listing.py (живу частину — з FACEBOOK_PAGE_TOKEN)
"""

import os

from handlers.stat import (
    POST_LIST_FIELDS, _clean_url, _parse_post_insights, _post_matches_article,
    get_fb_stats,
)
from handlers.helpers import extract_article_id

ARTICLE_URL = ("https://nikvesti.com/news/incidents/321935-u-mikolaievi-trivaye-"
               "likvidatsiia-naslidkiv-rosiiskoho-udaru-po-litseiu")

# Елемент листингу як його віддав Graph API 03.08.2026 (легкі поля)
REAL_POST = {
    "id": "301719373180657_1710649447730196",
    "message": (
        "У Миколаєві комунальні служби продовжують ліквідовувати наслідки "
        "російського удару по одному з навчальних закладів міста.\n\n"
        "На території школи прибирають уламки та закривають вибиті вікна, а "
        "міська влада вже готує звернення до міжнародних партнерів щодо допомоги "
        "у відновленні будівлі.\n\n"
        "За словами міського голови Миколаєва Олександра Сєнкевича, будівля ліцею "
        "зазнала значних пошкоджень: вибито вікна, а шкільний автобус повністю "
        "знищений\n\n🔗 " + ARTICLE_URL
    ),
    "permalink_url": "https://www.facebook.com/1653359633459178/posts/1710649447730196",
    "created_time": "2026-08-03T13:21:53+0000",
    "attachments": {"data": [{
        "media_type": "photo",
        "target": {"id": "1710649347730206",
                   "url": "https://www.facebook.com/photo.php?fbid=1710649347730206&set=a.483879077073912&type=3"},
        "url": "https://www.facebook.com/photo.php?fbid=1710649347730206&set=a.483879077073912&type=3",
        "unshimmed_url": "https://www.facebook.com/photo.php?fbid=1710649347730206&set=a.483879077073912&type=3",
    }]},
}

HEAVY = ("reactions", "comments.summary", "shares")

# Відповідь /insights того самого поста (Graph API, 03.08.2026). Поля об'єкта
# на ньому віддають (#100) «Object does not exist, cannot be loaded due to
# missing permission…» — тому метрики беруться звідси; в інтерфейсі під постом
# видно 15 реакцій, 1 коментар, 1 поширення
REAL_INSIGHTS = {"data": [
    {"name": "post_reactions_by_type_total", "period": "lifetime",
     "values": [{"value": {"like": 5, "sorry": 10}}]},
    {"name": "post_activity_by_action_type", "period": "lifetime",
     "values": [{"value": {"share": 1, "like": 15, "comment": 1}}]},
]}


def main():
    failed = 0
    article_id = extract_article_id(ARTICLE_URL)
    clean = _clean_url(ARTICLE_URL)

    # 1. Поля листингу не містять залученості — саме вона ховала пост
    for field in HEAVY:
        ok = field not in POST_LIST_FIELDS
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} у полях листингу немає «{field}»")

    # 2. Пост із листингу впізнається як публікація про матеріал
    ok = _post_matches_article(REAL_POST, clean, article_id)
    failed += 0 if ok else 1
    print(f"{'✅' if ok else '❌'} реальний пост 1710649447730196 збігається зі статтею {article_id}")

    # 3. Метрики з insights розбираються в ті самі числа, що видно під постом
    metrics = _parse_post_insights(REAL_INSIGHTS)
    for name, key, expected in (("реакції", "reactions", 15),
                                ("коментарі", "comments", 1),
                                ("шери", "shares", 1)):
        ok = metrics[key] == expected
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} {name} з insights: {metrics[key]} (мало бути {expected})")

    # 4. Друга лінія: відомий пост з індексу Нори підтверджується прямим
    #    запитом (з Нори береться ЛИШЕ id, живість каже сам Facebook)
    import handlers.stat as stat
    from handlers import stat_store

    saved_graph, saved_index = stat._graph, stat_store.load_index
    POST_ID = "301719373180657_1710649447730196"
    try:
        stat_store.load_index = lambda aid: {
            "facebook": {"items": [{"id": POST_ID, "type": "post"}],
                         "captured_at": None}}

        stat._graph = lambda path, params, timeout=15: ({"id": POST_ID}, None)
        got = stat.known_fb_post_alive(article_id)
        ok = got == POST_ID
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} відомий пост підтверджується: {got}")

        stat._graph = lambda path, params, timeout=15: (None, "(#100) Object does not exist")
        got = stat.known_fb_post_alive(article_id)
        ok = got is None
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} видалений пост не рятує від алерту: {got}")

        stat_store.load_index = lambda aid: {}
        stat._graph = lambda path, params, timeout=15: ({"id": POST_ID}, None)
        got = stat.known_fb_post_alive(article_id)
        ok = got is None
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} порожній індекс — нічого не вигадуємо: {got}")
    finally:
        stat._graph, stat_store.load_index = saved_graph, saved_index

    # 5. Знімок із Нори завжди підписаний часом — за свіжі дані не видається
    note = stat._nora_note([{"nora": "03.08 20:24", "id": POST_ID}])
    ok = note is not None and "03.08 20:24" in note
    failed += 0 if ok else 1
    print(f"{'✅' if ok else '❌'} знімок підписаний часом: {note}")

    # 6. Тижневий звіт: топ рахується по легкому листингу + поштучних метриках
    from handlers import facebook as fb

    saved_posts, saved_metrics = fb.get_page_posts, fb.get_post_metrics
    try:
        fb.get_page_posts = lambda since, until=None, max_pages=5: [
            {"id": "p1", "message": "Новина А https://nikvesti.com/news/1-a",
             "permalink_url": "https://www.facebook.com/1/posts/1"},
            {"id": "p2", "message": "Новина Б https://nikvesti.com/news/2-b",
             "permalink_url": "https://www.facebook.com/1/posts/2"},
            {"id": "p3", "message": "Без лінка на сайт",
             "permalink_url": "https://www.facebook.com/1/posts/3"},
        ]
        by_id = {
            "p1": {"views": 10, "reactions": 3, "comments": 1, "shares": 0, "note": None},
            "p2": {"views": 20, "reactions": 30, "comments": 2, "shares": 5, "note": None},
        }
        fb.get_post_metrics = lambda pid: by_id[pid]
        top, total = fb.get_top_posts(since_ts=0)

        ok = total == 2
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} пост без лінка на сайт у звіт не рахується: {total}")

        ok = [p["id"] for p in top] == ["p2", "p1"]
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} топ за залученістю: {[p['id'] for p in top]}")

        # метрики не зійшлись — звіт має вижити, а не впасти на None
        by_id["p2"] = {"views": None, "reactions": None, "comments": None,
                       "shares": None, "note": "(#100) Object does not exist"}
        top, _ = fb.get_top_posts(since_ts=0)
        ok = top and top[0]["id"] == "p1" and top[1]["reactions"] is None
        failed += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} пост без метрик не валить звіт і не вдає нулі")
    finally:
        fb.get_page_posts, fb.get_post_metrics = saved_posts, saved_metrics

    if os.environ.get("FACEBOOK_PAGE_TOKEN"):
        print("\n=== живий прогін get_fb_stats ===")
        items, scanned, error = get_fb_stats(ARTICLE_URL, article_id)
        print(f"переглянуто постів: {scanned}, помилка: {error}")
        for it in items:
            print(f"  {it['type']} {it['id']} від {it['date']} — "
                  f"❤️{it['reactions']} 💬{it['comments']} 🔄{it['shares']}")
        if not any(it["id"].endswith("1710649447730196") for it in items):
            print("  ❌ пост про 321935 так і не знайшовся")
            failed += 1
    else:
        print("\n(живий прогін пропущено — немає FACEBOOK_PAGE_TOKEN)")

    print(f"\nПідсумок: {'усе пройшло' if not failed else str(failed) + ' провалів'}")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
