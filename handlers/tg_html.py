"""
Ремонт HTML-розмітки, яку пише модель, перед відправкою в Telegram.

НАВІЩО (інцидент 12.08.2026, скрін Аліни): у беку модель написала закриття
тега як `</a}` замість `</a>`. Telegram відхилив усе повідомлення
("can't parse entities"), спрацював фолбек «шлемо як plain text» — і
журналістка отримала бек, у якому ВСЯ розмітка видна голими тегами:
`<a href="https://nikvesti.com/...">в місті не висадили жодного дерева</a>`.
Тобто одна крива дужка від моделі псує весь текст, і це видно лише тому,
кому не пощастило.

Два запобіжники, обидва детерміновані (промпт такого не гарантує — модель
стохастична, а ремонт коштує нуль):

  1. repair() — лагодимо типові збої розмітки ДО відправки: криве закриття
     тега, одинарні лапки чи їх відсутність у href, зайві атрибути,
     неекранований `&` і `<`, незакритий чи зайвий `</a>`.

  2. to_plain() — якщо після ремонту Telegram усе одно відмовився, фолбек
     більше не показує сирі теги: анкори стають «текст (URL)», решта тегів
     знімається. Гірше за клікабельний лінк, але читається як текст і
     копіюється разом з адресою — на відміну від `<a href="…">`.

Теги, які Telegram приймає (Bot API, HTML-режим): b/strong, i/em, u/ins,
s/strike/del, a, code, pre, blockquote, tg-spoiler, span class="tg-spoiler".
Усе інше в тексті від моделі — або помилка, або звичайний текст.
"""

import html as _html
import re

# Теги, які лишаємо жити. Порядок у групі важливий для регексів: довші
# альтернативи мають стояти перед коротшими (strong перед s, strike перед s),
# інакше `s` зʼїсть початок `strong` і зламає розмітку.
_TAG_NAMES = (
    "blockquote|tg-spoiler|strong|strike|code|pre|span|ins|del|em|a|b|i|u|s"
)

_ANCHOR_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_ANY_TAG_RE = re.compile(r"</?(?:" + _TAG_NAMES + r")\b[^>]*>", re.I)
_A_TOKEN_RE = re.compile(r'<a\s+href="[^"]*"[^>]*>|</a>', re.I)

# Закриття тега, яке модель написала не так: `</a}`, `</a)`, `</a]`, `</a`
# наприкінці рядка, `< / a >`. Правильне `</a>` теж підпадає — і просто
# нормалізується саме в себе, тож функція ідемпотентна.
_CLOSE_FIX_RE = re.compile(
    r"<\s*/\s*(" + _TAG_NAMES + r")\s*(?:>|[}\])»]|(?=\s)|$)", re.I)

# Відкриття анкора в будь-якому написанні лапок (або без них) + зайві
# атрибути (target, rel), яких Telegram не розуміє.
_OPEN_A_RE = re.compile(
    r"""<\s*a\s [^>]*? href \s*=\s* (?: "([^"]*)" | '([^']*)' | ([^\s'">]+) ) [^>]*>""",
    re.I | re.X)

# Амперсанд, який НЕ починає сутність — Telegram на такому теж падає.
_BARE_AMP_RE = re.compile(r"&(?!(?:[a-zA-Z][a-zA-Z0-9]{1,7}|#\d{1,6}|#x[0-9a-fA-F]{1,6});)")

# Кутова дужка, яка не починає дозволений тег: «менше <5%», «<Прізвище>».
# Саме вона найчастіше валить parse_mode у звичайних (не бекових) відповідях.
_BARE_LT_RE = re.compile(r"<(?!/?(?:" + _TAG_NAMES + r")\b)", re.I)


def repair(text):
    """Полагодити розмітку від моделі так, щоб Telegram прийняв її в HTML-режимі.

    Нічого не вигадує і не додає змісту: лише лагодить теги й екранує те,
    що тегом не є. Ідемпотентна — на вже коректному тексті нічого не міняє."""
    if not text or "<" not in text and "&" not in text:
        return text
    text = _CLOSE_FIX_RE.sub(lambda m: "</%s>" % m.group(1).lower(), text)
    text = _OPEN_A_RE.sub(
        lambda m: '<a href="%s">' % (m.group(1) or m.group(2) or m.group(3) or ""), text)
    text = _BARE_AMP_RE.sub("&amp;", text)
    text = _BARE_LT_RE.sub("&lt;", text)
    text = _balance_anchors(text)
    return text


def _balance_anchors(text):
    """Звести кількість `<a>` і `</a>`: зайве закриття прибрати, незакритий
    анкор закрити наприкінці ЙОГО рядка (а не тексту — інакше в лінк поїхали б
    усі наступні абзаци)."""
    parts, pos, opened = [], 0, False
    for m in _A_TOKEN_RE.finditer(text):
        is_open = not m.group(0).lower().startswith("</")
        if is_open:
            if opened:  # попередній анкор не закрили — закриваємо перед новим
                parts.append(text[pos:m.start()])
                parts.append("</a>")
                pos = m.start()
            opened = True
        else:
            if not opened:  # зайве закриття — просто викидаємо
                parts.append(text[pos:m.start()])
                pos = m.end()
                continue
            opened = False
    parts.append(text[pos:])
    out = "".join(parts)
    if opened:
        last = None
        for m in _A_TOKEN_RE.finditer(out):
            if not m.group(0).lower().startswith("</"):
                last = m
        if last:
            nl = out.find("\n", last.end())
            cut = len(out) if nl == -1 else nl
            out = out[:cut] + "</a>" + out[cut:]
    return out


def to_plain(text):
    """Фолбек, коли Telegram усе одно не прийняв HTML: анкор стає «текст (URL)»,
    решта тегів знімається, сутності розгортаються. Сирих тегів на екран не
    виходить ніколи."""
    if not text:
        return text
    text = _ANCHOR_RE.sub(
        lambda m: "%s (%s)" % (_ANY_TAG_RE.sub("", m.group(2)).strip(), m.group(1)), text)
    text = _ANY_TAG_RE.sub("", text)
    return _html.unescape(text)


def strip_tags(text):
    """Текст без розмітки і без URL — для заголовків, підписів, пошуку."""
    if not text:
        return text
    return _html.unescape(_ANY_TAG_RE.sub("", text))


async def send_html(send, text, **kwargs):
    """Надіслати/оновити повідомлення в HTML-режимі з подвійним запобіжником.

    send — корутина-відправник (msg.edit_text, message.reply_text тощо).
    Спершу ремонт розмітки, далі HTML; якщо Telegram усе одно відмовився —
    той самий текст без тегів (лінки лишаються видимими адресами)."""
    fixed = repair(text)
    try:
        return await send(fixed, parse_mode="HTML", **kwargs)
    except Exception as e:
        print(f"tg_html: HTML відхилено ({e}) — шлемо без розмітки")
        return await send(to_plain(fixed), **kwargs)
