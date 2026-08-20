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
    quotes_doc = a371["quotes"]

    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "attribution": "Олег Звягін"}], quotes)
    got = slides[0].get("quote", "")
    check("цитата за номером береться з тексту статті",
          got and got.rstrip(".") in quotes[0], got[:60])
    check("чесна цитата не додає зауважень", notes == [], str(notes))

    # Хвіст атрибуції на слайд не йде: поруч уже стоїть поле «хто сказав»
    check("хвіст «— запитав Олег Звягін» зрізано",
          "запитав" not in got and "Звягін" not in got, got[-60:])
    check("провідне тире прямої мови теж прибрано",
          not got.startswith("—"), got[:30])

    # Ім'я з хвоста стає підписом, коли агент лишив поле порожнім
    slides, _ = carousel.apply_plan_quotes([{"type": "quote", "quote_index": 1}], quotes)
    check("ім'я з хвоста підставилось у «хто сказав»",
          slides[0].get("attribution") == "Олена Іванова",
          str(slides[0].get("attribution")))

    # «зазначила вона» нікого не називає — підпису з нього бути не може
    speech, who = carousel.split_speech(
        "— Ми це впровадимо, як тільки знайдемо можливість, — зазначила вона.")
    check("«зазначила вона» не стає підписом", who == "", repr(who))
    check("а сама фраза лишається цілою",
          speech == "Ми це впровадимо, як тільки знайдемо можливість.", speech)

    # Цитата з документа: зовнішні «ялинки» знімаються, бо слайд малює свою лапку
    speech, _ = carousel.split_speech(quotes_doc[0])
    check("зовнішні «ялинки» цитати з документа зняті",
          not speech.startswith("«") and not speech.endswith("»"), speech[:40])
    check("текст документа не постраждав",
          speech.rstrip(".") in quotes_doc[0], speech[:60])

    # Скорочення ВИКИДАННЯМ слів — так працює редактор із довгою цитатою
    long_q = ("Я мер Миколаєва і я вважаю, що буду завжди виконувати свої "
              "обіцянки. Але найголовніше, я впевнений, це мікрорайон Намив")
    short = ("Я мер Миколаєва. Буду завжди виконувати обіцянки. "
             "Найголовніше — це мікрорайон Намив")
    check("зайві слова можна викинути, пунктуацію на шві поправити",
          carousel.fit_excerpt(short, long_q) == short,
          repr(carousel.fit_excerpt(short, long_q)))
    check("дописане слово не проходить",
          carousel.fit_excerpt("Я мер Миколаєва і обіцяю мільйон гривень", long_q) is None)
    check("переставлені слова не проходять",
          carousel.fit_excerpt("мікрорайон Намив це найголовніше я мер", long_q) is None)

    # Заперечення викидати не можна — це не скорочення, а протилежний сенс
    neg = "Не буде ремонту, поки не знайдуть кошти на нього"
    check("викинуте «не» перед збереженим словом відхиляється",
          carousel.fit_excerpt("буде ремонту поки не знайдуть кошти", neg) is None)
    check("а викинути цілу фразу разом із її «не» можна",
          carousel.fit_excerpt("поки не знайдуть кошти на нього", neg) is not None)

    # Дослівний фрагмент приймається (по СЛОВАХ, а не по символах: обрізок
    # посеред слова — це вже не цитата)
    piece = " ".join(carousel.split_speech(quotes[0])[0].split()[:9])
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "quote_excerpt": piece}], quotes)
    check("дослівний фрагмент приймається",
          slides[0]["quote"].rstrip(".") in piece, slides[0]["quote"][:60])

    # Фрагмент із іншими пробілами — теж свій текст, має пройти
    spaced = "  ".join(piece.split(" "))
    slides, _ = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0, "quote_excerpt": spaced}], quotes)
    check("зайві пробіли у фрагменті не роблять із нього вигадку",
          slides[0]["quote"].replace("  ", " ").rstrip(".") in piece.replace("  ", " "),
          slides[0]["quote"][:60])

    # ВИГАДКА: фрагмента в оригіналі немає
    slides, notes = carousel.apply_plan_quotes(
        [{"type": "quote", "quote_index": 0,
          "quote_excerpt": "ми знесемо цей пам'ятник до кінця тижня"}], quotes)
    check("вигаданий фрагмент відкинуто, стоїть текст зі статті",
          slides[0]["quote"].rstrip(".") in quotes[0], slides[0]["quote"][:60])
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

    # ---------- 3a. Правка одного слайда підказкою ----------
    print("\n— Правка слайда за підказкою —")

    # Головне тут — МЕЖА: хоч би що повернула модель, далі дозволених
    # текстових полів воно не проходить. Тому підсовуємо відповідь, яка
    # намагається переписати все: тип слайда, фото, кегль і сусідів.
    class _Msg:
        class usage:
            input_tokens = output_tokens = 0
            cache_read_input_tokens = cache_creation_input_tokens = 0

        def __init__(self, payload):
            self.content = [type("B", (), {"type": "tool_use", "input": payload})()]

    async def fake_call(**kwargs):
        return _Msg({
            "title": "Свято було в Тернополі",
            "body": "Мер Миколаєва поїхав на День міста до Тернополя.",
            "note": "Прибрав «сусідів» — Тернопіль на заході, ми на півдні.",
            # усе нижче агент віддавати не має права
            "type": "photo", "photo_index": 2, "fontScale": 3,
            "scheme": 3, "slides": [{"type": "cover", "title": "чуже"}],
        })

    import handlers.ai_messages as ai
    saved_create = ai.async_client.messages.create
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test"
    ai.async_client.messages.create = fake_call
    try:
        slide = {"type": "text", "title": "Гучне свято — у сусідів",
                 "body": "Тернопіль три дні гуляв День міста.", "kicker": ""}
        fixed, note, _ = asyncio.run(carousel.revise_slide(
            a377, slide, "Тернопіль не сусід Миколаєва, переформулюй",
            [(1, {"type": "cover", "title": "інший слайд"})]))
    finally:
        ai.async_client.messages.create = saved_create
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    check("правка міняє тексти слайда",
          fixed.get("title") == "Свято було в Тернополі", str(fixed.get("title")))
    check("агент пояснює людині, що змінив", "Тернопіль" in note, note)
    check("тип слайда змінити не може", "type" not in fixed, str(fixed))
    check("фото і кадр недосяжні", not {"photo_index", "fontScale", "scheme"} & set(fixed),
          str(sorted(fixed)))
    check("сусідні слайди недосяжні", "slides" not in fixed, str(sorted(fixed)))
    check("повертаються ЛИШЕ дозволені поля",
          set(fixed) <= set(carousel.REVISABLE_FIELDS) | {"quote"},
          str(sorted(fixed)))

    # Режим «додай ще слайд»: тут тип і фото обирати МОЖНА (слайда ж немає),
    # але сусідні слайди — так само недосяжні
    async def fake_new(**kwargs):
        return _Msg({
            "type": "photo", "photo_index": 1, "caption": "Мер серед гостей",
            "note": "Додав кадр із самого свята.",
            "slides": [{"type": "cover", "title": "чуже"}], "fontScale": 3,
        })

    os.environ["ANTHROPIC_API_KEY"] = "test"
    ai.async_client.messages.create = fake_new
    try:
        made, note2, _ = asyncio.run(carousel.revise_slide(
            a377, None, "", [(1, {"type": "cover", "title": "є"})], mode="new"))
    finally:
        ai.async_client.messages.create = saved_create
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    check("новий слайд може мати свій тип", made.get("type") == "photo", str(made))
    check("і своє фото зі статті", made.get("photo_index") == 1, str(made))
    check("а сусідні слайди все одно недосяжні", "slides" not in made, str(sorted(made)))
    check("кегль і схема лишаються за людиною",
          not {"fontScale", "scheme"} & set(made), str(sorted(made)))
    check("агент пояснює й новий слайд", "кадр" in note2.lower(), note2)

    # Фото поза списком не має стати битим слайдом
    async def fake_bad_photo(**kwargs):
        return _Msg({"type": "photo", "photo_index": 99, "caption": "х",
                     "note": "n"})

    os.environ["ANTHROPIC_API_KEY"] = "test"
    ai.async_client.messages.create = fake_bad_photo
    try:
        bad, _, _ = asyncio.run(carousel.revise_slide(a377, None, "", mode="new"))
    finally:
        ai.async_client.messages.create = saved_create
        if saved_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key
    check("неіснуюче фото не приїде у слайд", bad.get("photo_index") is None,
          str(bad))

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
          all(any(s["quote"].rstrip(".") in q for q in a371["quotes"])
              for s in quote_slides),
          str([s.get("quote", "")[:40] for s in quote_slides]))
    fake389 = carousel.normalize_plan(carousel.fake_plan(a389), a389)
    check("у статті без цитат чернетка не вигадує quote-слайдів",
          not any(s["type"] == "quote" for s in fake389["slides"]))

    # ---------- 10. Чи знайде підпис /stat ----------
    #
    # Сенс перевірок: підпис, написаний цілком своїми словами, красивий і
    # мертвий — пост вийде, а /stat його не знайде, бо в стрічці Instagram
    # посилання на статтю немає і зіставляти доводиться по смислу.
    print("\n— Підпис, який знайде /stat —")
    from handlers import stat_instagram

    named = ("Кінотеатр «Іскра» у Миколаєві не обстежували з 2019 року. "
             "У травні частина фасаду просто впала. Коштів на протиаварійні "
             "роботи не виділяли — але за охорону платили щороку.")
    blind = ("Сім років будівля стоїть без жодного обстеження. "
             "У травні впала частина фасаду.")
    r_named = carousel.caption_reach(named, a371)
    r_blind = carousel.caption_reach(blind, a371)
    check("підпис із назвами й датою /stat знайде сам",
          r_named["verdict"] == "ok", str(r_named))
    check("підпис без предмета й міста сам не знаходиться",
          r_blind["verdict"] != "ok", str(r_blind))
    check("бракуючі слова названі, і власні назви першими",
          r_blind["missing"] and r_blind["missing"][0][:1].isupper(),
          str(r_blind["missing"]))
    check("показуємо слова, а не основи (людині «Миколаєві», не «микола»)",
          any(w in f"{a371['title']} {a371['description']}"
              for w in r_blind["missing"]),
          str(r_blind["missing"]))
    check("порожній підпис не знайдеться взагалі",
          carousel.caption_reach("", a371)["verdict"] == "lost")

    # Та сама арифметика, що в /stat: інакше гаудж світив би зеленим там, де
    # матчер не знаходить нічого
    sig = {"title": a371["title"], "lead": a371["description"]}
    check("оцінка рахується тим самим кодом, що матчинг /stat",
          carousel.caption_reach(named, a371)["score"]
          == stat_instagram.caption_findability(named, sig)["score"])

    # Записаний підпис як ДРУГА сигнатура: допис, що дослівно повторює
    # скопійований текст, має знаходитись навіть коли з заголовком і лідом
    # у нього спільного мало
    own_words = ("Сім років ніхто не перевіряв. Аварійна будівля в центрі "
                 "міста, а гроші йшли лише на охорону. Гортай карусель.")
    plain = stat_instagram.make_scorer(sig)
    withhint = stat_instagram.make_scorer(dict(sig, caption=own_words))
    check("без записаного підпису такий допис у сірій зоні",
          plain(own_words) < stat_instagram.ACCEPT, f"{plain(own_words):.3f}")
    check("із записаним підписом той самий допис знаходиться впевнено",
          withhint(own_words) >= stat_instagram.ACCEPT,
          f"{withhint(own_words):.3f}")
    check("чужий допис записаний підпис не витягує",
          withhint("Погода в Миколаєві на вихідні: очікується дощ") <
          stat_instagram.ACCEPT)
    check("без записаного підпису поведінка матчера не змінилась",
          plain(named) == stat_instagram._score(
              stat_instagram._norm_tokens(f"{sig['title']} {sig['lead']}"), named))

    # ---------- 11. Додатковий слайд ----------
    print("\n— «Додай слайд» бачить, що вже зайнято —")
    slides = [
        {"type": "cover", "kicker": "Миколаїв",
         "title": "Будівлю «Іскри» не обстежували з 2019 року"},
        {"type": "text",
         "body": "У травні з покинутого кінотеатру обвалилася частина фасаду."},
        {"type": "quote", "quote": a371["quotes"][0], "quote_index": 0},
    ]
    fresh = carousel.unused_material(a371, slides)
    check("непрозвучале рахується, а не вгадується", len(fresh) >= 3,
          f"{len(fresh)}")
    check("сказане на слайдах у список не потрапляє",
          not any("обвалилася частина фасаду" in p for p in fresh),
          str([p[:60] for p in fresh]))
    check("порядок авторський, як у статті",
          [a371["paragraphs"].index(p) for p in fresh]
          == sorted(a371["paragraphs"].index(p) for p in fresh))

    others = list(enumerate([dict(s, photo_index=None) for s in slides], 1))
    prompt = carousel.build_revise_prompt(a371, None, "", others, mode="new")
    check("зайнята цитата позначена", "ВЖЕ на слайді 3" in prompt,
          prompt[:0])
    check("у промпті є список непрозвучалого",
          "ЩЕ НЕ ПРОЗВУЧАЛО" in prompt)
    check("агенту сказано, куди сяде слайд",
          "перед закликом" in prompt)
    check("сусідні слайди показані полями, а не обрізаною склейкою",
          "body: У травні з покинутого кінотеатру обвалилася частина фасаду."
          in prompt)
    check("у режимі правки списку непрозвучалого немає (він там ні до чого)",
          "ЩЕ НЕ ПРОЗВУЧАЛО" not in carousel.build_revise_prompt(
              a371, slides[1], "коротше", others, mode="fix"))
    cap_prompt = carousel.build_revise_prompt(
        a371, {"caption": blind}, "", others, mode="caption")
    check("переписуючи підпис, агент бачить ПОТОЧНИЙ текст",
          blind in cap_prompt)
    check("і бачить, яких слів у ньому бракує",
          "НЕ ЗНАЙДЕ" in cap_prompt)

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
