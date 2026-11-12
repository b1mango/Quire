/* 追更：NDJSON 流读取、单本检查、全部检查、续抓。依赖 app.js / library.js / capture.js。 */
"use strict";

/* NDJSON 流（probe / 检查更新 / OCR 下载同协议）：普通帧回调，result/error 帧收尾 */
function streamFrames(url, onFrame, body = {}) {
  return fetch(url, {
    method: "POST",
    headers: { "X-Quire-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify(body),
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

/* 跳到采集页，用原链接重新识别；probe 的 library 字段会把起点放在已抓章节之后 */
function resumeInCapture(book) {
  if (!book || !book.source_url) return;
  go("capture");
  captureUrl(book.source_url);
}

function updateLine(r) {
  if (r.error) return html`<small class="bad">检查失败：${r.error}</small>`;
  const tail = r.changed ? "目录有变动，建议整本重抓" : r.update ? `新增 ${r.update} 章` : "已是最新";
  return html`<small class="num">书库第 ${r.chapters} 章 → 来源第 ${r.remote_count} 章 · ${tail}</small>`;
}

/* 单本检查：结果写回 book.follow 并刷新检查器；有更新时给出续抓入口 */
async function checkBookUpdate(book, onStage) {
  const data = await streamFrames(`/api/books/${book.id}/check-update`, (frame) => {
    if (frame.stage && onStage) onStage(frame.stage);
  });
  book.follow = { chapters: data.chapters, update: data.update, changed: data.changed };
  renderSidebar();
  if (data.changed) toast(`《${book.title}》目录有变动，建议整本重抓`, "去重抓", () => resumeInCapture(book));
  else if (data.update > 0) toast(`《${book.title}》新增 ${data.update} 章`, "续抓", () => followBook(book));
  else toast(`《${book.title}》已是最新`);
  return data;
}

/* 一键续抓：服务端按追更进度只抓新章节并合并进原书 */
function followBook(book) {
  return api(`/api/books/${book.id}/follow`, { method: "POST" }).then((job) => {
    trackJob(job, true);
    toast(`已开始续抓《${book.title}》`, "查看", () => go("tasks"));
  }).catch((err) => toast(errText(err)));
}

let checkingAll = false;
function checkAllUpdates(silent) {
  if (checkingAll) return;
  checkingAll = true;
  const btn = $("checkAll"), label = $("checkAllTxt");
  btn.disabled = true;
  label.textContent = "检查中…";
  streamFrames("/api/books/check-all-updates", (frame) => {
    if (typeof frame.done === "number") label.textContent = `检查中 ${frame.done}/${frame.total}`;
  }).then((data) => {
    const results = data.results || [];
    const updates = results.filter((r) => r.update > 0 || r.changed);
    loadBooks();
    if (silent && !updates.length) return;
    if (!results.length) { toast("书库里没有追更中的书"); return; }
    if (!updates.length) { toast(`已检查 ${results.length} 本，全部是最新`); return; }
    toast(`${updates.length} 本有更新`, "查看", () => showCheckResults(results));
    if (!silent) showCheckResults(results);
  }).catch((err) => { if (!silent) toast(errText(err)); })
    .finally(() => { checkingAll = false; btn.disabled = false; label.textContent = "检查更新"; });
}

function showCheckResults(results) {
  const failed = results.filter((r) => r.error).length;
  const body = html`<p>共检查 ${results.length} 本${failed ? `，${failed} 本失败` : ""}</p>
    <div class="rows">${results.map((r) => html`<div><div class="t"><b>${r.title}</b>${updateLine(r)}</div>
      ${!r.error && (r.update > 0 || r.changed)
        ? html`<button class="btn ${r.changed ? "" : "primary"}" data-follow="${r.book_id}" data-changed="${r.changed ? 1 : ""}">${r.changed ? "去重抓" : "续抓"}</button>` : ""}</div>`)}</div>`;
  confirmBox({
    title: "追更检查结果", body, ok: "", cancel: "关闭", wide: true,
    onClick: (e, done) => {
      const b = e.target.closest("[data-follow]");
      if (!b) return false;
      const book = state.books.find((x) => x.id === b.dataset.follow);
      done(false);
      if (book) (b.dataset.changed ? resumeInCapture : followBook)(book);
      return true;
    },
  });
}

$("checkAll").addEventListener("click", () => checkAllUpdates(false));
