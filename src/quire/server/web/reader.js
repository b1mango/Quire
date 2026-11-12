/* 阅读器：参照「图书」。文字（EPUB/TXT）按 CSS 多栏分页，宽屏双页；漫画（CBZ/ZIP）单页、双页或条漫；
   PDF 交给内嵌的系统 PDF 视图。界面元素鼠标一动就浮现，静止两秒退场。
   后端契约见 server/reader.py：/read、/read/section/n、/read/page/n、/read/pdf、/progress。依赖 app.js。 */
"use strict";

const RD_ICONS = {
  back: '<path d="M10 3.5 5.5 8l4.5 4.5"/>',
  toc: '<path d="M5.5 4.5h8M5.5 8h8M5.5 11.5h8M2.5 4.5h0M2.5 8h0M2.5 11.5h0"/>',
  look: '<path d="M3 13 6.5 3h1L11 13M4.3 9.5h5.4M11.5 13l1.9-5h.2l1.9 5M12.1 11.5h2.8"/>',
  ext: '<path d="M9 3h4v4M13 3 7.5 8.5M11 9.5V13H3V5h3.5"/>',
};
const RD_FONTS = [["Songti SC", "宋体"], ["PingFang SC", "苹方"], ["Kaiti SC", "楷体"], ["Georgia", "Georgia"]];
const RD_TONES = [["paper", "#FFFFFF", "#1D1B18"], ["sepia", "#F6EFE3", "#4A3A28"], ["gray", "#4A4A4C", "#E8E6E1"], ["night", "#121212", "#C9C6C0"]];
const rd = {
  book: null, meta: null, section: 0, page: 0, pages: 1, frac: 0, calmTimer: null, saveTimer: null, loading: 0,
  prefs: Object.assign({ size: 19, font: "Songti SC", tone: "auto", flow: "paged", spread: "auto", rtl: false, comicFlow: "paged" },
    JSON.parse(localStorage.getItem("quire-reader") || "{}")),
};
const icon = (name) => raw(`<svg viewBox="0 0 16 16">${RD_ICONS[name]}</svg>`);
const rdq = (sel) => $("reader").querySelector(sel);
const withToken = (url) => `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}`;

function savePrefs() { localStorage.setItem("quire-reader", JSON.stringify(rd.prefs)); }

/* ------------------------------------------------------------ 打开 / 关闭 */

async function openReader(bookId, format) {
  const book = state.books.find((b) => b.id === bookId) || { id: bookId, title: "" };
  rd.book = book;
  mountReader();
  rdq(".rd-top .t").textContent = book.title;
  showMsg("正在打开…", "");
  try {
    rd.meta = await api(`/api/books/${bookId}/read${format ? `?format=${format}` : ""}`);
  } catch (err) {
    showMsg("这本书暂时打不开", errText(err), true);
    return;
  }
  rdq(".rd-top .t").textContent = rd.meta.title || book.title;
  rdq("[data-rd=toc]").hidden = !(rd.meta.toc || []).length;
  rdq("[data-rd=look]").hidden = rd.meta.mode === "pdf";
  const pos = (rd.meta.progress && rd.meta.progress.position) || "";
  if (rd.meta.mode === "text") {
    const [s, f] = pos.split(":");
    rd.section = Math.min(Math.max(0, parseInt(s, 10) || 0), Math.max(0, rd.meta.sections - 1));
    await loadSection(rd.section, Number(f) || 0);
  } else if (rd.meta.mode === "pages") {
    rd.page = Math.min(Math.max(0, parseInt(pos, 10) || 0), Math.max(0, rd.meta.pages - 1));
    renderPages();
  } else {
    rdq(".rd-stage").innerHTML = html`<iframe class="rd-pdf" title="${rd.meta.title}" src="${withToken(`/api/books/${bookId}/read/pdf`)}"></iframe>`;
    rdq(".rd-bot").hidden = true;
    saveProgress("0", 0);
  }
  wake();
}

