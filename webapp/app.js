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
  assignees: [],
  managers: [],
  view: "home",
  projView: "list",
  projQuery: "",
  currentProject: null,
  currentNorm: null,
  kpi: null,
  pending: null,        // черга звірки (тягнеться при вході в «Сповіщення»)
  pendingCount: 0,      // для лічильника на пункті меню
  notifs: null,         // стрічка подій
  unread: 0,
  homeView: "tasks",
  kpiTab: "norms",
  dash: { period: "week", offset: -1, data: null },
  form: null,
};

const TYPE_WORDS = {
  news: { one: "новина", few: "новини", many: "новин" },
  article: { one: "стаття", few: "статті", many: "статей" },
  post: { one: "пост", few: "пости", many: "постів" },
  any: { one: "матеріал", few: "матеріали", many: "матеріалів" },
};

const PLATFORM_PHRASES = { telegram: "у Telegram", instagram: "в Instagram" };

function typePhrase(t, qty) {
  let w = qtyWord(t.type, qty);
  if (t.type === "post" && PLATFORM_PHRASES[t.platform]) w += ` ${PLATFORM_PHRASES[t.platform]}`;
  return w;
}

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

/* «Сьогодні» ЛОКАЛЬНОЮ датою пристрою, не UTC. toISOString() віддає UTC, і
   в Києві (+3) до третьої ночі він показував учорашній день — прострочені
   дедлайни ще три години виглядали не простроченими. */
