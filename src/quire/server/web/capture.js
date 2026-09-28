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

const PROBE_STAGES = {
  fetch: "正在请求目录页面……",
  render: "页面需要浏览器渲染，正在等待……",
  parse: "正在识别章节……",
  estimate: "正在抽样估算体积……",
};
const PROBE_STAGE_PROGRESS = { fetch: 30, render: 55, parse: 75, estimate: 90 };

function probeProgress(stage) {
  $("probeMeterFill").style.width = `${PROBE_STAGE_PROGRESS[stage] || 30}%`;
}

/* probe 走 NDJSON 流（fetch + ReadableStream，EventSource 不支持 POST）：
   每个阶段一帧 {"stage": …}，收尾是 {"result": …} 或 {"error": …}。 */
function probeUrl(url) {
  clearTimeout(probeTimer);
  const request = Symbol(); state.probeRequest = request;
  $("startBtn").disabled = true;
  state.probe = null;
  $("probeHint").hidden = false;
  $("seriesField").hidden = true;
  $("probeHint").classList.add("is-loading");
  $("probeHint").textContent = PROBE_STAGES.fetch;
  $("probeMeter").hidden = false;
  $("probeMeterFill").style.width = "8%";
  fetch("/api/probe", {
    method: "POST",
    headers: { "X-Quire-Token": TOKEN, "Content-Type": "application/json" },
    body: JSON.stringify({ url, kind: state.kind, capture_mode: state.captureMode, split_by: $("splitMode").value }),
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
        if (frame.stage) {
          if (state.probeRequest === request) {
            $("probeHint").textContent = PROBE_STAGES[frame.stage] || frame.stage;
            probeProgress(frame.stage);
          }
        } else if (frame.error) {
          failure = frame;
        } else if (frame.result) {
          result = frame.result;
        }
      }
    }
    if (failure) {
      const err = new Error(failure.error);
      err.hint = failure.hint;
      throw err;
    }
    if (!result) throw new Error("识别没有返回结果，请再试一次");
    return result;
  }).then((result) => {
    if (state.probeRequest !== request) return;
    renderProbeResult(result);
  }).catch((err) => {
    if (state.probeRequest !== request) return;
    $("probeHint").classList.remove("is-loading");
    $("probeMeter").hidden = true;
    $("probeHint").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
    $("wsStatus").hidden = true;
    $("startBtn").disabled = true;
  });
}

/* 识别结果渲染:probe 成功与标签页状态恢复共用同一条路径 */
function renderProbeResult(result) {
  $("probeHint").classList.remove("is-loading");
  $("probeHint").hidden = true;
  $("probeMeter").hidden = true;
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
}

function syncSplitSizeOption() {
  const opt = $("splitSizeOpt");
  if (!opt) return;
  const settings = state.settings || {};
  const mb = state.kind === "novel" ? settings.task_novel_mb || 100 : settings.task_manga_mb || 500;
  opt.value = `size ${mb}MB`;
  opt.textContent = `每卷不超过 ${mb} MB`;
}

function updateKindFields() {
  syncOcrAvailability();
  syncSplitSizeOption();
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
  $("probeMeter").hidden = true;
  $("probeMeterFill").style.width = "0";
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
  if (!result) return;
  let selected = result.count, failed = false;
  if (result.series) {
    try { const range = rangeState(); if (range.custom) selected = range.numbers.length; } catch (_) { failed = true; }
  }
  const scale = failed || !result.count ? 0 : selected / result.count;
  const parts = [["archive", "原画"], ["balanced", "均衡"], ["small", "压缩"]]
    .map(([key, label]) => [label, result.estimates && result.estimates[key]])
    .filter(([, bytes]) => bytes && scale)
    .map(([label, bytes]) => `${label}约 ${humanSize(bytes * scale * .7)}–${humanSize(bytes * scale * 1.3)}`);
  $("sizeEstimate").textContent = parts.length
    ? parts.join(" · ")
    : result.estimate_bytes
      ? `原图约 ${humanSize(result.estimate_bytes)}`
      : "暂无法估算体积";
}

function syncOcrAvailability() {
  const ocr = state.caps && state.caps.ocr;
  const mode = segValue($("ocrSeg"), "ocr");
  const message = mode === "never" ? "已关闭图片文字识别，仅提取网页文字。"
    : !ocr ? "正在检查 OCR 可用性…"
    : ocr.tesseract || (ocr.onnxruntime && ocr.models_ready)
      ? "OCR 已就绪；自动模式仅在正文为图片时启用。"
      : ocr.onnxruntime ? "自动是识别策略：模型尚未下载，遇到图片正文时按需下载。"
      : "自动是识别策略：OCR 引擎未安装；普通文字可采集，图片正文暂不能识别。";
  $("ocrAvailability").textContent = message;
  $("ocrAvailability").hidden = state.kind !== "novel";
}
