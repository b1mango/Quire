/* 采集：一个输入框 → 自动识别（类型、目录/单章、续抓起点）→ 结果卡确认 → 开始。
   识别走 NDJSON 流（阶段帧 + result/error 收尾）。依赖 app.js / chapters.js / follows.js。 */
"use strict";

const FORMATS = { manga: [["pdf", "PDF"], ["cbz", "CBZ"], ["zip", "原图 ZIP"]],
                  novel: [["epub", "EPUB"], ["txt", "TXT"], ["pdf", "PDF"]] };
const QUALITY = [["archive", "原画", "保留原图，体积最大"], ["balanced", "均衡", "体积与清晰度兼顾，适合日常阅读"],
                 ["small", "压缩", "体积最小，适合手机"]];
/* 与 assemble/epub_style.py 对应：正文字族、行高、标题字族（仿古只换标题为楷体并加字距） */
const STYLES = [["songti", "宋体 · 舒朗", "Songti SC", 1.8, ""], ["heiti", "黑体 · 紧凑", "PingFang SC", 1.6, ""],
                ["classic", "仿古", "Songti SC", 1.8, "font-family:'Kaiti SC';letter-spacing:.12em"]];
const SPLITS = [["volume", "按站点卷结构"], ["chapters 20", "每 20 章一卷"], ["chapters 1", "每章一卷"],
                ["size", "按体积上限分卷"], ["none", "合为一卷"]];
const STAGE_STEP = { fetch: 0, render: 0, parse: 1, estimate: 2 };

/* 当前识别的选项：kind=null / mode="auto" 表示交给服务端判断 */
const capture = { request: null, kind: null, mode: "auto", split: "volume", formats: null,
                  compress: null, ocr: null, style: null };

/* 用户预选的类型（自动 / 小说 / 漫画），跨次记住 */
function kindPref() { const k = localStorage.getItem("quire-kind"); return k === "novel" || k === "manga" ? k : null; }
function paintKindPick() {
  const k = kindPref() || "auto";
  $("kindPick").querySelectorAll("[data-k]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.k === k));
    b.setAttribute("aria-checked", String(b.dataset.k === k));
  });
}

function host(url) { try { return new URL(url).host.replace(/^www\./, ""); } catch (_) { return url; } }
function unitOf(probe) { return probe.kind === "manga" && !(probe.chapters || []).length ? "页" : "章"; }

/* ------------------------------------------------------------ 识别 */

function captureUrl(url, opts = {}) {
  $("urlIn").value = url;
  $("probeBtn").disabled = !url.trim();
  Object.assign(capture, { kind: kindPref(), mode: "auto", formats: null }, opts);
  runProbe();
}

function setSteps(stage) {
  const steps = $("steps");
  steps.hidden = stage === null;
  const at = stage in STAGE_STEP ? STAGE_STEP[stage] : -1;
  [...steps.children].forEach((li, i) => { li.className = i < at ? "done" : i === at ? "on" : ""; });
  steps.children[0].textContent = stage === "render" ? "浏览器渲染" : "连接站点";
}

/* keep=true：结果卡上切换类型时原地重识别（卡片变淡并提示），不退回空白页 */
function runProbe(keep = false) {
  const url = $("urlIn").value.trim();
  if (!url) return;
  const request = Symbol();
  capture.request = request;
  state.probe = null;
  $("capHome").hidden = true;
  const card = keep && $("result").querySelector(".result");
  if (card) {
    card.classList.add("reprobing");
    card.querySelector(".rs-head > div")?.insertAdjacentHTML("beforeend",
      html`<div class="reprobe-note" role="status">正在按${capture.kind === "manga" ? "漫画" : "小说"}重新识别…</div>`);
  } else $("result").textContent = "";
  $("capErr").textContent = "";
  $("dock").hidden = true;
  $("resetCap").hidden = false;
  if (!card) $("capture").classList.remove("has-result");
  $("probeBtn").disabled = true;
  $("probeBtn").textContent = "识别中…";
  setSteps("fetch");
  const split = capture.split === "size" ? `size ${sizeCapMb()}MB` : capture.split;
  streamFrames("/api/probe", (frame) => {
    if (frame.stage && capture.request === request) setSteps(frame.stage);
  }, { url, kind: capture.kind, capture_mode: capture.mode, split_by: split }).then((result) => {
    if (capture.request !== request) return;
    if (!result) throw new Error("识别没有返回结果，请再试一次");
    showResult(result);
  }).catch((err) => {
    if (capture.request !== request) return;
    $("result").textContent = "";
    $("capture").classList.remove("has-result");
    showProbeError(err);
  }).finally(() => {
    if (capture.request !== request) return;
    setSteps(null);
    $("probeBtn").disabled = !$("urlIn").value.trim();
    $("probeBtn").textContent = "识别";
  });
}

