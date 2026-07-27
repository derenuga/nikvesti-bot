/* Mini App «Команда» — прототип редакторського інтерфейсу (концепція v2).
   Флоу Каті: «+» → людина → creative task (проєкт/позапроєкт, тип, тематика,
   кількість, нотатка). Проєкти і фото — з БД сайту, тематики й таски — з Нори.
   Таб «Проєкти»: список (донор великим + строки) або таймлайн (ґант зі
   смугами-проєктами, точка «сьогодні», горизонтальний скрол на телефоні).
   Картинки: ресайзер сайту (.webp) → фолбек на оригінал → ініціали.
   Журналіст поки бачить свої завдання read-only — його інтерфейс наступний. */

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (id) => document.getElementById(id);

const STATE = {
  me: null,
  tasks: [],
  people: [],
  projects: [],
  view: "home",
  projView: "list",
  currentProject: null,
  currentNorm: null,
  kpi: null,
  form: null,
};

const TYPE_WORDS = {
  news: { one: "новина", few: "новини", many: "новин" },
  article: { one: "стаття", few: "статті", many: "статей" },
  any: { one: "матеріал", few: "матеріали", many: "матеріалів" },
};

const THEME_FORMATS = [
  { v: null, label: "Без формату" },
  { v: "news", label: "Новина" },
  { v: "article", label: "Стаття" },
  { v: "post", label: "Пост" },
  { v: "video", label: "Відео" },
  { v: "hybrid", label: "Гібридний" },
];

function formatTitle(v) {
  const f = THEME_FORMATS.find((x) => x.v === v);
  return f && f.v ? f.label : null;
}

const DL_KINDS = [
  { v: "narrative", label: "Наративний звіт" },
  { v: "financial", label: "Фінансовий звіт" },
  { v: "milestone", label: "Майлстоун" },
];
const DL_STAGES = [
  { v: "interim", label: "Проміжний" },
  { v: "final", label: "Фінальний" },
];

function dlLabel(d) {
  const kind = (DL_KINDS.find((k) => k.v === d.kind) || {}).label || d.kind;
  const stage = d.stage ? (DL_STAGES.find((s) => s.v === d.stage) || {}).label : null;
  let s = d.kind === "milestone" ? d.title : kind + (stage ? ` · ${stage.toLowerCase()}` : "");
  if (d.kind !== "milestone" && d.title) s += ` — ${d.title}`;
  return s;
}

function dlDateHtml(d) {
  const today = new Date().toISOString().slice(0, 10);
  const soon = new Date(Date.now() + 7 * 86400e3).toISOString().slice(0, 10);
  const [y, m, day] = d.due.split("-");
  const cls = d.due < today ? "dl-over" : d.due <= soon ? "dl-soon" : "dl";
  return `<span class="dl-date ${cls}">${day}.${m}.${y.slice(2)}</span>`;
}

function nextDeadline(p) {
  const today = new Date().toISOString().slice(0, 10);
  return (p.deadlines || []).find((d) => d.due >= today) || null;
}

const MONTHS_SHORT = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"];

/* ---------- API ---------- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: "tma " + (tg ? tg.initData : ""),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
  return res.json();
}

/* ---------- Хелпери ---------- */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function haptic(kind) {
  try { tg && tg.HapticFeedback.notificationOccurred(kind); } catch (e) {}
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2400);
}

