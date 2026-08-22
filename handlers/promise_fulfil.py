"""
Детектор ВИКОНАННЯ обіцянок: новина «боларди поставили» закриває обіцянку.

**Навіщо.** Питання Олега 04.08: «коли в норі з'явиться новина, що боларди
поставили, чи поміняється статус на виконано?» Відповідь була — ні, і це
головна дірка банку: він умів лише накопичуватись. Витяг ловить заяви про
МАЙБУТНЄ, а «боларди встановили» це констатація минулого, тож у банк вона не
потрапляла взагалі. Обіцянку могли виконати, а вона й далі стояла
простроченою і нагадувала про себе в канал.

**Як шукає.** Пре-фільтр безкоштовний і має ДВА джерела:

1. **спільна КАРТКА сутності** — збіг по картці, а не по тексту: «боларди
   біля зоопарку» і «обмежувальні стовпчики на Богоявленському» це різні
   рядки й одна картка;
2. **стаття, яку суддя ланцюга вже прив'язав** до цієї обіцянки пізнішою
   ревізією. Друге джерело з'явилось після живого кейсу 04.08: у статті
   «дорогу до Матвіївки відремонтували» було дев'ять сутностей і ЖОДНА не
   збіглася з предметом обіцянки — по картках пара не знаходилась, хоч
   зв'язок між ними вже існував;
3. **стаття, прив'язана до СУСІДА ПО ТЕМІ.** Тема — це «одна справа», і
   якщо новина закриває одне зобов'язання цієї справи, вона цілком може
   закривати й друге. Той самий кейс показав чому: про дорогу до Матвіївки
   у банку чотири записи в трьох темах, і новина про ремонт зв'язана лише з
   одним із них. Рішення однаково ухвалює суддя поштучно — це розширення
   пошуку, а не автоматика.

Доказом не є рівно одна стаття — ПЕРШОДЖЕРЕЛО обіцянки. Спершу відсікались
усі статті з ревізіями, і це вбивало саме той випадок, заради якого детектор
писався: пізніший матеріал цілком може і переказувати обіцянку, і повідомляти
про її виконання.

Модель кличеться тільки там, де кандидат уже є.

**Два рівні, обидва за рішенням Олега (04.08):**

- `high` — бот **ставить статус сам**. Обіцянка йде у фасет «Перевірені» з
  підписом, що закрив її Лис, і з лінком на новину, яка це доводить. Вона не
  зникає: зірвана чи виконана обіцянка — це факт, на який посилаються в
  наступному тексті;
- `medium` — фасет «Схоже, виконано» в апці **плюс сповіщення Каті**. Я
  пропонував не чіпати її стрічку, щоб не залити; Олег: «там нічо не
  зіллється, вона має відслідковувати сповіщення в нуль постійно».

**Замір першого прогону (04.08): 2 хибних із 5.** Обидві помилки одного крою —
та сама локація або та сама організація, але ІНША дія: «земляний вал навколо
колодязя в парку „Ліски"» закрито новиною про лави на Намиві, а «скорегувати
проєктно-кошторисну документацію» — новиною про завершення фізичних робіт.
Тому головна перевірка в промпті тепер не «чи про це новина», а «чи це САМЕ ТА
дія щодо САМЕ ТОГО об'єкта»; обидва випадки вписані туди дослівно.

**Чому суддя схильний казати «ні».** Хибно закрита обіцянка ЗНИКАЄ з черги —
тобто редакція перестає перевіряти те, що, можливо, не зробили. Пропущене
виконання коштує лише зайвого рядка. Тому в промпті прямо: краще `none`, ніж
`done` на порожньому місці, а заява самого обіцяльника про виконання ніколи
не дає `high`.

**Але «роботи почались» порожнім місцем не є** (рішення Олега 04.08 на кейсі
дороги до Матвіївки: обіцяли відремонтувати → вийшла новина «начался
ремонт»). Спершу правило вимагало доконаного факту, і такий кейс чесно падав
у `none`. Тепер зараховується й початок робіт на місці: журналісти зафіксували,
що справа зрушила, а якщо потім щось піде не так — обіцянку відкриють знову.
Межа проходить по МІСЦЮ, а не по паперу: техніка вийшла — так; оголосили
тендер, виділили кошти, затвердили проєкт — ні.

**Дві межі, за які детектор не заходить** (обидві заміряні 22.08 на
обіцянці 1972 «Забезпечити безперервне управління громадою»):

1. **Картка населеного пункту пари не робить.** Предметом тієї обіцянки
   записано ціле місто — «Південноукраїнськ», — тож будь-яка новина про
   Южноукраїнськ формально «про той самий обʼєкт»: вода на пляжах, ДТП,
   самокати, затримання за продаж зброї. Суддя 22 рази чесно сказав «ні» і
   на 22-й закрив обіцянку новиною про те, що адміністрація почала
   працювати. Раніше контейнер ловили підтипом ПЛЮС частотою, і поріг
   відсікав рівно одну картку в усій норі («Миколаїв»), а всі райцентри
   проходили як предмет. Тепер ознака одна — підтип; деталі й розклад
   влучань за джерелом пари — у pp.CONTAINER_PLACE_SUBTYPES.
2. **Риторику детектор не судить узагалі** — див. `rhetoric_only`.

**Відкат є завжди** — `/promise_reopen <id>`: статус назад у «чекаємо», ознаки
знімаються. Будь-яке автоматичне рішення мусить мати одну кнопку відкату,
інакше помилка ховає живу тему назавжди й мовчки.
"""

