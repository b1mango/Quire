/* 阅读器排版与翻页：文字按 CSS 多栏分页（宽屏双页），漫画按单页 / 双页 / 条漫。
   翻页参照「图书」：触控板横扫时页面跟手，过阈值后带惯性滑过；切章时新旧两章同步滑动，下一章预取。依赖 reader.js。 */
"use strict";

const SPREAD_MIN = 860; // 文字双栏、漫画自动双页的最小视宽
const TURN_MS = 460;
const TURN_EASE = "cubic-bezier(.22,.72,.2,1)";

const stageEl = () => rdq(".rd-stage");
const curView = () => stageEl() && stageEl().querySelector(".rd-view:not(.leaving)");
const curFlow = () => curView() && curView().querySelector(".rd-flow");
const curPages = () => stageEl() && stageEl().querySelector(".rd-pages:not(.leaving)");

/* 两层同步滑动：旧层滑出、新层滑入（dir=1 向后翻，新层从右边进来） */
function slideSwap(oldEl, newEl, dir, fromPx = 0) {
  const w = stageEl().clientWidth;
  oldEl.classList.add("leaving");
  const opts = { duration: TURN_MS, easing: TURN_EASE, fill: "forwards" };
  oldEl.animate([{ transform: `translate3d(${fromPx}px,0,0)` }, { transform: `translate3d(${-dir * w}px,0,0)` }], opts)
    .finished.then(() => oldEl.remove(), () => oldEl.remove());
  newEl.animate([{ transform: `translate3d(${dir * w + fromPx}px,0,0)` }, { transform: "translate3d(0,0,0)" }], opts);
}

/* ------------------------------------------------------------ 文字 */

function fetchSection(index) {
  if (!rd.cache.has(index)) {
    const p = api(`/api/books/${rd.book.id}/read/section/${index}?format=${rd.meta.format}`);
    p.catch(() => rd.cache.delete(index));
    rd.cache.set(index, p);
  }
  return rd.cache.get(index);
}
function prefetchAround(index) {
  [index + 1, index - 1].forEach((i) => { if (i >= 0 && i < rd.meta.sections) fetchSection(i).catch(() => {}); });
}

async function loadSection(index, fraction = 0, fromEnd = false, dir = 0) {
  if (!rd.meta || index < 0 || index >= rd.meta.sections) return;
  const ticket = ++rd.loading;
  let data;
  try {
    data = await fetchSection(index);
  } catch (err) {
    if (ticket === rd.loading) showMsg("这一章没能读取", errText(err), true);
    return;
  }
  if (ticket !== rd.loading || !rd.meta) return;
  rd.section = index;
  const nav = rd.prefs.flow === "scroll" ? html`<div style="display:flex;gap:10px;justify-content:center;margin:3em 0 1em;text-indent:0">
      ${index > 0 ? html`<button class="btn" data-sec="${index - 1}">上一章</button>` : ""}
      ${index < rd.meta.sections - 1 ? html`<button class="btn primary" data-sec="${index + 1}">下一章</button>` : html`<span style="color:var(--rd-dim)">全书完</span>`}</div>` : "";
  const stage = stageEl();
  const old = curView();
  const animate = dir && old && rd.prefs.flow === "paged";
  if (!animate) stage.innerHTML = html`<div class="rd-hot l" aria-hidden="true"></div><div class="rd-hot r" aria-hidden="true"></div>`;
  else stage.querySelectorAll(".rd-view.leaving").forEach((v) => v.remove());
  stage.insertAdjacentHTML("afterbegin", html`<div class="rd-view"><div class="rd-flow" lang="zh">${raw(data.html)}${nav}</div></div>`);
  const view = stage.querySelector(".rd-view"), flow = view.querySelector(".rd-flow");
  flow.querySelectorAll("img").forEach((img) => { img.src = withToken(img.getAttribute("src")); img.loading = "eager"; });
  view.addEventListener("scroll", onTextScroll, { passive: true });
  // 只有一张图、几乎没有文字的段（封面、插图页）：单栏居中整页显示
  flow.classList.toggle("solo", flow.querySelectorAll("img").length === 1 && flow.textContent.trim().length < 20);
  rd.frac = fraction;
  relayout(fromEnd);
  if (animate) {
    // 旧章的跟手位移挪到外层，由 slideSwap 统一从当前位置滑走
    const oldFlow = old.querySelector(".rd-flow"), x = parseFloat((oldFlow.style.transform.match(/-?[\d.]+/) || [0])[0]);
    if (rd.dragPx) { oldFlow.style.transition = "none"; oldFlow.style.transform = `translate3d(${x - rd.dragPx}px,0,0)`; }
    slideSwap(old, view, dir, rd.dragPx || 0);
  }
  rd.dragPx = 0;
  prefetchAround(index);
}

