/* 章节范围：结果卡上的「起–止」下拉、目录正倒序、自定义表达式。
   表达式是唯一事实源（空 = 全部）；下拉只是快捷方式，改动即写回表达式。
   依赖 app.js 的 $ / html / state；结果卡渲染后调用 bindRange()。 */
"use strict";

/* 与后端 parse/chapter_range.py 同规则：逗号分段、连字符区间、单章、开放尾 200-。
   返回 [[first, last|null], …]；不合法抛 Error（message 为用户可读中文）。 */
function parseRangeExpr(expr) {
  const text = (expr || "").trim();
  if (!text) return [];
  if (text.length > 200) throw new Error("章节范围表达式过长");
  const segments = [];
  const covered = new Set();
  for (const raw of text.split(",")) {
    const part = raw.trim();
    const m = /^(\d+)(?:-(\d*))?$/.exec(part);
    if (!m) throw new Error(`「${part}」无法识别，示例：1-10,15-20,103`);
    const first = parseInt(m[1], 10);
    if (first < 1 || first > 20000) throw new Error("章节号须在 1–20000 之间");
    if (segments.length && segments[segments.length - 1][1] === null)
      throw new Error("开放区间（如 200-）只能是最后一段");
    if (m[2] !== undefined && m[2] === "") {
      if (covered.size && first <= Math.max(...covered))
        throw new Error(`章节 ${first} 在范围里重复出现`);
      segments.push([first, null]);
      continue;
    }
    const last = m[2] ? parseInt(m[2], 10) : first;
    if (last < first) throw new Error(`「${part}」倒置，结束不能早于起始`);
    if (last > 20000) throw new Error("章节号须在 1–20000 之间");
    for (let n = first; n <= last; n += 1) {
      if (covered.has(n)) throw new Error(`章节 ${n} 在范围里重复出现`);
      covered.add(n);
    }
    segments.push([first, last]);
  }
  return segments;
}

/* 展开为章号数组（开放尾按目录长度）；越界抛错。 */
function resolveRangeSegments(segments, total) {
  const numbers = [];
  segments.forEach(([first, last]) => {
    const end = last === null ? total : last;
    if (first > total || end > total)
      throw new Error(`范围超出当前目录（共 ${total} 章），请重新识别`);
    for (let n = first; n <= end; n += 1) numbers.push(n);
  });
  return numbers;
}

function segmentLabel([first, last]) {
  if (last === null) return `${first}-`;
  return first === last ? `${first}` : `${first}-${last}`;
}

/* ------------------------------------------------------------ 结果卡上的范围控件 */

const rangeOrders = new Map(); // 小说 / 漫画各自记住目录显示顺序，不进任务参数
let rangeKind = "novel";
let onRangeChange = () => {};

function probeChapters() { return (state.probe && state.probe.chapters) || []; }
function ownedChapters() {
  const lib = state.probe && state.probe.library;
  return Math.min((lib && lib.chapters) || 0, probeChapters().length);
}

/* 目录少于 2 章（单章页 / 单话漫画）不出范围行 */
function rangeRowHTML() {
  const chapters = probeChapters();
  if (chapters.length < 2) return "";
  const owned = ownedChapters();
  const opts = chapters.map((c, i) => html`<option value="${i + 1}">${i + 1} · ${c.title}${i < owned ? "（已在库）" : ""}</option>`);
  return html`<div class="row"><span class="lab" id="rngLab">章节</span><div class="ctl range" role="group" aria-labelledby="rngLab">
    <select class="field-input" id="rangeFirst" aria-label="起始章节">${opts}</select><span class="to">至</span>
    <select class="field-input" id="rangeLast" aria-label="结束章节">${opts}</select>
    <button type="button" class="btn quiet" id="rangeAll">全部</button>
    <button type="button" class="btn quiet order" id="rangeOrderBtn" aria-controls="rangeFirst rangeLast" aria-pressed="false">目录正序</button>
    <div class="range-expr"><label class="sr" for="rangeExpr">自定义章节范围</label>
      <input class="field-input num" id="rangeExpr" placeholder="自定义，如 1-10,15,200-" spellcheck="false" autocomplete="off"></div>
    <div class="range-sum" id="rangeSum" aria-live="polite"></div></div></div>`;
}

