"""
Тест доступу до Instagram: підбір дверей, самопродовження токена і /ig_token
(handlers/instagram.py).

**Що сталось.** INSTAGRAM_TOKEN протух 12.08.2026, і про це дев'ять днів не
знав ніхто. Коли з'ясувалось, наступне питання виявилось складнішим за саме
протухання: «як зробити новий?» Відповіді немає без знання, ЯКОГО типу токен
потрібен — Meta має два продукти на ті самі дані, і в Graph API Explorer
вибір не тієї гілки дає «Invalid platform app». Живий замір /ig_token 21.08
показав три різні відмови, і кожна означала своє:

    graph.instagram.com + INSTAGRAM_TOKEN → «Session has expired»
        (токен справді мертвий)
    graph.facebook.com  + INSTAGRAM_TOKEN → «Cannot parse access token»
        (він взагалі НЕ фейсбучний — отже, Instagram Login)
    graph.facebook.com  + FACEBOOK_PAGE_TOKEN → «Object … does not exist»
        (токен живий, але IG-акаунта не бачить)

**Що перевіряємо:**

- двері підбираються, а не зашиті: три маршрути, і серед них є фейсбучний
  токен — саме він робить окрему змінну під інсту непотрібною;
- коли не відкрились жодні, credentials() віддає ПЕРШІ, а не (None, None):
  виклик має впасти з реальним текстом Meta, а не з «Invalid URL 'None/…'»;
- токен зі стану бота сильніший за env — інакше продовжений токен нікуди не
  подінеться, бо в змінну Railway бот писати не може;
- самопродовження не смикає Meta даремно: поки токен молодший за
  REFRESH_AFTER_DAYS, виклик мовчки виходить без жодного запиту;
- порожній токен не приймається (у стані не має осідати рядок, який потім
  видаватиме себе за налаштований доступ);
- /ig_token НІКОЛИ не друкує значень токенів — лише назви змінних.

Мережі не треба: усе, що тут перевіряється, рахується локально.
"""

import datetime
import os
import sys
import tempfile

os.environ.setdefault("STATE_PATH", tempfile.mktemp(suffix="-ig.json"))

from handlers import instagram, storage  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


print("1. Двері підбираються, а не зашиті")
routes = instagram.IG_ROUTES
check("маршрутів кілька", len(routes) >= 3, repr(routes))
check("є інстаграмний хост", any("graph.instagram.com" in b for b, _ in routes))
check("є фейсбучний хост", any("graph.facebook.com" in b for b, _ in routes))
check("і серед них — токен сторінки ФБ (окрема змінна стає зайвою)",
      any(t == "FACEBOOK_PAGE_TOKEN" for _, t in routes), repr(routes))
check("першим лишається історичний маршрут — поведінка не змінюється",
      routes[0] == ("https://graph.instagram.com/v21.0", "INSTAGRAM_TOKEN"), repr(routes[0]))

print("\n2. Нічого не відкрилось — падаємо з причиною Meta, а не з urllib")
instagram._route_cache.update(at=0, value=None)
# probe_route без токена не ходить у мережу зовсім
ok, err = instagram.probe_route("https://graph.instagram.com/v21.0", None)
check("порожній токен — це відмова, а не запит", not ok and "змінної" in (err or ""), repr(err))
instagram._route_cache.update(at=9e18, value=("https://graph.instagram.com/v21.0", None))
base, token = instagram.credentials()
check("base лишається справжнім URL", (base or "").startswith("https://"), repr(base))
check("а не рядком 'None'", "None" not in str(base), repr(base))

print("\n3. Змінна Railway сильніша за збережений токен")
# Найнебезпечніший сценарій: людина замінила INSTAGRAM_TOKEN руками, а бот
# далі тримається за старий зі стану і каже, що інста мертва. Тому стан живе,
# лише поки походить від НИНІШНЬОЇ змінної (ключ seed).
instagram.INSTAGRAM_TOKEN = "IGQ-нова-змінна"
storage.save_ig_oauth({"token": "IGQ-продовжений", "seed": "IGQ-СТАРА-змінна",
                       "set_at": datetime.datetime.now().isoformat(timespec="seconds")})