/* 量出栏宽与页数；fromEnd 用于向前翻到上一章末页 */
function relayout(fromEnd = false) {
  const view = curView(), flow = curFlow();
  if (!view || !flow) return;
  if (rd.prefs.flow === "scroll") {
    flow.style.columnCount = ""; flow.style.transform = "";
    view.scrollTop = rd.frac * Math.max(0, view.scrollHeight - view.clientHeight);
    reportText();
    return;
  }
  measure();
  rd.page = fromEnd ? rd.pages - 1 : Math.min(rd.pages - 1, Math.floor(rd.frac * rd.pages + 1e-6));
  place(false);
}

function measure() {
  const view = curView(), flow = curFlow();
  if (!view || !flow || rd.prefs.flow !== "paged") return;
  view.classList.remove("one");
  const cols = rd.prefs.cols;
  const wide = view.clientWidth;
  const two = !flow.classList.contains("solo") && (cols === "2" ? wide >= 560 : cols === "1" ? false : wide >= SPREAD_MIN);
  view.classList.toggle("one", !two); // 单栏限制行长，不让一行铺满宽屏
  const w = view.clientWidth;
  const gap = Math.round(two ? Math.max(48, w * 0.08) : Math.max(32, w * 0.06));
  flow.style.setProperty("--rd-gap", `${gap}px`);
  // column-width 只是下限：两栏差一个亚像素就会退成一整栏，所以直接定栏数
  flow.style.columnWidth = "auto";
  flow.style.columnCount = two ? "2" : "1";
  rd.step = w + gap;
  rd.pages = Math.max(1, Math.round((flow.scrollWidth + gap) / rd.step));
}

/* 图片加载后版面变了：保持当前页，重新量页数 */
function remeasure() {
  measure();
  if (rd.page >= rd.pages) rd.page = rd.pages - 1;
  place(false);
}

function setFlowX(px, animate) {
  const flow = curFlow();
  if (!flow) return;
  flow.style.transition = animate ? `transform ${TURN_MS}ms ${TURN_EASE}` : "none";
  flow.style.transform = `translate3d(${px}px,0,0)`;
}

function place(animate) {
  setFlowX(-rd.page * rd.step, animate);
  rd.frac = rd.pages > 1 ? rd.page / rd.pages : 0;
  reportText();
}

function turnText(delta) {
  if (rd.prefs.flow === "scroll") {
    const view = curView();
    view.scrollBy({ top: delta * view.clientHeight * 0.9, behavior: "smooth" });
    return;
  }
  const next = rd.page + delta;
  if (next >= 0 && next < rd.pages) { rd.page = next; rd.dragPx = 0; place(true); return; }
  if (delta > 0 && rd.section < rd.meta.sections - 1) loadSection(rd.section + 1, 0, false, 1);
  else if (delta < 0 && rd.section > 0) loadSection(rd.section - 1, 0, true, -1);
  else { rd.dragPx = 0; place(true); } // 全书首尾：弹回
}

function onTextScroll() {
  if (rd.prefs.flow !== "scroll") return;
  const view = curView();
  rd.frac = view.scrollHeight > view.clientHeight ? view.scrollTop / (view.scrollHeight - view.clientHeight) : 0;
  reportText();
}

