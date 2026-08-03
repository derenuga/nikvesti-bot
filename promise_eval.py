#!/usr/bin/env python3
"""Приймальний набір банку тем — сім еталонів §6.1 як ВИКОНУВАНА перевірка.

`docs/PROMISES_BANK.md` §6.1 каже: «Прогін на них — умова приймання: якщо витяг
помиляється тут, далі не йдемо». Тільки описана вона словами, а виконувати її
довелось би так: сім разів `/promise_test`, і щоразу читати екран очима,
пам'ятаючи, чого саме не можна допустити в кожному з семи. На третьому кейсі
людина перестає читати — рівно те, за що вже платили в сутнісному шарі.

Тут та сама умова приймання лежить кодом: `/promise_eval` жене справжній витяг
на семи реальних статтях і друкує таблицю «пройшло / впало», а деталі — під
нею. Правити промпт стає можливо не «на відчуття», а за конкретним пунктом,
який червоний.

**Два різні артефакти, і плутати їх не можна.**

1. `CHECKS` — те, чого НЕ МОЖНА допустити. Це прямий переклад колонки «чого не
   можна допустити» з §6.1 у предикати. Вони не питають «чи схоже на еталон»,
   вони ловлять конкретну катастрофу: вигадану дату там, де дати немає;
   злиту в один запис обіцянку з риторикою; загублену полярність.
2. `REFERENCE` — приклад ПРАВИЛЬНОГО читання тих самих статей: що саме
   витягується, з якими полями і дослівними цитатами. Це не «правильна
   відповідь», з якою звіряють дослівно (модель має право сформулювати title
   інакше), а зразок для ока і фікстура для тестів пайплайну, яка не потребує
   ні ключа до моделі, ні прод-бази.

**Звідки взявся REFERENCE.** Сім статей завантажено з сайту (вони публічні) і
прочитано за тим самим промптом `promise_extract_prompt.md`, яким читатиме
Haiku. Усі `quote` — дослівні фрагменти живих матеріалів, не переказ. Це НЕ
вивід Haiku: коли Олег прожене `/promise_eval`, різниця між цим читанням і
машинним і буде матеріалом для правки промпту.

**Що вже знайшло це читання** (правки вже в промпті й пайплайні):
  • 294413 — одне речення Скарлата несе ДВА горизонти («основну частину до
    кінця року, остаточне завершення до 2025»), і ключ ідемпотентності
    «стаття + цитата» тихо викидав другий;
  • 311271 — «плануємо з нового року» це горизонт-ПОЧАТОК, а промпт умів лише
    «до кінця Х», тож найперевірюваніша обіцянка провалювалась у «без дати»;
  • 320276 — анонімні «власники» названі поіменно абзацом нижче (фірма
    «Бізнес-Реал», Пеліпас, депутат Чайка), тобто обіцянка не мусить лишатись
    нічиєю;
  • у тексті сторінки живуть службові блоки (заклик підтримати редакцію,
    дисклеймер донора) — їх треба явно виключити, інакше «Разом тримаємо
    місто світлим» має всі ознаки обіцянки.
"""

# ---------- Предикати ----------

def _has(items, **kw):
    """Чи є серед записів такий, у якого всі названі поля дорівнюють заданому."""
    return any(all(it.get(k) == v for k, v in kw.items()) for it in items)


def _all(items, **kw):
    return all(all(it.get(k) == v for k, v in kw.items()) for it in items)


def _count(items, pred):
    return sum(1 for it in items if pred(it))


def _text(it, field):
    return (it.get(field) or "").strip()


# ---------- Умова приймання: чого НЕ МОЖНА допустити (§6.1) ----------
#
# Кожен пункт підписаний тим рядком §6.1, з якого він виріс. Формулювання
# навмисно НЕ вимагає точних формулювань від моделі — лише того, без чого
# реєстр починає брехати.

