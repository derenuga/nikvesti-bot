"""
Тест ремонту розмітки беку (handlers/tg_html.py) і сторінки копіювання
(handlers/back_export.py).

**Що сталось у проді (12.08.2026).** Аліна натиснула «Написати бек» і
отримала текст, у якому вся розмітка видна голими тегами:
`<a href="https://nikvesti.com/...">в місті не висадили жодного дерева</a>`.
Олег на те саме питання отримав нормально відформатований бек. Різниця —
одна дужка: у тексті Аліни модель написала закриття тега як `</a}` замість
`</a>`. Telegram відхилив ВСЕ повідомлення, спрацював фолбек «шлемо як plain
text» — і фолбек показав сирий HTML. Тобто збій моделі в одному символі псує
весь бек, і це видно лише тому, кому не пощастило.

**Що перевіряємо тут (кожен пункт — та сама пастка):**

- реальний уривок беку Аліни з `</a}` після ремонту стає валідним HTML і
  зберігає ВСІ п'ять лінків (головна регресія);
- інші способи зламати анкор, які модель уже показувала: одинарні лапки,
  href без лапок, зайві атрибути, незакритий і зайвий `</a>`;
- неекранований `&` і «менше <5%» — те, на чому падає parse_mode у звичайних
  відповідях, не тільки в беках;
- ремонт ІДЕМПОТЕНТНИЙ: коректний бек (як у Олега) не змінюється взагалі —
  інакше лікування було б небезпечнішим за хворобу;
- фолбек to_plain НІКОЛИ не показує сирий тег: анкор стає «текст (URL)»;
- сторінка /back будується без винятків, віддає всі лінки і не ламається на
  фігурних дужках CSS/JS (шаблон іде через .format).

Мереж і БД не треба — усе локальне.
"""

import html
import re
import sys
from html.parser import HTMLParser

from handlers import tg_html

# Теги, які Telegram приймає в HTML-режимі (Bot API).
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
           "code", "pre", "blockquote", "tg-spoiler", "span"}