function sectionTitle(index) {
  let title = "";
  (rd.meta.toc || []).forEach((t) => { if (t.section <= index) title = t.title; });
  if (!title && index === 0 && curFlow() && curFlow().classList.contains("solo")) return "封面";
  return title;
}

function reportText() {
  const total = rd.meta.sections || 1;
  const overall = Math.min(1, (rd.section + rd.frac) / total);
  const where = rd.prefs.flow === "paged" && rd.pages > 1 ? ` · ${rd.page + 1}/${rd.pages}` : "";
  setBottom(overall, `${sectionTitle(rd.section) || `第 ${rd.section + 1} / ${total} 段`}${where} · ${Math.round(overall * 100)}%`);
  saveProgress(`${rd.section}:${rd.frac.toFixed(4)}`, Number(overall.toFixed(4)));
}

/* ------------------------------------------------------------ 漫画 */

function pageUrl(n) { return withToken(`/api/books/${rd.book.id}/read/page/${n}?format=${rd.meta.format}`); }

function twoUp() {
  if (rd.prefs.spread === "single") return false;
  const stage = stageEl();
  if (rd.prefs.spread === "double") return true;
  return stage.clientWidth >= SPREAD_MIN && stage.clientWidth > stage.clientHeight * 1.15;
}
/* 双页时封面单独一页，之后 1-2、3-4… 成对 */
function groupOf(p) {
  if (!twoUp() || p === 0) return [p];
  const first = 1 + 2 * Math.floor((p - 1) / 2);
  return first + 1 < rd.meta.pages ? [first, first + 1] : [first];
}

async function renderPages(dir = 0) {
  if (!rd.meta || rd.meta.mode !== "pages") return;
  applyLook();
  const stage = stageEl();
  if (!rd.meta.pages) { showMsg("这本书没有可显示的页面", "", true); return; }
  if (cflow() === "scroll") {
    // 条漫连读：图片按宽度铺开、上下无缝，懒加载；未加载的页按条漫比例占位，跳到进度处不乱跳
    const ph = rd.meta.strip ? "aspect-ratio:1/2.3" : "min-height:40vh";
    stage.innerHTML = html`<div class="rd-pages" tabindex="-1">${Array.from({ length: rd.meta.pages }, (_, n) =>
      html`<img data-n="${n}" loading="lazy" decoding="async" alt="第 ${n + 1} 页" src="${pageUrl(n)}" style="${ph}">`)}</div>`;
    const box = stage.querySelector(".rd-pages");
    box.addEventListener("load", (e) => { e.target.style.minHeight = ""; e.target.style.aspectRatio = ""; }, true);
    box.querySelector(`[data-n="${rd.page}"]`)?.scrollIntoView();
    box.addEventListener("scroll", () => {
      const top = box.getBoundingClientRect().top;
      const hit = [...box.querySelectorAll("img")].find((img) => img.getBoundingClientRect().bottom > top + 60);
      if (hit) { rd.page = Number(hit.dataset.n); reportPages(); }
    }, { passive: true });
    reportPages();
    return;
  }
  const group = groupOf(rd.page);
  rd.page = group[0];
  const shown = rd.prefs.rtl ? [...group].reverse() : group;
  const old = curPages();
  const animate = dir && old;
  if (!animate) stage.innerHTML = html`<div class="rd-hot l" aria-hidden="true"></div><div class="rd-hot r" aria-hidden="true"></div>`;
  else stage.querySelectorAll(".rd-pages.leaving").forEach((v) => v.remove());
  stage.insertAdjacentHTML("afterbegin", html`<div class="rd-pages ${group.length > 1 ? "spread" : ""}">${shown.map((n) => html`<img alt="第 ${n + 1} 页" src="${pageUrl(n)}">`)}</div>`);
  const fresh = curPages();
  if (animate) {
    // 图已预取：等解码（最多 200ms）再滑，避免滑进来一块空白
    await Promise.race([Promise.all([...fresh.querySelectorAll("img")].map((i) => i.decode().catch(() => {}))),
      new Promise((r) => setTimeout(r, 200))]);
    slideSwap(old, fresh, rd.prefs.rtl ? -dir : dir, rd.dragPx || 0);
  }
  rd.dragPx = 0;
  const last = group[group.length - 1];
  groupOf(last + 1 < rd.meta.pages ? last + 1 : rd.page).concat(group[0] > 0 ? groupOf(group[0] - 1) : [])
    .forEach((n) => { new Image().src = pageUrl(n); });
  reportPages();
}