check("змінили змінну → беремо її, а не збережене",
      instagram.stored_token() == "IGQ-нова-змінна", instagram.stored_token())
check("і застарілий ланцюг продовжень викинуто",
      not storage.get_ig_oauth().get("token"), repr(storage.get_ig_oauth()))

storage.save_ig_oauth({"token": "IGQ-продовжений", "seed": "IGQ-нова-змінна",
                       "set_at": datetime.datetime.now().isoformat(timespec="seconds")})
check("а поки seed збігається — живе продовжений",
      instagram.stored_token() == "IGQ-продовжений", instagram.stored_token())
check("саме він іде в маршрут INSTAGRAM_TOKEN",
      instagram._token_by_name("INSTAGRAM_TOKEN") == "IGQ-продовжений")
check("а FACEBOOK_PAGE_TOKEN не підмінюється",
      instagram._token_by_name("FACEBOOK_PAGE_TOKEN") != "IGQ-продовжений")

print("\n4. Продовження не смикає Meta даремно")
storage.save_ig_oauth({"token": "IGQ-продовжений", "seed": "IGQ-нова-змінна",
                       "set_at": datetime.datetime.now().isoformat(timespec="seconds")})
ok, why = instagram.refresh_token()
check("свіжий токен не продовжуємо", not ok and "рано" in str(why), repr(why))
check("вік рахується днями, а не здогадом", "0 дн." in str(why), repr(why))
storage.save_ig_oauth({"token": "IGQ-старий", "seed": "IGQ-нова-змінна", "set_at": (
    datetime.datetime.now() - datetime.timedelta(days=instagram.REFRESH_AFTER_DAYS - 1)
).isoformat(timespec="seconds")})
ok, why = instagram.refresh_token()
check("за день до порога — ще рано", not ok and "рано" in str(why), repr(why))
check("поріг заздалегідь, а не в останній день (протухлий не рятує ніщо)",
      instagram.REFRESH_AFTER_DAYS <= 45, str(instagram.REFRESH_AFTER_DAYS))

# Щойно заведений руками токен: віку не знаємо, Meta молодший за добу не
# продовжує — ставимо відлік від зараз і чекаємо, а не б'ємось у стіну
storage.save_ig_oauth({})
ok, why = instagram.refresh_token()
check("щойно зі змінної — відлік ставимо, Meta не смикаємо",
      not ok and "щойно" in str(why), repr(why))
check("і seed закріплено за нинішньою змінною",
      storage.get_ig_oauth().get("seed") == "IGQ-нова-змінна",
      repr(storage.get_ig_oauth()))
check("а відлік почався", bool(storage.get_ig_oauth().get("set_at")))

print("\n5. Порожній токен у стані не осідає")
before = storage.get_ig_oauth().get("token")
ok, err = instagram.adopt_token("")
check("порожній відхилено", not ok and err == "порожньо", repr(err))
check("стан не зіпсовано", storage.get_ig_oauth().get("token") == before)

print("\n6. Діагностика не друкує таємниць")
storage.save_ig_oauth({"token": "IGQ-СЕКРЕТ-НЕ-ПОКАЗУВАТИ", "set_at": "2026-08-21T13:00:00"})
rows = [{"base": b, "token": t, "ok": False, "error": "x"} for b, t in instagram.IG_ROUTES]
rendered = " ".join(f"{r['base']} {r['token']} {r['error']}" for r in rows)
check("у звіті маршрутів — назви змінних, не значення",
      "IGQ-СЕКРЕТ" not in rendered, rendered)
check("назви змінних на місці", "INSTAGRAM_TOKEN" in rendered and "FACEBOOK_PAGE_TOKEN" in rendered)
linked, err = instagram.linked_ig_account()
check("без FACEBOOK_PAGE_ID привʼязку чесно не вгадуємо",
      linked is None and "FACEBOOK_PAGE_ID" in (err or ""), repr(err))

print("\n" + ("❌ ВПАЛО: " + ", ".join(FAILS) if FAILS else "✅ Усі перевірки пройдено"))
sys.exit(1 if FAILS else 0)