function closeReader() {
  flushProgress();
  $("reader").hidden = true;
  rd.book = rd.meta = null;
  rdq(".rd-stage").textContent = "";
  document.activeElement?.blur();
}

function mountReader() {
  let el = $("reader");
  if (!el) {
    el = document.createElement("section");
    el.id = "reader";
    el.className = "reader";
    el.setAttribute("aria-label", "阅读器");
    document.body.appendChild(el);
    el.addEventListener("mousemove", wake);
    el.addEventListener("click", onReaderClick);
    el.addEventListener("input", onReaderInput);
    el.addEventListener("load", () => rd.meta && rd.meta.mode === "text" && rd.prefs.flow === "paged" && remeasure(), true);
  }
  el.innerHTML = html`<div class="rd-top">
      <button class="rd-btn" data-rd="close" title="返回书库（Esc）" aria-label="返回">${icon("back")}</button>
      <button class="rd-btn" data-rd="toc" title="目录（T）" aria-label="目录" aria-pressed="false">${icon("toc")}</button>
      <div class="t"></div>
      <button class="rd-btn" data-rd="look" title="外观" aria-label="外观" aria-pressed="false">${icon("look")}</button>
      <button class="rd-btn" data-rd="ext" title="用系统应用打开" aria-label="用系统应用打开">${icon("ext")}</button></div>
    <div class="rd-stage"></div>
    <div class="rd-bot"><input type="range" min="0" max="1000" value="0" aria-label="阅读位置" data-rd="scrub"><span class="pos num"></span></div>
    <div class="rd-pop toc" hidden role="menu"></div><div class="rd-pop look" hidden></div>`;
  el.hidden = false;
  applyLook();
}

function showMsg(title, detail, closeable) {
  rdq(".rd-stage").innerHTML = html`<div class="rd-msg"><div><b>${title}</b>${detail}
    ${closeable ? html`<div style="margin-top:14px"><button class="btn" data-rd="close">返回</button>
    <button class="btn" data-rd="ext">用系统应用打开</button></div>` : ""}</div></div>`;
}

/* ------------------------------------------------------------ 外观 */

function toneOf() {
  if (rd.prefs.tone !== "auto") return rd.prefs.tone;
  return document.documentElement.dataset.mode === "dark" ? "night" : "paper";
}
function applyLook() {
  const el = $("reader");
  el.dataset.tone = toneOf();
  el.dataset.flow = rd.meta && rd.meta.mode === "pages" ? rd.prefs.comicFlow : rd.prefs.flow;
  el.style.setProperty("--rd-size", `${rd.prefs.size}px`);
  el.style.setProperty("--rd-font", `"${rd.prefs.font}", var(--font-book)`);
}

function lookPanel() {
  const text = rd.meta && rd.meta.mode === "text";
  const pressed = (on) => String(Boolean(on));
  return html`${text ? html`<div class="row"><div class="rd-size"><button data-size="-1" aria-label="缩小字号" style="font-size:13px">A</button><button data-size="1" aria-label="放大字号" style="font-size:19px">A</button></div></div>` : ""}
    <div class="row"><span class="lab">主题</span><div class="rd-tones">
      <button data-tone="auto" aria-pressed="${pressed(rd.prefs.tone === "auto")}" title="跟随应用" style="background:linear-gradient(135deg,#fff 50%,#121212 50%);color:transparent">·</button>
      ${RD_TONES.map(([v, bg, fg]) => html`<button data-tone="${v}" aria-pressed="${pressed(rd.prefs.tone === v)}" style="background:${bg};color:${fg}" title="${{ paper: "原色", sepia: "纸色", gray: "灰", night: "夜间" }[v]}">文</button>`)}</div></div>
    ${text ? html`<div class="row"><div class="rd-fonts">${RD_FONTS.map(([f, label]) => html`<button data-font="${f}" aria-pressed="${pressed(rd.prefs.font === f)}" style="font-family:'${f}'">${label}</button>`)}</div></div>
      <div class="row"><div class="rd-seg"><button data-flow="paged" aria-pressed="${pressed(rd.prefs.flow === "paged")}">翻页</button><button data-flow="scroll" aria-pressed="${pressed(rd.prefs.flow === "scroll")}">滚动</button></div></div>`
    : html`<div class="row"><div class="rd-seg"><button data-cflow="paged" aria-pressed="${pressed(rd.prefs.comicFlow === "paged")}">翻页</button><button data-cflow="scroll" aria-pressed="${pressed(rd.prefs.comicFlow === "scroll")}">条漫</button></div></div>
      <div class="row"><div class="rd-seg"><button data-spread="single" aria-pressed="${pressed(rd.prefs.spread === "single")}">单页</button><button data-spread="auto" aria-pressed="${pressed(rd.prefs.spread === "auto")}">自动</button><button data-spread="double" aria-pressed="${pressed(rd.prefs.spread === "double")}">双页</button></div></div>
      <div class="row"><div class="rd-seg"><button data-rtl="0" aria-pressed="${pressed(!rd.prefs.rtl)}">从左往右</button><button data-rtl="1" aria-pressed="${pressed(rd.prefs.rtl)}">日漫 · 从右往左</button></div></div>`}`;
}