function todayISO(offsetDays = 0) {
  const d = new Date();
  if (offsetDays) d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function dlDateHtml(d) {
  const today = todayISO();
  const soon = todayISO(7);
  const [y, m, day] = d.due.split("-");
  const cls = d.due < today ? "dl-over" : d.due <= soon ? "dl-soon" : "dl";
  return `<span class="dl-date ${cls}">${day}.${m}.${y.slice(2)}</span>`;
}

function nextDeadline(p) {
  const today = todayISO();
  return (p.deadlines || []).find((d) => d.due >= today) || null;
}

const MONTHS_SHORT = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"];

/* ---------- API ---------- */

/* Помилки розрізняємо: обрив мережі (найчастіше в мобільному Telegram) — це
   err.offline, відмова сервера — err.status. Від цього залежить, чи є сенс
   пропонувати «спробувати ще». */
async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: "tma " + (tg ? tg.initData : ""),
        ...(options.headers || {}),
      },
    });
  } catch (e) {
    const err = new Error("Немає зв'язку з сервером");
    err.offline = true;
    throw err;
  }
  if (!res.ok) {
    const err = new Error((await res.text()) || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
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

/* Підтвердження деструктивної дії. Нативний діалог Telegram (showConfirm є з
   Bot API 6.2), у старих клієнтах і поза Telegram — window.confirm. Undo в нас
   немає, тож знята таска чи видалена норма не повертаються — питаємо. */
function confirmAction(message) {
  return new Promise((resolve) => {
    try {
      if (tg && typeof tg.showConfirm === "function") {
        tg.showConfirm(message, (ok) => resolve(!!ok));
        return;
      }
    } catch (e) {}
    resolve(window.confirm(message));
  });
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 2400);
}

/* Сегментований перемикач. Було дві окремі обведені кнопки: у темній темі
   рамка (--line світло-блакитна) світилась білим на чорному, і пара читалась
   як дві незалежні дії, а не як вибір одного з двох. Тепер одна доріжка з
   «таблеткою» активного пункту. sub — вкладений рівень: підкреслення замість
   таблетки, щоб два перемикачі поспіль не сперечались за увагу. */
function segment(attr, options, active, { sub = false } = {}) {
  return `<div class="seg${sub ? " sub" : ""}" role="tablist">
    ${options.map(([value, label]) => `
      <button role="tab" aria-selected="${value === active}"
        class="${value === active ? "on" : ""}" ${attr}="${esc(value)}">${esc(label)}</button>`).join("")}
  </div>`;
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

/* Ресайзер сайту віддає лише заведені розміри — для аватарок робочі 96x96 і
   255x255 (120/144 повертають порожньо). Кружечкам 42–48 px вистачає 96:
   4.4 КБ проти 23 КБ. На екранах із високою щільністю 96 замало, тож
   рахуємо реальні пікселі й беремо 255. */
function avatarSrc(entry, size) {
  if (!entry) return null;
  const need = size * (window.devicePixelRatio || 1);
  return (need <= 96 && entry.photo_sm) ? entry.photo_sm : entry.photo;
}

function avatar(personName, entry, size) {
  return `<span class="ava" style="width:${size}px;height:${size}px">
    <span class="init" style="font-size:${Math.round(size / 3)}px">${esc(initials(personName))}</span>
    ${entry ? imgHtml(avatarSrc(entry, size), entry.photo_orig) : ""}
  </span>`;
}

/* Скелетон замість «Завантажую…»: тримає висоту майбутнього вмісту, щоб
   екран не стрибав, коли дані приїдуть. */
function skeleton(kind, n) {
  if (kind === "rings")
    return `<div class="sk-grid">${'<span class="sk sk-ring"></span>'.repeat(n)}</div>`;
  if (kind === "bars")
    return `<div class="sk-bars">${Array.from({ length: n }, (_, i) =>
      `<i class="sk" style="height:${28 + ((i * 37) % 62)}%"></i>`).join("")}</div>`;
  return `<div>${'<div class="sk sk-row"></div>'.repeat(n)}</div>`;
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

/* Один рядок таска: «2 новини · IMS · Голоси Миколаєва (Репортажі з сесій)».
   donor — чи згадувати донора: у картці таска потрібен, а в списку
   журналістки він уже стоїть заголовком, і повторювати його ні до чого. */
function taskLine(t, { donor = false } = {}) {
  const { partner, projName } = taskProject(t);
  const parts = [t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1)];
  const withDonor = donor && partner;
  if (withDonor) parts.push(partner);
  if (projName) parts.push(projName);
  else if (!withDonor) parts.push("позапроєктне");
  const line = parts.join(" · ");
  return t.theme_name ? `${line} (${t.theme_name})` : line;
}

/* Фото людини за іменем. Шукаємо і серед журналісток, і серед керівництва:
   у people менеджерів навмисно немає (той список іде на постановку тасків і
   KPI-норми), тож без другої гілки Катя й Олена показувались ініціалами —
   зокрема у «Звітності», де вони і є основні відповідальні. */
function personEntry(name) {
  return STATE.people.find((x) => x.name === name)
    || STATE.managers.find((x) => x.name === name)
    || null;
}

/* Прогрес зарахованого виконання: «2/3». Порожньо там, де показувати нічого:
   на «1 новина» без жодного зарахування «0/1» був би шумом, а не інформацією. */
function progressHtml(t) {
  const done = t.done_count || 0;
  if (!done && (t.qty <= 1 || t.status !== "open")) return "";
  return `<span class="prog${done >= t.qty ? " ok" : ""}">${done}/${t.qty}</span>`;
}

function shortDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}.${m}`;
}

/* Зараховані публікації з лінками (вимога Олега: до «виконано» має бути
   прикріплений конкретний матеріал, а не сама лише цифра).

   editable — режим картки таска: галочка стає кнопкою «зняти саме цю
   публікацію». Без неї лишалось або приймати гуртом, або відкидати гуртом:
   якщо з пʼятнадцяти зарахованих одна не підходить, знімати треба її одну,
   а не все скопом. */
function matchesHtml(t, { editable = false } = {}) {
  const list = t.matches || [];
  if (!list.length) return "";
  return `<div class="mlist">${list.map((m) => `
    <div class="mrow">
      ${editable
        ? `<button class="st-mark done mr-off" data-unmatch="${m.id}"
             aria-label="Зняти з зарахованих">${icon("check")}</button>`
        : `<span class="st-mark done">${icon("check")}</span>`}
      <a class="mr-t" href="${esc(m.url)}" data-ext="${esc(m.url)}">${esc(m.title || m.url)}</a>
      <span class="mr-d">${esc(shortDate(m.published))}</span>
    </div>`).join("")}</div>`;
}

/* Дедлайн таска: "badge" — окремим значком праворуч у списках, інакше —
   дрібним усередині рядка (картка таска). Прострочений — червоним. */
function deadlineHtml(t, style) {
  if (!t.deadline) return "";
  const overdue = t.status === "open" && t.deadline < todayISO();
  const [, m, d] = t.deadline.split("-");
  if (style === "badge")
    return `<span class="dl-date ${overdue ? "dl-over" : "dl-soon"}">до ${d}.${m}</span>`;
  return ` · <span class="${overdue ? "dl-over" : "dl"}">до ${d}.${m}${overdue ? " ⚑" : ""}</span>`;
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



/* Стабільний колір проєкту: слот за порядком id (колір іде за сутністю,
   сортування/фільтри його не міняють) */
function projectColorIdx(id) {
  const ordered = [...STATE.projects].sort((a, b) => a.id - b.id);
  return (ordered.findIndex((p) => p.id === id) % 8) + 1;
}

/* ---------- Навігація ---------- */

/* ---------- Нативна кнопка «Назад» Telegram ----------
   Свої кнопки «‹ Назад» лишаються (вони кажуть, КУДИ повернешся — нативна
   цього не вміє), але апаратна кнопка Android і свайп-назад раніше просто
   закривали апку з будь-якого екрана. Куди вести — беремо не зі стеку історії,
   а зі статичної мапи «екран → батько»: та сама, що на самих кнопках, тож
   нативна і намальована ніколи не розійдуться. Зі стеком розійшлися б —
   nav("project") після збереження тематики клав би туди зайвий запис. */
function backTarget() {
  switch (STATE.view) {
    case "person":
    case "personhist":
    case "people": return ["home"];
    case "form": return ["people"];
    case "project": return ["projects"];
    case "bulk": return ["project", STATE.bulk && STATE.bulk.projectId];
    case "kpi": return ["home"];
    case "kpinorm": return ["kpi"];
    default: return null;      // корінь табів — назад нікуди
  }
}

function sheetOpen() {
  return !$("sheet-backdrop").classList.contains("hidden");
}

/* Кнопку показуємо і коли відкрита шторка — інакше на кореневому екрані
   свайп-назад закрив би апку замість того, щоб закрити шторку. */
function syncBackButton() {
  if (!tg || !tg.BackButton) return;
  try {
    if (sheetOpen() || backTarget()) tg.BackButton.show();
    else tg.BackButton.hide();
  } catch (e) {}
}

function goBack() {
  if (sheetOpen()) { closeSheet(); return; }
  const target = backTarget();
  if (target) nav(target[0], target[1]);
}

function nav(view, arg) {
  STATE.view = view;
  if (view === "project") STATE.currentProject = arg;
  if (view === "kpinorm") STATE.currentNorm = arg;
  if (view === "person" || view === "personhist") STATE.currentPerson = arg;
  if (view === "bulk") {
    const now = new Date();
    STATE.bulk = { ...arg, y: now.getFullYear(), m: now.getMonth(), qty: {} };
  }
  if (view === "kpi") STATE.kpi = null; // свіже зведення при кожному вході (факти кешує сервер)
  // Черга і стрічка могли змінитись у колеги — перечитуємо при кожному вході
  if (view === "alerts") { STATE.pending = null; STATE.notifs = null; }
  if (view === "form") STATE.form = {
    person: arg, project: undefined, type: null, platform: "telegram",
    theme_id: null, qty: 1, note: "", deadline: "",
  };
  // KPI і його підекрани більше не пункт нижнього меню — заходять із
  // Головної, тож і підсвічуємо Головну
  const navKey = view === "kpi" || view === "kpinorm" ? "home"
    : view === "project" || view === "bulk" ? "projects"
    : view === "person" || view === "personhist" ? "home" : view;
  document.querySelectorAll("#bottomnav .bn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === navKey));
  render();
  syncBackButton();
  window.scrollTo(0, 0);
}

function render() {
  const v = STATE.view;
  if (v === "home") renderHome();
  else if (v === "person") renderPerson();
  else if (v === "personhist") renderPersonHistory();
  else if (v === "people") renderPeople();
  else if (v === "form") renderForm();
  else if (v === "projects") renderProjects();
  else if (v === "project") renderProject();
  else if (v === "bulk") renderBulk();
  else if (v === "kpi") renderKpi();
  else if (v === "kpinorm") renderKpiNorm();
  else if (v === "reports") renderReports();
  else if (v === "alerts") renderAlerts();
}

/* ---------- Головна ---------- */

/* Позначка статусу таска: виконаний — зелена галочка (щоб виконане було
   помітно, а не крапкою), знятий — приглушений хрестик, відкритий — крапка. */
function statusMark(t) {
  if (t.status === "done") return `<span class="st-mark done">${icon("check")}</span>`;
  if (t.status === "dropped") return `<span class="st-mark dropped">${icon("x")}</span>`;
  return `<span class="status-dot open"></span>`;
}

/* Донор таска для групування: партнер → назва проєкту → «позапроєктні» */
function donorOf(t) {
  const tp = taskProject(t);
  return tp.partner || tp.projName || "Позапроєктні";
}

/* Головна редактора: перемикач Завдання/Звіт. Завдання — команда кружечками
   зі скільки в кого відкритих завдань і по яких донорах (тап → трекер).
   Звіт — дашборд виконання KPI з кільцями і гортанням по періодах. */
function renderHome() {
  const seg = segment("data-hv",
    [["tasks", "Завдання"], ["report", "Звіт"]],
    STATE.homeView === "report" ? "report" : "tasks");
  const wire = () => $("content").querySelectorAll("[data-hv]").forEach((b) =>
    b.onclick = () => { STATE.homeView = b.dataset.hv === "report" ? "report" : "tasks"; renderHome(); });

  if (STATE.homeView === "report") {
    $("content").innerHTML = `
      <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
      ${seg}
      <div id="dash-body">${skeleton("rings", 8)}</div>`;
    wire();
    renderDashboard();
    return;
  }

  const open = STATE.tasks.filter((t) => t.status === "open");
  const perPerson = {};
  open.forEach((t) => {
    const bucket = (perPerson[t.person] = perPerson[t.person] || {});
    const donor = donorOf(t);
    bucket[donor] = (bucket[donor] || 0) + 1;
  });
  // Без відкритих завдань — не показуємо (шум); їхні трекери — у табі «Команда»
  const people = STATE.people
    .filter((p) => perPerson[p.name])
    .sort((a, b) => {
      const ca = Object.values(perPerson[a.name]).reduce((s, x) => s + x, 0);
      const cb = Object.values(perPerson[b.name]).reduce((s, x) => s + x, 0);
      return cb - ca || a.name.localeCompare(b.name, "uk");
    });
  const rows = people.map((p) => {
    const donors = Object.entries(perPerson[p.name] || {});
    const total = donors.reduce((s, [, c]) => s + c, 0);
    const shown = donors.slice(0, 3).map(([d, c]) => `${d} ×${c}`).join(" · ");
    const more = donors.length > 3 ? ` · +${donors.length - 3}` : "";
    return `
    <button class="team-row" data-tracker="${esc(p.name)}">
      ${avatar(p.name, p, 48)}
      <span class="tr-main">
        <span class="tr-who">${esc(p.name)}</span>
        <span class="tr-what">${total ? esc(shown + more) : "без відкритих завдань"}</span>
      </span>
      <span class="tr-right">
        ${total ? `<span class="pcount">${total}</span>` : ""}
        ${icon("chevron-right", "ic chev")}
      </span>
    </button>`;
  }).join("");
  $("content").innerHTML = `
    <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
    ${seg}
    <div class="sub-row">
      <span class="h-sub">відкритих завдань: ${open.length}</span>
      <button class="text-link" data-nav="kpi">KPI та ролі ${icon("chevron-right", "ic chev")}</button>
    </div>
    ${rows || `<div class="empty-hint">Відкритих завдань ні в кого немає.<br>Натисни «+» або зайди в проєкт, щоб поставити.</div>`}`;
  wire();
}

/* Персональний трекер людини (для редактора): її рекурентні KPI зі шторкою
   правки + її таски, згруповані по донорах.

   Малюємо ОДРАЗУ, а KPI довантажуємо в готовий екран. Раніше весь екран чекав
   на /api/kpi (той іде в MySQL сайту, ~2 с), і тап по людині виглядав як
   зависання — по ньому тикали ще і ще, думаючи, що не спрацювало. Таски вже
   лежать у STATE, тож показати їх можна миттєво. */
function renderPerson() {
  const person = STATE.currentPerson;
  const entry = personEntry(person);
  if (!entry) { nav("home"); return; }
  const kpiReady = !!STATE.kpi;
  const norms = (STATE.kpi ? STATE.kpi.norms : []).filter((n) => n.dept === entry.dept);
  const kpiRows = norms.map((n) => {
    const r = n.rows.find((x) => x.person === person);
    if (!r) return "";
    if (r.excused) return `<button class="mykpi-row" data-kpn="${n.id}">
      <span class="mk-t">${esc(normTitle(n))}</span>
      <span class="kp-excused">звільнено${r.note ? " · " + esc(r.note) : ""}</span></button>`;
    const pct = r.fact === null || r.target <= 0 ? 0 : Math.min(100, Math.round(r.fact / r.target * 100));
    return `<button class="mykpi-row" data-kpn="${n.id}">
      <span class="mk-t">${esc(normTitle(n))}
        <span class="mk-p">· ${esc(n.period === "week" ? STATE.kpi.week_label : STATE.kpi.month_label)}</span></span>
      <span class="kp-fact ${r.done ? "ok" : ""}">${r.fact === null ? "—" : `${r.fact}/${r.target}${r.done ? " ✓" : ""}`}</span>
      <span class="kbar wide"><i class="${r.done ? "ok" : ""}" style="width:${pct}%"></i></span>
    </button>`;
  }).join("");

  const mine = STATE.tasks.filter((t) => t.person === person);
  const openTasks = mine.filter((t) => t.status === "open");
  const closed = mine.filter((t) => t.status !== "open").slice(0, 8);
  const byDonor = {};
  openTasks.forEach((t) => (byDonor[donorOf(t)] = byDonor[donorOf(t)] || []).push(t));
  // Перший рядок — тематика (Олег, 27.07), проєкт — дрібним під нею
  const taskRow = (t) => {
    const qtyPart = t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1);
    // Донор першим, проєкт після нього: «IMS · Голоси Миколаєва». Сама назва
    // проєкту мало що каже — по донору видно, кому цей матеріал у звіт.
    const { partner, projName } = taskProject(t);
    const where = [partner, projName].filter(Boolean).join(" · ");
    return `
    <button class="task-row ${t.status === "done" ? "is-done" : t.status === "dropped" ? "is-dropped" : ""}" data-task="${t.id}">
      <span class="tr-main">
        <span class="tr-who">${esc(qtyPart)}${t.theme_name ? ` · ${esc(t.theme_name)}` : ""}</span>
        ${where ? `<span class="tr-what">${esc(where)}</span>` : ""}
        ${t.note ? `<span class="tr-what">${esc(t.note)}</span>` : ""}
      </span>
      <span class="tr-right">
        ${t.status === "open" ? deadlineHtml(t, "badge") : ""}
        ${progressHtml(t)}
        ${statusMark(t)}
      </span>
    </button>`;
  };
  const donorSections = Object.entries(byDonor).map(([donor, list]) => `
    <div class="dept-title">${esc(donor)} · ${list.length}</div>
    <div class="soft-card">${list.map(taskRow).join("")}</div>`).join("");

  $("content").innerHTML = `
    <button class="back" data-nav="home">${icon("chevron-left")} Команда</button>
    <div class="who">
      ${avatar(person, entry, 56)}
      <div><div class="wn">${esc(person)}</div><div class="wd">${esc(entry.dept_title)}</div></div>
    </div>
    <div id="person-kpi">${kpiReady
      ? (kpiRows ? `<div class="soft-card" style="margin-top:16px"><div class="sc-t">Загальні задачі</div>${kpiRows}</div>` : "")
      : `<div class="soft-card" style="margin-top:16px"><div class="sc-t">Загальні задачі</div>${skeleton("rows", 2)}</div>`}</div>
    <div class="f-label" style="margin-top:20px;font-size:14px;color:var(--ink)">Проєктні задачі</div>
    ${donorSections || `<div class="empty-hint">Відкритих проєктних задач немає.</div>`}
    ${closed.length ? `<div class="dept-title">Закриті недавно · ${closed.length}</div>
      <div class="soft-card">${closed.map(taskRow).join("")}</div>` : ""}`;

  wirePersonKpi(person);
  if (!kpiReady) loadPersonKpi(person);
}

/* KPI людини приїжджають окремо: збій або повільна БД сайту не мають тримати
   екран із її завданнями. */
async function loadPersonKpi(person) {
  try {
    await loadKpi();
  } catch (e) {
    const box = $("person-kpi");
    if (box && STATE.view === "person") box.innerHTML = "";
    return;
  }
  if (STATE.view !== "person" || STATE.currentPerson !== person) return;
  renderPerson();
}

function wirePersonKpi(person) {
  // тап по KPI-рядку — одразу шторка правки цієї людини
  $("content").querySelectorAll("[data-kpn]").forEach((b) => b.onclick = () => {
    const n = normById(+b.dataset.kpn);
    const r = n && n.rows.find((x) => x.person === person);
    if (n && r) overrideSheet(n, r);
  });
}

function taskSheet(t) {
  openSheet(`
    <h2>${esc(t.person)}</h2>
    <p style="color:var(--muted);margin:-8px 0 6px">${esc(taskLine(t, { donor: true }))}${deadlineHtml(t)}</p>
    ${t.note ? `<p style="margin-bottom:6px">${esc(t.note)}</p>` : ""}
    ${t.done_count ? `<div class="sc-t" style="margin:10px 0 2px">Зараховано ${t.done_count} із ${t.qty}</div>
       ${matchesHtml(t, { editable: true })}
       <div class="mr-hint">тап по галочці — зняти публікацію і повернути її в чергу</div>` : ""}
    <p style="color:var(--muted);font-size:12.5px">поставив(ла) ${esc(t.creator.split(" ")[0])} · ${new Date(t.created_at).toLocaleDateString("uk-UA")}</p>
    <button class="link-btn" id="task-attach">${icon("plus")} Зарахувати публікацію за лінком</button>
    <div class="sheet-actions">
      ${t.status === "open"
        ? `<button class="sbtn danger" data-status="dropped">Зняти</button>
           <button class="sbtn" id="task-edit">Редагувати</button>
           <button class="sbtn primary" data-status="done">Виконано</button>`
        : `<button class="sbtn" data-status="open">Повернути у відкриті</button>`}
    </div>`);
  $("task-attach").onclick = () => attachSheet(t);
  const editBtn = $("task-edit");
  if (editBtn) editBtn.onclick = () => taskEditSheet(t);
  // Зняти ОДНУ публікацію із зарахованих: із пʼятнадцяти зарахованих одна
  // може не підходити, і тоді відкидати гуртом — не варіант.
  $("sheet").querySelectorAll("[data-unmatch]").forEach((b) => b.onclick = async () => {
    const m = (t.matches || []).find((x) => x.id === +b.dataset.unmatch);
    if (!m || !(await confirmAction(
        `Зняти з зарахованих і повернути в чергу?\n${m.title}`))) return;
    try {
      // requeue, а не reject: «це не сюди» ≠ «це взагалі не проєктне».
      // Публікація повертається у «Сповіщення» — буде куди її прилаштувати.
      const res = await api(`/api/matches/${m.id}/decide`, {
        method: "POST", body: JSON.stringify({ action: "requeue" }),
      });
      haptic("success");
      (res.tasks || []).forEach(patchTask);
      STATE.pendingCount = res.pending_count;
      syncAlertsBadge();
      render();
      toast("Повернув у чергу — розберемо в «Сповіщеннях»");
      STATE.pending = null;             // черга змінилась, перечитаємо при вході
      const fresh = STATE.tasks.find((x) => x.id === t.id);
      if (fresh) taskSheet(fresh);      // показуємо оновлений «14/15»
      else closeSheet();
    } catch (e) { toast(e.message); }
  });
  $("sheet").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-status]");
    if (!btn) return;
    if (btn.dataset.status === "dropped" &&
        !(await confirmAction(`Зняти завдання з ${t.person.split(" ")[0]}?\n${taskLine(t, { donor: true })}`))) return;
    try {
      const res = await api(`/api/tasks/${t.id}`, { method: "PATCH", body: JSON.stringify({ status: btn.dataset.status }) });
      closeSheet();
      haptic("success");
      patchTask(res.task);
      render();
    } catch (err) { toast(err.message); }
  });
}

/* Зарахувати публікацію руками, за лінком. Закриває два життєві випадки:
   перенести зараховане з помилково задубльованого завдання (та сама
   публікація просто переїде — запис на неї один) і сказати «оце сюди», не
   чекаючи прогону. Приймає і лінк матеріалу, і лінк поста каналу. */
function attachSheet(t) {
  openSheet(`
    <h2>Зарахувати публікацію</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(taskLine(t, { donor: true }))}</p>
    <input id="at-url" type="url" inputmode="url" placeholder="https://nikvesti.com/…">
    <div class="mr-hint">Можна і лінк поста каналу (t.me/nikvesti/…). Якщо
      публікацію вже зараховано деінде — вона переїде сюди, а не подвоїться.</div>
    <div class="sheet-actions">
      <button class="sbtn" id="at-cancel">Скасувати</button>
      <button class="sbtn primary" id="at-save">Зарахувати</button>
    </div>`);
  $("at-cancel").onclick = closeSheet;
  $("at-save").onclick = async () => {
    const url = $("at-url").value.trim();
    if (!url) { toast("Встав лінк публікації"); return; }
    $("at-save").disabled = true;
    try {
      const res = await api(`/api/tasks/${t.id}/attach`, {
        method: "POST", body: JSON.stringify({ url }),
      });
      haptic("success");
      (res.tasks || []).forEach(patchTask);
      STATE.pendingCount = res.pending_count;
      STATE.pending = null;
      syncAlertsBadge();
      render();
      const fresh = STATE.tasks.find((x) => x.id === t.id);
      if (fresh) taskSheet(fresh);
      else closeSheet();
      toast("Зараховано");
    } catch (e) {
      $("at-save").disabled = false;
      toast(e.message);
    }
  };
}

/* Редагування таска: кількість, тематика (з проєкту таска), дедлайн, нотатка */
function taskEditSheet(t) {
  const proj = t.project_id ? STATE.projects.find((p) => p.id === t.project_id) : null;
  const themes = (proj && proj.themes) || [];
  const st = { qty: t.qty, theme_id: t.theme_id };
  openSheet(`
    <h2>Редагувати завдання</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">${esc(t.person)}</p>
    <div class="f-label" style="margin-top:0">Кількість</div>
    <div class="stepper">
      <button id="te-minus">${icon("minus")}</button>
      <b id="te-val">${st.qty}</b>
      <button id="te-plus">${icon("plus")}</button>
    </div>
    ${themes.length ? `
      <div class="f-label">Тематика</div>
      <div class="chips" id="te-themes">${themes.map((th) => `
        <button class="chip ${st.theme_id === th.id ? "on" : ""}" data-th="${th.id}">${esc(th.name)}</button>`).join("")}</div>` : ""}
    <div class="f-label">Дедлайн</div>
    <input id="te-deadline" type="date" value="${t.deadline || ""}">
    <div class="f-label">Нотатка</div>
    <input id="te-note" maxlength="1000" value="${esc(t.note)}">
    <div class="sheet-actions">
      <button class="sbtn" id="te-cancel">Скасувати</button>
      <button class="sbtn primary" id="te-save">Зберегти</button>
    </div>`);
  $("te-minus").onclick = () => { st.qty = Math.max(1, st.qty - 1); $("te-val").textContent = st.qty; };
  $("te-plus").onclick = () => { st.qty = Math.min(99, st.qty + 1); $("te-val").textContent = st.qty; };
  const themesEl = $("te-themes");
  if (themesEl) themesEl.onclick = (e) => {
    const b = e.target.closest("[data-th]");
    if (!b) return;
    const id = +b.dataset.th;
    st.theme_id = st.theme_id === id ? null : id;
    themesEl.querySelectorAll(".chip").forEach((c) =>
      c.classList.toggle("on", +c.dataset.th === st.theme_id));
  };
  $("te-cancel").onclick = closeSheet;
  $("te-save").onclick = async () => {
    try {
      const res = await api(`/api/tasks/${t.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          qty: st.qty,
          theme_id: st.theme_id,
          deadline: $("te-deadline").value || null,
          note: $("te-note").value.trim(),
        }),
      });
      closeSheet();
      haptic("success");
      patchTask(res.task);
      render();
    } catch (e) { toast(e.message); }
  };
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
    <div class="chips">
      <button class="chip ${f.type === null ? "on" : ""}" data-type="">Будь-який</button>
      <button class="chip ${f.type === "news" ? "on" : ""}" data-type="news">Новина</button>
      <button class="chip ${f.type === "article" ? "on" : ""}" data-type="article">Стаття</button>
      <button class="chip ${f.type === "post" ? "on" : ""}" data-type="post">Пост</button>
    </div>

    ${f.type === "post" ? `
      <div class="f-label">Платформа</div>
      <div class="two">
        <button class="bigbtn slim ${f.platform === "telegram" ? "on" : ""}" data-platform="telegram">Telegram</button>
        <button class="bigbtn slim ${f.platform === "instagram" ? "on" : ""}" data-platform="instagram">Instagram</button>
      </div>` : ""}

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
  $("content").querySelectorAll("[data-platform]").forEach((b) => b.onclick = () => { f.platform = b.dataset.platform; renderForm(); });
  $("content").querySelectorAll("[data-theme]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.theme;
    f.theme_id = f.theme_id === id ? null : id;
    renderForm();
  });
  $("f-create").onclick = createTask;
}

