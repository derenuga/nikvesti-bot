"""
Тест /post — статистики ОДНОГО поста за лінком на сам пост
(handlers/post_stat.py).

**Навіщо модуль існує.** Олег дав чотири лінки (21.08.2026) і спитав, чи
можна по них дістати цифри. Дві з чотирьох форм id об'єкта не містять узагалі:
`facebook.com/share/p/1AmpyiGUfk/` і `facebook.com/nikvesti/posts/pfbid0qbLE…`.
Graph API таких ідентифікаторів не приймає, а pfbid не розкодовується — це
непрозорий токен, а не кодування числа. Тому модуль спершу ВПІЗНАЄ пост, і
цей крок — усе, що в ньому може зламатись.

**Що перевіряємо (кожен пункт — пастка, яку я вже перевірив на живих даних):**

- усі чотири реальні лінки Олега розбираються в те, чим вони є, і жоден із
  них не приймається за лінк матеріалу (інакше /stat перехопив би їх собі);
- og-теги читаються з РЕАЛЬНОЇ відповіді Facebook — а вона приходить із
  HTTP 400 і кирилицею в HTML-ентитях (`&#x41c;&#x438;…`). Не розекранувати
  її означає порівнювати текст поста з нечитабельним рядком, тобто не знайти
  ніколи;
- ГОЛОВНА ПАСТКА: число `1509803480261504` лежить у КОЖНІЙ сторінці pfbid
  однаково — на двох різних постах воно те саме. Спокуса «взяти найдовше
  число зі сторінки» дала б упевнений на вигляд, але завжди той самий і
  завжди чужий id. Перевіряємо, що воно не потрапляє в кандидати з og:image;
- зіставлення тексту знаходить ПОТРІБНИЙ пост і не бере сусідній: обидві
  фікстури — справжні пости нашої сторінки, і опис одного не має матчити
  інший;
- сторінка без og (Facebook віддає таку і на неіснуючий pfbid, і коли
  притримує запити) не дає ні збігу, ні вигаданих кандидатів;
- чужа сторінка відсікається за ніком у лінку — до будь-яких запитів;
- вивід екранує HTML: у текст поста людина може написати «<» або «&», а
  зламана розмітка гасить ВСЕ повідомлення (урок tg_html, 12.08).

Мережі й токенів не треба: сторінки лежать у data/fb_post_*.html.gz — це
справжні відповіді Facebook нашому серверу, цілком. Цілком, а не шапкою,
свідомо: спільне число 1509803480261504 лежить у ТІЛІ сторінки, і обрізана
фікстура тихо перестала б доводити головну пастку модуля.
"""

import gzip
import pathlib
import sys

from handlers import post_stat

FAILS = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def fixture(name):
    path = pathlib.Path(__file__).parent / "data" / f"{name}.html.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


# Лінки Олега, дослівно з його повідомлення
SHARE_1 = "https://www.facebook.com/share/p/1AmpyiGUfk/"
SHARE_2 = "https://www.facebook.com/share/p/1HeJY9LrQ3/"
PFBID = ("https://www.facebook.com/nikvesti/posts/pfbid0qbLEp5sC5CKYw4CKBD5M9SE"
         "nsA5Hzhcr886yUf3CZy8wzpoB2jqo1jtdg8x1GzHRl")
IG = "https://www.instagram.com/nikvesti/p/DcEW0bgDE2U/"

print("1. Чотири реальні лінки Олега")
for url, key in ((SHARE_1, "shortlink"), (SHARE_2, "shortlink"), (PFBID, "pfbid")):
    link = post_stat.parse_link(url)
    check(f"{url[:46]}… → {key}", bool(link) and bool(link.get(key)), repr(link))
ig = post_stat.parse_link(IG)
check("інста → shortcode DcEW0bgDE2U", (ig or {}).get("shortcode") == "DcEW0bgDE2U", repr(ig))
check("нік сторінки з інста-лінка", (ig or {}).get("owner") == "nikvesti", repr(ig))

