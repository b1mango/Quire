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
  booksSeq: 0, booksError: false,
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

/* 封面规则:有真实封面用真实封面(漫画=首章首页);没有则生成文字封面——
   书名前 8 字分两行居中,细线内框 + 双色渐变(按书名散列,与主题协调)。 */
function coverPlaceholder(title, seedText) {
  let seed = 0;
  for (const ch of seedText) seed = (seed * 31 + ch.codePointAt(0)) >>> 0;
  const colors = document.documentElement.dataset.theme === "paper" ? ["#625B50", "#746B5D", "#514D45"] : ["#1F3A5F", "#5B2A2A", "#243B32", "#3A2F52", "#4A3A1E", "#1E3B45", "#432B3B", "#2C3A20"];
  const c = colors[seed % colors.length], c2 = colors[(seed >> 3) % colors.length];
  const chars = (title || "书").slice(0, 8);
  const lines = [];
  for (let i = 0; i < chars.length; i += 4) lines.push(chars.slice(i, i + 4));
  const fontSize = lines.length > 1 ? 52 : 64;
  const startY = 210 - (lines.length - 1) * (fontSize * 0.72);
  const text = lines.map((line, i) =>
    `<text x="150" y="${startY + i * fontSize * 1.35}" font-size="${fontSize}" font-family="Songti SC,STSong,serif" fill="#fff" opacity=".94" text-anchor="middle" letter-spacing="4">${line}</text>`
  ).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420">` +
    `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs>` +
    `<rect width="300" height="420" fill="url(#g)"/>` +
    `<rect x="16" y="16" width="268" height="388" fill="none" stroke="#fff" stroke-opacity=".28" stroke-width="1"/>` +
    text +
    `<line x1="110" y1="330" x2="190" y2="330" stroke="#fff" stroke-opacity=".5" stroke-width="1"/>` +
    `</svg>`;
  return "data:image/svg+xml," + encodeURIComponent(svg);
}

function loadBooks() {
  const search = $("searchInput").value.trim();
  const seq = ++state.booksSeq;
  state.booksError = false;
  syncLibRetry();
  return api("/api/books" + (search ? `?search=${encodeURIComponent(search)}` : "")).then((data) => {
    if (seq !== state.booksSeq) return;
    state.books = data.books;
    state.groups = data.groups || [];
    state.selectedBook = null;
    state.selected = new Set();
    closeBookMenu(false);
    $("checkAllBtn").hidden = !data.books.some((b) => b.follow);
    renderLibrary(search);
  }).catch((err) => {
    if (seq !== state.booksSeq) return;
    state.booksError = true;
    syncLibRetry();
    $("libSub").textContent = `书库加载失败：${err.message || "网络错误"}，请重试`;
  });
}

function syncLibRetry() {
  $("libRetryBtn").hidden = !state.booksError;
}

/* ------------------------------------------------------------ 设置 */

function loadSettingsView() {
  api("/api/settings").then((settings) => {
    state.settings = settings;
    $("setOutput").value = settings.output_dir;
    setSeg($("setCompressSeg"), "compress", settings.compress);
    syncCompressDesc($("setCompressSeg"), "setCompressDesc");
    setSeg($("setOcrSeg"), "ocr", settings.ocr);
    $("setTaskNovel").value = settings.task_novel_mb;
    $("setTaskManga").value = settings.task_manga_mb;
    syncSplitSizeOption();
    $("setConcurrency").value = settings.concurrency;
    $("setRate").value = settings.rate;
    $("setAutoCheck").checked = !!settings.auto_check_updates;
    $("setRobots").checked = !!settings.obey_robots;
    $("setBrowserNative").checked = !!settings.browser_native;
    $("setCdpEndpoint").value = settings.cdp_endpoint || "";
  }).catch((err) => { $("settingsMeta").textContent = err.message; });
  api("/api/capabilities").then(renderDoctor).catch(() => {});
}

function updateOcrModule(caps) {
  const ocr = (caps && caps.ocr) || {};
  const status = $("ocrModuleStatus");
  $("ocrDownloadBtn").hidden = true;
  if (ocr.tesseract) {
    status.textContent = "tesseract 已就绪，图片正文可直接识别";
  } else if (!ocr.onnxruntime) {
    status.textContent = "没有 OCR 引擎（应用未含内置引擎；也可以安装 tesseract）";
  } else if (ocr.models_ready) {
    status.textContent = "内置 OCR 已就绪";
  } else {
    status.textContent = "内置引擎就绪，模型未下载（约 16 MB）";
    $("ocrDownloadBtn").hidden = false;
  }
}