function projectPickerSheet() {
  const f = STATE.form;
  let query = "";
  const rowsHtml = () => {
    const list = filteredProjects(query);
    if (!list.length)
      return `<div class="empty-hint" style="padding:24px">Нічого не знайшлось.</div>`;
    return list.map((p) => `
      <button class="pick-row" data-proj="${p.id}">
        ${logoSq(p, 40)}
        <span>
          <span class="pk-name">${esc(p.partner ? p.partner + " · " : "")}${esc(p.name)}</span>
          <span class="pk-meta">${esc(fmtRange(p))}${p.kpi_news || p.kpi_articles ? ` · квота ${p.kpi_news || 0}+${p.kpi_articles || 0}` : ""}</span>
        </span>
      </button>`).join("");
  };
  openSheet(`
    <h2>Проєкт</h2>
    ${STATE.projects.length > 8 ? searchBox("pick-q", "", "Пошук за донором чи назвою") : ""}
    <button class="pick-row plain" data-proj="none">
      <span class="pk-name">Позапроєктне завдання</span>
    </button>
    <div id="pick-list">${rowsHtml()}</div>`);
  const pq = $("pick-q");
  // Знову ж: перемальовуємо лише список, щоб не втратити фокус на кожній літері
  if (pq) pq.oninput = () => { query = pq.value; $("pick-list").innerHTML = rowsHtml(); };
  // Без { once: true }: воно знімало слухач від БУДЬ-ЯКОГО кліку в шторці —
  // тап по заголовку робив пікер мертвим. Слухач тепер помирає разом із
  // вузлом шторки при наступному openSheet (див. openSheet).
  $("sheet").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-proj]");
    if (!btn) return;
    f.project = btn.dataset.proj === "none" ? null : +btn.dataset.proj;
    f.theme_id = null;
    closeSheet();
    renderForm();
  });
}

async function createTask() {
  const f = STATE.form;
  const btn = $("f-create");
  btn.disabled = true;
  try {
    const res = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        person: f.person,
        project_id: f.project || null,
        type: f.type,
        platform: f.type === "post" ? f.platform : null,
        theme_id: f.theme_id,
        qty: f.qty,
        note: f.note.trim(),
        deadline: f.deadline || null,
      }),
    });
    haptic("success");
    toast(`Завдання полетіло до ${f.person.split(" ")[0]}`);
    patchTask(res.task);
    nav("home");
  } catch (e) {
    btn.disabled = false;
    toast(e.message);
  }
}

/* ---------- Проєкти: список і таймлайн ---------- */

/* Пошук по проєктах: списки ростуть (уже ~40), і гортати їх, щоб знайти
   потрібний донор, — найдовша дія в апці. Матчимо і донора, і назву. */
function projectMatches(p, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return `${p.partner || ""} ${p.name || ""}`.toLowerCase().includes(q);
}

function filteredProjects(query) {
  return STATE.projects.filter((p) => projectMatches(p, query));
}

function searchBox(id, value, placeholder) {
  return `<div class="search-box">
    ${icon("search", "ic")}
    <input id="${id}" type="search" inputmode="search" autocomplete="off"
      placeholder="${esc(placeholder)}" value="${esc(value)}">
  </div>`;
}

function renderProjects() {
  const seg = segment("data-pv",
    [["list", "Список"], ["timeline", "Таймлайн"]], STATE.projView);
  // Поле пошуку — лише в списку і лише коли є що шукати
  const withSearch = STATE.projView === "list" && STATE.projects.length > 8;
  if (!withSearch) STATE.projQuery = "";
  $("content").innerHTML = `
    <div class="h-big">Проєкти</div>
    <div class="h-sub">квоти й строки — з CMS сайту, тематики — тут</div>
    ${STATE.projects.length
      ? seg + (withSearch ? searchBox("proj-q", STATE.projQuery, "Пошук за донором чи назвою") : "")
        + `<div id="proj-body"></div>`
      : `<div class="empty-hint">Не бачу проєктів — БД сайту недоступна.</div>`}`;
  $("content").querySelectorAll("[data-pv]").forEach((b) => b.onclick = () => {
    STATE.projView = b.dataset.pv;
    renderProjects();
  });
  // Перемальовуємо ТІЛЬКИ список, поле пошуку лишається тим самим вузлом —
  // інакше на кожній літері губився б фокус і клавіатура закривалась
  const paintBody = () => {
    const box = $("proj-body");
    if (!box) return;
    box.innerHTML = STATE.projView === "timeline" ? timelineHtml() : listHtml();
    if (STATE.projView === "timeline") {
      const sc = $("tl-scroll");
      if (sc && sc.dataset.nowx) sc.scrollLeft = Math.max(0, +sc.dataset.nowx - sc.clientWidth * 0.45);
    } else if (!STATE.projQuery.trim()) {
      // Порядок перетягуванням — тільки на ПОВНОМУ списку: перетягнути щось
      // у відфільтрованому означало б записати в Нору порядок із десятка
      // проєктів замість сорока, тобто знищити решту.
      enableProjectDrag();
    }
  };
  paintBody();
  const q = $("proj-q");
  if (q) q.oninput = () => { STATE.projQuery = q.value; paintBody(); };
}

