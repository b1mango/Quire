/* 导入本地书籍：书库「导入」按钮 / 把文件拖进窗口任意位置。逐个上传到 /api/import，
   后端整理（TXT 转码、分章、去广告、转 EPUB；EPUB/PDF/CBZ/ZIP 校验入库）后进书库。
   右下角导入面板显示每个文件的上传进度、整理结果与失败原因。依赖 app.js / library.js / reader.js。 */
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
  const url = `/api/import?name=${encodeURIComponent(item.name)}${style ? `&style=${encodeURIComponent(style)}` : ""}`;
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
      if (xhr.status === 201) {
        Object.assign(item, { state: "done", book: data });
        loadBooks();
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

function importLine(it) {
  if (it.state === "queued") return "等待中";
  if (it.state === "uploading") return html`上传中 <span class="num">${Math.round(it.progress * 100)}%</span>`;
  if (it.state === "processing") return /\.txt$/i.test(it.name) ? "正在整理：转码、分章、去广告…" : "正在校验并放进书库…";
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
  const failed = imports.items.filter((it) => it.state === "failed").length;
  const head = left ? `正在导入 · 还有 ${left} 个` : failed ? `导入完成 · ${failed} 个没有成功` : "导入完成";
  tray.hidden = false;
  morph(tray, html`<div class="it-head"><b>${head}</b>${left ? "" : html`<button class="btn quiet icon-btn" data-imp="close" aria-label="关闭导入面板" title="关闭">×</button>`}</div>
    <div class="it-list">${imports.items.map((it) => html`<div class="it-row ${it.state}" data-key="i${it.id}">
      <div class="it-main"><div class="it-name" title="${it.name}">${it.book && it.book.title ? it.book.title : it.name}</div>
        <div class="it-sub">${importLine(it)}</div>
        ${it.state === "uploading" || it.state === "processing" ? html`<div class="bar ${it.state === "processing" ? "busy" : ""}"><i style="width:${it.state === "processing" ? 100 : (it.progress * 100).toFixed(1)}%"></i></div>` : ""}
        ${it.state === "done" && (it.book.notes || []).length ? html`<div class="it-notes">${it.book.notes.slice(0, 4).join("；")}</div>` : ""}
        ${it.state === "failed" && it.hint ? html`<div class="it-notes">${it.hint}</div>` : ""}</div>
      ${it.state === "done" ? html`<button class="btn sm" data-imp="open" data-book="${it.book.book_id}">打开</button>` : ""}
      ${it.existing ? html`<button class="btn sm" data-imp="open" data-book="${it.existing}">打开已有</button>` : ""}
    </div>`)}</div>`);
}

$("importTray").addEventListener("click", (e) => {
  const b = e.target.closest("[data-imp]");
  if (!b) return;
  if (b.dataset.imp === "close") { imports.items = []; paintImports(); return; }
  if (b.dataset.imp === "open") openReader(b.dataset.book);
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
