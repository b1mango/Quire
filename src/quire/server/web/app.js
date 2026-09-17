/* 卷帙 Quire Web UI — 零构建原生 JS。所有动态文本走 textContent，不拼 HTML。 */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

const FORMATS = { manga: [["pdf", "PDF"], ["cbz", "CBZ"], ["zip", "原图 ZIP"]],
                  novel: [["epub", "EPUB"], ["txt", "TXT"], ["pdf", "PDF"]] };

const state = {
  caps: null, settings: null, probe: null, kind: "novel", captureMode: "catalogue",
  job: null, events: null, lastSeq: 0, startedAt: 0, timer: null,
  bookId: null, books: [], selectedBook: null, lastSpec: null,
};

/* ------------------------------------------------------------ 基础 */

function api(path, options = {}) {
  const init = { headers: { "X-Quire-Token": TOKEN }, ...options };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers["Content-Type"] = "application/json";
  }
  return fetch(path, init).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `请求失败（${res.status}）`);
      err.hint = data.hint;
      throw err;
    }
    return data;
  });
}

function humanSize(num) {
  let value = num;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return `${value.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    value /= 1024;
  }
  return `${num} B`;
}

function setSeg(seg, attr, value) {
  seg.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset[attr] === value)));
}
function segValue(seg, attr) {
  const pressed = seg.querySelector('button[aria-pressed="true"]');
  return pressed ? pressed.dataset[attr] : null;
}
function bindSeg(seg, attr, onChange) {
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    setSeg(seg, attr, b.dataset[attr]);
    if (onChange) onChange(b.dataset[attr]);
  });
}

function showView(name) {
  document.querySelectorAll("[data-view-btn]").forEach((x) =>
    x.setAttribute("aria-current", String(x.dataset.viewBtn === name)));
  document.querySelectorAll(".view").forEach((x) => { x.hidden = x.dataset.view !== name; });
  if (name === "lib") loadBooks();
}

/* ------------------------------------------------------------ 主题 */

const PRESETS = {
  "paper:light": ["paper", "light"], "darkroom:light": ["darkroom", "light"],
  "darkroom:dark": ["darkroom", "dark"], "swiss:light": ["swiss", "light"],
};
function applyPreset(key, persist) {
  const [theme, mode] = PRESETS[key] || PRESETS["paper:light"];
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.mode = mode;
  document.querySelectorAll("[data-preset]").forEach((x) =>
    x.setAttribute("aria-pressed", String(x.dataset.preset === key)));
  localStorage.setItem("quire-theme", key);
  if (persist) api("/api/settings", { method: "PUT", body: { theme: key } }).catch(() => {});
}
document.querySelectorAll("[data-preset]").forEach((b) =>
  b.addEventListener("click", () => applyPreset(b.dataset.preset, true)));

/* ------------------------------------------------------------ 新建任务 */

/* ------------------------------------------------------------ 进行中 */

function attachJob(job) {
  if (state.events) { state.events.close(); state.events = null; }
  state.job = job;
  state.bookId = job.book_id || null;
  state.lastSeq = 0;
  state.startedAt = Date.now();
  resetRunView(job);
  appendChapters(job.chapters || []);
  if (job.status === "pending" || job.status === "running") {
    const source = new EventSource(`/api/jobs/${job.id}/events?token=${encodeURIComponent(TOKEN)}`);
    state.events = source;
    ["phase", "progress", "thumb", "volume", "chapter", "done", "failed", "cancelled", "paused"].forEach((kind) =>
      source.addEventListener(kind, (e) => {
        state.lastSeq = parseInt(e.lastEventId, 10) || state.lastSeq;
        onJobEvent(kind, JSON.parse(e.data));
      }));
  } else {
    finishRunView(job.status, { message: job.error, hint: job.hint, book_id: job.book_id,
      failures: job.failed_pages, partial: job.status === "partial", title: job.title });
  }
}

function resetRunView(job) {
  $("runTitle").textContent = job.title || "准备中";
  $("runSub").textContent = "正在连接……";
  $("stream").textContent = "";
  resetChapters();
  $("runEmpty").hidden = false;
  $("partialNote").hidden = true;
  $("failedNote").hidden = true;
  $("runDoneActions").hidden = true;
  $("retryBtn").hidden = true;
  $("resumeBtn").hidden = true;
  $("cancelBtn").hidden = false;
  $("pauseBtn").hidden = false;
  $("meterFill").style.width = "0";
  $("statDone").textContent = "0";
  $("statFailed").textContent = "0";
  $("statTotal").textContent = "—";
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    $("statElapsed").textContent = ((Date.now() - state.startedAt) / 1000).toFixed(1) + " s";
  }, 200);
}

function onJobEvent(kind, data) {
  if (kind === "phase") {
    $("runSub").textContent = { fetching: "正在抓取页面……", encoding: "正在压缩与合成……" }[data.phase] || data.phase;
  } else if (kind === "progress") {
    $("statDone").textContent = data.done;
    $("statFailed").textContent = data.failed;
    $("statTotal").textContent = data.total;
    if (data.total) $("meterFill").style.width = `${((data.done + data.failed) / data.total) * 100}%`;
  } else if (kind === "volume") {
    state.bookId = data.book_id;
    $("runSub").textContent = `已交付 ${data.done} 卷 · ${data.title}`;
    $("runDoneActions").hidden = false;
  } else if (kind === "thumb") {
    $("runEmpty").hidden = true;
    const tile = document.createElement("div");
    tile.className = "thumb";
    const img = document.createElement("img");
    img.src = `${data.url}?token=${encodeURIComponent(TOKEN)}`;
    img.alt = "";
    const pg = document.createElement("span");
    pg.className = "pg";
    pg.textContent = data.page;
    tile.append(img, pg);
    $("stream").appendChild(tile);
  } else if (kind === "chapter") {
    appendChapters([data]);
  } else if (kind === "done") {
    finishRunView(data.partial ? "partial" : "done", data);
  } else if (kind === "failed" || kind === "cancelled" || kind === "paused") {
    finishRunView(kind, data);
  }
}

function finishRunView(status, data) {
  if (state.job) state.job.status = status;
  if (state.events) { state.events.close(); state.events = null; }
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  $("cancelBtn").hidden = true;
  $("pauseBtn").hidden = true;
  $("runDoneActions").hidden = false;
  state.bookId = data.book_id || null;
  $("openBookBtn").hidden = !state.bookId;
  $("goLibBtn").hidden = !state.bookId;
  $("resumeBtn").hidden = true;
  $("retryBtn").hidden = !(state.lastSpec || (state.job && state.job.spec));
  if (status === "done") {
    $("runTitle").textContent = data.title || $("runTitle").textContent;
    $("runSub").textContent = `完成 · ${humanSize(data.bytes || 0)} · ${data.elapsed_s ?? "—"} 秒`;
    $("meterFill").style.width = "100%";
  } else if (status === "partial") {
    $("runTitle").textContent = data.title || $("runTitle").textContent;
    $("runSub").textContent = "已完成，但有缺页";
    $("partialNote").hidden = false;
    $("partialNote").textContent =
      `${data.failures} 页缺失，已用占位页补齐。修正链接后点「重试」可以只补缺失的部分。` +
      (data.warnings && data.warnings.length ? " " + data.warnings[0] : "");
    $("meterFill").style.width = "100%";
  } else if (status === "cancelled") {
    $("runSub").textContent = "已取消，已下载的部分会保留";
  } else if (status === "paused") {
    $("runSub").textContent = "已暂停，已抓取的部分保留在缓存里，点「继续」接着抓";
    $("resumeBtn").hidden = !(state.lastSpec || (state.job && state.job.spec));
    $("retryBtn").hidden = true;
  } else {
    $("runSub").textContent = "没有完成";
    $("failedNote").hidden = false;
    $("failedNote").textContent = data.hint ? `${data.message} ${data.hint}` : (data.message || "任务失败");
  }
}

/* ------------------------------------------------------------ 书库 */

function coverPlaceholder(title, seedText) {
  let seed = 0;
  for (const ch of seedText) seed = (seed * 31 + ch.codePointAt(0)) >>> 0;
  const colors = ["#1F3A5F", "#5B2A2A", "#243B32", "#3A2F52", "#4A3A1E", "#1E3B45", "#432B3B", "#2C3A20"];
  const c = colors[seed % colors.length], c2 = colors[(seed >> 3) % colors.length];
  const ch = (title || "书").slice(0, 1);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420">` +
    `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs>` +
    `<rect width="300" height="420" fill="url(#g)"/>` +
    `<text x="150" y="228" font-size="150" font-family="Songti SC,STSong,serif" fill="#fff" opacity=".92" text-anchor="middle">${ch}</text>` +
    `</svg>`;
  return "data:image/svg+xml," + encodeURIComponent(svg);
}