function listHtml() {
  const list = filteredProjects(STATE.projQuery);
  if (!list.length)
    return `<div class="empty-hint">Нічого не знайшлось за «${esc(STATE.projQuery.trim())}».</div>`;
  const rows = list.map((p) => {
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
    <div class="tl-note">${STATE.projQuery.trim()
      ? `Знайдено ${list.length} із ${STATE.projects.length}. Порядок міняється на повному списку — очисти пошук.`
      : "Зажми проєкт і потягни, щоб змінити порядок."}</div>`;
}

/* Перетягування проєктів: довгий натиск (350 мс без руху) → картка
   «підіймається» і їде за пальцем, сусіди плавно роз'їжджаються (transform,
   без перевставлянь у DOM до моменту відпускання). Порядок — у Нору. */
function enableProjectDrag() {
  const list = $("proj-list");
  if (!list) return;
  const drag = {
    active: false, el: null, timer: null, moved: false,
    startPageY: 0, origIndex: 0, others: [], slot: 0,
  };

  const clearVisuals = () => {
    list.querySelectorAll("[data-drag-id]").forEach((r) => {
      r.classList.remove("drag-src", "drag-anim");
      r.style.transform = "";
    });
  };

  const cleanup = () => {
    clearVisuals();
    clearTimeout(drag.timer);
    drag.active = false;
    drag.el = null;
  };

  list.addEventListener("pointerdown", (e) => {
    const row = e.target.closest("[data-drag-id]");
    if (!row) return;
    drag.el = row;
    drag.startPageY = e.clientY + window.scrollY;
    drag.moved = false;
    drag.timer = setTimeout(() => {
      drag.active = true;
      const rows = [...list.querySelectorAll("[data-drag-id]")];
      drag.origIndex = rows.indexOf(row);
      // Крок зсуву = висота картки + відступ між картками
      const step = rows.length > 1
        ? rows[1].getBoundingClientRect().top - rows[0].getBoundingClientRect().top
        : row.offsetHeight + 10;
      drag.others = rows.filter((r) => r !== row).map((r) => {
        const rect = r.getBoundingClientRect();
        return { el: r, center: rect.top + rect.height / 2 + window.scrollY };
      });
      drag.step = step;
      drag.center = row.getBoundingClientRect().top + row.offsetHeight / 2 + window.scrollY;
      drag.slot = drag.origIndex;
      row.classList.add("drag-src");
      drag.others.forEach((o) => o.el.classList.add("drag-anim"));
      haptic("success");
    }, 350);
  });

  list.addEventListener("pointermove", (e) => {
    if (!drag.el) return;
    const pageY = e.clientY + window.scrollY;
    if (!drag.active) {
      if (Math.abs(pageY - drag.startPageY) > 10) cleanup(); // це скрол, не drag
      return;
    }
    drag.moved = true;
    const dy = pageY - drag.startPageY;
    drag.el.style.transform = `translateY(${dy}px) scale(1.03)`;
    const current = drag.center + dy;
    // Куди «впаде» картка: скільки сусідів лишилось вище за її центр
    let slot = 0;
    drag.others.forEach((o) => { if (o.center < current) slot++; });
    if (slot !== drag.slot) {
      drag.slot = slot;
      haptic("success");
    }
    // Сусіди звільняють місце: хто опинився «по інший бік» — з'їжджає
    drag.others.forEach((o, i) => {
      const origSlot = i < drag.origIndex ? i : i + 1; // позиція сусіда без картки
      let shift = 0;
      if (origSlot < drag.origIndex && origSlot >= slot) shift = drag.step;
      else if (origSlot > drag.origIndex && origSlot <= slot) shift = -drag.step;
      o.el.style.transform = shift ? `translateY(${shift}px)` : "";
    });
    // Автоскрол біля країв екрана
    if (e.clientY < 90) window.scrollBy(0, -10);
    else if (e.clientY > window.innerHeight - 110) window.scrollBy(0, 10);
  });

  // блокуємо скрол сторінки, лише коли drag активний
  list.addEventListener("touchmove", (e) => {
    if (drag.active) e.preventDefault();
  }, { passive: false });

  const finish = async () => {
    if (!drag.el) return;
    const wasDrag = drag.active && drag.moved;
    const el = drag.el;
    const slot = drag.slot;
    const others = drag.others.map((o) => o.el);
    cleanup();
    if (!wasDrag) return;
    el.dataset.justDragged = "1";
    setTimeout(() => delete el.dataset.justDragged, 300);
    // Фінальний порядок: сусіди як були, картка — у свій слот
    const ordered = [...others];
    ordered.splice(slot, 0, el);
    ordered.forEach((r) => list.appendChild(r));
    const ids = ordered.map((r) => +r.dataset.dragId);
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
    <div class="f-label">Тематики — тап, щоб поставити таски</div>
    ${p.themes.map((t) => `
      <div class="theme-row tappable" data-bulk-theme="${t.id}">
        <span class="tn">${esc(t.name)}
          ${formatTitle(t.format) ? `<span class="fmt-badge">${esc(formatTitle(t.format))}</span>` : ""}</span>
        <span class="tc">${t.planned || ""}</span>
        <button class="tact" data-edit-theme="${t.id}" aria-label="Редагувати">${icon("edit")}</button>
      </div>`).join("")}
    ${!p.themes.length ? `<button class="add-theme" id="bulk-nothing">${icon("users")} Поставити проєктні таски</button>` : ""}
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
    b.onclick = (e) => {
      e.stopPropagation(); // олівець — редагування, а не масова постановка
      themeSheet(p, p.themes.find((t) => t.id === +b.dataset.editTheme));
    });
  $("content").querySelectorAll("[data-bulk-theme]").forEach((row) =>
    row.onclick = () => nav("bulk", { projectId: p.id, themeId: +row.dataset.bulkTheme }));
  const bulkNothing = $("bulk-nothing");
  if (bulkNothing) bulkNothing.onclick = () => nav("bulk", { projectId: p.id, themeId: null });
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
      const res = await api(`/api/projects/${p.id}/drive`, { method: "PUT", body: JSON.stringify({ url }) });
      closeSheet();
      haptic("success");
      patchDrive(p.id, res.url);
      nav("project", p.id);
    } catch (e) { toast(e.message); }
  };
  if (p.drive_url) $("dr-remove").onclick = async () => {
    if (!(await confirmAction("Відкріпити папку Google Drive від проєкту?"))) return;
    save("");
  };
  $("dr-save").onclick = () => {
    const url = $("dr-url").value.trim();
    if (!url) { toast("Встав лінк на папку"); return; }
    save(url);
  };
}

/* ---------- Масова постановка тасків з проєкту ----------
   Тап по тематиці → місяць + люди зі степерами → сейв: кожній таска
   (тип із формату тематики, дедлайн — кінець місяця) і пінг від Лиса. */

function renderBulk() {
  const b = STATE.bulk;
  const p = STATE.projects.find((x) => x.id === b.projectId);
  if (!p) { nav("projects"); return; }
  const theme = b.themeId ? (p.themes || []).find((t) => t.id === b.themeId) : null;
  const monthLabel = new Date(b.y, b.m, 1)
    .toLocaleDateString("uk-UA", { month: "long", year: "numeric" }).replace(" р.", "");
  const total = Object.values(b.qty).filter((q) => q > 0).length;
  const typeHint = theme && formatTitle(theme.format) ? ` · ${formatTitle(theme.format).toLowerCase()}` : "";

  const personRow = (pp) => {
    const q = b.qty[pp.name] || 0;
    return `
    <div class="bulk-row ${q ? "picked" : ""}">
      ${avatar(pp.name, pp, 44)}
      <span class="bk-name">${esc(pp.name)}</span>
      <span class="stepper slim">
        <button data-bq="${esc(pp.name)}" data-d="-1">${icon("minus")}</button>
        <b>${q || "·"}</b>
        <button data-bq="${esc(pp.name)}" data-d="1">${icon("plus")}</button>
      </span>
    </div>`;
  };
  const byDept = {};
  STATE.people.forEach((pp) => (byDept[pp.dept_title] = byDept[pp.dept_title] || []).push(pp));

  $("content").innerHTML = `
    <button class="back" data-nav-project="${p.id}">${icon("chevron-left")} ${esc(p.partner || p.name)}</button>
    <div class="h-big">${esc(theme ? theme.name : "Проєктні таски")}</div>
    <div class="h-sub">${esc(p.partner ? p.partner + " · " : "")}${esc(p.name)}${esc(typeHint)}</div>

    <div class="month-nav">
      <button class="arr" id="bulk-prev">${icon("chevron-left")}</button>
      <b>${esc(monthLabel)}</b>
      <button class="arr" id="bulk-next">${icon("chevron-right")}</button>
    </div>

    ${Object.entries(byDept).map(([dept, list]) => `
      <div class="dept-title">${esc(dept)}</div>
      ${list.map(personRow).join("")}`).join("")}

    <button class="cta" id="bulk-save" ${total ? "" : "disabled"}>
      Поставити завдання${total ? ` · ${total} людям` : ""}</button>
    <div class="tl-note">Кожна отримає пінг від Лиса; дедлайн — кінець місяця.</div>`;

  $("bulk-prev").onclick = () => { b.m--; if (b.m < 0) { b.m = 11; b.y--; } renderBulk(); };
  $("bulk-next").onclick = () => { b.m++; if (b.m > 11) { b.m = 0; b.y++; } renderBulk(); };
  $("content").querySelectorAll("[data-bq]").forEach((btn) => btn.onclick = () => {
    const name = btn.dataset.bq;
    b.qty[name] = Math.max(0, Math.min(99, (b.qty[name] || 0) + +btn.dataset.d));
    renderBulk();
  });
  $("bulk-save").onclick = async () => {
    const items = Object.entries(b.qty)
      .filter(([, q]) => q > 0)
      .map(([person, qty]) => ({ person, qty }));
    if (!items.length) return;
    $("bulk-save").disabled = true;
    try {
      const month = `${b.y}-${String(b.m + 1).padStart(2, "0")}`;
      const res = await api("/api/tasks/bulk", {
        method: "POST",
        body: JSON.stringify({ project_id: p.id, theme_id: b.themeId, month, items }),
      });
      haptic("success");
      toast(`Полетіло ${res.created} людям`);
      (res.tasks || []).forEach(patchTask);
      nav("project", p.id);
    } catch (e) {
      $("bulk-save").disabled = false;
      toast(e.message);
    }
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
    if (!(await confirmAction(`Видалити дедлайн «${dlLabel(dl)}»?`))) return;
    try {
      await api(`/api/project_deadlines/${dl.id}`, { method: "DELETE" });
      closeSheet();
      dropDeadline(project.id, dl.id);
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
      const res = dl
        ? await api(`/api/project_deadlines/${dl.id}`, { method: "PATCH", body: JSON.stringify(body) })
        : await api("/api/project_deadlines", { method: "POST", body: JSON.stringify(body) });
      closeSheet();
      haptic("success");
      patchDeadline(project.id, res.deadline);
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
    if (!(await confirmAction(
      `Видалити тематику «${theme.name}»?\nВже поставлені за нею завдання лишаться.`))) return;
    try {
      await api(`/api/themes/${theme.id}`, { method: "DELETE" });
      closeSheet();
      dropTheme(project.id, theme.id);
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
  $("t-save").onclick = async () => {
    const name = $("t-name").value.trim();
    if (!name) { toast("Потрібна назва"); return; }
    const planned = $("t-planned").value ? +$("t-planned").value : null;
    try {
      const res = theme
        ? await api(`/api/themes/${theme.id}`, { method: "PATCH", body: JSON.stringify({ name, planned, format: fmt }) })
        : await api("/api/themes", { method: "POST", body: JSON.stringify({ project_id: project.id, name, planned, format: fmt }) });
      closeSheet();
      haptic("success");
      patchTheme(project.id, res.theme);
      nav("project", project.id);
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Звітність ----------
   Один екран на питання «що і кому здавати найближчим часом»: донор, строк
   проєкту, дати звітів і хто їх пише. Дані вже є в bootstrap (проєкти з CMS
   + дедлайни з Нори), окремого запиту не треба. */

/* Найближчий за датою дедлайн проєкту — за ним сортуємо картки, щоб зверху
   було те, що горить. Проєкти без дедлайнів ідуть окремим блоком у кінці. */
function soonestDue(p) {
  const today = todayISO();
  const list = (p.deadlines || []);
  const next = list.find((d) => d.due >= today);
  return next ? next.due : (list.length ? list[list.length - 1].due : null);
}

const DL_STATUS_LABEL = { submitted: "подано", accepted: "прийнято" };

function assigneeRow(d) {
  const entry = personEntry(d.assignee);   // фото є лише в журналісток
  const st = DL_STATUS_LABEL[d.status];
  return `
    <button class="rep-row ${d.status === "accepted" ? "done" : ""}" data-dl-assign="${d.id}">
      ${avatar(d.assignee || "?", entry, 30)}
      <span class="rr-txt">
        <span class="rr-kind">${esc(dlLabel(d))}</span>
        <span class="rr-who">${d.assignee ? esc(d.assignee) : "не призначено"}</span>
      </span>
      <span class="rr-right">
        ${d.status === "accepted" ? "" : dlDateHtml(d)}
        ${st ? `<span class="rr-st ${d.status}">${st}</span>` : ""}
      </span>
      ${icon("chevron-right", "ic chev")}
    </button>`;
}

/* Сповіщення: перший і головний тип — черга звірки. Лис бачить, що людина
   опублікувала матеріал у проєкті, але не впевнений, яку саме тематику той
   закриває, — і питає Катю: «це тендери чи історія з інфозапиту?».
   Порожня черга — спокійний стан, а не помилка. */
const CONF_LABEL = { high: "впевнено", medium: "схоже", low: "слабкий звʼязок" };

/* Картка спірної публікації читається як новина, а не як рядок бази:
   зверху автор (його фото — обличчя картки), далі заголовок, дата, опис із
   маленьким зображенням праворуч, потім рядок проєкту з лого донора і
   підказка судді. Обґрунтування словами навмисно немає — заголовок і опис
   кажуть про зміст краще, а кожен його токен ми оплачуємо. */
function matchCard(m) {
  const proj = m.project_id ? projectById(m.project_id) : null;
  const donor = proj ? (proj.partner || proj.name) : "";
  const suggested = (m.options || []).find((o) => o.id === m.task_id);
  const tg = m.source === "telegram";
  const who = m.person || (tg ? "Пост каналу" : "Автор невідомий");
  const when = m.published || m.created_at;
  return `
  <div class="al-card" data-match="${m.id}">
    <div class="al-head">
      ${m.person ? avatar(m.person, personEntry(m.person), 38)
                 : `<span class="ava" style="width:38px;height:38px">${icon(tg ? "send" : "users")}</span>`}
      <span class="al-h-txt"><span class="al-who">${esc(who)}</span></span>
      ${m.confidence && suggested
        ? `<span class="al-conf ${esc(m.confidence)}">${esc(CONF_LABEL[m.confidence] || m.confidence)}</span>`
        : ""}
    </div>
    <a class="al-title" href="${esc(m.url)}" data-ext="${esc(m.url)}">${esc(m.title || m.url)}</a>
    <div class="al-date">${esc(longDate(when))}</div>
    <div class="al-body">
      <div class="al-desc">${esc(m.description || "")}</div>
      ${m.image ? `<span class="al-thumb">${imgHtml(m.image)}</span>` : ""}
    </div>
    <div class="al-proj-row">
      ${proj ? logoSq(proj, 26) : ""}
      <span>додано до проєкту <b>${esc(donor)}</b>${proj && proj.name && proj.name !== donor
        ? ` · ${esc(proj.name)}` : ""}</span>
    </div>
    ${suggested
      ? `<div class="al-guess">Схоже тематика: «${esc(suggested.theme_name || "без тематики")}» —
           ${suggested.done_count}/${suggested.qty}</div>`
      : `<div class="al-guess muted">${tg
           ? "Проєкт видно з дисклеймера — обери, кому і в яку тематику"
           : "Тематику суддя не обрав — обери вручну"}</div>`}
    <div class="al-actions">
      <button class="sbtn danger" data-mreject="${m.id}">Не те</button>
      <button class="sbtn primary" data-mconfirm="${m.id}">Зарахувати</button>
    </div>
  </div>`;
}

/* «25 липня 2026» — дата під заголовком картки */
const MONTHS_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня",
  "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"];

function longDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${+d} ${MONTHS_GEN[+m - 1] || ""} ${y}`;
}

/* Стрічка подій — другий шар «Сповіщень» (черга просить дії, стрічка просто
   розповідає, що сталось). Прочитане не зникає, а приглушується. */
const NOTIF_ICON = {
  task_assigned: "plus", task_done: "check", report_deadline: "calendar",
};

function notifRow(n) {
  const when = n.created_at ? shortDate(n.created_at) : "";
  const inner = `
    <span class="st-mark ${n.kind === "task_done" ? "done" : "dropped"}">
      ${icon(NOTIF_ICON[n.kind] || "bell")}</span>
    <span class="tr-main">
      <span class="tr-who">${esc(n.title)}</span>
      ${n.body ? `<span class="tr-what">${esc(n.body)}</span>` : ""}
    </span>
    <span class="tr-right"><span class="mr-d">${esc(when)}</span></span>`;
  const cls = `task-row nt-row${n.unread ? " unread" : ""}`;
  return n.url
    ? `<a class="${cls}" href="${esc(n.url)}" data-ext="${esc(n.url)}">${inner}</a>`
    : `<div class="${cls}">${inner}</div>`;
}

async function renderAlerts() {
  const already = STATE.pending && STATE.notifs;
  $("content").innerHTML = `
    <div class="head-row">
      <div class="h-big">Сповіщення</div>
      <button class="icon-btn" id="queue-add" aria-label="Додати публікацію в чергу">
        ${icon("link")}${icon("plus", "ic sm")}</button>
    </div>
    <div class="h-sub">що просить підтвердити і що вже сталось</div>
    <div id="alerts-body">${already ? "" : skeleton("rows", 3)}</div>`;
  $("queue-add").onclick = queueAddSheet;
  if (!already) {
    try {
      // Черга і стрічка — різні джерела; тягнемо паралельно, бо екран один
      const [q, feed] = await Promise.all([
        api("/api/matches/pending"), api("/api/notifications"),
      ]);
      STATE.pending = q.pending || [];
      STATE.notifs = feed.items || [];
      STATE.unread = feed.unread || 0;
    } catch (e) {
      $("alerts-body").innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
      return;
    }
    if (STATE.view !== "alerts") return;
  }
  paintAlerts();
  markNotifsRead();
}

/* Додати публікацію в чергу руками. Прогін бачить не все: автор у CMS може
   бути не той, матеріал — поза проєктом або старіший за вікно. Тоді редактор
   кидає лінк, і публікація стає в ту саму чергу, що й решта. */
function queueAddSheet() {
  openSheet(`
    <h2>Додати публікацію в чергу</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      Розберемо її тут само — кому і в яку тематику зарахувати.</p>
    <input id="q-url" type="url" inputmode="url" placeholder="https://nikvesti.com/…">
    <div class="mr-hint">Можна і лінк поста каналу (t.me/nikvesti/…).
      Уже зараховане повернеться в чергу, а не подвоїться.</div>
    <div class="sheet-actions">
      <button class="sbtn" id="q-cancel">Скасувати</button>
      <button class="sbtn primary" id="q-save">Додати</button>
    </div>`);
  $("q-cancel").onclick = closeSheet;
  $("q-save").onclick = async () => {
    const url = $("q-url").value.trim();
    if (!url) { toast("Встав лінк публікації"); return; }
    $("q-save").disabled = true;
    try {
      let res = await api("/api/matches/queue", {
        method: "POST", body: JSON.stringify({ url }),
      });
      // Публікацію вже комусь зараховано: мовчки знімати не можна — у людини
      // впав би прогрес без пояснення. Кажемо, де вона, і питаємо.
      if (res.conflict === "counted") {
        const where = [res.partner_name, res.project_name, res.theme_name]
          .filter(Boolean).join(" · ");
        const ok = await confirmAction(
          `Цю публікацію вже зараховано:\n${res.person || "—"}${where ? " · " + where : ""}\n\n` +
          `Зняти звідти і повернути в чергу?`);
        if (!ok) { $("q-save").disabled = false; return; }
        res = await api("/api/matches/queue", {
          method: "POST", body: JSON.stringify({ url, force: true }),
        });
      }
      haptic("success");
      (res.tasks || []).forEach(patchTask);
      STATE.pendingCount = res.pending_count;
      STATE.pending = null;          // перечитаємо чергу з кандидатами
      closeSheet();
      syncAlertsBadge();
      renderAlerts();
      toast(res.tasks && res.tasks.length ? "Зняв і повернув у чергу" : "Додав у чергу");
    } catch (e) {
      $("q-save").disabled = false;
      toast(e.message);
    }
  };
}

/* Побачила — прочитано. Позначаємо ПІСЛЯ малювання, щоб непрочитані ще раз
   підсвітились у цьому заході, і тихо: збій позначки нічого не ламає. */
async function markNotifsRead() {
  if (!STATE.unread) return;
  try {
    const res = await api("/api/notifications/read", {
      method: "POST", body: JSON.stringify({ all: true }),
    });
    STATE.unread = res.unread || 0;
    syncAlertsBadge();
  } catch (e) { /* лічильник просто оновиться наступного разу */ }
}

function paintAlerts() {
  const list = STATE.pending || [];
  const feed = STATE.notifs || [];
  const box = $("alerts-body");
  if (!box) return;
  box.innerHTML = `
    ${list.length
      ? `<div class="dept-title">Звірити виконання · ${list.length}</div>
         ${list.map(matchCard).join("")}`
      : `<div class="empty-hint">Спірних публікацій немає —
           усе, що вийшло, Лис розібрав сам.</div>`}
    ${feed.length
      ? `<div class="dept-title">Що сталось</div>
         <div class="soft-card">${feed.map(notifRow).join("")}</div>`
      : ""}
    <div class="soft-card">
      <div class="sc-t">Летить у чати</div>
      <div class="al-line">Звітні дедлайни грантів — у «Фінанси МикВісті»,
        за тиждень і за 2 доби до дати.</div>
      <div class="al-line">Нове завдання і виконане завдання — авторові
        в приват від Лиса.</div>
    </div>`;
  box.querySelectorAll("[data-mreject]").forEach((b) => b.onclick = async () => {
    const m = (STATE.pending || []).find((x) => x.id === +b.dataset.mreject);
    if (!m) return;
    if (!(await confirmAction(`Не зараховувати «${m.title}»?`))) return;
    decideMatch(m.id, { action: "reject" });
  });
  box.querySelectorAll("[data-mconfirm]").forEach((b) => b.onclick = () => {
    const m = (STATE.pending || []).find((x) => x.id === +b.dataset.mconfirm);
    if (m) matchThemeSheet(m);
  });
}

/* Куди зараховуємо. Два рівні:
   1) відкриті завдання людини в цьому проєкті (з прогресом);
   2) БУДЬ-ЯКА тематика проєкту — навіть та, на яку завдання ще не ставили.
      Без другого пункту черга впиралась у глухий кут: публікація очевидно
      закриває «критичні інформаційні потреби», а вибрати можна було лише те,
      що хтось колись завів. Тепер завдання під таку тематику заводиться на
      льоту (мовчки, без «тобі поставили завдання» за вчорашнє).
   Для поста каналу автор невідомий, тож спершу питаємо, чия це робота. */
function matchThemeSheet(m, forcedPerson) {
  const who = forcedPerson || m.person;
  if (!who) { matchPersonSheet(m); return; }

  const tasks = (m.options || []).filter((o) => o.person === who)
    .sort((a, b) => (b.id === m.task_id) - (a.id === m.task_id));
  const taken = new Set(tasks.map((t) => t.theme_id).filter(Boolean));
  const themes = (m.themes || []).filter((t) => !taken.has(t.id));
  if (!tasks.length && !themes.length) {
    toast("У проєкті немає ні завдань, ні тематик");
    return;
  }
  const taskRow = (o) => `
    <button class="pick-row" data-mtask="${o.id}">
      <span>
        <span class="pk-name">${esc(o.theme_name || "Без тематики")}</span>
        <span class="pk-meta">${o.done_count}/${o.qty}${o.id === m.task_id ? " · пропозиція Лиса" : ""}</span>
      </span>
      ${o.id === m.task_id ? icon("check", "ic chev") : ""}
    </button>`;
  const themeRow = (t) => `
    <button class="pick-row" data-mtheme="${t.id}">
      <span>
        <span class="pk-name">${esc(t.name)}</span>
        <span class="pk-meta">завдання ще немає — заведу і зарахую</span>
      </span>
    </button>`;
  openSheet(`
    <h2>Куди зарахувати?</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(who)} · ${esc(m.title)}</p>
    ${tasks.length ? `<div class="f-label" style="margin-top:0">Відкриті завдання</div>
      ${tasks.map(taskRow).join("")}` : ""}
    ${themes.length ? `<div class="f-label">Інші тематики проєкту</div>
      ${themes.map(themeRow).join("")}` : ""}`);
  $("sheet").querySelectorAll("[data-mtask]").forEach((b) => b.onclick = () => {
    closeSheet();
    decideMatch(m.id, { action: "confirm", task_id: +b.dataset.mtask });
  });
  $("sheet").querySelectorAll("[data-mtheme]").forEach((b) => b.onclick = () => {
    closeSheet();
    decideMatch(m.id, {
      action: "confirm", theme_id: +b.dataset.mtheme, person: who,
    });
  });
}

/* Пост каналу: автора Telegram не показує — питаємо, чия робота. Кандидати —
   ті, у кого в цьому проєкті є завдання, а якщо таких немає, весь ростер. */
function matchPersonSheet(m) {
  const fromTasks = [...new Set((m.options || []).map((o) => o.person))];
  const people = (fromTasks.length ? fromTasks : STATE.people.map((p) => p.name))
    .sort((a, b) => a.localeCompare(b, "uk"));
  openSheet(`
    <h2>Кому зарахувати?</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">${esc(m.title)}</p>
    ${people.map((name) => `
      <button class="pick-row" data-mperson="${esc(name)}">
        ${avatar(name, personEntry(name), 36)}
        <span><span class="pk-name">${esc(name)}</span></span>
      </button>`).join("")}`);
  $("sheet").querySelectorAll("[data-mperson]").forEach((b) => b.onclick = () =>
    matchThemeSheet(m, b.dataset.mperson));
}

async function decideMatch(matchId, body) {
  try {
    const res = await api(`/api/matches/${matchId}/decide`, {
      method: "POST", body: JSON.stringify(body),
    });
    haptic("success");
    STATE.pending = (STATE.pending || []).filter((x) => x.id !== matchId);
    (res.tasks || []).forEach(patchTask);
    // Прогрес у РЕШТІ карток черги теж змінився: у проєкті часто десяток
    // спірних публікацій на одне й те саме завдання, і після зарахування
    // сусідні картки показували стару цифру («14/15») до перезаходу в апку.
    patchPendingOptions(res.tasks || []);
    STATE.pendingCount = res.pending_count;
    syncAlertsBadge();
    paintAlerts();
  } catch (e) { toast(e.message); }
}

/* Свіжий прогрес у картках, яких рішення не стосувалось напряму. Заразом
   прибираємо з вибору завдання, що вже закрились: пропонувати «зарахувати» в
   набране завдання не можна — воно більше не відкрите. */
function patchPendingOptions(tasks) {
  if (!STATE.pending || !tasks.length) return;
  const byId = new Map(tasks.map((t) => [t.id, t]));
  STATE.pending.forEach((m) => {
    if (!m.options) return;
    m.options.forEach((o) => {
      const t = byId.get(o.id);
      if (t) {
        o.done_count = t.done_count || 0;
        o.qty = t.qty;
        o.status = t.status;
      }
    });
    m.options = m.options.filter((o) => o.status !== "done" && o.status !== "dropped");
  });
}

/* Лічильник на пункті меню «Сповіщення»: спірні + непрочитані події */
function syncAlertsBadge() {
  const btn = document.querySelector('#bottomnav [data-view="alerts"]');
  if (!btn) return;
  const n = (STATE.pendingCount || 0) + (STATE.unread || 0);
  let dot = btn.querySelector(".bn-badge");
  if (!n) { if (dot) dot.remove(); return; }
  if (!dot) {
    dot = document.createElement("span");
    dot.className = "bn-badge";
    btn.appendChild(dot);
  }
  // Показуємо саме число: у черзі бувають десятки, і «9+» приховує масштаб —
  // 40 і 10 вимагають різного настрою. Ріжемо лише за сотнею.
  dot.textContent = n > 99 ? "99+" : n;
}

function renderReports() {
  const withDl = STATE.projects.filter((p) => (p.deadlines || []).length);
  withDl.sort((a, b) => {
    const x = soonestDue(a), y = soonestDue(b);
    if (!x) return 1;
    if (!y) return -1;
    return x < y ? -1 : x > y ? 1 : 0;
  });
  const withoutDl = STATE.projects.filter((p) => !(p.deadlines || []).length);

  const cards = withDl.map((p) => `
    <div class="rep-card">
      <div class="rep-head">
        ${logoSq(p, 44)}
        <span class="rh-txt">
          <span class="rh-donor">${esc(p.partner || p.name)}</span>
          ${p.partner ? `<span class="rh-name">${esc(p.name)}</span>` : ""}
        </span>
      </div>
      <div class="rep-end">${p.end_date
        ? `проєкт до ${esc(fmtUnixDate(p.end_date))}`
        : "строк проєкту не вказано в CMS"}</div>
      ${p.deadlines.map(assigneeRow).join("")}
    </div>`).join("");

  $("content").innerHTML = `
    <div class="h-big">Звітність</div>
    <div class="h-sub">строки звітів по грантах і хто їх пише</div>
    ${cards || `<div class="empty-hint">Дедлайнів звітності ще немає.<br>
      Додай їх у картці проєкту на вкладці «Проєкти».</div>`}
    ${withoutDl.length ? `<div class="tl-note">Без заведених дедлайнів:
      ${withoutDl.map((p) => esc(p.partner || p.name)).join(", ")}</div>` : ""}`;

  $("content").querySelectorAll("[data-dl-assign]").forEach((b) => b.onclick = () => {
    for (const p of STATE.projects) {
      const dl = (p.deadlines || []).find((d) => d.id === +b.dataset.dlAssign);
      if (dl) { assigneeSheet(p, dl); return; }
    }
  });
}

/* Хто пише цей звіт. Дефолт (фінанси — Олена, наративка — Катя) не
   зберігається в БД, тож «Повернути за замовчуванням» реально прибирає
   призначення, а не проставляє поточного дефолтного вручну. */
function assigneeSheet(project, dl) {
  const admins = STATE.assignees.filter((a) => a.admin);
  const rest = STATE.assignees.filter((a) => !a.admin);
  const row = (a) => `
    <button class="pick-row" data-who="${esc(a.name)}">
      ${avatar(a.name, personEntry(a.name), 36)}
      <span>
        <span class="pk-name">${esc(a.name)}</span>
        <span class="pk-meta">${esc(a.dept_title)}</span>
      </span>
      ${a.name === dl.assignee ? icon("check", "ic chev") : ""}
    </button>`;
  const mark = (value, label, cls) => `
    <button class="sbtn ${cls}" data-st="${value}">${label}</button>`;
  openSheet(`
    <h2>${esc(dlLabel(dl))}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 4px">
      ${esc(project.partner || project.name)} · до ${esc(dl.due.split("-").reverse().join("."))}</p>
    ${dl.status ? `<p style="font-size:12.5px;margin-bottom:12px;color:var(--good)">
      ${esc(DL_STATUS_LABEL[dl.status])}${dl.status_by ? " · " + esc(dl.status_by.split(" ")[0]) : ""}
      ${dl.status_at ? esc(dl.status_at.split("-").reverse().join(".")) : ""}</p>`
      : `<p style="font-size:12.5px;margin-bottom:12px;color:var(--muted)">ще очікується</p>`}
    <div class="sheet-actions" style="margin-top:10px">
      ${dl.status === "submitted" || dl.status === "accepted"
        ? mark("", "Скасувати позначку", "")
        : mark("submitted", "Звіт подано", "")}
      ${dl.status === "accepted"
        ? mark("submitted", "Лише подано", "")
        : mark("accepted", "Звіт прийнято", "primary")}
    </div>
    <div class="f-label">Хто пише звіт</div>
    ${admins.map(row).join("")}
    ${rest.length ? `<div class="dept-title" style="margin:16px 0 10px">Решта команди</div>` : ""}
    ${rest.map(row).join("")}
    <div class="sheet-actions">
      ${dl.assignee_custom
        ? `<button class="sbtn" id="as-clear">За замовчуванням</button>` : ""}
      <button class="sbtn" id="as-cancel">Скасувати</button>
    </div>`);
  $("as-cancel").onclick = closeSheet;
  const apply = async (body) => {
    try {
      const res = await api(`/api/project_deadlines/${dl.id}/assignee`,
        { method: "PUT", body: JSON.stringify(body) });
      closeSheet();
      haptic("success");
      patchDeadline(project.id, res.deadline);
      renderReports();
    } catch (e) { toast(e.message); }
  };
  const clear = $("as-clear");
  if (clear) clear.onclick = () => apply({ clear: true });
  const applyStatus = async (status) => {
    try {
      const res = await api(`/api/project_deadlines/${dl.id}/status`, {
        method: "PUT",
        body: JSON.stringify(status ? { status } : { clear: true }),
      });
      closeSheet();
      haptic("success");
      patchDeadline(project.id, res.deadline);
      renderReports();
    } catch (e) { toast(e.message); }
  };
  $("sheet").addEventListener("click", (e) => {
    const who = e.target.closest("[data-who]");
    if (who) { apply({ person: who.dataset.who }); return; }
    const st = e.target.closest("[data-st]");
    if (st) applyStatus(st.dataset.st);
  });
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
    $("content").innerHTML = `<div class="h-big">Налаштування KPI</div>
      <div class="h-sub">норма на відділ, облік по людині · звіт — на Головній</div>
      ${skeleton("rows", 4)}`;
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
  // Один екран налаштувань: норми відділів і склад самих відділів. Раніше це
  // були два пункти нижнього меню, хоча по суті це одна рідко вживана
  // конфігурація: змінив норму — тут же перевірив, на кого вона діє.
  const seg = segment("data-kt",
    [["norms", "Норми"], ["team", "Команда"]], STATE.kpiTab);
  const body = STATE.kpiTab === "team" ? teamHtml() : `
    ${sections || `<div class="empty-hint">Норм ще немає.<br>Додай першу — і прогрес рахуватиметься сам із сайту.</div>`}
    <button class="add-theme" id="add-norm">${icon("plus")} Додати норму</button>
    ${!k.site_db ? `<div class="tl-note">БД сайту недоступна — факт тимчасово не рахується.</div>` : ""}`;
  $("content").innerHTML = `
    <div class="h-big">KPI та ролі</div>
    <div class="h-sub">${STATE.kpiTab === "team"
      ? "тап — трекер людини, олівець — перенести між відділами"
      : "норма на відділ, облік по людині · звіт — на Головній"}</div>
    ${seg}
    ${body}`;
  $("content").querySelectorAll("[data-kt]").forEach((b) => b.onclick = () => {
    STATE.kpiTab = b.dataset.kt;
    renderKpi();
  });
  const addNorm = $("add-norm");
  if (addNorm) addNorm.onclick = normCreateSheet;
  $("content").querySelectorAll("[data-norm]").forEach((b) =>
    b.onclick = () => nav("kpinorm", +b.dataset.norm));
  wireTeamRows();
}

/* Звіт відкриваємо на МИНУЛОМУ тижні: у понеділок-четвер поточний ще
   напівпорожній і нічого не каже — усі кружечки червоні просто тому, що
   тиждень не скінчився. Місяць лишаємо поточним: там видно прогрес до цілі.
   Гортання ‹ › працює як раніше, тож «зараз» за один тап. */
function defaultOffset(period) {
  return period === "week" ? -1 : 0;
}

/* Колір за % виконання: червоний (погано) → жовтий → зелений (добре).
   hue 0→120 лінійно від pct; null (немає факту) — сірий трек без кольору. */
function pctColor(pct) {
  if (pct == null) return null;
  const p = Math.max(0, Math.min(100, pct));
  return `hsl(${Math.round(1.2 * p)}, 68%, 45%)`;
}

/* Кільце навколо аватарки: % виконання KPI за період, колір за рівнем */
/* fixedColor — коли смуга має бути одного кольору незалежно від результату.
   Так зроблено на екрані журналістки: зелений завжди. Червоне кільце на
   власному профілі демотивує, а не інформує — вона й так бачить цифри. */
function avatarRing(person, entry, pct, size, fixedColor) {
  const r = 44, c = 2 * Math.PI * r;
  const p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const off = c * (1 - p / 100);
  const color = fixedColor || pctColor(pct);
  return `<span class="avaring" style="width:${size}px;height:${size}px">
    <svg viewBox="0 0 100 100" class="ring">
      <circle class="track" cx="50" cy="50" r="${r}"/>
      <circle class="prog" cx="50" cy="50" r="${r}"
        style="stroke-dasharray:${c.toFixed(1)};stroke-dashoffset:${off.toFixed(1)};stroke:${color || "transparent"}"/>
    </svg>
    <span class="ava-inner">${avatar(person, entry, size - 18)}</span>
  </span>`;
}

/* Звітний дашборд: перемикач тиждень/місяць + гортання період-влево/вправо,
   люди кружечками з кільцем % виконання KPI. Дані — з /api/kpi/dashboard
   (історія рахується з nodes за минулі періоди). */
async function renderDashboard() {
  const d = STATE.dash;
  try {
    d.data = await api(`/api/kpi/dashboard?period=${d.period}&offset=${d.offset}`);
  } catch (e) {
    const body = $("dash-body");
    if (body) body.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "home" || STATE.homeView !== "report") return;
  const data = d.data;
  const byDept = {};
  data.people.forEach((p) => (byDept[p.dept_title] = byDept[p.dept_title] || []).push(p));
  const grid = Object.entries(byDept).map(([dept, list]) => `
    <div class="dept-title">${esc(dept)}</div>
    <div class="ring-grid">
      ${list.map((p) => `
        <button class="ring-cell" data-rperson="${esc(p.person)}">
          ${avatarRing(p.person, personEntry(p.person), p.overall_pct, 104)}
          <span class="rc-name">${esc(p.person.split(" ")[0])}</span>
          <span class="rc-pct" style="color:${pctColor(p.overall_pct) || "var(--muted)"}">${p.overall_pct == null ? "—" : p.overall_pct + "%"}</span>
        </button>`).join("")}
    </div>`).join("");
  const body = $("dash-body");
  if (!body) return;
  body.innerHTML = `
    ${segment("data-dp", [["week", "Тиждень"], ["month", "Місяць"]], d.period, { sub: true })}
    <div class="month-nav">
      <button class="arr" data-doff="-1">${icon("chevron-left")}</button>
      <b>${esc(data.label)}${data.is_current ? " · зараз"
      : d.offset === -1 ? " · минулий" : ""}</b>
      <button class="arr" data-doff="1" ${data.is_current ? "disabled" : ""}>${icon("chevron-right")}</button>
    </div>
    ${grid || `<div class="empty-hint">Норм на цей період немає.</div>`}
    ${!data.site_db ? `<div class="tl-note">БД сайту недоступна — факт не рахується.</div>` : ""}`;
  body.querySelectorAll("[data-dp]").forEach((b) => b.onclick = () => {
    d.period = b.dataset.dp; d.offset = defaultOffset(d.period); renderDashboard();
  });
  body.querySelectorAll("[data-doff]").forEach((b) => b.onclick = () => {
    if (b.disabled) return;
    d.offset = Math.min(0, d.offset + (+b.dataset.doff)); renderDashboard();
  });
  body.querySelectorAll("[data-rperson]").forEach((b) => b.onclick = () =>
    nav("personhist", b.dataset.rperson));
}

/* Профіль співробітника: помісячна динаміка виконання KPI стовпчиками —
   видно, як людина працює місяць до місяця. Дані — /api/kpi/person. */
async function renderPersonHistory() {
  const person = STATE.currentPerson;
  const entry = personEntry(person);
  $("content").innerHTML = `
    <button class="back" data-nav="home">${icon("chevron-left")} Звіт</button>
    <div class="who">
      ${avatar(person, entry, 56)}
      <div><div class="wn">${esc(person)}</div>
      <div class="wd">${esc((entry || {}).dept_title || "")}</div></div>
    </div>
    <div id="hist-body">${skeleton("bars", 12)}</div>`;
  let data;
  try {
    data = await api(`/api/kpi/person?person=${encodeURIComponent(person)}`);
  } catch (e) {
    const b = $("hist-body"); if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "personhist") return;
  const body = $("hist-body");
  if (!body) return;
  if (!data.has_norms) {
    body.innerHTML = `<div class="empty-hint">У відділі немає місячної норми —<br>динаміку показати нема з чого.</div>`;
    return;
  }
  body.innerHTML = historyChartHtml(data);
  scrollHistoryToEnd();
}

/* Стовпчики помісячного виконання KPI. Спільні для профілю людини у Звіті
   (редакторський вид) і для власної динаміки журналістки — щоб дівчата
   бачили ті самі цифри, що бачить редакція, а не інші. */
function historyChartHtml(data, chartId = "hist-chart") {
  const maxH = 120;
  const bars = data.months.map((m) => {
    const pct = m.overall_pct;
    const h = pct == null ? 0 : Math.max(4, Math.min(100, pct) / 100 * maxH);
    const color = pctColor(pct) || "var(--line)";
    const nn = m.norms[0];
    const factLabel = nn ? (nn.fact == null ? "—" : `${nn.fact}/${nn.target}`) : "—";
    return `
    <div class="hb-col ${m.is_current ? "cur" : ""}">
      <div class="hb-val" style="color:${color}">${pct == null ? "—" : pct + "%"}</div>
      <div class="hb-track" style="height:${maxH}px">
        <div class="hb-bar" style="height:${h.toFixed(0)}px;background:${color}"></div>
      </div>
      <div class="hb-fact">${factLabel}</div>
      <div class="hb-month">${esc(m.label)}</div>
    </div>`;
  }).join("");
  const norm = data.months[data.months.length - 1].norms[0];
  return `
    <div class="h-sub">виконання KPI по місяцях${norm ? " · " + esc(norm.label) : ""}</div>
    <div class="hist-chart" id="${chartId}">${bars}</div>
    ${!data.site_db ? `<div class="tl-note">БД сайту недоступна — факт не рахується.</div>` : ""}`;
}

function scrollHistoryToEnd(chartId = "hist-chart") {
  const chart = $(chartId);
  if (chart) chart.scrollLeft = chart.scrollWidth;  // до найсвіжішого місяця
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
      ? `<span class="kp-excused">звільнено</span>`
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
    if (!(await confirmAction(
      `Видалити норму «${normTitle(n)}» для відділу ${n.dept_title}?\n` +
      `Разом з нею зникнуть персональні правки.`))) return;
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
    $("o-hint").textContent = target === 0 ? "0 — звільнено від норми цього періоду" : "";
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
      render();
    } catch (e) { toast(e.message); }
  };
  if (row.overridden) $("o-clear").onclick = () => apply({ clear: true });
  $("o-excuse").onclick = () => apply({ target: 0, note: $("o-note").value.trim() || "звільнено" });
  $("o-save").onclick = () => apply({ target, note: $("o-note").value.trim() });
}