class _TgParse(HTMLParser):
    """Груба модель парсера Telegram: незакритий/зайвий/невідомий тег = 400."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in TG_TAGS:
            self.errors.append(f"невідомий тег <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"закриття </{tag}> без відкриття")
            return
        self.stack.pop()

    def handle_data(self, data):
        # Сирий `<` у тексті — саме те, на чому Telegram каже can't parse entities.
        if "<" in data:
            self.errors.append(f"неекранований < у тексті: {data[:30]!r}")


def tg_accepts(text):
    """Список причин, чому Telegram відхилив би текст. Порожній — прийме."""
    p = _TgParse()
    p.feed(text)
    p.close()
    errs = list(p.errors)
    if p.stack:
        errs.append(f"незакриті теги: {p.stack}")
    for m in re.finditer(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,7}|#\d{1,6}|#x[0-9a-fA-F]{1,6});)", text):
        errs.append(f"голий & на позиції {m.start()}")
    return errs


# Реальний уривок беку Аліни: п'ять лінків, у передостанньому — `</a}`.
BROKEN = (
    'Нагадаємо, у 2025-2026 роках КП «Миколаївські парки» неодноразово ставало '
    'приводом для критики через якість роботи. Ще влітку 2025-го з\'ясувалося, що '
    'весною <a href="https://nikvesti.com/news/public/305697-v-mikolaievi-v-2025-ne-'
    'visadzheno-zhodnogo-dereva">в місті не висадили жодного дерева</a>, а на '
    'проспекті Центральному засохла бірючина — кущі, якими КП <a href="https://'
    'nikvesti.com/news/public/308127-u-mykolaivi-zasoh-zhivii-parkan-z-biruchini">'
    'хотіло замінити чавунний паркан</a> і закупило за 1,5 мільйона гривень.\n\n'
    'У грудні 2025-го підрядник <a href="https://nikvesti.com/news/public/311983-'
    'pidryadnyk-poshkodyv-korinnya">пошкодив коріння семи дерев</a>.\n\n'
    'Втім уже наприкінці березня в КП <a href="https://nikvesti.com/news/public/'
    '316578-mykolaivski-parky-finansuvannya">визнали брак фінансування на висадку '
    'дерев</a} цієї весни, а у квітні виникла суперечка навколо тополь, які '
    '«Миколаївські парки» <a href="https://nikvesti.com/news/public/317249-u-'
    'mykolaievi-hochut-znesti-topoli">хотіли знести як аварійні</a>, хоча '
    'екоінспекція наполягала на фаховій експертизі.'
)

# Бек, який дійшов до Олега нормально — контроль ідемпотентності.
GOOD = (
    'Нагадаємо, депутат Миколаївської міськради Федір Панченко — голова бюджетної '
    'комісії — неодноразово потрапляв у новини.\n\n'
    'У листопаді 2022 року Панченко <a href="https://nikvesti.com/news/politics/'
    '269222">заявляв, що підривачі мемориалу</a> мають відповісти перед законом.'
)

FAILS = []


def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def n_links(text):
    return len(re.findall(r'<a href="[^"]+">', text))


print("\n1. Реальний бек Аліни: `</a}` (те, через що вона побачила сирі теги)")
errs = tg_accepts(BROKEN)
check("до ремонту Telegram його НЕ приймає", bool(errs), "а мав би відхилити")
fixed = tg_html.repair(BROKEN)
errs = tg_accepts(fixed)
check("після ремонту приймає", not errs, str(errs))
check("усі 5 лінків на місці", n_links(fixed) == 5, f"знайшов {n_links(fixed)}")
check("текст анкора не зʼївся",
      "визнали брак фінансування на висадку дерев</a>" in fixed)
check("крива дужка не лишилась у тексті", "</a}" not in fixed and "}" not in fixed)

print("\n2. Інші способи зламати анкор, які модель уже показувала")
cases = [
    ("одинарні лапки", "текст <a href='https://nikvesti.com/news/x'>анкор</a> далі", 1),
    ("href без лапок", '<a href=https://nikvesti.com/news/x>анкор</a>', 1),
    ("зайві атрибути", '<a href="https://nikvesti.com/news/x" target="_blank" rel="nofollow">анкор</a>', 1),
    ("закриття без «>»", 'ось <a href="https://nikvesti.com/news/x">анкор</a і далі', 1),
    ("пробіли в закритті", '<a href="https://nikvesti.com/news/x">анкор< / a>', 1),
    ("незакритий анкор", 'рядок з <a href="https://nikvesti.com/news/x">анкором\nнаступний абзац', 1),
    ("зайве закриття", 'текст без лінка</a> далі', 0),
    ("два анкори, перший не закрито",
     '<a href="https://nikvesti.com/a">один <a href="https://nikvesti.com/b">два</a>', 2),
]
for name, raw, want in cases:
    out = tg_html.repair(raw)
    errs = tg_accepts(out)
    check(f"{name}: валідний HTML", not errs, str(errs))
    check(f"{name}: лінків {want}", n_links(out) == want, f"вийшло {n_links(out)}")

print("\n3. Символи, на яких падає parse_mode у звичайних відповідях")
for name, raw in [("голий &", "Іванов & Партнери виграли тендер"),
                  ("«менше <5%»", "частка Discover менше <5% трафіку"),
                  ("<Прізвище>", "підстав <Прізвище> у шаблон")]:
    out = tg_html.repair(raw)
    check(f"{name}: приймається", not tg_accepts(out), str(tg_accepts(out)))
    check(f"{name}: текст читається", html.unescape(tg_html.strip_tags(out)) == raw)

print("\n4. Ідемпотентність: коректний бек (варіант Олега) не чіпаємо")
check("Telegram приймав його і до ремонту", not tg_accepts(GOOD), str(tg_accepts(GOOD)))
check("ремонт нічого не змінив", tg_html.repair(GOOD) == GOOD)
check("подвійний ремонт = одинарний", tg_html.repair(tg_html.repair(BROKEN)) == fixed)

print("\n5. Фолбек to_plain: сирих тегів на екран не виходить ніколи")
plain = tg_html.to_plain(fixed)
check("жодного тега", "<a" not in plain and "</a>" not in plain and "&lt;" not in plain)
check("URL лишились видимими", plain.count("https://nikvesti.com") == 5)
check("анкор став «текст (URL)»", "в місті не висадили жодного дерева (https://" in plain)
check("на битому вході теж без тегів", "<a" not in tg_html.to_plain(BROKEN))

print("\n6. Сторінка /back: HTML абзацами і копіювання")
from handlers import back_export  # noqa: E402  (після tg_html — так само в проді)

body = back_export.to_article_html(BROKEN + "\n\n📊 Джерело даних: архів новин nikvesti.com")
check("три абзаци беку стали <p>", body.count("<p>") == 3, f"вийшло {body.count('<p>')}")
check("усі лінки доїхали", n_links(body) == 5, f"знайшов {n_links(body)}")
check("службовий підпис бота прибрано", "Джерело даних" not in body)
page = back_export._PAGE.format(body=body, source=html.escape(body), topic="«парки» · ")
check("шаблон сторінки будується", "<title>" in page and "{body}" not in page)
check("у сторінці ті самі 5 лінків", n_links(page) == 5, f"знайшов {n_links(page)}")
check("код у <textarea> екранований", "&lt;a href=" in page)
check("є обидві кнопки копіювання",
      "copyRich()" in page and "copyCode()" in page and "text/html" in page)
check("фігурні дужки CSS/JS вижили", "@media (prefers-color-scheme: dark) {" in page
      and "ClipboardItem({" in page)

print("\n" + ("❌ ВПАЛО: " + ", ".join(FAILS) if FAILS else "✅ Усі перевірки пройдено"))
sys.exit(1 if FAILS else 0)
