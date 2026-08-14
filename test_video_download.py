"""
Тест сторінки завантаження відео (handlers/video_download.py).

Дані НЕ вигадані — обидві фікстури зняті з живих джерел 14.08.2026:
  data/video_formats_sample.json     — YouTube, 30 форматів: 2160p…360p,
      розкадровки mhtml із висотами 27/45/90 і рівно ОДИН прогресивний
      формат (відео зі звуком в одному файлі);
  data/video_formats_fb_sample.json  — Facebook, відео сторінки: висоти
      НЕСТАНДАРТНІ (848 і 540), розмірів файлів джерело не каже взагалі,
      а в назві лежить «5.9K views · 62 reactions | <текст допису> | СТОРІНКА».
Обидві свого часу зламали наївну версію розкладки, тому тут вони й лежать.

Що стережеться:

  1. Розкадровки не стають варіантами якості: вони мають height і проходять за
     наївний фільтр — і тоді людині пропонується «90p», за яким не відео, а
     сітка мініатюр.
  2. Без ffmpeg пропонується ЛИШЕ те, що лежить одним файлом (на YouTube це
     360p): кнопка «1080p», за якою приїжджає німе відео, гірша за її брак.
  3. Нестандартні висоти Facebook не губляться. Перша версія лишала звичні
     сходинки (1080/720/480) плюс найвищу — і на Facebook із двох якостей
     показувала одну.
  4. Розмір рахується з бітрейту, коли джерело мовчить (весь Facebook).
  5. Назва Facebook чиститься від лічильників і назви сторінки — інакше саме
     цей рядок стає імʼям файлу.
  6. Хости: приймаються YouTube, Facebook, Instagram у всіх мобільних
     написаннях і не приймається решта інтернету — це не відкритий проксі.
  7. Помилки джерела перекладаються людською мовою. Головний випадок —
     «Sign in to confirm you're not a bot» і HTTP 400 від Facebook: це не
     поламаний бот і не поганий лінк, а відмова говорити з незалогіненим.
  8. Куки: приймається і cookies.txt, і JSON-експорт розширення, і base64;
     свої куки людини сильніші за редакційні.

Запуск: python test_video_download.py
"""

import json
import os
import sys
import tempfile

# Стан бота на час тесту — тимчасовий файл, щоб не чіпати робочий
os.environ["STATE_PATH"] = tempfile.mktemp(suffix="_video_test.json")

from handlers import video_download as vd

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FIXTURE = os.path.join(_DATA, "video_formats_sample.json")
FIXTURE_FB = os.path.join(_DATA, "video_formats_fb_sample.json")

failures = []