/* ---------- Команда ---------- */

/* Склад відділів — таб усередині «KPI та ролі» (раніше окремий пункт меню) */
function teamHtml() {
  const byDept = {};
  STATE.people.forEach((p) => (byDept[p.dept_title] = byDept[p.dept_title] || []).push(p));
  // Керівництво — окремим блоком і без дій: тасків і KPI-норм у них немає,
  // між відділами їх не носять, тож ні трекера, ні олівця тут не треба
  const heads = STATE.managers.length ? `
    <div class="dept-title">Керівництво · ${STATE.managers.length}</div>
    ${STATE.managers.map((m) => `
      <div class="team-row static">
        ${avatar(m.name, m, 46)}
        <div style="flex:1;text-align:left"><div class="tn">${esc(m.name)}</div>
          <div class="td">${esc(m.role)}</div></div>
      </div>`).join("")}` : "";
  return heads + Object.entries(byDept).map(([dept, list]) => `
    <div class="dept-title">${esc(dept)} · ${list.length}</div>
    ${list.map((p) => `
      <button class="team-row" data-tracker="${esc(p.name)}">
        ${avatar(p.name, p, 46)}
        <div style="flex:1;text-align:left"><div class="tn">${esc(p.name)}</div>
          <div class="td">${esc(p.dept_title)}</div></div>
        <span class="tact" data-move="${esc(p.name)}">${icon("edit")}</span>
      </button>`).join("")}`).join("");
}