function loadBooks() {
  const search = $("searchInput").value.trim();
  api("/api/books" + (search ? `?search=${encodeURIComponent(search)}` : "")).then((data) => {
    state.books = data.books;
    state.selectedBook = null;
    $("bookActions").hidden = true;
    $("checkAllBtn").hidden = !data.books.some((b) => b.follow);
    const grid = $("grid");
    grid.textContent = "";
    $("libEmpty").hidden = data.books.length > 0;
    const total = data.books.reduce((sum, b) => sum + b.bytes, 0);
    $("libSub").textContent = search
      ? `“${search}” · ${data.books.length} 部`
      : `${data.books.length} 部作品 · 共 ${humanSize(total)}`;
    data.books.forEach((book) => grid.appendChild(bookCard(book)));
  }).catch(() => {});
}

function bookCard(book) {
  const card = document.createElement("button");
  card.className = "card";
  card.type = "button";
  const cover = document.createElement("div");
  cover.className = "cover";
  const img = document.createElement("img");
  img.alt = "";
  img.src = book.cover ? `${book.cover}?token=${encodeURIComponent(TOKEN)}`
                       : coverPlaceholder(book.title, book.id);
  cover.appendChild(img);
  if (typeof followBadge === "function") {
    const badge = followBadge(book);
    if (badge) cover.appendChild(badge);
  }
  const title = document.createElement("div");
  title.className = "ct";
  title.textContent = book.title;
  const meta = document.createElement("div");
  meta.className = "cm";
  meta.textContent = `${book.formats.join(" / ").toUpperCase()} · ${humanSize(book.bytes)}`;
  card.append(cover, title, meta);
  card.addEventListener("click", () => selectBook(book, card));
  return card;
}

