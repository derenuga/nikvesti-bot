"""
«Бек для вставки» — сторінка /back на домені Mini App.

НАВІЩО (питання Олега 12.08.2026): бек у Telegram красивий і клікабельний,
але при копіюванні в Google Docs чи в редактор адмінки лінки зникають —
лишається голий текст. Це НЕ наш баг і не лікується форматуванням
повідомлення: клієнти Telegram кладуть у буфер обміну ЛИШЕ плоский текст,
без text/html-варіанта (відкритий запит tdesktop #5795; на Android копіювання
форматування немає взагалі). Скільки не міняй розмітку повідомлення —
з буфера вийде текст без лінків.

Тому лінки треба віддати з місця, яке вміє класти в буфер саме HTML —
зі звичайної веб-сторінки. Під беком зʼявляється кнопка «📋 Для Docs і
адмінки», яка веде сюди, а тут два способи копіювання під дві різні цілі:

  • «Скопіювати з лінками» — кладе в буфер text/html + text/plain. Вставка
    в Google Docs, Word чи візуальний редактор адмінки зберігає живі лінки.
  • «Скопіювати HTML-код» — той самий бек тегами, для режиму «Джерело/HTML»
    у редакторі адмінки (там візуальна вставка часто ріже розмітку).

Авторизації немає — свідомо, як і в генератора карток: сторінку відкривають
звичайним браузером (саме там і живуть Docs та адмінка), а Telegram-у initData
взяти нізвідки. Замість авторизації — випадковий токен у лінку, і вміст беку
не таємниця: це наші ж опубліковані новини. Сторінка нічого не приймає й не
пише, лише віддає збережений текст.

Стан — у storage (back_exports), кап 40 останніх: бек живе рівно доти, доки
його не вставили, а редеплой посеред роботи не має перетворювати кнопку на 404.
"""

import asyncio
import html
import os
import re
import secrets
from datetime import datetime

from handlers import storage
from handlers.helpers import normalize_https_url
from handlers.tg_html import repair, strip_tags

try:
    from aiohttp import web
except ImportError:  # локальний dev без aiohttp — веб-шар неактивний
    web = None

WEBAPP_URL = normalize_https_url(os.environ.get("WEBAPP_URL"))

# Службові хвости бота, яким не місце у вставленому в статтю тексті:
# підпис джерела даних і технічна примітка про ліміт новин.
_META_RE = re.compile(
    r"^\s*(?:📊 Джерело даних:.*|\(Взяв перші \d+ новин[^)]*\)\s*)$", re.M)


def to_article_html(text):
    """Бек із Telegram-розмітки → HTML абзацами, придатний для вставки.

    Порожній рядок ділить абзаци (<p>), одинарний перенос лишається переносом
    (<br>) — саме так текст і виглядає в редакторі статті."""
    text = _META_RE.sub("", repair(text or ""))
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "\n".join("<p>%s</p>" % b.replace("\n", "<br>\n") for b in blocks)