function showProbeError(err) {
  const noCatalogue = /章节列表|目录/.test(err.message) && capture.mode !== "single";
  const other = capture.kind === "manga" ? "novel" : "manga";
  const failure = readableError(err.message, "识别没有成功");
  $("capErr").innerHTML = html`<div class="err" role="alert"><svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 5v3.5M8 11h0"/></svg>
    <div><b>${failure.title}</b><p>${err.hint || "检查链接是否能在浏览器里打开，或换成作品的目录页再试。"}</p>
    ${failure.detail ? html`<p style="font-size:var(--fs-12);color:var(--text-3)">原始信息：${failure.detail}</p>` : ""}
    ${noCatalogue ? html`<button class="btn" data-retry="single">只抓这一章</button> ` : ""}
    <button class="btn" data-retry="kind">按${other === "manga" ? "漫画" : "小说"}再识别</button>
    <button class="btn quiet" data-retry="again">重试</button>
    ${/403|登录|拒绝/.test(err.message + (err.hint || "")) ? html` <button class="btn quiet" data-go="settings">打开网络设置</button>` : ""}</div></div>`;
}
$("capErr").addEventListener("click", (e) => {
  const b = e.target.closest("[data-retry]");
  if (!b) return;
  if (b.dataset.retry === "single") capture.mode = "single";
  if (b.dataset.retry === "kind") capture.kind = capture.kind === "manga" ? "novel" : "manga";
  runProbe();
});

/* ------------------------------------------------------------ 结果卡 */

function defaultFormats(kind) { return new Set([FORMATS[kind][0][0]]); }
function sizeCapMb() {
  const s = state.settings || {};
  return (state.probe || {}).kind === "manga" || capture.kind === "manga" ? s.task_manga_mb || 500 : s.task_novel_mb || 100;
}

