/* 卷帙 quire Web UI — 零构建原生 JS。
   核心：令牌与请求、转义模板、导航、主题、Toast/确认框、全局快捷键、启动。
   动态文本一律经 html`` 模板转义后再写入 DOM（站点标题、书名都可能含标记）。 */
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

const state = {
  view: "capture", group: "all", layout: "grid", search: "",
  caps: null, settings: null, sites: [],
  books: [], groups: [], booksLoaded: false, booksError: null,
  sel: new Set(), anchor: null,
  probe: null, jobs: new Map(), job: null,
};

/* ------------------------------------------------------------ 请求 */

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

function errText(err) { return err.hint ? `${err.message} ${err.hint}` : err.message; }
/* 后端个别底层报错还是英文：标题换成中文，原文降为补充说明 */
function readableError(message, fallback = "任务没有完成") {
  const text = String(message || "");
  return /[\u4e00-\u9fff]/.test(text) ? { title: text, detail: "" } : { title: fallback, detail: text };
}

function humanSize(num) {
  let value = num || 0;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024 || unit === "GB") return `${value.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    value /= 1024;
  }
  return `${num} B`;
}

/* ------------------------------------------------------------ 转义模板
   html`<b>${title}</b>` 对插值逐个转义；嵌套 html`` 或数组原样拼接。 */

function raw(text) { const s = String(text); return { raw: s, toString: () => s }; }
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function fmtPart(value) {
  if (value == null || value === false) return "";
  if (Array.isArray(value)) return value.map(fmtPart).join("");
  if (typeof value === "object" && typeof value.raw === "string") return value.raw;
  return esc(value);
}
function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => { out += fmtPart(value) + strings[i + 1]; });
  return raw(out);
}

/* ------------------------------------------------------------ 导航 */

const VIEW_HOOKS = {};
function go(view, group) {
  state.view = view;
  if (group) state.group = group;
  document.querySelectorAll("[data-view]").forEach((v) => { v.hidden = v.dataset.view !== view; });
  paintNav();
  if (VIEW_HOOKS[view]) VIEW_HOOKS[view]();
}
function paintNav() {
  document.querySelectorAll(".nav[data-go]").forEach((n) => {
    const on = n.dataset.go === state.view
      && (state.view !== "library" || (n.dataset.group || "all") === state.group);
    if (on) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
  });
}
document.addEventListener("click", (e) => {
  const n = e.target.closest("[data-go]");
  if (n) go(n.dataset.go, n.dataset.group);
});

/* ------------------------------------------------------------ 主题：明暗 × 强调色 */

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
function applyTheme(theme, accent) {
  const t = ["light", "dark", "system"].includes(theme) ? theme : "system";
  const a = ["cinnabar", "indigo", "ink"].includes(accent) ? accent : "cinnabar";
  document.documentElement.dataset.mode = t === "system" ? (darkQuery.matches ? "dark" : "light") : t;
  document.documentElement.dataset.accent = a;
  document.documentElement.dataset.theme = t;
  localStorage.setItem("quire-look", JSON.stringify([t, a]));
  document.querySelectorAll("[data-mode-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.modeSet === t)));
  document.querySelectorAll("[data-accent-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.accentSet === a)));
}
darkQuery.addEventListener("change", () => {
  if (document.documentElement.dataset.theme === "system")
    applyTheme("system", document.documentElement.dataset.accent);
});
try { applyTheme(...JSON.parse(localStorage.getItem("quire-look") || "[]")); } catch (_) { applyTheme(); }

/* ------------------------------------------------------------ Toast 与确认框 */

let toastTimer = null;
function toast(message, action, onAction) {
  $("toastTxt").textContent = message;
  const btn = $("toastAct");
  btn.hidden = !action;
  if (action) {
    btn.textContent = action;
    btn.onclick = () => { $("toast").classList.remove("on"); onAction(); };
  }
  $("toast").classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("on"), action ? 6000 : 3600);
}

/* 通用确认：body 为 html``；返回 Promise<boolean>。onClick(e, done) 返回 true 表示已处理。 */
function confirmBox({ title, body = "", ok = "确定", cancel = "取消", danger = false, wide = false, onClick = null }) {
  const box = $("confirmIn");
  box.className = wide ? "confirm wide" : "confirm";
  box.innerHTML = html`<h2 id="confirmTitle">${title}</h2>${body}
    <div class="acts">${cancel ? html`<button class="btn" data-r="0">${cancel}</button>` : ""}
    ${ok ? html`<button class="btn ${danger ? "danger" : "primary"}" data-r="1">${ok}</button>` : ""}</div>`;
  const dialog = $("confirm");
  return new Promise((resolve) => {
    const done = (value) => { dialog.onclose = null; if (dialog.open) dialog.close(); resolve(value); };
    box.onclick = (e) => {
      if (onClick && onClick(e, done)) return;
      const b = e.target.closest("[data-r]");
      if (b) done(b.dataset.r === "1");
    };
    dialog.onclose = () => resolve(false);
    dialog.showModal();
    box.querySelector('[data-r="0"]')?.focus();
  });
}

/* ------------------------------------------------------------ 侧栏 */

const GROUP_COLORS = ["#3D5A4C", "#8A5A3B", "#2E4A62", "#7A3B3B", "#4A4458", "#55636B", "#6B4A2E", "#3F5E5A"];
function groupColor(group) { return GROUP_COLORS[state.groups.indexOf(group) % GROUP_COLORS.length]; }
function hasUpdate(book) { return Boolean(book.follow && (book.follow.update > 0 || book.follow.changed)); }

function renderSidebar() {
  const books = state.books;
  const count = (n) => (n ? String(n) : "");
  $("cntAll").textContent = count(books.length);
  $("cntUpd").textContent = count(books.filter(hasUpdate).length);
  $("cntFol").textContent = count(books.filter((b) => b.follow).length);
  $("groupNav").innerHTML = html`${state.groups.map((g) => html`<button class="nav" data-go="library" data-group="${g.id}" data-drop="${g.id}"
    ><span class="dot" style="background:${groupColor(g)}"></span><span>${g.name}</span><span class="count num">${count(books.filter((b) => b.group === g.id).length)}</span></button>`)}`;
  paintNav();
}

/* ------------------------------------------------------------ 全局快捷键 */

function typing() {
  const el = document.activeElement;
  return Boolean(el && (el.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)));
}
function onShortcut(e) {
  if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
  const mod = e.metaKey || e.ctrlKey, key = e.key.toLowerCase();
  if ($("reader") && !$("reader").hidden) return; // 阅读器打开时键盘归阅读器
  if (mod && key === "k") { e.preventDefault(); openPalette(); return; }
  if (document.querySelector("dialog[open]")) return;
  const run = (fn) => { e.preventDefault(); fn(); };
  if (mod && key === "n") run(() => { resetCapture(); go("capture"); });
  else if (mod && key === "1") run(() => go("library", "all"));
  else if (mod && key === "2") run(() => go("tasks"));
  else if (mod && key === ",") run(() => go("settings"));
  else if (mod && key === "enter" && state.view === "capture" && state.probe) run(startJob);
  else if (mod && key === "f" && state.view === "library") run(() => $("libSearch").focus());
  else if (mod && key === "a" && state.view === "library" && !typing()) run(selectAllBooks);
  else if (mod && key === "backspace" && state.view === "library" && !typing() && state.sel.size) run(askDelete);
  else if (key === "escape" && state.view === "library" && state.sel.size && !typing()) run(() => setSelection([]));
}
document.addEventListener("keydown", onShortcut);

/* 任务页按 Esc 取消正在查看的任务（输入中、输入法组字、对话框打开时不触发）。 */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
  if (document.querySelector("dialog[open]")) return;
  if (e.target.isContentEditable || e.target.closest?.("input, textarea, select")) return;
  if (document.querySelector('.view[data-view="tasks"]').hidden) return;
  if (state.job && (state.job.status === "running" || state.job.status === "pending")) {
    api(`/api/jobs/${state.job.id}/cancel`, { method: "POST" }).catch(() => {});
  }
});

/* 不在输入框时 ⌘V 一个链接：直接去采集并识别 */
document.addEventListener("paste", (e) => {
  if (typing() || document.querySelector("dialog[open]")) return;
  const text = (e.clipboardData.getData("text") || "").trim();
  if (/^https?:\/\/\S+$/.test(text)) { e.preventDefault(); go("capture"); captureUrl(text); }
});

/* ------------------------------------------------------------ 启动 */

function boot() {
  if (!TOKEN) {
    document.body.innerHTML = html`<div class="no-token">缺少访问令牌：请从 quire ui 打印的地址进入。</div>`;
    return;
  }
  if (window.webkit?.messageHandlers?.windowDrag) document.documentElement.dataset.shell = "";
  api("/api/settings").then((settings) => {
    state.settings = settings;
    applyTheme(settings.theme, settings.accent);
    if (settings.auto_check_updates) checkAllUpdates(true);
  }).catch(() => {});
  api("/api/capabilities").then((caps) => { state.caps = caps; }).catch(() => {});
  loadBooks();
  loadJobs();
  loadSites();
  go("capture");
}
window.addEventListener("DOMContentLoaded", boot);