print("\n2. Інші форми лінка, які ходять по чатах")
cases = {
    "https://www.facebook.com/nikvesti/posts/1234567890123": ("object_id", "1234567890123"),
    "https://www.facebook.com/reel/998877665544": ("object_id", "998877665544"),
    "https://www.facebook.com/watch/?v=123456789012": ("object_id", "123456789012"),
    "https://www.instagram.com/reel/DcEW0bgDE2U/": ("shortcode", "DcEW0bgDE2U"),
    "https://fb.watch/abc123/": ("shortlink", True),
}
for url, (key, want) in cases.items():
    link = post_stat.parse_link(url) or {}
    check(f"{url[:44]}…", link.get(key) == want, repr(link))
perm = post_stat.parse_link(
    "https://www.facebook.com/permalink.php?story_fbid=987654321&id=301719373180657") or {}
check("permalink.php складає {сторінка}_{пост}",
      perm.get("object_id") == "301719373180657_987654321", repr(perm))

print("\n3. Лінк матеріалу /post НЕ забирає (інакше /stat лишився б без роботи)")
for url in ("https://nikvesti.com/news/politics/322371-slug",
            "https://nikvesti.com/ru/news/111079", "просто текст без лінка"):
    check(f"не пост: {url[:40]}", not post_stat.is_post_link(url))

print("\n4. og з РЕАЛЬНОЇ відповіді Facebook (вона приходить із HTTP 400)")
og_a = post_stat.parse_og(fixture("fb_post_pfbid"))
og_b = post_stat.parse_og(fixture("fb_post_share"))
check("текст поста прочитано", len(og_a.get("description", "")) > 80, repr(og_a)[:200])
check("кирилиця розекранована (не &#x41c;)",
      "МикВісті" in og_a.get("title", ""), repr(og_a.get("title")))
check("це справді про тендер на гуртожиток",
      "гуртожит" in og_a.get("description", ""), og_a.get("description", "")[:80])
check("друга сторінка — інший пост",
      og_b.get("description") and og_b["description"] != og_a["description"])
check("og:image є в обох", bool(og_a.get("image")) and bool(og_b.get("image")))

print("\n5. Пастка спільного числа: 1509803480261504 лежить в обох сторінках")
raw_a, raw_b = fixture("fb_post_pfbid"), fixture("fb_post_share")
check("число справді в обох (тому воно й пастка)",
      "1509803480261504" in raw_a and "1509803480261504" in raw_b)
cand_a = post_stat.image_object_ids(og_a.get("image"))
cand_b = post_stat.image_object_ids(og_b.get("image"))
check("у кандидати з og:image воно не потрапило",
      "1509803480261504" not in cand_a and "1509803480261504" not in cand_b,
      f"{cand_a} / {cand_b}")
check("кандидати в постів РІЗНІ", bool(cand_a) and bool(cand_b) and set(cand_a) != set(cand_b),
      f"{cand_a} / {cand_b}")

print("\n6. Впізнавання поста у стрічці — на справжніх текстах")
# «Стрічка» зібрана з двох справжніх постів (їхні тексти — це те, що Facebook
# сам віддав у og) плюс сусід про іншу подію.
feed = [[
    {"id": "301719373180657_111", "message": "Зовсім інша новина про ремонт дороги "
                                             "до Матвіївки", "attachments": {}},
    {"id": "301719373180657_222", "message": og_b["description"], "attachments": {}},
    {"id": "301719373180657_333", "message": og_a["description"] + " Читайте: "
                                             "https://nikvesti.com/news/322371-x",
     "attachments": {}},
]]
post, method, scanned = post_stat.find_in_feed(og_a, feed)
check("знайшов САМЕ свій пост", (post or {}).get("id") == "301719373180657_333",
      repr((post or {}).get("id")))
check("спосіб названо текстом", method == "text", repr(method))
check("перебрав усі три", scanned == 3, str(scanned))
post_b, _, _ = post_stat.find_in_feed(og_b, feed)
check("другий опис веде до другого поста",
      (post_b or {}).get("id") == "301719373180657_222", repr((post_b or {}).get("id")))