function togglePop(kind) {
  const pop = rdq(`.rd-pop.${kind}`), other = rdq(`.rd-pop.${kind === "toc" ? "look" : "toc"}`);
  other.hidden = true;
  rdq(`[data-rd=${kind === "toc" ? "look" : "toc"}]`).setAttribute("aria-pressed", "false");
  pop.hidden = !pop.hidden;
  rdq(`[data-rd=${kind}]`).setAttribute("aria-pressed", String(!pop.hidden));
  if (pop.hidden) return;
  if (kind === "look") { pop.innerHTML = lookPanel(); return; }
  pop.innerHTML = html`${rd.meta.toc.map((t) => html`<button data-sec="${t.section}" aria-current="${String(t.section === rd.section)}">${t.title}</button>`)}`;
  pop.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "center" });
}
function closePops() {
  $("reader").querySelectorAll(".rd-pop").forEach((p) => { p.hidden = true; });
  $("reader").querySelectorAll("[data-rd=toc],[data-rd=look]").forEach((b) => b.setAttribute("aria-pressed", "false"));
}

/* 界面浮现 / 退场 */
function wake() {
  const el = $("reader");
  if (!el || el.hidden) return;
  el.classList.remove("calm");
  clearTimeout(rd.calmTimer);
  rd.calmTimer = setTimeout(() => {
    const busy = [...el.querySelectorAll(".rd-pop")].some((p) => !p.hidden) || el.querySelector(".rd-top:hover, .rd-bot:hover");
    if (!busy) el.classList.add("calm");
  }, 2000);
}

/* ------------------------------------------------------------ 进度 */

function saveProgress(position, percent) {
  rd.pending = { position, percent };
  clearTimeout(rd.saveTimer);
  rd.saveTimer = setTimeout(flushProgress, 900);
}
function flushProgress() {
  clearTimeout(rd.saveTimer);
  if (!rd.pending || !rd.book || !rd.meta) return;
  const body = { format: rd.meta.format, ...rd.pending };
  rd.pending = null;
  api(`/api/books/${rd.book.id}/progress`, { method: "PUT", body }).catch(() => {});
}
function setBottom(fraction, label) {
  const scrub = rdq("[data-rd=scrub]");
  if (document.activeElement !== scrub) scrub.value = String(Math.round(fraction * 1000));
  rdq(".rd-bot .pos").textContent = label;
}

/* ------------------------------------------------------------ 事件 */

