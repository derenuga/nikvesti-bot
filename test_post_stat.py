"""
Тест /post — статистики ОДНОГО поста за лінком на сам пост
(handlers/post_stat.py).

**Історія модуля — і чому тест саме такий.** Половина лінків, якими діляться
в чаті, числового id не містить: `share/p/CODE` редиректить на pfbid-адресу.
Перша версія з цього виснувала «пост треба впізнавати за вмістом» і збудувала
двосторінкову конструкцію (og-теги, підбір User-Agent, фото, нора, перебір
стрічки, схожість текстів) — на одному неперевіреному припущенні, що Graph
не приймає pfbid. Олег зупинив це питанням «коли тобі дають ССИЛКУ на пост,
навіщо ти шукаєш схожий текст?» — і двома запитами в Graph API Explorer
(21.08.2026) довів прямий шлях:

    GET pfbid02nab…?fields=id           → (#12) singular statuses API is
                                          deprecated (об'єкт ЗНАЙДЕНО)
    GET {page_id}_pfbid02nab…?fields=id → {"id": "301719373180657_1715521640576310"}

Тобто складена форма `{page_id}_{pfbid}` працює як звичайний id. Уся
конструкція впізнавання видалена; цей тест стереже те, що ЛИШИЛОСЬ, і те,
чого більше НЕ МАЄ БУТИ.

**Що перевіряємо:**

- всі чотири реальні лінки Олега розбираються в те, чим вони є, і жоден не
  приймається за лінк матеріалу;
- pfbid іде в Graph СКЛАДЕНОЮ формою — це серце прямого шляху;
- семантики більше немає: модуль не має ні читання og, ні скорингу, ні
  проходу стрічки ФБ (їх повернення — регресія, а не фіча);
- чужа сторінка відсікається за ніком до будь-яких запитів;
- відмова API — не «не знайшли»: причина доїжджає до людини, протухлий
  токен називається токеном (12.08 INSTAGRAM_TOKEN помер, і /post дев'ять
  днів по тому переказав це як «пост давніший»);
- вивід екранує HTML і говорить про статистику, а не про пошук бота.

Мережі й токенів не треба — усе локальне.
"""

import os
import sys

from handlers import post_stat

FAILS = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


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

print("\n4. Прямий шлях: pfbid іде в Graph складеною формою")
# У пісочниці env-змінних немає, а _full_id без page_id свідомо віддає id як є
post_stat.FACEBOOK_PAGE_ID = post_stat.FACEBOOK_PAGE_ID or "301719373180657"
pfbid = "pfbid02nabJowuvNoET9pzUui4DuGJQACnSRnWA5qkPVt4QPzyR9aygqQ7tRgozdmJJqCnSl"
check("pfbid → {page_id}_{pfbid} (доведено Explorer-ом 21.08)",
      post_stat._full_id(pfbid) == f"301719373180657_{pfbid}",
      post_stat._full_id(pfbid))
check("числовий короткий id — так само",
      post_stat._full_id("987654321") == "301719373180657_987654321")
check("уже складений не чіпаємо",
      post_stat._full_id("301719373180657_987") == "301719373180657_987")
check("facebook_post_stat бере pfbid як object_id",
      "pfbid" in __import__("inspect").getsource(post_stat.facebook_post_stat))
check("а лінк без жодного id — чесна відмова, без запитів",
      post_stat.facebook_post_stat({"net": "fb"}).get("error") == "unresolved")

print("\n5. Семантики більше немає (її повернення — регресія)")
for gone in ("parse_og", "image_object_ids", "find_in_feed", "UA_LADDER",
             "_iter_feed_pages", "_nora_dates", "TEXT_MATCH_MIN", "fetch_page"):
    check(f"викинуто: {gone}", not hasattr(post_stat, gone))
check("фікстури сторінок ФБ видалені разом із нею",
      not any(os.path.exists(f"data/fb_post_{n}.html.gz")
              for n in ("pfbid", "share", "missing")))

print("\n6. Чужа сторінка відсікається до будь-яких запитів")
check("чужий нік видно", post_stat.foreign_owner(
    post_stat.parse_link("https://www.facebook.com/newspn/posts/pfbid0aaa")) == "newspn")
check("наш нік — не чужий", post_stat.foreign_owner(post_stat.parse_link(PFBID)) is None)
check("ніка немає — не чужий", post_stat.foreign_owner(post_stat.parse_link(SHARE_1)) is None)
check("чужий лінк одразу віддає причину",
      post_stat.collect("https://www.facebook.com/newspn/posts/pfbid0aaa")
      .get("error") == "foreign")

print("\n7. Текст об'єкта — за типом вузла")
check("рілз — із description (message у відео немає)",
      post_stat._post_text({"description": "рілз про ремонт"}) == "рілз про ремонт")
check("пост — із message", post_stat._post_text({"message": "пост"}) == "пост")

print("\n8. Протухлий токен: бот КАЖЕ сам, а не переказує як «пост давніший»")
IG_EXPIRED = ("Error validating access token: Session has expired on "
              "Wednesday, 12-Aug-26 04:43:10 PDT.")
check("текст помилки інсти впізнається як токен",
      post_stat.fb_token.is_token_error(IG_EXPIRED))
tok = post_stat.format_message({"net": "ig", "error": "api", "detail": IG_EXPIRED})
check("названо саме токен Instagram", "протух токен Instagram" in tok, tok)
check("сказано, що це НЕ тимчасово", "не тимчасово" in tok, tok)
check("жодного «пост давніший»", "давніш" not in tok, tok)
fbtok = post_stat.format_message({"net": "fb", "error": "api", "detail": IG_EXPIRED})
check("для ФБ — свій токен у тексті", "протух токен Facebook" in fbtok, fbtok)
ig_lost = post_stat.format_message({"net": "ig", "error": "not_found"})
check("порожньому інста-результату не радять лінк фейсбука",
      "facebook.com" not in ig_lost, ig_lost)

print("\n9. Вивід: розмітка не ламається, мова — про статистику")
msg = post_stat.format_message({
    "net": "fb", "type": "post", "id": "301719373180657_333",
    "permalink": "https://www.facebook.com/nikvesti/posts/333",
    "date": "14.08.2026 18:20",
    "text": 'Ціна впала на <5% — «Тімкрав-Буд» & Ко',
    "views": 12480, "reactions": 84, "comments": 19, "shares": 7,
})
check("сирого «<5%» у повідомленні немає", "<5%" not in msg, msg)
check("екрановане на місці", "&lt;5%" in msg and "&amp; Ко" in msg)
check("перегляди з нерозривним пробілом", "12 480" in msg, msg)
check("лінк на пост є", 'href="https://www.facebook.com/nikvesti/posts/333"' in msg)
check("розповіді бота про себе немає",
      "нор" not in msg and "стрічці" not in msg and "лінку не було" not in msg, msg)
check("прочерк замість нуля, коли метрики немає",
      "—" in post_stat.format_message({
          "net": "fb", "type": "post", "permalink": "u", "date": "d", "text": "",
          "views": None, "reactions": None, "comments": None, "shares": None}))
unres = post_stat.format_message({"net": "fb", "error": "unresolved"})
check("відмова починається зі статистики", unres.startswith("Статистики"), unres)
check("і каже, що робити далі", "posts/" in unres)

print("\n" + ("❌ ВПАЛО: " + ", ".join(FAILS) if FAILS else "✅ Усі перевірки пройдено"))
sys.exit(1 if FAILS else 0)