function wireTeamRows() {
  $("content").querySelectorAll("[data-move]").forEach((b) =>
    b.onclick = (e) => {
      e.stopPropagation();
      deptSheet(STATE.people.find((p) => p.name === b.dataset.move));
    });
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
      patchDept(p.name, dept, (depts.find(([v]) => v === dept) || [])[1] || "");
      renderKpi();
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Журналістський режим (read-only, інтерфейс — наступний крок) ---------- */

function renderJournalist() {
  const open = STATE.tasks.filter((t) => t.status === "open");
  $("content").innerHTML = `
    <div class="me-head">
      <div class="me-txt">
        <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
        <div class="h-sub">твої завдання і KPI</div>
      </div>
      <div class="me-ring" id="me-ring">${meRingHtml(null, null)}</div>
    </div>
    <div id="my-kpi"></div>
    <div id="my-notifs"></div>
    <div id="my-history"></div>
    ${open.length ? `<div class="soft-card">${open.map((t) => {
      const tp = taskProject(t);
      return `
      <div class="task-row">
        ${tp.logoHtml || ""}
        <span class="tr-main">
          <span class="tr-who">${esc(tp.partner || tp.projName || "Позапроєктне завдання")}</span>
          <span class="tr-what">${esc(taskLine(t))}</span>
          ${t.note ? `<span class="tr-what">${esc(t.note)}</span>` : ""}
          ${matchesHtml(t)}
        </span>
        <span class="tr-right">
          ${deadlineHtml(t, "badge")}
          ${progressHtml(t)}
          <span class="status-dot open"></span>
        </span>
      </div>`;
    }).join("")}</div>`
      : `<div class="empty-hint">Відкритих завдань немає.</div>`}
    <div class="empty-hint" style="padding-top:16px">Це попередній перегляд —
      повний твій інтерфейс уже в розробці.</div>`;
  renderMyKpi();
  renderMyNotifs();
  renderMyHistory();
}

/* Стрічка журналістки: нижнього меню в неї немає, тож сповіщення живуть
   прямо на її екрані — «тобі поставили завдання», «твоя публікація
   зарахована». Вантажиться окремо, як і решта блоків. */
async function renderMyNotifs() {
  let data;
  try { data = await api("/api/notifications"); } catch (e) { return; }
  const box = $("my-notifs");
  const items = (data.items || []).slice(0, 8);
  if (!box || !items.length) return;
  box.innerHTML = `<div class="soft-card">
    <div class="sc-t">Що нового${data.unread ? ` · ${data.unread}` : ""}</div>
    ${items.map(notifRow).join("")}</div>`;
  if (data.unread) {
    try {
      await api("/api/notifications/read", {
        method: "POST", body: JSON.stringify({ all: true }),
      });
    } catch (e) { /* побачить наступного разу */ }
  }
}

/* Власна помісячна динаміка журналістки — ті самі стовпчики, що бачить
   редакція у Звіті. Вантажиться після основного екрана, окремо від «Моїх
   KPI»: обидва блоки йдуть у БД сайту, і якщо один не відповість, другий
   усе одно з'явиться. */
async function renderMyHistory() {
  let data;
  try {
    data = await api("/api/kpi/person");   // сервер сам підставить мене
  } catch (e) { return; }
  const box = $("my-history");
  if (!box || !data.has_norms || !data.months || !data.months.length) return;
  box.innerHTML = `<div class="soft-card">
    <div class="sc-t">Як іде місяць до місяця</div>
    ${historyChartHtml(data, "my-hist-chart")}
  </div>`;
  scrollHistoryToEnd("my-hist-chart");
}

/* Кільце з фото журналістки та підписом, за який період цифри. Смуга завжди
   зелена (див. avatarRing), відсоток — теж: колір тут не носить інформації. */
function meRingHtml(pct, monthLabel) {
  return `
    ${avatarRing(STATE.me.name, STATE.me, pct, 92, "var(--good)")}
    <div class="me-cap">
      ${pct == null ? "" : `<b>${pct}%</b>`}
      <span>${esc(monthLabel ? `за ${monthLabel}` : "цього місяця")}</span>
    </div>`;
}

/* «Мої KPI» журналістки: норми її відділу зі своїм фактом тижня/місяця.
   Вантажиться після основного екрана — щоб таски не чекали на MySQL сайту. */
async function renderMyKpi() {
  let k;
  try { k = await api("/api/kpi"); } catch (e) { return; }
  // Кільце у шапці — за МІСЯЧНИМИ нормами: тижневі стрибають надто різко,
  // щоб бути обличчям екрана (у вівторок там завжди буде мало).
  const monthly = k.norms.filter((n) => n.period === "month");
  const pcts = monthly.map((n) => {
    const r = n.rows[0];
    return r && r.fact !== null && r.target > 0
      ? Math.min(100, Math.round(r.fact / r.target * 100)) : null;
  }).filter((v) => v !== null);
  const ring = $("me-ring");
  if (ring) {
    ring.innerHTML = meRingHtml(
      pcts.length ? Math.round(pcts.reduce((a, b) => a + b, 0) / pcts.length) : null,
      monthly.length ? k.month_label : null,
    );
  }

  const box = $("my-kpi");
  if (!box || !k.norms.length) return;
  box.innerHTML = `<div class="soft-card"><div class="sc-t">Мої KPI</div>
    ${k.norms.map((n) => {
      const r = n.rows[0];
      if (!r) return "";
      if (r.excused) return `<div class="mykpi-row">
        <span class="mk-t">${esc(normTitle(n))}</span>
        <span class="kp-excused">звільнено${r.note ? " · " + esc(r.note) : ""}</span></div>`;
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

/* Відкриття шторки ЗАМІНЮЄ вузол #sheet новим, а не переписує innerHTML.
   Це не косметика: шторки вішають на #sheet свої click-слухачі (taskSheet,
   projectPickerSheet), а зняти їх не було де — closeSheet лише ховає бекдроп.
   Через це кожна колись відкрита шторка спрацьовувала знову: відкрив картку
   таска A, закрив, потім у картці таска B натиснув «Виконано» — і таска A
   теж тихо ставала виконаною (по одному зайвому PATCH на кожне минуле
   відкриття). Заміна вузла вбиває слухачі разом зі старим вузлом, тож
   помилку не можна повторити випадково — новий код вішає слухачі вже на
   свіжий вузол, який помре при наступному openSheet. */
function openSheet(html) {
  const fresh = document.createElement("div");
  fresh.id = "sheet";
  fresh.className = "sheet";
  fresh.innerHTML = html;
  $("sheet").replaceWith(fresh);
  $("sheet-backdrop").classList.remove("hidden");
  syncBackButton();
}
function closeSheet() {
  $("sheet-backdrop").classList.add("hidden");
  syncBackButton();
}

/* ---------- Завантаження ---------- */

async function reload() {
  const data = await api("/api/bootstrap");
  STATE.me = data.me;
  STATE.tasks = data.tasks || [];
  STATE.people = data.people || [];
  STATE.projects = data.projects || [];
  STATE.assignees = data.assignees || [];
  STATE.managers = data.managers || [];
  STATE.pendingCount = data.pending_count || 0;
  STATE.unread = data.unread || 0;
  syncAlertsBadge();
}

/* ---------- Локальні патчі STATE ----------
   Раніше після КОЖНОЇ дії йшов повний /api/bootstrap: створив таску — апка
   перечитувала людей, фото, всі проєкти, тематики, дедлайни й Drive, хоча
   змінився один рядок. Це і затримка перед тим, як дія стане видимою, і
   зайва робота обом БД (проєкти й фото живуть у БД сайту з жорсткими
   лімітами).

   Усі роути мутацій і так повертають змінений об'єкт, тож просто вкладаємо
   його в STATE і малюємо одразу. Свіжість чужих змін (Олег і Катя в апці
   одночасно) забезпечує refreshIfStale при поверненні в апку — див. нижче. */

function patchTask(task) {
  if (!task || !task.id) return;
  const i = STATE.tasks.findIndex((t) => t.id === task.id);
  if (i >= 0) STATE.tasks[i] = task;
  else STATE.tasks.unshift(task);      // новіші — зверху, як у списку з сервера
}

function projectById(id) {
  return STATE.projects.find((p) => p.id === id) || null;
}

function patchTheme(projectId, theme) {
  const p = projectById(projectId);
  if (!p || !theme) return;
  p.themes = p.themes || [];
  const i = p.themes.findIndex((t) => t.id === theme.id);
  if (i >= 0) p.themes[i] = { ...p.themes[i], ...theme };
  else p.themes.push(theme);
  p.themes.sort((a, b) => a.id - b.id);          // порядок як у bootstrap
}

function dropTheme(projectId, themeId) {
  const p = projectById(projectId);
  if (p && p.themes) p.themes = p.themes.filter((t) => t.id !== themeId);
}

function patchDeadline(projectId, dl) {
  const p = projectById(projectId);
  if (!p || !dl) return;
  p.deadlines = p.deadlines || [];
  const i = p.deadlines.findIndex((d) => d.id === dl.id);
  if (i >= 0) p.deadlines[i] = dl;
  else p.deadlines.push(dl);
  // За датою — nextDeadline() бере ПЕРШИЙ невідбулий, тож порядок значущий
  p.deadlines.sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : a.id - b.id));
}

function dropDeadline(projectId, dlId) {
  const p = projectById(projectId);
  if (p && p.deadlines) p.deadlines = p.deadlines.filter((d) => d.id !== dlId);
}

function patchDrive(projectId, url) {
  const p = projectById(projectId);
  if (p) p.drive_url = url || null;
}

function patchDept(personName, dept, deptTitle) {
  const person = STATE.people.find((x) => x.name === personName);
  if (!person) return;
  person.dept = dept;
  person.dept_title = deptTitle;
  // Відділ визначає, які норми діють на людину — зведення KPI протухло
  STATE.kpi = null;
  STATE.dash.data = null;
}

/* Свіжість даних від колеги. Патчі показують ВЛАСНУ дію миттєво, але змін
   іншого менеджера в STATE взятись нізвідки — тому тихо перечитуємо все,
   коли апку повертають з фону (Mini App у Telegram живе короткими сеансами:
   згорнув — розгорнув). Не частіше ніж раз на 30 с і без спінера. */
const REFRESH_MIN_INTERVAL = 30000;
let lastLoadedAt = 0;

async function refreshIfStale() {
  if (!STATE.me || Date.now() - lastLoadedAt < REFRESH_MIN_INTERVAL) return;
  try {
    await reload();
    lastLoadedAt = Date.now();
    if (STATE.me.manager) render();
    else renderJournalist();
  } catch (e) { /* фонове оновлення мовчазне: показуємо, що маємо */ }
}

/* Екран помилки. canRetry — чи є сенс пробувати ще: мережа моргнула або
   сервер віддав 5xx. Відмова доступу (401/403) кнопки не отримує — від
   повторного тику вона не мине. */
function fail(title, text, canRetry) {
  $("screen-loading").classList.add("hidden");
  $("screen-main").classList.add("hidden");
  $("screen-error").classList.remove("hidden");
  $("error-title").textContent = title;
  $("error-text").textContent = text;
  const retry = $("error-retry");
  retry.classList.toggle("hidden", !canRetry);
  retry.onclick = () => {
    retry.disabled = true;
    $("screen-error").classList.add("hidden");
    $("screen-loading").classList.remove("hidden");
    boot().finally(() => { retry.disabled = false; });
  };
}

async function boot() {
  if (!tg || !tg.initData) {
    fail("Тільки з Telegram", "Ця сторінка працює як Telegram Mini App. Відкрий її через @mykvisti_bot → /team.");
    return;
  }
  tg.ready();
  tg.expand();
  // Без цього протягування проєкту пальцем згортає саму апку (Bot API 7.7;
  // у старих клієнтах методу немає — там лишається як було)
  try { tg.disableVerticalSwipes(); } catch (e) {}
  try { tg.BackButton.onClick(goBack); } catch (e) {}
  const syncTheme = () => document.body.classList.toggle("dark", tg.colorScheme === "dark");
  syncTheme();
  try { tg.onEvent("themeChanged", syncTheme); } catch (e) {}
  try {
    await reload();
    lastLoadedAt = Date.now();
    $("screen-error").classList.add("hidden");
    $("screen-loading").classList.add("hidden");
    $("screen-main").classList.remove("hidden");
    if (STATE.me.manager) {
      $("bottomnav").classList.remove("hidden");
      nav("home");
    } else {
      renderJournalist();
    }
    syncBackButton();
  } catch (e) {
    // 401/403 — це «тебе не пустили», решта (обрив мережі, 5xx, сплячий
    // сервіс) минає сама, тож там даємо кнопку замість глухого кута.
    if (e.status === 403) {
      fail("Апка для команди", e.message, false);
    } else if (e.status === 401) {
      fail("Не пустили", e.message, false);
    } else {
      fail(
        e.offline ? "Немає зв'язку" : "Не вдалося завантажити",
        e.offline
          ? "Схоже, пропав інтернет. Перевір зв'язок і спробуй ще раз."
          : e.message,
        true,
      );
    }
  }
}

/* ---------- Події ---------- */

$("content").addEventListener("click", (e) => {
  const navBtn = e.target.closest("[data-nav]");
  if (navBtn) { nav(navBtn.dataset.nav); return; }
  const navProj = e.target.closest("[data-nav-project]");
  if (navProj) { nav("project", +navProj.dataset.navProject); return; }
  const tracker = e.target.closest("[data-tracker]");
  if (tracker) { nav("person", tracker.dataset.tracker); return; }
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

/* Лінк на зараховану публікацію. Слухач документний, а не на #content:
   ті самі рядки живуть і в шторці таска, яку openSheet перестворює. */
document.addEventListener("click", (e) => {
  const ext = e.target.closest("[data-ext]");
  if (!ext) return;
  e.preventDefault();
  try { tg.openLink(ext.dataset.ext); } catch (err) { window.open(ext.dataset.ext, "_blank"); }
});

$("bottomnav").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (btn) nav(btn.dataset.view);
});

$("sheet-backdrop").addEventListener("click", (e) => {
  if (e.target === $("sheet-backdrop")) closeSheet();
});

// Повернулись в апку після згортання — тихо підтягуємо зміни колеги
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshIfStale();
});

boot();