function selectBook(book, card) {
  state.selectedBook = book;
  document.querySelectorAll(".card").forEach((c) => c.setAttribute("aria-current", String(c === card)));
  $("bookActions").hidden = false;
  $("bookActionsTitle").textContent = book.title;
  if (typeof updateFollowButtons === "function") updateFollowButtons(book);
}

/* ------------------------------------------------------------ 设置 */

function loadSettingsView() {
  api("/api/settings").then((settings) => {
    state.settings = settings;
    $("setOutput").value = settings.output_dir;
    setSeg($("setCompressSeg"), "compress", settings.compress);
    setSeg($("setOcrSeg"), "ocr", settings.ocr);
    $("setConcurrency").value = settings.concurrency;
    $("setRate").value = settings.rate;
    $("setAutoCheck").checked = !!settings.auto_check_updates;
  }).catch((err) => { $("settingsMeta").textContent = err.message; });
  api("/api/capabilities").then((caps) => {
    state.caps = caps;
    updateChromeHint();
    const dl = $("doctorList");
    dl.textContent = "";
    const rows = [
      ["版本", `quire ${caps.version}`],
      ["Chrome", caps.chrome || "未找到（小说转 PDF 需要）"],
      ["tesseract", caps.ocr.tesseract ? "已安装" : "未安装"],
      ["内置 OCR", caps.ocr.onnxruntime ? (caps.ocr.models_ready ? "模型已就绪" : "模型按需下载") : "未安装（quire-local[ocr]）"],
      ["数据目录", caps.data_dir],
      ["输出目录", caps.output_dir],
    ];
    rows.forEach(([key, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value;
      dl.append(dt, dd);
    });
    const foot = $("sideFoot");
    foot.textContent = "";
    const chromeDot = document.createElement("span");
    chromeDot.className = "dot" + (caps.chrome ? "" : " off");
    foot.append(chromeDot, document.createTextNode(caps.chrome ? "Chrome 已就绪" : "缺 Chrome"), document.createElement("br"));
    const ocrDot = document.createElement("span");
    ocrDot.className = "dot" + (caps.ocr.models_ready || caps.ocr.tesseract ? "" : " off");
    foot.append(ocrDot, document.createTextNode(caps.ocr.models_ready ? "OCR 模型已缓存" : "OCR 按需下载"));
  }).catch(() => {});
}

function saveSettings() {
  const payload = {
    output_dir: $("setOutput").value.trim(),
    compress: segValue($("setCompressSeg"), "compress"),
    ocr: segValue($("setOcrSeg"), "ocr"),
    concurrency: parseInt($("setConcurrency").value, 10),
    rate: parseFloat($("setRate").value),
    auto_check_updates: $("setAutoCheck").checked,
  };
  api("/api/settings", { method: "PUT", body: payload }).then((settings) => {
    state.settings = settings;
    $("settingsMeta").textContent = "已保存";
  }).catch((err) => { $("settingsMeta").textContent = err.message; });
}

/* ------------------------------------------------------------ 事件绑定 */

document.querySelectorAll("[data-view-btn]").forEach((b) =>
  b.addEventListener("click", () => showView(b.dataset.viewBtn)));
bindSeg($("kindSeg"), "kind", (kind) => {
  state.kind = kind; renderFormatChips(); updateKindFields(); recheckUrl();
});
bindSeg($("captureSeg"), "capture", (mode) => {
  state.captureMode = mode; updateKindFields(); recheckUrl();
});
$("splitMode").addEventListener("change", recheckUrl);
document.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", () => {
  $("commandMenu").close(); showView(button.dataset.command);
}));
bindSeg($("compressSeg"), "compress");
bindSeg($("ocrSeg"), "ocr");
bindSeg($("setCompressSeg"), "compress");
bindSeg($("setOcrSeg"), "ocr");