print("\n7. Фото поста — сигнал сильніший за текст (збіг точний, без порогів)")
photo_feed = [[
    {"id": "301719373180657_777", "message": "текст, який ні з чим не збігається",
     "attachments": {"data": [{"media_type": "photo",
                               "target": {"id": cand_a[0]}}]}},
]]
post_p, method_p, _ = post_stat.find_in_feed(og_a, photo_feed)
check("знайшов за id фото", (post_p or {}).get("id") == "301719373180657_777")
check("спосіб названо фото", method_p == "photo", repr(method_p))

print("\n8. Сторінка без og — ні збігу, ні вигаданих кандидатів")
og_none = post_stat.parse_og(fixture("fb_post_missing"))
check("опису немає", not og_none.get("description"), repr(og_none)[:160])
none_post, none_method, _ = post_stat.find_in_feed(og_none, feed)
check("у стрічці нічого не «впізналось»", none_post is None and none_method is None)
check("порожній og:image не дає кандидатів", post_stat.image_object_ids(og_none.get("image")) == [])

print("\n9. Чужа сторінка відсікається до будь-яких запитів")
check("чужий нік видно", post_stat.foreign_owner(
    post_stat.parse_link("https://www.facebook.com/newspn/posts/pfbid0aaa")) == "newspn")
check("наш нік — не чужий", post_stat.foreign_owner(post_stat.parse_link(PFBID)) is None)
check("ніка немає — не чужий", post_stat.foreign_owner(post_stat.parse_link(SHARE_1)) is None)
check("чужий лінк одразу віддає причину",
      post_stat.collect("https://www.facebook.com/newspn/posts/pfbid0aaa")
      .get("error") == "foreign")

print("\n10. Вивід: розмітка не ламається на тексті поста")
msg = post_stat.format_message({
    "net": "fb", "type": "post", "id": "301719373180657_333",
    "permalink": "https://www.facebook.com/nikvesti/posts/333",
    "date": "14.08.2026 18:20",
    "text": 'Ціна впала на <5% — «Тімкрав-Буд» & Ко',
    "views": 12480, "reactions": 84, "comments": 19, "shares": 7,
    "method": "nora",
})
check("сирого «<5%» у повідомленні немає", "<5%" not in msg, msg)
check("екрановане на місці", "&lt;5%" in msg and "&amp; Ко" in msg)
check("перегляди з нерозривним пробілом", "12 480" in msg, msg)
check("лінк на пост є", 'href="https://www.facebook.com/nikvesti/posts/333"' in msg)
check("сказано, як саме знайшов", "нори" in msg, msg)
check("прочерк замість нуля, коли метрики немає",
      "—" in post_stat.format_message({
          "net": "fb", "type": "post", "permalink": "u", "date": "d", "text": "",
          "views": None, "reactions": None, "comments": None, "shares": None,
          "method": "direct"}))

print("\n11. Відмова говорить про СТАТИСТИКУ, а не про пошук бота")
# Олег, 21.08: «я що просив шукати цей пост? я просив статистику». Перша
# версія відповідала «Не знайшов цей пост серед наших публікацій» — це переказ
# власного алгоритму, а не відповідь на питання (CLAUDE.md §9).
lost = post_stat.format_message({"error": "not_found", "scanned": 900,
                                 "og_text": og_a["description"]})
check("починається зі статистики", lost.startswith("Статистики"), lost[:60])
check("переказу алгоритму немає", "серед наших публікацій" not in lost, lost)
check("числа перебраних постів на екран не йдуть", "900" not in lost, lost)
check("текст поста в руках — і він на екрані", "гуртожит" in lost)
check("сказано, що робити далі", "posts/" in lost)

no_page = post_stat.format_message({"error": "no_page"})
check("«сторінку не дали» — окрема причина, а не те саме «не знайшов»",
      "не віддав" in no_page and "два місяці" not in no_page, no_page)
check("і вона теж починається зі статистики", no_page.startswith("Статистики"))
foreign = post_stat.format_message({"error": "foreign", "owner": "newspn"})
check("чужий пост — пояснено, ЧОМУ цифр немає",
      "не бачить ніхто ззовні" in foreign, foreign)