import asyncio
import os
import time

from handlers import bot_db, team_notifications
from handlers.helpers import escape_html
from handlers.notifier import notify_error
import entity_pipeline as ep
import promise_pipeline as pp
from handlers import promise_judge as pj

# Скільки днів свіжих статей дивимось за прогін. Тиждень: щогодинний прогін
# бере ті самі статті знову, але пари вже судились і повторно не платяться.
DEFAULT_DAYS = 7
# Скільки пар за раз. Стеля мусить із запасом перекривати ДЕННИЙ обсяг, бо
# прогонів тепер два на добу, а не двадцять чотири: на щогодинному розкладі
# недобрана пара поверталась за годину, а тут — за пів доби, і хвіст ріс би
# швидше, ніж розбирається. Замір 17.08 по серпню: у середньому 36 пар на
# добу, пік 76, плюс гілка ревізій і сусідів по темі (~11). Двісті — це
# приблизно вчетверо від піку, тобто навіть день із подвійним обсягом
# розбирається за один прогін.
MAX_PAIRS = 200
# Скільки тексту статті даємо судді. Виконання зазвичай у першому абзаці, а
# повний текст на кожну пару — це гроші без користі.
TEXT_CAP = 1800


def unfalsifiable(promise):
    """Обіцянка, у якої нема чим підтвердити ВИКОНАННЯ — отже, і закривати її
    автоматично нема чим.

    «Просувати інтереси Миколаївської області у державному бюджеті»,
    «продовжити підтримувати область», «забезпечити реалізацію розпочатих
    проєктів» — заяви без предмета, дати й критерію. Вони не просто
    неперевірні: вони МАГНІТ для хибних закриттів, бо будь-яка добра новина
    про область виглядає їх підтвердженням. У прогоні 04.08 одна така
    обіцянка (956) закрилась ТРИЧІ трьома різними новинами — про субвенцію на
    освіту, зарплати військових адміністрацій і харчування школярів.

    У черзі це клас `noproof` («перевірити нічим»), і він там саме тому, що
    рішення про такі заяви ухвалює людина.
    """
    return (promise or {}).get("verifiability") == "unfalsifiable"


def rhetoric_only(promise):
    """Риторика — заява без предмета, і закрити її може будь-яка добра новина.

    «Переконаний, що новостворена військова адміністрація працюватиме
    ефективно» (обіцянка 1972): предмета в ній немає, тому детектор
    послідовно шукав його по картці «Південноукраїнськ» — тобто по цілому
    місту — і 22 рази платив судді за відповідь «ні», поки на 22-й не
    закрив обіцянку новиною про те, що адміністрація почала працювати.

    З черги редакції риторика НЕ зникає: людина сама вирішує, писати про неї
    чи ні. Не зникає й межа між нею та обіцянкою — її ставить витяг полем
    `kind`, як і `modality`. Забороняється рівно одне — АВТОМАТИЧНЕ рішення
    по ній, бо ціна помилки несиметрична: хибне «виконано» ховає тему з
    черги, і робить це мовчки.

    Твін SQL-правила pp.FULFIL_SKIP_SQL. Тримається тут другим шаром не для
    краси: /promise_fulfil_test ходить повз пре-фільтр, а `kind` у записа
    буває проставлений уже після того, як пара потрапила в чергу.
    """
    return (promise or {}).get("kind") in pp.FULFIL_SKIP_KINDS


def too_early(verdict, promise, now=None):
    """«Зірвано» до того, як строк минув, — не помилка судді, а помилка типу.

    Обіцянка 543 («розширити програму ЖКГ для фінансування озеленення
    районними адміністраціями») має горизонт 31.12.2029: фінансування
    закладають на 2026–2029. Оголосити її зірваною в серпні 2026-го не можна
    ЖОДНИМИ доказами — просто тому, що строк іще йде. Це рахується з дати, а
    не з тексту, тож питати модель тут нема про що.

    Виняток — полярність `not_do`: для «не дамо приватизувати» порушенням є
    ДІЯ, і вона трапляється коли завгодно, зокрема задовго до строку.
    """
    if (verdict or {}).get("state") != "failed":
        return False
    if (promise or {}).get("polarity") == "not_do":
        return False
    deadline = (promise or {}).get("deadline")
    return bool(deadline) and int(deadline) > (now or int(time.time()))