function showResult(result) {
  state.probe = result;
  const kind = result.kind;
  if (!capture.formats || ![...capture.formats].every((f) => FORMATS[kind].some(([v]) => v === f)))
    capture.formats = defaultFormats(kind);
  capture.compress = capture.compress || (state.settings && state.settings.compress) || "balanced";
  capture.ocr = capture.ocr || (state.settings && state.settings.ocr) || "auto";
  capture.style = capture.style || (state.settings && state.settings.epub_style) || "songti";
  const owned = (result.library && result.library.chapters) || 0;
  const total = (result.chapters || []).length;
  const noChrome = state.caps && !state.caps.chrome;
  const libNote = !result.library ? "" : owned >= total
    ? html`<div class="lib-note"><svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="m5.5 8.2 1.8 1.8 3.3-3.6"/></svg><div>书库里已有全部 ${owned} 章，目录没有新章节。</div></div>`
    : html`<div class="lib-note"><svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="m5.5 8.2 1.8 1.8 3.3-3.6"/></svg><div>书库里已有第 1–${owned} 章，默认从第 ${owned + 1} 章续抓。</div></div>`;
  const fmtChips = FORMATS[kind].map(([v, label]) => {
    const needChrome = kind === "novel" && v === "pdf";
    return html`<button type="button" class="chip" data-f="${v}" aria-pressed="${String(capture.formats.has(v))}"
      ${needChrome && noChrome ? raw("disabled") : ""}>${label}${needChrome ? html` <small>${noChrome ? "未找到 Chrome" : "需 Chrome"}</small>` : ""}</button>`;
  });
  const styleCards = STYLES.map(([v, label, font, lh, head]) => html`<button type="button" class="style-card" data-sty="${v}" aria-pressed="${String(capture.style === v)}">
    <div class="pv" style="font-family:'${font}';line-height:${lh}"><b style="text-align:center;${head}">第一章</b><span style="text-indent:2em">天色将明未明，城门外的驿道上已有了车马声。</span></div><span>${label}</span></button>`);
  const quality = QUALITY.map(([v, label]) => html`<button type="button" data-q="${v}" aria-pressed="${String(capture.compress === v)}">${label}</button>`);
  const series = kind === "manga" && result.series;
  $("capture").classList.add("has-result");
  $("result").innerHTML = html`<article class="result" aria-labelledby="rsTitle">
    <div class="rs-head">${cover({ title: result.title, author: host(result.url) })}<div>
      <h2 id="rsTitle">${result.title}</h2>
      <div class="rs-meta"><span class="k">${host(result.url)}</span><span class="num">${total || result.count} ${unitOf(result)}</span>
        ${result.render ? html`<span class="k">动态页面</span>` : ""}
        <button type="button" class="kind-pill" id="kindPill" aria-label="内容类型：${kind === "manga" ? "漫画" : "小说"}，点击改为${kind === "manga" ? "小说" : "漫画"}"
          title="识别错了？点击改为${kind === "manga" ? "小说" : "漫画"}">${kind === "manga" ? "漫画" : "小说"} <svg class="i" viewBox="0 0 16 16" style="width:12px;height:12px"><path d="m5 6.5 3 3 3-3"/></svg></button></div>
      ${libNote}
    </div></div>
    <div class="rs-rows">
      ${rangeRowHTML()}
      <div class="row"><span class="lab" id="fmtLab">格式</span><div class="ctl chips" role="group" aria-labelledby="fmtLab" id="fmtChips">${fmtChips}</div></div>
      ${kind === "novel" ? html`<div class="row" id="styleRow"><span class="lab" id="styLab">版式</span><div class="ctl style-cards" role="group" aria-labelledby="styLab" id="styCards">${styleCards}</div></div>` : ""}
      ${kind === "manga" ? html`<div class="row"><span class="lab" id="qLab">画质</span><div class="ctl"><div class="seg" role="group" aria-labelledby="qLab" id="qPick">${quality}</div><span class="hint" id="qHint"></span></div></div>` : ""}
    </div>
    <details class="more"><summary><svg class="i" viewBox="0 0 16 16" style="width:12px;height:12px"><path d="m6 4 4 4-4 4"/></svg>更多选项</summary>
      ${kind === "manga" && total ? html`<div class="row"><label for="splitSel">分卷</label><div class="ctl"><select class="field-input" id="splitSel">
        ${SPLITS.map(([v, label]) => html`<option value="${v}" ${capture.split === v ? raw("selected") : ""}>${v === "size" ? `每卷不超过 ${sizeCapMb()} MB` : label}</option>`)}</select>
        ${series ? html`<div class="vol-list" id="volList" aria-label="选择需要的卷">${(result.volumes || []).map((v) => html`<label><input type="checkbox" value="${v.index}" checked> ${v.title} · ${v.chapters} 章</label>`)}</div>` : ""}
        <span class="hint" id="splitHint"></span></div></div>` : ""}
      ${kind === "novel" ? html`<div class="row"><span class="lab" id="ocrLab">图片正文</span><div class="ctl"><div class="seg" role="group" aria-labelledby="ocrLab" id="ocrPick">
        ${[["auto", "自动识别"], ["always", "总是 OCR"], ["never", "关闭"]].map(([v, l]) => html`<button type="button" data-o="${v}" aria-pressed="${String(capture.ocr === v)}">${l}</button>`)}</div>
        <span class="hint" id="ocrHint">${ocrHint()}</span></div></div>` : ""}
    </details>
  </article>`;
  bindResult(kind);
  $("dock").hidden = false;
}

function ocrHint() {
  const ocr = state.caps && state.caps.ocr;
  if (capture.ocr === "never") return "只提取网页文字";
  if (!ocr) return "";
  if (ocr.tesseract || (ocr.onnxruntime && ocr.models_ready)) return "OCR 已就绪，正文是图片时才启用";
  return ocr.onnxruntime ? "OCR 模型未下载，可在 设置 · 文字识别 下载" : "没有 OCR 引擎：文字正文照常采集，图片正文无法识别";
}

function pressOne(group, attr, value) {
  group.querySelectorAll(`[${attr}]`).forEach((b) => b.setAttribute("aria-pressed", String(b.getAttribute(attr) === value)));
}

