"use strict";

function renderFormatChips() {
  const wrap = $("formatChips");
  wrap.textContent = "";
  const previous = new Set(state.formats || []);
  FORMATS[state.kind].forEach(([value, label], index) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.textContent = label;
    chip.dataset.format = value;
    const on = previous.size ? previous.has(value) : index === 0;
    chip.setAttribute("aria-pressed", String(on));
    chip.addEventListener("click", () =>
      chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true"));
    wrap.appendChild(chip);
  });
  updateChromeHint();
}

function chosenFormats() {
  return [...$("formatChips").querySelectorAll('.chip[aria-pressed="true"]')]
    .map((c) => c.dataset.format);
}

function updateChromeHint() {
  const needChrome = state.kind === "novel" && chosenFormats().includes("pdf");
  $("chromeHint").hidden = !(needChrome && state.caps && !state.caps.chrome);
}

function probeUrl(url) {
  clearTimeout(probeTimer);
  const request = Symbol(); state.probeRequest = request;
  $("startBtn").disabled = true;
  state.probe = null;
  $("probeHint").hidden = false;
  $("seriesField").hidden = true;
  $("probeHint").classList.add("is-loading");
  $("probeHint").textContent = "正在识别链接，动态页面可能需要稍等……";
  api("/api/probe", { method: "POST", body: { url, kind: state.kind, capture_mode: state.captureMode, split_by: $("splitMode").value } }).then((result) => {
    if (state.probeRequest !== request) return;
    $("probeHint").classList.remove("is-loading");
    $("probeHint").hidden = true;
    state.probe = result;
    renderVolumes(result);
    renderRange(result);
    syncSeriesAvailability();
    state.kind = result.kind;
    setSeg($("kindSeg"), "kind", result.kind);
    renderFormatChips();
    const status = $("wsStatus");
    status.hidden = false;
    status.dataset.kind = result.kind;
    status.textContent =
      `${result.kind === "manga" ? "漫画" : "小说"} · ${result.title} · 约 ${result.count} ` +
      (result.kind === "manga" && !result.series ? "页" : "章") + (result.render ? " · 动态页面已就绪" : "");
    updateSizeEstimate();
    $("formatField").hidden = false;
    updateKindFields();
    $("startBtn").disabled = false;
    $("startMeta").textContent = "";
  }).catch((err) => {
    if (state.probeRequest !== request) return;
    $("probeHint").classList.remove("is-loading");
    $("probeHint").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
    $("wsStatus").hidden = true;
    $("startBtn").disabled = true;
  });
}

function updateKindFields() {
  $("captureHint").textContent = state.captureMode === "single"
    ? "仅抓取当前章节，保留本章分页，不跟随其他章节。"
    : state.kind === "novel" ? "识别小说目录，按阅读顺序合为一本书。" : "识别漫画目录，批量抓取章节，可选择分卷。";
  $("compressField").hidden = state.kind !== "manga";
  $("ocrField").hidden = state.kind !== "novel";
  updateChromeHint();
}

function renderVolumes(result) {
  $("volumeList").textContent = "";
  if (!result.series || $("splitMode").value.startsWith("size")) return;
  for (const volume of result.volumes) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox"; input.value = volume.index; input.checked = true;
    label.append(input, document.createTextNode(`${volume.title} · ${volume.chapters} 章`));
    $("volumeList").append(label);
  }
}

function startJob() {
  if (!state.probe) return;
  const formats = chosenFormats();
  if (!formats.length) { $("startMeta").textContent = "至少选一种格式"; return; }
  const compress = segValue($("compressSeg"), "compress") || "balanced";
  const range = chapterRangeSpec();
  const customRange = Boolean(range.chapter_ranges);
  const spec = {
    kind: state.kind,
    capture_mode: state.captureMode,
    render: Boolean(state.probe.render),
    url: state.probe ? state.probe.url : $("urlInput").value.trim(),
    title: state.probe ? state.probe.title : "book",
    formats,
    compress,
    target_mb: parseInt($("targetInput").value, 10) || 50,
    ocr: segValue($("ocrSeg"), "ocr") || "auto",
    series: Boolean(!customRange && state.probe && state.probe.series && state.kind === "manga"),
    split_by: $("splitMode").value,
    volumes: customRange
      ? []
      : [...$("volumeList").querySelectorAll("input:checked")].map(x => Number(x.value)),
    chapter_first: range.chapter_first,
    chapter_last: range.chapter_last,
    chapter_ranges: range.chapter_ranges,
  };
  if (spec.series && !spec.split_by.startsWith("size") && !spec.volumes.length) {
    $("startMeta").textContent = "至少选择一卷"; return;
  }
  state.lastSpec = spec;
  $("startBtn").disabled = true;
  api("/api/jobs", { method: "POST", body: spec }).then((job) => {
    attachJob(job);
    showView("run");
  }).catch((err) => {
    $("startMeta").textContent = err.message;
    $("startBtn").disabled = false;
  });
}

function resetProbe() {
  state.probeRequest = Symbol(); state.probe = null;
  $("seriesField").hidden = true;
  $("rangeField").hidden = true;
  $("wsStatus").hidden = true;
  $("sizeEstimate").textContent = "识别后可估";
  $("startBtn").disabled = true;
  $("probeHint").hidden = true;
  $("probeHint").classList.remove("is-loading");
}
function recheckUrl() {
  resetProbe(); clearTimeout(probeTimer);
  const url = $("urlInput").value.trim();
  if (url) probeUrl(url);
}
async function pasteUrl() {
  try {
    const bridge = window.webkit?.messageHandlers?.clipboard;
    const text = bridge ? await bridge.postMessage({action: "readText"}) : await navigator.clipboard.readText();
    if (!text || !text.trim()) throw new Error("剪贴板里没有文字链接");
    $("urlInput").value = text.trim();
    recheckUrl();
  } catch (err) {
    $("urlInput").focus();
    $("probeHint").hidden = false;
    $("probeHint").textContent = err.message || "无法读取剪贴板，请使用 ⌘V 粘贴";
  }
}

function updateSizeEstimate() {
  const result = state.probe;
  const preset = segValue($("compressSeg"), "compress");
  let bytes = result && result.estimates && result.estimates[preset];
  let selected = result && result.count;
  if (result && result.series) {
    try { const range = rangeState(); if (range.custom) selected = range.numbers.length; } catch (_) { bytes = null; }
  }
  if (bytes && result.count) bytes *= selected / result.count;
  $("sizeEstimate").textContent = bytes
    ? `图像体积约 ${humanSize(bytes * .7)}–${humanSize(bytes * 1.3)} · 抽样试压估算，不含封装；目标体积会影响结果`
    : result && result.estimate_bytes
      ? `原图总量约 ${humanSize(result.estimate_bytes)} · 当前档位暂无法估算成品`
      : "暂无法估算体积";
}