def decides_itself(verdict):
    """Чи бот ставить статус САМ, без людини.

    Тільки «виконано» і тільки при `high`. «Зірвано» бот не ставить ніколи —
    і це не обережність, а різна природа доказу: виконання підтверджує ПОДІЯ
    («боларди поставили», «техніка вийшла»), а зрив довелось би виводити з
    ВІДСУТНОСТІ події, чого в тексті не буває. Суддя на цьому місці підміняє
    доказ обстановкою: 04.08 він закрив «розширити програму ЖКГ для
    фінансування озеленення» новиною «у бюджеті немає грошей на знесення
    аварійних дерев» — і сам же написав «не виконано», хоч про розширення
    програми там не сказано нічого.

    Ціна помилки несиметрична. Хибне «виконано» ховає тему з черги; хибне
    «зірвано» ще й дає редакції неправдивий факт, на який можна послатись у
    тексті. Тож `failed` завжди йде людині — у фасет і в сповіщення.
    """
    v = verdict or {}
    return v.get("state") == "done" and v.get("confidence") == "high"


def _load(since, limit):
    conn = ep.connect()
    try:
        pp.ensure_schema(conn)
        conn.autocommit = True
        with conn.cursor() as cur:
            pairs = pp.fulfil_candidates(cur, since, limit)
            if not pairs:
                return []
            cids = list({p["commitment_id"] for p in pairs})
            aids = list({p["article_id"] for p in pairs})
            cur.execute(f"SELECT {pp.COMMITMENT_COLS} FROM commitments c "
                        "WHERE c.id = ANY(%s)", (cids,))
            rows = {r["id"]: r for r in pp._rows(cur)}
            quotes = {}
            for rev in pp.revisions(cur, cids):
                quotes.setdefault(rev["commitment_id"], rev.get("quote") or "")
            cur.execute("SELECT id, coalesce(title_ua, title_ru), "
                        "       coalesce(text_ua, text_ru) "
                        "FROM articles WHERE id = ANY(%s)", (aids,))
            arts = {r[0]: {"title": r[1] or "", "text": (r[2] or "")[:TEXT_CAP]}
                    for r in cur.fetchall()}
    finally:
        conn.close()
    out = []
    for p in pairs:
        row, art = rows.get(p["commitment_id"]), arts.get(p["article_id"])
        if not row or not art or not art["text"]:
            continue
        out.append({
            "commitment_id": p["commitment_id"],
            "article_id": p["article_id"],
            "promise": {"title": row.get("title"),
                        "owner_text": row.get("owner_text"),
                        "deadline": row.get("deadline"),
                        "polarity": row.get("polarity"),
                        "verifiability": row.get("verifiability"),
                        # Не для судді, а для запобіжника rhetoric_only:
                        # у промпт це поле не йде.
                        "kind": row.get("kind"),
                        "quote": (quotes.get(p["commitment_id"]) or "")[:400]},
            "article": art,
        })
    return out


async def scan(days=DEFAULT_DAYS, limit=MAX_PAIRS, on_progress=None):
    """Прогін детектора.

    Повертає (закрито, у чергу, переглянуто, розклад вердиктів). Розклад —
    не прикраса: без нього «0 з 60» однаково читається і як чесна робота
    скупого судді, і як шістдесят мовчазних збоїв моделі.
    """
    if not bot_db.is_configured() or not os.environ.get("ANTHROPIC_API_KEY"):
        return 0, 0, 0, {}
    since = int(time.time()) - int(days) * 86400
    pairs = await asyncio.to_thread(_load, since, limit)
    if not pairs:
        return 0, 0, 0, {}

    sem = asyncio.Semaphore(pj.CONCURRENCY)
    done_n = 0

    async def one(p):
        nonlocal done_n
        async with sem:
            p["verdict"] = await pj.judge_fulfil(p["promise"], p["article"])
        done_n += 1
        if on_progress and done_n % 15 == 0:
            try:
                await on_progress(done_n, len(pairs))
            except Exception:
                pass
        return p

    judged = list(await asyncio.gather(*(one(p) for p in pairs)))

    def apply():
        closed, queued = [], []
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                for p in judged:
                    v = p.get("verdict")
                    # Збій моделі — НЕ рішення: пари немає в журналі, отже
                    # наступний прогін спитає знову. А ось відповідь «ні»
                    # рішенням Є, і її треба записати, інакше ці ж пари
                    # суддя судитиме щогодини вічно.
                    if not v:
                        continue
                    conf = v.get("confidence")
                    settled = (v.get("state") not in ("done", "failed")
                               or conf not in ("high", "medium")
                               or too_early(v, p["promise"])
                               or unfalsifiable(p["promise"])
                               or rhetoric_only(p["promise"]))
                    if settled:
                        pp.record_closure(cur, p["commitment_id"],
                                          p["article_id"],
                                          {**v, "state": "none"})
                        continue
                    auto = decides_itself(v)
                    if not pp.record_closure(cur, p["commitment_id"],
                                             p["article_id"], v, applied=auto):
                        continue          # цю пару вже судили
                    if auto:
                        pp.mark_checked(cur, p["commitment_id"], "Лис",
                                        outcome=v["state"],
                                        note=f"{v.get('why') or ''} "
                                             f"[матеріал {p['article_id']}]")
                        closed.append(p)
                    else:
                        queued.append(p)
            conn.commit()
        finally:
            conn.close()
        return closed, queued

    closed, queued = await asyncio.to_thread(apply)
    if queued:
        await asyncio.to_thread(_notify, queued)
    if closed:
        await asyncio.to_thread(_notify, closed, True)
    # Що саме закрито — у звіт. «Закрито 1» без назви це рядок, який нічого
    # не дає: людина не може ні перевірити рішення, ні відкотити його, бо не
    # знає, про яку обіцянку йдеться.
    scan.last_closed = closed
    scan.last_queued = queued
    breakdown = {}
    for p in judged:
        v = p.get("verdict")
        key = "збій" if not v else f"{v.get('state')}/{v.get('confidence')}"
        breakdown[key] = breakdown.get(key, 0) + 1
    return len(closed), len(queued), len(judged), breakdown