def check(name, condition, detail=""):
    print(("  ✅ " if condition else "  ❌ ") + name + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def load(path=FIXTURE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_curation_with_ffmpeg():
    print("\nЗ ffmpeg — повний список якостей:")
    info = load()
    options = vd.curate_formats(info, ffmpeg=True)
    ids = [o["id"] for o in options]
    labels = [o["label"] for o in options]

    check("є 1080p і 2160p", "v1080" in ids and "v2160" in ids, ids)
    check("є «тільки звук»", "audio" in ids, ids)
    check("розкадровки не потрапили у варіанти",
          not any(x in ids for x in ("v90", "v45", "v27")), ids)
    check("варіанти йдуть від найкращої якості",
          labels[0] == "2160p", labels)
    check("селектор склеює відео зі звуком",
          all("bestvideo" in o["selector"] for o in options if o["kind"] == "video"),
          [o["selector"] for o in options])
    check("у кожної якості порахований розмір",
          all(o["bytes"] > 0 for o in options),
          [(o["id"], o["bytes"]) for o in options])
    # 1080p це відео (30.8 МБ) + звук (3.4 МБ) — розмір має враховувати обидва
    v1080 = next(o for o in options if o["id"] == "v1080")
    check("розмір 1080p рахує і звукову доріжку",
          v1080["bytes"] > 33 * 1024 * 1024, vd.human_size(v1080["bytes"]))


def test_curation_without_ffmpeg():
    print("\nБез ffmpeg — лише те, що лежить одним файлом:")
    info = load()
    options = vd.curate_formats(info, ffmpeg=False)
    ids = [o["id"] for o in options]
    videos = [o for o in options if o["kind"] == "video"]

    check("лишилось рівно 360p", ids == ["v360", "audio"], ids)
    check("жодної обіцянки 1080p", "v1080" not in ids, ids)
    check("селектор вимагає звук у тому самому файлі",
          all("acodec!=none" in o["selector"] for o in videos),
          [o["selector"] for o in videos])


def test_facebook_real():
    """Живе відео Facebook: нестандартні висоти, розмірів немає, назва — допис."""
    print("\nFacebook (живі дані):")
    info = load(FIXTURE_FB)
    options = vd.curate_formats(info, ffmpeg=True)
    ids = [o["id"] for o in options]

    check("обидві висоти на місці (848 і 540)",
          "v848" in ids and "v540" in ids, ids)
    check("є «тільки звук»", "audio" in ids, ids)
    check("розмір порахований, хоч джерело його не сказало",
          all(o["bytes"] > 0 for o in options),
          [(o["id"], o["note"]) for o in options])

    # Без ffmpeg прогресивних форматів у цього відео немає взагалі —
    # лишається чесне «найкраща якість», а не порожній екран
    fallback = [o["id"] for o in vd.curate_formats(info, ffmpeg=False)]
    check("без ffmpeg лишається запасний варіант", "best" in fallback, fallback)

    title = vd.clean_title(info["title"])
    check("лічильники переглядів зрізано",
          "views" not in title and "reactions" not in title, title[:60])
    check("назву сторінки в хвості прибрано",
          not title.endswith("SPILNO.UA"), title[-30:])
    check("зміст допису лишився", "Джонсон" in title, title[:60])

    fname = vd.safe_filename(info["title"], "mp4")
    check("імʼя файлу закінчується розширенням", fname.endswith(".mp4"), fname)
    check("імʼя файлу не довше сотні символів", len(fname) < 105, len(fname))
    check("в імені немає слешів і двокрапок",
          not set(fname) & set('\\/:*?"<>|'), fname)


def test_no_heights():
    """Джерело не сказало ЖОДНОЇ висоти (HLS-стрічка) — кнопки все одно є."""
    print("\nДжерело без висот:")
    info = load()
    for f in info["formats"]:
        f["height"] = None
    options = vd.curate_formats(info, ffmpeg=True)
    check("є принаймні один варіант", bool(options), options)
    check("це «найкраща якість»",
          any(o["id"] == "best" for o in options), [o["id"] for o in options])


def test_describe():
    print("\nКартка відео:")
    data = vd.describe(load(), ffmpeg=True)
    check("є назва", bool(data["title"]))
    check("тривалість людською мовою", data["duration_text"] == "3:33",
          data["duration_text"])
    check("є обкладинка", data["thumbnail"].startswith("http"))
    check("названо джерело", bool(data["source"]), data["source"])


def test_hosts():
    print("\nЯкі лінки приймаються:")
    good = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.facebook.com/watch/?v=123",
        "https://web.facebook.com/nikvesti/videos/123",
        "https://fb.watch/abc123/",
    ]
    bad = [
        "https://evil.com/video",
        "https://youtube.com.evil.com/watch?v=1",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",   # метадані хмари
        "not a url at all",
        "",
    ]
    for url in good:
        check(f"приймається {url[:46]}", vd.host_allowed(url))
    for url in bad:
        check(f"відкидається {url[:46] or '(порожньо)'}", not vd.host_allowed(url))


def test_errors():
    print("\nПомилки людською мовою:")
    capture = ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use "
               "--cookies-from-browser or --cookies for the authentication. See "
               "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies")
    bot = vd.humanize_error(capture, "https://youtu.be/abc")
    check("капча пояснена як розвʼязна",
          "від твого імені" in bot and "один раз" in bot, bot)
    check("без посилань на wiki yt-dlp", "github" not in bot.lower(), bot)
    check("капчі пропонується дозвіл", vd.wants_cookies(capture))

    fb_raw = ("ERROR: [facebook] 123: Cannot parse data; please report this "
              "issue on https://github.com/yt-dlp/yt-dlp/issues")
    fb = vd.humanize_error(fb_raw, "https://facebook.com/watch/?v=123")
    check("логін-стіна Facebook названа своїм ім'ям", "Facebook" in fb, fb)
    check("рілзу пропонується дозвіл", vd.wants_cookies(fb_raw))

    priv = vd.humanize_error("ERROR: Private video. Sign in if you've been granted access",
                             "https://youtu.be/x")
    check("приватне відео — окреме пояснення", "риватн" in priv, priv)

    unsup = vd.humanize_error("ERROR: Unsupported URL: https://facebook.com/nikvesti/videos",
                              "https://facebook.com/nikvesti/videos")
    check("лінк на канал замість відео — окреме пояснення",
          "канал" in unsup, unsup)

    check("службовий хвіст зрізано",
          "report this issue" not in vd.humanize_error(
              "Weird thing happened; please report this issue on https://x", ""))

    # Кнопка «Продовжити» не має зʼявлятись там, де куки не допоможуть:
    # запропонувати дію, яка не полагодить, гірше, ніж не пропонувати нічого
    for name, raw in (
        ("DRM", "ERROR: [youtube] x: This video is DRM protected"),
        ("видалене", "ERROR: [youtube] x: This video is unavailable"),
        ("не той лінк", "ERROR: Unsupported URL: https://facebook.com/x/videos"),
    ):
        check(f"{name} — дозволу НЕ пропонуємо", not vd.wants_cookies(raw))
    check("Instagram — пропонуємо", vd.wants_cookies(
        "ERROR: [Instagram] x: Instagram sent an empty media response."))