function turnPages(delta) {
  if (cflow() === "scroll") {
    const box = curPages();
    box.scrollBy({ top: delta * box.clientHeight * 0.9, behavior: "smooth" });
    return;
  }
  const group = groupOf(rd.page);
  const next = delta > 0 ? group[group.length - 1] + 1 : group[0] - 1;
  if (next < 0 || next >= rd.meta.pages) { snapPages(); return; }
  rd.page = groupOf(next)[0];
  renderPages(delta);
}
function snapPages() {
  const box = curPages();
  if (box && rd.dragPx) box.animate([{ transform: `translate3d(${rd.dragPx}px,0,0)` }, { transform: "translate3d(0,0,0)" }], { duration: 320, easing: TURN_EASE });
  if (box) box.style.transform = "";
  rd.dragPx = 0;
}

function reportPages() {
  const n = rd.meta.pages;
  const group = cflow() === "scroll" ? [rd.page] : groupOf(rd.page);
  const label = group.length > 1 ? `${group[0] + 1}–${group[1] + 1}` : `${group[0] + 1}`;
  const fraction = n > 1 ? rd.page / (n - 1) : 1;
  setBottom(fraction, `第 ${label} / ${n} 页`);
  saveProgress(String(rd.page), Number(fraction.toFixed(4)));
}

/* ------------------------------------------------------------ 触控板手势 */

/* 横扫：页面跟手位移，越过阈值即提交翻页（从当前位置接着滑完）；松手未过阈值则弹回。
   系统惯性会在松手后继续送滚轮事件，提交后锁住直到事件停 160ms。竖向滚轮累计 60px 翻一页。 */
const swipe = { dx: 0, dy: 0, locked: false, timer: 0 };
function onReaderWheel(e) {
  const el = $("reader");
  if (!el || el.hidden || !rd.meta || rd.meta.mode === "pdf" || e.ctrlKey) return;
  if (el.dataset.flow === "scroll" || e.target.closest(".rd-pop")) return;
  e.preventDefault();
  clearTimeout(swipe.timer);
  swipe.timer = setTimeout(endSwipe, 160);
  if (swipe.locked) return;
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    swipe.dx -= e.deltaX;
    const width = stageEl().clientWidth;
    rd.dragPx = Math.max(-width, Math.min(width, swipe.dx));
    if (rd.meta.mode === "text") setFlowX(-rd.page * rd.step + rd.dragPx, false);
    else if (curPages()) curPages().style.transform = `translate3d(${rd.dragPx}px,0,0)`;
    if (Math.abs(swipe.dx) > Math.min(120, width * 0.12)) commitSwipe(swipe.dx < 0 ? 1 : -1, true);
  } else {
    swipe.dy += e.deltaY;
    if (Math.abs(swipe.dy) > 60) commitSwipe(swipe.dy > 0 ? 1 : -1, false);
  }
}
function commitSwipe(dir, horizontal) {
  swipe.locked = true;
  swipe.dx = swipe.dy = 0;
  // 日漫从右往左：手指向右扫才是往后翻
  turn(horizontal && rd.meta.mode === "pages" && rd.prefs.rtl ? -dir : dir, true);
}
function endSwipe() {
  if (!swipe.locked && rd.dragPx && rd.meta) {
    if (rd.meta.mode === "text") { rd.dragPx = 0; place(true); } else snapPages();
  }
  swipe.dx = swipe.dy = 0;
  swipe.locked = false;
}
document.addEventListener("wheel", (e) => { if (e.target.closest?.("#reader .rd-stage")) onReaderWheel(e); }, { passive: false });