def _notify(queued, closed_by_bot=False):
    """Сповіщення Каті. Два приводи, і другий не менш важливий за перший:

    - `closed_by_bot=False` — ознака, якої бот не наважився застосувати:
      «Схоже, виконано», підтвердь або відхили;
    - `closed_by_bot=True` — бот УЖЕ закрив обіцянку сам. Про це теж треба
      сказати: рішення автоматичне, і людина мусить мати змогу його
      побачити й відкотити. Інакше банк тихо порожніє, а редакція не знає,
      чому теми зникають (Олег, 04.08: «а що він закрив, я навіть не бачу»).
    """
    from handlers.promises import _links_for
    from handlers.promise_app import _images_for

    conn = ep.connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            art_ids = {p["article_id"] for p in queued}
            links = _links_for(cur, art_ids)
            images = _images_for(cur, art_ids)
    finally:
        conn.close()
    for p in queued:
        v = p.get("verdict") or {}
        link = links.get(p["article_id"]) or {}
        word = "виконано" if v.get("state") == "done" else "зірвано"
        head = (f"Лис закрив як {word}" if closed_by_bot else f"Схоже, {word}")
        # meta — структуровані шматки для стрічки апки: подія про ОБІЦЯНКУ
        # має читатись інакше, ніж про таск (Олег, 11.08 — «неотличимо від
        # тасків»): підпис типу, назва обіцянки жирним, новина-доказ окремим
        # рядком із мініатюрою (og:image уже закешовано в норі — _images_for
        # нічого не тягне). Заголовок лишається повним — це фолбек для подій,
        # які читатимуть без meta
        meta = {
            "promise": True,
            "label": (f"Лис оновив обіцянку · {word}" if closed_by_bot
                      else f"Схоже, {word} — перевір"),
            "title": p["promise"]["title"],
            "news_title": link.get("title"),
            "image": images.get(p["article_id"]),
        }
        try:
            team_notifications.notify_safe(
                "promise_closed" if closed_by_bot else "promise_closure",
                f"{head}: {p['promise']['title']}",
                body=(v.get("why") or ""),
                url=link.get("url"),
                meta={k: val for k, val in meta.items() if val},
                object_type="promise",
                object_id=p["commitment_id"],
                # Одна стаття про одну обіцянку смикає раз.
                dedup_key=(f"promise_{'closed' if closed_by_bot else 'closure'}:"
                           f"{p['commitment_id']}:{p['article_id']}"))
        except Exception as e:
            print(f"promise_fulfil: сповіщення не пішло — {e}")


async def hourly(bot):
    """Щогодини о :40 — після витягу (:25) і сутностей попередньої години.

    Тихий: пише лише тоді, коли щось закрив. Опт-ін не потрібен — детектор
    нічого не розсилає в канал, а помилка відкатна.
    """
    try:
        closed, queued, _seen, _by = await scan()
        if closed or queued:
            print(f"банк тем: закрито за фактом виконання — {closed}, "
                  f"у чергу Каті — {queued}")
        # Авто-закриття — важливий сигнал (Олег, 04.08), тож іде ДВОМА
        # шляхами: у стрічку сповіщень апки (її бачать обидва менеджери) і
        # окремо в приват Олегу. Причина не в дублюванні: бот тут ухвалив
        # рішення САМ і прибрав тему з черги редакції, а таке не має
        # ставатись у місці, куди можна не зайти.
        got = getattr(scan, "last_closed", [])
        if got:
            await _tell_admin(bot, got)
    except Exception as e:
        await notify_error(bot, "детектор виконання обіцянок", e)


