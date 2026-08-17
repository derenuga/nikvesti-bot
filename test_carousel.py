"""
Бекенд генератора каруселей: скрап статті, точність цитат, токени доступу.

Головне, що тут стережеться, — **вигадана цитата**. Агент бачить статтю й
пише слайди, і найдорожча його помилка не «нудний заголовок», а пряма мова,
якої ніхто не казав: підпис «Іванов, депутат» перетворює переказ на цитату,
і це вже не чернетка, а брехня від імені видання. Тому текст цитати ставить
КОД, а не промпт, і саме цю межу перевіряють половина тестів нижче.

Друге — скрап на РЕАЛЬНОМУ HTML (правило 3 CLAUDE.md). Фікстури в data/ —
живі сторінки, зняті 17.08.2026, стиснуті gzip'ом (по 130 КБ кожна):
  • 322389 — Нацрада: 1 фото, 0 blockquote, 10 абзаців (з них один порожній);
  • 322377 — мішки з Дюка: 2 фото, 6 blockquote, 22 абзаци (один із них —
    обгортка iframe без тексту);
  • 322371 — «Іскра»: 3 фото, з них ДВА скриншоти акта обстеження. Саме на
    цій статті Олег сказав, що для таких випадків потрібне ручне
    підвантаження фото, — тож детектор «це папір, а не кадр» теж тут.

Запуск: python test_carousel.py
"""

import asyncio
import gzip
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("STATE_PATH", "/tmp/carousel_test_state.json")

# Стан попереднього прогону зносимо: тест перевіряє КАП кеша планів, тобто
# сам його переповнює, і залишки означали б, що наступний прогін стартує з
# повного кеша і перша ж перевірка «план дістається за id» падає — не через
# код, а через сусідній тест. Спіймано загальним прогоном 17.08.
if os.path.exists(os.environ["STATE_PATH"]):
    os.remove(os.environ["STATE_PATH"])

from handlers import card_maker, carousel  # noqa: E402


def fixture(article_id):
    path = ROOT / "data" / f"article_{article_id}.html.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def article_of(article_id):
    """Те саме склеювання, що робить carousel.scrape_article, лише без мережі."""
    html = fixture(article_id)
    data = card_maker.parse_article(html)
    body = card_maker.parse_article_body(html)
    data["quotes"] = body["quotes"]
    data["paragraphs"] = body["paragraphs"]
    data["author"] = body["author"]
    return data


