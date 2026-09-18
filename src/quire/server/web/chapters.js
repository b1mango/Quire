/* 章节功能：新建任务的范围选择、进行中页面的已落定章节列表。
   依赖 app.js 的 $ 与 state；列表只增不改，快照恢复按 300 条/帧分块追加。 */
"use strict";

/* ------------------------------------------------------------ 章节范围 */

function renderRange(result) {
  const chapters = result.chapters || [];
  if (state.captureMode === "single" || chapters.length < 2) {
    $("rangeField").hidden = true;
    return;
  }
  $("rangeField").hidden = false;
  const first = $("rangeFirst"), last = $("rangeLast");
  first.textContent = ""; last.textContent = "";
  chapters.forEach((chapter, index) => {
    const option = document.createElement("option");
    option.value = String(index + 1);
    option.textContent = `${index + 1} · ${chapter.title}`;
    first.appendChild(option);
    last.appendChild(option.cloneNode(true));
  });
  first.value = "1";
  last.value = String(chapters.length);
  $("rangeExpr").value = "";
  updateRangeSummary();
}

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

function rangeState() {
  const total = ((state.probe && state.probe.chapters) || []).length;
  const expr = $("rangeExpr").value.trim();
  if (!expr) return { expr: "", custom: false, segments: [], numbers: [], total };
  const segments = parseRangeExpr(expr);
  return { expr, custom: true, segments, numbers: resolveRangeSegments(segments, total), total };
}

function segmentLabel([first, last]) {
  if (last === null) return `${first}-`;
  return first === last ? `${first}` : `${first}-${last}`;
}

function updateRangeSummary() {
  if ($("rangeField").hidden || !state.probe) return;
  const chapters = state.probe.chapters || [];
  const summary = $("rangeSummary");
  summary.classList.remove("invalid");
  try {
    const sel = rangeState();
    if (!sel.custom) {
      summary.textContent = `全部 ${sel.total} 章`;
      if (state.probe) $("startBtn").disabled = false;
      return;
    }
    const preview = sel.numbers.length
      ? ` ·「${chapters[sel.numbers[0] - 1].title}」` +
        (sel.numbers.length > 1 ? ` …「${chapters[sel.numbers[sel.numbers.length - 1] - 1].title}」` : "")
      : "";
    summary.textContent =
      `已选 ${sel.numbers.length} 章：${sel.segments.map(segmentLabel).join("、")}` +
      preview + "（按所选范围重新分卷）";
    if (state.probe) $("startBtn").disabled = false;
  } catch (err) {
    summary.classList.add("invalid");
    summary.textContent = err.message;
    $("startBtn").disabled = true;
  }
}

/* 自定义范围与分卷互斥（与后端一致）：选了范围就隐藏分卷设置 */
function syncSeriesAvailability() {
  const probe = state.probe;
  let custom = false;
  try {
    custom = !$("rangeField").hidden && probe && rangeState().custom;
  } catch (err) {
    custom = true; // 表达式非法时也按自定义处理，避免同时勾选分卷
  }
  $("seriesField").hidden = Boolean(custom) || !(probe && probe.series);
}

/* 提交用：未选范围返回全量默认（1 / 0 = 到末尾） */
function chapterRangeSpec() {
  if ($("rangeField").hidden || !state.probe)
    return { chapter_first: 1, chapter_last: 0, chapter_ranges: "" };
  return { chapter_first: 1, chapter_last: 0, chapter_ranges: $("rangeExpr").value.trim() };
}

/* 下拉是快捷方式：改动即写回表达式（全选则清空），与输入框单一事实源 */
function syncExprFromSelects() {
  const total = ((state.probe && state.probe.chapters) || []).length;
  const first = parseInt($("rangeFirst").value, 10) || 1;
  const last = parseInt($("rangeLast").value, 10) || total;
  $("rangeExpr").value = first === 1 && last === total
    ? ""
    : first === last ? String(first) : `${first}-${last}`;
  updateRangeSummary(); syncSeriesAvailability();
}

$("rangeFirst").addEventListener("change", () => {
  const first = $("rangeFirst"), last = $("rangeLast");
  if (parseInt(last.value, 10) < parseInt(first.value, 10)) last.value = first.value;
  syncExprFromSelects();
});
$("rangeLast").addEventListener("change", () => {
  const first = $("rangeFirst"), last = $("rangeLast");
  if (parseInt(first.value, 10) > parseInt(last.value, 10)) first.value = last.value;
  syncExprFromSelects();
});
$("rangeExpr").addEventListener("input", () => {
  /* 单段闭区间时回显到下拉，多段/开放尾保持下拉不动 */
  try {
    const sel = rangeState();
    const total = sel.total;
    if (sel.segments.length === 1 && sel.segments[0][1] !== null) {
      const [first, last] = sel.segments[0];
      if (last <= total) { $("rangeFirst").value = String(first); $("rangeLast").value = String(last); }
    }
  } catch (err) { /* 输入中途，等完整表达式 */ }
  updateRangeSummary(); syncSeriesAvailability();
});

/* ------------------------------------------------------------ 已落定章节 */

const chapterState = { seen: new Set(), done: 0, failed: 0 };

function resetChapters() {
  chapterState.seen = new Set();
  chapterState.done = 0;
  chapterState.failed = 0;
  $("chapterPanel").hidden = true;
  $("chapterList").textContent = "";
  $("chapterMeta").textContent = "";
}

function chapterRow(chapter) {
  const row = document.createElement("div");
  row.className = "chapter-row";
  const dot = document.createElement("span");
  dot.className = "cdot " +
    (chapter.status === "failed" ? "failed" : chapter.reused ? "reused" : "done");
  const title = document.createElement("span");
  title.className = "chapter-title";
  title.textContent = chapter.title;
  const meta = document.createElement("span");
  meta.className = "chapter-meta";
  meta.textContent = chapter.status === "failed"
    ? `失败 ${chapter.failed_pages}/${chapter.pages} 页`
    : `${chapter.pages} 页${chapter.reused ? " · 缓存复用" : ""}`;
  row.append(dot, title, meta);
  return row;
}

function updateChapterMeta() {
  $("chapterMeta").textContent =
    `已落定 ${chapterState.done + chapterState.failed} 章` +
    (chapterState.failed ? ` · ${chapterState.failed} 章失败` : "");
}

function appendChapters(chapters) {
  const fresh = chapters.filter((c) => !chapterState.seen.has(String(c.id)));
  if (!fresh.length) return;
  fresh.forEach((c) => {
    chapterState.seen.add(String(c.id));
    if (c.status === "failed") chapterState.failed += 1; else chapterState.done += 1;
  });
  $("chapterPanel").hidden = false;
  const list = $("chapterList");
  let index = 0;
  const flush = () => {
    const fragment = document.createDocumentFragment();
    const end = Math.min(index + 300, fresh.length);
    for (; index < end; index += 1) fragment.appendChild(chapterRow(fresh[index]));
    list.appendChild(fragment);
    if (index < fresh.length) setTimeout(flush, 0);
  };
  flush();
  updateChapterMeta();
}