def test_retry():
    """Примхливість джерела бот переживає сам, не турбуючи людину.

    Заміряно 14.08: те саме завантаження, яке щойно двічі пройшло, дало
    «HTTP Error 403: Forbidden», а одразу наступний свіжий прогін пройшов з
    першого разу. Отже 403 — не привід ні для помилки, ні для питання про
    куки, а привід тихо спробувати ще раз.
    """
    print("\nКоли варто просто спробувати ще раз:")
    for name, raw, want in (
        ("403 від YouTube", "ERROR: unable to download video data: HTTP Error 403: Forbidden", True),
        ("обірваний звʼязок", "ERROR: [Errno 104] Connection reset by peer", True),
        ("таймаут", "ERROR: The read operation timed out", True),
        ("видалене відео", "ERROR: [youtube] x: This video is unavailable", False),
        ("DRM", "ERROR: [youtube] x: This video is DRM protected", False),
        ("капча", "ERROR: [youtube] x: Sign in to confirm you’re not a bot", False),
    ):
        check(f"{name} — {'повторюємо' if want else 'не повторюємо'}",
              vd._worth_retry(raw) == want, raw[:50])

    # 403 не має тягти за собою пропозицію дати куки: доступ тут ні до чого
    check("403 не просить кук", not vd.wants_cookies(
        "unable to download video data: HTTP Error 403: Forbidden"))


def test_human():
    print("\nЧисла людською мовою:")
    check("байти", vd.human_size(1536) == "2 КБ", vd.human_size(1536))
    check("мегабайти", vd.human_size(11 * 1024 * 1024) == "11.0 МБ",
          vd.human_size(11 * 1024 * 1024))
    check("нуль — порожньо", vd.human_size(0) == "")
    check("година", vd.human_duration(3725) == "1:02:05", vd.human_duration(3725))
    check("хвилини", vd.human_duration(213) == "3:33", vd.human_duration(213))