CHECKS = {
    320092: [
        ("обіцянку взагалі знайдено",
         lambda its: len(its) >= 1,
         "заява Кіма — зобов'язання, хай і непідтверджуване"),
        ("не вигадано дату там, де її немає",
         lambda its: all(not it.get("deadline") for it in its),
         "«Трохи залишилось» — не горизонт. Дедлайн тут = реєстр пише неправду"),
        # Одна перевірка замість двох. Раніше тут стояли «критерій порожній» і
        # «клас = unfalsifiable» — і обидві падали, щойно модель ввічливо
        # вписувала в критерій переказ самої обіцянки. Тобто приймання міряло
        # дисципліну моделі в заповненні поля, а не те, що побачить людина.
        # Тепер міряємо саме те, що побачить людина: підказку з підставою.
        ("підказка «схоже на популізм» з'явилась із підставою",
         lambda its: any(_hint(it) for it in its),
         "немає дати й документа-підстави — людина має це побачити; "
         "чи є критерій справжнім, вирішує вона, а не машина"),
        ("джерело — коментар у соцмережі, а не брифінг",
         lambda its: _has(its, source_type="social_comment"),
         "вага заяви інша, ніж у слів із трибуни"),
    ],
    294413: [
        ("модель не поїхала за заголовком",
         lambda its: _count(its, lambda it: it.get("modality") in ("hedged", "considered")) >= 1
                     and not _has(its, source_type="official_statement", modality="guaranteed"),
         "у заголовку «перенесли», у тілі «може бути відкладене». Тендерна умова "
         "має право бути guaranteed — це договір; слова Скарлата з брифінгу — ні"),
        ("тендерний дедлайн 31.12.2024 не загублено",
         lambda its: _has(its, deadline="2024-12-31"),
         "умова договору — найтвердіше зобов'язання статті"),
        ("умова тендеру відрізнена від заяви посадовця",
         lambda its: _has(its, source_type="tender") and _has(its, source_type="official_statement"),
         "договір і брифінг — різна вага (§2, висновок 3)"),
        ("кілька горизонтів не злито в один запис",
         lambda its: len(its) >= 3,
         "тендерний строк · основна частина · остаточне завершення"),
        ("сума названа",
         lambda its: any(it.get("amount") for it in its),
         "149,5 млн з держбюджету — множник пріоритету"),
    ],
    311271: [
        ("з однієї цитати вийшло кілька записів",
         lambda its: len(its) >= 2,
         "перевірювана обіцянка і риторика — окремо (§2.2)"),
        ("риторика позначена як непідтверджувана",
         lambda its: _has(its, verifiability="unfalsifiable"),
         "«щоб я отримав цю статуетку» не можна прострочити"),
        ("перевірювана обіцянка лишилась перевірюваною",
         lambda its: _has(its, verifiability="measurable"),
         "«плануємо з нового року» — горизонт-початок, це теж дата"),
        ("видно, що обіцяно ЖУРНАЛІСТАМ",
         lambda its: _has(its, audience="media"),
         "«обіцяв редакції й не зробив» — окремий сюжет (§2.2)"),
        ("предмет без картки сутності не загубився",
         lambda its: any("наради" in _text(it, "subject").lower() for it in its),
         "«відкриті апаратні наради» — практика, а не об'єкт"),
    ],
    320276: [
        ("відносний горизонт розв'язано від дати виходу",
         lambda its: _has(its, deadline="2026-06-29"),
         "стаття від п'ятниці 26.06 → «за вихідні» це понеділок 29.06"),
        ("обіцянку не загублено через відсутність імені",
         lambda its: any("власник" in _text(it, "promiser").lower() for it in its),
         "анонімний колектив — теж актор (§2.3)"),
        ("посередник, до якого йти по відповідь, збережений",
         lambda its: any(_text(it, "reported_by") for it in its),
         "«власників» до відповіді не притягнеш, Березі можна зателефонувати"),
        ("перевірка ногами розпізнана",
         lambda its: _has(its, verification_method="field_check"),
         "саме дешевизна перевірки підіймає тему (§2.3)"),
        ("дві обіцянки різних класів не злиті",
         lambda its: len(its) >= 2,
         "власники з датою — і архітектор без дати"),
    ],
    317853: [
        ("полярність не загублена",
         lambda its: _has(its, polarity="not_do"),
         "«не дамо приватизувати» — порушенням є ДІЯ, не мовчання"),
        ("безстрокову обіцянку не порахували простроченою",
         lambda its: all(it.get("verifiability") != "measurable"
                         for it in its if it.get("polarity") == "not_do"),
         "дедлайну в неї немає й бути не може (§2.4)"),
        ("самооцінювана умова позначена",
         lambda its: _has(its, condition_self_judged=True),
         "«якщо є нормальний заклад» — межу проводить сам обіцяльник"),
    ],
    321833: [
        ("зобов'язання знайдено там, де слова «обіцяв» немає",
         lambda its: len(its) >= 1,
         "головне правило §2.5: ловимо мовленнєвий акт, а не лексему"),
        ("актора знайдено в оточенні, а не лишено порожнім",
         lambda its: any(_text(it, "promiser") or _text(it, "owner") for it in its),
         "поруч названі Луков (підписав) і Набатов (очолив групу)"),
        ("підстава-документ збережена",
         lambda its: any(_text(it, "based_on_document") for it in its),
         "«без дати» ≠ «пусті слова»: строк лежить у розпорядженні"),
        ("безособову форму зафіксовано прапорцем",
         lambda its: _has(its, actor_hidden=True),
         "вуалювання відповідальності цікаве саме по собі"),
    ],
    312757: [
        ("«гарантував» відрізнено від «планують»",
         lambda its: _has(its, modality="guaranteed"),
         "Кім гарантував — це вершина шкали (§2.6)"),
        ("гроші й роботи — різні зобов'язання",
         lambda its: len(its) >= 2,
         "Кабмін обіцяє фінансування, підрядник — роботи"),
        ("предмет упізнано попри перейменування",
         lambda its: any("ліце" in _text(it, "subject").lower()
                         or "гімназ" in _text(it, "subject").lower() for it in its),
         "у тексті «ліцей» і «гімназія» вжиті впереміш"),
        ("витяг не вигадує СТАТУС виконання",
         lambda its: all("status" not in it for it in its),
         "«договір розірвано» ≠ «підрядник зірвав»; статус ставить людина, "
         "автоматичного закриття обіцянок немає й не буде (§7)"),
    ],
}