print("\n12. Дірки, знайдені перечитуванням власного коду")
check("текст рілза беремо з description (message у відео немає)",
      post_stat._post_text({"description": "рілз про ремонт"}) == "рілз про ремонт")
check("а поста — з message", post_stat._post_text({"message": "пост"}) == "пост")
# У пісочниці env-змінних немає, а _full_id без page_id свідомо віддає id як є
post_stat.FACEBOOK_PAGE_ID = post_stat.FACEBOOK_PAGE_ID or "301719373180657"
check("короткий id поста добудовується до {сторінка}_{пост}",
      post_stat._full_id("987654321") == "301719373180657_987654321",
      post_stat._full_id("987654321"))
check("уже складений не чіпаємо",
      post_stat._full_id("301719373180657_987") == "301719373180657_987")
# Сторінку не прочитали (Facebook притримав запити) — гортати стрічку нема
# чим і не треба: це дванадцять запитів у порожнечу. Мережі тут не буде.
oid, method, diag = post_stat.resolve_facebook({"net": "fb"}, {})
check("без og стрічку не гортаємо взагалі", oid is None and diag["scanned"] == 0,
      repr(diag))

print("\n13. Порядок сходинок: свіжий пост не має коштувати походу в нору")
calls = {"nora": 0, "pages": 0}


def fake_feed(since_ts=None, until_ts=None, max_pages=post_stat.FEED_SCAN_PAGES):
    for i in range(max_pages):
        calls["pages"] += 1
        if i == 0:
            yield [{"id": "301719373180657_333",
                    "message": og_a["description"], "attachments": {}}]
        else:
            yield [{"id": f"301719373180657_{i}", "message": "щось інше",
                    "attachments": {}}]


real_feed, real_nora = post_stat._iter_feed_pages, post_stat._nora_dates
post_stat._iter_feed_pages = fake_feed
post_stat._nora_dates = lambda text: (calls.__setitem__("nora", calls["nora"] + 1), [])[1]
try:
    oid, method, diag = post_stat.resolve_facebook({"net": "fb"}, og_a)
    check("пост зі свіжої сторінки знайдено", oid == "301719373180657_333", repr(oid))
    check("спосіб — стрічка", method == "feed", repr(method))
    check("однією сторінкою, без другої", calls["pages"] == 1, str(calls["pages"]))
    check("у нору не ходили взагалі", calls["nora"] == 0, str(calls["nora"]))

    # А тепер поста в стрічці немає — сходинки мають вичерпатись і не піти
    # на друге коло: межа FEED_SCAN_PAGES спільна на обидва проходи стрічки
    calls.update(nora=0, pages=0)
    stray = dict(og_a, description="цілком інший текст про зовсім інший привід")
    oid2, method2, diag2 = post_stat.resolve_facebook({"net": "fb"}, stray)
    check("не знайшли — і чесно", oid2 is None and method2 is None)
    check("у нору сходили", calls["nora"] == 1, str(calls["nora"]))
    check("стрічку гортали рівно до межі, а не двічі",
          calls["pages"] == post_stat.FEED_SCAN_PAGES, str(calls["pages"]))
finally:
    post_stat._iter_feed_pages, post_stat._nora_dates = real_feed, real_nora

print("\n14. User-Agent: вдавати браузер — саме те, що ламало все")
# Замір 21.08 із серверної адреси, підряд: повний Chrome/126.0.0.0 → HTTP 400
# і 1542 байти без жодного og; NikVesti-Bot, без UA і facebookexternalhit →
# 200 з тегами. Це не стиль, це умова роботи модуля.
uas = post_stat.UA_LADDER
check("першим іде чесний бот", (uas[0] or "").startswith("NikVesti-Bot"), repr(uas[0]))
check("є захід зовсім без UA", None in uas, repr(uas))
check("жоден захід не вдає браузер",
      not any("Mozilla" in (u or "") or "Chrome" in (u or "") for u in uas), repr(uas))
check("заходів кілька — одна зачинена форма не гасить команду", len(uas) >= 3)

print("\n" + ("❌ ВПАЛО: " + ", ".join(FAILS) if FAILS else "✅ Усі перевірки пройдено"))
sys.exit(1 if FAILS else 0)