async def _tell_admin(bot, closed):
    from handlers.notifier import ADMIN_CHAT_ID
    from handlers.promises import _links_for

    def links():
        conn = ep.connect()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                return _links_for(cur, {p["article_id"] for p in closed})
        finally:
            conn.close()

    try:
        by_article = await asyncio.to_thread(links)
    except Exception:
        by_article = {}
    n = len(closed)
    # Заголовок мусить називати те, що СТАЛОСЬ. Суддя віддає два різні
    # вердикти — `done` і `failed`, — і другий закриває обіцянку як ЗІРВАНУ:
    # «закрив за фактом виконання» над висновком «не виконано» читається як
    # збій бота (скріншот Олега 04.08), хоч рішення було саме таким.
    kinds = {("виконано" if (p.get("verdict") or {}).get("state") == "done"
              else "зірвано") for p in closed}
    what = ("за фактом виконання" if kinds == {"виконано"}
            else "як зірвані" if kinds == {"зірвано"}
            else "за фактом перевірки")
    lines = [f"✅ <b>Лис закрив {n} "
             f"{pp.plural(n, 'обіцянку', 'обіцянки', 'обіцянок')} "
             f"{what}</b>", ""]
    for p in closed[:8]:
        v = p.get("verdict") or {}
        link = by_article.get(p["article_id"]) or {}
        word = "виконано" if v.get("state") == "done" else "зірвано"
        lines.append(f"• <b>{escape_html(p['promise']['title'] or '?')}</b>")
        if len(kinds) > 1:
            lines.append(f"  <b>{word}</b>")
        if v.get("why"):
            lines.append(f"  <i>{escape_html(v['why'])}</i>")
        if link.get("url"):
            lines.append(f"  <a href=\"{link['url']}\">"
                         f"{escape_html(link.get('title') or 'доказ')}</a>")
        lines.append(f"  Не згоден — /promise_reopen {p['commitment_id']}")
    try:
        await bot.send_message(ADMIN_CHAT_ID, "\n".join(lines)[:4000],
                               parse_mode="HTML",
                               disable_web_page_preview=True,
                               reply_markup=await _cards_keyboard(bot, closed))
    except Exception as e:
        print(f"promise_fulfil: не сказав адміну — {e}")


async def _cards_keyboard(bot, closed):
    """Кнопки «відкрити картку» — по одній на закриту обіцянку.

    Команда `/promise_reopen 543` у тексті лишається, але вона ВІДКОЧУЄ, а
    перше, що треба зробити з автоматичним рішенням, — подивитись на нього:
    ланцюг, цитати, лінк на кожен факт. Це і є картка в апці, тож шлях до неї
    має бути в один тап (Олег, 04.08: «пусть показывает мини-апп кнопку, при
    клике на которую я попаду в карточку обещания»).

    Лінк збирається тим самим резолвером, що в нагадуваннях; без нього
    кнопок просто немає, а повідомлення лишається корисним.
    """
    try:
        from handlers.helpers import app_link_with_param, resolve_app_link
        app_url, _ = await resolve_app_link(bot)
    except Exception as e:
        print(f"promise_fulfil: лінк апки не зібрався — {e}")
        return None
    if not app_url:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for p in closed[:8]:
        url = app_link_with_param(app_url, f"promise_{p['commitment_id']}")
        # Одна закрита — кнопка називає дію; кілька — назву обіцянки, бо
        # вісім однакових «Відкрити картку» не розрізнити.
        title = (p["promise"].get("title") or "").strip()
        label = ("Відкрити картку" if len(closed) == 1
                 else (title[:28] + "…") if len(title) > 29 else title or "Картка")
        rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows)


# ---------- Команди ----------

async def promise_fulfil_handler(update, context):
    """/promise_fulfil [днів] — прогнати детектор зараз."""
    from handlers.promises import _allowed

    if not _allowed(update):
        await update.message.reply_text("⛔ Тільки для редакції.")
        return
    args = context.args or []
    days = int(args[0]) if args and args[0].isdigit() else DEFAULT_DAYS
    msg = await update.message.reply_text(
        f"🦊 Шукаю ознаки виконання за {days} дн.…")

    async def progress(done, total):
        await msg.edit_text(f"🦊 Дивлюсь: {done} з {total}…")

    try:
        closed, queued, seen, by = await scan(days, on_progress=progress)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
        return
    if not seen:
        await msg.edit_text(
            f"🦊 За {days} дн. немає жодної свіжої новини про об'єкт, "
            f"якому щось обіцяли. Це нормально: збіг рахується по картці "
            f"сутності, а не по словах.")
        return
    # Розклад вердиктів у звіті обов'язковий: «0 з 60» без нього однаково
    # читається і як чесна робота скупого судді, і як шістдесят мовчазних
    # збоїв моделі.
    lines = " · ".join(f"{k}: {v}" for k, v in sorted(by.items()))
    broke = by.get("збій", 0)
    named = _named(getattr(scan, "last_closed", []), "Закрив",
                   getattr(scan, "last_queued", []))
    await msg.edit_text(
        f"🦊 <b>Ознаки виконання</b>\n\n"
        f"Перевірено пар: {seen}\n"
        f"Закрито ботом (впевнено): <b>{closed}</b>\n"
        f"Пішло Каті на підтвердження: <b>{queued}</b>\n\n"
        f"<code>{escape_html(lines)}</code>\n"
        + named
        + (f"\n⚠️ Модель не відповіла на {broke} — дивись логи Railway.\n"
           if broke else "")
        + f"\n<i>«none» означає, що новина про цей об'єкт є, але про виконання "
          f"не каже — суддя навмисно скупий. Закрите лежить у «Перевірені» з "
          f"лінком на новину-доказ, відкат — /promise_reopen &lt;id&gt;.</i>",
        parse_mode="HTML")


