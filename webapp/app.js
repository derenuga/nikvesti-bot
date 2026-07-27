/* Mini App «Команда» — прототип редакторського інтерфейсу (концепція v2).
   Флоу Каті: «+» → людина → creative task (проєкт/позапроєкт, тип, тематика,
   кількість, нотатка). Проєкти і фото — з БД сайту, тематики й таски — з Нори.
   Журналіст поки бачить свої завдання read-only — його інтерфейс наступний. */

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (id) => document.getElementById(id);

const STATE = {
  me: null,
  tasks: [],
  people: [],
  projects: [],
  view: "home",
  currentProject: null,
  form: null,
};

const TYPE_WORDS = {
  news: { one: "новина", few: "новини", many: "новин" },
  article: { one: "стаття", few: "статті", many: "статей" },
};

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

function avatar(person, photo, size) {
  return `<span class="ava" style="width:${size}px;height:${size}px">
    <span class="init" style="font-size:${Math.round(size / 3)}px">${esc(initials(person))}</span>
    ${photo ? `<img src="${esc(photo)}" alt="" loading="lazy" onerror="this.remove()">` : ""}
  </span>`;
}

function logoSq(name, logo, size) {
  return `<span class="logo-sq" style="width:${size}px;height:${size}px">
    <span class="init" style="font-size:${Math.round(size / 3.4)}px">${esc(initials(name))}</span>
    ${logo ? `<img src="${esc(logo)}" alt="" loading="lazy" onerror="this.remove()">` : ""}
  </span>`;
}

