/* Mini App «Команда» — логіка (хвиля 1: таски).
   Ролі: менеджер (Олег/головред) — ставить і приймає; журналістка — виконує.
   Авторизація: initData Telegram у кожному запиті, сервер сам вирішує, хто ти. */

const tg = window.Telegram ? window.Telegram.WebApp : null;

const $ = (id) => document.getElementById(id);

const STATE = {
  me: null,
  tasks: [],
  roster: [],
  tab: "board",
};

const STATUS_TITLES = {
  todo: "Черга",
  doing: "У роботі",
  review: "На перевірці",
  done: "Перемоги тижня",
  dropped: "Зняті",
};

const PRIO_ICON = { 0: "", 1: "", 2: "🔥" };

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
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
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

function confetti() {
  const layer = $("confetti-layer");
  const emoji = ["🦊", "🎉", "⭐", "🧡", "✨", "🏆"];
  for (let i = 0; i < 26; i++) {
    const c = document.createElement("div");
    c.className = "confetto";
    c.textContent = emoji[i % emoji.length];
    c.style.left = Math.random() * 100 + "vw";
    c.style.animationDuration = 1.6 + Math.random() * 1.6 + "s";
    c.style.animationDelay = Math.random() * 0.4 + "s";
    c.style.fontSize = 16 + Math.random() * 16 + "px";
    layer.appendChild(c);
    setTimeout(() => c.remove(), 3800);
  }
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("uk-UA", { day: "numeric", month: "short" });
}

function isOverdue(task) {
  if (!task.deadline || task.status === "done" || task.status === "dropped") return false;
  return task.deadline < new Date().toISOString().slice(0, 10);
}

function firstName(full) {
  return full.split(" ")[0];
}

/* ---------- Рендер ---------- */

function renderHeader() {
  const me = STATE.me;
  const hour = new Date().getHours();
  const hi = hour < 11 ? "Доброго ранку" : hour < 18 ? "Привіт" : "Доброго вечора";
  $("greeting").textContent = `${hi}, ${me.first_name}!`;
  $("dept-badge").textContent = me.dept_title + (me.manager ? " · менеджер" : "");
  const s = me.stats;
  $("stat-chips").innerHTML = `
    <div class="chip fire"><div class="num">${s.active}</div><div class="lbl">в роботі</div></div>
    <div class="chip"><div class="num">${s.review}</div><div class="lbl">на перевірці</div></div>
    <div class="chip win"><div class="num">${s.done_week}</div><div class="lbl">✅ цього тижня</div></div>`;
}

function taskCard(task) {
  const me = STATE.me;
  const overdue = isOverdue(task);
  const metas = [];
  if (task.project) metas.push(`<span class="meta project">${esc(task.project)}</span>`);
  if (task.deadline)
    metas.push(`<span class="meta deadline${overdue ? " overdue" : ""}">⏰ ${fmtDate(task.deadline)}${overdue ? " · прострочено" : ""}</span>`);
  if (me.manager) metas.push(`<span class="meta who">${esc(firstName(task.assignee))}</span>`);
  else if (task.creator !== task.assignee) metas.push(`<span class="meta who">від ${esc(firstName(task.creator))}</span>`);
  if (task.article_url)
    metas.push(`<span class="meta link"><a href="${esc(task.article_url)}" target="_blank">матеріал ↗</a></span>`);

  const actions = [];
  const own = task.assignee === me.name;
  if (task.status === "todo" && own)
    actions.push(`<button class="btn primary" data-act="doing" data-id="${task.id}">Беру в роботу 🏃‍♀️</button>`);
  if (task.status === "doing" && own) {
    actions.push(`<button class="btn primary" data-act="submit" data-id="${task.id}">Здаю на перевірку ✋</button>`);
    actions.push(`<button class="btn ghost" data-act="todo" data-id="${task.id}">У чергу</button>`);
  }
  if (task.status === "review" && own && !me.manager)
    actions.push(`<button class="btn ghost" data-act="doing" data-id="${task.id}">Повернути в роботу</button>`);
  if (task.status === "review" && me.manager) {
    actions.push(`<button class="btn accept" data-act="done" data-id="${task.id}">Прийняти ✅</button>`);
    actions.push(`<button class="btn" data-act="return" data-id="${task.id}">Повернути ↩</button>`);
  }
  if (me.manager && task.status !== "done" && task.status !== "dropped")
    actions.push(`<button class="btn ghost" data-act="edit" data-id="${task.id}">Редагувати</button>`);

  return `<div class="task-card st-${task.status}${overdue ? " overdue" : ""}">
    <div class="task-top">
      <div class="task-title">${esc(task.title)}</div>
      ${PRIO_ICON[task.priority] ? `<div class="prio">${PRIO_ICON[task.priority]}</div>` : ""}
    </div>
    ${metas.length ? `<div class="task-meta">${metas.join("")}</div>` : ""}
    ${task.body ? `<div class="task-body">${esc(task.body)}</div>` : ""}
    ${actions.length ? `<div class="task-actions">${actions.join("")}</div>` : ""}
  </div>`;
}