$("pasteBtn").addEventListener("click", pasteUrl);
let probeTimer = null;
$("urlInput").addEventListener("input", () => {
  resetProbe(); clearTimeout(probeTimer);
  const url = $("urlInput").value.trim();
  if (/^https?:\/\/.+/.test(url)) probeTimer = setTimeout(() => probeUrl(url), 500);
});
$("startBtn").addEventListener("click", startJob);
$("cancelBtn").addEventListener("click", () => {
  if (state.job) api(`/api/jobs/${state.job.id}/cancel`, { method: "POST" }).catch(() => {});
});
$("pauseBtn").addEventListener("click", () => {
  if (!state.job) return;
  $("pauseBtn").disabled = true;
  api(`/api/jobs/${state.job.id}/pause`, { method: "POST" })
    .catch(() => {})
    .finally(() => { $("pauseBtn").disabled = false; });
});
function resubmitSpec() {
  const spec = state.lastSpec || (state.job && state.job.spec);
  if (!spec) return;
  api("/api/jobs", { method: "POST", body: spec }).then((job) => attachJob(job)).catch((err) => {
    $("failedNote").hidden = false;
    $("failedNote").textContent = err.message;
  });
}
$("resumeBtn").addEventListener("click", resubmitSpec);
$("goLibBtn").addEventListener("click", () => showView("lib"));
$("emptyNewBtn").addEventListener("click", () => showView("new"));
$("openBookBtn").addEventListener("click", () => {
  if (state.bookId) api(`/api/books/${state.bookId}/open`, { method: "POST" }).catch(() => {});
});
$("retryBtn").addEventListener("click", resubmitSpec);
$("bookOpenBtn").addEventListener("click", () => {
  if (state.selectedBook) api(`/api/books/${state.selectedBook.id}/open`, { method: "POST" }).catch(() => {});
});
$("bookRevealBtn").addEventListener("click", () => {
  if (!state.selectedBook) return;
  api(`/api/books/${state.selectedBook.id}/reveal`, { method: "POST" }).catch((err) => {
    $("libSub").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  });
});
$("bookDeleteBtn").addEventListener("click", () => {
  const book = state.selectedBook;
  if (!book) return;
  if (!window.confirm(`删除《${book.title}》？成品文件会一起删掉。`)) return;
  api(`/api/books/${book.id}`, { method: "DELETE" }).then(() => loadBooks()).catch((err) => {
    $("libSub").textContent = err.message;
  });
});
$("bookActionsClose").addEventListener("click", () => {
  state.selectedBook = null;
  $("bookActions").hidden = true;
  document.querySelectorAll(".card").forEach((c) => c.setAttribute("aria-current", "false"));
});
let searchTimer = null;
$("searchInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadBooks, 250);
});
$("saveSettingsBtn").addEventListener("click", saveSettings);
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault(); $("commandMenu").showModal(); return;
  }
  if ($("commandMenu").open) return;
  if (e.key === "Escape" && state.job && (state.job.status === "running" || state.job.status === "pending")) {
    api(`/api/jobs/${state.job.id}/cancel`, { method: "POST" }).catch(() => {});
  }
});

/* ------------------------------------------------------------ 启动 */

(function init() {
  if (!TOKEN) {
    document.querySelector(".content").textContent = "缺少访问令牌：请从 quire ui 打印的地址进入。";
    return;
  }
  // 主题锚点（与 ui-preview 一致）：#paper / #darkroom / #darkroom,light / #swiss
  const anchor = { paper: "paper:light", darkroom: "darkroom:dark",
                   "darkroom,light": "darkroom:light", swiss: "swiss:light" }[
    (location.hash || "").replace("#", "")
  ];
  if (anchor) applyPreset(anchor, false);
  api("/api/settings").then((settings) => {
    if (!anchor) applyPreset(settings.theme || localStorage.getItem("quire-theme") || "paper:light", false);
    if (settings.auto_check_updates && typeof checkAllUpdates === "function") checkAllUpdates(true);
  }).catch(() => {
    if (!anchor) applyPreset(localStorage.getItem("quire-theme") || "paper:light", false);
  });
  renderFormatChips(); updateKindFields();
  loadSettingsView();
  api("/api/jobs").then((data) => {
    const active = data.jobs.find((j) => j.status === "running" || j.status === "pending");
    if (active) { attachJob(active); showView("run"); return; }
    const paused = data.jobs.find((j) => j.status === "paused");
    if (paused) { attachJob(paused); showView("run"); }
  }).catch(() => {});
})();