function icon(name, cls = "ic") {
  return `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
}

function initials(name) {
  return name.split(" ").slice(0, 2).map((w) => w[0] || "").join("").toUpperCase();
}

/* Ланцюжок фолбеків: ресайз .webp → оригінал (data-alt) → зник (видно ініціали) */
function imgHtml(src, orig, extra = "") {
  if (!src) return "";
  return `<img src="${esc(src)}"${orig ? ` data-alt="${esc(orig)}"` : ""} alt="" loading="lazy" ${extra}
    onerror="if(this.dataset.alt){this.src=this.dataset.alt;this.removeAttribute('data-alt')}else{this.remove()}">`;
}

function avatar(personName, entry, size) {
  return `<span class="ava" style="width:${size}px;height:${size}px">
    <span class="init" style="font-size:${Math.round(size / 3)}px">${esc(initials(personName))}</span>
    ${entry ? imgHtml(entry.photo, entry.photo_orig) : ""}
  </span>`;
}

function logoSq(project, size) {
  return `<span class="logo-sq" style="width:${size}px;height:${size}px">
    <span class="init" style="font-size:${Math.round(size / 3.4)}px">${esc(initials(project.partner || project.name))}</span>
    ${imgHtml(project.logo, project.logo_orig)}
  </span>`;
}

function fmtUnixDate(ts) {
  if (!ts) return null;
  return new Date(ts * 1000).toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function fmtRange(p) {
  const a = fmtUnixDate(p.start_date), b = fmtUnixDate(p.end_date);
  if (a && b) return `${a} — ${b}`;
  if (b) return `до ${b}`;
  if (a) return `з ${a}`;
  return "без строку";
}

function qtyWord(type, qty) {
  const w = TYPE_WORDS[type] || TYPE_WORDS.any;
  if (qty === 1) return w.one;
  return qty < 5 ? w.few : w.many;
}

function taskSummary(t) {
  let line = t.qty > 1 ? `${t.qty} ${qtyWord(t.type, t.qty)}` : qtyWord(t.type, 1);
  const partner = t.partner_name || (taskProject(t).partner || null);
  if (partner) line += ` · ${partner}`;
  if (t.project_name) line += ` · ${t.project_name}`;
  else if (!partner) line += " · позапроєктне";
  if (t.theme_name) line += ` (${t.theme_name})`;
  return line;
}

function personEntry(name) {
  return STATE.people.find((x) => x.name === name) || null;
}

function deadlineHtml(t) {
  if (!t.deadline) return "";
  const overdue = t.status === "open" && t.deadline < new Date().toISOString().slice(0, 10);
  const [y, m, d] = t.deadline.split("-");
  return ` · <span class="${overdue ? "dl-over" : "dl"}">до ${d}.${m}${overdue ? " ⚑" : ""}</span>`;
}

/* Дедлайн праворуч у рядку таска: «до 30.07», прострочений — червоним */
function deadlineBadge(t) {
  if (!t.deadline) return "";
  const overdue = t.status === "open" && t.deadline < new Date().toISOString().slice(0, 10);
  const [y, m, d] = t.deadline.split("-");
  return `<span class="dl-date ${overdue ? "dl-over" : "dl-soon"}">до ${d}.${m}</span>`;
}

/* Донор і лого таска: снапшот у тасці + лукап проєкту (для лого і старих тасків) */
function taskProject(t) {
  const proj = t.project_id ? STATE.projects.find((p) => p.id === t.project_id) : null;
  return {
    partner: t.partner_name || (proj && proj.partner) || null,
    projName: t.project_name || (proj && proj.name) || null,
    logoHtml: proj ? logoSq(proj, 40) : null,
  };
}

/* Другий рядок таска: «2 матеріали · Назва проєкту (Тематика)» */
function taskLine2(t) {
  let line = t.qty > 1 ? `${t.qty} ${qtyWord(t.type, t.qty)}` : qtyWord(t.type, 1);
  const { projName } = taskProject(t);
  line += ` · ${projName || "позапроєктне"}`;
  if (t.theme_name) line += ` (${t.theme_name})`;
  return line;
}

/* Стабільний колір проєкту: слот за порядком id (колір іде за сутністю,
   сортування/фільтри його не міняють) */
function projectColorIdx(id) {
  const ordered = [...STATE.projects].sort((a, b) => a.id - b.id);
  return (ordered.findIndex((p) => p.id === id) % 8) + 1;
}

/* ---------- Навігація ---------- */

function nav(view, arg) {
  STATE.view = view;
  if (view === "project") STATE.currentProject = arg;
  if (view === "kpinorm") STATE.currentNorm = arg;
  if (view === "kpi") STATE.kpi = null; // свіже зведення при кожному вході (факти кешує сервер)
  if (view === "form") STATE.form = {
    person: arg, project: undefined, type: null, theme_id: null, qty: 1, note: "", deadline: "",
  };
  const navKey = view === "kpinorm" ? "kpi" : view === "project" ? "projects" : view;
  document.querySelectorAll("#bottomnav .bn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === navKey));
  render();
  window.scrollTo(0, 0);
}

function render() {
  const v = STATE.view;
  if (v === "home") renderHome();
  else if (v === "people") renderPeople();
  else if (v === "form") renderForm();
  else if (v === "projects") renderProjects();
  else if (v === "project") renderProject();
  else if (v === "kpi") renderKpi();
  else if (v === "kpinorm") renderKpiNorm();
  else if (v === "team") renderTeam();
}

/* ---------- Головна ---------- */

function renderHome() {
  const open = STATE.tasks.filter((t) => t.status === "open");
  const closed = STATE.tasks.filter((t) => t.status !== "open").slice(0, 10);
  const row = (t) => {
    const tp = taskProject(t);
    return `
    <button class="task-row" data-task="${t.id}">
      ${avatar(t.person, personEntry(t.person), 42)}
      <span class="tr-main">
        <span class="tr-who">${esc(t.person.split(" ")[0])} ${esc((t.person.split(" ")[1] || "")[0] || "")}.
          ${tp.partner ? `<b class="tr-donor">· ${esc(tp.partner)}</b>` : ""}</span>
        <span class="tr-what">${esc(taskLine2(t))}</span>
      </span>
      <span class="tr-right">
        ${deadlineBadge(t)}
        <span class="status-dot ${t.status}"></span>
      </span>
    </button>`;
  };
  $("content").innerHTML = `
    <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
    <div class="h-sub">${new Date().toLocaleDateString("uk-UA", { weekday: "long", day: "numeric", month: "long" })}</div>
    ${open.length ? `<div class="soft-card"><div class="sc-t">Відкриті завдання · ${open.length}</div>${open.map(row).join("")}</div>` : ""}
    ${closed.length ? `<div class="soft-card"><div class="sc-t">Нещодавно закриті</div>${closed.map(row).join("")}</div>` : ""}
    ${!open.length && !closed.length ? `<div class="empty-hint">Завдань поки немає.<br>Натисни «+» унизу, щоб поставити перше.</div>` : ""}
  `;
}

function taskSheet(t) {
  openSheet(`
    <h2>${esc(t.person)}</h2>
    <p style="color:var(--muted);margin:-8px 0 6px">${esc(taskSummary(t))}${deadlineHtml(t)}</p>
    ${t.note ? `<p style="margin-bottom:6px">${esc(t.note)}</p>` : ""}
    <p style="color:var(--muted);font-size:12.5px">поставила ${esc(t.creator.split(" ")[0])} · ${new Date(t.created_at).toLocaleDateString("uk-UA")}</p>
    <div class="sheet-actions">
      ${t.status === "open"
        ? `<button class="sbtn danger" data-status="dropped">Зняти</button>
           <button class="sbtn primary" data-status="done">Виконано</button>`
        : `<button class="sbtn" data-status="open">Повернути у відкриті</button>`}
    </div>`);
  $("sheet").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-status]");
    if (!btn) return;
    try {
      await api(`/api/tasks/${t.id}`, { method: "PATCH", body: JSON.stringify({ status: btn.dataset.status }) });
      closeSheet();
      haptic("success");
      await reload();
      render();
    } catch (err) { toast(err.message); }
  }, { once: true });
}

/* ---------- Вибір людини ---------- */

function renderPeople() {
  const byDept = {};
  STATE.people.forEach((p) => (byDept[p.dept_title] = byDept[p.dept_title] || []).push(p));
  const blocks = Object.entries(byDept).map(([dept, list]) => `
    <div class="dept-title">${esc(dept)}</div>
    <div class="people-grid">
      ${list.map((p) => `
        <button class="pv" data-person="${esc(p.name)}">
          ${avatar(p.name, p, 76)}
          <span class="nm">${esc(p.name.split(" ")[0])}</span>
          <span class="dp">${esc(p.name.split(" ")[1] || "")}</span>
        </button>`).join("")}
    </div>`).join("");
  $("content").innerHTML = `
    <button class="back" data-nav="home">${icon("chevron-left")} Скасувати</button>
    <div class="h-big">Кому ставимо завдання?</div>
    <div class="h-sub">&nbsp;</div>
    ${blocks}`;
}

/* ---------- Форма creative task ---------- */

function currentProjectObj() {
  const f = STATE.form;
  if (!f || !f.project) return null;
  return STATE.projects.find((p) => p.id === f.project) || null;
}

function renderForm() {
  const f = STATE.form;
  const proj = currentProjectObj();
  const themes = proj ? proj.themes : [];
  const projLabel = f.project === undefined
    ? `<span class="ph">Обрати проєкт…</span>`
    : f.project === null
      ? `Позапроєктне завдання`
      : `${logoSq(proj, 32)} ${esc(proj.name)}`;
  $("content").innerHTML = `
    <button class="back" data-nav="people">${icon("chevron-left")} Назад</button>
    <div class="who">
      ${avatar(f.person, personEntry(f.person), 48)}
      <div><div class="wn">${esc(f.person)}</div>
      <div class="wd">${esc((personEntry(f.person) || {}).dept_title || "")}</div></div>
    </div>

    <div class="f-label">Проєкт</div>
    <button class="bigpick" id="f-project">${projLabel}${icon("chevron-right", "ic chev")}</button>

    <div class="f-label">Тип матеріалу</div>
    <div class="two">
      <button class="bigbtn slim ${f.type === null ? "on" : ""}" data-type="">Будь-який</button>
      <button class="bigbtn slim ${f.type === "news" ? "on" : ""}" data-type="news">Новина</button>
      <button class="bigbtn slim ${f.type === "article" ? "on" : ""}" data-type="article">Стаття</button>
    </div>

    ${proj ? `
      <div class="f-label">Тематика проєкту</div>
      ${themes.length
        ? `<div class="chips">${themes.map((t) => `
            <button class="chip ${f.theme_id === t.id ? "on" : ""}" data-theme="${t.id}">${esc(t.name)}</button>`).join("")}
           </div>`
        : `<div style="color:var(--muted);font-size:13px">У проєкту ще немає тематик —
             їх можна завести на вкладці «Проєкти».</div>`}
    ` : ""}

    <div class="f-label">Кількість і дедлайн</div>
    <div class="count-row">
      <div class="stepper">
        <button id="qty-minus">${icon("minus")}</button>
        <b id="qty-val">${f.qty}</b>
        <button id="qty-plus">${icon("plus")}</button>
      </div>
      <input id="f-deadline" type="date" value="${esc(f.deadline)}" style="flex:1">
    </div>

    <div class="f-label">Нотатка</div>
    <textarea id="f-note" maxlength="1000" placeholder="нотатка, тема, деталі…">${esc(f.note)}</textarea>

    <button class="cta" id="f-create" ${f.project === undefined ? "disabled" : ""}>Поставити завдання</button>`;

  $("f-project").onclick = projectPickerSheet;
  $("qty-minus").onclick = () => { f.qty = Math.max(1, f.qty - 1); $("qty-val").textContent = f.qty; };
  $("qty-plus").onclick = () => { f.qty = Math.min(99, f.qty + 1); $("qty-val").textContent = f.qty; };
  $("f-deadline").oninput = (e) => { f.deadline = e.target.value; };
  $("f-note").oninput = (e) => { f.note = e.target.value; };
  $("content").querySelectorAll("[data-type]").forEach((b) => b.onclick = () => { f.type = b.dataset.type || null; renderForm(); });
  $("content").querySelectorAll("[data-theme]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.theme;
    f.theme_id = f.theme_id === id ? null : id;
    renderForm();
  });
  $("f-create").onclick = createTask;
}

function projectPickerSheet() {
  const f = STATE.form;
  openSheet(`
    <h2>Проєкт</h2>
    <button class="pick-row plain" data-proj="none">
      <span class="pk-name">Позапроєктне завдання</span>
    </button>
    ${STATE.projects.map((p) => `
      <button class="pick-row" data-proj="${p.id}">
        ${logoSq(p, 40)}
        <span>
          <span class="pk-name">${esc(p.partner ? p.partner + " · " : "")}${esc(p.name)}</span>
          <span class="pk-meta">${esc(fmtRange(p))}${p.kpi_news || p.kpi_articles ? ` · квота ${p.kpi_news || 0}+${p.kpi_articles || 0}` : ""}</span>
        </span>
      </button>`).join("")}`);
  $("sheet").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-proj]");
    if (!btn) return;
    f.project = btn.dataset.proj === "none" ? null : +btn.dataset.proj;
    f.theme_id = null;
    closeSheet();
    renderForm();
  }, { once: true });
}

async function createTask() {
  const f = STATE.form;
  const btn = $("f-create");
  btn.disabled = true;
  try {
    await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        person: f.person,
        project_id: f.project || null,
        type: f.type,
        theme_id: f.theme_id,
        qty: f.qty,
        note: f.note.trim(),
        deadline: f.deadline || null,
      }),
    });
    haptic("success");
    toast(`Завдання полетіло до ${f.person.split(" ")[0]}`);
    await reload();
    nav("home");
  } catch (e) {
    btn.disabled = false;
    toast(e.message);
  }
}

/* ---------- Проєкти: список і таймлайн ---------- */

function renderProjects() {
  const seg = `
    <div class="two seg-slim">
      <button class="bigbtn slim ${STATE.projView === "list" ? "on" : ""}" data-pv="list">Список</button>
      <button class="bigbtn slim ${STATE.projView === "timeline" ? "on" : ""}" data-pv="timeline">Таймлайн</button>
    </div>`;
  const body = STATE.projView === "timeline" ? timelineHtml() : listHtml();
  $("content").innerHTML = `
    <div class="h-big">Проєкти</div>
    <div class="h-sub">квоти й строки — з CMS сайту, тематики — тут</div>
    ${STATE.projects.length ? seg + body : `<div class="empty-hint">Не бачу проєктів — БД сайту недоступна.</div>`}`;
  $("content").querySelectorAll("[data-pv]").forEach((b) => b.onclick = () => {
    STATE.projView = b.dataset.pv;
    renderProjects();
  });
  if (STATE.projView === "timeline") {
    const sc = $("tl-scroll");
    if (sc && sc.dataset.nowx) sc.scrollLeft = Math.max(0, +sc.dataset.nowx - sc.clientWidth * 0.45);
  } else {
    enableProjectDrag();
  }
}

function listHtml() {
  const rows = STATE.projects.map((p) => {
    const nd = nextDeadline(p);
    return `
    <button class="proj-row" data-project="${p.id}" data-drag-id="${p.id}">
      ${logoSq(p, 46)}
      <span class="pr-txt">
        <span class="pr-donor">${esc(p.partner || p.name)}</span>
        ${p.partner ? `<span class="pr-name2">${esc(p.name)}</span>` : ""}
        <span class="pr-meta">${esc(fmtRange(p))}${p.kpi_news || p.kpi_articles ? ` · квота ${p.kpi_news || 0}+${p.kpi_articles || 0}` : ""}</span>
        ${nd ? `<span class="pr-meta">${esc(dlLabel(nd))} ${dlDateHtml(nd)}</span>` : ""}
      </span>
      ${icon("chevron-right", "ic chev")}
    </button>`;
  }).join("");
  return `<div id="proj-list">${rows}</div>
    <div class="tl-note">Зажми проєкт і потягни, щоб змінити порядок.</div>`;
}

/* Перетягування проєктів: довгий натиск (350 мс без руху) → drag.
   Порядок зберігається в Нору і діє скрізь, де є список проєктів. */
function enableProjectDrag() {
  const list = $("proj-list");
  if (!list) return;
  const drag = { active: false, el: null, timer: null, startY: 0, moved: false };

  const cleanup = () => {
    if (drag.el) drag.el.classList.remove("drag-src");
    clearTimeout(drag.timer);
    drag.active = false;
    drag.el = null;
  };

  list.addEventListener("pointerdown", (e) => {
    const row = e.target.closest("[data-drag-id]");
    if (!row) return;
    drag.el = row;
    drag.startY = e.clientY;
    drag.moved = false;
    drag.timer = setTimeout(() => {
      drag.active = true;
      row.classList.add("drag-src");
      haptic("success");
    }, 350);
  });

  list.addEventListener("pointermove", (e) => {
    if (!drag.el) return;
    if (!drag.active) {
      // рух до активації — це скрол, не drag
      if (Math.abs(e.clientY - drag.startY) > 10) cleanup();
      return;
    }
    drag.moved = true;
    const rows = [...list.querySelectorAll("[data-drag-id]")];
    const over = rows.find((r) => {
      if (r === drag.el) return false;
      const rect = r.getBoundingClientRect();
      return e.clientY > rect.top && e.clientY < rect.bottom;
    });
    if (over) {
      const rect = over.getBoundingClientRect();
      if (e.clientY < rect.top + rect.height / 2) list.insertBefore(drag.el, over);
      else list.insertBefore(drag.el, over.nextSibling);
    }
  });

  // блокуємо скрол сторінки, лише коли drag активний
  list.addEventListener("touchmove", (e) => {
    if (drag.active) e.preventDefault();
  }, { passive: false });

  const finish = async () => {
    if (!drag.el) return;
    const wasDrag = drag.active && drag.moved;
    const el = drag.el;
    cleanup();
    if (!wasDrag) return;
    el.dataset.justDragged = "1";
    setTimeout(() => delete el.dataset.justDragged, 300);
    const ids = [...list.querySelectorAll("[data-drag-id]")].map((r) => +r.dataset.dragId);
    STATE.projects.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
    try {
      await api("/api/projects/order", { method: "PUT", body: JSON.stringify({ ids }) });
      toast("Порядок збережено");
    } catch (e) { toast(e.message); }
  };
  list.addEventListener("pointerup", finish);
  list.addEventListener("pointercancel", cleanup);
}

function timelineHtml() {
  const dated = STATE.projects
    .filter((p) => p.start_date && p.end_date)
    .sort((a, b) => a.start_date - b.start_date);
  const undated = STATE.projects.filter((p) => !(p.start_date && p.end_date));
  if (!dated.length) return `<div class="empty-hint">У проєктів немає дат — таймлайн порожній.</div>`;

  const now = Date.now() / 1000;
  let min = Math.min(...dated.map((p) => p.start_date), now);
  let max = Math.max(...dated.map((p) => p.end_date), now);
  const pad = (max - min) * 0.04;
  min -= pad; max += pad;
  const days = (max - min) / 86400;
  const width = Math.max(720, Math.min(2600, Math.round(days * 2.6)));
  const x = (ts) => ((ts - min) / (max - min)) * width;

  // Місячні поділки; лейбл ставимо не частіше ніж раз на ~56px
  let ticks = "";
  const d = new Date(min * 1000);
  d.setDate(1); d.setHours(0, 0, 0, 0);
  d.setMonth(d.getMonth() + 1);
  const monthPx = width / (days / 30.4);
  const step = Math.max(1, Math.ceil(64 / monthPx));
  let mi = 0;
  for (; d.getTime() / 1000 < max; d.setMonth(d.getMonth() + 1), mi++) {
    const ts = d.getTime() / 1000;
    const label = (mi % step === 0)
      ? `<span>${MONTHS_SHORT[d.getMonth()]} ${String(d.getFullYear()).slice(2)}</span>`
      : "";
    ticks += `<div class="tl-tick" style="left:${x(ts).toFixed(1)}px">${label}</div>`;
  }

  const rows = dated.map((p, i) => {
    const left = x(p.start_date), w = Math.max(48, x(p.end_date) - left);
    const c = projectColorIdx(p.id);
    return `<div class="tl-row">
      <button class="tl-bar c${c}" data-project="${p.id}"
        style="left:${left.toFixed(1)}px;width:${w.toFixed(1)}px">
        <span class="tl-inner">
          ${p.logo ? `<span class="tl-logo">${imgHtml(p.logo, p.logo_orig)}</span>` : ""}
          <span class="tl-lbl">${esc(p.partner || p.name)}</span>
        </span>
      </button>
    </div>`;
  }).join("");

  const nowX = x(now);
  return `
    <div class="tl-scroll" id="tl-scroll" data-nowx="${nowX.toFixed(0)}">
      <div class="tl-canvas" style="width:${width}px;height:${dated.length * 46 + 44}px">
        ${ticks}
        <div class="tl-now" style="left:${nowX.toFixed(1)}px"><span>сьогодні</span></div>
        <div class="tl-rows">${rows}</div>
      </div>
    </div>
    <div class="tl-note">Смуга — строк проєкту, тап відкриває його. Скрольте вбік.</div>
    ${undated.length ? `<div class="tl-note">Без дат у CMS: ${undated.map((p) => esc(p.partner || p.name)).join(", ")}</div>` : ""}`;
}

function renderProject() {
  const p = STATE.projects.find((x) => x.id === STATE.currentProject);
  if (!p) { nav("projects"); return; }
  const quotaTotal = (p.kpi_news || 0) + (p.kpi_articles || 0);
  const planned = p.themes.reduce((s, t) => s + (t.planned || 0), 0);
  $("content").innerHTML = `
    <button class="back" data-nav="projects">${icon("chevron-left")} Проєкти</button>
    <div class="proj-head">
      ${logoSq(p, 56)}
      <div>
        <div class="pn">${esc(p.partner || p.name)}</div>
        <div class="pd">${p.partner ? esc(p.name) + "<br>" : ""}${esc(fmtRange(p))}</div>
      </div>
    </div>
    ${quotaTotal ? `<div class="quota-pill">Квота з сайту: <b>${p.kpi_news || 0} новин + ${p.kpi_articles || 0} статей</b></div>` : ""}
    ${p.drive_url ? `
      <button class="bigpick" id="drive-open" style="margin-top:10px">
        ${icon("folder")} Папка проєкту на Google Drive
        <span class="chev" id="drive-edit" style="margin-left:auto">${icon("edit", "ic chev")}</span>
      </button>`
      : `<button class="add-theme" id="drive-attach" style="margin-top:10px">${icon("folder")} Прикріпити папку Google Drive</button>`}
    <div class="f-label">Тематики</div>
    ${p.themes.map((t) => `
      <div class="theme-row">
        <span class="tn">${esc(t.name)}
          ${formatTitle(t.format) ? `<span class="fmt-badge">${esc(formatTitle(t.format))}</span>` : ""}</span>
        <span class="tc">${t.planned || ""}</span>
        <button class="tact" data-edit-theme="${t.id}" aria-label="Редагувати">${icon("edit")}</button>
      </div>`).join("")}
    <button class="add-theme" id="add-theme">${icon("plus")} Додати тематику</button>
    ${quotaTotal && planned ? `<div class="left-hint">розкроєно ${planned} із ${quotaTotal}${planned < quotaTotal ? ` · <b>ще ${quotaTotal - planned} без тематики</b>` : ""}</div>` : ""}
    <div class="f-label">Звітність і майлстоуни</div>
    ${(p.deadlines || []).map((d) => `
      <button class="theme-row" data-edit-dl="${d.id}">
        <span class="tn">${esc(dlLabel(d))}</span>
        ${dlDateHtml(d)}
      </button>`).join("")}
    <button class="add-theme" id="add-dl">${icon("calendar")} Додати дедлайн звіту</button>`;
  $("add-theme").onclick = () => themeSheet(p, null);
  $("content").querySelectorAll("[data-edit-theme]").forEach((b) =>
    b.onclick = () => themeSheet(p, p.themes.find((t) => t.id === +b.dataset.editTheme)));
  $("add-dl").onclick = () => dlSheet(p, null);
  $("content").querySelectorAll("[data-edit-dl]").forEach((b) =>
    b.onclick = () => dlSheet(p, p.deadlines.find((d) => d.id === +b.dataset.editDl)));
  if (p.drive_url) {
    $("drive-open").onclick = (e) => {
      if (e.target.closest("#drive-edit")) { driveSheet(p); return; }
      try { tg.openLink(p.drive_url); } catch (err) { window.open(p.drive_url, "_blank"); }
    };
  } else {
    $("drive-attach").onclick = () => driveSheet(p);
  }
}

function driveSheet(p) {
  openSheet(`
    <h2>Папка на Google Drive</h2>
    <div class="field"><label>Лінк на папку проєкту</label>
      <input id="dr-url" type="url" placeholder="https://drive.google.com/drive/folders/…"
        value="${esc(p.drive_url || "")}"></div>
    <div class="sheet-actions">
      ${p.drive_url ? `<button class="sbtn danger" id="dr-remove">Відкріпити</button>` : ""}
      <button class="sbtn" id="dr-cancel">Скасувати</button>
      <button class="sbtn primary" id="dr-save">Зберегти</button>
    </div>`);
  $("dr-cancel").onclick = closeSheet;
  const save = async (url) => {
    try {
      await api(`/api/projects/${p.id}/drive`, { method: "PUT", body: JSON.stringify({ url }) });
      closeSheet();
      haptic("success");
      await reload();
      nav("project", p.id);
    } catch (e) { toast(e.message); }
  };
  if (p.drive_url) $("dr-remove").onclick = () => save("");
  $("dr-save").onclick = () => {
    const url = $("dr-url").value.trim();
    if (!url) { toast("Встав лінк на папку"); return; }
    save(url);
  };
}

/* Шторка дедлайну звітності: наративний/фінансовий звіт (проміжний/фінальний)
   або майлстоун з назвою. Нагадування Лиса — майбутній шар. */
function dlSheet(project, dl) {
  const st = {
    kind: dl ? dl.kind : "narrative",
    stage: dl ? dl.stage || "interim" : "interim",
    due: dl ? dl.due : "",
  };
  const paint = () => {
    $("dl-kind").querySelectorAll(".chip").forEach((c) =>
      c.classList.toggle("on", c.dataset.k === st.kind));
    $("dl-stage-block").classList.toggle("hidden", st.kind === "milestone");
    $("dl-stage").querySelectorAll(".chip").forEach((c) =>
      c.classList.toggle("on", c.dataset.s === st.stage));
    $("dl-title-label").textContent =
      st.kind === "milestone" ? "Назва майлстоуна" : "Нотатка (необовʼязково)";
  };
  openSheet(`
    <h2>${dl ? "Дедлайн" : "Новий дедлайн"}</h2>
    <div class="f-label" style="margin-top:0">Тип</div>
    <div class="chips" id="dl-kind">${DL_KINDS.map((k) => `
      <button class="chip" data-k="${k.v}">${k.label}</button>`).join("")}</div>
    <div id="dl-stage-block">
      <div class="f-label">Стадія</div>
      <div class="chips" id="dl-stage">${DL_STAGES.map((s) => `
        <button class="chip" data-s="${s.v}">${s.label}</button>`).join("")}</div>
    </div>
    <div class="f-label" id="dl-title-label">Нотатка</div>
    <input id="dl-title" maxlength="120" placeholder="напр. звіт для IMS за Q3" value="${dl ? esc(dl.title) : ""}">
    <div class="f-label">Дата</div>
    <input id="dl-due" type="date" value="${st.due}">
    <div class="sheet-actions">
      ${dl ? `<button class="sbtn danger" id="dl-delete">Видалити</button>` : ""}
      <button class="sbtn" id="dl-cancel">Скасувати</button>
      <button class="sbtn primary" id="dl-save">Зберегти</button>
    </div>`);
  paint();
  $("dl-kind").onclick = (e) => {
    const b = e.target.closest("[data-k]");
    if (b) { st.kind = b.dataset.k; paint(); }
  };
  $("dl-stage").onclick = (e) => {
    const b = e.target.closest("[data-s]");
    if (b) { st.stage = b.dataset.s; paint(); }
  };
  $("dl-cancel").onclick = closeSheet;
  if (dl) $("dl-delete").onclick = async () => {
    try {
      await api(`/api/project_deadlines/${dl.id}`, { method: "DELETE" });
      closeSheet();
      await reload();
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
  $("dl-save").onclick = async () => {
    const due = $("dl-due").value;
    if (!due) { toast("Потрібна дата"); return; }
    const body = {
      project_id: project.id,
      kind: st.kind,
      stage: st.kind === "milestone" ? null : st.stage,
      title: $("dl-title").value.trim(),
      due,
    };
    if (st.kind === "milestone" && !body.title) { toast("Майлстоуну потрібна назва"); return; }
    try {
      if (dl) await api(`/api/project_deadlines/${dl.id}`, { method: "PATCH", body: JSON.stringify(body) });
      else await api("/api/project_deadlines", { method: "POST", body: JSON.stringify(body) });
      closeSheet();
      haptic("success");
      await reload();
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
}

function themeSheet(project, theme) {
  let fmt = theme ? theme.format || null : null;
  openSheet(`
    <h2>${theme ? "Тематика" : "Нова тематика"}</h2>
    <div class="field"><label>Назва</label>
      <input id="t-name" maxlength="120" placeholder="напр. Репортажі з сесій" value="${theme ? esc(theme.name) : ""}"></div>
    <div class="field"><label>Формат</label>
      <div class="chips" id="t-format">${THEME_FORMATS.map((f) => `
        <button class="chip ${fmt === f.v ? "on" : ""}" data-f="${f.v || ""}">${f.label}</button>`).join("")}</div></div>
    <div class="field"><label>Скільки матеріалів (необовʼязково)</label>
      <input id="t-planned" type="number" min="0" max="999" inputmode="numeric" value="${theme && theme.planned ? theme.planned : ""}"></div>
    <div class="sheet-actions">
      ${theme ? `<button class="sbtn danger" id="t-delete">Видалити</button>` : ""}
      <button class="sbtn" id="t-cancel">Скасувати</button>
      <button class="sbtn primary" id="t-save">Зберегти</button>
    </div>`);
  $("t-format").onclick = (e) => {
    const b = e.target.closest("[data-f]");
    if (!b) return;
    fmt = b.dataset.f || null;
    $("t-format").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("t-cancel").onclick = closeSheet;
  if (theme) $("t-delete").onclick = async () => {
    try {
      await api(`/api/themes/${theme.id}`, { method: "DELETE" });
      closeSheet();
      await reload();
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
  $("t-save").onclick = async () => {
    const name = $("t-name").value.trim();
    if (!name) { toast("Потрібна назва"); return; }
    const planned = $("t-planned").value ? +$("t-planned").value : null;
    try {
      if (theme) await api(`/api/themes/${theme.id}`, { method: "PATCH", body: JSON.stringify({ name, planned, format: fmt }) });
      else await api("/api/themes", { method: "POST", body: JSON.stringify({ project_id: project.id, name, planned, format: fmt }) });
      closeSheet();
      haptic("success");
      await reload();
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
}

/* ---------- KPI: норми по відділах, правки по людині ---------- */

async function loadKpi() {
  STATE.kpi = await api("/api/kpi");
}

function normTitle(n) {
  const own = n.own ? (n.target === 1 ? "власна " : "власних ") : "";
  return `${n.target} ${own}${qtyWord(n.metric, n.target)} · ${n.period === "week" ? "щотижня" : "щомісяця"}`;
}

function normById(id) {
  return (STATE.kpi ? STATE.kpi.norms : []).find((n) => n.id === id) || null;
}

async function renderKpi() {
  if (!STATE.kpi) {
    $("content").innerHTML = `<div class="h-big">KPI</div><div class="empty-hint">Завантажую…</div>`;
    try { await loadKpi(); } catch (e) { toast(e.message); return; }
    if (STATE.view !== "kpi") return;
  }
  const k = STATE.kpi;
  const byDept = {};
  k.norms.forEach((n) => (byDept[n.dept_title] = byDept[n.dept_title] || []).push(n));
  const sections = Object.entries(byDept).map(([dept, norms]) => `
    <div class="dept-title">${esc(dept)}</div>
    ${norms.map((n) => {
      const active = n.rows.filter((r) => !r.excused);
      const done = active.filter((r) => r.done).length;
      return `<button class="norm-row" data-norm="${n.id}">
        <span class="nr-txt">
          <span class="nr-title">${esc(normTitle(n))}</span>
          <span class="nr-meta">${n.period === "week" ? esc(k.week_label) : esc(k.month_label)} · на людину</span>
        </span>
        <span class="nr-done">${k.site_db ? `${done}/${active.length} ✓` : ""}</span>
        ${icon("chevron-right", "ic chev")}
      </button>`;
    }).join("")}`).join("");
  $("content").innerHTML = `
    <div class="h-big">KPI</div>
    <div class="h-sub">тиждень ${esc(k.week_label)} · ${esc(k.month_label)} · норма на відділ, облік по людині</div>
    ${sections || `<div class="empty-hint">Норм ще немає.<br>Додай першу — і прогрес рахуватиметься сам із сайту.</div>`}
    <button class="add-theme" id="add-norm">${icon("plus")} Додати норму</button>
    ${!k.site_db ? `<div class="tl-note">БД сайту недоступна — факт тимчасово не рахується.</div>` : ""}`;
  $("add-norm").onclick = normCreateSheet;
  $("content").querySelectorAll("[data-norm]").forEach((b) =>
    b.onclick = () => nav("kpinorm", +b.dataset.norm));
}

function normCreateSheet() {
  const depts = STATE.people.reduce((acc, p) => {
    if (!acc.find((d) => d.dept === p.dept)) acc.push({ dept: p.dept, title: p.dept_title });
    return acc;
  }, []);
  const st = { dept: depts[0] ? depts[0].dept : null, metric: "news", period: "week", target: 5, own: false };
  openSheet(`
    <h2>Нова норма</h2>
    <div class="f-label" style="margin-top:0">Відділ</div>
    <div class="chips" id="n-dept">${depts.map((d) => `
      <button class="chip ${st.dept === d.dept ? "on" : ""}" data-dept="${esc(d.dept)}">${esc(d.title)}</button>`).join("")}</div>
    <div class="f-label">Метрика</div>
    <div class="two" id="n-metric">
      <button class="bigbtn slim on" data-m="news">Новини</button>
      <button class="bigbtn slim" data-m="article">Статті</button>
    </div>
    <div class="f-label">Які матеріали</div>
    <div class="two" id="n-own">
      <button class="bigbtn slim on" data-o="all">Усі</button>
      <button class="bigbtn slim" data-o="own">Лише власні</button>
    </div>
    <div class="f-label">Період</div>
    <div class="two" id="n-period">
      <button class="bigbtn slim on" data-p="week">Тиждень</button>
      <button class="bigbtn slim" data-p="month">Місяць</button>
    </div>
    <div class="f-label">Ціль на людину</div>
    <div class="stepper">
      <button id="n-minus">${icon("minus")}</button>
      <b id="n-val">${st.target}</b>
      <button id="n-plus">${icon("plus")}</button>
    </div>
    <div class="sheet-actions">
      <button class="sbtn" id="n-cancel">Скасувати</button>
      <button class="sbtn primary" id="n-save">Створити</button>
    </div>`);
  $("n-dept").onclick = (e) => {
    const b = e.target.closest("[data-dept]");
    if (!b) return;
    st.dept = b.dataset.dept;
    $("n-dept").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("n-metric").onclick = (e) => {
    const b = e.target.closest("[data-m]");
    if (!b) return;
    st.metric = b.dataset.m;
    $("n-metric").querySelectorAll(".bigbtn").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("n-own").onclick = (e) => {
    const b = e.target.closest("[data-o]");
    if (!b) return;
    st.own = b.dataset.o === "own";
    $("n-own").querySelectorAll(".bigbtn").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("n-period").onclick = (e) => {
    const b = e.target.closest("[data-p]");
    if (!b) return;
    st.period = b.dataset.p;
    $("n-period").querySelectorAll(".bigbtn").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("n-minus").onclick = () => { st.target = Math.max(1, st.target - 1); $("n-val").textContent = st.target; };
  $("n-plus").onclick = () => { st.target = Math.min(500, st.target + 1); $("n-val").textContent = st.target; };
  $("n-cancel").onclick = closeSheet;
  $("n-save").onclick = async () => {
    try {
      await api("/api/kpi/norms", { method: "POST", body: JSON.stringify(st) });
      closeSheet();
      haptic("success");
      await loadKpi();
      renderKpi();
    } catch (e) { toast(e.message); }
  };
}

function renderKpiNorm() {
  const n = normById(STATE.currentNorm);
  if (!n) { nav("kpi"); return; }
  const k = STATE.kpi;
  const rows = n.rows.map((r) => {
    const pct = r.fact === null || r.target <= 0 ? 0 : Math.min(100, Math.round(r.fact / r.target * 100));
    const right = r.excused
      ? `<span class="kp-excused">звільнена</span>`
      : r.fact === null
        ? `<span class="kp-nofact">—</span>`
        : `<span class="kp-fact ${r.done ? "ok" : ""}">${r.fact}/${r.target}${r.done ? " ✓" : ""}</span>`;
    return `<button class="kpi-person" data-kp="${esc(r.person)}">
      ${avatar(r.person, personEntry(r.person), 42)}
      <span class="kp-main">
        <span class="kp-name">${esc(r.person)}
          ${r.overridden ? `<span class="kp-badge">${esc(r.note || "правка")}</span>` : ""}</span>
        ${!r.excused ? `<span class="kbar"><i class="${r.done ? "ok" : ""}" style="width:${pct}%"></i></span>` : ""}
      </span>
      ${right}
    </button>`;
  }).join("");
  $("content").innerHTML = `
    <button class="back" data-nav="kpi">${icon("chevron-left")} KPI</button>
    <div class="h-big">${esc(n.dept_title)}</div>
    <div class="h-sub">${esc(normTitle(n))} · ${esc(n.period === "week" ? k.week_label : k.month_label)}</div>
    ${rows}
    <div class="tl-note">Тап по людині — правка цього періоду: інша ціль,
      звільнення (відпустка/відрядження) або повернення до норми відділу.</div>
    <div class="sheet-actions" style="margin-top:14px">
      <button class="sbtn danger" id="norm-delete">Видалити норму</button>
      <button class="sbtn" id="norm-edit">Змінити ціль</button>
    </div>`;
  $("norm-delete").onclick = async () => {
    try {
      await api(`/api/kpi/norms/${n.id}`, { method: "DELETE" });
      haptic("success");
      await loadKpi();
      nav("kpi");
    } catch (e) { toast(e.message); }
  };
  $("norm-edit").onclick = () => normTargetSheet(n);
  $("content").querySelectorAll("[data-kp]").forEach((b) =>
    b.onclick = () => overrideSheet(n, n.rows.find((r) => r.person === b.dataset.kp)));
}

function normTargetSheet(n) {
  let target = n.target;
  openSheet(`
    <h2>Ціль відділу</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">Зміниться для всіх без персональних правок.</p>
    <div class="stepper">
      <button id="e-minus">${icon("minus")}</button>
      <b id="e-val">${target}</b>
      <button id="e-plus">${icon("plus")}</button>
    </div>
    <div class="sheet-actions">
      <button class="sbtn" id="e-cancel">Скасувати</button>
      <button class="sbtn primary" id="e-save">Зберегти</button>
    </div>`);
  $("e-minus").onclick = () => { target = Math.max(1, target - 1); $("e-val").textContent = target; };
  $("e-plus").onclick = () => { target = Math.min(500, target + 1); $("e-val").textContent = target; };
  $("e-cancel").onclick = closeSheet;
  $("e-save").onclick = async () => {
    try {
      await api(`/api/kpi/norms/${n.id}`, { method: "PATCH", body: JSON.stringify({ target }) });
      closeSheet();
      await loadKpi();
      renderKpiNorm();
    } catch (e) { toast(e.message); }
  };
}

function overrideSheet(n, row) {
  let target = row.excused ? 0 : row.target;
  const paint = () => {
    $("o-val").textContent = target;
    $("o-hint").textContent = target === 0 ? "0 — звільнена цього періоду" : "";
  };
  openSheet(`
    <h2>${esc(row.person)}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(normTitle(n))} · норма відділу: ${n.base_target || n.target}</p>
    <div class="f-label" style="margin-top:0">Ціль на цей ${n.period === "week" ? "тиждень" : "місяць"}</div>
    <div class="stepper">
      <button id="o-minus">${icon("minus")}</button>
      <b id="o-val">${target}</b>
      <button id="o-plus">${icon("plus")}</button>
    </div>
    <div id="o-hint" style="color:var(--muted);font-size:12px;margin-top:6px"></div>
    <div class="f-label">Причина (необовʼязково)</div>
    <input id="o-note" maxlength="120" placeholder="відпустка, відрядження…" value="${esc(row.note || "")}">
    <div class="sheet-actions">
      ${row.overridden ? `<button class="sbtn" id="o-clear">До норми відділу</button>` : ""}
      <button class="sbtn danger" id="o-excuse">Звільнити</button>
      <button class="sbtn primary" id="o-save">Зберегти</button>
    </div>`);
  paint();
  $("o-minus").onclick = () => { target = Math.max(0, target - 1); paint(); };
  $("o-plus").onclick = () => { target = Math.min(500, target + 1); paint(); };
  const apply = async (body) => {
    try {
      await api("/api/kpi/override", { method: "PUT", body: JSON.stringify({ norm_id: n.id, person: row.person, ...body }) });
      closeSheet();
      haptic("success");
      await loadKpi();
      renderKpiNorm();
    } catch (e) { toast(e.message); }
  };
  if (row.overridden) $("o-clear").onclick = () => apply({ clear: true });
  $("o-excuse").onclick = () => apply({ target: 0, note: $("o-note").value.trim() || "звільнена" });
  $("o-save").onclick = () => apply({ target, note: $("o-note").value.trim() });
}

/* ---------- Команда ---------- */

function renderTeam() {
  const byDept = {};
  STATE.people.forEach((p) => (byDept[p.dept_title] = byDept[p.dept_title] || []).push(p));
  $("content").innerHTML = `
    <div class="h-big">Команда</div>
    <div class="h-sub">тап по людині — перенести між відділами</div>
    ${Object.entries(byDept).map(([dept, list]) => `
      <div class="dept-title">${esc(dept)} · ${list.length}</div>
      ${list.map((p) => `
        <button class="team-row" data-move="${esc(p.name)}">
          ${avatar(p.name, p, 46)}
          <div style="flex:1;text-align:left"><div class="tn">${esc(p.name)}</div>
            <div class="td">${esc(p.dept_title)}</div></div>
          ${icon("chevron-right", "ic chev")}
        </button>`).join("")}`).join("")}`;
  $("content").querySelectorAll("[data-move]").forEach((b) =>
    b.onclick = () => deptSheet(STATE.people.find((p) => p.name === b.dataset.move)));
}

function deptSheet(p) {
  if (!p) return;
  let dept = p.dept;
  const depts = [
    ["newsroom", "Newsroom"], ["creative", "Creative"], ["digital", "Діджитал та дистрибуція"],
  ];
  openSheet(`
    <h2>${esc(p.name)}</h2>
    <div class="f-label" style="margin-top:0">Відділ</div>
    <div class="chips" id="d-pick">${depts.map(([v, t]) => `
      <button class="chip ${dept === v ? "on" : ""}" data-d="${v}">${t}</button>`).join("")}</div>
    <p style="color:var(--muted);font-size:12.5px;margin-top:12px">Відділ визначає,
      які KPI-норми діють на людину — з моменту перенесення.</p>
    <div class="sheet-actions">
      <button class="sbtn" id="d-cancel">Скасувати</button>
      <button class="sbtn primary" id="d-save">Зберегти</button>
    </div>`);
  $("d-pick").onclick = (e) => {
    const b = e.target.closest("[data-d]");
    if (!b) return;
    dept = b.dataset.d;
    $("d-pick").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  };
  $("d-cancel").onclick = closeSheet;
  $("d-save").onclick = async () => {
    try {
      await api("/api/people/dept", { method: "PUT", body: JSON.stringify({ person: p.name, dept }) });
      closeSheet();
      haptic("success");
      await reload();
      renderTeam();
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Журналістський режим (read-only, інтерфейс — наступний крок) ---------- */

function renderJournalist() {
  const open = STATE.tasks.filter((t) => t.status === "open");
  $("content").innerHTML = `
    <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
    <div class="h-sub">твої завдання і KPI</div>
    <div id="my-kpi"></div>
    ${open.length ? `<div class="soft-card">${open.map((t) => {
      const tp = taskProject(t);
      return `
      <div class="task-row">
        ${tp.logoHtml || ""}
        <span class="tr-main">
          <span class="tr-who">${esc(tp.partner || tp.projName || "Позапроєктне завдання")}</span>
          <span class="tr-what">${esc(taskLine2(t))}</span>
          ${t.note ? `<span class="tr-what">${esc(t.note)}</span>` : ""}
        </span>
        <span class="tr-right">
          ${deadlineBadge(t)}
          <span class="status-dot open"></span>
        </span>
      </div>`;
    }).join("")}</div>`
      : `<div class="empty-hint">Відкритих завдань немає.</div>`}
    <div class="empty-hint" style="padding-top:16px">Це попередній перегляд —
      повний твій інтерфейс уже в розробці.</div>`;
  renderMyKpi();
}

/* «Мої KPI» журналістки: норми її відділу зі своїм фактом тижня/місяця.
   Вантажиться після основного екрана — щоб таски не чекали на MySQL сайту. */
async function renderMyKpi() {
  let k;
  try { k = await api("/api/kpi"); } catch (e) { return; }
  const box = $("my-kpi");
  if (!box || !k.norms.length) return;
  box.innerHTML = `<div class="soft-card"><div class="sc-t">Мої KPI</div>
    ${k.norms.map((n) => {
      const r = n.rows[0];
      if (!r) return "";
      if (r.excused) return `<div class="mykpi-row">
        <span class="mk-t">${esc(normTitle(n))}</span>
        <span class="kp-excused">звільнена${r.note ? " · " + esc(r.note) : ""}</span></div>`;
      const pct = r.fact === null || r.target <= 0 ? 0 : Math.min(100, Math.round(r.fact / r.target * 100));
      return `<div class="mykpi-row">
        <span class="mk-t">${esc(normTitle(n))}
          <span class="mk-p">· ${esc(n.period === "week" ? k.week_label : k.month_label)}</span></span>
        <span class="kp-fact ${r.done ? "ok" : ""}">${r.fact === null ? "—" : `${r.fact}/${r.target}${r.done ? " ✓" : ""}`}</span>
        <span class="kbar wide"><i class="${r.done ? "ok" : ""}" style="width:${pct}%"></i></span>
      </div>`;
    }).join("")}</div>`;
}

/* ---------- Шторка ---------- */

function openSheet(html) {
  $("sheet").innerHTML = html;
  $("sheet-backdrop").classList.remove("hidden");
}
function closeSheet() {
  $("sheet-backdrop").classList.add("hidden");
}

/* ---------- Завантаження ---------- */

async function reload() {
  const data = await api("/api/bootstrap");
  STATE.me = data.me;
  STATE.tasks = data.tasks || [];
  STATE.people = data.people || [];
  STATE.projects = data.projects || [];
}

function fail(title, text) {
  $("screen-loading").classList.add("hidden");
  $("screen-main").classList.add("hidden");
  $("screen-error").classList.remove("hidden");
  $("error-title").textContent = title;
  $("error-text").textContent = text;
}

async function boot() {
  if (!tg || !tg.initData) {
    fail("Тільки з Telegram", "Ця сторінка працює як Telegram Mini App. Відкрий її через @mykvisti_bot → /team.");
    return;
  }
  tg.ready();
  tg.expand();
  const syncTheme = () => document.body.classList.toggle("dark", tg.colorScheme === "dark");
  syncTheme();
  try { tg.onEvent("themeChanged", syncTheme); } catch (e) {}
  try {
    await reload();
    $("screen-loading").classList.add("hidden");
    $("screen-main").classList.remove("hidden");
    if (STATE.me.manager) {
      $("bottomnav").classList.remove("hidden");
      nav("home");
    } else {
      renderJournalist();
    }
  } catch (e) {
    fail("Не пустили", e.message);
  }
}

/* ---------- Події ---------- */

$("content").addEventListener("click", (e) => {
  const navBtn = e.target.closest("[data-nav]");
  if (navBtn) { nav(navBtn.dataset.nav); return; }
  const person = e.target.closest("[data-person]");
  if (person) { nav("form", person.dataset.person); return; }
  const proj = e.target.closest("[data-project]");
  if (proj) {
    if (proj.dataset.justDragged) return; // клік одразу після перетягування — не навігація
    nav("project", +proj.dataset.project);
    return;
  }
  const task = e.target.closest("[data-task]");
  if (task && STATE.me.manager) {
    const t = STATE.tasks.find((x) => x.id === +task.dataset.task);
    if (t) taskSheet(t);
  }
});

$("bottomnav").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (btn) nav(btn.dataset.view);
});

$("sheet-backdrop").addEventListener("click", (e) => {
  if (e.target === $("sheet-backdrop")) closeSheet();
});

boot();
