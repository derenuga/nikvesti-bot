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
  // Історія переходів: «назад» має вести туди, звідки прийшли, а не до
  // статично призначеного «батька» екрана.
  stack: [],
  projView: "list",
  projQuery: "",
  currentProject: null,
  currentNorm: null,
  kpi: null,
  pending: null,        // черга звірки (тягнеться при вході в «Сповіщення»)
  pendingCount: 0,      // для лічильника на пункті меню
  notifs: null,         // стрічка подій
  myFeed: null,         // стрічка журналістки (лічильник на дверях «Події»)
  absenceRequests: null, // запити на відпустку на погодження (менеджерам)
  contactQuery: "",     // пошук у телефонній базі
  previewPerson: null,  // менеджерський перегляд екрана журналістки
  unread: 0,
  homeView: null,        // ставиться нижче: остання обрана таба Головної
  kpiTab: "norms",
  dash: { period: "week", offset: -1, data: null },
  an: { period: "week", offset: -1, data: null, allAuthors: false },
  form: null,
};

/* Яку табу Головної відкривати. Олег живе в «Аналітиці», Катя — у
   «Завданнях», і сперечатись за дефолт їм ні до чого: пам'ятаємо останній
   вибір на пристрої. localStorage у WebView Telegram є, але доступ до нього
   буває заборонений (приватний режим, старі клієнти) — тому під try. */
const HOME_VIEWS = ["analytics", "tasks", "report"];
const HOME_VIEW_KEY = "team.homeView";

function setHomeView(view) {
  STATE.homeView = HOME_VIEWS.includes(view) ? view : "tasks";
  try { localStorage.setItem(HOME_VIEW_KEY, STATE.homeView); } catch (e) {}
}

try {
  const saved = localStorage.getItem(HOME_VIEW_KEY);
  STATE.homeView = HOME_VIEWS.includes(saved) ? saved : "tasks";
} catch (e) {
  STATE.homeView = "tasks";
}

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

/* Один рядок таска: «2 новини · Тендери · IMS».
   Порядок не випадковий (Олег, 28.07): спершу ЩО писати (тематика), потім
   кому це в звіт (донор). Назва проєкту — найменш корисне з трьох, тож іде
   лише тоді, коли ні тематики, ні донора немає. */
function taskLine(t, { donor = false } = {}) {
  const { partner, projName } = taskProject(t);
  const parts = [t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1)];
  if (t.theme_name) parts.push(t.theme_name);
  if (donor && partner) parts.push(partner);
  if (!t.theme_name && !(donor && partner)) parts.push(projName || "позапроєктне");
  return parts.join(" · ");
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

/* Українські форми числа. Наївне «n < 5 ? few : many» ламається на 22, 23,
   24, 32… — саме на цьому спіймався підпис «+22 новини в стрічку». Правило:
   останні дві цифри 11–14 — завжди many, далі дивимось на останню. */
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  if (a >= 11 && a <= 14) return many;
  const b = a % 10;
  return b === 1 ? one : (b >= 2 && b <= 4 ? few : many);
}

/* Пояснення, звідки взявся факт більший за кількість новин: стаття важить
   три новини (Олег, 28.07 — «людям дозволено забивати на новини, коли вони в
   статті»). Без цього підпису цифра виглядала б помилкою. */
function articleHint(row) {
  const n = row && row.articles;
  if (!n) return "";
  const w = row.article_weight || 3;
  const word = plural(n, "стаття", "статті", "статей");
  return `<span class="mk-bonus">+${n} ${word} ×${w}</span>`;
}

/* «+22 новини в стрічку» — робота ПОЗА нормою на власні матеріали.
   Олег, 29.07: у Аліни за липень 22 такі новини не було видно ніде, тобто
   третина роботи не існувала на екрані. Слово «рерайт» в інтерфейс не йде
   принципово — людина бачить, що зробила, а не оцінку жанру.

   Смуги прогресу тут немає навмисно: це не норма. А якщо у відділу колись
   зʼявиться норма і на такі новини, сервер сам перестане слати feed_news
   (див. kpi_payload) — щоб не було і смуги, і цього ж надпису про те саме. */
function feedHint(row) {
  const n = row && row.feed_news;
  if (!n) return "";
  const word = plural(n, "новина", "новини", "новин");
  return `<span class="mk-feed">+${n} ${word} в стрічку</span>`;
}

/* Дзеркало feedHint для Newsroom (Олег, 29.07): їхня норма рахує всі новини
   гуртом, і власний матеріал — найважча частина роботи — у цій купі не видно
   зовсім. Тон тут теплий, а не нейтральний, як у стрічки: власний матеріал
   норму не закриває, але це те, чим редакція пишається. */
