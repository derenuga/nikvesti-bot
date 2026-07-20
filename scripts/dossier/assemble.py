#!/usr/bin/env python3
"""Крок 6b: збирає фінальне HTML-досьє (наратив + траєкторія + розбір
однофамільців + відкриті питання + повний індекс) у фірмовому стилі МикВісті
(шрифт Commissioner, синій акцент). Не-ASCII → HTML-сутності, щоб сторінка
читалась за будь-якої кодировки (Safari без charset-заголовка).

env:
  WORKDIR — тека з subject_all.json / all_classified.json / periods_meta.json
            і narrative/pN.md (вихід reduce-агентів)
  META    — JSON від агента-синтезатора:
            {subject, identity, trajectory:[{period,role,note}],
             open_questions:[..], homonym_note}
  OUT     — шлях вихідного .html
"""
import html
import json
import os
import re
from collections import Counter, defaultdict

WORKDIR = os.environ["WORKDIR"]
META = os.environ["META"]
OUT = os.environ["OUT"]

meta = json.load(open(META))
SUBJECT = meta["subject"]
subj = json.load(open(f"{WORKDIR}/subject_all.json"))
allc = json.load(open(f"{WORKDIR}/all_classified.json"))
pmeta = json.load(open(f"{WORKDIR}/periods_meta.json"))
subj.sort(key=lambda r: r["date"])


def inline(s):
    parts = re.split(r'(\[[^\]]+\]\([^)]+\))', s)
    out = []
    for p in parts:
        m = re.match(r'\[([^\]]+)\]\(([^)]+)\)', p)
        if m:
            txt = html.escape(html.unescape(m.group(1)))
            url = html.escape(m.group(2), quote=True)
            out.append(f'<a href="{url}" target="_blank" rel="noopener">{txt}</a>')
        else:
            t = html.escape(p)
            t = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', t)
            out.append(t)
    return ''.join(out)


def md_to_html(md):
    blocks = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith('### '):
            blocks.append(f'<h3>{inline(s[4:])}</h3>')
        elif s.startswith('## '):
            body = s[3:]
            m = re.match(r'([0-9]{4}[–\-][0-9]{4}|[0-9]{4}):\s*(.*)', body)
            if m:
                blocks.append(f'<h2><span class="yr">{m.group(1)}</span>'
                              f'<span class="ht">{inline(m.group(2))}</span></h2>')
            else:
                blocks.append(f'<h2>{inline(body)}</h2>')
        else:
            blocks.append(f'<p>{inline(s)}</p>')
    return '\n'.join(blocks)


# наратив
narr = []
for p in pmeta:
    fp = f"{WORKDIR}/narrative/p{p['idx']}.md"
    if os.path.exists(fp):
        narr.append(f'<section class="period">{md_to_html(open(fp, encoding="utf-8").read())}</section>')
narrative_html = '\n'.join(narr)

# індекс по роках
by_year = defaultdict(list)
for r in subj:
    by_year[r["date"][:4]].append(r)
index_blocks = []
for y in sorted(by_year):
    items = by_year[y]
    rows = []
    for r in items:
        star = ' <span class="star">★</span>' if r.get("significance", 0) >= 4 else ''
        title = html.escape(html.unescape((r["title"] or "(без заголовка)").strip()))
        url = html.escape(r["url"], quote=True)
        d = r["date"][8:10] + '.' + r["date"][5:7]
        rows.append(f'<li><span class="d">{d}</span>'
                    f'<a href="{url}" target="_blank" rel="noopener">{title}</a>{star}</li>')
    key_n = sum(1 for r in items if r.get("significance", 0) >= 4)
    kt = f' · {key_n} ключових' if key_n else ''
    index_blocks.append(
        f'<details class="yr-block"><summary>{y}'
        f'<span class="yc">{len(items)} матеріалів{kt}</span></summary>'
        f'<ul class="idx">{"".join(rows)}</ul></details>')
index_html = '\n'.join(index_blocks)

# розбір однофамільців
others = [r for r in allc if r.get("person") == "other"]
who_counts = Counter((r.get("who") or "?").strip()[:70] for r in others)
homonym_rows = "".join(
    f'<tr><td>{html.escape(w)}</td><td class="num">{n}</td></tr>'
    for w, n in who_counts.most_common(8))