function renderDoctor(caps) {
  state.caps = caps;
  syncOcrAvailability();
  updateChromeHint();
  const dl = $("doctorList");
  dl.textContent = "";
  const rows = [
    ["版本", `quire ${caps.version}`],
    ["Chrome", caps.chrome || "未找到（小说转 PDF 需要）"],
    ["tesseract", caps.ocr.tesseract ? "已安装" : "未安装"],
    ["内置 OCR", caps.ocr.onnxruntime ? (caps.ocr.models_ready ? "模型已就绪" : "模型未下载") : "未安装（quire-local[ocr]）"],
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
  updateOcrModule(caps);
}

function saveSettings() {
  const concurrency = parseInt($("setConcurrency").value, 10);
  const rate = parseFloat($("setRate").value);
  const taskNovel = parseInt($("setTaskNovel").value, 10);
  const taskManga = parseInt($("setTaskManga").value, 10);
  const fieldError = $("setFieldError");
  if (!Number.isFinite(concurrency) || concurrency < 1 || concurrency > 32) {
    fieldError.textContent = "并发须为 1–32 的整数";
    fieldError.hidden = false;
    $("setConcurrency").focus();
    return;
  }
  if (!Number.isFinite(rate) || rate <= 0 || rate > 10) {
    fieldError.textContent = "每站限速须为 0–10 之间的数值";
    fieldError.hidden = false;
    $("setRate").focus();
    return;
  }
  const checkTaskSize = (value, input) => {
    if (Number.isFinite(value) && value >= 1 && value <= 1000000) return true;
    fieldError.textContent = "任务体积上限须为 1-1000000 MB 的整数";
    fieldError.hidden = false;
    input.focus();
    return false;
  };
  if (!checkTaskSize(taskNovel, $("setTaskNovel")) || !checkTaskSize(taskManga, $("setTaskManga"))) return;
  fieldError.hidden = true;
  const payload = {
    output_dir: $("setOutput").value.trim(),
    compress: segValue($("setCompressSeg"), "compress"),
    ocr: segValue($("setOcrSeg"), "ocr"),
    task_novel_mb: taskNovel,
    task_manga_mb: taskManga,
    concurrency,
    rate,
    auto_check_updates: $("setAutoCheck").checked,
    obey_robots: $("setRobots").checked,
    browser_native: $("setBrowserNative").checked,
    cdp_endpoint: $("setCdpEndpoint").value.trim(),
  };
  const btn = $("saveSettingsBtn");
  btn.disabled = true;
  $("settingsMeta").textContent = "保存中…";
  api("/api/settings", { method: "PUT", body: payload }).then((settings) => {
    state.settings = settings;
    syncSplitSizeOption();
    $("settingsMeta").textContent = "已保存";
  }).catch((err) => {
    $("settingsMeta").textContent = `保存失败：${err.message}`;
  }).finally(() => { btn.disabled = false; });
}

/* OCR 模块按需下载（模型文件，约 16 MB）；引擎本身随应用分发，不能页面下载 */
$("ocrDownloadBtn").addEventListener("click", () => {
  const btn = $("ocrDownloadBtn"), status = $("ocrModuleStatus");
  btn.disabled = true;
  $("ocrMeter").hidden = false;
  $("ocrMeterFill").style.width = "5%";
  streamFrames("/api/ocr/models/download", (frame) => {
    if (typeof frame.done === "number") {
      $("ocrMeterFill").style.width = `${Math.round((frame.done / (frame.total || 1)) * 100)}%`;
      status.textContent = `正在下载 ${frame.done}/${frame.total} · ${frame.file}`;
    } else if (frame.total) {
      status.textContent = `准备下载 ${frame.total} 个文件…`;
    }
  }).then(() => api("/api/capabilities")).then((caps) => {
    $("ocrMeter").hidden = true;
    renderDoctor(caps);
  }).catch((err) => {
    $("ocrMeter").hidden = true;
    status.textContent = err.hint ? `${err.message} ${err.hint}` : `下载失败：${err.message}`;
  }).finally(() => { btn.disabled = false; });
});

/* 原生壳里通过 NSOpenPanel 选目录（chooseDirectory 桥）；纯浏览器运行没有该桥，按钮保持隐藏 */
const dirBridge = window.webkit && window.webkit.messageHandlers
  && window.webkit.messageHandlers.chooseDirectory;
if (dirBridge) {
  $("browseOutputBtn").hidden = false;
  $("browseOutputBtn").addEventListener("click", async () => {
    try {
      const path = await dirBridge.postMessage({});
      if (path && typeof path === "string") {
        $("setOutput").value = path;
        $("settingsMeta").textContent = "";
      }
    } catch (_) { /* 面板不可用时仍走手动输入 */ }
  });
}

/* 重新编辑任一设置字段后，撤去旧的“已保存/保存失败”状态 */
["setOutput", "setConcurrency", "setRate", "setCdpEndpoint", "setTaskNovel", "setTaskManga"].forEach((id) => {
  $(id).addEventListener("input", () => { $("settingsMeta").textContent = ""; });
});
["setRobots", "setAutoCheck", "setBrowserNative"].forEach((id) => {
  $(id).addEventListener("change", () => { $("settingsMeta").textContent = ""; });
});

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
$("libRetryBtn").addEventListener("click", loadBooks);
$("emptyClearBtn").addEventListener("click", () => {
  $("searchInput").value = "";
  state.filterGroup = null;
  loadBooks();
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

/* ------------------------------------------------------------ 推荐站点
   清单由服务端 /api/sites 提供(本项目实测可抓,见 项目进度.md)。
   每张卡:标题 + 网址 + 资源特点;「打开站点」在默认浏览器打开主页找书,
   「复制地址」供回采集台粘贴。 */
(async function recSites() {
  const cols = $("recCols");
  if (!cols) return;
  const data = await api("/api/sites").catch(() => null);
  if (!data) return;
  const buildCard = (site) => {
    const url = `https://${site.host}/`;
    const card = document.createElement("div");
    card.className = "rec-card";
    const head = document.createElement("div");
    head.className = "rec-head";
    const name = document.createElement("b");
    name.textContent = site.name;
    const kind = document.createElement("span");
    kind.className = "rec-kind";
    kind.textContent = site.kind;
    head.append(name, kind);
    const host = document.createElement("div");
    host.className = "rec-host";
    host.textContent = site.host;
    card.append(head, host);
    if (site.note) {
      const note = document.createElement("div");
      note.className = "rec-note";
      note.textContent = site.note;
      card.append(note);
    }
    const actions = document.createElement("div");
    actions.className = "rec-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "btn primary";
    open.textContent = "打开站点";
    open.addEventListener("click", () => {
      api("/api/sites/open", { method: "POST", body: { host: site.host } })
        .then(() => {
          open.textContent = "已在浏览器打开";
          setTimeout(() => { open.textContent = "打开站点"; }, 1500);
        })
        .catch(() => { open.textContent = "打开失败"; setTimeout(() => { open.textContent = "打开站点"; }, 1500); });
    });
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "btn ghost";
    copy.textContent = "复制地址";
    copy.addEventListener("click", () => {
      const done = () => {
        copy.textContent = "已复制";
        setTimeout(() => { copy.textContent = "复制地址"; }, 1200);
      };
      const fallback = () => {
        const ta = document.createElement("textarea");
        ta.value = url;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { if (document.execCommand("copy")) done(); } catch (_) {}
        ta.remove();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done).catch(fallback);
      } else {
        fallback();
      }
    });
    actions.append(open, copy);
    card.append(actions);
    return card;
  };
  /* 按类型分列（小说/漫画在前，未知类型依出现顺序补列）；站点增多时列内滑动 */
  const kinds = ["小说", "漫画"];
  data.sites.forEach((site) => { if (!kinds.includes(site.kind)) kinds.push(site.kind); });
  kinds.forEach((kind) => {
    const sites = data.sites.filter((site) => site.kind === kind);
    if (!sites.length) return;
    const section = document.createElement("section");
    section.className = "rec-col";
    const title = document.createElement("h2");
    title.className = "rec-col-title";
    title.textContent = kind;
    const list = document.createElement("div");
    list.className = "rec-col-list";
    sites.forEach((site) => list.appendChild(buildCard(site)));
    section.append(title, list);
    cols.appendChild(section);
  });
})();