def _named(closed, word, queued=()):
    """Назвати обіцянки поіменно, з id для відкату. Без цього «закрито 1» —
    рядок, за яким людина нічого не може ні перевірити, ні відкотити."""
    out = []
    if closed:
        out.append(f"\n<b>{word}:</b>")
        for p in closed[:8]:
            out.append(f"• {escape_html(p['promise']['title'] or '?')}\n"
                       f"  <i>{escape_html((p.get('verdict') or {}).get('why') or '')}</i>\n"
                       f"  /promise_show {p['commitment_id']} · відкат "
                       f"/promise_reopen {p['commitment_id']}")
    if queued:
        out.append("\n<b>Каті на підтвердження:</b>")
        for p in queued[:8]:
            out.append(f"• {escape_html(p['promise']['title'] or '?')}\n"
                       f"  /promise_show {p['commitment_id']}")
    return "\n".join(out) + ("\n" if out else "")


async def promise_reopen_handler(update, context):
    """/promise_reopen <id> — повернути обіцянку в чергу."""
    from handlers.promises import _allowed

    if not _allowed(update):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Використання: /promise_reopen <id обіцянки>")
        return
    cid = int(args[0])
    who = (update.effective_user.full_name if update.effective_user else "—")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                ok = pp.reopen(cur, cid, who)
            conn.commit()
            return ok, row
        finally:
            conn.close()

    ok, row = await asyncio.to_thread(run)
    if not ok:
        await update.message.reply_text("🦊 Такої обіцянки немає.")
        return
    await update.message.reply_text(
        f"↩️ Повернув у чергу: <b>{escape_html((row or {}).get('title') or cid)}</b>",
        parse_mode="HTML")


# ---------- /promise_auto ----------
#
# Ревізія того, що бот вирішив САМ. До 04.08 автоматичні закриття було видно
# лише в момент прогону — одним повідомленням, яке легко проґавити. Олег,
# 04.08: «так таких обещаний куча было выполнений, за 2 дня, я тебе не писал
# просто». Тобто рішень накопичилось, а списку, у якому їх можна переглянути
# й відкотити гуртом, не було взагалі.
#
# Дві купи розділені навмисно. «Зірвано» бот віднині не ставить ніколи
# (decides_itself), тож усе, що він устиг закрити як зірване, — це рішення за
# правилом, від якого ми відмовились, і воно повертається однією кнопкою.
# «Виконано» лишається поштучним переглядом: там правило чинне, і гуртом
# скасовувати його немає підстав.

async def promise_auto_handler(update, context):
    """/promise_auto [виконано|зірвано] — що бот закрив сам."""
    from handlers.promises import _allowed, _links_for, _clip

    if not _allowed(update):
        return
    arg = (context.args or [""])[0].lower()
    state = ("done" if arg.startswith("викон") else
             "failed" if arg.startswith("зірв") or arg.startswith("зирв") else None)

    def run():
        conn = ep.connect()
        try:
            conn.autocommit = True
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                rows = pp.auto_closures(cur, limit=1000, state=state)
                links = _links_for(cur, {r["article_id"] for r in rows})
                failed = len(pp.auto_closures(cur, limit=1000, state="failed"))
            return rows, links, failed
        finally:
            conn.close()

    rows, links, failed = await asyncio.to_thread(run)
    if not rows:
        await update.message.reply_text(
            "🦊 Бот нічого не закривав сам — або все вже переглянуто.")
        return
    done_n = sum(1 for r in rows if r["state"] == "done")
    lines = [f"🦊 <b>Що Лис закрив сам</b> — {len(rows)} "
             f"{pp.plural(len(rows), 'обіцянка', 'обіцянки', 'обіцянок')}",
             f"виконано {done_n} · зірвано {len(rows) - done_n}", ""]
    for r in rows[:25]:
        link = links.get(r["article_id"]) or {}
        word = "виконано" if r["state"] == "done" else "ЗІРВАНО"
        lines.append(f"• <b>{escape_html(r['title'] or '?')}</b>")
        lines.append(f"  {word} · {escape_html((r['why'] or '')[:160])}")
        if link.get("url"):
            lines.append(f"  <a href=\"{link['url']}\">"
                         f"{escape_html(link.get('title') or 'доказ')}</a>")
        lines.append(f"  /promise_show {r['commitment_id']} · "
                     f"/promise_reopen {r['commitment_id']}")
    if len(rows) > 25:
        lines.append(f"\n<i>…і ще {len(rows) - 25}. Фільтр: "
                     f"/promise_auto виконано | зірвано</i>")
    markup = None
    if failed:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            f"Повернути в чергу всі, закриті як зірвані ({failed})",
            callback_data="pau:undo_failed")]])
        lines.append("\n<i>«Зірвано» бот віднині не ставить сам — доказом зриву "
                     "довелось би вважати відсутність події. Ті, що встиг, "
                     "повертаються однією кнопкою.</i>")
    await update.message.reply_text(_clip("\n".join(lines)), parse_mode="HTML",
                                    disable_web_page_preview=True,
                                    reply_markup=markup)
    # ПОВНИЙ список — файлом. У чат влазить два десятки, а рішень бота
    # накопичується більше, і розбирають їх поза ботом: назад приїжджає .txt
    # із рядками `reopen <id>` (той самий шлях, що /roles_audit і
    # /entity_junk). Готовий рядок стоїть у кожному записі, тож правити файл
    # означає лише викинути зайве.
    if len(rows) <= 8:
        return
    import io
    buf = io.StringIO()
    buf.write("# promises-fix\n")
    buf.write("# Лишай рядки reopen для тих, які бот закрив ДАРЕМНО;\n"
              "# решту видали. Файл кинь Лису в приват — покаже, що зробить.\n\n")
    for r in rows:
        link = links.get(r["article_id"]) or {}
        word = "виконано" if r["state"] == "done" else "ЗІРВАНО"
        buf.write(f"# [{word}] {r['title'] or '?'}\n")
        buf.write(f"#   строк: {pp.fmt_date(r['deadline']) if r['deadline'] else '—'}"
                  f" · закрито: {pp.fmt_date(r['created']) if r['created'] else '—'}\n")
        if r.get("why"):
            buf.write(f"#   підстава: {r['why']}\n")
        if link.get("title"):
            buf.write(f"#   доказ: {link['title']}\n")
        if link.get("url"):
            buf.write(f"#   {link['url']}\n")
        buf.write(f"reopen {r['commitment_id']}\n\n")
    await update.message.reply_document(
        document=io.BytesIO(buf.getvalue().encode("utf-8")),
        filename=f"promise_auto_{len(rows)}.txt",
        caption=(f"🦊 Усі {len(rows)}, які Лис закрив сам — із підставою й "
                 f"лінком на доказ.\n\nВикинь рядки тих, де бот має рацію, "
                 f"решту надішли мені файлом назад — поверну в чергу пакетом."))


