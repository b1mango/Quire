/* 追更：检查更新 / 全部检查 / 一键追更。依赖 app.js 的 api/state/loadBooks/attachJob。 */
"use strict";

function followBadge(book) {
  if (!book.follow) return null;
  const badge = document.createElement("span");
  if (book.follow.changed) {
    badge.className = "badge changed";
    badge.textContent = "目录变动";
  } else if (book.follow.update > 0) {
    badge.className = "badge";
    badge.textContent = `+${book.follow.update} 章`;
  } else {
    return null;
  }
  return badge;
}

function updateFollowButtons(book) {
  const follow = book && book.follow;
  $("bookCheckBtn").hidden = !follow;
  $("bookFollowBtn").hidden = !(follow && follow.update > 0 && !follow.changed);
  if (follow && follow.update > 0) $("bookFollowBtn").textContent = `追更 +${follow.update} 章`;
}

/* NDJSON 流读取（与 probe 同协议）：阶段/进度帧回调，result/error 帧收尾 */
function streamFrames(url, onFrame) {
  return fetch(url, {
    method: "POST",
    headers: { "X-Quire-Token": TOKEN, "Content-Type": "application/json" },
    body: "{}",
  }).then(async (res) => {
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const err = new Error(data.error || `请求失败（${res.status}）`);
      err.hint = data.hint;
      throw err;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", result = null, failure = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const frame = JSON.parse(line);
        if (frame.error) failure = frame;
        else if (frame.result) result = frame.result;
        else if (onFrame) onFrame(frame);
      }
    }
    if (failure) {
      const err = new Error(failure.error);
      err.hint = failure.hint;
      throw err;
    }
    return result;
  });
}

function showLibCheck(text, fraction) {
  $("libCheckBar").hidden = false;
  $("libCheckText").textContent = text;
  if (fraction != null) $("libCheckFill").style.width = `${Math.round(fraction * 100)}%`;
}

function hideLibCheck() {
  $("libCheckBar").hidden = true;
  $("libCheckFill").style.width = "0";
}

/* ------------------------------------------------------------ 检查结果弹窗 */

let updateDialogBook = null;

function showUpdateDialog(build) {
  const body = $("updateDialogBody");
  body.textContent = "";
  build(body);
  $("updateDialog").showModal();
}

/* 跳转采集台续解析：带上原链接立即识别，章节列表会标注已在库进度（probe 的 library 字段） */
function resumeInCapture(bookId) {
  const book = (state.books || []).find((item) => item.id === bookId);
  if (!book || !book.source_url) return;
  $("updateDialog").close();
  saveTabState(tabKey());
  state.kind = book.kind;
  state.captureMode = "catalogue";
  switchTab();
  showView("new");
  setSeg($("kindSeg"), "kind", book.kind);
  setSeg($("captureSeg"), "capture", "catalogue");
  $("urlInput").value = book.source_url;
  recheckUrl();
}

function openBookUpdateDialog(book, data) {
  showUpdateDialog((body) => {
    const title = document.createElement("p");
    title.textContent = `《${book.title}》`;
    const counts = document.createElement("p");
    counts.textContent = `书库已抓到第 ${data.chapters} 章 · 来源更新到第 ${data.remote_count} 章`;
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = data.changed
      ? "来源目录在已抓末章处对不上，可能是站点改号或插章，建议去采集整本重抓。"
      : data.update > 0
        ? `有 ${data.update} 章新内容，去采集会从第 ${data.chapters + 1} 章续起。`
        : "已是最新，没有新章节。";
    body.append(title, counts, note);
  });
}