function bindResult(kind) {
  bindRange(kind, updateDock);
  $("kindPill").addEventListener("click", () => {
    capture.kind = kind === "manga" ? "novel" : "manga";
    capture.formats = null;
    runProbe(true);
  });
  $("fmtChips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip || chip.disabled) return;
    const v = chip.dataset.f;
    if (capture.formats.has(v)) {
      if (capture.formats.size === 1) { toast("至少保留一种格式"); return; }
      capture.formats.delete(v);
    } else capture.formats.add(v);
    chip.setAttribute("aria-pressed", String(capture.formats.has(v)));
    updateDock();
  });
  $("styCards")?.addEventListener("click", (e) => {
    const card = e.target.closest(".style-card");
    if (!card) return;
    capture.style = card.dataset.sty;
    pressOne($("styCards"), "data-sty", capture.style);
    updateDock();
  });
  $("qPick")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (!b) return;
    capture.compress = b.dataset.q;
    pressOne($("qPick"), "data-q", capture.compress);
    updateDock();
  });
  $("ocrPick")?.addEventListener("click", (e) => {
    const b = e.target.closest("[data-o]");
    if (!b) return;
    capture.ocr = b.dataset.o;
    pressOne($("ocrPick"), "data-o", capture.ocr);
    $("ocrHint").textContent = ocrHint();
  });
  $("splitSel")?.addEventListener("change", (e) => {
    capture.split = e.target.value;
    runProbe(); // 分卷方式决定卷结构，需要重新识别
  });
  $("volList")?.addEventListener("change", updateDock);
  updateDock();
}

/* ------------------------------------------------------------ 悬浮条摘要与估算 */

function selectedVolumes() {
  return [...document.querySelectorAll("#volList input:checked")].map((x) => Number(x.value));
}

function estimateBytes(count) {
  const p = state.probe;
  const full = (p.chapters || []).length || p.count || 1;
  const scale = count / full;
  if (p.kind === "manga" && p.estimates && p.estimates[capture.compress]) return p.estimates[capture.compress] * scale;
  return p.estimate_bytes ? p.estimate_bytes * scale : 0;
}

function updateDock() {
  const p = state.probe;
  if (!p) return;
  const r = rangeSummary();
  const btn = $("startBtn");
  if (r.error) {
    $("dockSum").innerHTML = html`<span style="color:var(--danger)">${r.error}</span>`;
    btn.disabled = true;
    return;
  }
  const volumes = p.kind === "manga" && p.series && $("volList") ? selectedVolumes() : null;
  const custom = Boolean(chapterRangeSpec().chapter_ranges);
  if (volumes && !custom && capture.split !== "size" && !volumes.length) {
    $("dockSum").textContent = "至少选择一卷";
    btn.disabled = true;
    return;
  }
  const count = r.count || p.count;
  const bytes = estimateBytes(count);
  const fmts = FORMATS[p.kind].filter(([v]) => capture.formats.has(v)).map(([, l]) => l).join(" + ");
  const style = p.kind === "novel" && capture.formats.has("epub") ? STYLES.find(([v]) => v === capture.style)[1] : "";
  const quality = p.kind === "manga" ? QUALITY.find(([v]) => v === capture.compress)[1] : "";
  const parts = [r.label && !r.label.startsWith("全部") ? html`<b>${r.label}</b>` : "",
    `${count} ${unitOf(p)}`, fmts, style, quality, bytes ? `约 ${humanSize(bytes)}` : ""].filter(Boolean);
  $("dockSum").innerHTML = html`${parts.map((x, i) => html`${i ? " · " : ""}${x}`)}`;
  $("qHint") && ($("qHint").textContent = QUALITY.find(([v]) => v === capture.compress)[2]);
  if ($("styleRow")) $("styleRow").hidden = !capture.formats.has("epub");
  btn.disabled = false;
}

/* ------------------------------------------------------------ 开始 */