async def promise_auto_callback(update, context):
    from handlers.promises import _allowed

    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    who = (update.effective_user.full_name if update.effective_user else "—")
    await query.edit_message_text("🦊 Повертаю…")

    def run():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            with conn.cursor() as cur:
                rows = pp.auto_closures(cur, limit=1000, state="failed")
                for r in rows:
                    pp.reopen(cur, r["commitment_id"], who)
            conn.commit()
            return rows
        finally:
            conn.close()

    try:
        rows = await asyncio.to_thread(run)
    except Exception as e:
        await query.edit_message_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
        return
    head = (f"↩️ Повернув у чергу {len(rows)} "
            f"{pp.plural(len(rows), 'обіцянку', 'обіцянки', 'обіцянок')}, "
            f"закритих як зірвані.")
    names = "\n".join(f"• {escape_html(r['title'] or r['commitment_id'])}"
                      for r in rows[:15])
    await query.edit_message_text(f"{head}\n\n{names}", parse_mode="HTML")


# ---------- /promise_fulfil_test ----------
#
# Питання Олега 04.08 на реальній парі: обіцянка про дорогу до Матвіївки
# (319402) і пізніша новина «начался ремонт дороги» (321200) — «чому бот не
# зарахував?». Причин рівно три (стаття поза вікном · не збіглася картка
# сутності · суддя сказав «ні»), і мовчазний нуль у звіті їх не розрізняє.
# Ця команда проганяє ОДНУ пару і показує кожен крок.

