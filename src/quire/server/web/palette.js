/* ⌘K 命令面板：粘贴链接直接采集；搜书、跳任务、执行命令。依赖 app.js 及各视图模块。 */
"use strict";

const COMMANDS = [
  ["采集新链接", "⌘N", () => { resetCapture(); go("capture"); }],
  ["书库", "⌘1", () => go("library", "all")],
  ["任务", "⌘2", () => go("tasks")],
  ["设置", "⌘,", () => go("settings")],
  ["检查全部追更", "", () => { go("library", "following"); checkAllUpdates(false); }],
  ["切换浅色 / 深色", "", () => {
    const next = document.documentElement.dataset.mode === "dark" ? "light" : "dark";
    applyTheme(next, document.documentElement.dataset.accent);
    api("/api/settings", { method: "PUT", body: { theme: next } }).then((s) => { state.settings = s; }).catch(() => {});
  }],
];
let palItems = [];
let palIndex = 0;

function openPalette() {
  const dialog = $("palette");
  if (dialog.open) { $("palIn").select(); return; }
  if (document.querySelector("dialog[open]")) return;
  $("palIn").value = "";
  renderPalette();
  dialog.showModal();
  $("palIn").focus();
}

function palMatch(text, q) { return text.toLowerCase().includes(q); }

function renderPalette() {
  const q = $("palIn").value.trim();
  const lq = q.toLowerCase();
  const items = [];
  if (/^https?:\/\/\S+$/.test(q)) items.push({ sec: "链接", label: `采集 ${host(q)}`, hint: "↵", run: () => { go("capture"); captureUrl(q); } });
  const books = state.books.filter((b) => !q || palMatch(`${b.title} ${b.author || ""}`, lq)).slice(0, q ? 8 : 5);
  books.forEach((b) => items.push({ sec: "书籍", book: b, label: b.title, hint: fmtList(b), run: () => revealInLibrary(b.id) }));
  if (q) visibleJobs().filter((j) => palMatch(j.title || "", lq)).slice(0, 4)
    .forEach((j) => items.push({ sec: "任务", job: j, label: j.title, hint: STATUS_TEXT[j.status] || "", run: () => showJob(j.id) }));
  COMMANDS.filter(([label]) => !q || palMatch(label, lq))
    .forEach(([label, hint, run]) => items.push({ sec: "命令", label, hint, run }));
  palItems = items;
  palIndex = 0;
  let sec = "";
  $("palList").innerHTML = items.length ? html`${items.map((it, i) => {
    const head = it.sec !== sec ? html`<li class="sec" role="presentation">${(sec = it.sec)}</li>` : "";
    return html`${head}<li role="option" id="pal-${i}" data-i="${i}" aria-selected="${String(i === 0)}">
      ${it.book ? cover(coverItem(it.book)) : it.job ? cover({ title: it.job.title }) : ""}<span>${it.label}</span><small>${it.hint}</small></li>`;
  })}` : html`<li class="sec" role="presentation">没有匹配的结果</li>`;
  $("palIn").setAttribute("aria-activedescendant", items.length ? "pal-0" : "");
}

function movePal(delta) {
  if (!palItems.length) return;
  palIndex = (palIndex + delta + palItems.length) % palItems.length;
  $("palList").querySelectorAll("[data-i]").forEach((li) => li.setAttribute("aria-selected", String(Number(li.dataset.i) === palIndex)));
  $(`pal-${palIndex}`).scrollIntoView({ block: "nearest" });
  $("palIn").setAttribute("aria-activedescendant", `pal-${palIndex}`);
}

function runPal(i) {
  const it = palItems[i];
  if (!it) return;
  $("palette").close();
  it.run();
}

$("palIn").addEventListener("input", renderPalette);
$("palIn").addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "ArrowDown") { e.preventDefault(); movePal(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); movePal(-1); }
  else if (e.key === "Enter") { e.preventDefault(); runPal(palIndex); }
});
$("palList").addEventListener("click", (e) => {
  const li = e.target.closest("[data-i]");
  if (li) runPal(Number(li.dataset.i));
});
$("palList").addEventListener("mousemove", (e) => {
  const li = e.target.closest("[data-i]");
  if (li && Number(li.dataset.i) !== palIndex) movePal(Number(li.dataset.i) - palIndex);
});
$("palette").addEventListener("click", (e) => { if (e.target === $("palette")) $("palette").close(); });
$("paletteBtn").addEventListener("click", openPalette);
