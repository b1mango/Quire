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
  groups: [], filterGroup: null, managing: false, selected: new Set(),
};

/* ------------------------------------------------------------ 标签页状态
   采集台四个标签(小说/漫画 × 目录/单章)各自独立保留 URL 输入、识别结果
   与章节选择;切走再切回看到原样。进行中的识别不后台继续,只保留已渲染结果。 */

const tabStates = {};
function tabKey() { return `${state.kind}:${state.captureMode}`; }

function saveTabState(key) {
  tabStates[key] = {
    url: $("urlInput").value,
    probe: state.probe,
    rangeExpr: $("rangeExpr").value,
    splitMode: $("splitMode").value,
    volumes: [...$("volumeList").querySelectorAll("input")].map((x) => x.checked),
  };
}

function switchTab() {
  /* 调用方已更新 state.kind / state.captureMode;先作废旧标签在途的识别 */
  state.probeRequest = Symbol();
  clearTimeout(probeTimer);
  const saved = tabStates[tabKey()];
  resetProbe();
  if (!saved) {
    $("urlInput").value = "";
    renderFormatChips(); updateKindFields();
    return;
  }
  $("urlInput").value = saved.url;
  $("splitMode").value = saved.splitMode;
  if (saved.probe) {
    renderProbeResult(saved.probe);
    [...$("volumeList").querySelectorAll("input")].forEach((x, i) => {
      if (i < saved.volumes.length) x.checked = saved.volumes[i];
    });
    $("rangeExpr").value = saved.rangeExpr;
    syncSelectsFromExpr();
    updateRangeSummary(); syncSeriesAvailability();
  } else {
    renderFormatChips(); updateKindFields();
  }
}

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
  if (typeof updateQueueJob === "function") updateQueueJob(job);
  appendChapters(job.chapters || []);
  if (job.status === "pending" || job.status === "running") {
    if (job.status === "pending") $("runSub").textContent = "排队中，等前面的任务完成……";
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


function finishRunView(status, data) {
  if (state.job) state.job.status = status;
  if (state.events) { state.events.close(); state.events = null; }
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  if (typeof updateQueueJob === "function") updateQueueJob(state.job);
  $("cancelBtn").hidden = true;
  $("pauseBtn").hidden = true;
  $("runDoneActions").hidden = false;
  state.bookId = data.book_id || null;
  $("openBookBtn").hidden = !state.bookId;
  $("goLibBtn").hidden = !state.bookId;
  $("resumeBtn").hidden = true;
  $("retryBtn").hidden = !(state.lastSpec || (state.job && state.job.spec));
  if (Number.isFinite(data.failures)) {
    $("statFailed").textContent = data.failures;
    $("statDone").textContent = Math.max(0, Number($("statTotal").textContent) - data.failures);
  }
  if (status === "done") {
    $("runTitle").textContent = data.title || $("runTitle").textContent;
    $("runSub").textContent = `完成 · ${humanSize(data.bytes || 0)} · ${data.elapsed_s ?? "—"} 秒`;
    $("meterFill").style.width = "100%";
  } else if (status === "partial") {
    $("runTitle").textContent = data.title || $("runTitle").textContent;
    const cancelled = (data.warnings || []).some((w) => String(w).includes("已取消"));
    $("runSub").textContent = cancelled
      ? "已取消：已抓取的部分已导出为半成品，点「重试」从缓存续抓，不重头"
      : "已完成，但有缺页";
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
  const colors = document.documentElement.dataset.theme === "paper" ? ["#625B50", "#746B5D", "#514D45"] : ["#1F3A5F", "#5B2A2A", "#243B32", "#3A2F52", "#4A3A1E", "#1E3B45", "#432B3B", "#2C3A20"];
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
    state.groups = data.groups || [];
    state.selectedBook = null;
    state.selected = new Set();
    closeBookMenu(false);
    $("checkAllBtn").hidden = !data.books.some((b) => b.follow);
    renderLibrary(search);
  }).catch(() => {});
}

/* ------------------------------------------------------------ 设置 */

function loadSettingsView() {
  api("/api/settings").then((settings) => {
    state.settings = settings;
    $("setOutput").value = settings.output_dir;
    setSeg($("setCompressSeg"), "compress", settings.compress);
    syncCompressDesc($("setCompressSeg"), "setCompressDesc");
    setSeg($("setOcrSeg"), "ocr", settings.ocr);
    $("setConcurrency").value = settings.concurrency;
    $("setRate").value = settings.rate;
    $("setAutoCheck").checked = !!settings.auto_check_updates;
    $("setRobots").checked = !!settings.obey_robots;
    $("setBrowserNative").checked = !!settings.browser_native;
    $("setCdpEndpoint").value = settings.cdp_endpoint || "";
  }).catch((err) => { $("settingsMeta").textContent = err.message; });
  api("/api/capabilities").then((caps) => {
    state.caps = caps;
    syncOcrAvailability();
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
    obey_robots: $("setRobots").checked,
    browser_native: $("setBrowserNative").checked,
    cdp_endpoint: $("setCdpEndpoint").value.trim(),
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
  saveTabState(tabKey());
  state.kind = kind; switchTab();
});
bindSeg($("captureSeg"), "capture", (mode) => {
  saveTabState(tabKey());
  state.captureMode = mode; switchTab();
});
$("splitMode").addEventListener("change", recheckUrl);
document.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", () => {
  $("commandMenu").close(); showView(button.dataset.command);
}));
bindSeg($("compressSeg"), "compress", () => syncCompressDesc($("compressSeg"), "compressDesc"));
bindSeg($("ocrSeg"), "ocr", syncOcrAvailability);
bindSeg($("setCompressSeg"), "compress", () => syncCompressDesc($("setCompressSeg"), "setCompressDesc"));
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
  /* 暂停是协作式收尾，落定需要几秒；立即给出反馈，免得误以为没生效 */
  $("pauseBtn").disabled = true;
  $("pauseBtn").textContent = "暂停中…";
  $("runSub").textContent = "正在暂停：进行中的页面先收尾，已抓取的部分保留在缓存里";
  api(`/api/jobs/${state.job.id}/pause`, { method: "POST" }).catch(() => {
    $("pauseBtn").disabled = false;
    $("pauseBtn").textContent = "暂停";
    $("runSub").textContent = "暂停请求没发出去，请再试一次";
  });
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
  if (state.selectedBook) openLibraryBook(state.selectedBook);
});
$("bookRevealBtn").addEventListener("click", () => {
  if (!state.selectedBook) return;
  api(`/api/books/${state.selectedBook.id}/reveal`, { method: "POST" }).catch((err) => {
    $("libSub").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  });
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
  if ($("commandMenu").open || $("deleteBookDialog").open || $("groupDialog").open) return;
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