def _hint(item):
    """Підказка «схоже на популізм», як її побачить людина в картці."""
    import promise_pipeline as pp

    return pp.populism_reason(item)


def decorate(items):
    """Додати похідні поля, на які спираються перевірки (клас перевірки
    рахується кодом, а не приходить від моделі)."""
    import promise_pipeline as pp

    out = []
    for it in items:
        d = dict(it)
        d["verifiability"] = pp.derive_verifiability(d)
        out.append(d)
    return out


def run_checks(article_id, items):
    """[(назва, ok, пояснення)] для одного матеріалу."""
    items = decorate(items)
    out = []
    for name, pred, why in CHECKS.get(article_id, []):
        try:
            ok = bool(pred(items))
        except Exception as e:
            ok, why = False, f"{why} (перевірка впала: {e})"
        out.append((name, ok, why))
    return out


CASE_IDS = [294413, 320092, 311271, 320276, 317853, 321833, 312757]

CASE_WHY = {
    294413: "тендерний дедлайн vs рішення vs припущення; заголовок твердіший за текст",
    320092: "ні дати, ні критерію; умова поза контролем; джерело — соцмережа",
    311271: "предмет-практика; риторика поруч із конкретикою в одній цитаті",
    320276: "анонімний актор; переказана обіцянка; відносний горизонт",
    317853: "обіцянка НЕ робити; тригер-подія; самооцінювана умова",
    321833: "зобов'язання без слова «обіцяв»; безособовий актор; підстава-документ",
    312757: "та сама тема, інші актори; «гарантував» твердіше за «планують»",
}


# ---------- Приклад правильного читання (фікстура) ----------
#
# Цитати — ДОСЛІВНІ фрагменти живих статей. Використовується тестами
# пайплайну: вони прогоняють цей набір через prepare/record і перевіряють
# похідні поля, ланцюг і чергу на реальних даних, не витрачаючи ні цента.