function section(title, tasks, icon) {
  if (!tasks.length) return "";
  return `<div class="section-title">${icon || ""} ${title}
      <span class="count">${tasks.length}</span></div>
    ${tasks.map(taskCard).join("")}`;
}

function renderBoard() {
  const tasks = STATE.tasks;
  const by = (st) => tasks.filter((t) => t.status === st);
  let html = "";
  if (STATE.me.manager) {
    html += section("Чекають перевірки", by("review"), "👀");
    html += section("У роботі", by("doing"), "🏃‍♀️");
    html += section("Черга", by("todo"), "📋");
    html += section("Закриті недавно", by("done"), "🏆");
    html += section("Зняті", by("dropped"), "🗑");
  } else {
    html += section("На перевірці", by("review"), "👀");
    html += section("У роботі", by("doing"), "🔥");
    html += section("Черга", by("todo"), "📋");
    html += section("Перемоги", by("done"), "🏆");
  }
  if (!html) {
    html = `<div class="empty"><div class="fox-big">🦊💤</div>
      ${STATE.me.manager
        ? "Завдань немає. Натисни ＋, щоб поставити перше."
        : "Завдань немає — Микита теж відпочиває.<br>Гарного дня!"}</div>`;
  }
  $("content").innerHTML = html;
}

function renderPeople() {
  const load = {};
  STATE.tasks.forEach((t) => {
    if (t.status === "done" || t.status === "dropped") return;
    load[t.assignee] = (load[t.assignee] || 0) + 1;
  });
  const rows = STATE.roster
    .filter((p) => !p.manager)
    .map((p) => {
      const n = load[p.name] || 0;
      return `<div class="person-row">
        <div style="flex:1">
          <div class="p-name">${esc(p.name)}</div>
          <div class="p-dept">${esc(p.dept_title)}</div>
        </div>
        <div class="p-load${n ? "" : " free"}">${n ? n + " акт." : "вільна"}</div>
      </div>`;
    });
  $("content").innerHTML = rows.join("");
}

function render() {
  renderHeader();
  if (STATE.me.manager) {
    $("tabs").classList.remove("hidden");
    $("fab").classList.remove("hidden");
  }
  if (STATE.tab === "people") renderPeople();
  else renderBoard();
}

/* ---------- Шторки ---------- */

function openSheet(html) {
  $("sheet").innerHTML = html;
  $("sheet-backdrop").classList.remove("hidden");
}

function closeSheet() {
  $("sheet-backdrop").classList.add("hidden");
}

