/* 导入本地书籍：书库「导入」按钮 / 把文件拖进窗口任意位置。两步：
   1. 逐个上传到 /api/import?preview=1，后端识别书名、作者、类型（ZIP 里装书时取出书文件），暂不入库；
   2. 右下角导入面板里确认或修改书名作者 → POST /api/import/<令牌> 整理入库（TXT 转码、分章、去广告、
      转 EPUB；EPUB 去广告段；PDF/CBZ/ZIP 校验），或「跳过」→ DELETE 放弃。
   依赖 app.js / library.js / reader.js。 */
"use strict";

const IMPORT_EXT = /\.(txt|epub|pdf|cbz|zip)$/i;
const imports = { items: [], busy: false, seq: 0, depth: 0 };

function importFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (imports.items.length && imports.items.every((it) => it.state === "done" || it.state === "failed")) imports.items = [];
  files.forEach((file) => {
    const ok = IMPORT_EXT.test(file.name);
    imports.items.push({
      id: ++imports.seq, file, name: file.name, size: file.size,
      state: ok ? "queued" : "failed", progress: 0,
      error: ok ? "" : "暂不支持这种文件，可导入 TXT、EPUB、PDF、CBZ、ZIP",
    });
  });
  paintImports();
  pumpImports();
}

function pumpImports() {
  if (imports.busy) return;
  const next = imports.items.find((it) => it.state === "queued");
  if (!next) return;
  imports.busy = true;
  uploadOne(next).finally(() => { imports.busy = false; paintImports(); pumpImports(); });
}

function uploadOne(item) {
  item.state = "uploading";
  paintImports();
  const style = (state.settings && state.settings.epub_style) || "";
  item.style = style;
  const url = `/api/import?preview=1&name=${encodeURIComponent(item.name)}`;
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-Quire-Token", TOKEN);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      item.progress = e.loaded / e.total;
      if (item.progress >= 1) item.state = "processing"; // 上传完，后端在整理
      paintImports();
    };
    xhr.upload.onload = () => { item.state = "processing"; paintImports(); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { /* 非 JSON 时按状态码报错 */ }
      if (xhr.status === 200) {
        Object.assign(item, { state: "confirm", preview: data, title: data.title || "", author: data.author || "" });
      } else {
        item.state = "failed";
        item.error = data.error || `导入失败（${xhr.status}）`;
        item.hint = data.hint || "";
        if (xhr.status === 409 && data.book_id) item.existing = data.book_id;
      }
      resolve();
    };
    xhr.onerror = () => { item.state = "failed"; item.error = "连接中断，没能上传"; resolve(); };
    xhr.send(item.file);
  });
}

function commitImport(item) {
  if (item.state !== "confirm") return;
  item.state = "processing";
  paintImports();
  const body = { title: item.title.trim(), author: item.author.trim(), style: item.style };
  api(`/api/import/${item.preview.token}`, { method: "POST", body }).then((book) => {
    Object.assign(item, { state: "done", book });
    loadBooks();
  }).catch((err) => {
    Object.assign(item, { state: "failed", error: err.message, hint: err.hint || "" });
    if (err.bookId) item.existing = err.bookId;
  }).finally(paintImports);
}
function skipImport(item) {
  if (item.state !== "confirm") return;
  api(`/api/import/${item.preview.token}`, { method: "DELETE" }).catch(() => {}); // 过期也无妨：后端 2 小时后自动清
  item.state = "skipped";
  paintImports();
}
const IMPORT_KIND = { novel: "小说", manga: "漫画" };
function previewLine(p) {
  const what = p.format === "txt" ? "将转码、分章、去广告，转为 EPUB + TXT"
    : p.format === "epub" ? `${p.chapters ? `${p.chapters} 章 · ` : ""}将删除广告段落`
    : p.pages ? `${p.pages} 页` : "";
  return `${String(p.format || "").toUpperCase()} · ${IMPORT_KIND[p.kind] || ""}${what ? ` · ${what}` : ""}`;
}

function importLine(it) {
  if (it.state === "queued") return "等待中";
  if (it.state === "uploading") return html`上传中 <span class="num">${Math.round(it.progress * 100)}%</span>`;
  if (it.state === "processing") return it.preview && it.preview.format === "txt" ? "正在整理：转码、分章、去广告…" : "正在整理并放进书库…";
  if (it.state === "confirm") return previewLine(it.preview);
  if (it.state === "skipped") return "已跳过";
  if (it.state === "done") {
    const b = it.book || {};
    const count = b.chapters ? `${b.chapters} 章` : b.pages ? `${b.pages} 页` : "";
    return html`<span class="ok">已导入</span>${count ? ` · ${count}` : ""}${(b.formats || []).length ? ` · ${b.formats.map((f) => f.toUpperCase()).join(" + ")}` : ""}`;
  }
  return html`<span class="bad">${it.error}</span>`;
}