def main():
    failures = []

    def check(label, cond, detail=""):
        print(f"{'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    # ---------- 1. Скрап: текст без промо Клубу ----------
    print("\n— Скрап статті (реальні сторінки) —")

    a389 = article_of("322389")
    check("322389: абзаци з прямих дітей контейнера",
          len(a389["paragraphs"]) == 9, f"{len(a389['paragraphs'])} абзаців")
    joined = " ".join(a389["paragraphs"])
    check("322389: промо Клубу МикВісті не потрапило в текст",
          "Клуб МикВісті" not in joined and "донат" not in joined.lower())
    check("322389: цитат немає — і слайдів quote бути не може",
          a389["quotes"] == [], f"знайшлось {len(a389['quotes'])}")
    check("322389: автор зі сторінки",
          (a389["author"] or {}).get("name") == "Таміла Ксьонжик",
          str(a389["author"]))

    a377 = article_of("322377")
    check("322377: шість цитат по порядку", len(a377["quotes"]) == 6,
          f"{len(a377['quotes'])}")
    check("322377: 21 абзац (порожня обгортка iframe відкинута)",
          len(a377["paragraphs"]) == 21, f"{len(a377['paragraphs'])}")
    check("322377: цитати не потрапили в абзаци",
          not any(q in a377["paragraphs"] for q in a377["quotes"]))
    check("322377: посада автора зчитана",
          (a377["author"] or {}).get("position") == "Репортерка",
          str(a377["author"]))

    a371 = article_of("322371")
    check("322371: три фото і три цитати",
          len(a371["images"]) == 3 and len(a371["quotes"]) == 3,
          f"фото {len(a371['images'])}, цитат {len(a371['quotes'])}")

    # ---------- 2. Аватарка автора ----------
    print("\n— Аватарка автора —")
    photo = a371["author"]["photo"]
    big = card_maker.author_photo_url(photo)
    check("96x96 → 255x255", "/255x255/" in big and big.endswith(".webp"), big)
    check("великий шлях лишається на nikvesti.com",
          big.startswith("https://nikvesti.com/"), big)
    check("розмір підміняється рівно один раз",
          big.count("/255x255/") == 1, big)

    # ---------- 3. Точність цитат ----------
    print("\n— Точність цитат (головне) —")
    quotes = a377["quotes"]

    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "attribution": "Олег Звягін"}], quotes)
    check("цитата за номером береться дослівно з тексту статті",
          slides[0].get("quote") == quotes[0], slides[0].get("quote", "")[:60])
    check("чесна цитата не додає зауважень", notes == [], str(notes))

    # Дослівний фрагмент приймається
    piece = quotes[0][10:60]
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "quote_excerpt": piece}], quotes)
    check("дослівний фрагмент приймається", slides[0]["quote"] == piece,
          slides[0]["quote"][:60])

    # Фрагмент із іншими пробілами — теж свій текст, має пройти
    spaced = "  ".join(piece.split(" "))
    slides, _ = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "quote_excerpt": spaced}], quotes)
    check("зайві пробіли у фрагменті не роблять із нього вигадку",
          slides[0]["quote"] == spaced, slides[0]["quote"][:60])

    # ВИГАДКА: фрагмента в оригіналі немає
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0,
          "quote_excerpt": "ми знесемо цей пам'ятник до кінця тижня"}], quotes)
    check("вигаданий фрагмент відкинуто, стоїть текст зі статті",
          slides[0]["quote"] == quotes[0], slides[0]["quote"][:60])
    check("про підміну сказано вголос", len(notes) == 1 and "дослівно" in notes[0],
          str(notes))

    # Номер поза списком → слайд деградує в текстовий
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 99, "attribution": "Хтось"}], quotes)
    check("неіснуюча цитата не стає слайдом", slides[0]["type"] == "text",
          slides[0]["type"])
    check("деградований слайд не несе ні цитати, ні підпису під нею",
          "quote" not in slides[0] and "attribution" not in slides[0],
          str(slides[0]))
    check("про деградацію сказано вголос", len(notes) == 1, str(notes))

    # Стаття без цитат: quote-слайд неможливий у принципі
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0}], a389["quotes"])
    check("у статті без blockquote quote-слайдів не буває",
          slides[0]["type"] == "text", slides[0]["type"])

    # ---------- 4. Нормалізація плану ----------
    print("\n— Нормалізація плану агента —")
    plan = carousel.normalize_plan({
        "slides": [
            {"type": "text", "title": "Не обкладинка"},
            {"type": "photo", "caption": "Кадр", "photo_index": 42},
            {"type": "quote", "quote_index": 1},
            {"type": "стрибок", "title": "невідомий тип"},
        ],
        "cta_suggestion": "невідомий",
    }, a377)
    check("перший слайд завжди обкладинка", plan["slides"][0]["type"] == "cover",
          plan["slides"][0]["type"])
    check("невідомий тип слайда викинуто", len(plan["slides"]) == 3,
          f"{len(plan['slides'])} слайдів")
    check("фото поза списком не стає битим слайдом",
          plan["slides"][1]["type"] == "text" and plan["slides"][1]["photo_index"] is None,
          str(plan["slides"][1]))
    check("невідомий CTA замінено дефолтним",
          plan["cta_suggestion"] == carousel.DEFAULT_CTA, plan["cta_suggestion"])

    long_plan = carousel.normalize_plan(
        {"slides": [{"type": "text", "body": f"слайд {i}"} for i in range(20)]}, a377)
    check(f"більше {carousel.MAX_SLIDES} слайдів агент не нав'яже",
          len(long_plan["slides"]) == carousel.MAX_SLIDES,
          f"{len(long_plan['slides'])}")

    # ---------- 5. Скриншоти документів ----------
    print("\n— «Це папір, а не кадр» —")
    flags = [carousel.looks_like_document(im["caption"]) for im in a371["images"]]
    check("два скриншоти акта позначені, живий кадр — ні",
          flags == [False, True, True], str(flags))
    check("фото без підпису не оголошується документом",
          not carousel.looks_like_document(""))
    prompt = carousel.build_prompt(a371)
    check("агент бачить попередження біля саме тих фото",
          prompt.count("⚠ схоже на скриншот документа") == 2,
          str(prompt.count("⚠ схоже на скриншот документа")))
    check("нумерація фото і цитат іде в промпт",
          "[0]" in prompt and "[2]" in prompt)
    check("промпт несе текст статті",
          a371["paragraphs"][0][:40] in prompt)

    no_quotes_prompt = carousel.build_prompt(a389)
    check("статті без цитат промпт каже про це прямо",
          "ЦИТАТ У СТАТТІ НЕМАЄ" in no_quotes_prompt)

    forced = carousel.build_prompt(a371, force=True)
    check("«запропонувати інакше» просить інший кут подачі",
          "ІНШОГО КУТА" in forced)

    # Довгий лонгрід ріжеться з чесною позначкою
    long_article = dict(a371, paragraphs=["а" * 500] * 40)
    cut = carousel.build_prompt(long_article)
    check("довгий текст обрізано і про це сказано",
          "текст статті обрізано" in cut and len(cut) < 20000, f"{len(cut)} символів")

    # ---------- 6. Токени доступу ----------
    print("\n— Токени сторінки —")
    from datetime import datetime, timedelta

    from handlers import storage
    storage.save_carousel_tokens({})

    t1 = carousel.token_for("Єлизавета Москвіна", 386403807)
    t2 = carousel.token_for("Єлизавета Москвіна", 386403807)
    check("той самий лінк не змінюється від кожного /carousel", t1 == t2)
    other = carousel.token_for("Іміра Борухова", 111)
    check("у кожної людини свій токен", other != t1)
    who = carousel.whois(t1)
    check("токен мапиться на людину",
          who and who["person"] == "Єлизавета Москвіна", str(who))
    check("чужий токен не пускає", carousel.whois("не-той-токен") is None)
    check("порожній токен не пускає", carousel.whois("") is None)

    stale = dict(storage.get_carousel_tokens())
    stale[t1] = dict(stale[t1],
                     at=(datetime.now() - timedelta(days=carousel.TOKEN_TTL_DAYS + 1)
                         ).isoformat(timespec="seconds"))
    storage.save_carousel_tokens(stale)
    check(f"токен старший за {carousel.TOKEN_TTL_DAYS} днів не працює",
          carousel.whois(t1) is None)
    fresh = carousel.token_for("Єлизавета Москвіна", 386403807)
    check("протухлий не перевикористовується — видається новий", fresh != t1)

    # ---------- 7. Лінк сторінки ----------
    print("\n— Лінк у бота —")
    saved = carousel.WEBAPP_URL
    carousel.WEBAPP_URL = "https://bot.example"
    try:
        check("лінк несе токен",
              carousel.carousel_url("abc") == "https://bot.example/carousel?k=abc",
              carousel.carousel_url("abc"))
        check("із новиною лінк відкриває її одразу",
              carousel.carousel_url("abc", "322371").endswith("&url=322371"),
              carousel.carousel_url("abc", "322371"))
    finally:
        carousel.WEBAPP_URL = saved
    check("без WEBAPP_URL кнопки немає, а не кнопка в нікуди",
          (lambda: (setattr(carousel, "WEBAPP_URL", ""),
                    carousel.carousel_url("abc"))[1])() is None)
    carousel.WEBAPP_URL = saved

    check("id новини витягується з повного URL",
          carousel.article_id_from(
              "https://nikvesti.com/news/public/322371-stan-budivli") == "322371")

    # ---------- 8. Кеш планів ----------
    print("\n— Кеш планів —")
    from handlers import storage as st2
    st2.save_carousel_plan("322371", {"plan": {"slides": []}, "at": "2026-08-17T10:00:00"})
    check("план дістається за id новини",
          st2.get_carousel_plan("322371") is not None)
    check("незнайома новина кеша не має", st2.get_carousel_plan("999999") is None)
    for i in range(st2.CAROUSEL_PLANS_MAX + 5):
        st2.save_carousel_plan(f"90{i:04d}",
                               {"plan": {}, "at": f"2026-08-17T10:{i:02d}:00"})
    kept = len(st2._read_state().get("carousel_plans", {}))
    check(f"кеш не росте нескінченно (кап {st2.CAROUSEL_PLANS_MAX})",
          kept <= st2.CAROUSEL_PLANS_MAX, f"{kept} записів")

    # ---------- 8a. Гроші видно в /aicost ----------
    print("\n— Розріз витрат по інструментах —")
    from datetime import datetime as _dt

    from handlers import ai_usage

    month = _dt.now().strftime("%Y-%m")

    class _Usage:
        input_tokens = 12000
        output_tokens = 900
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    carousel._record_usage(_Usage(), 386403807, "Єлизавета Москвіна")
    features = st2.get_ai_usage_features(month)
    check("виклик каруселі позначений інструментом", "carousel" in features,
          str(list(features)))
    check("токени каруселі порахувались",
          features.get("carousel", {}).get(carousel.PLAN_MODEL, {}).get("input") == 12000,
          str(features.get("carousel")))
    report = ai_usage.format_month_report(month)
    check("у /aicost є рядок про каруселі",
          report and "Каруселі для Instagram" in report,
          (report or "")[:200])
    check("витрати каруселі лягли і на людину",
          "Єлизавета" in (report or ""), (report or "")[:200])

    # ---------- 9. Фолбек-план без моделі ----------
    print("\n— План без агента (немає ключа / тест) —")
    fake = carousel.normalize_plan(carousel.fake_plan(a371), a371)
    check("чернетка з тексту статті теж починається з обкладинки",
          fake["slides"][0]["type"] == "cover")
    check("чернетка не порожня", len(fake["slides"]) >= 4, f"{len(fake['slides'])}")
    quote_slides = [s for s in fake["slides"] if s["type"] == "quote"]
    check("цитата у чернетці — справжня з тексту",
          all(s["quote"] in a371["quotes"] for s in quote_slides),
          str([s.get("quote", "")[:40] for s in quote_slides]))
    fake389 = carousel.normalize_plan(carousel.fake_plan(a389), a389)
    check("у статті без цитат чернетка не вигадує quote-слайдів",
          not any(s["type"] == "quote" for s in fake389["slides"]))

    print()
    if failures:
        print(f"Провалено: {len(failures)}")
        for f in failures:
            print(f"  • {f}")
        return 1
    print("Усі перевірки пройдені")
    return 0


if __name__ == "__main__":
    sys.exit(main())