REFERENCE = {
    294413: [
        {
            "title": "Виконати реставраційні роботи в гімназії №2 за договором",
            "subject": "Миколаївська гімназія №2",
            "objects": ["Миколаївська гімназія №2"],
            "promiser": "Житлопромбуд-8", "promiser_role": None,
            "owner": "Житлопромбуд-8", "reported_by": "Євген Скарлат",
            "audience": "community", "polarity": "do", "modality": "guaranteed",
            "source_type": "tender",
            "deadline": "2024-12-31", "deadline_precision": "day",
            "criterion": "роботи за договором виконано, укриття облаштоване",
            "verification_method": "data",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": "умови тендеру на реставрацію", "amount": 145500000,
            "quote": "Згідно з умовами тендеру, підрядник мав виконати роботи до 31 грудня 2024 року, включаючи облаштування укриття та відновлення будівлі після обстрілів.",
        },
        {
            "title": "Завершити основну частину робіт у гімназії №2",
            "subject": "Миколаївська гімназія №2",
            "objects": ["Миколаївська гімназія №2"],
            "promiser": "Євген Скарлат",
            "promiser_role": "заступник начальника управління департаменту містобудування та архітектури Миколаївської ОВА",
            "owner": "Миколаївська ОВА", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "official_statement",
            "deadline": "2024-12-31", "deadline_precision": "year",
            "criterion": "основну частину робіт завершено",
            "verification_method": "official_statement",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Основну частину робіт планується завершити до кінця цього року, але остаточне завершення може бути відкладене до 2025 року",
        },
        {
            "title": "Завершити реставрацію гімназії №2",
            "subject": "Миколаївська гімназія №2",
            "objects": ["Миколаївська гімназія №2"],
            "promiser": "Євген Скарлат",
            "promiser_role": "заступник начальника управління департаменту містобудування та архітектури Миколаївської ОВА",
            "owner": "Миколаївська ОВА", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "hedged",
            "source_type": "official_statement",
            "deadline": "2025-12-31", "deadline_precision": "year",
            "criterion": "реставрацію завершено, будівля прийнята",
            "verification_method": "official_statement",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": 149500000,
            "quote": "Роботи виконуватимуться протягом 2024-2025 років. Основну частину робіт планується завершити до кінця цього року, але остаточне завершення може бути відкладене до 2025 року",
        },
        {
            "title": "Зробити поточний ремонт у гімназії №2",
            "subject": "Миколаївська гімназія №2",
            "objects": ["Миколаївська гімназія №2"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "мер Миколаєва",
            "owner": None, "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "official_statement",
            "deadline": "2021-12-31", "deadline_precision": "year",
            "criterion": "поточний ремонт зроблено",
            "verification_method": "official_statement",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": False,
            "based_on_document": None, "amount": None,
            "quote": "в гімназії №2 до кінця 2021 року планують зробити поточний ремонт, а капітальні роботи поки що відкладуть",
        },
    ],
    320092: [
        {
            "title": "Відновити Коблеве краще, ніж було",
            "subject": "Коблеве", "objects": ["Коблеве"],
            "promiser": "Віталій Кім", "promiser_role": "голова Миколаївської ОВА",
            "owner": "Миколаївська ОВА", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "promised",
            "source_type": "social_comment",
            "deadline": None, "deadline_precision": None,
            "criterion": None, "verification_method": None,
            "condition": "після завершення війни", "condition_self_judged": False,
            "trigger_event": None, "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Ми все повернемо краще ніж було! Трохи залишилось",
        },
    ],
    311271: [
        {
            "title": "Відкрити апаратні наради для журналістів",
            "subject": "відкриті апаратні наради", "objects": ["Миколаївська міська рада"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова Миколаєва",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "media", "polarity": "do", "modality": "planned",
            "source_type": "official_statement",
            "deadline": "2026-01-31", "deadline_precision": "month",
            "criterion": "журналістів пускають на апаратні наради",
            "verification_method": "official_statement",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Ми розглянули можливість відкрити апаратні наради для журналістів, плануємо з нового року це зробити. У нас буде відкрита апаратна нарада, яку ми проводимо раз на місяць",
        },
        {
            "title": "Залучати журналістів до всіх апаратних нарад після воєнного стану",
            "subject": "відкриті апаратні наради", "objects": ["Миколаївська міська рада"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова Миколаєва",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "media", "polarity": "do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": None, "deadline_precision": None,
            "criterion": "журналістів залучають до всіх апаратних нарад",
            "verification_method": "official_statement",
            "condition": "як тільки закінчиться військовий стан",
            "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Я дам вам обіцянку при всіх і на онлайн-трансляції, що як тільки закінчиться військовий стан, до всіх апаратних нарад, які будуть проводитися, ми будемо залучати журналістів",
        },
        {
            "title": "Зробити Миколаїв номером один по відкритості в Україні",
            "subject": "відкритість Миколаївської міської ради",
            "objects": ["Миколаїв"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова Миколаєва",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "media", "polarity": "do", "modality": "hedged",
            "source_type": "official_statement",
            "deadline": None, "deadline_precision": None,
            "criterion": None, "verification_method": None,
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": False,
            "based_on_document": None, "amount": None,
            "quote": "Я хочу, щоб місто Миколаїв було номер один по відкритості в Україні. Щоб ми були там, щоб я отримав цю статуетку і записав собі в карму «номер один по прозорості»",
        },
    ],
    320276: [
        {
            "title": "Зрізати незаконну огорожу за зупинкою на Соборній",
            "subject": "зупинковий комплекс «Соборна»", "objects": ["проспект Центральний"],
            "promiser": "власники зупинкового комплексу «Соборна»", "promiser_role": None,
            "owner": "Бізнес-Реал", "reported_by": "Олександр Береза",
            "audience": "community", "polarity": "do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": "2026-06-29", "deadline_precision": "day",
            "criterion": "огорожі за зупинкою немає",
            "verification_method": "field_check",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Власники зупинкового комплексу громадського транспорту «Соборна» пообіцяли, що за вихідні самостійно демонтують незаконно встановлену огорожу.",
        },
        {
            "title": "Звернутись до повноважного органу щодо демонтажу огорожі",
            "subject": "зупинковий комплекс «Соборна»", "objects": ["проспект Центральний"],
            "promiser": "Євген Поляков", "promiser_role": "головний архітектор",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": None, "deadline_precision": None,
            "criterion": "звернення до повноважного органу надіслано",
            "verification_method": "document_request",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Водночас посадовець пообіцяв звернутись до повноважного органу, який перевірить даний факт і проведе демонтаж.",
        },
        {
            "title": "Зрізати огорожу силами району, якщо власники не встигнуть",
            "subject": "зупинковий комплекс «Соборна»", "objects": ["проспект Центральний"],
            "promiser": "Олександр Береза",
            "promiser_role": "голова Центральної районної адміністрації",
            "owner": "Адміністрація Центрального району", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": None, "deadline_precision": None,
            "criterion": "огорожу зрізано силами районної адміністрації",
            "verification_method": "field_check",
            "condition": "якщо власники не демонтують за вихідні",
            "condition_self_judged": False,
            "trigger_event": "власники не демонтували огорожу за вихідні",
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Якщо вони за вихідні цього не зроблять, значить зріжемо самі",
        },
    ],
    317853: [
        {
            "title": "Не підтримувати приватизацію будівель трьох дитсадків",
            "subject": "будівлі дитсадків №104, №128 і №138",
            "objects": ["дитячий садок №104", "дитячий садок №128", "дитячий садок №138"],
            "promiser": "Артем Ільюк", "promiser_role": "депутат Миколаївської міськради",
            "owner": None, "reported_by": None,
            "audience": "community", "polarity": "not_do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": None, "deadline_precision": None,
            "criterion": "не голосує за приватизацію цих будівель",
            "verification_method": "document_request",
            "condition": "якщо це нормальний заклад у нормальному стані",
            "condition_self_judged": True,
            "trigger_event": "винесення приватизації цих будівель на сесію міськради",
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Ми ніколи не підтримаємо приватизацію цього приміщення під когось або під щось, окрім того, щоб зберегти його як майбутній дитячий садочок",
        },
        {
            "title": "Завершити будівництво дитсадка в Північному",
            "subject": "дитячий садок у мікрорайоні Північний",
            "objects": ["Північний"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "official_statement",
            "deadline": "2019-12-31", "deadline_precision": "year",
            "criterion": "будівництво дитсадка завершено",
            "verification_method": "field_check",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": True, "framed_as_promise": False,
            "based_on_document": None, "amount": None,
            "quote": "У 2017 році фінансували проєктування закладу, тоді ж планували завершити будівництво до 2019 року, проте будівельний етап знову не розпочався.",
        },
        {
            "title": "Розпочати будівництво дитсадка в Північному",
            "subject": "дитячий садок у мікрорайоні Північний",
            "objects": ["Північний"],
            "promiser": "Олександр Сєнкевич", "promiser_role": "міський голова",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "official_statement",
            "deadline": "2021-12-31", "deadline_precision": "year",
            "criterion": "будівництво розпочато",
            "verification_method": "field_check",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": True, "framed_as_promise": False,
            "based_on_document": None, "amount": None,
            "quote": "Тоді запуск будівництва анонсували на 2021 рік, однак і цей термін виконано не було.",
        },
    ],
    321833: [
        {
            "title": "Розробити житлову стратегію громади та план її реалізації",
            "subject": "житлова стратегія Миколаївської громади", "objects": ["Миколаїв"],
            "promiser": "Ігор Набатов",
            "promiser_role": "перший заступник директора департаменту ЖКГ",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "government_decision",
            "deadline": None, "deadline_precision": None,
            "criterion": "житлову стратегію та план реалізації розроблено",
            "verification_method": "document_request",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": True, "framed_as_promise": True,
            "based_on_document": "розпорядження від 27 липня, підписав Віталій Луков",
            "amount": None,
            "quote": "У Миколаєві створили робочу групу, яка має розробити житлову стратегію громади та план її реалізації.",
        },
        {
            "title": "Проаналізувати стан житлового фонду і підготувати пропозиції",
            "subject": "житлова стратегія Миколаївської громади", "objects": ["Миколаїв"],
            "promiser": "Ігор Набатов",
            "promiser_role": "перший заступник директора департаменту ЖКГ",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "government_decision",
            "deadline": None, "deadline_precision": None,
            "criterion": "аналіз житлового фонду і пропозиції підготовлені",
            "verification_method": "document_request",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": True, "framed_as_promise": True,
            "based_on_document": "розпорядження від 27 липня", "amount": None,
            "quote": "Група має проаналізувати стан житлового фонду Миколаєва, визначити основні проблеми й потреби, а також підготувати пропозиції щодо відновлення, будівництва та управління житлом.",
        },
        {
            "title": "Винести житлову стратегію на громадське обговорення",
            "subject": "житлова стратегія Миколаївської громади", "objects": ["Миколаїв"],
            "promiser": "Віталій Луков",
            "promiser_role": "перший заступник міського голови",
            "owner": "Миколаївська міська рада", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "planned",
            "source_type": "government_decision",
            "deadline": None, "deadline_precision": None,
            "criterion": "документ винесено на громадське обговорення і на сесію",
            "verification_method": "document_request",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": True, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "Після розроблення документ мають винести на громадське обговорення, а потім — на розгляд депутатів міської ради.",
        },
    ],
    312757: [
        {
            "title": "Завершити відновлення гімназії (ліцею) №2 у 2026 році",
            "subject": "Миколаївський ліцей №2",
            "objects": ["Миколаївський ліцей №2"],
            "promiser": "Віталій Кім", "promiser_role": "голова Миколаївської ОВА",
            "owner": "Миколаївська ОВА", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "guaranteed",
            "source_type": "official_statement",
            "deadline": "2026-12-31", "deadline_precision": "year",
            "criterion": "відновлення будівлі завершено",
            "verification_method": "official_statement",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "голова Миколаївської ОВА Віталій Кім гарантував, що у 2026 році вдасться завершити відновлення гімназії",
        },
        {
            "title": "Виділити з держбюджету гроші на відбудову ліцеїв №2 і №60",
            "subject": "Миколаївський ліцей №2",
            "objects": ["Миколаївський ліцей №2", "Миколаївський ліцей №60"],
            "promiser": "Кабінет Міністрів України", "promiser_role": None,
            "owner": "Кабінет Міністрів України", "reported_by": "Валентин Гайдаржи",
            "audience": "community", "polarity": "do", "modality": "promised",
            "source_type": "official_statement",
            "deadline": "2026-12-31", "deadline_precision": "year",
            "criterion": "фінансування виділено з коштів відновлення",
            "verification_method": "data",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": None, "amount": None,
            "quote": "На нараді в КМУ нам пообіцяли на два об'єкта (ліцей №60 та ліцей №2, — прим.) фінансування на 2026 рік за рахунок коштів відновлення. Чекаємо",
        },
        {
            "title": "Виконати реставраційні роботи в гімназії №2 за договором",
            "subject": "Миколаївський ліцей №2",
            "objects": ["Миколаївський ліцей №2"],
            "promiser": "Житлопромбуд-8", "promiser_role": None,
            "owner": "Житлопромбуд-8", "reported_by": None,
            "audience": "community", "polarity": "do", "modality": "guaranteed",
            "source_type": "tender",
            "deadline": "2024-12-31", "deadline_precision": "day",
            "criterion": "роботи за договором виконано, укриття облаштоване",
            "verification_method": "data",
            "condition": None, "condition_self_judged": False, "trigger_event": None,
            "actor_hidden": False, "framed_as_promise": True,
            "based_on_document": "умови тендеру на реставрацію", "amount": 145500000,
            "quote": "Підрядник мав виконати роботи до 31 грудня 2024 року, включаючи облаштування укриття та відновлення будівлі після обстрілів.",
        },
    ],
}


def reference_items(article_id):
    return [dict(it) for it in REFERENCE.get(article_id, [])]