function onReaderClick(e) {
  const t = e.target.closest("[data-rd],[data-sec],[data-size],[data-tone],[data-font],[data-flow],[data-cflow],[data-spread],[data-rtl],.rd-hot");
  if (!t) { if (!e.target.closest(".rd-pop")) closePops(); return; }
  const d = t.dataset;
  if (d.rd === "close") closeReader();
  else if (d.rd === "toc" || d.rd === "look") togglePop(d.rd);
  else if (d.rd === "ext") openExternal(rd.book.id, rd.meta && rd.meta.format);
  else if (d.sec !== undefined) { closePops(); loadSection(Number(d.sec), 0); }
  else if (t.classList.contains("rd-hot")) {
    const right = t.classList.contains("r") ? 1 : -1;
    turn(rd.meta && rd.meta.mode === "pages" && rd.prefs.rtl ? -right : right, true); // 日漫：点左边往后翻
  }
  else {
    if (d.size) rd.prefs.size = Math.min(34, Math.max(13, rd.prefs.size + Number(d.size) * 2));
    if (d.tone) rd.prefs.tone = d.tone;
    if (d.font) rd.prefs.font = d.font;
    if (d.flow) rd.prefs.flow = d.flow;
    if (d.cflow) rd.prefs.comicFlow = d.cflow;
    if (d.spread) rd.prefs.spread = d.spread;
    if (d.rtl) rd.prefs.rtl = d.rtl === "1";
    savePrefs();
    applyLook();
    rdq(".rd-pop.look").innerHTML = lookPanel();
    if (rd.meta.mode === "text" && (d.size || d.font || d.flow)) relayout();
    if (rd.meta.mode === "pages" && (d.cflow || d.spread || d.rtl)) renderPages();
  }
}

function onReaderInput(e) {
  if (e.target.dataset.rd !== "scrub" || !rd.meta) return;
  const f = Number(e.target.value) / 1000;
  clearTimeout(rd.scrubTimer);
  rd.scrubTimer = setTimeout(() => {
    if (rd.meta.mode === "text") {
      const exact = f * rd.meta.sections;
      const s = Math.min(rd.meta.sections - 1, Math.floor(exact));
      loadSection(s, exact - s);
    } else if (rd.meta.mode === "pages") { rd.page = Math.round(f * (rd.meta.pages - 1)); renderPages(); }
  }, 120);
}

/* 阅读器打开时，键盘只归它管（捕获阶段拦截，全局快捷键不再触发） */
document.addEventListener("keydown", (e) => {
  const el = $("reader");
  if (!el || el.hidden || e.isComposing || e.target.closest?.("input")) return;
  const fwd = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1, PageDown: 1, PageUp: -1, " ": e.shiftKey ? -1 : 1 }[e.key];
  let handled = true;
  if (e.key === "Escape") { if ([...el.querySelectorAll(".rd-pop")].some((p) => !p.hidden)) closePops(); else closeReader(); }
  else if (fwd && rd.meta && rd.meta.mode !== "pdf") {
    const scrolling = el.dataset.flow === "scroll";
    if (scrolling && (e.key === "ArrowDown" || e.key === "ArrowUp")) handled = false;
    else turn(rd.meta.mode === "pages" && rd.prefs.rtl && e.key.startsWith("Arrow") && /Left|Right/.test(e.key) ? -fwd : fwd, true);
  } else if ((e.key === "t" || e.key === "T") && rd.meta && rd.meta.toc.length) togglePop("toc");
  else if ((e.metaKey || e.ctrlKey) && (e.key === "=" || e.key === "+" || e.key === "-") && rd.meta && rd.meta.mode === "text") {
    rd.prefs.size = Math.min(34, Math.max(13, rd.prefs.size + (e.key === "-" ? -2 : 2)));
    savePrefs(); applyLook(); relayout();
  } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "w") closeReader();
  else handled = false;
  if (handled) { e.preventDefault(); e.stopImmediatePropagation(); wake(); }
}, true);
window.addEventListener("resize", () => {
  if (!rd.meta || $("reader").hidden) return;
  clearTimeout(rd.resizeTimer);
  rd.resizeTimer = setTimeout(() => (rd.meta.mode === "text" ? relayout() : renderPages()), 120);
});
window.addEventListener("beforeunload", flushProgress);

function turn(delta, user) {
  if (!rd.meta) return;
  if (rd.meta.mode === "text") turnText(delta);
  else if (rd.meta.mode === "pages") turnPages(delta);
  if (user) closePops();
}
