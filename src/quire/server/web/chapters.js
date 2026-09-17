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
  updateRangeSummary();
}

function rangeSelection() {
  const total = ((state.probe && state.probe.chapters) || []).length;
  const first = parseInt($("rangeFirst").value, 10) || 1;
  const last = parseInt($("rangeLast").value, 10) || total;
  return { first, last, custom: first !== 1 || last !== total };
}

function updateRangeSummary() {
  if ($("rangeField").hidden || !state.probe) return;
  const chapters = state.probe.chapters || [];
  const { first, last, custom } = rangeSelection();
  $("rangeSummary").textContent = `已选 ${last - first + 1} 章：${chapters[first - 1].title}` +
    (last > first ? ` … ${chapters[last - 1].title}` : "") +
    (custom ? "（按所选范围重新分卷）" : "");
}

/* 自定义范围与分卷互斥（与后端一致）：选了范围就隐藏分卷设置 */
function syncSeriesAvailability() {
  const probe = state.probe;
  const custom = !$("rangeField").hidden && probe && rangeSelection().custom;
  $("seriesField").hidden = Boolean(custom) || !(probe && probe.series);
}

/* 提交用：未选范围返回全量默认（1 / 0 = 到末尾） */
function chapterRangeSpec() {
  if ($("rangeField").hidden || !state.probe) return { chapter_first: 1, chapter_last: 0 };
  const { first, last, custom } = rangeSelection();
  return custom ? { chapter_first: first, chapter_last: last } : { chapter_first: 1, chapter_last: 0 };
}

$("rangeFirst").addEventListener("change", () => {
  const first = $("rangeFirst"), last = $("rangeLast");
  if (parseInt(last.value, 10) < parseInt(first.value, 10)) last.value = first.value;
  updateRangeSummary(); syncSeriesAvailability();
});
$("rangeLast").addEventListener("change", () => {
  const first = $("rangeFirst"), last = $("rangeLast");
  if (parseInt(first.value, 10) > parseInt(last.value, 10)) first.value = last.value;
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