function fmtUnixDate(ts) {
  if (!ts) return null;
  return new Date(ts * 1000).toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function qtyWord(type, qty) {
  const w = TYPE_WORDS[type];
  if (qty === 1) return w.one;
  return qty < 5 ? w.few : w.many;
}

function taskSummary(t) {
  let line = t.qty > 1 ? `${t.qty} ${qtyWord(t.type, t.qty)}` : qtyWord(t.type, 1);
  if (t.project_name) line += ` · ${t.project_name}`;
  else line += " · позапроєктне";
  if (t.theme_name) line += ` (${t.theme_name})`;
  return line;
}

function personPhoto(name) {
  const p = STATE.people.find((x) => x.name === name);
  return p ? p.photo : null;
}

/* ---------- Навігація ---------- */

function nav(view, arg) {
  STATE.view = view;
  if (view === "project") STATE.currentProject = arg;
  if (view === "form") STATE.form = {
    person: arg, project: undefined, type: "news", theme_id: null, qty: 1, note: "",
  };
  document.querySelectorAll("#bottomnav .bn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === view));
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
  else if (v === "team") renderTeam();
}

/* ---------- Головна ---------- */

function renderHome() {
  const open = STATE.tasks.filter((t) => t.status === "open");
  const closed = STATE.tasks.filter((t) => t.status !== "open").slice(0, 10);
  const row = (t) => `
    <button class="task-row" data-task="${t.id}">
      ${avatar(t.person, personPhoto(t.person), 42)}
      <span class="tr-main">
        <span class="tr-who">${esc(t.person.split(" ")[0])} ${esc(t.person.split(" ")[1] || "")}</span>
        <span class="tr-what">${esc(taskSummary(t))}</span>
      </span>
      <span class="status-dot ${t.status}"></span>
    </button>`;
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
    <p style="color:var(--muted);margin:-8px 0 6px">${esc(taskSummary(t))}</p>
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
          ${avatar(p.name, p.photo, 76)}
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
      : `${logoSq(proj.name, proj.logo, 32)} ${esc(proj.name)}`;
  $("content").innerHTML = `
    <button class="back" data-nav="people">${icon("chevron-left")} Назад</button>
    <div class="who">
      ${avatar(f.person, personPhoto(f.person), 48)}
      <div><div class="wn">${esc(f.person)}</div>
      <div class="wd">${esc((STATE.people.find((p) => p.name === f.person) || {}).dept_title || "")}</div></div>
    </div>

    <div class="f-label">Проєкт</div>
    <button class="bigpick" id="f-project">${projLabel}${icon("chevron-right", "ic chev")}</button>

    <div class="f-label">Тип матеріалу</div>
    <div class="two">
      <button class="bigbtn ${f.type === "news" ? "on" : ""}" data-type="news">Новина</button>
      <button class="bigbtn ${f.type === "article" ? "on" : ""}" data-type="article">Стаття</button>
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

    <div class="f-label">Кількість і нотатка</div>
    <div class="count-row">
      <div class="stepper">
        <button id="qty-minus">${icon("minus")}</button>
        <b id="qty-val">${f.qty}</b>
        <button id="qty-plus">${icon("plus")}</button>
      </div>
      <textarea id="f-note" maxlength="1000" placeholder="нотатка, тема, деталі…">${esc(f.note)}</textarea>
    </div>

    <button class="cta" id="f-create" ${f.project === undefined ? "disabled" : ""}>Поставити завдання</button>`;

  $("f-project").onclick = projectPickerSheet;
  $("qty-minus").onclick = () => { f.qty = Math.max(1, f.qty - 1); $("qty-val").textContent = f.qty; };
  $("qty-plus").onclick = () => { f.qty = Math.min(99, f.qty + 1); $("qty-val").textContent = f.qty; };
  $("f-note").oninput = (e) => { f.note = e.target.value; };
  $("content").querySelectorAll("[data-type]").forEach((b) => b.onclick = () => { f.type = b.dataset.type; renderForm(); });
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
        ${logoSq(p.name, p.logo, 40)}
        <span>
          <span class="pk-name">${esc(p.name)}</span>
          <span class="pk-meta">${p.end_date ? "до " + fmtUnixDate(p.end_date) : ""}${p.kpi_news || p.kpi_articles ? ` · квота ${p.kpi_news || 0}+${p.kpi_articles || 0}` : ""}</span>
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

/* ---------- Проєкти ---------- */

function renderProjects() {
  $("content").innerHTML = `
    <div class="h-big">Проєкти</div>
    <div class="h-sub">квоти — з CMS сайту, тематики — тут</div>
    ${STATE.projects.length ? STATE.projects.map((p) => `
      <button class="proj-row" data-project="${p.id}">
        ${logoSq(p.name, p.logo, 46)}
        <span>
          <span class="pr-name">${esc(p.name)}</span>
          <span class="pr-meta">${p.end_date ? "до " + fmtUnixDate(p.end_date) : "без строку"}
            ${p.kpi_news || p.kpi_articles ? ` · квота ${p.kpi_news || 0} новин + ${p.kpi_articles || 0} статей` : ""}
            · тематик: ${p.themes.length}</span>
        </span>
        ${icon("chevron-right", "ic chev")}
      </button>`).join("")
      : `<div class="empty-hint">Не бачу проєктів — БД сайту недоступна.</div>`}`;
}

function renderProject() {
  const p = STATE.projects.find((x) => x.id === STATE.currentProject);
  if (!p) { nav("projects"); return; }
  const quotaTotal = (p.kpi_news || 0) + (p.kpi_articles || 0);
  const planned = p.themes.reduce((s, t) => s + (t.planned || 0), 0);
  $("content").innerHTML = `
    <button class="back" data-nav="projects">${icon("chevron-left")} Проєкти</button>
    <div class="proj-head">
      ${logoSq(p.name, p.logo, 56)}
      <div>
        <div class="pn">${esc(p.name)}</div>
        <div class="pd">${p.partner ? esc(p.partner) + " · " : ""}${p.end_date ? "до " + fmtUnixDate(p.end_date) : "без строку"}</div>
      </div>
    </div>
    ${quotaTotal ? `<div class="quota-pill">Квота з сайту: <b>${p.kpi_news || 0} новин + ${p.kpi_articles || 0} статей</b></div>` : ""}
    <div class="f-label">Тематики</div>
    ${p.themes.map((t) => `
      <div class="theme-row">
        <span class="tn">${esc(t.name)}</span>
        <span class="tc">${t.planned || ""}</span>
        <button class="tact" data-edit-theme="${t.id}" aria-label="Редагувати">${icon("edit")}</button>
      </div>`).join("")}
    <button class="add-theme" id="add-theme">${icon("plus")} Додати тематику</button>
    ${quotaTotal && planned ? `<div class="left-hint">розкроєно ${planned} із ${quotaTotal}${planned < quotaTotal ? ` · <b>ще ${quotaTotal - planned} без тематики</b>` : ""}</div>` : ""}`;
  $("add-theme").onclick = () => themeSheet(p, null);
  $("content").querySelectorAll("[data-edit-theme]").forEach((b) =>
    b.onclick = () => themeSheet(p, p.themes.find((t) => t.id === +b.dataset.editTheme)));
}

function themeSheet(project, theme) {
  openSheet(`
    <h2>${theme ? "Тематика" : "Нова тематика"}</h2>
    <div class="field"><label>Назва</label>
      <input id="t-name" maxlength="120" placeholder="напр. Репортажі з сесій" value="${theme ? esc(theme.name) : ""}"></div>
    <div class="field"><label>Скільки матеріалів (необовʼязково)</label>
      <input id="t-planned" type="number" min="0" max="999" inputmode="numeric" value="${theme && theme.planned ? theme.planned : ""}"></div>
    <div class="sheet-actions">
      ${theme ? `<button class="sbtn danger" id="t-delete">Видалити</button>` : ""}
      <button class="sbtn" id="t-cancel">Скасувати</button>
      <button class="sbtn primary" id="t-save">Зберегти</button>
    </div>`);
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
      if (theme) await api(`/api/themes/${theme.id}`, { method: "PATCH", body: JSON.stringify({ name, planned }) });
      else await api("/api/themes", { method: "POST", body: JSON.stringify({ project_id: project.id, name, planned }) });
      closeSheet();
      haptic("success");
      await reload();
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
}

/* ---------- KPI (заглушка) і Команда ---------- */

function renderKpi() {
  $("content").innerHTML = `
    <div class="h-big">KPI</div>
    <div class="h-sub">рекурентні норми на тиждень і місяць</div>
    <div class="soft-card">
      <div class="sc-t">Скоро</div>
      <p style="color:var(--muted)">Налаштування зʼявляться після рішення,
      як задаються норми: на відділ чи персонально. Правки по людині
      (відпустка, відрядження) будуть у будь-якому разі.</p>
    </div>`;
}

function renderTeam() {
  $("content").innerHTML = `
    <div class="h-big">Команда</div>
    <div class="h-sub">фото — з профілів на сайті</div>
    ${STATE.people.map((p) => `
      <div class="team-row">
        ${avatar(p.name, p.photo, 46)}
        <div><div class="tn">${esc(p.name)}</div><div class="td">${esc(p.dept_title)}</div></div>
      </div>`).join("")}`;
}

/* ---------- Журналістський режим (read-only, інтерфейс — наступний крок) ---------- */

function renderJournalist() {
  const open = STATE.tasks.filter((t) => t.status === "open");
  $("content").innerHTML = `
    <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
    <div class="h-sub">твої завдання</div>
    ${open.length ? `<div class="soft-card">${open.map((t) => `
      <div class="task-row">
        <span class="tr-main">
          <span class="tr-who">${esc(taskSummary(t))}</span>
          ${t.note ? `<span class="tr-what">${esc(t.note)}</span>` : ""}
        </span>
        <span class="status-dot open"></span>
      </div>`).join("")}</div>`
      : `<div class="empty-hint">Відкритих завдань немає.</div>`}
    <div class="empty-hint" style="padding-top:16px">Це попередній перегляд —
      повний твій інтерфейс уже в розробці.</div>`;
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
  if (proj) { nav("project", +proj.dataset.project); return; }
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