function ownHint(row) {
  const n = row && row.own_news;
  if (!n) return "";
  const word = plural(n, "власний матеріал", "власні матеріали", "власних матеріалів");
  return `<span class="mk-own">${n} ${word} 🔥</span>`;
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
  // Спершу — справжня історія: куди людина реально йшла. Статична мапа нижче
  // лишається запасним варіантом, коли історії немає (екран відкрито першим).
  if (STATE.stack.length) return STATE.stack[STATE.stack.length - 1];
  // У перегляді чужими очима підекрани повертають у САМ перегляд, а не на
  // головну менеджера — інакше «Назад» із її публікацій викидало б із
  // перегляду зовсім
  if (inPreview() && STATE.view !== "preview") {
    return ["preview", STATE.previewPerson];
  }
  switch (STATE.view) {
    case "person":
    case "personhist":
    case "people": return ["home"];
    case "form": return ["people"];
    case "project": return ["projects"];
    case "bulk": return ["project", STATE.bulk && STATE.bulk.projectId];
    case "kpi": return ["home"];
    case "kpinorm": return ["kpi"];
    case "impact": return [STATE.impactFrom === "myimpacts" ? "myimpacts"
      : STATE.impactFrom === "home" ? "home" : "impacts"];
    case "mydone":
    case "myfeed":
    case "myhist":
    case "myimpacts":
    case "mypubs":
    case "todo":
    case "away":
    case "impacts":
    case "promises":
    case "contacts": return ["home"];
    case "promise":
    case "dupes": return ["promises"];
    case "preview": return ["personhist", STATE.previewPerson];
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
  if (!target) return;
  if (STATE.stack.length) STATE.stack.pop();   // йдемо НАЗАД, а не вглиб
  nav(target[0], target[1], true);
}

/* Аргумент, з яким відкрито поточний екран. Потрібен, щоб «назад» повертало
   не просто екран, а ТОЙ САМИЙ його стан: список дублів, конкретний проєкт,
   профіль конкретної людини. */
function currentArg() {
  switch (STATE.view) {
    case "project": return STATE.currentProject;
    case "kpinorm": return STATE.currentNorm;
    case "person":
    case "personhist": return STATE.currentPerson;
    case "preview": return STATE.previewPerson;
    case "promise": return STATE.currentPromise;
    case "impact": return STATE.currentImpact;
    case "bulk": return STATE.bulk;
    default: return undefined;
  }
}

/* Пункти нижнього меню — КОРЕНІ. Захід у корінь обнуляє історію: інакше
   стек ріс би нескінченно і «назад» водило б по колу вчорашніх переходів. */
const NAV_ROOTS = new Set(["home", "projects", "people", "reports", "alerts",
                           "mypubs", "todo", "contacts", "myfeed", "away"]);
const STACK_MAX = 12;

function nav(view, arg, back) {
  // Історія переходів. Доти «назад» рахувалось за статичною мапою «екран →
  // батько», тому третій рівень губився ЗАВЖДИ: банк → дублі → картка і назад
  // викидало в банк, а не в дублі. Мапа лишається запасним варіантом — для
  // випадку, коли екран відкрито першим (глибокий лінк, startapp).
  if (!back && STATE.view && STATE.view !== view) {
    if (NAV_ROOTS.has(view)) STATE.stack = [];
    else {
      STATE.stack.push([STATE.view, currentArg()]);
      if (STATE.stack.length > STACK_MAX) STATE.stack.shift();
    }
  }
  STATE.view = view;
  if (view === "project") STATE.currentProject = arg;
  if (view === "kpinorm") STATE.currentNorm = arg;
  if (view === "person" || view === "personhist") STATE.currentPerson = arg;
  if (view === "preview") STATE.previewPerson = arg;
  // Вийшли за межі журналістських підекранів — перегляд закінчився
  else if (!J_SUBVIEWS.has(view)) STATE.previewPerson = null;
  if (view === "bulk") {
    const now = new Date();
    STATE.bulk = { ...arg, y: now.getFullYear(), m: now.getMonth(), qty: {} };
  }
  if (view === "impacts" || view === "myimpacts") STATE.impactFrom = view;
  if (view === "promise") STATE.currentPromise = arg;
  // Черга банку перечитується при кожному вході: колега міг щось зарахувати
  // чи злити дублі, а показувати вчорашній список немає сенсу
  if (view === "promises") { STATE.promises = null; STATE.promiseFacet = STATE.promiseFacet || "all"; }
  if (view === "dupes") STATE.dupes = null;
  if (view === "mypubs") STATE.pubsOffset = 0;   // входимо завжди в поточний місяць
  // Блокнот перечитуємо при кожному вході: запис міг прилетіти з /todo у чаті
  if (view === "todo") STATE.todos = null;
  if (view === "kpi") STATE.kpi = null; // свіже зведення при кожному вході (факти кешує сервер)
  // Черга і стрічка могли змінитись у колеги — перечитуємо при кожному вході
  if (view === "alerts") {
    STATE.pending = null; STATE.notifs = null; STATE.absenceRequests = null;
  }
  if (view === "form") {
    // Тема, принесена з банку, приїжджає в нотатку — далі це звичайне
    // завдання: банк тем не окремий світ, а ще один вхід у постановку.
    const seed = STATE.promiseSeed;
    STATE.form = {
      person: arg, project: undefined, type: null, platform: "telegram",
      theme_id: null, qty: 1, deadline: "",
      note: seed ? `Банк тем: ${seed.title}` : "",
      promise_id: seed ? seed.id : null,
    };
    STATE.promiseSeed = null;
  }
  paintNav();
  // KPI і його підекрани більше не пункт нижнього меню — заходять із
  // Головної, тож і підсвічуємо Головну
  const navKey = view === "preview" ? "home"
    : view === "kpi" || view === "kpinorm" ? "home"
    : view === "project" || view === "bulk" ? "projects"
    : view === "person" || view === "personhist" ? "home"
    : view === "mydone" || view === "myhist" ? "home" : view;
  document.querySelectorAll("#bottomnav .bn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === navKey));
  render();
  syncBackButton();
  window.scrollTo(0, 0);
}

function render() {
  const v = STATE.view;
  // Журналістка ходить тим самим роутером, що менеджер: у неї зʼявились
  // підекрани («Виконані», «Події», «KPI по місяцях»), а без роутера їх не
  // було б куди повертати нативним «Назад».
  if (v === "preview") renderJournalist();
  else if (v === "home" && STATE.me && !STATE.me.manager) renderJournalist();
  else if (v === "mydone") renderMyDone();
  else if (v === "myfeed") renderMyFeed();
  else if (v === "myhist") renderMyHistory();
  else if (v === "mypubs") renderMyPubs();
  else if (v === "todo") renderTodos();
  else if (v === "contacts") renderContacts();
  else if (v === "away") renderAway();
  else if (v === "promises") renderPromises();
  else if (v === "promise") renderPromise();
  else if (v === "dupes") renderDupes();
  else if (v === "impacts") renderImpacts();
  else if (v === "myimpacts") renderMyImpacts();
  else if (v === "impact") renderImpact();
  else if (v === "home") renderHome();
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

/* Шапка Головної менеджера: привітання + кнопка утиліт (Олег, 29.07 —
   «навпроти імені хай буде кнопка з утилітами, там поки що книга, потім ще
   блокнот»). Утиліти живуть НЕ в нижньому меню: там п'ять пунктів робочого
   потоку, і додавати шосте заради довідника — розмивати головне. */
function homeHead() {
  return `
    <div class="head-row">
      <div class="h-big">Привіт, ${esc(STATE.me.first_name)}</div>
      <button class="icon-btn" id="home-tools" aria-label="Утиліти">
        ${icon("grid")}</button>
    </div>`;
}

function toolsSheet() {
  openSheet(`
    <h2>Утиліти</h2>
    <button class="pick-row" data-tool="contacts">
      <span class="door-ic c-blue-ic">${icon("book")}</span>
      <span class="pk-txt">
        <span class="pk-name">Контакти редакції</span>
        <span class="pk-meta">телефони, які вже не треба шукати в чаті</span>
      </span>
    </button>
    <button class="pick-row" data-tool="todo">
      <span class="door-ic c-blue-ic">${icon("check")}</span>
      <span class="pk-txt">
        <span class="pk-name">Блокнот</span>
        <span class="pk-meta">особистий список справ · /todo у чаті</span>
      </span>
    </button>
    <button class="pick-row" data-tool="impacts">
      <span class="door-ic c-blue-ic">${icon("award")}</span>
      <span class="pk-txt">
        <span class="pk-name">Імпакт-архів</span>
        <span class="pk-meta">що змінилось завдяки текстам — для заявок і донорів</span>
      </span>
    </button>
    <button class="pick-row" data-tool="promises">
      <span class="door-ic c-blue-ic">${icon("bell")}</span>
      <span class="pk-txt">
        <span class="pk-name">Банк тем</span>
        <span class="pk-meta">що влада обіцяла і кому пора нагадати</span>
      </span>
    </button>
    <button class="pick-row" data-tool="away">
      <span class="door-ic c-blue-ic">${icon("calendar")}</span>
      <span class="pk-txt">
        <span class="pk-name">Хто коли відсутній</span>
        <span class="pk-meta">відпустки й дедлайни завдань на одній шкалі</span>
      </span>
    </button>`);
  $("sheet").querySelectorAll("[data-tool]").forEach((b) => b.onclick = () => {
    closeSheet();
    nav(b.dataset.tool);
  });
}

/* Головна редактора: перемикач Завдання/Звіт. Завдання — команда кружечками
   зі скільки в кого відкритих завдань і по яких донорах (тап → трекер).
   Звіт — дашборд виконання KPI з кільцями і гортанням по періодах. */
function renderHome() {
  const seg = segment("data-hv",
    [["analytics", "Аналітика"], ["tasks", "Завдання"], ["report", "Звіт"]],
    STATE.homeView);
  const wire = () => {
    $("content").querySelectorAll("[data-hv]").forEach((b) =>
      b.onclick = () => { setHomeView(b.dataset.hv); renderHome(); });
    const tools = $("home-tools");
    if (tools) tools.onclick = toolsSheet;
  };

  if (STATE.homeView === "analytics") {
    $("content").innerHTML = `
      ${homeHead()}
      ${seg}
      <div id="an-body">${skeleton("rows", 4)}</div>`;
    wire();
    renderAnalytics();
    return;
  }

  if (STATE.homeView === "report") {
    $("content").innerHTML = `
      ${homeHead()}
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
    ${homeHead()}
    ${seg}
    <div class="sub-row">
      <span class="h-sub">відкритих завдань: ${open.length}</span>
      <button class="text-link" data-nav="kpi">KPI та ролі ${icon("chevron-right", "ic chev")}</button>
    </div>
    ${rows || `<div class="empty-hint">Відкритих завдань ні в кого немає.<br>Натисни «+» або зайди в проєкт, щоб поставити.</div>`}`;
  wire();
}

/* ---------- Аналітика: дашборд редакції ----------
   Один екран на питання «як ідуть справи»: сайт, пошук Google, вихід
   редакції, соцмережі, гранти й топ матеріалів за тиждень або місяць — усе з
   того, що вже накопичено в норі (див. handlers/team_analytics.py).

   Форми обрані за роботою даних, а не за красою (skill dataviz):
   - читачі періоду — ГЕРОЙСЬКЕ число (одне на екран), а не графік з однією точкою;
   - решта підсумків — стат-плитки з дельтою до попереднього періоду;
   - динаміка по днях — стовпчики одного синього (магнітуда, не ідентичність),
     підписаний лише максимум, значення читаються тапом;
   - частки типів пошуку — одна складена смуга з 2px розривами + легенда з
     цифрами (три сутності = категоріальна палітра, перевірена валідатором
     у світлій і темній темі);
   - автори — горизонтальні бари одного хʼю; топ матеріалів — список.
   Дельта фарбується напрямком (це стат-плитка), решта тексту — чорнилом. */

const AN_PERIOD_WORD = { week: "тиждень", month: "місяць" };

function fmtNum(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("uk-UA");
}

/* Абсолютна зміна: «+120» / «−80». Мінус — саме типографський, не дефіс. */
function fmtSigned(n) {
  if (n == null) return "";
  if (n === 0) return "±0";
  return (n > 0 ? "+" : "−") + fmtNum(Math.abs(n));
}

/* Дельта у %. invert — для метрик, де зростання погане (у нас таких поки
   немає, але «відписки» колись будуть). Нуль показуємо сірим «=»: різниця між
   «не змінилось» і «немає з чим порівняти» важлива. */
function deltaHtml(pct, { invert = false, abs = false } = {}) {
  if (pct == null) return "";
  const flat = pct === 0;
  const up = pct > 0;
  const cls = flat ? "flat" : (invert ? !up : up) ? "up" : "down";
  const arrow = flat ? "=" : up ? "▲" : "▼";
  const value = abs ? fmtNum(Math.abs(pct)) : Math.abs(pct) + "%";
  return `<span class="st-d ${cls}">${arrow} ${flat ? "" : value}</span>`;
}

function anTile(label, value, delta, meta) {
  return `<div class="stat">
    <span class="st-l">${esc(label)}</span>
    <b class="st-v">${typeof value === "number" ? fmtNum(value) : esc(value)}</b>
    <span class="st-b">${delta || ""}${meta ? `<span class="st-m">${esc(meta)}</span>` : ""}</span>
  </div>`;
}

function anHead(iconName, title, sub) {
  return `<div class="an-head">
    ${icon(iconName, "ic an-i")}
    <span class="an-t">${esc(title)}</span>
    ${sub ? `<span class="an-s">${esc(sub)}</span>` : ""}
  </div>`;
}

/* Перемикач періоду + гортання назад. У майбутнє не листаємо (даних там
   немає), тож стрілка «вперед» на поточному періоді вимкнена. */
function anControls(data) {
  const a = STATE.an;
  const label = data ? data.label : "…";
  const tail = !data ? "" : data.is_current ? " · триває" : a.offset === -1 ? " · минулий" : "";
  return `
    ${segment("data-ap", [["week", "Тиждень"], ["month", "Місяць"]], a.period, { sub: true })}
    <div class="month-nav">
      <button class="arr" data-aoff="-1">${icon("chevron-left")}</button>
      <b>${esc(label)}${tail}</b>
      <button class="arr" data-aoff="1" ${data && data.is_current ? "disabled" : ""}>${icon("chevron-right")}</button>
    </div>`;
}

function wireAnControls() {
  const body = $("an-body");
  if (!body) return;
  body.querySelectorAll("[data-ap]").forEach((b) => b.onclick = () => {
    STATE.an.period = b.dataset.ap;
    STATE.an.offset = -1;      // новий масштаб — знову з завершеного періоду
    STATE.an.allAuthors = false;
    renderAnalytics();
  });
  body.querySelectorAll("[data-aoff]").forEach((b) => b.onclick = () => {
    if (b.disabled) return;
    STATE.an.offset = Math.min(0, STATE.an.offset + (+b.dataset.aoff));
    STATE.an.allAuthors = false;   // інший період — інший склад списку
    renderAnalytics();
  });
}

/* Стовпчики читачів по днях. Один хʼю (магнітуда), підписаний лише максимум —
   число на кожному стовпчику ніхто не читає. Тап по стовпчику дає його
   значення: на телефоні тап — це і є hover. */
function anSpark(series) {
  if (!series || !series.length) return "";
  const max = Math.max(...series.map((d) => d.users));
  const top = series.find((d) => d.users === max);
  const sparse = series.length > 14;
  const cols = series.map((d, i) => {
    const h = max ? Math.max(3, Math.round((d.users / max) * 100)) : 3;
    const day = +d.date.slice(8, 10);
    const label = sparse ? (day % 5 === 0 ? day : "") : day;
    return `<button class="sp-col${d.users === max ? " max" : ""}" data-sp="${i}"
      title="${esc(shortDate(d.date))} · ${fmtNum(d.users)}">
      <span class="sp-track"><i style="height:${h}%"></i></span>
      <span class="sp-x">${label}</span>
    </button>`;
  }).join("");
  return `
    <div class="sp-cap">максимум ${fmtNum(max)} · ${esc(shortDate(top.date))}</div>
    <div class="spark" id="an-spark">${cols}</div>
    <div class="sp-note">читачі по днях · тап по стовпчику — цифра дня</div>`;
}

function anSiteHtml(d) {
  const s = d.site;
  if (!s) {
    return anHead("globe", "Сайт") +
      `<div class="empty-hint">Пам'ять аналітики (daily_stats) порожня або недоступна.</div>`;
  }
  const perSession = s.sessions.value && s.pageviews.value
    ? (s.pageviews.value / s.sessions.value).toFixed(1).replace(".", ",")
    : "—";
  return `
    ${anHead("globe", "Сайт", `GA4 · порівняння з ${d.prev_label}`)}
    <div class="hero">
      <span class="hero-l">читачів за ${esc(d.label)}</span>
      <b class="hero-v">${fmtNum(s.users.value)}</b>
      <span class="hero-d">${deltaHtml(s.users.delta)}
        <span class="st-m">${s.users.prev == null ? "" : `було ${fmtNum(s.users.prev)}`}</span></span>
      ${anSpark(s.series)}
    </div>
    <div class="stat-grid">
      ${anTile("Сесії", s.sessions.value, deltaHtml(s.sessions.delta))}
      ${anTile("Перегляди", s.pageviews.value, deltaHtml(s.pageviews.delta))}
      ${anTile("Сторінок на сесію", perSession, "")}
    </div>`;
}

function anSearchHtml(d) {
  const s = d.search;
  if (!s || !s.has_data) {
    return anHead("search", "Пошук Google") +
      `<div class="empty-hint">Розрізу Search Console за цей період немає.<br>
        Залити історію — /sc_backfill.</div>`;
  }
  const total = s.types.reduce((acc, t) => acc + t.clicks, 0);
  const bar = s.types.map((t, i) => t.clicks
    ? `<i class="c${i + 1}" style="width:${total ? (t.clicks / total) * 100 : 0}%"></i>` : "").join("");
  const legend = s.types.map((t, i) => `
    <div class="lg-row">
      <span class="lg-dot c${i + 1}"></span>
      <span class="lg-l">${esc(t.label)}</span>
      <b class="lg-v">${fmtNum(t.clicks)}</b>
      <span class="lg-s">${t.share}%</span>
      ${deltaHtml(t.delta)}
    </div>`).join("");
  return `
    ${anHead("search", "Пошук Google", "кліки з видачі, Discover і Новин")}
    <div class="soft-card">
      <div class="an-two">
        <span><span class="st-l">Кліків</span>
          <b class="st-v big">${fmtNum(s.clicks.value)}</b></span>
        <span class="an-two-r">${deltaHtml(s.clicks.delta)}
          <span class="st-m">показів ${fmtNum(s.impressions.value)}</span></span>
      </div>
      <div class="shr">${bar}</div>
      <div class="legend">${legend}</div>
      <div class="sp-note">Search Console дозріває 2–3 дні — свіжі дні ще підростуть</div>
    </div>`;
}

function anNewsroomHtml(d) {
  const n = d.newsroom;
  if (!n) {
    return anHead("file-text", "Вихід редакції") +
      `<div class="empty-hint">БД сайту недоступна — вихід не порахувати.</div>`;
  }
  // Порядок і довжина барів — за ВАГОЮ власного матеріалу (коефіцієнт із
  // сервера, own_weight), а не за кількістю рядків: 85 рерайтів і 30 власних —
  // різна робота. У цифрі праворуч лишається чесна кількість.
  const maxScore = n.authors.length
    ? Math.max(...n.authors.map((a) => a.score || a.count)) : 0;
  // Сервер віддає ВСІХ; показуємо перших `shown`, решту — по тапу. Смуги
  // рахуються від максимуму всього списку, тож «показати всіх» не перемальовує
  // пропорції — просто дописує хвіст.
  const limit = n.shown || 8;
  const visible = STATE.an.allAuthors ? n.authors : n.authors.slice(0, limit);
  const hidden = n.authors.length - visible.length;
  const rows = visible.map((a) => {
    // Ім'я людини ростера сервер уже підставив (a.person) — по ньому і фото,
    // і трекер; a.name лишається на випадок автора поза ростером
    const who = a.person || a.name;
    const entry = personEntry(who);
    const score = a.score == null ? a.count : a.score;
    const w = maxScore ? Math.max(4, Math.round((score / maxScore) * 100)) : 0;
    const inner = `
      ${avatar(who, entry, 36)}
      <span class="ar-main">
        <span class="ar-n">${esc(who)}</span>
        <span class="kbar"><i style="width:${w}%"></i></span>
      </span>
      <span class="ar-r">
        <b>${fmtNum(a.count)}</b>
        <span class="st-m">${a.own ? `${a.own} власних` : "рерайти"}</span>
      </span>`;
    // Тап по автору з ростера веде в його трекер — аналітика і завдання
    // однієї людини не мають бути двома різними подорожами
    return entry
      ? `<button class="an-row" data-tracker="${esc(who)}">${inner}</button>`
      : `<div class="an-row">${inner}</div>`;
  }).join("");
  return `
    ${anHead("file-text", "Вихід редакції", `опубліковано за ${AN_PERIOD_WORD[d.period]}`)}
    <div class="stat-grid">
      ${anTile("Новин", n.news.value, deltaHtml(n.news.delta))}
      ${anTile("Статей", n.articles.value, deltaHtml(n.articles.delta))}
      ${anTile("Власних", n.own.value, deltaHtml(n.own.delta),
        n.own_share == null ? "" : `${n.own_share}% усього`)}
    </div>
    ${rows ? `<div class="soft-card">
      <div class="sc-t">Хто написав · ${n.authors.length}</div>
      ${rows}
      ${hidden > 0 ? `<button class="more-btn" id="an-more">
        Показати всіх · ще ${hidden}</button>` : ""}
      <div class="sp-note">порядок і смуги — за вагою: власний матеріал
        вважається за ${esc(String(n.own_weight || 1).replace(".", ","))}
        звичайних</div>
      ${(n.zero || []).length ? `<div class="sp-note">Без публікацій за
        ${AN_PERIOD_WORD[d.period]}: ${esc(n.zero.filter((z) => z.linked)
          .map((z) => z.name.split(" ")[0] + " " + (z.name.split(" ")[1] || ""))
          .join(", ") || "нікого")}${
        n.zero.some((z) => !z.linked)
          ? `. Не привʼязані до акаунта сайту (шукати через /kpi_link):
             ${esc(n.zero.filter((z) => !z.linked).map((z) => z.name).join(", "))}`
          : ""}</div>` : ""}
      ${(n.by_type || []).length ? `<div class="sp-note">Інші типи матеріалів у
        CMS за цей період: ${esc(n.by_type.map((t) => `${t.type} ×${t.count}`)
          .join(", "))} — у «новини/статті» вони не входять.</div>` : ""}
    </div>` : ""}`;
}

/* Знак і бренд-відтінок мережі. Іконка тут — ідентичність, а не кодування
   даних: тому їй можна брендовий колір, поки категоріальна палітра лишається
   за частками пошуку. */
const SOC_ICON = {
  facebook: "facebook", instagram: "instagram", telegram: "send",
  tiktok: "tiktok", youtube: "youtube", viber: "viber",
};

/* Метрики, які показуємо в картці: у кожної мережі свій набір, і прочерк на
   тому, чого API не віддає (охоплення TikTok, взаємодії YouTube), — це шум. */
const SOC_METRICS = {
  facebook: ["views", "engagement", "posts"],
  instagram: ["views", "engagement", "posts"],
  telegram: ["views", "posts"],
  tiktok: ["views", "engagement", "posts"],
  youtube: ["views"],
  viber: ["posts"],
};
const SOC_LABEL = { views: "Перегляди", engagement: "Взаємодії", posts: "Постів" };

/* Підпис під картку мережі. Пояснювати «зрізу немає» треба ЛИШЕ коли в картці
   справді нема чого читати: підписники з приростом — це вже зріз, і сказати
   при них «зрізу за цей період ще немає» означає збрехати (Олег, 28.07).

   Метрики періоду й підписники приходять із різних місць: підписники — останнє
   ВІДОМЕ число (може бути з попереднього місяця), а перегляди/пости — рядок
   саме цього періоду. Тому картка «лише з підписниками» — нормальний стан
   місяця, що ще триває: місячний знімок пишеться 1-го числа наступного. */
function socNote(p, metrics, d) {
  if (metrics) return "";
  if (p.live) {
    return `<div class="sp-note">живий знімок t.me — у норі зрізів ще немає,
      перший буде в неділю</div>`;
  }
  if (p.followers != null) {
    // Числа в картці є — не сперечаємось із ними, лише кажемо, чого бракує
    return `<div class="sp-note">${d.is_current
      ? "показники періоду будуть, коли він закінчиться"
      : "показників за цей період у норі немає — лише підписники"}</div>`;
  }
  return `<div class="sp-note">зрізу за цей період ще немає —
    знімок беремо щонеділі</div>`;
}

function anSocialHtml(d) {
  const s = d.social;
  const list = (s && s.platforms) || [];
  if (!s || !list.length) {
    return anHead("heart", "Соцмережі") +
      `<div class="empty-hint">Зрізів соцмереж у норі ще немає.<br>
        Засіяти зараз — <b>/social_capture</b>, залити історію з таблиці —
        <b>/social_import_sheet</b>.</div>`;
  }
  const card = (p) => {
    const metrics = (SOC_METRICS[p.key] || ["views", "posts"])
      .filter((m) => p[m] && p[m].value != null)
      .map((m) => `<span><i>${esc(SOC_LABEL[m])}</i> ${fmtNum(p[m].value)} ${deltaHtml(p[m].delta)}</span>`)
      .join("");
    return `
    <div class="soc-card">
      <div class="soc-h">
        <span class="soc-ic ${esc(p.key)}">${icon(SOC_ICON[p.key] || "heart")}</span>
        <span class="soc-n">${esc(p.title)}</span>
        <span class="soc-f">${fmtNum(p.followers)}
          ${p.followers_delta == null ? "" :
            `<span class="st-d ${p.followers_delta >= 0 ? "up" : "down"}">${fmtSigned(p.followers_delta)}</span>`}</span>
      </div>
      ${metrics ? `<div class="soc-m">${metrics}</div>` : ""}
      ${socNote(p, metrics, d)}
    </div>`;
  };
  const grain = s.grain === "month" ? "місячні зрізи" : "тижневі зрізи";
  return `
    ${anHead("heart", "Соцмережі", `${grain} · підписники — останнє відоме число`)}
    ${list.map(card).join("")}
    ${s.missing && s.missing.length ? `<div class="sp-note">Ще не знімали:
      ${esc(s.missing.join(", "))} — /social_capture засіє, /social_import_sheet
      заллє історію з таблиці.</div>` : ""}`;
}

function anGrantsHtml(d) {
  const g = d.grants;
  if (!g) {
    return anHead("award", "Гранти") +
      `<div class="empty-hint">Нора недоступна — грантові цифри сплять.</div>`;
  }
  const donors = g.donors.map((x) => `${esc(x.name)} ×${x.count}`).join(" · ");
  // Найближчий звітний дедлайн уже лежить у STATE (bootstrap), окремий
  // запит по нього не потрібен
  let soonest = null;
  STATE.projects.forEach((p) => {
    const nd = nextDeadline(p);
    if (nd && (!soonest || nd.due < soonest.dl.due)) soonest = { p, dl: nd };
  });
  return `
    ${anHead("award", "Гранти", "проєкти, завдання і зараховане виконання")}
    <div class="stat-grid">
      ${anTile("Активних проєктів", g.projects_active, "")}
      ${anTile("Відкритих завдань", g.tasks_open, "")}
      ${anTile("Зараховано", g.counted, "", `за ${AN_PERIOD_WORD[d.period]}`)}
    </div>
    <div class="soft-card">
      ${donors ? `<div class="an-line"><b>По донорах:</b> ${donors}</div>` : ""}
      <div class="an-line">Завдань закрито за ${AN_PERIOD_WORD[d.period]}: <b>${g.tasks_done}</b>${
        g.pending ? ` · у черзі звірки <b>${g.pending}</b>` : ""}</div>
      ${soonest ? `<div class="an-line">Найближчий звіт:
        <b>${esc(soonest.p.partner || soonest.p.name)}</b> —
        ${esc(dlLabel(soonest.dl))} ${dlDateHtml(soonest.dl)}</div>` : ""}
    </div>`;
}

function anTopHtml(d) {
  const top = d.top || [];
  if (!top.length) {
    return anHead("trending-up", "Топ матеріалів") +
      `<div class="empty-hint">Денних топів за цей період у норі немає.</div>`;
  }
  const rows = top.map((t, i) => `
    <a class="top-row" href="${esc(t.url)}" data-ext="${esc(t.url)}">
      <span class="tp-n">${i + 1}</span>
      <span class="tp-main">
        <span class="tp-t">${esc(t.title)}</span>
        <span class="tp-m">${esc([t.author, t.days > 1 ? `${t.days} дні в топі` : null]
          .filter(Boolean).join(" · "))}</span>
      </span>
      <span class="tp-v">${fmtNum(t.views)}</span>
    </a>`).join("");
  return `
    ${anHead("trending-up", "Топ матеріалів", `за ${AN_PERIOD_WORD[d.period]}, перегляди GA4`)}
    <div class="soft-card">${rows}</div>`;
}

function anDataHtml(d) {
  if (!d.nora) {
    return `<div class="empty-hint">Нора (Postgres) недоступна —
      аналітика живе саме в ній.</div>`;
  }
  if (!d.site && !d.search && !d.newsroom) {
    return `<div class="empty-hint">За цей період даних ще немає.<br>
      Захват аналітики працює о 09:00 і пише вчорашній день.</div>`;
  }
  return `
    ${anSiteHtml(d)}
    ${anSearchHtml(d)}
    ${anNewsroomHtml(d)}
    ${anSocialHtml(d)}
    ${anGrantsHtml(d)}
    ${anTopHtml(d)}
    <div class="tl-note">Дані до ${esc(shortDate(d.range.data_end))}${
      d.is_current ? ` · період триває, порівнюю перші ${d.days} дн. з тими самими днями ${esc(d.prev_label)}` : ""}.
      Сайт і пошук — з пам'яті нори (захват о 09:00), вихід редакції — з БД сайту,
      соцмережі — тижневі зрізи.</div>`;
}

/* «Показати всіх» — локальне розкриття списку: дані вже приїхали, запит не
   потрібен. Перемальовуємо лише #an-data, щоб не смикати перемикачі періодів. */
function wireAnAuthors(d) {
  const btn = $("an-more");
  if (!btn) return;
  btn.onclick = () => {
    STATE.an.allAuthors = true;
    const box = $("an-data");
    if (!box) return;
    box.innerHTML = anDataHtml(d);
    wireAnSpark(d);
    wireAnAuthors(d);
  };
}

function wireAnSpark(d) {
  const chart = $("an-spark");
  if (!chart || !d.site) return;
  chart.querySelectorAll("[data-sp]").forEach((b) => b.onclick = () => {
    const day = d.site.series[+b.dataset.sp];
    if (day) toast(`${shortDate(day.date)} · ${fmtNum(day.users)} читачів`);
  });
}

async function renderAnalytics() {
  const a = STATE.an;
  const body = $("an-body");
  if (!body) return;
  // Поки їде відповідь — лишаємо підписи попереднього перегляду (якщо це той
  // самий період), щоб перемикач не блимав порожнім заголовком
  const stale = a.data && a.data.period === a.period && a.data.offset === a.offset
    ? a.data : null;
  body.innerHTML = anControls(stale) + `<div id="an-data">${skeleton("rows", 4)}</div>`;
  wireAnControls();
  let data;
  try {
    data = await api(`/api/dashboard?period=${a.period}&offset=${a.offset}`);
  } catch (e) {
    const box = $("an-data");
    if (box) box.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "home" || STATE.homeView !== "analytics") return;
  a.data = data;
  const fresh = $("an-body");
  if (!fresh) return;
  fresh.innerHTML = anControls(data) + `<div id="an-data">${anDataHtml(data)}</div>`;
  wireAnControls();
  wireAnSpark(data);
  wireAnAuthors(data);
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
      <span class="mk-t">${esc(normTitle(n, r))}</span>
      <span class="kp-excused">звільнено${r.note ? " · " + esc(r.note) : ""}</span></button>`;
    const pct = r.fact === null || r.target <= 0 ? 0 : Math.min(100, Math.round(r.fact / r.target * 100));
    return `<button class="mykpi-row" data-kpn="${n.id}">
      <span class="mk-t">${esc(normTitle(n, r))}
        <span class="mk-p">· ${esc(n.period === "week" ? STATE.kpi.week_label : STATE.kpi.month_label)}</span>
        ${normWhy(n, r)}${articleHint(r)}${feedHint(r)}${ownHint(r)}</span>
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
    // Другим рядком — ДОНОР, і тільки він: по донору видно, кому цей матеріал
    // у звіт, а назва проєкту місця не варта (Олег, 28.07).
    const { partner, projName } = taskProject(t);
    const where = partner || projName || "";
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
    <button class="link-btn" id="person-away">${icon("calendar")} Відпустка / лікарняна / відрядження</button>
    <button class="link-btn" id="person-rate">${icon("target")} Ставка · ${personRate(person)}%</button>
    <div class="f-label" style="margin-top:20px;font-size:14px;color:var(--ink)">Проєктні задачі</div>
    ${donorSections || `<div class="empty-hint">Відкритих проєктних задач немає.</div>`}
    ${closed.length ? `<div class="dept-title">Закриті недавно · ${closed.length}</div>
      <div class="soft-card">${closed.map(taskRow).join("")}</div>` : ""}`;

  $("person-away").onclick = () => absenceSheet(person);
  $("person-rate").onclick = () => rateSheet(person);
  wirePersonKpi(person);
  if (!kpiReady) loadPersonKpi(person);
}

/* Ставка людини у відсотках. Олег, 29.07: «давай % ставки вказувати людині».

   Це ПОСТІЙНА властивість, а не правка місяця: правка цілі живе рівно в тому
   періоді, на який поставлена, тож першого числа норма поверталась би до
   відділової — і «пів ставки» довелось би вбивати щомісяця заново. Ставка
   множить норму відділу назавжди, а відпустка й ручна правка лягають поверх. */
function personRate(person) {
  const rows = ((STATE.kpi && STATE.kpi.norms) || [])
    .map((n) => (n.rows || []).find((r) => r.person === person))
    .filter(Boolean);
  const withRate = rows.find((r) => r.rate != null);
  return withRate ? withRate.rate : 100;
}

/* Норми, де в людини стоїть РУЧНА правка цілі. Правка сильніша за ставку, і
   поки вона є — ставка на цифри не впливає взагалі. Мовчати про це не можна:
   менеджер ставить 60%, бачить 200 і думає, що зламалось. */
function rateClashes(person) {
  return ((STATE.kpi && STATE.kpi.norms) || []).map((n) => {
    const r = (n.rows || []).find((x) => x.person === person);
    return r && r.overridden ? { n, r } : null;
  }).filter(Boolean);
}

function rateSheet(person) {
  let rate = personRate(person);
  const clash = rateClashes(person);
  openSheet(`
    <h2>${esc(person)}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 14px">
      Ставка множить норму відділу назавжди — на відміну від правки цілі,
      яка діє один період.</p>
    ${clash.length ? `<div class="rt-clash">
      <div>Зараз ціль тримає ручна правка (${clash.map((c) =>
        `${c.r.target} · ${c.n.period === "week" ? "тиждень" : "місяць"}`).join(", ")}),
        а вона сильніша за ставку — тож ставка на ці цифри не вплине.</div>
      <button class="link-btn" id="rt-clear">${icon("x")} Прибрати правку, лишити ставку</button>
    </div>` : ""}
    <div class="chips" id="rt-quick">
      ${[100, 75, 50, 25].map((v) => `<button class="chip${v === rate ? " on" : ""}"
        data-rt="${v}">${v}%</button>`).join("")}
    </div>
    <div class="f-label">Або точно</div>
    <div class="stepper with-input">
      <button id="rt-minus">${icon("minus")}</button>
      <input id="rt-val" type="number" inputmode="numeric" min="0" max="100" value="${rate}">
      <button id="rt-plus">${icon("plus")}</button>
    </div>
    <div class="sheet-actions">
      <button class="sbtn" id="rt-cancel">Скасувати</button>
      <button class="sbtn primary" id="rt-save">Зберегти</button>
    </div>`);
  const paint = () => {
    if ($("rt-val").value !== String(rate)) $("rt-val").value = rate;
    $("rt-quick").querySelectorAll("[data-rt]").forEach((b) =>
      b.classList.toggle("on", +b.dataset.rt === rate));
  };
  $("rt-quick").querySelectorAll("[data-rt]").forEach((b) =>
    b.onclick = () => { rate = +b.dataset.rt; paint(); });
  $("rt-minus").onclick = () => { rate = Math.max(0, rate - 5); paint(); };
  $("rt-plus").onclick = () => { rate = Math.min(100, rate + 5); paint(); };
  $("rt-val").oninput = () => {
    const v = parseInt($("rt-val").value, 10);
    rate = Number.isFinite(v) ? Math.max(0, Math.min(100, v)) : 0;
  };
  $("rt-cancel").onclick = closeSheet;
  if (clash.length) $("rt-clear").onclick = async () => {
    try {
      for (const c of clash) {
        await api("/api/kpi/override", { method: "PUT", body: JSON.stringify(
          { norm_id: c.n.id, person, clear: true }) });
      }
      haptic("success");
      STATE.kpi = null;
      await loadKpi();
      render();
      rateSheet(person);        // та сама шторка, вже без застереження
    } catch (e) { toast(e.message); }
  };
  $("rt-save").onclick = async () => {
    try {
      await api("/api/rate", { method: "PUT",
        body: JSON.stringify({ person, rate }) });
      closeSheet();
      haptic("success");
      STATE.kpi = null;          // цілі змінились назавжди — зведення протухло
      STATE.dash.data = null;
      await loadKpi();
      render();
    } catch (e) { toast(e.message); }
  };
}

/* Відпустка / лікарняна / відрядження. Це факт про ЛЮДИНУ, а не про окрему
   норму: одна відпустка пропорційно знижує всі її цілі — і тижневі, і
   місячні — у кожному періоді, якого торкається. «Звільнити від норми» руками
   лишається для випадків, коли рішення не про календар. */
const ABSENCE_KINDS = [
  { v: "vacation", label: "Відпустка" },
  { v: "sick", label: "Лікарняна" },
  { v: "trip", label: "Відрядження" },
];

async function absenceSheet(person) {
  let list = [];
  try {
    list = (await api(`/api/absences?person=${encodeURIComponent(person)}`)).absences || [];
  } catch (e) { /* покажемо порожній список */ }
  const st = { kind: "vacation" };
  openSheet(`
    <h2>Відсутність</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">${esc(person)}</p>
    ${list.length ? `<div class="f-label" style="margin-top:0">Уже записано</div>
      ${list.map((a) => `
        <div class="pick-row" style="justify-content:space-between">
          <span>
            <span class="pk-name">${esc(a.title)}</span>
            <span class="pk-meta">${esc(shortDate(a.start))} — ${esc(shortDate(a.end))}${a.note ? " · " + esc(a.note) : ""}</span>
          </span>
          <button class="st-mark dropped" data-away-del="${a.id}"
            aria-label="Прибрати">${icon("x")}</button>
        </div>`).join("")}` : ""}
    <div class="f-label">Тип</div>
    ${segment("data-away-kind", ABSENCE_KINDS.map((k) => [k.v, k.label]), st.kind)}
    <div class="f-label">З якого дня</div>
    <input id="aw-start" type="date" value="${todayISO()}">
    <div class="f-label">По який (включно)</div>
    <input id="aw-end" type="date" value="${todayISO()}">
    <div class="mr-hint">Цілі KPI перерахуються самі: пропорційно робочим дням,
      які випали. Повністю пропущений період — людина зникає зі «Звіту».</div>
    <div class="sheet-actions">
      <button class="sbtn" id="aw-cancel">Скасувати</button>
      <button class="sbtn primary" id="aw-save">Зберегти</button>
    </div>`);
  $("sheet").querySelectorAll("[data-away-kind]").forEach((b) => b.onclick = () => {
    st.kind = b.dataset.awayKind;
    $("sheet").querySelectorAll("[data-away-kind]").forEach((x) =>
      x.classList.toggle("on", x.dataset.awayKind === st.kind));
  });
  $("sheet").querySelectorAll("[data-away-del]").forEach((b) => b.onclick = async () => {
    try {
      await api(`/api/absences/${b.dataset.awayDel}`, { method: "DELETE" });
      STATE.kpi = null;
      STATE.dash.data = null;
      closeSheet();
      renderPerson();
      toast("Прибрав");
    } catch (e) { toast(e.message); }
  });
  $("aw-cancel").onclick = closeSheet;
  $("aw-save").onclick = async () => {
    const start = $("aw-start").value, end = $("aw-end").value;
    if (!start || !end) { toast("Вкажи обидві дати"); return; }
    $("aw-save").disabled = true;
    try {
      await api("/api/absences", {
        method: "POST",
        body: JSON.stringify({ person, start, end, kind: st.kind }),
      });
      haptic("success");
      STATE.kpi = null;              // цілі змінились — зведення протухло
      STATE.dash.data = null;
      closeSheet();
      renderPerson();
      toast("Записав — цілі перерахую");
    } catch (e) {
      $("aw-save").disabled = false;
      toast(e.message);
    }
  };
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
  const st = { qty: t.qty, theme_id: t.theme_id, type: t.type || "",
               platform: t.platform || "telegram" };
  openSheet(`
    <h2>Редагувати завдання</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">${esc(t.person)}</p>
    <div class="f-label" style="margin-top:0">Тип матеріалу</div>
    ${segment("data-te-type", [["news", "Новина"], ["article", "Стаття"],
                               ["post", "Пост"], ["", "Будь-який"]], st.type)}
    <div id="te-platform" class="${st.type === "post" ? "" : "hidden"}">
      ${segment("data-te-plat", [["telegram", "Telegram"], ["instagram", "Instagram"]],
                st.platform, { sub: true })}
    </div>
    <div class="f-label">Кількість</div>
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
  // Тип: саме через нього зникає безликий «матеріал» у списку виконавиці
  $("sheet").querySelectorAll("[data-te-type]").forEach((b) => b.onclick = () => {
    st.type = b.dataset.teType;
    $("sheet").querySelectorAll("[data-te-type]").forEach((x) =>
      x.classList.toggle("on", x.dataset.teType === st.type));
    $("te-platform").classList.toggle("hidden", st.type !== "post");
  });
  $("sheet").querySelectorAll("[data-te-plat]").forEach((b) => b.onclick = () => {
    st.platform = b.dataset.tePlat;
    $("sheet").querySelectorAll("[data-te-plat]").forEach((x) =>
      x.classList.toggle("on", x.dataset.tePlat === st.platform));
  });
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
          type: st.type || null,
          platform: st.type === "post" ? st.platform : null,
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
      // Тап по засічці відкриває сам дедлайн, а не проєкт під нею. Слухач
      // на самій кнопці + stopPropagation: смуга проєкту лежить у тому ж
      // рядку, і без цього відкривався б проєкт.
      box.querySelectorAll("[data-dlmark]").forEach((b) => b.onclick = (e) => {
        e.stopPropagation();
        const [pid, due] = b.dataset.dlmark.split(":");
        const proj = projectById(+pid);
        if (!proj) return;
        const list = (proj.deadlines || []).filter((d) => d.due === due);
        if (list.length === 1) dlSheet(proj, list[0]);
        else if (list.length) dlPickSheet(proj, list);
      });
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

/* Кілька звітів на одну дату — спершу питаємо, який саме відкрити. Інакше
   тап по склеєному чипу «2» вів би навмання. */
function dlPickSheet(project, list) {
  openSheet(`
    <h2>${esc(shortDate(list[0].due))}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(project.partner || project.name)}</p>
    ${list.map((d, i) => `
      <button class="pick-row plain" data-dlpick="${i}">
        <span class="pk-txt">
          <span class="pk-name">${esc(dlLabel(d))}</span>
          ${d.assignee ? `<span class="pk-meta">${esc(d.assignee)}</span>` : ""}
        </span>
      </button>`).join("")}`);
  $("sheet").querySelectorAll("[data-dlpick]").forEach((b) => b.onclick = () => {
    closeSheet();
    dlSheet(project, list[+b.dataset.dlpick]);
  });
}

/* ---------- Засічки звітності на таймлайні ----------
   Олег, 29.07. Дані вже приїхали в bootstrap — сервер не чіпаємо.

   Шар лежить ПОВЕРХ рядка, а не всередині смуги, і на це дві причини:
   у .tl-bar стоїть overflow: clip (він потрібен липкому підпису), тож засічка
   там просто обрізалась би; а фінальний звіт часто припадає на дату ПІЗНІШЕ
   кінця проєкту — відзвітувати треба після закриття, і мітка мусить мати
   право стояти за межами смуги.

   Колір у нас уже зайнятий ідентичністю проєкту (вісім кольорів смуг), тому
   вид звіту показуємо ФОРМОЮ гліфа, а сам чип малюємо кольором поверхні —
   так він читається на будь-якій смузі без підбору під кожен колір. Те саме
   рішення, що border: 2px solid var(--card) між сусідніми смугами.

   Стан несе не колір проєкту, а сам чип: прострочене — єдине червоне на
   екрані, і його видно з іншого кінця гант-діаграми. */

const DL_GLYPH = { narrative: "file-text", financial: "bar-chart", milestone: "award" };

function dlMarkState(d, today) {
  if (d.status === "accepted") return "ok";
  if (d.status === "submitted") return "sent";
  return d.due < today ? "late" : "todo";
}

/* Збіги дат склеюємо: наративний і фінансовий звіти часто на одне число
   (у чаті фінансів вони теж ідуть одним нагадуванням), а два чипи на одному
   пікселі — це просто накладені один на одного кружечки. */
function timelineMarks(p, x, today) {
  const byDate = {};
  (p.deadlines || []).forEach((d) => {
    if (!d.due) return;
    (byDate[d.due] = byDate[d.due] || []).push(d);
  });
  return Object.entries(byDate).map(([due, list]) => {
    const ts = Date.parse(due + "T12:00:00") / 1000;
    // найгірший стан групи визначає вигляд: якщо один звіт прострочено,
    // група має світитись, навіть коли другий уже прийнято
    const rank = { late: 0, todo: 1, sent: 2, ok: 3 };
    const state = list.map((d) => dlMarkState(d, today))
      .sort((a, b) => rank[a] - rank[b])[0];
    const glyph = list.length > 1 ? null : DL_GLYPH[list[0].kind] || "calendar";
    return `<button class="tl-mark ${state}" data-dlmark="${p.id}:${due}"
      style="left:${x(ts).toFixed(1)}px"
      title="${esc(list.map(dlLabel).join(" · "))}">
      ${glyph ? icon(glyph) : `<i>${list.length}</i>`}
    </button>`;
  }).join("");
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

  const today = todayISO();
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
      ${timelineMarks(p, x, today)}
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
    <div class="tl-legend">
      <span class="tl-lg"><i class="tl-mark todo static">${icon("file-text")}</i>наративний</span>
      <span class="tl-lg"><i class="tl-mark todo static">${icon("bar-chart")}</i>фінансовий</span>
      <span class="tl-lg"><i class="tl-mark todo static">${icon("award")}</i>майлстоун</span>
      <span class="tl-lg"><i class="tl-mark late static">${icon("file-text")}</i>прострочено</span>
      <span class="tl-lg"><i class="tl-mark ok static">${icon("check")}</i>прийнято</span>
    </div>
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
  impact_credit: "award",
};
/* Події-нагороди мають виглядати нагородою, а не відмовою: у st-mark лише
   два стани, і все, що не «зроблено», малювалось червоним хрестом */
const NOTIF_GOOD = new Set(["task_done", "impact_credit"]);

function notifRow(n) {
  const when = n.created_at ? shortDate(n.created_at) : "";
  const m = n.meta || null;
  // Розкладений рядок (Олег, 30.07: «тематику догори, далі завдання, автора
  // вниз, біля автора — донора з маленькою іконкою»). Старі події без meta
  // малюються як раніше, лише без обрізання в один рядок.
  // «1 із 1» не має рватись переносом посеред числа
  const glue = (t) => esc(t).replace(/(\d+) із (\d+)/g, "$1\u00A0із\u00A0$2");
  const structured = m && (m.theme || m.task_line);
  const main = structured ? `
    <span class="tr-main">
      <span class="tr-who">${esc(m.theme || m.task_line)}</span>
      <span class="tr-what">${m.theme ? esc(m.task_line) + " · " : ""}${glue(n.body || "")}</span>
      <span class="nt-by">
        ${m.person ? `<span class="nt-name">${esc(m.person)}</span>` : ""}
        ${m.donor ? `${donorChip(m.donor)}<span class="nt-donor">${esc(m.donor)}</span>` : ""}
        <span class="nt-when">${esc(when)}</span>
      </span>
    </span>` : `
    <span class="tr-main">
      <span class="tr-who">${esc(n.title)}</span>
      ${n.body ? `<span class="tr-what">${glue(n.body)}</span>` : ""}
    </span>`;
  const inner = `
    <span class="st-mark ${NOTIF_GOOD.has(n.kind) ? "done" : "dropped"}">
      ${icon(NOTIF_ICON[n.kind] || "bell")}</span>
    ${main}
    ${structured ? "" : `<span class="tr-right"><span class="mr-d">${esc(when)}</span></span>`}`;
  const cls = `task-row nt-row${n.unread ? " unread" : ""}`;
  return n.url
    ? `<a class="${cls}" href="${esc(n.url)}" data-ext="${esc(n.url)}">${inner}</a>`
    : `<div class="${cls}">${inner}</div>`;
}

async function renderAlerts() {
  const already = STATE.pending && STATE.notifs && STATE.absenceRequests;
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
      // Три джерела, один екран — тягнемо паралельно. Запити на відпустку
      // деградують окремо: їхній збій не має гасити чергу звірки.
      const [q, feed, ar] = await Promise.all([
        api("/api/matches/pending"), api("/api/notifications"),
        api("/api/absences/requests").catch(() => ({ requests: [] })),
      ]);
      STATE.pending = q.pending || [];
      STATE.notifs = feed.items || [];
      STATE.unread = feed.unread || 0;
      STATE.absenceRequests = ar.requests || [];
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

/* З якого екрана починати. web_app-кнопка не вміє start_param (це не
   t.me-лінк), тож екран передається звичайним query-параметром до URL апки —
   так пінг «записав контакт» відкриває одразу базу, а не головну. */
const START_SCREENS = ["contacts", "reports", "alerts", "todo"];

/* Нагадування в «винюхав» — одне повідомлення на одну обіцянку, тож і кнопка
   під ним має вести в ЦЮ обіцянку, а не в загальну чергу: інакше людина
   прочитала конкретну історію, тапнула — і шукає її очима серед сотень.
   startapp=promise_<id>; голе `promises` лишається входом у чергу. */
function promiseDeepLink(start) {
  const m = /^promise_(\d+)$/.exec(start || "");
  return m ? +m[1] : null;
}

function startScreen() {
  try {
    const v = new URLSearchParams(location.search).get("screen");
    return START_SCREENS.includes(v) ? v : null;
  } catch (e) { return null; }
}

/* ---------- Телефонна база редакції ----------
   Олег, 29.07: «у кого є телефон віцемера Лукова?» — хтось кидає в чат, а
   через пів року питають знову. Два входи, обидва ручні: форма тут і
   пересланий Лису контакт (чат бот не сканує — свідоме рішення).

   Пошук один на все: реальне питання частіше «хто в нас по енергетиці?», ніж
   «дай Лукова», тож теми шукаються нарівні з іменем і посадою. */

function contactRow(c) {
  const meta = [c.role, c.tags].filter(Boolean).join(" · ");
  return `
    <div class="cnt-row" data-cedit="${c.id}">
      <span class="cnt-main">
        <span class="cnt-n">${esc(c.name)}</span>
        ${meta ? `<span class="cnt-m">${esc(meta)}</span>` : ""}
      </span>
      ${c.phone ? `<a class="cnt-tel" href="tel:${esc(telHref(c.phone))}"
        onclick="event.stopPropagation()">${icon("phone")}${
        (c.phones || []).length > 1
          ? `<i class="cnt-more">+${(c.phones || []).length - 1}</i>` : ""}</a>` : ""}
      ${icon("chevron-right", "ic chev")}
    </div>`;
}

async function renderContacts() {
  const q = STATE.contactQuery || "";
  const head = `
    <div class="head-row">
      <div class="h-big">Контакти</div>
      <button class="icon-btn" id="cnt-add" aria-label="Додати контакт">
        ${icon("plus")}</button>
    </div>
    <div class="h-sub">телефони редакції — спільні на всіх</div>
    ${searchBox("cnt-q", q, "імʼя, посада, тема, номер…")}`;
  $("content").innerHTML = head + `<div id="cnt-body">${skeleton("rows", 4)}</div>`;
  wireContactControls();
  let data;
  try {
    data = await api(`/api/contacts?q=${encodeURIComponent(q)}`);
  } catch (e) {
    const b = $("cnt-body");
    if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "contacts") return;
  const list = data.contacts || [];
  const body = $("cnt-body");
  if (!body) return;
  body.innerHTML = list.length
    ? `<div class="soft-card">${list.map(contactRow).join("")}</div>
       ${data.mine ? `<div class="cnt-thanks">У базі ${data.mine.total} ${
         esc(plural(data.mine.total, "контакт", "контакти", "контактів"))}.
         ${data.mine.added ? `Твоїх — ${data.mine.added}. Дякуємо 💙` : ""}</div>` : ""}`
    : `<div class="empty-hint">${q
        ? "За таким запитом нікого."
        : "База поки порожня.<br>Додай контакт кнопкою «＋» або перешли картку Лису в приват."}</div>`;
  body.querySelectorAll("[data-cedit]").forEach((r) => r.onclick = () => {
    const c = list.find((x) => x.id === +r.dataset.cedit);
    if (c) contactSheet(c);
  });
}

function wireContactControls() {
  $("cnt-add").onclick = () => contactSheet(null);
  const input = $("cnt-q");
  if (!input) return;
  let t = null;
  input.oninput = () => {
    STATE.contactQuery = input.value;
    clearTimeout(t);
    // Пошук іде в Нору — не смикаємо її на кожну літеру
    t = setTimeout(() => { if (STATE.view === "contacts") renderContacts(); }, 300);
  };
}

/* Кілька номерів на людину (Олег, 29.07). У чиновника зазвичай мобільний,
   приймальня і прессекретар — і саме той, що потрібен зараз, найчастіше не
   перший. Мітки не вигадуємо: люди пишуть по-своєму («0512… приймальня»), і
   словник міток змусив би їх воювати з формою. Пошук іде по всіх номерах. */
function telHref(phone) {
  // У tel: лишаємо тільки те, що набирається: підпис «приймальня» в номері
  // не заважає людині, але заважає телефону
  return (phone || "").replace(/[^\d+]/g, "");
}

function phoneInputs(c) {
  const list = (c && c.phones && c.phones.length ? c.phones : [""]);
  return list.map((p, i) => phoneRow(p, i)).join("");
}

function phoneRow(value, i) {
  return `
    <div class="c-phone-row">
      <input class="c-phone" type="tel" maxlength="60" value="${esc(value || "")}"
        placeholder="${i ? "0512… приймальня" : "+380…"}">
      ${i ? `<button class="c-phone-del" aria-label="Прибрати">${icon("x")}</button>` : ""}
    </div>`;
}

function collectPhones() {
  return Array.from(document.querySelectorAll("#c-phones .c-phone"))
    .map((i) => i.value.trim()).filter(Boolean);
}

function wirePhoneRows() {
  const box = $("c-phones");
  if (!box) return;
  box.querySelectorAll(".c-phone-del").forEach((b) => b.onclick = () => {
    b.closest(".c-phone-row").remove();
    if (!box.querySelector(".c-phone-row")) box.innerHTML = phoneRow("", 0);
    wirePhoneRows();
  });
}

/* «Чик-чик» — підтягнути посаду з архіву (Олег, 29.07: «щоб я міг натиснути
   на сірого Миколу Логвинова, і йому підтяглася сутність, що він директор
   Миколаївобтеплоенерго»).

   Сутнісний шар нори вже знає, ким людина була в новинах — знання просто
   лежало не там, де його питають. Підставляємо в поле, але НЕ зберігаємо
   самі: рішення за людиною, а посади змінюються.

   Чесна межа: сутності залито приблизно за останні 2–3 роки, тож знайдеться
   не кожен — так і кажемо, а не вдаємо, що людини не існує. */
async function lookupEntity() {
  const name = $("c-name").value.trim();
  const box = $("c-found");
  if (!name) { toast("Спершу імʼя"); return; }
  box.innerHTML = `<span class="sk" style="display:block;height:14px;width:60%"></span>`;
  let data;
  try {
    data = await api(`/api/contacts/lookup?name=${encodeURIComponent(name)}`);
  } catch (e) { box.textContent = e.message; return; }
  const found = (data.candidates || []).filter((c) => c.role);
  if (!found.length) {
    box.innerHTML = `<span class="cnt-nofound">В архіві не знайшов.
      Сутності залито приблизно за останні 2–3 роки — старіших там ще немає.</span>`;
    return;
  }
  box.innerHTML = found.map((c, i) => `
    <button class="cnt-cand" data-cand="${i}">
      <span class="cnt-cand-r">${esc(c.role)}</span>
      <span class="cnt-cand-m">${esc(c.name || "")} · ${c.mentions} ${
        esc(plural(c.mentions, "згадка", "згадки", "згадок"))}${
        c.last_year ? ` · востаннє ${c.last_year}` : ""}</span>
    </button>`).join("");
  box.querySelectorAll("[data-cand]").forEach((b) => b.onclick = () => {
    const c = found[+b.dataset.cand];
    $("c-role").value = c.role;
    box.innerHTML = `<span class="cnt-nofound">Підставив. Перевір і збережи —
      посади змінюються.</span>`;
    haptic("success");
  });
}

/* Хто завів картку і хто правив востаннє. Олег, 29.07: «краще б тут писалось,
   хто додав номер» — замість нотатки «переслано в Лиса», яка займала єдине
   вільне поле і не казала нічого. Це підпис, а не поле: авторство не
   редагується. */
function authorLine(c) {
  if (!c || !c.added_by) return "";
  const when = c.created_at ? shortDate(c.created_at.slice(0, 10)) : "";
  const parts = [`Додав${c.added_by === STATE.me.name ? "(ла) ти" : ": " + esc(c.added_by)}`];
  if (when) parts.push(when);
  if (c.updated_by && c.updated_by !== c.added_by) {
    parts.push(`правив(ла) ${esc(c.updated_by)}`);
  }
  return `<div class="cnt-author">${parts.join(" · ")}</div>`;
}

/* Картка контакту. Посада й теми — окремі поля, бо саме за ними шукають:
   «хто в нас по енергетиці» — це пошук по темах, а не по імені.

   Підказки в полях — ЗАВЕДОМО вигадана людина («Степан Яблучко», «директор
   ЖЕК №5»). Спершу там стояв «Ігор Луков · віцемер Миколаєва» — реальне
   прізвище з вигаданим імʼям, і Олег одразу сказав: справжній Луков існує, а
   Івана Лукова немає, такі підказки збивають. У базі, де лежать справжні
   чиновники, підказка не має виглядати як запис. */
function contactSheet(c) {
  const v = (k) => esc((c && c[k]) || "");
  openSheet(`
    <h2>${c ? "Контакт" : "Новий контакт"}</h2>
    <div class="f-label" style="margin-top:0">Імʼя</div>
    <input id="c-name" maxlength="120" value="${v("name")}" placeholder="Степан Яблучко">
    <div class="f-label">Посада й орган</div>
    <input id="c-role" maxlength="160" value="${v("role")}"
      placeholder="директор ЖЕК №5">
    <button class="link-btn" id="c-lookup">${icon("search")} Підтягнути з архіву</button>
    <div id="c-found" class="cnt-found"></div>
    <div class="f-label">Телефони</div>
    <div id="c-phones">${phoneInputs(c)}</div>
    <button class="link-btn" id="c-addphone">${icon("plus")} Ще номер</button>
    <div class="f-label">Telegram</div>
    <input id="c-tg" maxlength="60" value="${v("telegram")}" placeholder="@нік">
    <div class="f-label">Теми — через кому</div>
    <input id="c-tags" maxlength="200" value="${v("tags")}"
      placeholder="міськрада, бюджет, енергетика">
    <div class="f-label">Нотатка</div>
    <input id="c-note" maxlength="200" value="${v("note")}"
      placeholder="коли краще телефонувати, хто познайомив…">
    ${authorLine(c)}
    <div class="sheet-actions">
      ${c && STATE.me.manager
        ? `<button class="sbtn danger" id="c-del">Видалити</button>` : ""}
      <button class="sbtn primary" id="c-save">Зберегти</button>
    </div>`);
  wirePhoneRows();
  $("c-addphone").onclick = () => {
    const box = $("c-phones");
    box.insertAdjacentHTML("beforeend",
      phoneRow("", box.querySelectorAll(".c-phone-row").length));
    wirePhoneRows();
    const rows = box.querySelectorAll(".c-phone");
    rows[rows.length - 1].focus();
  };
  $("c-lookup").onclick = lookupEntity;
  $("c-save").onclick = async () => {
    const body = {
      name: $("c-name").value.trim(), role: $("c-role").value.trim(),
      phones: collectPhones(), telegram: $("c-tg").value.trim(),
      tags: $("c-tags").value.trim(), note: $("c-note").value.trim(),
    };
    if (!body.name) { toast("Без імені картка не має сенсу"); return; }
    try {
      await api(c ? `/api/contacts/${c.id}` : "/api/contacts", {
        method: c ? "PATCH" : "POST", body: JSON.stringify(body),
      });
      closeSheet();
      haptic("success");
      renderContacts();
    } catch (e) { toast(e.message); }
  };
  if (c && STATE.me.manager) $("c-del").onclick = async () => {
    if (!(await confirmAction(`Видалити «${c.name}» з бази?`))) return;
    try {
      await api(`/api/contacts/${c.id}`, { method: "DELETE" });
      closeSheet();
      renderContacts();
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Імпакт-архів ----------

   Олег, 29.07: «мені постійно важко шукати і наново описувати імпакти при
   кожній заявці по грант». Флоу: «+» → URL новини-фіксації + суть своїми
   словами → бот сам збирає серію (беклінки зі сторінки + нора), донорський
   заголовок, наратив, ключовий текст і медальки. Це ЧЕРНЕТКА: ключовий можна
   перепризначити (вага в серії різна — розслідування важить більше за
   новину-фіксацію), зайве прибрати, імена поправити. Готове — файлом у приват.

   Донор ключового тексту підсвічений окремо: якщо текст, що все змінив,
   вийшов у межах проєкту — донора можна порадувати кейсом. */
/* ---------- Банк тем ----------

   Черга того, що влада наобіцяла. Три речі, яких немає в чаті і заради яких
   екран узагалі знадобився (перший місяць дав 473 обіцянки — списком по
   вісім штук це не переглянути):

   • фасети з ЧИСЛАМИ: одразу видно, скільки чого, а не «показано 8 з 473»;
   • смуга кольору кодує КЛАС ПЕРЕВІРКИ, а не важливість. «Перевірити нічим»
     сіре і ніколи не дзвонить, «піти й подивитись» зелене — дешева перевірка
     підіймає тему, а не відсіює її;
   • дублі окремим входом: та сама обіцянка з двох статей лягла двома
     записами, і скорочувати банк починають звідси, а не з видалення тем.

   Цитата в кожній картці не декор: обіцянка без дослівного фрагмента в банк
   не пишеться взагалі, і саме цитата відрізняє факт від переказу. */

const PROMISE_EMPTY = {
  all: "У банку порожньо.",
  overdue: "Прострочених немає — рідкісний день.",
  soon: "Найближчим часом нічого не горить.",
  waiting: "Обіцянок, що чекають події, немає.",
  stale: "Забутих тем немає.",
  noproof: "Обіцянок без способу перевірки немає.",
  populism: "Заяв без дати й критерію не знайшлось.",
  fresh: "За тиждень нових обіцянок не з'явилось.",
  mine: "З твоїх матеріалів обіцянок поки немає.",
  closed: "Перевірених ще немає — після походу натисни «Перевірили».",
};

async function renderPromises() {
  const facet = STATE.promiseFacet || "all";
  const head = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="head-row">
      <div class="h-big">Банк тем</div>
      <button class="icon-btn" id="pr-search" aria-label="Пошук">${icon("search")}</button>
    </div>`;
  if (!STATE.promises) {
    $("content").innerHTML = head + `<div id="pr-body">${skeleton("rows", 4)}</div>`;
    wirePromiseSearch();
    try {
      STATE.promises = await api("/api/promises?cls=" + encodeURIComponent(facet)
        + (STATE.promiseQuery ? "&q=" + encodeURIComponent(STATE.promiseQuery) : ""));
    } catch (e) {
      const b = $("pr-body");
      if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
      return;
    }
    if (STATE.view !== "promises") return;
  } else {
    $("content").innerHTML = head + `<div id="pr-body"></div>`;
    wirePromiseSearch();
  }
  paintPromises();
}

/* Межі даних — у кожному виводі. Порожній екран без цього рядка читається як
   зламаний, а не як «сюди ще не дійшли руки». */
function promiseBounds(d) {
  const b = d.bounds || {};
  if (!b.from) return d.total ? "" : "Банк ще порожній — обіцянки збирає бот.";
  const dt = new Date(b.from * 1000);
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  return `з ${mm}.${dt.getFullYear()} · переглянуто ${b.articles} матеріалів`;
}

function paintPromises() {
  const d = STATE.promises;
  const body = $("pr-body");
  if (!body || !d) return;
  const facet = STATE.promiseFacet || "all";
  const bounds = promiseBounds(d);
  body.innerHTML = `
    ${STATE.promiseQuery ? `<div class="pr-q">
        Пошук: <b>${esc(STATE.promiseQuery)}</b>
        ${(d.matched || []).length ? `<span class="pr-q-m">знайшлись картки:
          ${d.matched.map((m) => esc(m.name)).join(" · ")}</span>` : ""}
        <button class="pr-q-x" id="pr-clear">${icon("x")}</button>
      </div>` : ""}
    <div class="chips" id="pr-facets">
      ${(d.facets || []).map((f) => `
        <button class="chip${f.key === facet ? " on" : ""}" data-facet="${f.key}">
          ${esc(f.label)} <b>${f.n}</b></button>`).join("")}
    </div>
    ${d.dupes ? `<button class="pr-dupes" data-nav="dupes">
        ${icon("link")} <span>Схоже на дублі — <b>${d.dupes}</b></span>
        <span class="pr-dupes-m">та сама обіцянка з двох статей</span>
        ${icon("chevron-right", "ic chev")}</button>` : ""}
    ${d.items.length ? d.items.map(promiseCard).join("")
      : `<div class="empty-hint">${esc(PROMISE_EMPTY[facet] || PROMISE_EMPTY.all)}</div>`}
    ${d.total > d.items.length + d.offset ? `<button class="pr-more" id="pr-more">
        Ще ${d.total - d.items.length - d.offset}</button>` : ""}
    ${bounds ? `<div class="pr-bounds">${esc(bounds)}</div>` : ""}`;
  wirePromises();
}

function promiseWho(w) {
  if (!w) return "";
  const parts = [];
  if (w.promiser) {
    parts.push(`Хто обіцяв: <b>${esc(w.promiser)}</b>`
      + (w.role ? `, ${esc(w.role)}` : ""));
  } else if (w.hidden) {
    parts.push("Хто саме — не названо");
  }
  if (w.reported) parts.push(`переказує <b>${esc(w.reported)}</b>`);
  if (w.owner) parts.push(`відповідає ${esc(w.owner)}`);
  return parts.join(" · ");
}

function promiseCard(p) {
  const who = promiseWho(p.who);
  return `
    <article class="pr-item ${p.cls}" data-promise="${p.id}">
      <div class="pr-row1">
        <span class="pr-state">${esc(p.state)}</span>
        ${p.how ? `<span class="pr-how${p.cheap ? " cheap" : ""}">${esc(p.how)}</span>` : ""}
      </div>
      <div class="pr-title">${esc(p.title)}</div>
      ${p.image ? `<img class="pr-img" src="${esc(p.image)}" alt="" loading="lazy"
        onerror="this.remove()">` : ""}
      ${who ? `<div class="pr-who">${who}</div>` : ""}
      ${p.quote ? `<div class="pr-quote">«${esc(p.quote)}»</div>` : ""}
      ${p.populism ? `<div class="pr-pop">
          <span class="pr-pop-tag">Схоже на популізм</span>
          <span class="pr-pop-why">${esc(p.populism)}</span>
        </div>` : ""}
      ${p.meta.length || p.author || p.found ? `<div class="pr-meta">${
        [STATE.promiseFacet === "fresh" ? p.found : "", ...p.meta,
         p.author ? "автор: " + p.author : ""]
          .filter(Boolean).map(esc).join(" · ")}</div>` : ""}
    </article>`;
}

function wirePromiseSearch() {
  const btn = $("pr-search");
  if (!btn) return;
  btn.onclick = () => {
    openSheet(`
      <h2>Пошук у банку</h2>
      <p class="sheet-note">За обіцяльником або об'єктом: «Сєнкевич», «гімназія».
      По назві органу підтягуються й обіцянки його посадовців.</p>
      <form id="pr-sform">
        <input id="pr-sq" maxlength="80" autocomplete="off" placeholder="Кого шукаємо?"
          value="${esc(STATE.promiseQuery || "")}">
        <button class="cta" type="submit">Шукати</button>
      </form>`);
    const inp = $("pr-sq");
    if (inp) inp.focus();
    $("pr-sform").onsubmit = (e) => {
      e.preventDefault();
      const v = $("pr-sq").value.trim();
      closeSheet();
      STATE.promiseQuery = v.length > 3 ? v : "";
      if (v && v.length <= 3) { toast("Треба хоча б чотири літери"); return; }
      STATE.promises = null;
      renderPromises();
    };
  };
}

function wirePromises() {
  const body = $("pr-body");
  if (!body) return;
  body.querySelectorAll("[data-facet]").forEach((b) => b.onclick = () => {
    STATE.promiseFacet = b.dataset.facet;
    STATE.promises = null;
    renderPromises();
  });
  body.querySelectorAll("[data-promise]").forEach((el) => el.onclick = () =>
    nav("promise", +el.dataset.promise));
  const clear = $("pr-clear");
  if (clear) clear.onclick = () => {
    STATE.promiseQuery = "";
    STATE.promises = null;
    renderPromises();
  };
  const more = $("pr-more");
  if (more) more.onclick = async () => {
    more.disabled = true;
    try {
      const next = await api("/api/promises?cls="
        + encodeURIComponent(STATE.promiseFacet || "all")
        + "&offset=" + (STATE.promises.offset + STATE.promises.items.length)
        + (STATE.promiseQuery ? "&q=" + encodeURIComponent(STATE.promiseQuery) : ""));
      STATE.promises.items = STATE.promises.items.concat(next.items);
      paintPromises();
    } catch (e) { toast(e.message); more.disabled = false; }
  };
}

/* ---------- Картка обіцянки: історія питання ----------

   Головне тут — ланцюг із лінком на КОЖЕН факт. Обіцянку переносять тихо, і
   довести це можна лише двома датами поруч із двома цитатами. Точка кроку
   червона там, де строк у ревізії пізніший за попередній: перенос
   вираховується з дат, а не зі слів. */

async function renderPromise() {
  const id = STATE.currentPromise;
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    ${skeleton("rows", 4)}`;
  let d;
  try {
    d = await api(`/api/promises/${id}`);
  } catch (e) {
    $("content").innerHTML = `<button class="back" data-back>${icon("chevron-left")} Назад</button>
      <div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "promise") return;
  const p = d.commitment;
  const mgr = STATE.me.manager;
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Банк тем</button>
    <div class="pr-detail ${p.cls}">
      <div class="pr-dtitle">${esc(p.title)}</div>
      ${p.image ? `<img class="pr-img" src="${esc(p.image)}" alt="" loading="lazy"
        onerror="this.remove()">` : ""}
      ${p.found ? `<div class="pr-found">${esc(p.found)}</div>` : ""}
      <div class="pr-tags">${(p.tags || []).map((t) =>
        `<span class="pr-tag${t.danger ? " danger" : ""}">${esc(t.text)}</span>`).join("")}</div>
      ${promiseWho(p.who) ? `<div class="pr-who">${promiseWho(p.who)}</div>` : ""}
      ${p.populism ? `<div class="pr-pop">
          <span class="pr-pop-tag">Схоже на популізм</span>
          <span class="pr-pop-why">${esc(p.populism)}</span>
          <span class="pr-pop-note">Це підказка, а не вирок: мітка стоїть на
            заяві, а не на людині — брати тему в роботу вирішуєш ти.</span>
        </div>` : ""}
      ${p.criterion ? `<div class="pr-field"><b>Критерій:</b> ${esc(p.criterion)}</div>` : ""}
      ${p.based_on ? `<div class="pr-field"><b>Підстава:</b> ${esc(p.based_on)}</div>` : ""}
      ${p.condition ? `<div class="pr-field"><b>Умова:</b> ${esc(p.condition)}
        <span class="pr-dim">— умовна обіцянка не прострочується</span></div>` : ""}
      <div class="pr-chain">${d.chain.map(chainStep).join("")}</div>
      ${d.siblings.length ? `<div class="pr-sib">
        <div class="pr-sib-h">Та сама тема</div>
        ${d.siblings.map((s) => `<button class="pr-sib-r" data-sib="${s.id}">
          ${esc(s.title)}${icon("chevron-right", "ic chev")}</button>`).join("")}
      </div>` : ""}
      <div class="pr-act">
        <button class="sbtn primary" id="pr-work">${mgr ? "Дати в роботу" : "Взяти в роботу"}</button>
        <button class="sbtn" id="pr-check">Перевірили</button>
      </div>
      <div class="pr-note">${mgr
        ? "<b>Дати в роботу</b> відкриє звичайну форму завдання — людина, тип, проєкт, тематика, дедлайн."
        : "<b>Взяти в роботу</b> бере тему на себе і йде редактору на погодження: донора, проєкт і формат проставляє він."}</div>
      ${mgr ? `<button class="pr-drop" id="pr-drop">Не наша тема — прибрати з банку</button>` : ""}
    </div>`;
  wirePromiseCard(d);
}

function chainStep(s) {
  return `
    <div class="pr-step ${s.kind}">
      <div class="pr-when">${esc(s.when)}</div>
      <div class="pr-sbody">
        ${s.modality ? `<span class="pr-mod">${esc(s.modality)}</span>` : ""}
        ${s.quote ? `<p>«${esc(s.quote)}»</p>` : ""}
        <div class="pr-src">
          ${s.deadline ? `строк ${esc(s.deadline)} · ` : ""}
          ${s.source ? esc(s.source) + " · " : ""}
          ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${
            esc(s.article_title || "матеріал")}</a>` : "джерела немає"}
          ${s.author ? `<div class="pr-author">${icon("edit", "ic xs")}
            ${esc(s.author)}</div>` : ""}
        </div>
      </div>
    </div>`;
}

function wirePromiseCard(d) {
  const id = d.commitment.id;
  document.querySelectorAll("[data-sib]").forEach((b) => b.onclick = () =>
    nav("promise", +b.dataset.sib));
  const check = $("pr-check");
  // «Перевірили» ПИТАЄ результат, а не просто відсуває тему вниз. Висновок і
  // є продукт банку: обіцянка, перевірена й зірвана, — факт, на який
  // посилаються в наступному тексті, а не «менш терміновий рядок черги».
  if (check) check.onclick = () => openSheet(`
    <h2>Що з обіцянкою?</h2>
    <p class="sheet-note">Відповідь лишиться в банку назавжди — саме з неї
      потім видно, хто скільки разів зривав.</p>
    <button class="pick-row" data-outcome="done">
      <span class="door-ic c-good-ic">${icon("check")}</span>
      <span class="pk-txt"><span class="pk-name">Виконано</span>
      <span class="pk-meta">зробили, питання закрите</span></span>
    </button>
    <button class="pick-row" data-outcome="failed">
      <span class="door-ic c-red-ic">${icon("x")}</span>
      <span class="pk-txt"><span class="pk-name">Не виконано</span>
      <span class="pk-meta">строк минув, обіцянку зірвано</span></span>
    </button>
    <button class="pick-row" data-outcome="expected">
      <span class="door-ic c-blue-ic">${icon("chevron-right")}</span>
      <span class="pk-txt"><span class="pk-name">Ще в процесі</span>
      <span class="pk-meta">подивився, лишається в черзі</span></span>
    </button>
    <input id="pr-note" maxlength="300" autocomplete="off"
      placeholder="Що саме побачив? (не обов'язково)">`);

  if (check) check.addEventListener("click", () => {
    // Кнопки шторки вішаємо ПІСЛЯ її відкриття, як усюди в апці: openSheet
    // щоразу підміняє вузол, тож делегат на document накопичувався б.
    $("sheet").querySelectorAll("[data-outcome]").forEach((b) => b.onclick = async () => {
      const outcome = b.dataset.outcome;
      const note = ($("pr-note") || {}).value || "";
      b.disabled = true;
      try {
        await api(`/api/promises/${id}/check`, { method: "POST",
          body: JSON.stringify({ outcome, note }) });
        haptic("success");
        closeSheet();
        toast(outcome === "failed" ? "Записав: обіцянку зірвано"
          : outcome === "done" ? "Записав: виконано"
          : "Позначив перевіреною — лишається в черзі");
        STATE.promises = null;
        nav("promises");
      } catch (e) { toast(e.message); b.disabled = false; }
    });
  });
  const work = $("pr-work");
  if (work) work.onclick = async () => {
    if (STATE.me.manager) {
      // Банк тем — ще один вхід у постановку завдань, а не окремий світ:
      // ведемо в ту саму форму, лише з підказаною нотаткою.
      STATE.promiseSeed = { id, title: d.commitment.title };
      nav("people");
      return;
    }
    work.disabled = true;
    try {
      await api(`/api/promises/${id}/take`, { method: "POST" });
      haptic("success");
      toast("Редактор побачить у «Сповіщеннях»");
    } catch (e) { toast(e.message); work.disabled = false; }
  };
  const drop = $("pr-drop");
  if (drop) drop.onclick = async () => {
    if (!(await confirmAction("Прибрати обіцянку з банку? Знімок лишиться в журналі."))) return;
    try {
      await api(`/api/promises/${id}/drop`, { method: "POST",
        body: JSON.stringify({ reason: "не наша тема" }) });
      toast("Прибрав");
      STATE.promises = null;
      nav("promises");
    } catch (e) { toast(e.message); }
  };
}

/* ---------- Дублі ----------

   473 обіцянки за місяць — число завищене: суддя ланцюга спрацював 63 рази
   на 762 статті, і та сама обіцянка з двох статей лягла двома записами.
   Тут вони стоять парами з цитатами: злиття не видаляє нічого, а СКЛЕЮЄ
   ланцюг — обидві цитати лишаються доказами в одній картці.

   Вирішує людина: однакові назва і строк бувають і в двох сусідніх
   дитсадків, і відрізняє їх саме текст. */

async function renderDupes() {
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Банк тем</button>
    <div class="h-big">Схоже на дублі</div>
    ${skeleton("rows", 3)}`;
  let d;
  try {
    d = await api("/api/promises/dupes");
  } catch (e) {
    $("content").innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "dupes") return;
  STATE.dupes = d;
  paintDupes();
}

function paintDupes() {
  const d = STATE.dupes;
  const mgr = STATE.me.manager;
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Банк тем</button>
    <div class="h-big">Схоже на дублі</div>
    <div class="h-sub">одна обіцянка, записана з двох статей: однаковий строк
      і спільний предмет або обіцяльник. Злиття не видаляє — обидві цитати
      лишаються в одній картці${mgr ? "" : ". Зливає редактор"}</div>
    ${d.pairs.length ? d.pairs.map((p) => dupePair(p, mgr)).join("")
      : `<div class="empty-hint">Дублів не видно.</div>`}`;
  wireDupes();
}

function dupeSide(p) {
  return `
    <div class="dp-side ${p.cls}">
      <div class="dp-state">${esc(p.state)}</div>
      <div class="dp-title">${esc(p.title)}</div>
      ${p.quote ? `<div class="pr-quote">«${esc(p.quote)}»</div>` : ""}
      <div class="pr-meta">${p.meta.map(esc).join(" · ")}</div>
      <button class="dp-open" data-promise="${p.id}">Відкрити картку</button>
    </div>`;
}

function dupePair(pair, mgr) {
  return `
    <div class="dp-pair">
      ${dupeSide(pair.a)}
      <div class="dp-mid">схожість ${String(pair.sim).replace(".", ",")}</div>
      ${dupeSide(pair.b)}
      ${mgr ? `<div class="dp-act">
        <button class="sbtn" data-merge="${pair.a.id}:${pair.b.id}">
          Лишити перший</button>
        <button class="sbtn" data-merge="${pair.b.id}:${pair.a.id}">
          Лишити другий</button>
      </div>
      <button class="dp-keep" data-keep="${pair.a.id}:${pair.b.id}">
        Це різні — лишити обидві</button>` : ""}
    </div>`;
}

function wireDupes() {
  document.querySelectorAll("[data-promise]").forEach((b) => b.onclick = () =>
    nav("promise", +b.dataset.promise));
  document.querySelectorAll("[data-merge]").forEach((b) => b.onclick = async () => {
    const [keep, dup] = b.dataset.merge.split(":").map(Number);
    if (!(await confirmAction("Звести в один запис? Ревізії другого перейдуть у перший."))) return;
    b.disabled = true;
    try {
      await api(`/api/promises/${keep}/merge`, { method: "POST",
        body: JSON.stringify({ dup_id: dup }) });
      haptic("success");
      STATE.dupes = null;
      STATE.promises = null;
      renderDupes();
    } catch (e) { toast(e.message); b.disabled = false; }
  });
  // «Це різні» — рішення на все життя пари: детектор бачить сигнали, а не
  // суть, і два меморіальні комплекси з однаковим строком він розрізнити не
  // може ніколи. Без цієї кнопки та сама пара поверталась би щоразу.
  document.querySelectorAll("[data-keep]").forEach((b) => b.onclick = async () => {
    const [a, other] = b.dataset.keep.split(":").map(Number);
    b.disabled = true;
    try {
      await api(`/api/promises/${a}/notdupe`, { method: "POST",
        body: JSON.stringify({ other_id: other }) });
      haptic("success");
      toast("Запам'ятав — більше не питатиму");
      STATE.dupes = null;
      STATE.promises = null;
      renderDupes();
    } catch (e) { toast(e.message); b.disabled = false; }
  });
}

async function renderImpacts() {
  const head = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="head-row">
      <div class="h-big">Імпакт-архів</div>
      <button class="icon-btn" id="im-add" aria-label="Новий кейс">${icon("plus")}</button>
    </div>
    <div class="h-sub">що змінилось завдяки нашим текстам — для заявок і донорів</div>`;
  $("content").innerHTML = head + `<div id="im-body">${skeleton("rows", 3)}</div>`;
  $("im-add").onclick = impactAddSheet;
  let data;
  try {
    data = await api("/api/impacts");
  } catch (e) {
    const b = $("im-body");
    if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "impacts") return;
  STATE.impacts = data.impacts || [];
  paintImpacts();
}

/* Логотип донора за назвою партнера — з уже завантажених проєктів CMS.
   Немає лого (чи проєкт закінчився і випав зі списку) — чипом з назвою. */
function donorChip(name) {
  const proj = (STATE.projects || []).find((p) => p.partner === name && p.logo);
  return proj
    ? `<span class="imp-donor" title="${esc(name)}">${imgHtml(proj.logo, proj.logo_orig)}</span>`
    : `<span class="imp-donor imp-donor-txt" title="${esc(name)}">${esc(_initials(name))}</span>`;
}

function _initials(name) {
  // лише літери й цифри: «ГО «ІНТЕРНЬЮЗ-УКРАЇНА»» давало ініціали «Г«»
  const words = (name || "").split(/\s+/)
    .map((w) => (w.match(/[\p{L}\d]/u) || [""])[0]).filter(Boolean);
  return words.slice(0, 2).join("").toUpperCase();
}

/* Стек кружечків авторів кейсу: до трьох, далі «+2». Фото — з ростера. */
function creditAvatars(people) {
  const list = (people || []).slice(0, 3);
  if (!list.length) return "";
  const more = (people || []).length - list.length;
  return `<span class="imp-avs">${list.map((name) =>
    `<span class="imp-av">${avatar(name, personEntry(name) || {}, 28)}</span>`).join("")}${
    more > 0 ? `<span class="imp-av imp-av-more">+${more}</span>` : ""}</span>`;
}

/* Картка кейсу (Олег, 29.07: «некрасиво выглядит — давай фотку основної
   новини, окремо заголовок, кружечки авторів, кружечок донора, дату,
   кількість публікацій»). Фото — og:image новини-фіксації; зникне на
   сайті — картка тихо стає текстовою (onerror ховає блок). */
function impactCard(im) {
  const building = im.status === "building";
  const failed = im.status === "failed";
  const partners = (im.partners || "").split(" · ").filter(Boolean);
  return `
    <button class="imp-card" data-impact="${im.id}">
      ${im.image && !building && !failed ? `<span class="imp-img">
        <img src="${esc(im.image)}" alt="" loading="lazy"
          onerror="this.parentNode.style.display='none'"></span>` : ""}
      <span class="imp-body">
        <span class="imp-title">${esc(im.title || im.essence || im.source_url)}</span>
        <span class="imp-meta">${building ? "збирається…"
          : failed ? "не зібрався — відкрий і спробуй ще"
          : `${im.date ? esc(im.date) + " · " : ""}${im.articles} ${
              plural(im.articles, "матеріал", "матеріали", "матеріалів")}`}</span>
        ${building || failed ? "" : `<span class="imp-foot">
          ${creditAvatars(im.people)}
          <span class="imp-donors">${partners.slice(0, 2).map(donorChip).join("")}</span>
        </span>`}
      </span>
      ${building ? `<span class="im-spin imp-spin"></span>` : ""}
    </button>`;
}

/* Рік кейсу — з дати фіксації «дд.мм.рррр». Без дати (ще збирається/збився)
   року немає — такі картки видно за будь-якого фільтра: вони чекають дії. */
function impactYear(im) {
  return im.date ? im.date.slice(-4) : null;
}

function paintImpacts() {
  const body = $("im-body");
  if (!body) return;
  const list = STATE.impacts || [];
  if (!list.length) {
    body.innerHTML = `<div class="empty-hint">Поки порожньо.<br>
      Кинь «+» лінк новини, де видно результат («відремонтували», «скасували»,
      «повернули») — решту серії бот збере сам.</div>`;
    return;
  }
  // Фільтр по роках (Олег, 30.07: «поставил 2024 — видишь импакты за 2024»).
  // Роки — лише ті, за які кейси Є; один рік — фільтр не потрібен, не малюємо
  const years = [...new Set(list.map(impactYear).filter(Boolean))]
    .sort((a, b) => b.localeCompare(a));
  if (STATE.impactYear && !years.includes(STATE.impactYear)) STATE.impactYear = null;
  const chips = years.length > 1 ? `
    <div class="chips im-years">
      <button class="chip${!STATE.impactYear ? " on" : ""}" data-imyear="">Всі</button>
      ${years.map((y) => `<button class="chip${STATE.impactYear === y ? " on" : ""}"
        data-imyear="${y}">${y}</button>`).join("")}
    </div>` : "";
  const shown = STATE.impactYear
    ? list.filter((im) => !impactYear(im) || impactYear(im) === STATE.impactYear)
    : list;
  body.innerHTML = chips + (shown.length
    ? shown.map(impactCard).join("")
    : `<div class="empty-hint">За ${esc(STATE.impactYear)} рік кейсів немає.</div>`);
  body.querySelectorAll("[data-imyear]").forEach((b) => b.onclick = () => {
    STATE.impactYear = b.dataset.imyear || null;
    paintImpacts();
  });
  body.querySelectorAll("[data-impact]").forEach((b) => b.onclick = () => {
    STATE.currentImpact = +b.dataset.impact;
    nav("impact");
  });
  // хтось ще збирається — тихо оновлюємо список, поки не добудується
  if (list.some((im) => im.status === "building")) {
    clearTimeout(STATE.impactPollT);
    STATE.impactPollT = setTimeout(async () => {
      if (STATE.view !== "impacts") return;
      try {
        STATE.impacts = (await api("/api/impacts")).impacts || [];
        paintImpacts();
      } catch (e) {}
    }, 4000);
  }
}

function impactAddSheet() {
  openSheet(`
    <h2>Новий імпакт-кейс</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      Кинь лінк новини, де зафіксовано результат. Передісторію, серію і
      авторів бот збере сам — потім усе можна поправити.</p>
    <div class="f-label">Лінк на новину-фіксацію</div>
    <input id="im-url" inputmode="url" placeholder="https://nikvesti.com/news/…">
    <div class="f-label">Суть своїми словами (необовʼязково)</div>
    <input id="im-essence" maxlength="300"
      placeholder="після нас відремонтували, реакція на новину">
    <div class="sheet-actions">
      <button class="sbtn" id="im-cancel">Скасувати</button>
      <button class="sbtn primary" id="im-save">Зібрати кейс</button>
    </div>`);
  $("im-cancel").onclick = closeSheet;
  $("im-save").onclick = async () => {
    const url = $("im-url").value.trim();
    if (!url.includes("nikvesti.com")) { toast("Потрібен лінк nikvesti.com"); return; }
    $("im-save").disabled = true;
    try {
      await api("/api/impacts", { method: "POST", body: JSON.stringify(
        { url, essence: $("im-essence").value.trim() }) });
      closeSheet();
      haptic("success");
      renderImpacts();
    } catch (e) {
      $("im-save").disabled = false;
      toast(e.message);
    }
  };
}

async function renderImpact() {
  const id = STATE.currentImpact;
  // «Назад» — контекстний: з банера на головній вертає на головну, з «Моїх
  // імпактів» — у них, з архіву — в архів (маршрут знає backTarget)
  const backLabel = STATE.impactFrom === "myimpacts" ? "Мої імпакти"
    : STATE.impactFrom === "home" ? "Назад" : "Імпакт-архів";
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} ${backLabel}</button>
    <div id="imd-body">${skeleton("rows", 4)}</div>`;
  let im;
  try {
    im = await api(`/api/impacts/${id}`);
  } catch (e) {
    const b = $("imd-body");
    if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "impact" || STATE.currentImpact !== id) return;
  paintImpact(im);
}

function paintImpact(im) {
  const body = $("imd-body");
  if (!body) return;
  if (im.status === "building") {
    body.innerHTML = `<div class="h-big" style="font-size:20px">${esc(im.essence || "Кейс")}</div>
      <div class="empty-hint">Збираю серію: беклінки, нора, автори, донори…<br>
        Це пів хвилини — можна вийти, кейс добудується сам.</div>`;
    clearTimeout(STATE.impactPollT);
    STATE.impactPollT = setTimeout(async () => {
      if (STATE.view !== "impact" || STATE.currentImpact !== im.id) return;
      try { paintImpact(await api(`/api/impacts/${im.id}`)); } catch (e) {}
    }, 4000);
    return;
  }
  if (im.status === "failed") {
    body.innerHTML = `
      <div class="empty-hint">Кейс не зібрався:<br>${esc(im.error || "невідома причина")}</div>
      <button class="cta" id="im-retry">Спробувати ще</button>
      <button class="link-btn" id="im-del">${icon("trash")} Видалити чернетку</button>`;
    $("im-retry").onclick = async () => {
      try {
        await api(`/api/impacts/${im.id}/retry`, { method: "POST" });
        renderImpact();
      } catch (e) { toast(e.message); }
    };
    $("im-del").onclick = () => impactDelete(im.id);
    return;
  }

  const key = im.articles.find((a) => a.is_key);
  const donor = key && key.partner_name;
  // Читання без редагування: журналістка дивиться свій кейс, але чернетку,
  // медальки і серію правит лише менеджер зі свого входу
  const ro = STATE.impactFrom === "myimpacts" || STATE.impactFrom === "home"
    || !STATE.me.manager;
  body.innerHTML = `
    ${im.image ? `<div class="imp-hero"><img src="${esc(im.image)}" alt=""
      onerror="this.parentNode.style.display='none'"></div>` : ""}
    <div class="head-row">
      <div class="h-big" style="font-size:20px">${esc(im.title)}</div>
      ${ro ? "" : `<button class="icon-btn" id="imd-edit" aria-label="Редагувати">${icon("edit")}</button>`}
    </div>
    <div class="soft-card" style="margin-top:12px">
      <p class="im-p${ro ? "" : " im-editable"}" ${ro ? "" : 'data-imedit="ime-what"'}>${esc(im.what_happened)}</p>
      <div class="sc-t" style="margin-top:12px">Значення та вплив</div>
      <p class="im-p${ro ? "" : " im-editable"}" ${ro ? "" : 'data-imedit="ime-sig"'}>${esc(im.significance)}</p>
      ${ro ? "" : `<div class="mr-hint" style="margin-top:10px">тап по тексту — виправити:
        AI міг згалюцинувати слово, останнє слово за людиною</div>`}
      ${donor ? `<div class="im-donor">Ключовий матеріал — у межах проєкту
        «${esc(key.project_name)}» за підтримки <b>${esc(donor)}</b>: цим кейсом
        можна порадувати донора.</div>` : ""}
    </div>
    <div class="dept-title">Серія · ${im.articles.length}</div>
    <div class="soft-card itl">${im.articles.map((a) => impactTimelineItem(a, ro)).join("")}</div>
    ${ro ? "" : `<div class="mr-hint">тап по матеріалу — відкрити, зробити ключовим або прибрати з серії</div>
    <button class="link-btn" id="imd-add-art">${icon("plus")} Додати матеріал за лінком</button>`}
    <div class="dept-title">${ro ? "Команда кейсу" : "Кому записати"}</div>
    <div class="soft-card" id="imd-credits">
      ${im.credits.map((c) => `
        <div class="td-row">
          <span class="td-t"><span class="td-text">${esc(c.person)}</span>
            ${c.note ? `<span class="td-age">${esc(c.note)}</span>` : ""}</span>
          ${ro ? "" : `<button class="td-act" data-imcredit-del="${c.id}" aria-label="Зняти">${icon("x")}</button>`}
        </div>`).join("") || `<div class="empty-hint">Поки нікого.</div>`}
      ${ro ? "" : `<form class="td-add" id="imd-credit-form" style="margin:10px 0 2px">
        <input id="imd-credit-name" maxlength="120" placeholder="Додати людину…">
        <button class="td-plus" type="submit" aria-label="Додати">${icon("plus")}</button>
      </form>`}
    </div>
    ${ro ? "" : `<button class="cta" id="imd-send">Надіслати файлом у приват</button>
    <button class="link-btn" id="imd-rebuild" style="margin-top:4px">
      ${icon("chevron-right")} Перезібрати кейс заново</button>
    <button class="link-btn" id="imd-del" style="margin-top:4px;color:var(--red)">${icon("trash")} Видалити кейс</button>`}`;
  if (!ro) wireImpactDetail(im);
}

/* Матеріал серії — пункт ВЕРТИКАЛЬНОГО таймлайна (Олег, 30.07: «не понятно,
   что это серия — полоска зліва, на ній точки-дати, заголовок виділити,
   донора виділити, автора не скопом»). Зліва рейка з точкою (зелена повна —
   ключовий, контурна — решта), праворуч: дата, заголовок, бейджі ролі,
   внизу автор із кружечком фото і донор кольоровою пігулкою.

   У читанні пункт — лінк на матеріал; у редакторському режимі відкриває
   шторку з підписаними діями (іконки-загадки провалились першого ж дня). */
function impactTimelineItem(a, ro) {
  const badges = [
    a.is_key ? `<span class="itl-badge key">★ ключовий</span>` : "",
    a.role === "fixer" ? `<span class="itl-badge">фіксація результату</span>` : "",
  ].filter(Boolean).join("");
  const inner = `
    <span class="itl-rail"><i class="itl-dot${a.is_key ? " key" : ""}"></i></span>
    <span class="itl-body">
      <span class="itl-date">${esc(a.date)}</span>
      <span class="itl-title">${esc(a.title || a.url)}</span>
      ${badges ? `<span class="itl-badges">${badges}</span>` : ""}
      ${a.authors || a.partner_name ? `<span class="itl-foot">
        ${a.authors ? `<span class="itl-author">
          <span class="imp-av">${avatar(a.authors, personEntry(a.authors) || {}, 20)}</span>
          ${esc(a.authors)}</span>` : ""}
        ${a.partner_name ? `<span class="itl-donor">${donorChip(a.partner_name)}
          <span class="itl-donor-name">${esc(a.partner_name)}</span></span>` : ""}
      </span>` : ""}
    </span>`;
  return ro
    ? `<a class="itl-item" href="${esc(a.url)}" data-ext="${esc(a.url)}">${inner}</a>`
    : `<button class="itl-item" data-imart="${a.id}">${inner}
        ${icon("chevron-right", "ic chev itl-chev")}</button>`;
}

/* Дії над матеріалом серії — шторка зі словами замість іконок-загадок. */
function impactArticleSheet(im, a, patch) {
  const meta = [a.date, a.authors, a.partner_name].filter(Boolean).join(" · ");
  openSheet(`
    <h2>${esc(a.title || a.url)}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">${esc(meta)}${
      a.role === "fixer" ? " · новина-фіксація" : ""}${
      a.is_key ? " · ключовий" : ""}</p>
    <button class="link-btn" id="ia-open">${icon("link")} Відкрити матеріал</button>
    ${a.is_key ? "" : `<button class="link-btn" id="ia-key">
      ${icon("award")} Зробити ключовим — він важить найбільше</button>`}
    ${a.role === "fixer" ? "" : `<button class="link-btn" id="ia-drop" style="color:var(--red)">
      ${icon("trash")} Прибрати з серії</button>`}
    <div class="sheet-actions">
      <button class="sbtn" id="ia-cancel">Закрити</button>
    </div>`);
  $("ia-cancel").onclick = closeSheet;
  $("ia-open").onclick = () => {
    try { tg.openLink(a.url); } catch (e) { window.open(a.url, "_blank"); }
  };
  const key = $("ia-key");
  if (key) key.onclick = () => { closeSheet(); patch({ action: "set_key", row_id: a.id }); };
  const drop = $("ia-drop");
  if (drop) drop.onclick = async () => {
    // Підтвердження обовʼязкове: прибраний матеріал назад не додається
    // (лише повним перезбором), тож мовчазне видалення — втрата роботи
    if (!await confirmAction(`Прибрати «${a.title || a.url}» з серії?`)) return;
    closeSheet();
    patch({ action: "remove_article", row_id: a.id });
  };
}

function wireImpactDetail(im) {
  const patch = async (payload) => {
    try {
      paintImpact(await api(`/api/impacts/${im.id}`, {
        method: "PATCH", body: JSON.stringify(payload) }));
    } catch (e) { toast(e.message); }
  };
  const body = $("imd-body");
  body.querySelectorAll("[data-imart]").forEach((b) => b.onclick = () => {
    const a = im.articles.find((x) => x.id === +b.dataset.imart);
    if (a) impactArticleSheet(im, a, patch);
  });
  body.querySelectorAll("[data-imcredit-del]").forEach((b) => b.onclick = async () => {
    const c = im.credits.find((x) => x.id === +b.dataset.imcreditDel);
    if (!await confirmAction(`Зняти медальку${c ? " з " + c.person : ""}?`)) return;
    patch({ action: "remove_credit", credit_id: +b.dataset.imcreditDel });
  });
  $("imd-credit-form").onsubmit = (e) => {
    e.preventDefault();
    const person = $("imd-credit-name").value.trim();
    if (person) patch({ action: "add_credit", person });
  };
  // Збір не всесильний: стаття поза норою чи без беклінка лишається
  // невидимою (реальний кейс 30.07 — стаття про автошколу, і з нею донор
  // Sigrid Rausing Trust). Людина знає свою серію — хай додає лінком.
  $("imd-add-art").onclick = () => {
    openSheet(`
      <h2>Додати матеріал</h2>
      <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
        Лінк на матеріал nikvesti.com — автора і проєкт/донора бот
        підтягне сам.</p>
      <input id="ia-url" inputmode="url" placeholder="https://nikvesti.com/…">
      <div class="sheet-actions">
        <button class="sbtn" id="ia-add-cancel">Скасувати</button>
        <button class="sbtn primary" id="ia-add-save">Додати</button>
      </div>`);
    $("ia-add-cancel").onclick = closeSheet;
    $("ia-add-save").onclick = async () => {
      const url = $("ia-url").value.trim();
      if (!url.includes("nikvesti.com")) { toast("Потрібен лінк nikvesti.com"); return; }
      $("ia-add-save").disabled = true;
      try {
        const fresh = await api(`/api/impacts/${im.id}`, { method: "PATCH",
          body: JSON.stringify({ action: "add_article", url }) });
        closeSheet();
        haptic("success");
        paintImpact(fresh);
      } catch (e) {
        $("ia-add-save").disabled = false;
        toast(e.message);
      }
    };
  };
  $("imd-edit").onclick = () => impactEditSheet(im);
  // Перезбір готового кейсу: рятує, коли з серії прибрали зайве (назад
  // матеріал інакше не повернути) або сюжет доріс новими текстами. Раніше
  // «спробувати ще» жило тільки на збитих кейсах — і Олег, шукаючи його на
  // готовому, видалив кейс цілком (29.07). Перезбір перезаписує ручні правки
  // тексту й серії, тому питаємо прямо.
  $("imd-rebuild").onclick = async () => {
    if (!await confirmAction(
      "Перезібрати кейс з нуля? Серію збере заново, ручні правки тексту буде перезаписано.")) return;
    try {
      await api(`/api/impacts/${im.id}/retry`, { method: "POST" });
      renderImpact();
    } catch (e) { toast(e.message); }
  };
  body.querySelectorAll("[data-imedit]").forEach((el) => el.onclick = () =>
    impactEditSheet(im, el.dataset.imedit));
  $("imd-del").onclick = () => impactDelete(im.id);
  $("imd-send").onclick = async () => {
    $("imd-send").disabled = true;
    try {
      await api(`/api/impacts/${im.id}/send`, { method: "POST" });
      haptic("success");
      toast("Полетіло в приват із Лисом 🦊");
    } catch (e) { toast(e.message); }
    $("imd-send").disabled = false;
  };
}

/* focusId — з якого поля почати: тап по абзацу «що сталось» не має змушувати
   продиратись повз заголовок, щоб виправити одне слово саме там. */
function impactEditSheet(im, focusId) {
  openSheet(`
    <h2>Редагувати кейс</h2>
    <div class="f-label">Заголовок</div>
    <input id="ime-title" maxlength="300" value="${esc(im.title || "")}">
    <div class="f-label">Що сталось</div>
    <textarea id="ime-what" rows="5">${esc(im.what_happened)}</textarea>
    <div class="f-label">Значення та вплив</div>
    <textarea id="ime-sig" rows="5">${esc(im.significance)}</textarea>
    <div class="sheet-actions">
      <button class="sbtn" id="ime-cancel">Скасувати</button>
      <button class="sbtn primary" id="ime-save">Зберегти</button>
    </div>`);
  if (focusId && $(focusId)) $(focusId).focus();
  $("ime-cancel").onclick = closeSheet;
  $("ime-save").onclick = async () => {
    try {
      const fresh = await api(`/api/impacts/${im.id}`, { method: "PATCH",
        body: JSON.stringify({ title: $("ime-title").value,
          what_happened: $("ime-what").value, significance: $("ime-sig").value }) });
      closeSheet();
      paintImpact(fresh);
    } catch (e) { toast(e.message); }
  };
}

async function impactDelete(id) {
  if (!await confirmAction("Видалити кейс?")) return;
  try {
    await api(`/api/impacts/${id}`, { method: "DELETE" });
    nav("impacts");
  } catch (e) { toast(e.message); }
}

/* ---------- Мої імпакти (журналістський бік архіву) ----------

   Олег, 29.07: «зарахував імпакт Аліні — їй має прийти це у події, і має
   зʼявитись блок про свої імпакти: натиснула і переглянула». Подія летить із
   impact_archive (kind=impact_credit), а тут — двері на головній (лише коли
   медальки Є: двері без числа не мають сенсу) і список кейсів, у яких людина
   має медальку. Кейс відкривається ТОЙ САМИЙ, що в менеджера, але читанням:
   чужі оцінки внесків і чернетки правити з цього боку не можна. */
async function renderMyImpacts() {
  const me = viewPerson();
  const q = me.preview ? `?person=${encodeURIComponent(me.name)}` : "";
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="h-big">${me.preview ? esc(me.first_name) + " · імпакти" : "Мої імпакти"}</div>
    <div class="h-sub">кейси, де є твій внесок — те, що змінилось завдяки текстам</div>
    <div id="mi-body">${skeleton("rows", 2)}</div>`;
  let data;
  try {
    data = await api("/api/impacts/mine" + q);
  } catch (e) {
    const b = $("mi-body");
    if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "myimpacts") return;
  const list = data.impacts || [];
  const body = $("mi-body");
  if (!body) return;
  body.innerHTML = list.length ? list.map((im) => `
    <button class="imp-card" data-impact="${im.id}">
      ${im.image ? `<span class="imp-img"><img src="${esc(im.image)}" alt=""
        loading="lazy" onerror="this.parentNode.style.display='none'"></span>` : ""}
      <span class="imp-body">
        <span class="imp-title">${esc(im.title)}</span>
        <span class="imp-meta">${im.date ? esc(im.date) + " · " : ""}${
          im.articles} ${plural(im.articles, "матеріал", "матеріали", "матеріалів")}</span>
        ${im.note ? `<span class="imp-note">${icon("award")} ${esc(im.note)}</span>` : ""}
      </span>
    </button>`).join("")
    : `<div class="empty-hint">Поки жодного кейсу.<br>
        Медальки зʼявляються, коли редакція фіксує вплив твоїх текстів.</div>`;
  body.querySelectorAll("[data-impact]").forEach((b) => b.onclick = () => {
    STATE.currentImpact = +b.dataset.impact;
    nav("impact");
  });
}

/* Банер над KPI: імпакт ПОТОЧНОГО місяця за участі людини (Олег, 30.07:
   «нехай і у верхній панелі буде, якщо в поточному періоді відбувся»).
   Це найкраща новина, яку екран може принести, — вона важливіша за цифри
   норми, тому стоїть над ними. Тап веде просто в кейс. Один банер, не стек:
   якщо кейсів кілька — найсвіжіший, решта за дверима «Мої імпакти». */
function impactBannerHtml(me) {
  const now = new Date();
  const cur = myImpactsFresh(me).find((im) => {
    if (!im.date) return false;
    const [, mo, y] = im.date.split(".");
    return +mo === now.getMonth() + 1 && +y === now.getFullYear();
  });
  if (!cur) return "";
  return `
    <button class="imp-banner" data-impbanner="${cur.id}">
      <span class="st-mark done">${icon("award")}</span>
      <span class="pk-txt">
        <span class="pk-name">Імпакт за твоєї участі</span>
        <span class="pk-meta">${esc(cur.title)}</span>
      </span>
      ${icon("chevron-right", "ic chev")}
    </button>`;
}

/* Двері «Імпакти» на головній журналістки — лише коли медальки Є. Тихо, раз
   на сеанс, як стрічка подій: нуль медальок → нуль дверей, а не двері з
   нулем. У перегляді чужими очима вантажимо імпакти ОБРАНОЇ людини (Олег,
   30.07: «где на этом экране импакты журналиста?» — прев'ю їх не вантажило
   взагалі). Кеш підписаний, чиї це імпакти, — зміна людини перечитує. */
function myImpactsFresh(me) {
  const key = me.preview ? me.name : "@me";
  return STATE.myImpactsFor === key ? (STATE.myImpacts || []) : [];
}

async function loadMyImpacts() {
  const me = viewPerson();
  const key = me.preview ? me.name : "@me";
  if (STATE.myImpactsFor === key && STATE.myImpacts) return;
  try {
    const q = me.preview ? `?person=${encodeURIComponent(me.name)}` : "";
    STATE.myImpacts = (await api("/api/impacts/mine" + q)).impacts || [];
    STATE.myImpactsFor = key;
  } catch (e) { return; }
  if ((STATE.view === "home" || STATE.view === "preview")
      && STATE.myImpacts.length) renderJournalist();
}

/* ---------- Хто коли відсутній ----------

   Олег, 29.07: «утиліта — дивитись таймлайн відпусток, лікарняних і іншого, у
   кого коли відпустки, і щоб на ній засічками були дедлайни тасків. Адже таски
   не дивляться на відпустку і не перераховуються, і люди, йдучи у відпустку,
   мають думати, що робити з їхніми тасками».

   Ключове тут не «подивитись календар», а саме ЗІТКНЕННЯ: KPI відпустка
   знижує сама (absence_factor), а creative task — ні, він просто мовчки
   згорить. Тому екран відкривається списком колізій, а шкала під ним — це вже
   доказ, а не головна дія.

   Ні рядка нового на сервері: відсутності віддає той самий /api/absences, що
   й картка людини, запити — /api/absences/requests, а дедлайни завдань уже
   лежать у STATE.tasks із bootstrap. */

const AWAY_KIND_CLS = { vacation: "k-vac", sick: "k-sick", trip: "k-trip" };

function awayItems(absences, requests) {
  // Показуємо те, що ще не закінчилось три тижні тому: історія відпусток за
  // рік розтягнула б шкалу так, що найближчий місяць став би непомітним
  const from = todayISO(-21);
  return [
    ...absences.filter((a) => a.end >= from).map((a) => ({ ...a, pending: false })),
    ...requests.filter((r) => r.end >= from).map((r) => ({ ...r, pending: true })),
  ].sort((a, b) => (a.start < b.start ? -1 : 1));
}

/* Завдання, дедлайн яких припадає на відсутність. Саме через них уся утиліта
   й існує: людина у відпустці, а таск горить. Беремо лише відкриті — знятий
   чи виконаний нікого вже не турбує. */
function awayClashes(items, tasks) {
  const out = [];
  items.forEach((it) => {
    tasks.forEach((t) => {
      if (t.status !== "open" || !t.deadline) return;
      if (t.person !== it.person) return;
      if (t.deadline < it.start || t.deadline > it.end) return;
      out.push({ item: it, task: t });
    });
  });
  return out;
}

async function renderAway() {
  const head = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="h-big">Хто коли відсутній</div>
    <div class="h-sub">відпустки, лікарняні, відрядження · засічки — дедлайни завдань</div>`;
  $("content").innerHTML = head + `<div id="aw-body">${skeleton("rows", 3)}</div>`;
  let absences = [], requests = [];
  try {
    const [a, r] = await Promise.all([
      api("/api/absences"),
      api("/api/absences/requests").catch(() => ({ requests: [] })),
    ]);
    absences = a.absences || [];
    requests = (r.requests || []).filter((x) => x.status === "pending");
  } catch (e) {
    const box = $("aw-body");
    if (box) box.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
    return;
  }
  if (STATE.view !== "away") return;
  const body = $("aw-body");
  if (!body) return;

  const items = awayItems(absences, requests);
  if (!items.length) {
    body.innerHTML = `<div class="empty-hint">Найближчим часом усі на місці.<br>
      Відпустку заводять на екрані людини, запит від журналістки приходить у «Сповіщення».</div>`;
    return;
  }
  const clashes = awayClashes(items, STATE.tasks);
  body.innerHTML = awayClashHtml(clashes) + awayTimelineHtml(items);
  wireAway(items, clashes);
}

function awayClashHtml(clashes) {
  if (!clashes.length) {
    return `<div class="aw-ok">${icon("check")} Жоден відкритий дедлайн не припадає на відсутність.</div>`;
  }
  // Два рядки, а не один абзац: «Даріна · 31.07 3 матеріали · Новини з
  // тендерів · Internews · відпустка» в одну стрічку не читається взагалі.
  //
  // І групуємо по відсутності: у Олега на екрані було девʼять зіткнень
  // поспіль, і кожне повторювало «Даріна … відпустка». Хто і коли — це
  // властивість ВІДПУСТКИ, а не кожного завдання; у рядку лишається те, чим
  // вони різняться — саме завдання і донор.
  const groups = [];
  clashes.forEach((c, i) => {
    const key = `${c.item.person}|${c.item.pending ? "r" : "a"}${c.item.id}`;
    let g = groups.find((x) => x.key === key);
    if (!g) groups.push(g = { key, item: c.item, list: [] });
    g.list.push({ c, i });
  });
  const rows = groups.map((g) => `
    <div class="dept-title">${esc(g.item.person.split(" ")[0])} · ${
      esc(g.item.title.toLowerCase())} ${esc(shortDate(g.item.start))}–${
      esc(shortDate(g.item.end))} · ${g.list.length}</div>
    <div class="soft-card">${g.list.map(({ c, i }) => {
      const { partner, projName } = taskProject(c.task);
      const where = [shortDate(c.task.deadline), partner || projName]
        .filter(Boolean).join(" · ");
      return `
      <button class="aw-row" data-awclash="${i}">
        <span class="tl-mark late static">${icon("file-text")}</span>
        <span class="pub-t">
          <span class="pub-title">${esc(taskLine(c.task))}</span>
          <span class="pub-tags">${esc(where)}</span>
        </span>
        ${icon("chevron-right", "ic chev")}
      </button>`;
    }).join("")}</div>`).join("");
  return `
    <div class="aw-warn">
      <div class="aw-warn-t">${plural(clashes.length, "Дедлайн припадає", "Дедлайни припадають",
        "Дедлайнів припадає")} на відсутність — ${clashes.length}</div>
      <div class="aw-warn-s">KPI відпустка знижує сама, а завдання — ні: його треба
        або продовжити, або зняти, або передати.</div>
    </div>
    ${rows}`;
}

function awayTimelineHtml(items) {
  const today = todayISO();
  const byPerson = {};
  items.forEach((it) => (byPerson[it.person] = byPerson[it.person] || []).push(it));
  const people = Object.keys(byPerson).sort();

  const day = 86400000;
  const stamp = (iso) => Date.parse(iso + "T12:00:00");
  let min = Math.min(stamp(today), ...items.map((i) => stamp(i.start))) - 3 * day;
  let max = Math.max(stamp(today) + 30 * day, ...items.map((i) => stamp(i.end))) + 3 * day;
  const days = (max - min) / day;
  const width = Math.max(660, Math.min(2600, Math.round(days * 11)));
  const x = (ms) => ((ms - min) / (max - min)) * width;

  // Поділки по тижнях, підпис — раз на стільки, щоб не злипались
  let ticks = "";
  const d = new Date(min);
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7));   // найближчий понеділок
  let ti = 0;
  const step = Math.max(1, Math.ceil(60 / (width / days * 7)));
  for (; d.getTime() < max; d.setDate(d.getDate() + 7), ti++) {
    const lbl = ti % step === 0
      ? `<span>${d.getDate()}.${String(d.getMonth() + 1).padStart(2, "0")}</span>` : "";
    ticks += `<div class="tl-tick" style="left:${x(d.getTime()).toFixed(1)}px">${lbl}</div>`;
  }

  const rows = people.map((person) => {
    const bars = byPerson[person].map((it) => {
      const left = x(stamp(it.start));
      const w = Math.max(18, x(stamp(it.end)) - left + 11);
      const cls = `${AWAY_KIND_CLS[it.kind] || "k-vac"}${it.pending ? " pending" : ""}`;
      return `<button class="aw-bar ${cls}" data-awbar="${it.pending ? "r" : "a"}:${it.id}"
        style="left:${left.toFixed(1)}px;width:${w.toFixed(1)}px"
        title="${esc(it.title)} ${esc(shortDate(it.start))}–${esc(shortDate(it.end))}">
        <span class="tl-inner"><span class="tl-lbl">${esc(it.title)}${
          it.pending ? " · просить" : ""}</span></span>
      </button>`;
    }).join("");
    // Засічки — дедлайни ВІДКРИТИХ завдань цієї людини у вікні шкали
    const marks = STATE.tasks.filter((t) => t.person === person && t.status === "open"
        && t.deadline && stamp(t.deadline) >= min && stamp(t.deadline) <= max)
      .map((t) => {
        const inside = byPerson[person].some(
          (it) => t.deadline >= it.start && t.deadline <= it.end);
        return `<button class="tl-mark ${inside ? "late" : "todo"}" data-awtask="${t.id}"
          style="left:${x(stamp(t.deadline)).toFixed(1)}px"
          title="${esc(taskLine(t, { donor: true }))} · ${esc(shortDate(t.deadline))}">
          ${icon("file-text")}</button>`;
      }).join("");
    return `<div class="tl-row">${bars}${marks}</div>`;
  }).join("");

  const names = people.map((p) => {
    const entry = personEntry(p) || {};
    return `<div class="aw-name">${avatar(p, entry, 26)}<span>${esc(p.split(" ")[0])}</span></div>`;
  }).join("");

  const nowX = x(stamp(today));
  return `
    <div class="aw-grid">
      <div class="aw-names">${names}</div>
      <div class="tl-scroll" id="aw-scroll" data-nowx="${nowX.toFixed(0)}">
        <div class="tl-canvas" style="width:${width}px;height:${people.length * 46 + 34}px">
          ${ticks}
          <div class="tl-now" style="left:${nowX.toFixed(1)}px"><span>сьогодні</span></div>
          <div class="tl-rows">${rows}</div>
        </div>
      </div>
    </div>
    <div class="tl-legend">
      <span class="tl-lg"><i class="aw-chip k-vac"></i>відпустка</span>
      <span class="tl-lg"><i class="aw-chip k-sick"></i>лікарняна</span>
      <span class="tl-lg"><i class="aw-chip k-trip"></i>відрядження</span>
      <span class="tl-lg"><i class="aw-chip k-vac pending"></i>просить, не погоджено</span>
      <span class="tl-lg"><i class="tl-mark late static">${icon("file-text")}</i>дедлайн у відсутності</span>
    </div>
    <div class="tl-note">Тап по смузі — картка людини, по засічці — завдання. Скрольте вбік.</div>`;
}

function wireAway(items, clashes) {
  const box = $("aw-body");
  box.querySelectorAll("[data-awclash]").forEach((b) => b.onclick = () => {
    const c = clashes[+b.dataset.awclash];
    if (c) taskSheet(c.task);
  });
  box.querySelectorAll("[data-awtask]").forEach((b) => b.onclick = () => {
    const t = STATE.tasks.find((x) => x.id === +b.dataset.awtask);
    if (t) taskSheet(t);
  });
  box.querySelectorAll("[data-awbar]").forEach((b) => b.onclick = () => {
    const [kind, id] = b.dataset.awbar.split(":");
    const it = items.find((x) => x.id === +id && x.pending === (kind === "r"));
    if (it) nav("person", it.person);
  });
  // Скролимо до «сьогодні»: історія ліворуч потрібна рідше, ніж найближчі тижні
  const sc = $("aw-scroll");
  if (sc) sc.scrollLeft = Math.max(0, +sc.dataset.nowx - 90);
}

/* ---------- Прострочені завдання ----------
   Олег, 29.07: «якщо дедлайн минув, треба щоб Каті приходила картка з
   пропозицією продовжити дедлайн або зняти задачу».

   Список рахуємо з того, що вже є в STATE (менеджер бачить усі таски) — це
   нуль нових запитів. Подія в стрічку і лічильник приходять із сервера
   (team_tasks.notify_overdue), щоб штовхнути Катю навіть тоді, коли апку не
   відкривали.

   Дії — «Зарахувати скільки є» і «Продовжити», БЕЗ «Зняти» (Олег, 01.08):
   на «9/10» зняття виглядало як списання недостачі, а насправді дропало весь
   таск — дев'ять зарахованих переставали рахуватись виконанням. Тепер:
   є зараховане → закрити як виконане з тим, що є (руками закритий таск
   auto-reopen не чіпає); зарахованого нуль → те саме, що зняти, і втрачати
   нічого. Справжнє «Зняти» живе в картці таска в «Завданнях». */

function overdueTasks() {
  const today = todayISO();
  return (STATE.tasks || [])
    .filter((t) => t.status === "open" && t.deadline && t.deadline < today)
    .sort((a, b) => a.deadline.localeCompare(b.deadline));
}

function daysSince(iso) {
  const ms = Date.parse(todayISO()) - Date.parse(iso);
  return Math.max(0, Math.round(ms / 86400000));
}

/* Заголовок картки — ЩО писати першим (Олег, 01.08): «Новини з тендерів ×
   3 матеріали», а не кількість поперед тематики; донор — окремим рядком з
   міні-лого, як у стрічці подій. */
function overdueTitle(t) {
  const what = t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1);
  const topic = t.theme_name || taskProject(t).projName || "позапроєктне";
  return t.qty > 1 ? `${topic} × ${what}` : `${topic} · ${what}`;
}

function overdueCard(t) {
  const d = daysSince(t.deadline);
  const word = d === 1 ? "день" : (d >= 2 && d <= 4 ? "дні" : "днів");
  const done = t.done_count || 0;
  const { partner } = taskProject(t);
  return `
    <div class="al-card">
      <div class="al-head">
        ${avatar(t.person, personEntry(t.person), 38)}
        <div class="al-h-txt">
          <span class="al-who">${esc(overdueTitle(t))}</span>
          ${partner ? `<span class="nt-by od-donor">${donorChip(partner)}<span class="nt-donor">${esc(partner)}</span></span>` : ""}
          <div class="al-date">${esc(t.person)} · строк був ${esc(shortDate(t.deadline))},
            ${d} ${word} тому</div>
        </div>
        ${progressHtml(t)}
      </div>
      ${t.note ? `<div class="al-why">${esc(t.note)}</div>` : ""}
      <div class="al-actions">
        <button class="sbtn ${done > 0 ? "good" : "danger"}" data-oclose="${t.id}">
          ${done > 0
            ? `Закрити завдання<span class="sbtn-sub">зараховано ${done} з ${t.qty}</span>`
            : `Зняти завдання<span class="sbtn-sub">нічого не зараховано</span>`}</button>
        <button class="sbtn primary" data-oext="${t.id}">Новий дедлайн</button>
      </div>
    </div>`;
}

/* Продовження строку. Швидкі варіанти закривають майже всі випадки, але дата
   лишається: «до кінця тижня» і «до кінця місяця» — не одне й те саме, коли
   місяць закінчується в середу. */
function extendSheet(t) {
  const plus = (days) => {
    const d = new Date();
    d.setDate(d.getDate() + days);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  const endOfMonth = () => {
    const d = new Date();
    const e = new Date(d.getFullYear(), d.getMonth() + 1, 0);
    return `${e.getFullYear()}-${String(e.getMonth() + 1).padStart(2, "0")}-${String(e.getDate()).padStart(2, "0")}`;
  };
  openSheet(`
    <h2>Продовжити строк</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(t.person)} · ${esc(taskLine(t, { donor: true }))}</p>
    <div class="chips">
      <button class="chip" data-od="${plus(3)}">+3 дні</button>
      <button class="chip" data-od="${plus(7)}">+тиждень</button>
      <button class="chip" data-od="${endOfMonth()}">до кінця місяця</button>
    </div>
    <div class="f-label">Або конкретна дата</div>
    <input id="od-date" type="date" min="${todayISO()}" value="${esc(t.deadline)}">
    <div class="sheet-actions">
      <button class="sbtn" id="od-cancel">Скасувати</button>
      <button class="sbtn primary" id="od-save">Зберегти</button>
    </div>`);
  const save = async (deadline) => {
    try {
      const res = await api(`/api/tasks/${t.id}`, {
        method: "PATCH", body: JSON.stringify({ deadline }),
      });
      patchTask(res.task);
      closeSheet();
      haptic("success");
      toast(`Строк — до ${shortDate(deadline)}`);
      render();
    } catch (e) { toast(e.message); }
  };
  $("sheet").querySelectorAll("[data-od]").forEach((b) =>
    b.onclick = () => save(b.dataset.od));
  $("od-cancel").onclick = closeSheet;
  $("od-save").onclick = () => {
    const v = $("od-date").value;
    if (v) save(v);
  };
}

/* Картка погодження відпустки. Дві дії й нічого більше: або людина йде, або
   ні. Погодження створює відсутність, і цілі KPI перераховуються самі. */
function absenceRequestCard(r) {
  const range = r.start === r.end ? shortDate(r.start)
    : `${shortDate(r.start)} — ${shortDate(r.end)}`;
  return `
    <div class="al-card">
      <div class="al-head">
        ${avatar(r.person, personEntry(r.person), 38)}
        <div class="al-h-txt">
          <span class="al-who">${esc(r.person)} просить ${esc(r.title)}</span>
          <div class="al-date">${esc(range)}</div>
        </div>
      </div>
      ${r.note ? `<div class="al-why">${esc(r.note)}</div>` : ""}
      <div class="al-actions">
        <button class="sbtn danger" data-arno="${r.id}">Відхилити</button>
        <button class="sbtn primary" data-aryes="${r.id}">Погодити</button>
      </div>
    </div>`;
}

function paintAlerts() {
  const list = STATE.pending || [];
  const feed = STATE.notifs || [];
  const box = $("alerts-body");
  if (!box) return;
  const late = overdueTasks();
  const asks = STATE.absenceRequests || [];
  box.innerHTML = `
    ${asks.length
      ? `<div class="dept-title">Просять відсутність · ${asks.length}</div>
         ${asks.map(absenceRequestCard).join("")}`
      : ""}
    ${late.length
      ? `<div class="dept-title">Строк минув · ${late.length}</div>
         ${late.map(overdueCard).join("")}`
      : ""}
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
  const decideAbsence = async (id, approve) => {
    try {
      await api(`/api/absences/requests/${id}/decide`, {
        method: "POST", body: JSON.stringify({ approve }),
      });
      STATE.absenceRequests = (STATE.absenceRequests || [])
        .filter((x) => x.id !== id);
      haptic("success");
      // Погоджена відпустка змінює цілі — зведення KPI протухло
      if (approve) { STATE.kpi = null; STATE.dash.data = null; }
      paintAlerts();
      syncAlertsBadge();
    } catch (e) { toast(e.message); }
  };
  box.querySelectorAll("[data-aryes]").forEach((b) =>
    b.onclick = () => decideAbsence(+b.dataset.aryes, true));
  box.querySelectorAll("[data-arno]").forEach((b) => b.onclick = async () => {
    const r = (STATE.absenceRequests || []).find((x) => x.id === +b.dataset.arno);
    if (!r) return;
    if (!(await confirmAction(`Відхилити ${r.title} для ${r.person}?`))) return;
    decideAbsence(r.id, false);
  });
  box.querySelectorAll("[data-oext]").forEach((b) => b.onclick = () => {
    const t = (STATE.tasks || []).find((x) => x.id === +b.dataset.oext);
    if (t) extendSheet(t);
  });
  // «Зарахувати скільки є»: із зарахованим — закрити як виконане (публікації
  // лишаються прикріпленими, «9/10» видно в закритому); без жодного
  // зарахованого закривати «виконаним» було б брехнею — таск знімається,
  // і це рівноцінно, бо втрачати нічого.
  box.querySelectorAll("[data-oclose]").forEach((b) => b.onclick = async () => {
    const t = (STATE.tasks || []).find((x) => x.id === +b.dataset.oclose);
    if (!t) return;
    const done = t.done_count || 0;
    const status = done > 0 ? "done" : "dropped";
    const q = done > 0
      ? `Закрити завдання «${taskLine(t)}» як виконане з тим, що є (${done}/${t.qty})?`
      : `Зарахованого нічого немає — зняти завдання «${taskLine(t)}» з ${t.person}?`;
    if (!(await confirmAction(q))) return;
    try {
      const res = await api(`/api/tasks/${t.id}`, {
        method: "PATCH", body: JSON.stringify({ status }),
      });
      patchTask(res.task);
      haptic("success");
      toast(done > 0 ? `Закрито · зараховано ${done}/${t.qty}` : "Завдання знято");
      render();
    } catch (e) { toast(e.message); }
  });
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

/* Нижнє меню журналістки (Олег, 29.07: «може, треба таки меню журналісту —
   мої публікації, блокнот, телефон, сповіщення?»).

   Причина не в симетрії з менеджером, а в арифметиці: щойно до «Виконаних»,
   «Подій», «KPI по місяцях», «Контактів» і «Не буду на роботі» додались
   «Публікації», головна перетворилась на список із шести посилань. У меню
   пішло те, що відкривають РЕГУЛЯРНО і що не має числа; на головній лишились
   двері, які без числа не мають сенсу («Виконані ×3», «66%»).

   У перегляді чужими очима меню теж журналістське — інакше половина її
   екрана була б менеджеру недосяжна. «Події» там вимкнено: /api/notifications
   віддає стрічку того, хто дивиться, і це була б чужа стрічка під її іменем. */
const NAV_JOURNALIST = [
  ["home", "home", "Головна"],
  ["mypubs", "trending-up", "Публікації"],
  ["todo", "check", "Блокнот"],
  ["contacts", "book", "Контакти"],
  ["myfeed", "bell", "Події"],
];

function paintNav() {
  const nav = $("bottomnav");
  // Менеджерське меню лежить у розмітці — запамʼятовуємо його ДО першої
  // перемальовки, інакше повернення з перегляду лишало б порожню смугу
  if (!STATE.navManager) STATE.navManager = nav.innerHTML;
  const preview = inPreview();
  const role = preview ? "p" : (STATE.me.manager ? "m" : "j");
  if (nav.dataset.role === role) return;
  nav.dataset.role = role;
  if (role === "m") { nav.innerHTML = STATE.navManager; syncAlertsBadge(); return; }
  nav.innerHTML = NAV_JOURNALIST.map(([v, ic, label]) => {
    // Блокнот приватний за задумом: у перегляді чужими очима його
    // не показуємо навіть вимкненим змістом — це чужі особисті записи
    const dead = preview && (v === "myfeed" || v === "todo");
    return `<button class="bn${dead ? " off" : ""}"
      ${dead ? 'disabled aria-disabled="true"' : `data-view="${v}"`}>
      <svg class="ic"><use href="#i-${ic}"/></svg><span>${label}</span></button>`;
  }).join("");
  syncAlertsBadge();
}

/* Лічильник на пункті меню «Сповіщення»: спірні + непрочитані події.
   У журналістки той самий пункт зветься «Події» — лічильник має знайти саме
   його, інакше вона не бачила б, що їй щось зарахували. */
function syncAlertsBadge() {
  const btn = document.querySelector('#bottomnav [data-view="alerts"]')
    || document.querySelector('#bottomnav [data-view="myfeed"]');
  if (!btn) return;
  // Прострочені теж просять дії, тож і в лічильнику вони мають бути: інакше
  // Катя бачила б «0» на пункті меню, у якому лежить п'ять завислих завдань
  const n = STATE.me && !STATE.me.manager
    ? ((STATE.myFeed && STATE.myFeed.unread) || 0)
    : (STATE.pendingCount || 0) + (STATE.unread || 0)
      + (STATE.me && STATE.me.manager
         ? overdueTasks().length + (STATE.absenceRequests || []).length : 0);
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

/* Заголовок норми. row — рядок КОНКРЕТНОЇ людини, якщо він є: тоді число
   беремо її, а не відділове.

   Раніше заголовок завжди брав ціль відділу, а дріб праворуч — особисту, і на
   екрані виходило «60 власних новин … 40/39» (Олег, 29.07: «ти щось розумієш
   із цього табло?»). Два різні числа про одне й те саме в одному рядку
   читаються як помилка, а причина розбіжності — відпустка — не була сказана
   ніде. */
function normTitle(n, row) {
  const target = row && row.target != null ? row.target : n.target;
  const own = n.own ? (target === 1 ? "власна " : "власних ") : "";
  return `${target} ${own}${qtyWord(n.metric, target)} · ${n.period === "week" ? "щотижня" : "щомісяця"}`;
}

/* Чому ціль не така, як у відділу: «відпустка · замість 60». Без цього
   зменшена цифра виглядає помилкою.

   Ручну правку показуємо ЗАВЖДИ — навіть коли її число збіглося з відділовим.
   Саме мовчазна правка «рівно 200» з'їдала щойно виставлену ставку, і на
   екрані не лишалось ані сліду причини (Олег, 29.07: «поставил Кристине
   ставку 60%, а 200 KPI не пересчиталось»). Правка сильніша за ставку — це
   задумано, але воно має бути СКАЗАНО. */
function normWhy(n, row) {
  if (!row || row.target == null || row.base_target == null) return "";
  const same = row.target === row.base_target;
  if (row.overridden) {
    const tail = row.rate != null && row.rate < 100
      ? `ставка ${row.rate}% не діє`
      : same ? "" : `замість ${row.base_target}`;
    return `<span class="mk-why">правка${tail ? " · " + tail : ""}</span>`;
  }
  if (same) return "";
  const why = row.absence ? row.absence.title
    : (row.rate != null && row.rate < 100) ? `ставка ${row.rate}%` : null;
  return `<span class="mk-why">${why ? esc(why) + " · " : ""}замість ${row.base_target}</span>`;
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
/* mark — засічка «де ти маєш бути сьогодні» (відсоток очікуваного). Саме вона
   робить нуль першого числа спокійним: заповнення порожнє, але й засічка
   стоїть на нулі, тобто відставання немає.
   animate — намалювати порожнім і залити наступним кадром (див. animateFill). */
function avatarRing(person, entry, pct, size, fixedColor, { mark, animate } = {}) {
  const r = 44, c = 2 * Math.PI * r;
  const p = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  const off = c * (1 - p / 100);
  const color = fixedColor || pctColor(pct);
  let notch = "";
  if (mark != null && mark > 0 && mark < 100) {
    const a = 2 * Math.PI * (Math.min(100, mark) / 100);
    const xy = (rad) => [50 + rad * Math.cos(a), 50 + rad * Math.sin(a)];
    const [x1, y1] = xy(r - 7), [x2, y2] = xy(r + 7);
    notch = `<line class="mark" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}"
      x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
  }
  return `<span class="avaring" style="width:${size}px;height:${size}px">
    <svg viewBox="0 0 100 100" class="ring">
      <circle class="track" cx="50" cy="50" r="${r}"/>
      <circle class="prog" cx="50" cy="50" r="${r}"
        ${animate ? `data-fill="${off.toFixed(1)}"` : ""}
        style="stroke-dasharray:${c.toFixed(1)};stroke-dashoffset:${(animate ? c : off).toFixed(1)};stroke:${color || "transparent"}"/>
      ${notch}
    </svg>
    <span class="ava-inner">${avatar(person, entry, size - 18)}</span>
  </span>`;
}

/* Заливка анімацією: намальоване в нулі приїжджає до свого значення наступним
   кадром. Робимо це ПРИ ВХОДІ на екран, а не при кожній перемальовці — інакше
   після кожного тику по чому завгодно все повзло б заново і за день набридло.
   Два вкладені rAF — щоб браузер устиг застосувати початковий стан, інакше
   він схлопне обидва значення в одне й переходу не буде. */
function animateFill(root) {
  const nodes = root ? root.querySelectorAll("[data-fill]") : [];
  if (!nodes.length) return;
  const apply = () => nodes.forEach((n) => {
    if (n.tagName === "circle") n.style.strokeDashoffset = n.dataset.fill;
    else n.style.width = n.dataset.fill;
    n.removeAttribute("data-fill");
  });
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) {
    apply();
    return;
  }
  requestAnimationFrame(() => requestAnimationFrame(apply));
}

/* Звітний дашборд: перемикач тиждень/місяць + гортання період-влево/вправо,
   люди кружечками з кільцем % виконання KPI. Дані — з /api/kpi/dashboard
   (історія рахується з nodes за минулі періоди). */
/* Шапка дашборда (перемикач + гортання періодів) окремо від кілець: її треба
   намалювати ОДРАЗУ по тапу, ще до відповіді сервера. Факт іде в БД сайту і
   їде помітно, а до цього екран стояв зі старими цифрами під старим
   перемикачем — виглядало як зависання, і Олег тикав удруге (28.07).
   data == null — режим завантаження: підпис періоду шимерить. */
function dashControls(data) {
  const d = STATE.dash;
  const label = data
    ? `<b>${esc(data.label)}${data.is_current ? " · зараз"
        : d.offset === -1 ? " · минулий" : ""}</b>`
    : `<b class="sk sk-lbl"></b>`;
  // Поки не знаємо is_current — беремо клієнтську оцінку, інакше «вперед»
  // на секунду відкривалась би в майбутнє
  const atNow = data ? data.is_current : d.offset >= defaultOffset(d.period);
  return `
    ${segment("data-dp", [["week", "Тиждень"], ["month", "Місяць"]], d.period, { sub: true })}
    <div class="month-nav">
      <button class="arr" data-doff="-1">${icon("chevron-left")}</button>
      ${label}
      <button class="arr" data-doff="1" ${atNow ? "disabled" : ""}>${icon("chevron-right")}</button>
    </div>`;
}

function wireDashControls(body) {
  body.querySelectorAll("[data-dp]").forEach((b) => b.onclick = () => {
    const d = STATE.dash;
    if (d.period === b.dataset.dp) return;
    d.period = b.dataset.dp; d.offset = defaultOffset(d.period); renderDashboard();
  });
  body.querySelectorAll("[data-doff]").forEach((b) => b.onclick = () => {
    if (b.disabled) return;
    const d = STATE.dash;
    d.offset = Math.min(0, d.offset + (+b.dataset.doff)); renderDashboard();
  });
}

let dashReq = 0;

async function renderDashboard() {
  const d = STATE.dash;
  const loading = $("dash-body");
  if (loading) {
    loading.innerHTML = dashControls(null) + skeleton("rings", 6);
    wireDashControls(loading);   // перемикач лишається живим і під час завантаження
  }
  // Тики швидші за відповідь: перемогти має ОСТАННІЙ вибір, інакше повільніша
  // відповідь попереднього періоду перемалює екран поверх свіжішої
  const req = ++dashReq;
  try {
    d.data = await api(`/api/kpi/dashboard?period=${d.period}&offset=${d.offset}`);
  } catch (e) {
    const body = $("dash-body");
    if (body && req === dashReq) body.innerHTML = dashControls(null)
      + `<div class="empty-hint">${esc(e.message)}</div>`;
    if (body && req === dashReq) wireDashControls(body);
    return;
  }
  if (req !== dashReq) return;
  if (STATE.view !== "home" || STATE.homeView !== "report") return;
  const data = d.data;
  const byDept = {};
  data.people.forEach((p) => (byDept[p.dept_title] = byDept[p.dept_title] || []).push(p));
  const grid = Object.entries(byDept).map(([dept, list]) => `
    <div class="dept-title">${esc(dept)}</div>
    <div class="ring-grid">
      ${list.map((p) => {
        // Колір — за ТЕМПОМ, заповнення — за виконанням. У завершеному періоді
        // це одне й те саме число (очікування = ціль), тож минулі періоди
        // виглядають рівно як раніше; змінюється лише той, що триває — і 1
        // серпня вся редакція більше не червона (Олег, 28.07).
        // Період, що ТРИВАЄ, без темпу (перші дні: очікування ще нульове) не
        // фарбуємо взагалі. Інакше overall_pct = 0 давав рівно те червоне
        // кільце, від якого ми йдемо, — просто в іншому місці.
        const ring = p.pace_pct != null ? p.pace_pct
          : (data.is_current ? null : p.overall_pct);
        const mark = markPct(p);
        return `
        <button class="ring-cell" data-rperson="${esc(p.person)}">
          ${avatarRing(p.person, personEntry(p.person), p.overall_pct, 104,
            pctColor(ring) || "transparent", { mark, animate: true })}
          <span class="rc-name">${esc(p.person.split(" ")[0])}</span>
          <span class="rc-pct" style="color:${pctColor(ring) || "var(--muted)"}">${p.overall_pct == null ? "—" : p.overall_pct + "%"}</span>
        </button>`;
      }).join("")}
    </div>`).join("");
  const body = $("dash-body");
  if (!body) return;
  body.innerHTML = dashControls(data) + `
    ${grid || `<div class="empty-hint">Норм на цей період немає.</div>`}
    ${!data.site_db ? `<div class="tl-note">БД сайту недоступна — факт не рахується.</div>` : ""}`;
  wireDashControls(body);
  animateFill(body);
  body.querySelectorAll("[data-rperson]").forEach((b) => b.onclick = () =>
    nav("personhist", b.dataset.rperson));
}

/* Засічка «де людина має бути сьогодні» у відсотках від цілі. Одна норма —
   беремо її; кілька — усереднюємо, бо кільце теж усереднене. */
function markPct(p) {
  const vals = (p.norms || [])
    .filter((n) => n.expected != null && n.target > 0)
    .map((n) => Math.min(100, Math.round(n.expected / n.target * 100)));
  if (!vals.length) return null;
  return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
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
    <button class="peek-btn" data-peek="${esc(person)}">
      ${icon("search")} Подивитись її екран
    </button>
    <div id="hist-body">${skeleton("bars", 12)}</div>`;
  $("content").querySelectorAll("[data-peek]").forEach((b) =>
    b.onclick = () => nav("preview", b.dataset.peek));
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
function historyChartHtml(data, chartId = "hist-chart", sub = "виконання KPI по місяцях") {
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
  // Підпис: у журналістки заголовок картки вже каже «KPI по місяцях», тож
  // вона передає sub=null і лишається сама норма — без масла масляного.
  const norm = data.months[data.months.length - 1].norms[0];
  const subLine = [sub, norm ? norm.label : null].filter(Boolean).join(" · ");
  return `
    ${subLine ? `<div class="h-sub">${esc(subLine)}</div>` : ""}
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
  // Ціль вводиться і кнопками, і з клавіатури: Кристина на пів ставки — 200
  // треба замінити на 100, а це сто натискань на «мінус» (Олег, 29.07).
  let target = row.excused ? 0 : row.target;
  const paint = () => {
    if ($("o-val").value !== String(target)) $("o-val").value = target;
    $("o-hint").textContent = target === 0 ? "0 — звільнено від норми цього періоду" : "";
  };
  openSheet(`
    <h2>${esc(row.person)}</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 12px">
      ${esc(normTitle(n, row))} · норма відділу: ${n.base_target || n.target}</p>
    <div class="f-label" style="margin-top:0">Ціль на цей ${n.period === "week" ? "тиждень" : "місяць"}</div>
    <div class="stepper with-input">
      <button id="o-minus">${icon("minus")}</button>
      <input id="o-val" type="number" inputmode="numeric" min="0" max="500" value="${target}">
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
  // Кнопки міняють число в полі, а поле — змінну; межі ті самі, щоб зберегти
  // не можна було ні відʼємне, ні тризначне понад стелю
  $("o-val").oninput = () => {
    const v = parseInt($("o-val").value, 10);
    target = Number.isFinite(v) ? Math.max(0, Math.min(500, v)) : 0;
    $("o-hint").textContent = target === 0 ? "0 — звільнено від норми цього періоду" : "";
  };
  $("o-val").onblur = paint;
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

/* ---------- Журналістський режим ----------
   Два поверхи (Олег, 28.07: «а що як розділити на зрозумілі кнопки»):
   ЗВЕРХУ — відповідь на сьогоднішнє питання (як я йду по нормі + що робити),
   живцем і цілком. ЗНИЗУ — двері з числами.

   Чому саме так, а не список кнопок: зробити перший екран меню означало б
   відсунути головне на другий тап, а журналістка відкриває апку рівно заради
   нього. І чому двері, а не згортання (той тумблер на графіку був імпульсом,
   як Олег сам і сказав): згорнутий блок усе одно займає рядок і не каже, чи
   є всередині щось нове, — а двері з числом кажуть.

   ПРАВИЛО: двері зобов'язані нести число. «Мої публікації →» це пункт меню;
   «Мої публікації · 12 за місяць» — це вже інформація, і заразом двері. Без
   числа екран-хаб перетворюється на витрачений екран. */

/* Двері: іконка в мʼякій плашці, назва, підпис і число праворуч. */
/* off — двері видно, але вони не відкриваються. Це для перегляду чужими очима:
   Олег, 29.07 — «я хочу бачити все, що у неї, просто нехай не натискається
   там, де сенситивне, бо мені так складно з тобою кодити наосліп». Ховати
   двері не можна (екран перестає бути тим самим екраном), відкривати теж:
   «Події» позначились би прочитаними за людину, а «Не буду на роботі» подало
   б запит на відпустку від її імені. */
function doorHtml(view, iconName, tone, title, meta, n, off) {
  return `
    <button class="door ${tone}${off ? " off" : ""}"
      ${off ? 'disabled aria-disabled="true"' : `data-nav="${view}"`}>
      <span class="door-ic">${icon(iconName)}</span>
      <span class="door-txt">
        <span class="door-t">${esc(title)}</span>
        ${meta ? `<span class="door-m">${esc(meta)}</span>` : ""}
      </span>
      ${n ? `<span class="door-n">${esc(String(n))}</span>` : ""}
      ${off ? `<span class="door-off">перегляд</span>` : icon("chevron-right", "ic chev")}
    </button>`;
}

/* Чия «Головна» малюється. Зазвичай — власна; у менеджерському перегляді
   (STATE.view === "preview") — обраної журналістки: «зроби мені можливість
   дивитись перший екран обраного журналіста, так дебажити легше» (Олег,
   29.07). Перегляд лише читає: чужу стрічку подій не показуємо і прочитаною
   не робимо. */
/* Підекрани журналістського режиму. Поки Олег ходить ними, перегляд лишається
   переглядом: раніше «preview» жив рівно один екран, і тап у «Публікації»
   мовчки перемикав його на власні дані. */
const J_SUBVIEWS = new Set(["preview", "mypubs", "myhist", "mydone", "myfeed", "myimpacts", "impact"]);

function inPreview() {
  return !!STATE.previewPerson && J_SUBVIEWS.has(STATE.view);
}

function viewPerson() {
  if (inPreview()) {
    const name = STATE.previewPerson;
    return { name, first_name: name.split(" ")[0],
             entry: personEntry(name) || {}, preview: true };
  }
  return { name: STATE.me.name, first_name: STATE.me.first_name,
           entry: STATE.me, preview: false };
}

/* Заглушка «Моїх KPI» на час завантаження. Факт іде в MySQL сайту і думає по
   дві-три секунди, а до цього блок був порожній — потім різко з'являвся і
   зсував увесь екран униз (Олег, 29.07: «так не повинно бути»). Заглушка
   тримає приблизну висоту майбутньої картки, тож нічого не стрибає. */
function kpiSkeleton() {
  return `
    <div class="pace-card">
      <span class="sk" style="display:block;height:19px;width:72%"></span>
      <div class="steps">${Array.from({ length: 5 }, () =>
        `<span class="step"><span class="sk sk-dot"></span>
         <span class="sk" style="display:block;height:9px;width:26px"></span></span>`).join("")}</div>
      <span class="sk" style="display:block;height:13px;width:58%;margin-top:18px"></span>
    </div>
    <div class="soft-card">
      <span class="sk" style="display:block;height:16px;width:34%;margin-bottom:14px"></span>
      <span class="sk" style="display:block;height:13px;width:76%"></span>
      <span class="sk" style="display:block;height:5px;width:100%;margin-top:9px"></span>
      <span class="sk" style="display:block;height:13px;width:64%;margin-top:16px"></span>
      <span class="sk" style="display:block;height:5px;width:100%;margin-top:9px"></span>
    </div>`;
}

/* Запит на відпустку від самої людини (Олег, 29.07: «щоб співробітник сам міг
   запропонувати дати, а Каті прийшло на погодження»). До погодження нічого не
   змінюється: непогоджений запит не має знижувати норму — інакше її можна було
   б збити самим фактом прохання. */
function absenceRequestSheet() {
  let kind = "vacation";
  openSheet(`
    <h2>Коли тебе не буде</h2>
    <p style="color:var(--muted);font-size:13px;margin:-8px 0 14px">
      Катя побачить запит у «Сповіщеннях» і погодить або відхилить.</p>
    <div class="chips" id="ar-kinds">
      ${ABSENCE_KINDS.map((k) => `<button class="chip${k.v === kind ? " on" : ""}"
        data-ak="${k.v}">${k.label}</button>`).join("")}
    </div>
    <div class="f-label">З якого дня</div>
    <input id="ar-start" type="date" min="${todayISO()}" value="${todayISO()}">
    <div class="f-label">По який день включно</div>
    <input id="ar-end" type="date" min="${todayISO()}" value="${todayISO()}">
    <div class="f-label">Коментар (необовʼязково)</div>
    <input id="ar-note" maxlength="120" placeholder="за свій рахунок, планую…">
    <div class="sheet-actions">
      <button class="sbtn" id="ar-cancel">Скасувати</button>
      <button class="sbtn primary" id="ar-send">Надіслати</button>
    </div>`);
  $("ar-kinds").querySelectorAll("[data-ak]").forEach((b) => b.onclick = () => {
    kind = b.dataset.ak;
    $("ar-kinds").querySelectorAll("[data-ak]").forEach((x) =>
      x.classList.toggle("on", x.dataset.ak === kind));
  });
  $("ar-cancel").onclick = closeSheet;
  $("ar-send").onclick = async () => {
    const start = $("ar-start").value, end = $("ar-end").value;
    if (!start || !end) { toast("Вкажи дати"); return; }
    try {
      await api("/api/absences/request", {
        method: "POST",
        body: JSON.stringify({ start, end, kind, note: $("ar-note").value.trim() }),
      });
      closeSheet();
      haptic("success");
      toast("Запит пішов Каті");
    } catch (e) { toast(e.message); }
  };
}

function renderJournalist() {
  const me = viewPerson();
  const mine = me.preview
    ? STATE.tasks.filter((t) => t.person === me.name) : STATE.tasks;
  const open = mine.filter((t) => t.status === "open");
  const closed = mine.filter((t) => t.status === "done");
  const feed = STATE.myFeed;
  $("content").innerHTML = `
    ${me.preview ? `<button class="back" data-back>${icon("chevron-left")} Профіль</button>
      <div class="peek">Перегляд очима ${esc(me.first_name)} · вона цього не бачить</div>` : ""}
    <div class="me-head">
      <div class="me-txt">
        <div class="h-big">Привіт, ${esc(me.first_name)}</div>
        <div class="h-sub">твої завдання і KPI</div>
      </div>
      <div class="me-ring" id="me-ring">${meRingHtml(null)}</div>
    </div>
    ${impactBannerHtml(me)}
    <div id="my-kpi">${kpiSkeleton()}</div>
    ${open.length ? `<div class="soft-card">${open.map((t) => {
      const tp = taskProject(t);
      const qtyPart = t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1);
      // Головне для журналістки — ЩО писати: тематика великим, під нею
      // «скільки і чого» + донор. Назви проєкту тут немає навмисно: вона
      // займала перший рядок і не відповідала на жодне питання.
      const what = t.theme_name || qtyPart;
      const meta = [t.theme_name ? qtyPart : null, tp.partner].filter(Boolean).join(" · ");
      return `
      <div class="task-row">
        ${tp.logoHtml || ""}
        <span class="tr-main">
          <span class="tr-who">${esc(what)}</span>
          ${meta ? `<span class="tr-what">${esc(meta)}</span>` : ""}
          ${t.note ? `<span class="tr-note">${esc(t.note)}</span>` : ""}
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
    ${/* На головній лишаються лише двері, які без числа не мають сенсу:
          «Виконані ×3», «66%». Регулярне — «Публікації», «Контакти», «Події» —
          переїхало в нижнє меню (див. NAV_JOURNALIST), інакше екран був би
          списком із шести посилань.

          Перегляд чужими очима показує ТОЙ САМИЙ екран (Олег, 29.07: «я хочу
          бачити все, що у неї»). Не відкривається лише «Не буду на роботі» —
          воно подало б запит на відпустку від її імені. */""}
    <div class="doors">
      ${closed.length ? doorHtml("mydone", "check", "c-good", "Виконані",
        "завдання, які вже закрито", closed.length) : ""}
      ${doorHtml("myhist", "bar-chart", "c-sky", "KPI по місяцях",
        "як іде місяць до місяця", "")}
      ${doorHtml("promises", "bell", "c-sky", "Банк тем",
        "що влада обіцяла і кому пора нагадати", "")}
      ${myImpactsFresh(me).length
        ? doorHtml("myimpacts", "award", "c-good", "Мої імпакти",
            "що змінилось завдяки твоїм текстам", myImpactsFresh(me).length)
        : ""}
      <button class="door c-good${me.preview ? " off" : ""}"
        ${me.preview ? 'disabled aria-disabled="true"' : "data-ask"}>
        <span class="door-ic">${icon("calendar")}</span>
        <span class="door-txt">
          <span class="door-t">Не буду на роботі</span>
          <span class="door-m">відпустка · лікарняна · відрядження</span>
        </span>
        ${me.preview ? `<span class="door-off">перегляд</span>`
          : icon("chevron-right", "ic chev")}
      </button>
    </div>`;
  $("content").querySelectorAll("[data-nav]").forEach((b) =>
    b.onclick = () => nav(b.dataset.nav));
  const ask = $("content").querySelector("[data-ask]");
  if (ask) ask.onclick = absenceRequestSheet;
  const banner = $("content").querySelector("[data-impbanner]");
  if (banner) banner.onclick = () => {
    STATE.currentImpact = +banner.dataset.impbanner;
    STATE.impactFrom = "home";
    nav("impact");
  };
  renderMyKpi();
  if (!me.preview) loadMyFeed();
  loadMyImpacts();
}

/* Лічильник на дверях «Події». Стрічку тягнемо ОДИН раз і кладемо в STATE:
   прочитаним вона стає лише коли двері відкрили — раніше подія позначалась
   прочитаною просто за те, що екран намалювався. */
async function loadMyFeed() {
  if (STATE.myFeed) return;
  try {
    STATE.myFeed = await api("/api/notifications");
  } catch (e) { return; }
  // Непрочитані тепер живуть не на дверях, а числом на пункті меню «Події» —
  // двері з головної переїхали в нижнє меню
  syncAlertsBadge();
  if (STATE.view === "home") renderJournalist();
}

/* Виконані завдання — окремий екран. На головній вони займали пів скрола, а
   дивляться в них рідко: це СТАН, а не робота. */
function renderMyDone() {
  const closed = STATE.tasks.filter((t) => t.status === "done");
  $("content").innerHTML = `
    <button class="back" data-nav="home">${icon("chevron-left")} Головна</button>
    <div class="h-big">Виконані</div>
    <div class="h-sub">завдань закрито: ${closed.length}</div>
    ${closed.length ? `<div class="soft-card">${closed.map((t) => {
      const qtyPart = t.qty > 1 ? `${t.qty} ${typePhrase(t, t.qty)}` : typePhrase(t, 1);
      return `
      <div class="task-row is-done">
        <span class="tr-main">
          <span class="tr-who">${esc(t.theme_name || qtyPart)}</span>
          <span class="tr-what">${esc([t.theme_name ? qtyPart : null,
            taskProject(t).partner].filter(Boolean).join(" · "))}</span>
          ${matchesHtml(t)}
        </span>
        <span class="tr-right">${progressHtml(t)}${statusMark(t)}</span>
      </div>`;
    }).join("")}</div>` : `<div class="empty-hint">Поки нічого не закрито.</div>`}`;
  $("content").querySelectorAll("[data-nav]").forEach((b) =>
    b.onclick = () => nav(b.dataset.nav));
}

/* ---------- Мої публікації ----------

   Олег, 29.07: «дати можливість журналістці в своєму інтерфейсі бачити кнопку
   "Мої публікації"». Джерело — ті самі `nodes`, що рахують факт KPI, тож
   перелік і кружечок норми не можуть розійтися: якщо вона бачить тут 34
   матеріали, у нормі теж 34, і сперечатися нема про що.

   Соцмереж тут немає. /stat по кожному рядку — це походи у Facebook,
   Instagram, TikTok, YouTube і GA4 на КОЖЕН матеріал; екран, який відкривають
   щодня, так робити не можна. Лічильник переглядів — власний, сайтовий, і він
   ЗА ВЕСЬ ЧАС, а не за місяць: тому підпис «на сайті», без «за липень».

   Гортання місяців тут таке саме, як у «Звіті» менеджера: людина хоче
   побачити не лише поточний місяць, а й «а скільки було в червні». */
async function renderMyPubs() {
  const me = viewPerson();
  const off = STATE.pubsOffset || 0;
  const q = me.preview ? `&person=${encodeURIComponent(me.name)}` : "";
  $("content").innerHTML = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="h-big">${me.preview ? esc(me.first_name) + " · публікації" : "Мої публікації"}</div>
    <div class="h-sub">${me.preview ? "вихід за місяць" : "що вийшло під твоїм іменем"}</div>
    <div id="pubs-body">${skeleton("rows", 4)}</div>`;
  $("content").querySelectorAll("[data-nav]").forEach((b) =>
    b.onclick = () => nav(b.dataset.nav));
  let data;
  try {
    data = await api(`/api/publications?offset=${off}${q}`);
  } catch (e) {
    data = { error: e.message };
  }
  if (STATE.view !== "mypubs") return;
  const body = $("pubs-body");
  if (!body) return;
  if (data.error) {
    body.innerHTML = `<div class="empty-hint">${esc(data.error)}</div>`;
    return;
  }
  const items = data.items || [];
  const bits = [`${data.total} ${plural(data.total, "матеріал", "матеріали", "матеріалів")}`];
  if (data.own) bits.push(`${data.own} ${plural(data.own, "власний", "власні", "власних")}`);
  if (data.articles) bits.push(`${data.articles} ${plural(data.articles, "стаття", "статті", "статей")}`);
  body.innerHTML = `
    ${periodStrip(data.label, off)}
    <div class="pub-hero">
      <div class="pub-n">${data.views == null ? data.total : fmtNum(data.views)}</div>
      <div class="pub-l">${data.views == null ? "матеріалів за місяць" : "переглядів на сайті"}</div>
      <div class="pub-s">${esc(bits.join(" · "))}</div>
    </div>
    ${items.length ? `<div class="soft-card">${items.map(pubRow).join("")}</div>`
      : `<div class="empty-hint">Цього місяця публікацій ще немає.</div>`}
    ${data.capped ? `<div class="tl-note">Показано перші ${items.length} — за місяць вийшло більше.</div>` : ""}
    <div class="tl-note">Лічильник переглядів сайту — за весь час матеріалу, не за місяць.</div>`;
  body.querySelectorAll("[data-poff]").forEach((b) => b.onclick = () => {
    STATE.pubsOffset = +b.dataset.poff;
    renderMyPubs();
  });
}

function periodStrip(label, off) {
  return `<div class="per-strip">
    <button class="per-arrow" data-poff="${off - 1}">${icon("chevron-left")}</button>
    <span class="per-lbl">${esc(label || "")}</span>
    <button class="per-arrow${off >= 0 ? " off" : ""}"
      ${off >= 0 ? "disabled" : `data-poff="${off + 1}"`}>${icon("chevron-right")}</button>
  </div>`;
}

/* Рядок публікації. Дата й час стоять ПЕРШИМИ і в окремому боксі (Олег,
   29.07). Спершу вони йшли підписом під заголовком — і в живому списку з
   тридцяти новин заголовок перетікав у дату одним абзацом:
   «…призначили майора Гіпіка 27.07 · 21:35» читалось як частина заголовка.
   Бокс ліворуч розриває це фізично: око бачить стовпчик дат і окремо тексти. */
function pubRow(p) {
  const tags = [p.own ? "власний" : null, p.kind === "article" ? "стаття" : null]
    .filter(Boolean).join(" · ");
  return `
    <a class="pub-row" data-ext="${esc(p.url)}" href="${esc(p.url)}">
      <span class="pub-when"><b>${esc(p.date)}</b><i>${esc(p.time)}</i></span>
      <span class="pub-t">
        <span class="pub-title">${esc(p.title)}</span>
        ${tags ? `<span class="pub-tags">${esc(tags)}</span>` : ""}
      </span>
      ${p.views ? `<span class="pub-v">${fmtNum(p.views)}</span>` : ""}
    </a>`;
}

/* ---------- Блокнот ----------

   Олег, 29.07: «блокнот to-do, адмінам теж його… почитай, які існують найкращі
   практики todo-лістів, чому одні ефективні, а інші ні». Що з дослідження
   перетворилось саме на цей екран (розгорнуто — у docstring team_todos.py):

   1. 41% пунктів не роблять ніколи, а половину зроблених закривають у той
      самий день, 10% — за хвилину після запису. Отже ЗАХОПЛЕННЯ важливіше за
      все інше: поле зверху, Enter додає і лишає фокус, жодного діалогу,
      жодного обовʼязкового поля. Плюс /todo просто в чаті.
   2. Masicampo & Baumeister: незавершене вантажить память доти, доки не
      СПЛАНОВАНЕ, а не доки не зроблене. Тому два кошики — «Сьогодні» і
      «Потім»: це мінімальний план, який знімає вантаж, а не просто список.
   3. Gollwitzer: намір «коли» подвоює шанс виконання. «Сьогодні чи потім» —
      найдешевша його форма, яку люди справді заповнюють.
   4. Айві Лі: сила в ЛІМІТІ, не в порядку. Тому мʼякий поріг на «Сьогодні»:
      не забороняємо, а кажемо вголос.
   5. Amabile: видимий прогрес — найсильніший драйвер. «Зроблено сьогодні · 4»
      лишається на екрані до кінця дня.

   Пріоритетів, дедлайнів, тегів і підзадач тут немає СВІДОМО — кожен із них
   вимагає рішення в момент захоплення, тобто платить саме там, де має бути
   безкоштовно. Для зобовʼязань перед редакцією є creative tasks. */
async function renderTodos() {
  const head = `
    <button class="back" data-back>${icon("chevron-left")} Назад</button>
    <div class="h-big">Блокнот</div>
    <div class="h-sub">особистий список — його не бачить ніхто, крім тебе</div>
    <form class="td-add" id="td-form">
      <input id="td-text" maxlength="300" autocomplete="off"
        placeholder="Що зробити?">
      <button class="td-plus" type="submit" aria-label="Додати">${icon("plus")}</button>
    </form>`;
  if (!STATE.todos) {
    $("content").innerHTML = head + `<div id="td-body">${skeleton("rows", 3)}</div>`;
    wireTodoForm();
    try {
      STATE.todos = await api("/api/todos");
    } catch (e) {
      const b = $("td-body");
      if (b) b.innerHTML = `<div class="empty-hint">${esc(e.message)}</div>`;
      return;
    }
    if (STATE.view !== "todo") return;
  } else {
    $("content").innerHTML = head + `<div id="td-body"></div>`;
    wireTodoForm();
  }
  paintTodos();
}

function paintTodos() {
  const d = STATE.todos || { today: [], later: [], done: [], done_today: 0 };
  const body = $("td-body");
  if (!body) return;
  const over = d.today.length > (d.soft_max || 6);
  const empty = !d.today.length && !d.later.length && !d.done.length;
  body.innerHTML = empty ? `<div class="empty-hint">Порожньо.<br>
      Запиши перше — або кинь Лису в приват <code>/todo передзвонити Луковій</code>.</div>` : `
    ${d.today.length ? `
      <div class="dept-title">Сьогодні · ${d.today.length}</div>
      ${over ? `<div class="td-note">Більше шести справ на день майже ніколи
        не встигається — перенеси зайве в «Потім», щоб список лишався тим,
        у який заглядаєш.</div>` : ""}
      <div class="soft-card">${d.today.map(todoRow).join("")}</div>` : ""}
    ${d.later.length ? `
      <div class="dept-title">Потім · ${d.later.length}</div>
      <div class="soft-card">${d.later.map(todoRow).join("")}</div>` : ""}
    ${d.done.length ? `
      <div class="dept-title">${d.done_today
        ? `Зроблено сьогодні · ${d.done_today}` : "Зроблено"}</div>
      <div class="soft-card">${d.done.map(todoRow).join("")}</div>` : ""}`;
  wireTodos();
}

/* Вік пункту — без червоного і без докорів. Це інформація для рішення
   «роблю чи викидаю», а не оцінка людини: половину списків кидають саме тому,
   що вони почали виглядати як звинувачення. */
function todoAge(t) {
  if (t.done || !t.age) return "";
  if (t.bucket === "today") {
    return t.age === 1 ? "з учора"
      : `${t.age} ${plural(t.age, "день", "дні", "днів")} у списку`;
  }
  return t.stale ? `висить ${Math.round(t.age / 7)} тиж.` : "";
}

function todoRow(t) {
  const age = todoAge(t);
  return `
    <div class="td-row${t.done ? " is-done" : ""}" data-todo="${t.id}">
      <button class="td-check" data-tdone="${t.id}" aria-label="Готово">
        ${t.done ? icon("check") : ""}</button>
      <span class="td-t">
        <span class="td-text">${esc(t.text)}</span>
        ${age ? `<span class="td-age">${esc(age)}</span>` : ""}
      </span>
      ${t.done ? `<button class="td-act" data-tdel="${t.id}"
          aria-label="Видалити">${icon("trash")}</button>`
        : `<button class="td-act" data-tmove="${t.id}"
          aria-label="${t.bucket === "today" ? "У «Потім»" : "Зробити сьогодні"}"
          >${icon(t.bucket === "today" ? "inbox" : "chevron-up")}</button>`}
    </div>`;
}

function wireTodoForm() {
  const form = $("td-form");
  if (!form) return;
  form.onsubmit = async (e) => {
    e.preventDefault();
    const input = $("td-text");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.focus();          // фокус лишається — підряд записують кілька справ
    try {
      const { todo } = await api("/api/todos", { method: "POST",
        body: JSON.stringify({ text, bucket: "today" }) });
      STATE.todos = STATE.todos || { today: [], later: [], done: [], done_today: 0 };
      STATE.todos.today.push(todo);
      paintTodos();
      // фокус міг злетіти на перемальовці — повертаємо
      const fresh = $("td-text");
      if (fresh) fresh.focus();
    } catch (err) { toast(err.message); }
  };
}

function wireTodos() {
  const body = $("td-body");
  if (!body) return;
  const patch = async (id, payload) => {
    try {
      await api(`/api/todos/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      STATE.todos = await api("/api/todos");
      if (STATE.view === "todo") paintTodos();
    } catch (e) { toast(e.message); }
  };
  body.querySelectorAll("[data-tdone]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.tdone;
    const t = allTodos().find((x) => x.id === id);
    if (!t) return;
    haptic("success");
    patch(id, { done: !t.done });
  });
  body.querySelectorAll("[data-tmove]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.tmove;
    const t = allTodos().find((x) => x.id === id);
    if (!t) return;
    patch(id, { bucket: t.bucket === "today" ? "later" : "today" });
  });
  body.querySelectorAll("[data-tdel]").forEach((b) => b.onclick = async () => {
    try {
      await api(`/api/todos/${b.dataset.tdel}`, { method: "DELETE" });
      STATE.todos = await api("/api/todos");
      if (STATE.view === "todo") paintTodos();
    } catch (e) { toast(e.message); }
  });
}

function allTodos() {
  const d = STATE.todos || {};
  return [...(d.today || []), ...(d.later || []), ...(d.done || [])];
}

/* Стрічка подій — окремий екран. Два названі блоки лишились: «Виконані» це
   СТАН тасків, а тут ПОДІЇ (що зарахували, що поставили). */
const MY_FEED_BLOCKS = [
  { kind: "impact_credit", title: "Імпакти за твоєї участі" },
  { kind: "task_done", title: "Нещодавно зараховані" },
  { kind: null, title: "Нові завдання" },   // решта стрічки
];

async function renderMyFeed() {
  const back = `<button class="back" data-nav="home">${icon("chevron-left")} Головна</button>
    <div class="h-big">Події</div>`;
  $("content").innerHTML = back + skeleton("rows", 3);
  let data = STATE.myFeed;
  if (!data) {
    try { data = STATE.myFeed = await api("/api/notifications"); } catch (e) {
      $("content").innerHTML = back +
        `<div class="empty-hint">Не вдалося завантажити стрічку.</div>`;
      return;
    }
  }
  if (STATE.view !== "myfeed") return;
  const items = (data.items || []).slice(0, 20);
  $("content").innerHTML = back + (items.length
    ? MY_FEED_BLOCKS.map((b) => {
        const named = MY_FEED_BLOCKS.filter((x) => x.kind).map((x) => x.kind);
        const rows = items.filter((n) => b.kind ? n.kind === b.kind : !named.includes(n.kind));
        if (!rows.length) return "";
        return `<div class="soft-card">
          <div class="sc-t">${b.title}</div>
          ${rows.map(notifRow).join("")}</div>`;
      }).join("")
    : `<div class="empty-hint">Поки тихо.</div>`);
  $("content").querySelectorAll("[data-nav]").forEach((b) =>
    b.onclick = () => nav(b.dataset.nav));
  if (data.unread) {
    try {
      await api("/api/notifications/read", {
        method: "POST", body: JSON.stringify({ all: true }),
      });
      data.unread = 0;   // двері більше не світяться
    } catch (e) { /* побачить наступного разу */ }
  }
}

/* Власна помісячна динаміка журналістки — ті самі стовпчики, що бачить
   редакція у «Звіті», але окремим екраном за дверима. Раніше це був блок на
   головній, згорнутий тумблером із памʼяттю в localStorage: він з'їдав пів
   екрана щодня, хоч дивляться в нього раз на місяць. Тумблер був імпульсом
   (Олег сам так і сказав), двері — рішенням: і довжину прибирають, і кажуть,
   що всередині. */
async function renderMyHistory() {
  const back = `<button class="back" data-nav="home">${icon("chevron-left")} Головна</button>
    <div class="h-big">KPI по місяцях</div>`;
  $("content").innerHTML = back + skeleton("bars", 12);
  const wire = () => $("content").querySelectorAll("[data-nav]").forEach((b) =>
    b.onclick = () => nav(b.dataset.nav));
  wire();
  let data;
  try {
    // Без person сервер підставляє того, хто дивиться; у перегляді чужими
    // очима це була б історія менеджера під її іменем
    const who = inPreview() ? `?person=${encodeURIComponent(STATE.previewPerson)}` : "";
    data = await api("/api/kpi/person" + who);
  } catch (e) {
    $("content").innerHTML = back +
      `<div class="empty-hint">${esc(e.message)}</div>`;
    wire();
    return;
  }
  if (STATE.view !== "myhist") return;
  if (!data.has_norms || !data.months || !data.months.length) {
    $("content").innerHTML = back +
      `<div class="empty-hint">У відділі немає місячної норми —<br>динаміку показати нема з чого.</div>`;
    wire();
    return;
  }
  $("content").innerHTML = back +
    `<div class="soft-card">${historyChartHtml(data, "my-hist-chart", null)}</div>`;
  wire();
  scrollHistoryToEnd("my-hist-chart");
}

/* ---------- Прогрес періоду: темп замість вироку ----------
   Олег, 28.07: «скоро початок місяця — людина відкриє, побачить 0% і
   засмутиться». І мала б рацію: 0% першого числа це рівно за планом, а
   кружечок був червоний, бо колір рахувався від КІНЦЯ періоду.

   Сервер тепер віддає очікування на сьогодні (team_kpi.period_pace /
   pace_row), а екран говорить про ТЕМП: фраза, засічка на кільці й колір
   беруться від відставання від графіка. У завершеному періоді очікування
   дорівнює цілі, тож минулі місяці виглядають рівно як раніше. */

/* ---------- Довідка «?» ----------
   Правило (28.07): спершу лагодимо ЗАГОЛОВОК, «?» ставимо лише туди, де сенс
   не влазить у три слова — тобто де число ПОСАХОВАНЕ, зважене або відстає в
   часі. Не більше двох на екран, сірий (синій = дія), тап відкриває звичну
   шторку (hover на телефоні не існує). Тексти — одним словником, щоб їх можна
   було вичитати як текст, а не виловлювати по розмітці. */
const HELP = {
  pace: ["Кільце і кружечки",
    "Кільце — скільки зроблено з місячної норми. Рисочка — де зазвичай "
    + "буваєш у цей день місяця.\n\n"
    + "Кружечок тижня зелений, якщо на його кінець ти була в графіку. "
    + "Рахується накопичувально, тож сильний тиждень витягує слабкий."],
  weight: ["Чому цифра більша за кількість новин",
    "Одна стаття зараховується як три новини — щоб місяць, у якому ти робила "
    + "велику статтю, не виглядав проваленим."],
};

function helpBtn(key) {
  return `<button class="qmark" data-help="${key}" aria-label="Що це">?</button>`;
}

function wireHelp(root) {
  (root || document).querySelectorAll("[data-help]").forEach((b) =>
    b.onclick = (e) => {
      e.stopPropagation();
      const [title, body] = HELP[b.dataset.help] || [];
      if (!title) return;
      openSheet(`<h2>${esc(title)}</h2>
        <div class="help-body">${body.split("\n\n").map((p) =>
          `<p>${esc(p)}</p>`).join("")}</div>
        <div class="sheet-actions">
          <button class="sbtn primary" id="help-ok">Зрозуміло</button>
        </div>`);
      $("help-ok").onclick = closeSheet;
    });
}

/* Колір темпу. На ВЛАСНОМУ екрані червоного немає навіть тоді, коли людина
   відстає: вона це й так бачить у цифрах, а червоне коло на своєму фото
   демотивує (те саме рішення, що було в avatarRing із fixedColor). Відставання
   позначаємо бурштиновим — це «підтягнись», а не «ти провалила». */
const PACE_COLOR = {
  done: "var(--good)", ahead: "var(--good)",
  on: "var(--blue)", start: "var(--blue)", behind: "var(--warn)",
};

/* Фраза — коротка, людська і БЕЗ ЖОДНОЇ ЦИФРИ (Олег, 29.07).
   Було «Трохи позаду графіка: 7 із 9 на сьогодні. Попереду 12 робочих днів» —
   і в цьому три помилки одразу:
   1. «позаду графіка» — калька, живою мовою так не кажуть;
   2. рахунок робочих днів у зверненні до людини звучить як завод, а ми
      робили спокій — і замість нього зробили таймер зворотного відліку;
   3. цифри у фразі тягнуть за собою хвіст умов (а раптом відпустка, раптом
      лікарняна, раптом неповний місяць) — купа правил заради коректності
      речення, яке й без них працює.

   Тому темп лишається, але говорить МОВЧКИ: кольором кільця і засічкою.
   Цифри лишились тільки там, де вони про роботу, а не про час: «13 новин до
   норми». Матриця «фаза × стан» збережена — фраза має бути правдивою і її
   не можна отримати за ніщо. */
const PACE_SAY = {
  done: {
    start: "Норму вже закрито 🔥",
    mid: "Норму закрито 👏 далі все — понад план",
    end: "Норму закрито 👏",
  },
  ahead: {
    start: "Потужний початок 🔥",
    mid: "Ідеш із запасом 🔥",
    end: "Ідеш із запасом — норма вже близько 🔥",
  },
  on: {
    start: "Гарний початок 🙂",
    mid: "Усе йде за планом 🙂",
    end: "Тримаєш темп — фініш уже близько",
  },
  behind: {
    start: "Ще все попереду 🙂",
    mid: "Час додати темпу 💪",
    end: "Місяць добігає кінця — саме час додати темпу 💪",
  },
};

function pacePhrase(pace, phase, monthWord) {
  if (pace === "start") return `${monthWord} щойно почався — усе попереду 🙂`;
  const row = PACE_SAY[pace];
  return row ? (row[phase] || row.mid) : "";
}

/* «Серпень» із підпису «серпень 2026» — назвати місяць тепліше, ніж «місяць»,
   і при цьому не тягнути в текст жодного числа. */
function monthWord(label) {
  const w = String(label || "").split(" ")[0] || "Місяць";
  return w.charAt(0).toUpperCase() + w.slice(1);
}

/* Місяць у родовому відмінку для підпису «Тижні липня». Сервер віддає
   називний («липень 2026»), а по-українськи над рядом тижнів має стояти
   родовий — інакше виходить «Тижні липень». */
const MONTH_GENITIVE = {
  січень: "січня", лютий: "лютого", березень: "березня", квітень: "квітня",
  травень: "травня", червень: "червня", липень: "липня", серпень: "серпня",
  вересень: "вересня", жовтень: "жовтня", листопад: "листопада",
  грудень: "грудня",
};

function monthGenitive(label) {
  const w = String(label || "").split(" ")[0].toLowerCase();
  return MONTH_GENITIVE[w] || w;
}

/* Кроки тижнів місяця — НАКОПИЧУВАЛЬНІ (Олег, 29.07: «поясни, чому перший і
   третій кружечки не виконані» — дивлячись на екран із написом «Норму
   закрито»). Пояснити було нічим: перша версія міряла РІВНОМІРНІСТЬ, якої від
   людини ніхто не вимагає. Норма місячна: можна написати п'ятнадцять за
   тиждень і два за наступний — це не порушення.

   Тепер крок каже інше: чи була ти в графіку НА КІНЕЦЬ цього тижня. Тиждень,
   коли набрала наперед, лишається зеленим і далі; незакритим лишається тільки
   той відрізок, де справді була позаду. Це вже шлях — видно, де просіла і де
   наздогнала, — і суперечити «Норму закрито» більше нічим. */
function stepsHtml(steps, monthLabel) {
  if (!steps || steps.length < 2) return "";
  // Підпис над рядом — Олег, 29.07: «1–5, 6–12 незрозуміло, хтось подумає, що
  // це кількість новин». Одне слово знімає двозначність одразу в усіх пʼяти
  // і, на відміну від «I тиждень», не з'їдає самі дати: людина має впізнати
  // тиждень, коли була у відпустці, а не рахувати римські цифри.
  const cap = monthLabel
    ? `<div class="steps-cap">Тижні ${esc(monthGenitive(monthLabel))}</div>` : "";
  return `${cap}<div class="steps">${steps.map((s) => {
    // Порядок важливий: «була у графіку» сильніше за «була у відпустці».
    // Раніше away перебивав done, і тиждень відпустки в людини, яка йшла з
    // запасом, малювався блідим порожнім кружечком — тобто виглядав як
    // провал, хоча провалу немає й бути не могло.
    const cls = s.done ? "done" : s.future ? "fut" : s.away ? "away" : "miss";
    return `<span class="step ${cls}${s.is_current ? " cur" : ""}">
      <span class="sdot">${s.done ? icon("check") : ""}</span>
      <span class="slbl">${esc(s.label)}</span>
    </span>`;
  }).join("")}</div>`;
}

/* Салют. Без бібліотеки: CSP не пускає CDN, статика гзіпиться в ~26 КБ, і
   тягнути пакет заради тридцяти квадратиків не варто.
   ГОЛОВНЕ — правило запуску: рівно один раз на ПЕРЕТИН норми, а не «поки
   100%». Інакше людина, що закрила норму 20-го, до кінця місяця відкриває
   апку під конфеті, і за тиждень це дратує сильніше за червоний кружечок. */
function celebrate() {
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const box = document.createElement("div");
  box.className = "confetti";
  const colors = ["var(--cat1)", "var(--cat2)", "var(--cat3)", "var(--good)", "var(--blue)"];
  for (let i = 0; i < 32; i++) {
    const p = document.createElement("i");
    p.style.left = `${Math.round(Math.random() * 100)}%`;
    p.style.background = colors[i % colors.length];
    p.style.animationDelay = `${(Math.random() * 0.5).toFixed(2)}s`;
    p.style.animationDuration = `${(1.6 + Math.random() * 0.9).toFixed(2)}s`;
    p.style.transform = `rotate(${Math.round(Math.random() * 360)}deg)`;
    box.appendChild(p);
  }
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 2800);
}

/* Пам'ять святкування — у localStorage, бо це вкус момента, а не дані:
   ключ «місяць + норма», тож новий місяць святкується заново, а той самий —
   ніколи двічі. Без localStorage (приватний режим) просто не святкуємо. */
function celebrateOnce(key) {
  try {
    if (localStorage.getItem(`kpiCheers:${key}`)) return;
    localStorage.setItem(`kpiCheers:${key}`, "1");
  } catch (e) { return; }
  haptic("success");
  celebrate();
}

/* Кільце з фото журналістки: заповнення — це рух крізь період (факт від
   цілі), засічка — де вона мала б бути сьогодні, колір — темп. */
function meRingHtml(info) {
  // Поки нічого не зроблено і нічого ще не чекають (перші дні місяця), «0%»
  // не пишемо: цифра тут не несе змісту, а читається як докір. Замість неї
  // говорить фраза — рівно те, чого просив Олег.
  const pct = info && !(info.pace === "start" && !info.fact) ? info.pct : null;
  return `
    ${avatarRing(viewPerson().name, viewPerson().entry, pct, 92,
      info ? PACE_COLOR[info.pace] || "var(--good)" : "var(--good)",
      { mark: info ? info.mark : null, animate: true })}
    <div class="me-cap">
      ${pct == null ? "" : `<b style="color:${PACE_COLOR[info.pace] || "var(--good)"}">${pct}%</b>`}
      <span>${esc(info && info.label ? `за ${info.label}` : "цього місяця")}</span>
    </div>`;
}

/* «Мої KPI» журналістки: норми її відділу зі своїм фактом тижня/місяця.
   Вантажиться після основного екрана — щоб таски не чекали на MySQL сайту. */
/* «Що далі» — конкретика замість відсотка. Відсоток каже, як усе, а людині
   треба знати, що зробити: скільки лишилось, чим це можна закрити і що вже
   стоїть у чергу з дедлайном. */
function nextStepsHtml(norm, r) {
  if (!r || !r.remaining) return "";
  const bits = [`<b>${r.remaining} ${esc(qtyWord(norm.metric, r.remaining))}</b> до норми`];
  if (norm.metric === "news" && r.article_weight > 1) {
    bits.push(`одна стаття закриє ${r.article_weight}`);
  }
  return `<div class="pace-next">${bits.join(" · ")}</div>`;
}

async function renderMyKpi() {
  const me = viewPerson();
  let k;
  try {
    k = await api("/api/kpi" + (me.preview
      ? `?person=${encodeURIComponent(me.name)}` : ""));
  } catch (e) {
    // Заглушку треба прибрати навіть при збої — інакше вона шимеріла б вічно
    const box0 = $("my-kpi");
    if (box0) box0.innerHTML = "";
    return;
  }
  if (me.preview && STATE.view !== "preview") return;
  // Обличчя екрана — МІСЯЧНА норма: тижнева стрибає надто різко (у вівторок
  // там завжди буде мало). Беремо провідну, а не середнє: фраза і засічка
  // мають говорити про одну конкретну норму, інакше вони ні про що.
  const monthly = k.norms.filter((n) => n.period === "month");
  const lead = monthly.find((n) => n.rows[0] && n.rows[0].fact !== null) || monthly[0];
  const lr = lead && lead.rows[0];
  const live = lr && lr.fact !== null && !lr.excused;
  const pct = live && lr.target > 0 ? Math.min(100, Math.round(lr.fact / lr.target * 100))
    : (live && lr.target <= 0 ? 100 : null);
  const mark = live && lr.target > 0 && lr.expected != null
    ? Math.min(100, Math.round(lr.expected / lr.target * 100)) : null;

  const ring = $("me-ring");
  if (ring) {
    ring.innerHTML = meRingHtml(live
      ? { pct, mark, pace: lr.pace, fact: lr.fact, label: k.month_label }
      : null);
    animateFill(ring);
  }
  // Двері «KPI по місяцях» отримують своє число — інакше це просто пункт меню
  paintPromiseDoor();
  const histDoor = document.querySelector('.door[data-nav="myhist"]');
  if (histDoor && pct != null && !histDoor.querySelector(".door-n")) {
    const n = document.createElement("span");
    n.className = "door-n";
    n.textContent = `${pct}%`;
    histDoor.insertBefore(n, histDoor.querySelector(".chev"));
  }

  const box = $("my-kpi");
  if (!box) return;
  if (!k.norms.length) { box.innerHTML = ""; return; }
  const pace = lead && lead.pace;
  const phrase = live && pace
    ? pacePhrase(lr.pace, pace.phase, monthWord(k.month_label)) : null;

  box.innerHTML = `
    ${phrase ? `<div class="pace-card">
      <div class="pace-say">${esc(phrase)}${
        lr.expected ? helpBtn("pace") : ""}</div>
      ${stepsHtml(lead && lead.week_steps, k.month_label)}
      ${nextStepsHtml(lead, lr)}
    </div>` : ""}
    <div class="soft-card"><div class="sc-t">Мої KPI${
      k.norms.some((n) => (n.rows[0] || {}).articles) ? helpBtn("weight") : ""}</div>
    ${k.norms.map((n) => {
      const r = n.rows[0];
      if (!r) return "";
      if (r.excused) return `<div class="mykpi-row">
        <span class="mk-t">${esc(normTitle(n, r))}</span>
        <span class="kp-excused">звільнено${r.note ? " · " + esc(r.note) : ""}</span></div>`;
      const p = r.fact === null || r.target <= 0 ? 0 : Math.min(100, Math.round(r.fact / r.target * 100));
      const m = r.expected != null && r.target > 0
        ? Math.min(100, Math.round(r.expected / r.target * 100)) : null;
      return `<div class="mykpi-row">
        <span class="mk-t">${esc(normTitle(n, r))}
          <span class="mk-p">· ${esc(n.period === "week" ? k.week_label : k.month_label)}</span>
          ${normWhy(n, r)}${articleHint(r)}${feedHint(r)}${ownHint(r)}</span>
        <span class="kp-fact ${r.done ? "ok" : ""}">${r.fact === null ? "—" : `${r.fact}/${r.target}${r.done ? " ✓" : ""}`}</span>
        <span class="kbar wide">
          <i class="${r.done ? "ok" : ""}" data-fill="${p}%" style="width:0%"></i>
          ${m != null && m > 0 && m < 100 ? `<u style="left:${m}%"></u>` : ""}
        </span>
      </div>`;
    }).join("")}</div>`;
  animateFill(box);
  wireHelp(box);

  // Салют — рівно на перетин норми, один раз на місяць і норму. У
  // менеджерському перегляді не стріляє: норма чужа, а ключ пам'яті згорів би
  // на телефоні Олега замість телефона журналістки.
  if (!me.preview && lr && lr.done && lead.period === "month") {
    celebrateOnce(`${k.month_label}:${lead.id}`);
  }
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
/* Число на двері «Банк тем» у журналістки. Доганяємо окремим легким запитом
   після відмальовки: головна й так вантажить KPI з БД сайту, а двері без
   числа не відкривають — «Банк тем» звучить як чужа адміністративна штука,
   «Банк тем · 3 прострочені» як особиста справа. */
async function paintPromiseDoor() {
  const door = document.querySelector('.door[data-nav="promises"]');
  if (!door || door.querySelector(".door-n")) return;
  let d;
  try {
    d = await api("/api/promises/mine/count");
  } catch (e) { return; }
  if (!d || !d.total) return;
  const meta = door.querySelector(".door-m");
  if (meta) {
    meta.textContent = d.overdue
      ? `${d.overdue} ${plural(d.overdue, "прострочена", "прострочені", "прострочених")}`
        + ` з твоїх матеріалів`
      : "обіцянки з твоїх матеріалів";
  }
  const n = document.createElement("span");
  n.className = "door-n";
  n.textContent = String(d.total);
  door.insertBefore(n, door.querySelector(".chev") || null);
}

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
    render();
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
    $("bottomnav").classList.remove("hidden");
    if (STATE.me.manager) {
      // startapp із прямого лінка: кнопка «Дедлайни в апці» під нагадуванням
      // у чаті фінансів має відкривати одразу «Звітність», а не Головну
      // Куди відкривати: ?screen= від web_app-кнопки (пінг Лиса) або
      // start_param від t.me-лінка (кнопка «Дедлайни в апці» у чаті фінансів)
      const start = (tg.initDataUnsafe || {}).start_param || "";
      const deep = promiseDeepLink(start);
      if (deep) nav("promise", deep);
      else nav(startScreen() || (start === "reports" ? "reports"
        : start === "promises" ? "promises" : "home"));
    } else {
      // Журналістці лінк із «винюхав» теж має відкривати банк одразу
      const start = (tg.initDataUnsafe || {}).start_param || "";
      const deep = promiseDeepLink(start);
      if (deep) nav("promise", deep);
      else nav(startScreen() || (start === "promises" ? "promises" : "home"));
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
  // Універсальне «назад» — окремим атрибутом, а НЕ data-nav="back": на
  // [data-nav] висить оцей делегат, і він перебивав би локальний обробник,
  // намагаючись перейти на неіснуючий екран «back».
  if (e.target.closest("[data-back]")) { goBack(); return; }
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