def save(text, topic=""):
    """Покласти бек у стан і повернути токен для лінка."""
    token = secrets.token_urlsafe(9)
    storage.save_back_export(token, {
        "html": to_article_html(text),
        "topic": strip_tags(topic or "")[:120],
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    return token


def _markup(token):
    # Імпорт тут, а не вгорі: модуль тягне ще й веб-шар (сторінка /back),
    # і сторінці класи Telegram ні до чого.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📋 Для Docs і адмінки", url=f"{WEBAPP_URL}/back?k={token}")]])


async def copy_button(text, topic=""):
    """Клавіатура з кнопкою копіювання під беком. None — коли веб-шар спить
    (немає WEBAPP_URL) або текст порожній: кнопка в нікуди гірша за її брак."""
    if not WEBAPP_URL or not text:
        return None
    try:
        token = await asyncio.to_thread(save, text, topic)
    except Exception as e:
        print(f"back_export: не вдалось зберегти бек — {e}")
        return None
    return _markup(token)


# ---------- Сторінка ----------

_PAGE = """<!doctype html>
<html lang="uk"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Бек для вставки — МикВісті</title>
<style>
  :root {{
    --brand: #3478F9; --bg: #f4f6fb; --card: #fff; --ink: #16181d;
    --muted: #6b7280; --line: #e3e7ef;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #14161a; --card: #1c1f26; --ink: #e9ecf2;
             --muted: #9aa3b2; --line: #2b303a; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 20px 16px 60px; background: var(--bg); color: var(--ink);
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 14px; margin: 0 0 18px; }}
  .bar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }}
  button {{ font: inherit; font-weight: 600; border: 0; border-radius: 10px;
    padding: 12px 18px; cursor: pointer; background: var(--brand); color: #fff; }}
  button.ghost {{ background: transparent; color: var(--brand);
    box-shadow: inset 0 0 0 1.5px var(--brand); }}
  button:active {{ transform: translateY(1px); }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 20px 22px; }}
  #doc p {{ margin: 0 0 14px; }}
  #doc p:last-child {{ margin-bottom: 0; }}
  #doc a {{ color: var(--brand); }}
  details {{ margin-top: 18px; }}
  summary {{ cursor: pointer; color: var(--muted); font-size: 14px; }}
  textarea {{ width: 100%; min-height: 220px; margin-top: 12px; padding: 12px;
    border: 1px solid var(--line); border-radius: 10px; background: var(--card);
    color: var(--ink); font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .hint {{ color: var(--muted); font-size: 13px; margin-top: 18px; }}
  .toast {{ position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: #16a34a; color: #fff; padding: 11px 18px; border-radius: 10px;
    font-size: 14px; opacity: 0; pointer-events: none; transition: opacity .2s; }}
  .toast.show {{ opacity: 1; }}
  .toast.err {{ background: #dc2626; }}
</style>
</head><body>
<div class="wrap">
  <h1>🦊 Бек для вставки</h1>
  <p class="sub">{topic}Копіюй звідси — лінки доїдуть у Google Docs і в редактор адмінки.
     З самого Telegram вони не копіюються: він кладе в буфер лише голий текст.</p>
  <div class="bar">
    <button onclick="copyRich()">Скопіювати з лінками</button>
    <button class="ghost" onclick="copyCode()">Скопіювати HTML-код</button>
  </div>
  <div class="card"><div id="doc">{body}</div></div>
  <details>
    <summary>HTML-код (для режиму «Джерело» в редакторі)</summary>
    <textarea id="src" readonly>{source}</textarea>
  </details>
  <p class="hint">Якщо кнопка не спрацювала — виділи текст вручну (Ctrl+A всередині блоку)
     і скопіюй. У браузері Telegram копіювання іноді заблоковано: відкрий сторінку
     в Chrome або Safari через меню «⋮ → Відкрити в браузері».</p>
</div>
<div class="toast" id="toast"></div>
<script>
function toast(msg, err) {{
  var t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
  setTimeout(function () {{ t.className = 'toast'; }}, 1800);
}}
function selectionCopy(node) {{
  var r = document.createRange(); r.selectNodeContents(node);
  var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  var ok = false;
  try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
  s.removeAllRanges();
  return ok;
}}
async function copyRich() {{
  var doc = document.getElementById('doc');
  try {{
    // Головний шлях: у буфер ідуть ОБИДВА варіанти — html (лінки живі
    // у Docs і візуальному редакторі) і plain (для простих полів).
    await navigator.clipboard.write([new ClipboardItem({{
      'text/html': new Blob([doc.innerHTML], {{type: 'text/html'}}),
      'text/plain': new Blob([doc.innerText], {{type: 'text/plain'}})
    }})]);
    toast('Скопійовано з лінками');
  }} catch (e) {{
    // Фолбек для WKWebView і старих браузерів, де async-буфер заблоковано:
    // копіюємо виділення — воно теж лягає в буфер розміткою.
    var ok = selectionCopy(doc);
    toast(ok ? 'Скопійовано з лінками' : 'Не вийшло — виділи вручну', !ok);
  }}
}}
async function copyCode() {{
  var src = document.getElementById('src');
  src.closest('details').open = true;
  try {{
    await navigator.clipboard.writeText(src.value);
    toast('HTML-код скопійовано');
  }} catch (e) {{
    src.focus(); src.select();
    var ok = false;
    try {{ ok = document.execCommand('copy'); }} catch (e2) {{ ok = false; }}
    toast(ok ? 'HTML-код скопійовано' : 'Не вийшло — виділи вручну', !ok);
  }}
}}
</script>
</body></html>"""


async def page(request):
    """GET /back?k=<token> — сторінка копіювання беку."""
    token = (request.query.get("k") or "").strip()
    entry = await asyncio.to_thread(storage.get_back_export, token) if token else None
    if not entry:
        return web.Response(
            text="<!doctype html><meta charset=utf-8><body style='font:16px sans-serif;"
                 "padding:40px;text-align:center'>Бек не знайдено — склади його заново"
                 " в боті.</body>",
            content_type="text/html", status=404)
    body = entry.get("html") or ""
    topic = entry.get("topic") or ""
    return web.Response(
        text=_PAGE.format(
            body=body,
            source=html.escape(body),
            topic=("«%s» · " % html.escape(topic)) if topic else "",
        ),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )
