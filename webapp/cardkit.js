/*
 * cardkit.js — canvas-ядро генератора каруселей (/carousel).
 *
 * ЧОМУ ОКРЕМИЙ ФАЙЛ, А НЕ КОД УСЕРЕДИНІ carousel.html. Спершу планувалось
 * винести сюди спільне ядро з card.html і підключити в обидві сторінки, але
 * рішення Олега 17.08.2026: «CARD не трогал бы, он останется для коротких
 * простых картинок». Це свідомий розмін: /card — робочий щоденний інструмент,
 * і рефакторити його заради новонародженої сторінки означає ризикувати тим,
 * що працює, без жодної користі для людини за екраном.
 *
 * ЦІНА РОЗМІНУ, щоб вона не була сюрпризом: механіки нижче тепер живуть у
 * ДВОХ місцях — тут і всередині webapp/card.html. Головна з них — фолбек
 * Safari для ctx.filter. Ламатиметься знову — лагодити треба ОБИДВА файли;
 * шукати за словом SUPPORTS_FILTER.
 *
 * Що тут зібрано і що це вже коштувало:
 *
 *   • paintPhoto — псевдокроп фото у слоті з обробкою (яскравість, контраст,
 *     насиченість, тепло, виразність) і ФОЛБЕКОМ ДЛЯ SAFARI. У WebKit
 *     ctx.filter приймається і читається назад, але при малюванні мовчки не
 *     діє, тож перевірка «чи є властивість» брехала. Питаємо РЕЗУЛЬТАТ:
 *     малюємо білий піксель крізь brightness(0%) і дивимось, чи почорнів.
 *     Немає фільтра — рахуємо пікселі руками (LUT + Rec.601).
 *   • обробка йде в ОФСКРІН-канвас: putImageData ігнорує кліп, і без
 *     офскріна круглий кроп ставав би квадратним.
 *   • setFont/wrap/paintLine — трекінг Commissioner і плашки виділення з
 *     псевдопробілами по краях рану.
 *   • createAdjuster — модалка обробки фото цілком (розмітка, стилі,
 *     повзунки, пресети, драг і зум прямо в прев'ю).
 *   • zipStore — ZIP БЕЗ стиснення, ~60 рядків: JPEG уже стиснутий, тягнути
 *     заради архіву бібліотеку з CDN немає жодних підстав.
 *
 * Без збірки: звичайний <script src>, усе в window.CardKit.
 */
