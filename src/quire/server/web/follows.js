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

function checkBookUpdate() {
  const book = state.selectedBook;
  if (!book) return;
  $("bookCheckBtn").disabled = true;
  api(`/api/books/${book.id}/check-update`, { method: "POST" }).then((data) => {
    book.follow = { chapters: data.chapters, update: data.update, changed: data.changed };
    updateFollowButtons(book);
    $("libSub").textContent = data.changed
      ? "目录在已抓末章处对不上，建议整本重抓"
      : data.update > 0
        ? `有 ${data.update} 章新内容`
        : "已是最新";
    loadBooks();
  }).catch((err) => {
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
  api("/api/books/check-all-updates", { method: "POST" }).then((data) => {
    const updates = data.results.filter((r) => r.update > 0).length;
    const failed = data.results.filter((r) => r.error).length;
    if (!silent || updates) {
      $("libSub").textContent =
        `检查完成：${updates} 本有更新` + (failed ? ` · ${failed} 本检查失败` : "");
    }
    loadBooks();
  }).catch(() => {}).finally(() => { $("checkAllBtn").disabled = false; });
}

$("bookCheckBtn").addEventListener("click", checkBookUpdate);
$("bookFollowBtn").addEventListener("click", followBook);
$("checkAllBtn").addEventListener("click", () => checkAllUpdates(false));