function openCheckAllDialog(results) {
  showUpdateDialog((body) => {
    const summary = document.createElement("p");
    if (!results.length) {
      summary.textContent = "书库里没有可追更的书。";
      body.append(summary);
      return;
    }
    const updates = results.filter((r) => r.update > 0).length;
    const failed = results.filter((r) => r.error).length;
    summary.textContent = `共检查 ${results.length} 本：${updates} 本有更新` +
      (failed ? `，${failed} 本检查失败` : updates ? "" : "，全部已是最新");
    body.append(summary);
    const rows = document.createElement("div");
    rows.className = "update-rows";
    results.forEach((r) => {
      const row = document.createElement("div");
      row.className = "update-row";
      const text = document.createElement("span");
      text.className = "umeta";
      text.textContent = r.error
        ? `${r.title} · 检查失败：${r.error}`
        : `${r.title} · 库中第 ${r.chapters} 章 → 来源第 ${r.remote_count} 章` +
          (r.changed ? "（目录变动）" : r.update ? `（+${r.update} 章）` : "");
      row.append(text);
      if (!r.error) {
        const go = document.createElement("button");
        go.className = "btn ghost";
        go.textContent = r.changed ? "去重抓" : r.update ? "去续更" : "去解析";
        go.addEventListener("click", () => resumeInCapture(r.book_id));
        row.append(go);
      }
      rows.append(row);
    });
    body.append(rows);
  });
}

function checkBookUpdate() {
  const book = state.selectedBook;
  if (!book) return;
  $("bookCheckBtn").disabled = true;
  showLibCheck(`《${book.title}》 ${PROBE_STAGES.fetch}`, 0.05);
  streamFrames(`/api/books/${book.id}/check-update`, (frame) => {
    if (frame.stage)
      showLibCheck(
        `《${book.title}》 ${PROBE_STAGES[frame.stage] || frame.stage}`,
        (PROBE_STAGE_PROGRESS[frame.stage] || 30) / 100
      );
  }).then((data) => {
    hideLibCheck();
    book.follow = {
      chapters: data.chapters,
      update: data.update,
      changed: data.changed,
      remote_count: data.remote_count,
    };
    updateFollowButtons(book);
    updateDialogBook = book;
    $("updateDialogGo").hidden = false;
    $("updateDialogGo").textContent = data.changed ? "去采集重抓" : "去采集续更";
    openBookUpdateDialog(book, data);
    loadBooks();
  }).catch((err) => {
    hideLibCheck();
    $("libSub").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  }).finally(() => { $("bookCheckBtn").disabled = false; });
}

function followBook() {
  const book = state.selectedBook;
  if (!book) return;
  $("bookFollowBtn").disabled = true;
  api(`/api/books/${book.id}/follow`, { method: "POST" }).then((job) => {
    attachJob(job);
    showView("run");
  }).catch((err) => {
    $("libSub").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  }).finally(() => { $("bookFollowBtn").disabled = false; });
}

function checkAllUpdates(silent) {
  $("checkAllBtn").disabled = true;
  showLibCheck("正在准备检查……", 0);
  streamFrames("/api/books/check-all-updates", (frame) => {
    if (typeof frame.total === "number" && typeof frame.done !== "number") {
      showLibCheck(frame.total ? `开始检查 ${frame.total} 本……` : "书库里没有可追更的书", 0);
    } else if (typeof frame.done === "number") {
      showLibCheck(
        `正在检查 ${frame.done}/${frame.total} · 《${frame.title}》`,
        frame.total ? frame.done / frame.total : 1
      );
    }
  }).then((data) => {
    hideLibCheck();
    const updates = data.results.filter((r) => r.update > 0).length;
    updateDialogBook = null;
    $("updateDialogGo").hidden = true;
    if (!silent || updates) openCheckAllDialog(data.results);
    loadBooks();
  }).catch(() => { hideLibCheck(); }).finally(() => { $("checkAllBtn").disabled = false; });
}

$("bookCheckBtn").addEventListener("click", checkBookUpdate);
$("bookFollowBtn").addEventListener("click", followBook);
$("checkAllBtn").addEventListener("click", () => checkAllUpdates(false));
$("updateDialogClose").addEventListener("click", () => $("updateDialog").close());
$("updateDialogGo").addEventListener("click", () => {
  if (updateDialogBook) resumeInCapture(updateDialogBook.id);
});