async def promise_fulfil_test_handler(update, context):
    """/promise_fulfil_test <id обіцянки> <id|URL новини> — чому не зарахувало."""
    from handlers.promises import _allowed
    from handlers.helpers import extract_article_id

    if not _allowed(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Використання: /promise_fulfil_test <id обіцянки> <id або URL новини>")
        return
    cid = int(args[0])
    raw = args[1]
    aid = extract_article_id(raw) if "/" in raw else (raw if raw.isdigit() else None)
    if not aid:
        await update.message.reply_text("Не видно id новини.")
        return
    aid = int(aid)
    msg = await update.message.reply_text("🦊 Розбираю пару…")

    def load():
        conn = ep.connect()
        try:
            pp.ensure_schema(conn)
            conn.autocommit = True
            with conn.cursor() as cur:
                row = pp.get(cur, cid)
                cur.execute(
                    "SELECT published, coalesce(title_ua, title_ru), "
                    "       coalesce(text_ua, text_ru), region "
                    "FROM articles WHERE id = %s", (aid,))
                art = cur.fetchone()
                quote = ""
                for rev in pp.revisions(cur, [cid]):
                    quote = rev.get("quote") or ""
                    break
                # Чи бачить їх ПРЕ-ФІЛЬТР: спільна картка сутності
                cur.execute(
                    "SELECT e.id, coalesce(e.name_ua, e.name_ru), e.kind, "
                    "       coalesce(e.subtype, '') FROM entities e "
                    "JOIN article_entities ae ON ae.entity_id = e.id "
                    "WHERE ae.article_id = %s AND (e.id = %s OR e.id IN "
                    "  (SELECT entity_id FROM commitment_objects WHERE commitment_id = %s))",
                    (aid, (row or {}).get("subject_entity_id") or 0, cid))
                shared = cur.fetchall()
                cur.execute("SELECT count(*) FROM article_entities WHERE article_id = %s",
                            (aid,))
                art_entities = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM commitment_revisions "
                            "WHERE commitment_id = %s AND article_id = %s", (cid, aid))
                is_source = bool(cur.fetchone())
                cur.execute("SELECT state, confidence, why, applied FROM promise_closures "
                            "WHERE commitment_id = %s AND article_id = %s", (cid, aid))
                already = cur.fetchone()
        finally:
            conn.close()
        return row, art, quote, shared, art_entities, is_source, already

    try:
        row, art, quote, shared, art_entities, is_source, already = \
            await asyncio.to_thread(load)
    except Exception as e:
        await msg.edit_text(f"❌ Не вийшло: {type(e).__name__}: {e}")
        return
    if not row:
        await msg.edit_text(f"🦊 Обіцянки {cid} немає.")
        return
    if not art:
        await msg.edit_text(f"🦊 Статті {aid} немає в норі — інкремент дзеркала "
                            f"її ще не забрав.")
        return

    published, title, text, region = art
    days = (int(time.time()) - int(published or 0)) // 86400
    lines = [f"🦊 <b>{escape_html(row.get('title') or cid)}</b>",
             f"проти: {escape_html(title or aid)}", ""]
    lines.append(f"{'✅' if days <= DEFAULT_DAYS else '⚠️'} Новині {days} дн. "
                 f"— щогодинний прогін дивиться {DEFAULT_DAYS}"
                 + ("" if days <= DEFAULT_DAYS else f"; тут потрібен "
                    f"/promise_fulfil {days + 1}"))
    lines.append(f"{'✅' if region == 1 else '❌'} Регіон: {region} (беремо лише 1)")
    lines.append(f"{'❌' if is_source else '✅'} "
                 + ("це стаття, з якої обіцянку й записали — доказом бути не може"
                    if is_source else "стаття не є джерелом самої обіцянки"))
    # Спільна картка міста парою НЕ є (див. pp.CONTAINER_PLACE_SUBTYPES), і
    # діагностика мусить це показувати окремо — інакше вона каже «спільна
    # картка ✅» там, де пре-фільтр пари не зробить, тобто бреше рівно в тому
    # місці, заради якого її й викликають.
    subject = [(i, n) for i, n, k, st in shared
               if not (k == "place" and st in pp.CONTAINER_PLACE_SUBTYPES)]
    background = [(i, n) for i, n, k, st in shared
                  if (k == "place" and st in pp.CONTAINER_PLACE_SUBTYPES)]
    if subject:
        lines.append("✅ Спільна картка: "
                     + ", ".join(escape_html(n or "—") for _i, n in subject[:5]))
    else:
        lines.append(f"❌ <b>Спільної картки-предмета немає</b> — пре-фільтр цю "
                     f"пару не побачить. Сутностей у статті: {art_entities}"
                     + ("" if art_entities else " (стаття ще не розібрана "
                        "сутнісним шаром)"))
    if background:
        lines.append("➖ Спільне лише тло: "
                     + ", ".join(escape_html(n or "—") for _i, n in background[:5])
                     + " — населений пункт чи район відповідає на питання «де», "
                       "а не «що», і пари не робить")
    if rhetoric_only(row):
        lines.append("➖ Це <b>риторика</b> (kind=rhetoric) — детектор такі "
                     "обіцянки не судить узагалі: предмета в них немає, тож "
                     "підтвердити їх може будь-яка добра новина")
    if already:
        lines.append(f"ℹ️ Пару вже судили: {already[0]}/{already[1]} — "
                     f"{escape_html(already[2] or '')}")

    if not text:
        lines.append("\n❌ У статті немає тексту в норі — судити нема що.")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")
        return

    lines.append("\n<i>Питаю суддю (нічого не записую)…</i>")
    await msg.edit_text("\n".join(lines), parse_mode="HTML")
    v = await pj.judge_fulfil(
        {"title": row.get("title"), "owner_text": row.get("owner_text"),
         "deadline": row.get("deadline"), "quote": quote[:400]},
        {"title": title or "", "text": (text or "")[:TEXT_CAP]})
    lines.pop()
    if not v:
        lines.append("\n❌ <b>Суддя не відповів</b> — дивись логи Railway.")
    else:
        mark = {"done": "✅", "failed": "🚫", "none": "➖"}.get(v.get("state"), "•")
        lines.append(f"\n{mark} <b>{v.get('state')}/{v.get('confidence')}</b>: "
                     f"{escape_html(v.get('why') or '')}")
        blocked = ("риторика" if rhetoric_only(row)
                   else "обіцянку нема чим підтвердити (unfalsifiable)"
                   if unfalsifiable(row)
                   else "строк ще не минув" if too_early(v, row) else None)
        if blocked:
            lines.append(f"<i>Але записаний він не був би: {blocked}.</i>")
        elif v.get("state") == "done" and v.get("confidence") == "high":
            lines.append("<i>Такий вердикт закрив би обіцянку сам.</i>")
        elif v.get("state") in ("done", "failed"):
            lines.append("<i>Такий вердикт пішов би Каті на підтвердження.</i>")
        else:
            lines.append("<i>«none» — обіцянка лишається в черзі.</i>")
    await msg.edit_text(_clip_local("\n".join(lines)), parse_mode="HTML")


def _clip_local(text, limit=4000):
    return text if len(text) <= limit else text[:limit - 1] + "…"
