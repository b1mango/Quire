/* 阅读器排版与翻页：文字按 CSS 多栏分页（宽屏双页），漫画按单页 / 双页 / 条漫。依赖 reader.js。 */
"use strict";

const SPREAD_MIN = 860; // 文字双栏、漫画自动双页的最小视宽

/* ------------------------------------------------------------ 文字 */

async function loadSection(index, fraction = 0, fromEnd = false) {
  if (!rd.meta || index < 0 || index >= rd.meta.sections) return;
  const ticket = ++rd.loading;
  rd.section = index;
  let data;
  try {
    data = await api(`/api/books/${rd.book.id}/read/section/${index}?format=${rd.meta.format}`);
  } catch (err) {
    if (ticket === rd.loading) showMsg("这一章没能读取", errText(err), true);
    return;
  }
  if (ticket !== rd.loading || !rd.meta) return;
  const nav = rd.prefs.flow === "scroll" ? html`<div style="display:flex;gap:10px;justify-content:center;margin:3em 0 1em;text-indent:0">
      ${index > 0 ? html`<button class="btn" data-sec="${index - 1}">上一章</button>` : ""}
      ${index < rd.meta.sections - 1 ? html`<button class="btn primary" data-sec="${index + 1}">下一章</button>` : html`<span style="color:var(--rd-dim)">全书完</span>`}</div>` : "";
  rdq(".rd-stage").innerHTML = html`<div class="rd-view"><div class="rd-flow" lang="zh">${raw(data.html)}${nav}</div></div>
    <div class="rd-hot l" aria-hidden="true"></div><div class="rd-hot r" aria-hidden="true"></div>`;
  rdq(".rd-flow").querySelectorAll("img").forEach((img) => { img.src = withToken(img.getAttribute("src")); img.loading = "eager"; });
  rdq(".rd-view").addEventListener("scroll", onTextScroll, { passive: true });
  rd.frac = fraction;
  relayout(fromEnd);
}

/* 量出栏宽与页数；fromEnd 用于向前翻到上一章末页 */
function relayout(fromEnd = false) {
  const view = rdq(".rd-view"), flow = rdq(".rd-flow");
  if (!view || !flow) return;
  if (rd.prefs.flow === "scroll") {
    flow.style.columnWidth = ""; flow.style.transform = "";
    view.scrollTop = rd.frac * Math.max(0, view.scrollHeight - view.clientHeight);
    reportText();
    return;
  }
  measure();
  rd.page = fromEnd ? rd.pages - 1 : Math.min(rd.pages - 1, Math.floor(rd.frac * rd.pages + 1e-6));
  place(false);
}

function measure() {
  const view = rdq(".rd-view"), flow = rdq(".rd-flow");
  if (!view || !flow || rd.prefs.flow !== "paged") return;
  const w = view.clientWidth;
  const two = w >= SPREAD_MIN;
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

function place(animate) {
  const flow = rdq(".rd-flow");
  if (!flow) return;
  if (!animate) flow.style.transition = "none";
  flow.style.transform = `translateX(${-rd.page * rd.step}px)`;
  if (!animate) { void flow.offsetWidth; flow.style.transition = ""; }
  rd.frac = rd.pages > 1 ? rd.page / rd.pages : 0;
  reportText();
}

function turnText(delta) {
  if (rd.prefs.flow === "scroll") {
    const view = rdq(".rd-view");
    view.scrollBy({ top: delta * view.clientHeight * 0.9, behavior: "smooth" });
    return;
  }
  const next = rd.page + delta;
  if (next >= 0 && next < rd.pages) { rd.page = next; place(true); return; }
  if (delta > 0 && rd.section < rd.meta.sections - 1) loadSection(rd.section + 1, 0);
  else if (delta < 0 && rd.section > 0) loadSection(rd.section - 1, 0, true);
}

function onTextScroll() {
  if (rd.prefs.flow !== "scroll") return;
  const view = rdq(".rd-view");
  rd.frac = view.scrollHeight > view.clientHeight ? view.scrollTop / (view.scrollHeight - view.clientHeight) : 0;
  reportText();
}

function sectionTitle(index) {
  let title = "";
  (rd.meta.toc || []).forEach((t) => { if (t.section <= index) title = t.title; });
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
  const stage = rdq(".rd-stage");
  if (rd.prefs.spread === "double") return true;
  return stage.clientWidth >= SPREAD_MIN && stage.clientWidth > stage.clientHeight * 1.15;
}
/* 双页时封面单独一页，之后 1-2、3-4… 成对 */
function groupOf(p) {
  if (!twoUp() || p === 0) return [p];
  const first = 1 + 2 * Math.floor((p - 1) / 2);
  return first + 1 < rd.meta.pages ? [first, first + 1] : [first];
}

function renderPages() {
  if (!rd.meta || rd.meta.mode !== "pages") return;
  applyLook();
  const stage = rdq(".rd-stage");
  if (!rd.meta.pages) { showMsg("这本书没有可显示的页面", "", true); return; }
  if (rd.prefs.comicFlow === "scroll") {
    stage.innerHTML = html`<div class="rd-pages" tabindex="-1">${Array.from({ length: rd.meta.pages }, (_, n) =>
      html`<img data-n="${n}" loading="lazy" alt="第 ${n + 1} 页" src="${pageUrl(n)}" style="min-height:40vh">`)}</div>`;
    const box = stage.querySelector(".rd-pages");
    box.addEventListener("load", (e) => { e.target.style.minHeight = ""; }, true);
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
  stage.innerHTML = html`<div class="rd-pages ${group.length > 1 ? "spread" : ""}">${shown.map((n) => html`<img alt="第 ${n + 1} 页" src="${pageUrl(n)}">`)}</div>
    <div class="rd-hot l" aria-hidden="true"></div><div class="rd-hot r" aria-hidden="true"></div>`;
  const ahead = groupOf(group[group.length - 1] + 1 < rd.meta.pages ? group[group.length - 1] + 1 : rd.page);
  ahead.forEach((n) => { new Image().src = pageUrl(n); });
  reportPages();
}

function turnPages(delta) {
  if (rd.prefs.comicFlow === "scroll") {
    const box = rdq(".rd-pages");
    box.scrollBy({ top: delta * box.clientHeight * 0.9, behavior: "smooth" });
    return;
  }
  const group = groupOf(rd.page);
  const next = delta > 0 ? group[group.length - 1] + 1 : group[0] - 1;
  if (next < 0 || next >= rd.meta.pages) return;
  rd.page = groupOf(next)[0];
  renderPages();
}

function reportPages() {
  const n = rd.meta.pages;
  const group = rd.prefs.comicFlow === "scroll" ? [rd.page] : groupOf(rd.page);
  const label = group.length > 1 ? `${group[0] + 1}–${group[1] + 1}` : `${group[0] + 1}`;
  const fraction = n > 1 ? rd.page / (n - 1) : 1;
  setBottom(fraction, `第 ${label} / ${n} 页`);
  saveProgress(String(rd.page), Number(fraction.toFixed(4)));
}