function startJob() {
  const p = state.probe;
  if (!p || $("startBtn").disabled) return;
  const range = chapterRangeSpec();
  const custom = Boolean(range.chapter_ranges);
  const series = Boolean(!custom && p.series && p.kind === "manga");
  const spec = {
    kind: p.kind,
    capture_mode: (p.chapters || []).length ? "catalogue" : "single",
    render: Boolean(p.render),
    url: p.url,
    title: p.title,
    formats: FORMATS[p.kind].map(([v]) => v).filter((v) => capture.formats.has(v)),
    compress: p.kind === "manga" ? capture.compress : (state.settings && state.settings.compress) || "balanced",
    ocr: capture.ocr || "auto",
    epub_style: capture.style,
    series,
    split_by: capture.split === "size" ? `size ${sizeCapMb()}MB` : capture.split,
    volumes: series && $("volList") ? selectedVolumes() : [],
    ...range,
  };
  $("startBtn").disabled = true;
  api("/api/jobs", { method: "POST", body: spec }).then((job) => {
    trackJob(job, true);
    toast(job.status === "pending" ? `《${p.title}》已加入队列` : `已开始采集《${p.title}》`, "查看", () => go("tasks"));
    resetCapture();
  }).catch((err) => {
    $("dockSum").innerHTML = html`<span style="color:var(--danger)">${errText(err)}</span>`;
    $("startBtn").disabled = false;
  });
}

function resetCapture() {
  capture.request = Symbol();
  Object.assign(capture, { kind: kindPref(), mode: "auto", split: "volume", formats: null });
  state.probe = null;
  $("capture").classList.remove("has-result");
  $("result").textContent = "";
  $("capErr").textContent = "";
  setSteps(null);
  $("dock").hidden = true;
  $("capHome").hidden = false;
  $("resetCap").hidden = true;
  $("urlIn").value = "";
  $("probeBtn").disabled = true;
  $("probeBtn").textContent = "识别";
  if (state.view === "capture") $("urlIn").focus();
}

async function pasteUrl() {
  try {
    const bridge = window.webkit?.messageHandlers?.clipboard;
    const text = bridge ? await bridge.postMessage({ action: "readText" }) : await navigator.clipboard.readText();
    if (!text || !text.trim()) throw new Error("剪贴板里没有链接");
    captureUrl(text.trim());
  } catch (err) {
    $("urlIn").focus();
    toast(err.message || "无法读取剪贴板，请用 ⌘V 粘贴");
  }
}

/* ------------------------------------------------------------ 推荐站点（空态） */

const SITE_TINT = ["#7A2E2A", "#2E4A62", "#3A4A4A", "#3D5A4C", "#2B3A55", "#8A5A2B"];
function loadSites() {
  api("/api/sites").then((data) => {
    state.sites = data.sites || [];
    $("sitesBox").hidden = !state.sites.length;
    $("siteGrid").innerHTML = html`${state.sites.map((s, i) => html`<button type="button" class="site" data-site="${s.host}" title="${s.note || ""}">
      <span class="fav" style="background:${SITE_TINT[i % SITE_TINT.length]}">${[...s.name][0]}</span>
      <div><b>${s.name}</b><span>${s.host}</span></div><span class="kind">${s.kind}</span></button>`)}`;
  }).catch(() => {});
}
$("siteGrid").addEventListener("click", (e) => {
  const s = e.target.closest(".site");
  if (!s) return;
  api("/api/sites/open", { method: "POST", body: { host: s.dataset.site } })
    .then(() => toast(`已在浏览器打开 ${s.dataset.site}，找到书后把链接贴回来`))
    .catch((err) => toast(errText(err)));
});

$("urlIn").addEventListener("input", () => { $("probeBtn").disabled = !$("urlIn").value.trim(); });
$("omniForm").addEventListener("submit", (e) => {
  e.preventDefault();
  Object.assign(capture, { kind: kindPref(), mode: "auto", formats: null });
  runProbe();
});
$("kindPick").addEventListener("click", (e) => {
  const b = e.target.closest("[data-k]");
  if (!b) return;
  if (b.dataset.k === "auto") localStorage.removeItem("quire-kind"); else localStorage.setItem("quire-kind", b.dataset.k);
  paintKindPick();
  $("urlIn").focus();
});
paintKindPick();
$("pasteBtn").addEventListener("click", pasteUrl);
$("resetCap").addEventListener("click", resetCapture);
$("startBtn").addEventListener("click", startJob);
VIEW_HOOKS.capture = () => setTimeout(() => { if (!state.probe) $("urlIn").focus(); }, 0);