/* eslint-disable no-unused-vars */
"use strict";
window.CardKit = (() => {

  // ---------- Бренд ----------
  // 4 фони, і в кожного СВОЯ плашка виділення: біла плашка на білому фоні
  // невидима, тому на світлих фонах виділення — брендовий синій із білими
  // буквами; на синьому й чорному — біла плашка з буквами кольору фону.
  const SCHEMES = [
    { name: "Синій #3478F9", bg: "#3478f9", text: "#ffffff", boxBg: "#ffffff", boxText: "#3478f9", dark: true },
    { name: "Чорний",        bg: "#000000", text: "#ffffff", boxBg: "#ffffff", boxText: "#000000", dark: true },
    { name: "Білий",         bg: "#ffffff", text: "#000000", boxBg: "#3478f9", boxText: "#ffffff", dark: false },
    { name: "Лавандовий #C9CFF2", bg: "#c9cff2", text: "#000000", boxBg: "#3478f9", boxText: "#ffffff", dark: false },
  ];
  const BRAND = "#3478f9";
  const LOGO_URL = "https://nikvesti.com/images/logos/logo-ua.svg";

  // ---------- Обробка фото ----------
  function newAdj() {
    return { bright: 100, contrast: 100, sat: 100, warm: 0, vivid: 0, blur: 0 };
  }

  // Пресети — не «інстаграмні», а те, що реально рятує газетне фото:
  // темний кадр, плаский пасмурний, ч/б для важких тем
  const PRESETS = {
    none:     { label: "Оригінал",   adj: { bright: 100, contrast: 100, sat: 100, warm: 0, vivid: 0, blur: 0 } },
    pop:      { label: "Соковитий",  adj: { bright: 103, contrast: 112, sat: 125, warm: 6, vivid: 20, blur: 0 } },
    light:    { label: "Освітлити",  adj: { bright: 118, contrast: 104, sat: 105, warm: 4, vivid: 10, blur: 0 } },
    contrast: { label: "Контраст",   adj: { bright: 98,  contrast: 130, sat: 105, warm: 0, vivid: 25, blur: 0 } },
    warm:     { label: "Теплий",     adj: { bright: 104, contrast: 104, sat: 110, warm: 34, vivid: 8, blur: 0 } },
    cool:     { label: "Холодний",   adj: { bright: 102, contrast: 106, sat: 100, warm: -32, vivid: 8, blur: 0 } },
    mute:     { label: "Приглушений",adj: { bright: 102, contrast: 96,  sat: 72,  warm: 8, vivid: 0, blur: 0 } },
    bw:       { label: "Чорно-біле", adj: { bright: 102, contrast: 112, sat: 0,   warm: 0, vivid: 12, blur: 0 } },
  };

  const ADJ_KEYS = [
    { key: "bright",   label: "Яскравість",  min: 50, max: 150, base: 100 },
    { key: "contrast", label: "Контраст",    min: 50, max: 150, base: 100 },
    { key: "sat",      label: "Насиченість", min: 0,  max: 200, base: 100 },
    { key: "warm",     label: "Тепло",       min: -60, max: 60, base: 0 },
    { key: "vivid",    label: "Виразність",  min: 0,  max: 60,  base: 0 },
    { key: "blur",     label: "Розмиття",    min: 0,  max: 40,  base: 0 },
  ];

  function isNeutral(a) {
    return a.bright === 100 && a.contrast === 100 && a.sat === 100
      && !a.warm && !a.vivid && !a.blur;
  }

  // Safari приймає ctx.filter (записав — прочитав те саме) і мовчки не
  // застосовує його при малюванні. Тому питаємо не властивість, а результат.
  const SUPPORTS_FILTER = (() => {
    try {
      const c = document.createElement("canvas");
      c.width = c.height = 1;
      const x = c.getContext("2d", { willReadFrequently: true });
      x.filter = "brightness(0%)";
      x.fillStyle = "#fff";
      x.fillRect(0, 0, 1, 1);
      return x.getImageData(0, 0, 1, 1).data[0] < 40;
    } catch (e) {
      return false; // не змогли перевірити — рахуємо пікселі самі, це надійніше
    }
  })();

  const _off = document.createElement("canvas");
  const _octx = _off.getContext("2d", { willReadFrequently: !SUPPORTS_FILTER });

  // Ручні яскравість/контраст/насиченість (фолбек для Safari).
  // LUT на 256 значень для яскравості+контрасту, насиченість — лерп до
  // яскравісної складової (Rec. 601), як це робить CSS-фільтр saturate.
  function applyPixelAdjust(c, w, h, a) {
    const img = c.getImageData(0, 0, w, h);
    const d = img.data;
    const b = a.bright / 100, ct = a.contrast / 100, sat = a.sat / 100;
    const lut = new Uint8ClampedArray(256);
    for (let i = 0; i < 256; i++) {
      lut[i] = Math.max(0, Math.min(255, ((i * b - 128) * ct) + 128));
    }
    for (let i = 0; i < d.length; i += 4) {
      let r = lut[d[i]], g = lut[d[i + 1]], bl = lut[d[i + 2]];
      if (sat !== 1) {
        const lum = 0.299 * r + 0.587 * g + 0.114 * bl;
        r = lum + (r - lum) * sat;
        g = lum + (g - lum) * sat;
        bl = lum + (bl - lum) * sat;
      }
      d[i] = r; d[i + 1] = g; d[i + 2] = bl;
    }
    c.putImageData(img, 0, 0);
  }

  // Розмиття для Safari, де ctx.filter не діє. Зменшуємо кадр у стільки
  // разів, скільки просять радіуса, і повертаємо назад: інтерполяція
  // браузера дає ту саму мильну пляму, тільки без фільтра. Той самий прийом,
  // що добудовує обрізаний край кадру в carousel.html.
  const _blurScratch = document.createElement("canvas");
  const _blurScratchX = _blurScratch.getContext("2d");

  const _blurScratch2 = document.createElement("canvas");
  const _blurScratch2X = _blurScratch2.getContext("2d");

  /**
   * Розмиття для Safari, де ctx.filter не діє.
   *
   * ПОЕТАПНЕ зменшення вдвічі, а не одне різке. Одноразовий стрибок «зменшити
   * у 20 разів і повернути» дає не розмиття, а квадрати: браузер бере
   * поодинокі пікселі, і на екрані видно мозаїку (спіймано Олегом 17.08 —
   * «що це за колхозний блюр з пікселями?»). Кожне ж халвання усереднює
   * чотири сусідні пікселі, тобто працює як мipmap: п'ять кроків поспіль
   * дають плавну пляму, близьку до гаусової.
   */
  function manualBlur(c, w, h, radius) {
    let sw = Math.max(1, Math.round(w)), sh = Math.max(1, Math.round(h));
    const target = Math.max(1, w / Math.max(2, radius));
    let src = c.canvas, srcW = sw, srcH = sh;
    let a = [_blurScratch, _blurScratchX], b = [_blurScratch2, _blurScratch2X];

    while (sw > target && sw > 2) {
      sw = Math.max(1, Math.round(sw / 2));
      sh = Math.max(1, Math.round(sh / 2));
      a[0].width = sw; a[0].height = sh;
      a[1].setTransform(1, 0, 0, 1, 0, 0);
      a[1].imageSmoothingEnabled = true;
      a[1].clearRect(0, 0, sw, sh);
      a[1].drawImage(src, 0, 0, srcW, srcH, 0, 0, sw, sh);
      src = a[0]; srcW = sw; srcH = sh;
      [a, b] = [b, a];        // канваси чергуються, щоб не читати й писати в один
    }

    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, w, h);
    c.imageSmoothingEnabled = true;
    c.drawImage(src, 0, 0, srcW, srcH, 0, 0, w, h);
  }

  /**
   * Де саме ляже кадр у слоті (координати канваса). Потрібне не лише
   * рендеру: коли фото зсунуте за межі слота, той, хто малює, має знати, яку
   * смугу лишилось закрити (див. заповнення градієнтом у carousel.html).
   */
  function photoDrawRect(st, rect) {
    const sf = Math.max(rect.w / st.photo.naturalWidth,
                        rect.h / st.photo.naturalHeight) * (st.zoom || 1);
    const dw = st.photo.naturalWidth * sf, dh = st.photo.naturalHeight * sf;
    return {
      x: rect.x + rect.w / 2 - dw / 2 + (st.offX || 0),
      y: rect.y + rect.h / 2 - dh / 2 + (st.offY || 0),
      w: dw, h: dh,
    };
  }

  /**
   * Малює фото слота у вже накладений кліп, із його обробкою.
   * st — {photo, zoom, offX, offY, adj}; rect — слот у координатах канваса.
   */
  function paintPhoto(c, st, rect) {
    const a = st.adj || newAdj();
    const drawn = photoDrawRect(st, rect);
    const dw = drawn.w, dh = drawn.h;
    const dx = drawn.x - rect.x;   // координати ВСЕРЕДИНІ слота
    const dy = drawn.y - rect.y;

    if (isNeutral(a)) { // без обробки — без зайвого проміжного канваса
      c.drawImage(st.photo, rect.x + dx, rect.y + dy, dw, dh);
      return;
    }

    _off.width = Math.round(rect.w);
    _off.height = Math.round(rect.h);
    _octx.setTransform(1, 0, 0, 1, 0, 0);
    _octx.clearRect(0, 0, _off.width, _off.height);

    // Розмиття «з'їдає» край кадру: фільтр підмішує туди прозорість, і в
    // слоті з'явилась би світла кайма. Тому при розмитті малюємо кадр із
    // запасом на кілька радіусів у кожен бік — кайма лишається за кліпом.
    const blur = a.blur || 0;
    const pad = blur * 3;

    if (SUPPORTS_FILTER) {
      _octx.filter = `brightness(${a.bright}%) contrast(${a.contrast}%) `
        + `saturate(${a.sat}%)` + (blur ? ` blur(${blur}px)` : "");
      _octx.drawImage(st.photo, dx - pad, dy - pad, dw + pad * 2, dh + pad * 2);
      _octx.filter = "none";
    } else {
      _octx.drawImage(st.photo, dx - pad, dy - pad, dw + pad * 2, dh + pad * 2);
      if (a.bright !== 100 || a.contrast !== 100 || a.sat !== 100) {
        applyPixelAdjust(_octx, _off.width, _off.height, a);
      }
      if (blur) manualBlur(_octx, _off.width, _off.height, blur);
    }

    if (a.warm) {
      _octx.globalCompositeOperation = "soft-light";
      _octx.globalAlpha = Math.min(0.85, Math.abs(a.warm) / 60 * 0.8);
      _octx.fillStyle = a.warm > 0 ? "#ff9a3c" : "#3ca8ff";
      _octx.fillRect(0, 0, _off.width, _off.height);
      _octx.globalAlpha = 1;
      _octx.globalCompositeOperation = "source-over";
    }
    if (a.vivid) {
      // «Виразність» — м'який overlay самого кадру: підіймає локальний
      // контраст, не вибілюючи світлі ділянки, як робить contrast
      _octx.globalCompositeOperation = "overlay";
      _octx.globalAlpha = a.vivid / 100;
      _octx.drawImage(_off, 0, 0);
      _octx.globalAlpha = 1;
      _octx.globalCompositeOperation = "source-over";
    }

    c.drawImage(_off, rect.x, rect.y);
  }

  /**
   * Межі зсуву кадру. `slack` — наскільки дозволено вийти ЗА край слота
   * (частка від його розміру). Нуль означає класичне «порожнечі не буває».
   *
   * Ненульовий slack потрібен там, де порожнечу є чим закрити: у каруселі
   * фото можна підняти вище краю, а низ добудовується градієнтом кольору
   * самого кадру (Олег, 17.08: «невже не можна понад норму вище потягнути, а
   * недостачу компенсувати градієнтом?»). Без цього кадр упирається в свою
   * висоту, і композицію не поправити.
   */
  function clampSlot(st, rect, slack = 0) {
    if (!st.photo) return;
    const s = Math.max(rect.w / st.photo.naturalWidth,
                       rect.h / st.photo.naturalHeight) * (st.zoom || 1);
    const dw = st.photo.naturalWidth * s, dh = st.photo.naturalHeight * s;
    const maxX = (dw - rect.w) / 2 + rect.w * slack;
    const maxY = (dh - rect.h) / 2 + rect.h * slack;
    st.offX = Math.min(maxX, Math.max(-maxX, st.offX || 0));
    st.offY = Math.min(maxY, Math.max(-maxY, st.offY || 0));
  }

  // ---------- Текст ----------

  // Легкий трекінг 0.02 кегля: без нього букви Commissioner Bold злипаються.
  // letterSpacing — властивість контексту і враховується в measureText.
  function setFont(c, size, weight) {
    c.font = `${weight} ${size}px Commissioner, sans-serif`;
    c.letterSpacing = (size * 0.02).toFixed(1) + "px";
  }
  const lineH = (size) => size * 1.34;

  /** Текст → токени; збіг за індексом+текстом зберігає виділення при правці. */
  function tokenize(text, prev) {
    prev = prev || [];
    const toks = [];
    let pi = 0;
    String(text || "").split("\n").forEach((line, li) => {
      if (li > 0) { toks.push({ br: true }); if (prev[pi] && prev[pi].br) pi++; }
      line.split(/\s+/).filter(Boolean).forEach((w) => {
        const keep = prev[pi] && !prev[pi].br && prev[pi].text === w ? prev[pi].hl : false;
        toks.push({ text: w, hl: keep });
        pi++;
      });
    });
    return toks;
  }

  /** Прості токени без виділень (підписи, абзаци). */
  function tokenizePlain(text) {
    const toks = [];
    String(text || "").split("\n").forEach((line, li) => {
      if (li > 0) toks.push({ br: true });
      line.split(/\s+/).filter(Boolean).forEach((w) => toks.push({ text: w, hl: false }));
    });
    return toks;
  }

  const runPadOf = (size) => size * 0.28;

  // Коробка плашки виділення рахується з МЕТРИК ШРИФТА, а не з підібраних
  // констант. Причина (Олег, 17.08): на плашці «обвалився фасад» хвіст «ф»
  // майже торкався низу, а над «і» лишалась зайва порожнеча — тобто плашка
  // стояла не там, де ink шрифта. fontBoundingBox* при textBaseline="middle"
  // дає відстані рівно від тієї лінії, по якій ми малюємо, тож плашка сідає
  // симетрично навколо літер із будь-яким кеглем.
  //
  // Метрики лінійні за кеглем, тому міряємо раз на вагу і масштабуємо.
  const _plateCache = {};
  function plateBox(c, size, weight = 700) {
    let m = _plateCache[weight];
    if (!m) {
      const saved = c.font, savedBase = c.textBaseline;
      c.textBaseline = "middle";
      c.font = `${weight} 100px Commissioner, sans-serif`;
      const t = c.measureText("Ніфр");   // висхідні, низхідні і кирилиця
      const asc = t.fontBoundingBoxAscent, desc = t.fontBoundingBoxDescent;
      m = (asc > 0 && desc > 0)
        ? { asc: asc / 100, desc: desc / 100 }
        : { asc: 0.55, desc: 0.69 };     // фолбек: старі константи /card
      _plateCache[weight] = m;
      c.font = saved;
      c.textBaseline = savedBase;
    }
    const pad = size * 0.06;
    return { top: -(m.asc * size + pad), height: (m.asc + m.desc) * size + pad * 2 };
  }

  /** Мінімальний міжрядок, за якого плашки сусідніх рядків НЕ злипаються. */
  function minLineH(c, size, weight = 700) {
    return plateBox(c, size, weight).height * 1.06;
  }

  function wrap(c, words, size, maxW, weight = 700, spaceMul = 1.10) {
    setFont(c, size, weight);
    const spaceW = c.measureText(" ").width * spaceMul;
    const runPad = runPadOf(size); // те саме повітря країв плашки, що в рендері
    const lines = [];
    let cur = [], curW = 0;
    for (let n = 0; n < words.length; n++) {
      const t = words[n];
      if (t.br) {
        if (cur.length) lines.push(cur);
        cur = []; curW = 0;
        continue;
      }
      const next = words[n + 1];
      const w = c.measureText(t.text).width;
      // Крайні слова рану ширші на псевдопробіл — інакше плашка вилазила б за поля
      let adv = w;
      if (t.hl && (!cur.length || !cur[cur.length - 1].hl)) adv += runPad;
      if (t.hl && (!next || next.br || !next.hl)) adv += runPad;
      if (cur.length && curW + spaceW + adv > maxW) {
        lines.push(cur);
        // на новому рядку слово-продовження рану знову відкриває плашку зліва
        adv = w + (t.hl ? runPad : 0) +
              (t.hl && (!next || next.br || !next.hl) ? runPad : 0);
        cur = [t]; curW = adv;
      } else {
        curW += (cur.length ? spaceW : 0) + adv;
        cur.push(t);
      }
    }
    if (cur.length) lines.push(cur);
    return lines;
  }

  /**
   * Розкладка одного рядка: позиції слів з урахуванням псевдопробілів на
   * краях плашок. Шрифт має бути вже встановлений (setFont).
   */
  function layoutLine(c, line, size, spaceMul = 1.10) {
    const widths = line.map((t) => c.measureText(t.text).width);
    const spaceW = c.measureText(" ").width * spaceMul;
    const runPad = runPadOf(size);
    const runStart = (k) => line[k].hl && (k === 0 || !line[k - 1].hl);
    const runEnd = (k) => line[k].hl && (k === line.length - 1 || !line[k + 1].hl);
    const xs = [];
    let cursor = 0;
    line.forEach((t, k) => {
      if (k > 0) cursor += spaceW;
      if (runStart(k)) cursor += runPad;
      xs.push(cursor);
      cursor += widths[k];
      if (runEnd(k)) cursor += runPad;
    });
    return { xs, widths, total: cursor, runPad };
  }

  /**
   * Малює рядок: спершу суцільні плашки по ранах підряд виділених слів,
   * потім самі слова. y — середина рядка (textBaseline має бути "middle").
   */
  function paintLine(c, line, opts) {
    const { x0, y, size, boxBg, boxText, color, spaceMul = 1.10,
            weight = 700, radius = 0 } = opts;
    const { xs, widths, runPad } = layoutLine(c, line, size, spaceMul);
    const box = plateBox(c, size, weight);
    let i = 0;
    while (i < line.length) {
      if (line[i].hl) {
        let j = i;
        while (j < line.length && line[j].hl) j++;
        const left = x0 + xs[i] - runPad;
        const right = x0 + xs[j - 1] + widths[j - 1] + runPad;
        c.fillStyle = boxBg;
        if (radius) {
          c.beginPath();
          c.roundRect(left, y + box.top, right - left, box.height, radius);
          c.fill();
        } else {
          c.fillRect(left, y + box.top, right - left, box.height);
        }
        i = j;
      } else i++;
    }
    line.forEach((t, k) => {
      c.fillStyle = t.hl ? boxText : color;
      c.fillText(t.text, x0 + xs[k], y);
    });
  }

  // ---------- Лого ----------
  // SVG-лого біле; на світлих фонах перефарбовуємо в колір тексту, інакше
  // вордмарк просто невидимий.
  const _logoTints = {};
  function logoTinted(logo, color) {
    if (!logo || color === "#ffffff") return logo;
    if (!_logoTints[color]) {
      const c = document.createElement("canvas");
      c.width = logo.naturalWidth; c.height = logo.naturalHeight;
      const cc = c.getContext("2d");
      cc.drawImage(logo, 0, 0);
      cc.globalCompositeOperation = "source-in";
      cc.fillStyle = color;
      cc.fillRect(0, 0, c.width, c.height);
      _logoTints[color] = c;
    }
    return _logoTints[color];
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("не завантажилось: " + src));
      img.src = src;
    });
  }

  // ---------- Модалка обробки фото ----------

  const MODAL_CSS = `
.ck-modal { position: fixed; inset: 0; z-index: 60; background: rgba(6,10,20,.88);
  display: flex; align-items: center; justify-content: center; padding: 20px;
  font-family: Commissioner, system-ui, sans-serif; }
/* display:flex перебиває атрибут hidden — без цього закрита модалка
   лишається невидимо поверх сторінки і з'їдає кліки */
.ck-modal[hidden] { display: none; }
.ck-box { background: #10182c; color: #eef2fb; border-radius: 18px; padding: 18px;
  width: min(980px, 100%); max-height: 100%; overflow: auto; display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(300px, 420px);
  grid-template-areas: "head head" "pic ctrl"; gap: 16px 20px; }
@media (max-width: 780px) {
  .ck-box { grid-template-columns: 1fr; grid-template-areas: "head" "pic" "ctrl"; }
}
.ck-head { grid-area: head; display: flex; align-items: center; gap: 12px;
  font-size: 13px; text-transform: uppercase; letter-spacing: .06em;
  color: #93a4c8; font-weight: 600; }
.ck-x { margin-left: auto; background: #1b2540; color: #cdd8ef; padding: 6px 12px;
  border: 0; border-radius: 8px; font: inherit; font-size: 15px; cursor: pointer; }
.ck-pic { grid-area: pic; width: 100%; height: auto; border-radius: 12px;
  display: block; background: #0a1020; cursor: grab; touch-action: none; }
.ck-pic.dragging { cursor: grabbing; }
.ck-ctrl { grid-area: ctrl; }
.ck-tabs { display: inline-flex; background: #1b2540; border-radius: 10px; padding: 3px; gap: 3px; }
.ck-tabs[hidden] { display: none; }
.ck-tabs button { padding: 7px 14px; border: 0; border-radius: 8px; font: inherit;
  font-size: 13px; background: transparent; color: #cdd8ef; cursor: pointer; }
.ck-tabs button.on { background: #2c3a63; color: #fff; }
.ck-presets { display: flex; flex-wrap: wrap; gap: 8px; }
.ck-presets button { background: #1b2540; color: #dfe7f7; border: 0; border-radius: 10px;
  padding: 8px 12px; font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer; }
.ck-presets button.on { background: #fff; color: ${BRAND}; }
.ck-sliders { display: grid; grid-template-columns: auto 1fr auto; gap: 8px 12px;
  align-items: center; margin-top: 16px; }
.ck-sliders label { font-size: 13px; color: #93a4c8; white-space: nowrap; }
.ck-sliders output { font-size: 12px; color: #93a4c8; min-width: 42px; text-align: right; }
.ck-sliders input[type=range] { width: 100%; accent-color: ${BRAND}; }
.ck-actions { display: flex; gap: 10px; margin-top: 18px; }
.ck-actions button { flex: 1; padding: 12px; border: 0; border-radius: 10px; font: inherit;
  font-weight: 600; cursor: pointer; background: ${BRAND}; color: #fff; }
.ck-actions button.ghost { background: #1b2540; color: #cdd8ef; }
`;

  let _cssInjected = false;
  function injectCss() {
    if (_cssInjected) return;
    const st = document.createElement("style");
    st.textContent = MODAL_CSS;
    document.head.appendChild(st);
    _cssInjected = true;
  }

  /**
   * Модалка обробки фото — одна на обидві сторінки.
   *
   * opts:
   *   getSlot()  → {photo, zoom, offX, offY, adj, preset} — що правимо зараз
   *   getRect()  → {x,y,w,h} слота цього фото (прев'ю показує РІВНО той кадр,
   *                що піде в картинку)
   *   onChange() → перемалювати сторінку
   *   tabs()     → {items:[…], active, onPick(i)} або null (перемикач слотів)
   *   title      → підпис угорі
   */
  function createAdjuster(opts) {
    injectCss();
    const el = document.createElement("div");
    el.className = "ck-modal";
    el.hidden = true;
    el.innerHTML = `
      <div class="ck-box">
        <div class="ck-head">
          <div class="ck-title">${opts.title || "Обробка фото"}</div>
          <div class="ck-tabs" hidden></div>
          <button class="ck-x" title="Закрити">✕</button>
        </div>
        <canvas class="ck-pic" width="900" height="900"></canvas>
        <div class="ck-ctrl">
          <div class="ck-presets"></div>
          <div class="ck-sliders"></div>
          <div class="ck-actions">
            <button class="ghost ck-reset">Скинути</button>
            <button class="ck-done">Готово</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(el);

    const q = (sel) => el.querySelector(sel);
    const canvas = q(".ck-pic"), c = canvas.getContext("2d");
    const tabsBox = q(".ck-tabs");

    Object.entries(PRESETS).forEach(([key, pr]) => {
      const b = document.createElement("button");
      b.dataset.preset = key;
      b.textContent = pr.label;
      b.addEventListener("click", () => {
        const st = opts.getSlot();
        if (!st) return;
        st.adj = { ...pr.adj };
        st.preset = key;
        sync(); opts.onChange();
      });
      q(".ck-presets").appendChild(b);
    });

    const inputs = {};
    ADJ_KEYS.forEach((spec) => {
      const label = document.createElement("label");
      label.textContent = spec.label;
      const input = document.createElement("input");
      input.type = "range";
      input.min = spec.min; input.max = spec.max; input.value = spec.base;
      const out = document.createElement("output");
      out.textContent = "0";
      input.addEventListener("input", () => {
        const st = opts.getSlot();
        if (!st) return;
        st.adj[spec.key] = +input.value;
        st.preset = "custom";  // ручна правка знімає позначку пресета
        sync(); opts.onChange();
      });
      q(".ck-sliders").append(label, input, out);
      inputs[spec.key] = { input, out, spec };
    });

    q(".ck-reset").addEventListener("click", () => {
      const st = opts.getSlot();
      if (!st) return;
      st.adj = newAdj(); st.preset = "none";
      sync(); opts.onChange();
    });
    q(".ck-done").addEventListener("click", close);
    q(".ck-x").addEventListener("click", close);
    el.addEventListener("click", (e) => { if (e.target === el) close(); });
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !el.hidden) close();
    });

    function sync() {
      const st = opts.getSlot();
      if (!st) return;
      const a = st.adj || newAdj();
      Object.values(inputs).forEach(({ input, out, spec }) => {
        input.value = a[spec.key];
        const d = a[spec.key] - spec.base;
        out.textContent = d > 0 ? "+" + d : String(d);
      });
      el.querySelectorAll(".ck-presets button").forEach((b) =>
        b.classList.toggle("on", b.dataset.preset === st.preset));
      const t = opts.tabs && opts.tabs();
      tabsBox.hidden = !t;
      if (t) {
        tabsBox.textContent = "";
        t.items.forEach((label, i) => {
          const b = document.createElement("button");
          b.textContent = label;
          b.className = i === t.active ? "on" : "";
          b.addEventListener("click", () => { t.onPick(i); sync(); draw(); });
          tabsBox.appendChild(b);
        });
      }
      if (!el.hidden) draw();
    }

    // Прев'ю в модалці — той самий слот, лише в своєму масштабі:
    // видно РІВНО той кадр, який піде в картинку
    function draw() {
      const st = opts.getSlot(), r = opts.getRect();
      if (!r) return;
      const k = Math.min(900 / r.w, 900 / r.h);
      canvas.width = Math.round(r.w * k);
      canvas.height = Math.round(r.h * k);
      c.setTransform(k, 0, 0, k, -r.x * k, -r.y * k);
      c.clearRect(r.x, r.y, r.w, r.h);
      if (st && st.photo) paintPhoto(c, st, r);
      c.setTransform(1, 0, 0, 1, 0, 0);
    }

    // Драг і зум просто в модалці — кроп правиться там само, де дивишся
    let drag = null;
    const toSlot = (e) => {
      const r = opts.getRect();
      const box = canvas.getBoundingClientRect();
      const k = r.w / box.width;
      return { x: (e.clientX - box.left) * k, y: (e.clientY - box.top) * k };
    };
    canvas.addEventListener("pointerdown", (e) => {
      const st = opts.getSlot();
      if (!st || !st.photo) return;
      const p = toSlot(e);
      drag = { x: p.x, y: p.y, offX: st.offX || 0, offY: st.offY || 0 };
      canvas.classList.add("dragging");
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const st = opts.getSlot();
      const p = toSlot(e);
      st.offX = drag.offX + (p.x - drag.x);
      st.offY = drag.offY + (p.y - drag.y);
      clampSlot(st, opts.getRect(), opts.slack || 0);
      draw(); opts.onChange();
    });
    canvas.addEventListener("pointerup", () => {
      drag = null; canvas.classList.remove("dragging");
    });
    canvas.addEventListener("wheel", (e) => {
      const st = opts.getSlot();
      if (!st || !st.photo) return;
      e.preventDefault();
      st.zoom = Math.min(3, Math.max(1, (st.zoom || 1) * (e.deltaY < 0 ? 1.06 : 0.94)));
      clampSlot(st, opts.getRect(), opts.slack || 0);
      draw(); opts.onChange();
      if (opts.onZoom) opts.onZoom();
    }, { passive: false });

    function open() {
      if (!opts.getSlot() || !opts.getSlot().photo) return;
      el.hidden = false;
      sync(); draw();
    }
    function close() { el.hidden = true; }

    return { open, close, sync, draw, el, isOpen: () => !el.hidden };
  }

  // ---------- ZIP без стиснення ----------
  //
  // JPEG уже стиснутий — deflate додав би відсоток розміру й цілу бібліотеку
  // з CDN, якої на сторінці бути не повинно. Store-only архів пишеться руками
  // і читається будь-яким розпакувальником.

  const _crcTable = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = _crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }

  /** files: [{name, bytes: Uint8Array}] → Blob (application/zip). */
  function zipStore(files) {
    const enc = new TextEncoder();
    const chunks = [];      // локальні заголовки + дані
    const central = [];     // центральний каталог
    let offset = 0;

    // Дата в архіві фіксована: інакше два однакові експорти давали б різні
    // байти, а користі від часу створення в такому архіві нуль.
    const dosTime = 0, dosDate = (2026 - 1980) << 9 | (1 << 5) | 1;

    for (const f of files) {
      const name = enc.encode(f.name);
      const crc = crc32(f.bytes);
      const size = f.bytes.length;

      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true);
      local.setUint16(4, 20, true);        // version needed
      local.setUint16(6, 0, true);         // flags
      local.setUint16(8, 0, true);         // method: 0 = store
      local.setUint16(10, dosTime, true);
      local.setUint16(12, dosDate, true);
      local.setUint32(14, crc, true);
      local.setUint32(18, size, true);     // compressed
      local.setUint32(22, size, true);     // uncompressed
      local.setUint16(26, name.length, true);
      local.setUint16(28, 0, true);        // extra
      chunks.push(new Uint8Array(local.buffer), name, f.bytes);

      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true);
      cd.setUint16(4, 20, true);           // version made by
      cd.setUint16(6, 20, true);           // version needed
      cd.setUint16(8, 0, true);
      cd.setUint16(10, 0, true);
      cd.setUint16(12, dosTime, true);
      cd.setUint16(14, dosDate, true);
      cd.setUint32(16, crc, true);
      cd.setUint32(20, size, true);
      cd.setUint32(24, size, true);
      cd.setUint16(28, name.length, true);
      cd.setUint32(42, offset, true);      // зсув локального заголовка
      central.push(new Uint8Array(cd.buffer), name);

      offset += 30 + name.length + size;
    }

    const cdSize = central.reduce((a, b) => a + b.length, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, files.length, true);
    end.setUint16(10, files.length, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);
    return new Blob([...chunks, ...central, new Uint8Array(end.buffer)],
                    { type: "application/zip" });
  }

  function download(blob, name) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  return {
    SCHEMES, BRAND, LOGO_URL, PRESETS, ADJ_KEYS, SUPPORTS_FILTER,
    newAdj, isNeutral, applyPixelAdjust, paintPhoto, photoDrawRect, clampSlot,
    setFont, lineH, tokenize, tokenizePlain, wrap, layoutLine, paintLine,
    plateBox, minLineH,
    logoTinted, loadImage, createAdjuster, zipStore, crc32, download,
  };
})();