unclear_n = sum(1 for r in allc if r.get("person") == "unclear")
vit_n = len(subj)
tot = len(allc)
key_total = sum(1 for r in subj if r.get('significance', 0) >= 4)
first = subj[0]["date"] if subj else ""
last = subj[-1]["date"] if subj else ""

traj_html = "".join(
    f'<li><span class="ty">{html.escape(t.get("period",""))}</span>'
    f'<div class="tb"><span class="tr">{html.escape(t.get("role",""))}</span>'
    f'<span class="tn">{html.escape(t.get("note",""))}</span></div></li>'
    for t in meta.get("trajectory", []))
leads_html = "".join(f'<li>{inline(q)}</li>' for q in meta.get("open_questions", []))
identity = html.escape(meta.get("identity", ""))
homonym_note = inline(meta.get("homonym_note", ""))

PAGE = f"""<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Commissioner:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {{
  --blue:#3478F9; --blue-dark:#235FD6; --wash:#EAF1FC;
  --bg:#ffffff; --ink:#111318; --muted:#5B6472;
  --line:rgba(0,0,0,.10); --line2:rgba(0,0,0,.06); --star:#E0902E;
  --font:"Commissioner",-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","Roboto Mono",Menlo,monospace;
}}
@media (prefers-color-scheme:dark) {{
  :root {{
    --blue:#5b93ff; --blue-dark:#8fb4ff; --wash:#16233b;
    --bg:#0f1216; --ink:#e9ecf1; --muted:#94a0b0;
    --line:rgba(255,255,255,.12); --line2:rgba(255,255,255,.07); --star:#e6b04a;
  }}
}}
:root[data-theme="light"] {{
  --blue:#3478F9; --blue-dark:#235FD6; --wash:#EAF1FC;
  --bg:#ffffff; --ink:#111318; --muted:#5B6472;
  --line:rgba(0,0,0,.10); --line2:rgba(0,0,0,.06); --star:#E0902E;
}}
:root[data-theme="dark"] {{
  --blue:#5b93ff; --blue-dark:#8fb4ff; --wash:#16233b;
  --bg:#0f1216; --ink:#e9ecf1; --muted:#94a0b0;
  --line:rgba(255,255,255,.12); --line2:rgba(255,255,255,.07); --star:#e6b04a;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; }}
.wrap {{ background:var(--bg); color:var(--ink); font-family:var(--font);
  font-size:18px; line-height:1.62; -webkit-font-smoothing:antialiased;
  padding:32px 20px 64px; }}
.col {{ max-width:760px; margin:0 auto; }}
a {{ color:var(--blue-dark); text-decoration:none;
  border-bottom:1px solid color-mix(in srgb,var(--blue) 40%,transparent); }}
a:hover {{ border-bottom-color:var(--blue); }}
a:focus-visible {{ outline:2px solid var(--blue); outline-offset:2px; border-radius:2px; }}

.kicker {{ font-size:12px; font-weight:700; letter-spacing:.14em; text-transform:uppercase;
  color:var(--blue); margin:0 0 14px; display:flex; align-items:center; gap:.5rem; }}
h1 {{ font-size:36px; line-height:1.2; font-weight:800; letter-spacing:-.02em;
  margin:0 0 18px; padding-bottom:18px; border-bottom:3px solid var(--blue); text-wrap:balance; }}
.lead {{ font-size:20px; line-height:1.5; margin:0 0 22px; text-wrap:balance; }}

.meta {{ display:flex; flex-wrap:wrap; gap:0 32px; padding:16px 0;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line); margin:0 0 26px; }}
.meta div {{ display:flex; flex-direction:column; gap:2px; }}
.meta .k {{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); font-weight:600; }}
.meta .v {{ font-size:24px; font-weight:800; font-variant-numeric:tabular-nums; line-height:1.1; }}
.meta .v small {{ font-size:13px; font-weight:500; color:var(--muted); }}

.note {{ background:var(--wash); border-left:3px solid var(--blue);
  padding:14px 18px; border-radius:0 8px 8px 0; font-size:16px; line-height:1.55; margin:0 0 26px; }}
.note b {{ display:block; font-size:11px; letter-spacing:.12em; text-transform:uppercase;
  color:var(--blue-dark); font-weight:700; margin-bottom:6px; }}

h2.sec {{ font-size:12px; font-weight:700; letter-spacing:.14em; text-transform:uppercase;
  color:var(--blue); border-bottom:1px solid var(--line); padding-bottom:8px; margin:48px 0 20px; }}

ul.traj {{ list-style:none; padding:0; margin:0; }}
ul.traj li {{ display:grid; grid-template-columns:118px 1fr; gap:16px;
  padding:12px 0; border-bottom:1px solid var(--line2); }}
ul.traj li:first-child {{ border-top:1px solid var(--line2); }}
ul.traj .ty {{ font-weight:700; color:var(--blue-dark); font-variant-numeric:tabular-nums; padding-top:1px; }}
.tb {{ display:flex; flex-direction:column; gap:3px; }}
.tr {{ font-weight:700; }}
.tn {{ font-size:15px; color:var(--muted); line-height:1.45; }}

.period h2 {{ display:flex; flex-direction:column; gap:2px; margin:40px 0 14px; text-wrap:balance; }}
.period h2 .yr {{ font-size:14px; font-weight:700; color:var(--blue); letter-spacing:.02em; }}
.period h2 .ht {{ font-size:25px; line-height:1.28; font-weight:700; letter-spacing:-.01em; }}
.period h3 {{ font-size:13px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:var(--muted); margin:26px 0 6px; }}
.period p {{ margin:0 0 18px; }}
.period p strong {{ font-weight:700; }}

/* явний color: reset в'ювера артефактів перебиває успадкування для table */
table.hom {{ width:100%; border-collapse:collapse; font-size:16px; margin:6px 0; color:var(--ink); }}
table.hom td {{ padding:10px 4px; border-bottom:1px solid var(--line2); color:var(--ink); }}
table.hom td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:700;
  color:var(--blue-dark); width:64px; }}
table.hom tr:last-child td {{ border-bottom:none; }}
.hsum {{ font-size:15px; color:var(--muted); margin-top:10px; line-height:1.5; }}

ol.leads {{ padding-left:0; list-style:none; counter-reset:l; margin:0; }}
ol.leads li {{ counter-increment:l; position:relative; padding:12px 0 12px 40px;
  border-bottom:1px solid var(--line2); }}
ol.leads li:first-child {{ border-top:1px solid var(--line2); }}
ol.leads li::before {{ content:counter(l); position:absolute; left:0; top:11px;
  font-size:13px; font-weight:800; color:#fff; background:var(--blue);
  width:24px; height:24px; border-radius:50%; display:flex; align-items:center; justify-content:center; }}

.idx-intro {{ font-size:15px; color:var(--muted); margin-bottom:14px; line-height:1.5; }}
details.yr-block {{ border-bottom:1px solid var(--line); }}
details.yr-block summary {{ cursor:pointer; padding:12px 0; font-weight:700;
  display:flex; justify-content:space-between; align-items:baseline; list-style:none; gap:16px; }}
details.yr-block summary::-webkit-details-marker {{ display:none; }}
details.yr-block summary::before {{ content:"\\25B8"; color:var(--blue); font-size:12px;
  margin-right:8px; transition:transform .15s; display:inline-block; }}
details.yr-block[open] summary::before {{ transform:rotate(90deg); }}
.yc {{ font-size:13px; color:var(--muted); font-weight:500; white-space:nowrap;
  font-variant-numeric:tabular-nums; }}
ul.idx {{ list-style:none; padding:0 0 14px 20px; margin:0; }}
ul.idx li {{ display:grid; grid-template-columns:52px 1fr; gap:10px; padding:7px 0;
  font-size:16px; align-items:baseline; border-bottom:1px solid var(--line2); }}
ul.idx .d {{ font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; font-weight:600; }}
.star {{ color:var(--star); }}

.method {{ margin-top:52px; padding-top:24px; border-top:3px solid var(--blue); }}
.method h2 {{ font-size:19px; font-weight:700; margin:0 0 14px; color:var(--ink);
  letter-spacing:0; text-transform:none; border:none; padding:0; }}
.method dl {{ display:grid; grid-template-columns:auto 1fr; gap:8px 18px; margin:0;
  font-size:15px; color:var(--muted); }}
.method dt {{ font-weight:600; color:var(--ink); }}
.method dd {{ margin:0; }}
.method .foot {{ margin-top:16px; font-size:15px; color:var(--muted); line-height:1.55; }}
.foxline {{ text-align:center; font-size:13px; color:var(--muted); margin-top:28px; letter-spacing:.04em; }}
code, .mono {{ font-family:var(--mono); font-size:.9em; }}
@media (max-width:640px) {{
  .wrap {{ padding:22px 16px 48px; font-size:17px; }}
  h1 {{ font-size:28px; }} .lead {{ font-size:18px; }}
  .period h2 .ht {{ font-size:22px; }}
  ul.traj li {{ grid-template-columns:1fr; gap:2px; }}
  ul.idx li {{ grid-template-columns:46px 1fr; }}
}}
</style>

<div class="wrap"><div class="col">
  <p class="kicker"><span>🦊</span> Лисяча нора · глибоке досьє</p>
  <h1>{html.escape(SUBJECT)}</h1>
  <p class="lead">{identity}</p>
  <div class="meta">
    <div><span class="k">Матеріали про нього</span><span class="v">{vit_n}</span></div>
    <div><span class="k">Згадок за запитом</span><span class="v">{tot} <small>у 17-річному архіві</small></span></div>
    <div><span class="k">Період</span><span class="v">{first[:4]}–{last[:4]}</span></div>
    <div><span class="k">Ключових епізодів</span><span class="v">{key_total}</span></div>
  </div>
  <div class="note">
    <b>Що це таке</b>
    <strong>Глибоке досьє</strong> з 17-річного архіву МикВісті. Прочитано всі <strong>{tot} згадки</strong> за пошуковим запитом, з них відділено <strong>{vit_n} матеріалів саме про цю людину</strong>, і зведено в наратив із посиланням на кожен факт. Нижче — траєкторія, історія питання, розбір однофамільців, зачіпки для копання і повний індекс.
  </div>

  <h2 class="sec">Траєкторія: ким він був</h2>
  <ul class="traj">{traj_html}</ul>

  <h2 class="sec">Історія питання</h2>
  {narrative_html}

  <h2 class="sec">Розбір: хто ще ховається під прізвищем</h2>
  <p class="idx-intro">Пошук дає {tot} результатів, але лише {vit_n} — про нашого фігуранта. Решту map-фаза відсіяла. Це показує, навіщо потрібен сутнісний шар: повнотекстовий пошук не відрізняє людину від тезки чи топоніма.</p>
  <table class="hom">{homonym_rows}
    <tr><td>Неоднозначні / згадка поза фрагментом</td><td class="num">{unclear_n}</td></tr>
  </table>
  <p class="hsum">{homonym_note}</p>

  <h2 class="sec">Відкриті питання — зачіпки для копання</h2>
  <ol class="leads">{leads_html}</ol>

  <h2 class="sec">Повний індекс — усі {vit_n} матеріалів</h2>
  <p class="idx-intro">Хронологічно, по роках. ★ — ключові матеріали. Наратив вибирає головне, а індекс дає все — 100% покриття.</p>
  {index_html}

  <div class="method">
    <h2>Як це зроблено</h2>
    <dl>
      <dt>Джерело</dt><dd>«Лисяча нора» — дзеркало 17-річного архіву МикВісті</dd>
      <dt>Відбір</dt><dd>повнотекстовий пошук → {tot} статей з повним текстом</dd>
      <dt>Map-фаза</dt><dd>пачки по ~25 статей → класифікація (фігурант / однофамілець / неясно) + мікрофакти</dd>
      <dt>Reduce-фаза</dt><dd>мікрофакти по періодах → наратив із лінком на кожен факт</dd>
      <dt>Правило чесності</dt><dd>у наративі немає фактів поза текстами статей; кожне твердження лінковане</dd>
    </dl>
    <div class="foot">Зібрано скілом <span class="mono">deep-dossier</span> у сесії Claude Code.</div>
  </div>
  <p class="foxline">🦊 зібрано Лисом Микитою · {first} — {last}</p>
</div></div>
"""

PAGE = ''.join(c if ord(c) < 128 else f'&#{ord(c)};' for c in PAGE)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(PAGE)
print(f"written {OUT}, subject={vit_n} total={tot}")
