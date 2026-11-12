/* 设置：改动即保存（PUT 只发改动的字段，服务端合并），字段就地校验与报错。
   外观（明暗 × 强调色）立即生效并写回。依赖 app.js / follows.js（streamFrames）。 */
"use strict";

const Q_DESC = { archive: "保留原图，体积最大", balanced: "体积与清晰度兼顾，适合日常阅读", small: "体积最小，适合手机" };
const FIELD_RULES = {
  setOutput: { err: "errOutput", read: (v) => {
    const t = v.trim();
    if (!t) throw new Error("保存位置不能为空");
    if (!t.startsWith("/") && !t.startsWith("~")) throw new Error("请填写完整路径，例如 ~/Documents/Quire");
    return t;
  } },
  setTaskNovel: { err: "errTask", read: (v) => intIn(v, 1, 1000000, "体积上限须为 1–1000000 的整数") },
  setTaskManga: { err: "errTask", read: (v) => intIn(v, 1, 1000000, "体积上限须为 1–1000000 的整数") },
  setConcurrency: { err: "errConcurrency", read: (v) => intIn(v, 1, 32, "须为 1–32 的整数") },
  setRate: { err: "errRate", read: (v) => {
    const n = Number(v);
    if (!v.trim() || !Number.isFinite(n) || n <= 0 || n > 10) throw new Error("须大于 0 且不超过 10");
    return n;
  } },
  setCdpEndpoint: { err: "errCdp", read: (v) => {
    const t = v.trim();
    if (t && !/^(https?|wss?):\/\/[^\s/]+/.test(t)) throw new Error("示例：http://127.0.0.1:9222");
    return t;
  } },
};
function intIn(v, lo, hi, message) {
  const n = Number(v);
  if (!/^\s*\d+\s*$/.test(v) || n < lo || n > hi) throw new Error(message);
  return n;
}

let savedTimer = null;
function showSaved(text, on = true) {
  $("savedTxt").textContent = text;
  $("saved").classList.toggle("on", on);
  clearTimeout(savedTimer);
  if (on && text === "已保存") savedTimer = setTimeout(() => $("saved").classList.remove("on"), 1800);
}

function setFieldError(id, message) {
  const rule = FIELD_RULES[id];
  $(id).classList.toggle("invalid", Boolean(message));
  if (message) $(id).setAttribute("aria-invalid", "true"); else $(id).removeAttribute("aria-invalid");
  const err = $(rule.err);
  err.hidden = !message;
  err.textContent = message || "";
  if (message) $(id).setAttribute("aria-describedby", rule.err); else $(id).removeAttribute("aria-describedby");
}

/* 保存一组字段；失败时把服务端错误挂到触发它的输入框上（没有则 toast） */
function saveSettings(patch, sourceId) {
  showSaved("保存中…");
  return api("/api/settings", { method: "PUT", body: patch }).then((settings) => {
    state.settings = settings;
    if (sourceId && FIELD_RULES[sourceId]) setFieldError(sourceId, "");
    showSaved("已保存");
    return settings;
  }).catch((err) => {
    showSaved("", false);
    if (sourceId && FIELD_RULES[sourceId]) setFieldError(sourceId, errText(err));
    else toast(`没有保存：${errText(err)}`);
    fillSettings(); // 回到服务端的真实值
    throw err;
  });
}

function pressSeg(group, value) {
  group.querySelectorAll("[data-v]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === value)));
}

function fillSettings() {
  const s = state.settings;
  if (!s) return;
  document.querySelectorAll("#settings [data-key]").forEach((el) => {
    if (el.type === "checkbox") el.checked = Boolean(s[el.dataset.key]);
    else if (document.activeElement !== el && !el.classList.contains("invalid")) el.value = s[el.dataset.key] ?? "";
  });
  pressSeg($("qSeg"), s.compress);
  $("qDesc").textContent = Q_DESC[s.compress] || "";
  pressSeg($("ocrSeg"), s.ocr);
  $("cdpRow").hidden = !s.browser_native && !s.cdp_endpoint;
  applyTheme(s.theme, s.accent);
  fillEpubStyleRow(s);
}

/* 活板版式合并后，服务端设置里会出现 epub_style；出现才显示这一行 */
function fillEpubStyleRow(s) {
  let row = $("epubStyleRow");
  if (!("epub_style" in s)) { if (row) row.remove(); return; }
  if (!row) {
    $("qSeg").closest(".srow").insertAdjacentHTML("afterend", html`<div class="srow" id="epubStyleRow"><div class="txt"><span class="lab">小说默认版式</span><small>每次采集时仍可单独选择</small></div>
      <div class="seg" id="styleSeg" role="group" aria-label="小说默认版式">${STYLES.map(([v, label]) => html`<button aria-pressed="false" data-v="${v}">${label.split(" · ")[0]}</button>`)}</div></div>`);
    row = $("epubStyleRow");
    $("styleSeg").addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (b) { pressSeg($("styleSeg"), b.dataset.v); saveSettings({ epub_style: b.dataset.v }).catch(() => {}); }
    });
  }
  pressSeg($("styleSeg"), s.epub_style);
}

/* ------------------------------------------------------------ OCR 与关于 */