function paintImports() {
  const tray = $("importTray");
  if (!imports.items.length) { tray.hidden = true; return; }
  const left = imports.items.filter((it) => it.state === "queued" || it.state === "uploading" || it.state === "processing").length;
  const waiting = imports.items.filter((it) => it.state === "confirm").length;
  const failed = imports.items.filter((it) => it.state === "failed").length;
  const head = waiting ? `确认书名和作者 · ${waiting} 本待导入` : left ? `正在导入 · 还有 ${left} 个`
    : failed ? `导入完成 · ${failed} 个没有成功` : "导入完成";
  tray.hidden = false;
  morph(tray, html`<div class="it-head"><b>${head}</b>${waiting > 1 ? html`<button class="btn sm primary" data-imp="all">全部导入</button>` : ""}
    ${left || waiting ? "" : html`<button class="btn quiet icon-btn" data-imp="close" aria-label="关闭导入面板" title="关闭">×</button>`}</div>
    <div class="it-list">${imports.items.map((it) => html`<div class="it-row is-${it.state}" data-key="i${it.id}">
      <div class="it-main">${it.state === "confirm" ? html`<div class="it-file" title="${it.name}">${it.name}</div>
          <label class="it-field"><span>书名</span><input class="field-input" data-f="title" value="${it.title}" placeholder="书名" spellcheck="false"></label>
          <label class="it-field"><span>作者</span><input class="field-input" data-f="author" value="${it.author}" placeholder="未知" spellcheck="false"></label>`
        : html`<div class="it-name" title="${it.name}">${it.book && it.book.title ? it.book.title : it.name}</div>`}
        <div class="it-sub">${importLine(it)}</div>
        ${it.state === "uploading" || it.state === "processing" ? html`<div class="bar ${it.state === "processing" ? "busy" : ""}"><i style="width:${it.state === "processing" ? 100 : (it.progress * 100).toFixed(1)}%"></i></div>` : ""}
        ${it.state === "done" && (it.book.notes || []).length ? html`<div class="it-notes">${it.book.notes.slice(0, 4).join("；")}</div>` : ""}
        ${it.state === "failed" && it.hint ? html`<div class="it-notes">${it.hint}</div>` : ""}
        ${it.state === "confirm" && (it.preview.notes || []).length ? html`<div class="it-notes">${it.preview.notes.join("；")}</div>` : ""}</div>
      ${it.state === "confirm" ? html`<div class="it-acts"><button class="btn sm primary" data-imp="go" data-id="${it.id}">导入</button><button class="btn sm quiet" data-imp="skip" data-id="${it.id}">跳过</button></div>` : ""}
      ${it.state === "done" ? html`<button class="btn sm" data-imp="open" data-book="${it.book.book_id}">打开</button>` : ""}
      ${it.existing ? html`<button class="btn sm" data-imp="open" data-book="${it.existing}">打开已有</button>` : ""}
    </div>`)}</div>`);
}

const importItem = (el) => imports.items.find((it) => `i${it.id}` === el.closest("[data-key]")?.dataset.key);
$("importTray").addEventListener("click", (e) => {
  const b = e.target.closest("[data-imp]");
  if (!b) return;
  const act = b.dataset.imp;
  if (act === "close") { imports.items = []; paintImports(); return; }
  if (act === "open") openReader(b.dataset.book);
  if (act === "all") imports.items.filter((it) => it.state === "confirm").forEach(commitImport);
  if (act === "go") commitImport(importItem(b));
  if (act === "skip") skipImport(importItem(b));
});
$("importTray").addEventListener("input", (e) => {
  const it = e.target.dataset.f && importItem(e.target);
  if (it) it[e.target.dataset.f] = e.target.value;
});
$("importTray").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset.f && !e.isComposing) commitImport(importItem(e.target));
});

/* ------------------------------------------------------------ 入口：按钮与拖放 */

$("importBtn").addEventListener("click", () => $("importFile").click());
$("importFile").addEventListener("change", (e) => { importFiles(e.target.files); e.target.value = ""; });
document.addEventListener("click", (e) => { if (e.target.closest("[data-import]")) $("importFile").click(); });

const hasFiles = (e) => [...(e.dataTransfer && e.dataTransfer.types) || []].includes("Files");
window.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  imports.depth += 1;
  $("dropZone").hidden = false;
});
window.addEventListener("dragover", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); // 否则 WebView 会把文件当网页打开
  e.dataTransfer.dropEffect = "copy";
});
window.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  imports.depth = Math.max(0, imports.depth - 1);
  if (!imports.depth) $("dropZone").hidden = true;
});
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  imports.depth = 0;
  $("dropZone").hidden = true;
  importFiles(e.dataTransfer.files);
});
