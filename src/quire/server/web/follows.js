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
    $("libSub").textContent = data.changed
      ? "目录在已抓末章处对不上，建议整本重抓"
      : data.update > 0
        ? `有 ${data.update} 章新内容`
        : "已是最新";
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
    const failed = data.results.filter((r) => r.error).length;
    if (!silent || updates) {
      $("libSub").textContent =
        `检查完成：${updates} 本有更新` + (failed ? ` · ${failed} 本检查失败` : "");
    }
    loadBooks();
  }).catch(() => { hideLibCheck(); }).finally(() => { $("checkAllBtn").disabled = false; });
}

$("bookCheckBtn").addEventListener("click", checkBookUpdate);
$("bookFollowBtn").addEventListener("click", followBook);
$("checkAllBtn").addEventListener("click", () => checkAllUpdates(false));