function paintOcr(caps) {
  const ocr = (caps && caps.ocr) || {};
  const status = $("ocrModuleStatus");
  $("ocrDownloadBtn").hidden = true;
  if (ocr.tesseract) status.textContent = "tesseract 已就绪，图片正文可直接识别";
  else if (!ocr.onnxruntime) status.textContent = "没有 OCR 引擎：文字正文照常采集，图片正文无法识别";
  else if (ocr.models_ready) status.textContent = "内置 OCR 已就绪";
  else { status.textContent = "内置引擎就绪，模型还没下载"; $("ocrDownloadBtn").hidden = false; }
}

function paintAbout(caps) {
  if (!caps) return;
  const rows = [
    ["版本", `卷帙 Quire ${caps.version}`],
    ["Chrome", caps.chrome ? "已找到" : "未找到 · 小说转 PDF 和动态页面需要", caps.chrome],
    ["数据目录", caps.data_dir, caps.data_dir],
    ["实际输出目录", caps.output_dir, caps.output_dir],
  ];
  $("aboutGroup").innerHTML = html`${rows.map(([k, v, title]) => html`<div class="srow"><div class="txt"><span class="lab">${k}</span></div>
    <span class="path" title="${title || ""}">${v}</span></div>`)}`;
}

function loadCaps() {
  return api("/api/capabilities").then((caps) => { state.caps = caps; paintOcr(caps); paintAbout(caps); return caps; });
}

$("ocrDownloadBtn").addEventListener("click", () => {
  const btn = $("ocrDownloadBtn"), status = $("ocrModuleStatus");
  btn.disabled = true;
  $("ocrMeter").hidden = false;
  $("ocrMeterFill").style.width = "4%";
  streamFrames("/api/ocr/models/download", (frame) => {
    if (typeof frame.done === "number") {
      $("ocrMeterFill").style.width = `${Math.round((frame.done / (frame.total || 1)) * 100)}%`;
      status.textContent = `正在下载 ${frame.done}/${frame.total}`;
    } else if (frame.total) status.textContent = `准备下载 ${frame.total} 个文件…`;
  }).then(loadCaps).then(() => toast("OCR 模型已就绪")).catch((err) => {
    status.textContent = `下载失败：${errText(err)}`;
  }).finally(() => { btn.disabled = false; $("ocrMeter").hidden = true; });
});

/* ------------------------------------------------------------ 绑定 */

$("appear").addEventListener("click", (e) => {
  const b = e.target.closest("[data-mode-set]");
  if (!b) return;
  applyTheme(b.dataset.modeSet, document.documentElement.dataset.accent);
  saveSettings({ theme: b.dataset.modeSet }).catch(() => {});
});
$("accents").addEventListener("click", (e) => {
  const b = e.target.closest("[data-accent-set]");
  if (!b) return;
  applyTheme(document.documentElement.dataset.theme, b.dataset.accentSet);
  saveSettings({ accent: b.dataset.accentSet }).catch(() => {});
});
$("qSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-v]");
  if (!b) return;
  pressSeg($("qSeg"), b.dataset.v);
  $("qDesc").textContent = Q_DESC[b.dataset.v];
  saveSettings({ compress: b.dataset.v }).catch(() => {});
});
$("ocrSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-v]");
  if (!b) return;
  pressSeg($("ocrSeg"), b.dataset.v);
  saveSettings({ ocr: b.dataset.v }).catch(() => {});
});

Object.keys(FIELD_RULES).forEach((id) => {
  const el = $(id);
  el.addEventListener("input", () => { if (el.classList.contains("invalid")) setFieldError(id, ""); });
  el.addEventListener("change", () => {
    let value;
    try { value = FIELD_RULES[id].read(el.value); } catch (err) { setFieldError(id, err.message); return; }
    setFieldError(id, "");
    if (state.settings && state.settings[el.dataset.key] === value) return;
    saveSettings({ [el.dataset.key]: value }, id).catch(() => {});
  });
  el.addEventListener("keydown", (e) => { if (e.key === "Enter") el.blur(); });
});
document.querySelectorAll("#settings input.switch[data-key]").forEach((el) => el.addEventListener("change", () => {
  if (el.id === "setBrowserNative") $("cdpRow").hidden = !el.checked && !$("setCdpEndpoint").value.trim();
  saveSettings({ [el.dataset.key]: el.checked }).catch(() => {});
}));

/* 原生壳里通过 NSOpenPanel 选目录；纯浏览器没有该桥，按钮保持隐藏 */
const dirBridge = window.webkit?.messageHandlers?.chooseDirectory;
if (dirBridge) {
  $("browseOutputBtn").hidden = false;
  $("browseOutputBtn").addEventListener("click", async () => {
    try {
      const path = await dirBridge.postMessage({});
      if (path && typeof path === "string") {
        $("setOutput").value = path;
        $("setOutput").dispatchEvent(new Event("change"));
      }
    } catch (_) { /* 面板不可用时仍可手动输入 */ }
  });
}

VIEW_HOOKS.settings = () => {
  fillSettings();
  api("/api/settings").then((s) => { state.settings = s; fillSettings(); }).catch((err) => toast(errText(err)));
  loadCaps().catch(() => { $("ocrModuleStatus").textContent = "无法读取运行环境"; });
};