function applyRangeOrder() {
  const descending = rangeOrders.get(rangeKind) === "desc";
  for (const id of ["rangeFirst", "rangeLast"]) {
    const select = $(id), selected = select.value;
    [...select.children]
      .sort((a, b) => (descending ? b.value - a.value : a.value - b.value))
      .forEach((option) => select.appendChild(option));
    select.value = selected;
  }
  const btn = $("rangeOrderBtn");
  btn.textContent = descending ? "目录倒序" : "目录正序";
  btn.setAttribute("aria-pressed", String(descending));
}

function bindRange(kind, onChange) {
  rangeKind = kind;
  onRangeChange = onChange || (() => {});
  if (!$("rangeFirst")) return;
  const total = probeChapters().length, owned = ownedChapters();
  const start = owned && owned < total ? owned + 1 : 1;
  $("rangeFirst").value = String(start);
  $("rangeLast").value = String(total);
  $("rangeExpr").value = start === 1 ? "" : `${start}-${total}`;
  applyRangeOrder();
  $("rangeFirst").addEventListener("change", () => {
    if (+$("rangeLast").value < +$("rangeFirst").value) $("rangeLast").value = $("rangeFirst").value;
    syncExprFromSelects();
  });
  $("rangeLast").addEventListener("change", () => {
    if (+$("rangeFirst").value > +$("rangeLast").value) $("rangeFirst").value = $("rangeLast").value;
    syncExprFromSelects();
  });
  $("rangeAll").addEventListener("click", () => {
    $("rangeFirst").value = "1"; $("rangeLast").value = String(total); syncExprFromSelects();
  });
  $("rangeOrderBtn").addEventListener("click", () => {
    rangeOrders.set(rangeKind, rangeOrders.get(rangeKind) === "desc" ? "asc" : "desc");
    applyRangeOrder();
  });
  $("rangeExpr").addEventListener("input", () => { syncSelectsFromExpr(); updateRangeSummary(); });
  updateRangeSummary();
}

function syncExprFromSelects() {
  const total = probeChapters().length;
  const first = parseInt($("rangeFirst").value, 10) || 1;
  const last = parseInt($("rangeLast").value, 10) || total;
  $("rangeExpr").value = first === 1 && last === total ? "" : first === last ? String(first) : `${first}-${last}`;
  updateRangeSummary();
}

/* 表达式 → 下拉回显：单段闭区间时同步，多段 / 开放尾保持下拉不动 */
function syncSelectsFromExpr() {
  try {
    const sel = rangeState();
    if (sel.segments.length === 1 && sel.segments[0][1] !== null && sel.segments[0][1] <= sel.total) {
      $("rangeFirst").value = String(sel.segments[0][0]);
      $("rangeLast").value = String(sel.segments[0][1]);
    }
  } catch (_) { /* 输入中途，等完整表达式 */ }
}

function rangeState() {
  const total = probeChapters().length;
  const input = $("rangeExpr");
  const expr = input ? input.value.trim() : "";
  if (!expr) return { expr: "", custom: false, segments: [], numbers: [], total };
  const segments = parseRangeExpr(expr);
  return { expr, custom: true, segments, numbers: resolveRangeSegments(segments, total), total };
}

/* 摘要：合法时返回 {count, label}；非法时 {error} */
function rangeSummary() {
  const total = probeChapters().length;
  if (!$("rangeExpr")) return { count: state.probe ? state.probe.count : 0, label: "" };
  try {
    const sel = rangeState();
    if (!sel.custom) return { count: total, label: `全部 ${total} 章` };
    const label = sel.segments.length === 1 && sel.segments[0][1] !== null
      ? `第 ${segmentLabel(sel.segments[0]).replace("-", "–")} 章`
      : `第 ${sel.segments.map(segmentLabel).join("、")} 章`;
    return { count: sel.numbers.length, label };
  } catch (err) {
    return { error: err.message };
  }
}

function updateRangeSummary() {
  const sum = $("rangeSum");
  const s = rangeSummary();
  if (sum) {
    sum.classList.toggle("invalid", Boolean(s.error));
    sum.textContent = s.error || (s.label.startsWith("全部") ? "" : `已选 ${s.count} 章`);
  }
  onRangeChange(s);
}

/* 提交用：未选范围返回全量（1 / 0 = 到末尾） */
function chapterRangeSpec() {
  const input = $("rangeExpr");
  return { chapter_first: 1, chapter_last: 0, chapter_ranges: input ? input.value.trim() : "" };
}