function sheetCreateOrEdit(task) {
  const isEdit = !!task;
  const people = STATE.roster
    .map((p) => `<option value="${esc(p.name)}"${task && task.assignee === p.name ? " selected" : ""}>
        ${esc(p.name)} — ${esc(p.dept_title)}</option>`)
    .join("");
  openSheet(`
    <h2>${isEdit ? "Редагувати завдання" : "🦊 Нове завдання"}</h2>
    <div class="field"><label>Що зробити</label>
      <input id="f-title" maxlength="200" placeholder="Заголовок завдання" value="${task ? esc(task.title) : ""}"></div>
    <div class="field"><label>Кому</label>
      <select id="f-assignee">${people}</select></div>
    <div class="field"><label>Проєкт (необовʼязково)</label>
      <input id="f-project" maxlength="80" placeholder="Бюджет-2026, Афіша…" value="${task ? esc(task.project) : ""}"></div>
    <div class="field"><label>Дедлайн</label>
      <input id="f-deadline" type="date" value="${task && task.deadline ? task.deadline : ""}"></div>
    <div class="field"><label>Пріоритет</label>
      <div class="seg" id="f-prio">
        <button data-v="0">🌿 не горить</button>
        <button data-v="1">📌 звичайний</button>
        <button data-v="2">🔥 терміново</button>
      </div></div>
    <div class="field"><label>Деталі</label>
      <textarea id="f-body" maxlength="2000" placeholder="Контекст, джерела, очікування…">${task ? esc(task.body) : ""}</textarea></div>
    <div class="sheet-actions">
      ${isEdit ? `<button class="btn danger" id="f-drop">Зняти</button>` : ""}
      <button class="btn ghost" id="f-cancel">Скасувати</button>
      <button class="btn primary" id="f-save">${isEdit ? "Зберегти" : "Поставити"}</button>
    </div>`);

  let prio = task ? task.priority : 1;
  const seg = $("f-prio");
  const paint = () => seg.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("on", +b.dataset.v === prio));
  paint();
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { prio = +b.dataset.v; paint(); }
  });
  $("f-cancel").onclick = closeSheet;
  if (isEdit) {
    $("f-drop").onclick = async () => {
      await patchTask(task.id, { status: "dropped" });
      closeSheet();
      toast("Знято 🗑");
    };
  }
  $("f-save").onclick = async () => {
    const payload = {
      title: $("f-title").value.trim(),
      assignee: $("f-assignee").value,
      project: $("f-project").value.trim(),
      deadline: $("f-deadline").value || null,
      priority: prio,
      body: $("f-body").value.trim(),
    };
    if (!payload.title) { toast("Потрібен заголовок"); return; }
    try {
      if (isEdit) await patchTask(task.id, payload);
      else {
        await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
        await reload();
        haptic("success");
        toast(`Полетіло до ${firstName(payload.assignee)} 🦊`);
      }
      closeSheet();
    } catch (e) { toast(e.message); }
  };
}

function sheetSubmit(task) {
  openSheet(`
    <h2>✋ Здати на перевірку</h2>
    <p style="color:var(--hint);font-size:13.5px;margin-bottom:12px">«${esc(task.title)}»</p>
    <div class="field"><label>Лінк на матеріал (якщо є)</label>
      <input id="f-url" type="url" placeholder="https://nikvesti.com/…" value="${esc(task.article_url)}"></div>
    <div class="sheet-actions">
      <button class="btn ghost" id="f-cancel">Скасувати</button>
      <button class="btn primary" id="f-go">Здаю ✋</button>
    </div>`);
  $("f-cancel").onclick = closeSheet;
  $("f-go").onclick = async () => {
    try {
      await patchTask(task.id, { status: "review", article_url: $("f-url").value.trim() });
      closeSheet();
      haptic("success");
      confetti();
      toast("Пішло на перевірку — тримаю кулаки 🦊");
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Дії ---------- */

async function patchTask(id, fields) {
  await api(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify(fields) });
  await reload();
}

async function handleAction(act, id) {
  const task = STATE.tasks.find((t) => t.id === +id);
  if (!task) return;
  try {
    if (act === "doing") { await patchTask(id, { status: "doing" }); haptic("success"); }
    else if (act === "todo") { await patchTask(id, { status: "todo" }); }
    else if (act === "submit") { sheetSubmit(task); }
    else if (act === "done") {
      await patchTask(id, { status: "done" });
      haptic("success"); confetti();
      toast(`Прийнято! ${firstName(task.assignee)} отримає вітання 🎉`);
    }
    else if (act === "return") { await patchTask(id, { status: "doing" }); toast("Повернуто в роботу ↩"); }
    else if (act === "edit") { sheetCreateOrEdit(task); }
  } catch (e) { toast(e.message); }
}

/* ---------- Завантаження ---------- */

async function reload() {
  const [me, tasks] = await Promise.all([api("/api/me"), api("/api/tasks")]);
  STATE.me = me;
  STATE.tasks = tasks.tasks;
  if (me.manager && !STATE.roster.length) {
    STATE.roster = (await api("/api/roster")).people;
  }
  render();
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
  } catch (e) {
    fail("Не пустили", e.message);
  }
}

/* ---------- Події ---------- */

$("content").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");
  if (btn) handleAction(btn.dataset.act, btn.dataset.id);
});

$("tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  STATE.tab = tab.dataset.tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  render();
});

$("fab").addEventListener("click", () => sheetCreateOrEdit(null));

$("sheet-backdrop").addEventListener("click", (e) => {
  if (e.target === $("sheet-backdrop")) closeSheet();
});

boot();