def test_cookies_parsing():
    """Три способи, якими куки приїжджають від людини, мають читатись усі."""
    print("\nРозбір кук:")
    import base64
    netscape = ("# Netscape HTTP Cookie File\n"
                ".facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t123\n"
                ".facebook.com\tTRUE\t/\tTRUE\t0\txs\tabc\n")
    as_json = ('[{"domain": ".facebook.com", "name": "c_user", "value": "123",'
               ' "path": "/", "secure": true, "expirationDate": 1800000000}]')

    for name, value, domain in (
        ("cookies.txt як є", netscape, "facebook.com"),
        ("base64", base64.b64encode(netscape.encode()).decode(), "facebook.com"),
        ("JSON-експорт розширення", as_json, "facebook.com"),
        ("без заголовка Netscape",
         ".youtube.com\tTRUE\t/\tTRUE\t0\tX\t1\n", "youtube.com"),
    ):
        try:
            text, domains = vd.normalize_cookies(value)
        except ValueError as e:
            check(f"{name} — розібрано", False, str(e))
            continue
        check(f"{name} — розібрано", True)
        check(f"{name} — заголовок Netscape дописано",
              text.lstrip().startswith("# Netscape"), text[:30])
        check(f"{name} — домен упізнано", domain in domains, domains)

    for name, bad in (("порожньо", "  "),
                      ("не куки", "просто текст без табуляцій"),
                      ("побитий JSON", "[{зламано")):
        try:
            vd.normalize_cookies(bad)
            check(f"{name} — відхилено", False, "прийняв сміття")
        except ValueError:
            check(f"{name} — відхилено", True)


def test_cookies_priority():
    """Свої куки людини сильніші за редакційні — саме цього просив Олег."""
    print("\nЧиї куки беруться:")
    netscape = "# Netscape HTTP Cookie File\n.facebook.com\tTRUE\t/\tTRUE\t0\tc_user\t{}\n"

    vd.save_cookies(netscape.format("SHARED"), shared=True, name="Редакція")
    info = vd.cookie_info("777")
    check("є редакційні", info["shared"] and info["ok"], info)
    check("своїх поки немає", not info["own"], info)
    check("файл кук — редакційний",
          "SHARED" in open(vd._cookie_file("777"), encoding="utf-8").read())

    vd.save_cookies(netscape.format("MINE"), user_id="777", name="Олег")
    info = vd.cookie_info("777")
    check("зʼявились свої", info["own"], info)
    check("файл кук став особистим",
          "MINE" in open(vd._cookie_file("777"), encoding="utf-8").read())
    check("іншій людині далі йдуть редакційні",
          "SHARED" in open(vd._cookie_file("888"), encoding="utf-8").read())

    vd.forget_cookies("777")
    check("після прибирання — знову редакційні",
          "SHARED" in open(vd._cookie_file("777"), encoding="utf-8").read())
    check("свої зникли", not vd.cookie_info("777")["own"])
    vd.forget_cookies(shared=True)


def test_access_tokens():
    """Вхід у кожного свій і стабільний."""
    print("\nДоступ до сторінки:")
    first = vd.token_for(56631818, "Олег Деренюга")
    again = vd.token_for(56631818, "Олег Деренюга")
    other = vd.token_for(56424866, "Катерина Середа")

    check("токен стабільний (сторінку можна в закладки)", first == again,
          (first, again))
    check("у різних людей різні токени", first != other)
    check("токен упізнає людину",
          (vd.whois(first) or {}).get("name") == "Олег Деренюга", vd.whois(first))
    check("чужий токен не впізнається", vd.whois("сміття") is None)
    check("порожній токен не впізнається", vd.whois("") is None)

    link = vd.video_url(first, "https://youtu.be/abc")
    check("лінк несе і токен, і відео",
          link is None or (first in link and "youtu.be" in link), link)


def main():
    for path in (FIXTURE, FIXTURE_FB):
        if not os.path.exists(path):
            print(f"❌ немає фікстури {path}")
            return 1
    test_curation_with_ffmpeg()
    test_curation_without_ffmpeg()
    test_facebook_real()
    test_no_heights()
    test_describe()
    test_hosts()
    test_errors()
    test_retry()
    test_human()
    test_cookies_parsing()
    test_cookies_priority()
    test_access_tokens()

    print()
    if failures:
        print(f"❌ впало {len(failures)}: " + ", ".join(failures))
        return 1
    print("✅ усе пройшло")
    return 0


if __name__ == "__main__":
    sys.exit(main())
