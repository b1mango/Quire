/* 书库：封面格 / 列表、选择语义（单选 / ⌘ 多选 / Shift 连选 / ⌘A / 方向键）、
   拖到侧栏分组、移动可撤销、删除（部分失败只重试失败的书）、分组增删改。
   检查器渲染在 inspector.js。依赖 app.js / covers.js。 */
"use strict";

function bookHost(b) { try { return new URL(b.source_url).host.replace(/^www\./, ""); } catch (_) { return ""; } }
function addedOn(b) {
  const d = new Date(b.created_at);
  return Number.isNaN(d.getTime()) ? "" : `${d.getMonth() + 1}月${d.getDate()}日`;
}
function coverItem(b) { return { ...b, upd: hasUpdate(b) }; }
function fmtList(b) { return (b.formats || []).map((f) => f.toUpperCase()).join(" · "); }

function loadBooks() {
  return api("/api/books").then((data) => {
    state.books = data.books || [];
    state.groups = data.groups || [];
    state.booksLoaded = true;
    state.booksError = null;
    const ids = new Set(state.books.map((b) => b.id));
    state.sel = new Set([...state.sel].filter((id) => ids.has(id)));
    if (state.group !== "all" && !["updates", "following"].includes(state.group)
        && !state.groups.some((g) => g.id === state.group)) state.group = "all";
    renderSidebar();
    if (state.view === "library") renderLibrary();
  }).catch((err) => {
    state.booksError = errText(err);
    if (state.view === "library") renderLibrary();
  });
}

function visibleBooks() {
  let books = state.books;
  if (state.group === "updates") books = books.filter(hasUpdate);
  else if (state.group === "following") books = books.filter((b) => b.follow);
  else if (state.group !== "all") books = books.filter((b) => b.group === state.group);
  const q = state.search.toLowerCase();
  if (q) books = books.filter((b) => `${b.title} ${b.author || ""}`.toLowerCase().includes(q));
  return books;
}

function currentGroup() { return state.groups.find((g) => g.id === state.group); }
function groupTitle() {
  return { all: "书库", updates: "有更新", following: "追更中" }[state.group] || (currentGroup() || {}).name || "书库";
}

/* ------------------------------------------------------------ 渲染 */

function bookCard(b) {
  const upd = b.follow && b.follow.update > 0 ? html`<span class="new">+${b.follow.update} 章</span>` : b.follow && b.follow.changed ? html`<span class="new">目录变动</span>` : "";
  return html`<button class="book" role="option" draggable="true" data-id="${b.id}" aria-selected="${String(state.sel.has(b.id))}" title="${b.title}">
    ${cover(coverItem(b))}<div><div class="t">${b.title}</div><div class="m num">${upd}<span>${fmtList(b)}</span></div></div></button>`;
}

function bookRow(b) {
  return html`<tr data-id="${b.id}" draggable="true" aria-selected="${String(state.sel.has(b.id))}" tabindex="0">
    <td><div class="lt">${cover(coverItem(b))}<span>${b.title}</span>${b.follow && b.follow.update > 0 ? html` <span class="num" style="color:var(--accent-text);font-weight:600">+${b.follow.update}</span>` : ""}</div></td>
    <td>${b.author || "—"}</td><td style="color:var(--text-2)">${bookHost(b)}</td>
    <td class="r">${fmtList(b)}</td><td class="r num">${humanSize(b.bytes)}</td><td class="r">${addedOn(b)}</td></tr>`;
}

function emptyShelf() {
  if (state.booksError) return html`<div class="empty"><div><h3>书库没能加载</h3><p>${state.booksError}</p><button class="btn" id="libRetry">重试</button></div></div>`;
  if (state.search) return html`<div class="empty"><div><h3>没有找到「${state.search}」</h3><p>换个关键词，或在全部书籍里找找</p><button class="btn" id="clearSearch">清除搜索</button></div></div>`;
  if (!state.books.length) return html`<div class="empty"><div><div class="ghost-books" style="justify-content:center"><i></i><i></i><i></i></div>
    <h3>书架还空着</h3><p>贴一个作品链接采集，或把网盘下载的 TXT、EPUB、PDF、CBZ 直接拖进窗口</p>
    <div style="display:flex;gap:8px;justify-content:center"><button class="btn primary" data-go="capture">去采集</button><button class="btn" data-import="1">导入本地文件</button></div></div></div>`;
  const hint = state.group === "updates" ? "追更中的书有新章节时会出现在这里"
    : state.group === "following" ? "采集过的书可以在详情里打开「追更」" : "把书拖到左侧分组，或在详情里选择「移动到分组」";
  return html`<div class="empty"><div><h3>这里还没有书</h3><p>${hint}</p><button class="btn" data-go="library" data-group="all">查看全部书籍</button></div></div>`;
}

function renderLibrary() {
  const books = visibleBooks();
  const group = currentGroup();
  $("libTitle").textContent = groupTitle();
  $("libSub").textContent = books.length ? `${books.length} 本` : "";
  $("checkAll").hidden = !state.books.some((b) => b.follow);
  $("libMsg").innerHTML = group ? html`<div style="display:flex;gap:6px;padding:0 24px 8px">
    <button class="btn quiet" id="renameGroup">重命名分组</button><button class="btn quiet danger" id="deleteGroup">删除分组</button></div>` : "";
  const shelf = $("shelf");
  if (!books.length) {
    shelf.className = "";
    shelf.removeAttribute("role");
    shelf.innerHTML = emptyShelf();
  } else if (state.layout === "grid") {
    shelf.className = "shelf";
    shelf.setAttribute("role", "listbox");
    shelf.setAttribute("aria-multiselectable", "true");
    shelf.setAttribute("aria-label", groupTitle());
    shelf.innerHTML = html`${books.map(bookCard)}`;
  } else {
    shelf.className = "list";
    shelf.removeAttribute("role");
    shelf.innerHTML = html`<table><thead><tr><th>书名</th><th>作者</th><th>来源</th><th class="r">格式</th><th class="r">大小</th><th class="r">加入</th></tr></thead>
      <tbody>${books.map(bookRow)}</tbody></table>`;
  }
  renderInspector();
}

/* ------------------------------------------------------------ 选择 */

function setSelection(ids, anchor) {
  state.sel = new Set(ids);
  if (anchor !== undefined) state.anchor = anchor;
  document.querySelectorAll("#shelf [data-id]").forEach((el) =>
    el.setAttribute("aria-selected", String(state.sel.has(el.dataset.id))));
  renderInspector();
}
function selectAllBooks() { setSelection(visibleBooks().map((b) => b.id)); }

$("shelf").addEventListener("click", (e) => {
  const el = e.target.closest("[data-id]");
  if (!el) { if (e.target.closest("#shelf") && !e.target.closest("button")) setSelection([]); return; }
  const id = el.dataset.id, ids = visibleBooks().map((b) => b.id);
  if (e.shiftKey && state.anchor && ids.includes(state.anchor)) {
    const a = ids.indexOf(state.anchor), z = ids.indexOf(id);
    setSelection(ids.slice(Math.min(a, z), Math.max(a, z) + 1));
  } else if (e.metaKey || e.ctrlKey) {
    const next = new Set(state.sel);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelection(next, id);
  } else setSelection([id], id);
  el.focus();
});
$("shelf").addEventListener("dblclick", (e) => {
  const el = e.target.closest("[data-id]");
  if (el) openBook(el.dataset.id);
});
$("shelf").addEventListener("keydown", (e) => {
  const el = e.target.closest("[data-id]");
  if (!el) return;
  if (e.key === "Enter") { e.preventDefault(); openBook(el.dataset.id); return; }
  if (e.key === " ") { e.preventDefault(); el.click(); return; }
  const cols = state.layout === "grid"
    ? Math.max(1, getComputedStyle($("shelf")).gridTemplateColumns.split(" ").length) : 1;
  const delta = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
  if (!delta) return;
  const items = [...$("shelf").querySelectorAll("[data-id]")];
  const next = items[Math.min(items.length - 1, Math.max(0, items.indexOf(el) + delta))];
  if (!next) return;
  e.preventDefault();
  next.focus();
  if (e.shiftKey) setSelection([...state.sel, next.dataset.id]);
  else setSelection([next.dataset.id], next.dataset.id);
});
$("libMsg").addEventListener("click", (e) => {
  if (e.target.id === "renameGroup") renameGroup(currentGroup());
  if (e.target.id === "deleteGroup") deleteGroup(currentGroup());
});
$("shelf").addEventListener("click", (e) => {
  if (e.target.id === "libRetry") loadBooks();
  if (e.target.id === "clearSearch") { $("libSearch").value = ""; state.search = ""; renderLibrary(); }
});

/* ------------------------------------------------------------ 拖到分组 */

document.addEventListener("dragstart", (e) => {
  const el = e.target.closest?.("#shelf [data-id]");
  if (!el) return;
  if (!state.sel.has(el.dataset.id)) setSelection([el.dataset.id], el.dataset.id);
  e.dataTransfer.setData("application/x-quire-books", [...state.sel].join(","));
  e.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragover", (e) => {
  const t = e.target.closest?.("[data-drop]");
  if (t && e.dataTransfer.types.includes("application/x-quire-books")) { e.preventDefault(); t.classList.add("drop"); }
});
document.addEventListener("dragleave", (e) => { e.target.closest?.("[data-drop]")?.classList.remove("drop"); });
document.addEventListener("drop", (e) => {
  const t = e.target.closest?.("[data-drop]");
  if (!t) return;
  e.preventDefault();
  t.classList.remove("drop");
  const ids = e.dataTransfer.getData("application/x-quire-books").split(",").filter(Boolean);
  if (ids.length) moveBooks(ids, t.dataset.drop);
});

/* ------------------------------------------------------------ 移动（可撤销） */

function moveBooks(ids, groupId, undoable = true) {
  const before = ids.map((id) => [id, (state.books.find((b) => b.id === id) || {}).group || null]);
  return api("/api/books/batch", { method: "POST", body: { action: "move", ids, group_id: groupId } }).then(() => {
    const name = groupId ? (state.groups.find((g) => g.id === groupId) || {}).name : null;
    loadBooks();
    if (!undoable) return;
    toast(name ? `已将 ${ids.length} 本移到「${name}」` : `已将 ${ids.length} 本移出分组`, "撤销", () => {
      const byGroup = new Map();
      before.forEach(([id, g]) => byGroup.set(g, [...(byGroup.get(g) || []), id]));
      Promise.all([...byGroup].map(([g, list]) =>
        api("/api/books/batch", { method: "POST", body: { action: "move", ids: list, group_id: g } })))
        .then(loadBooks).catch((err) => toast(errText(err)));
    });
  }).catch((err) => toast(errText(err)));
}

/* ------------------------------------------------------------ 删除（不可撤销，确认写清后果） */

async function askDelete() {
  let ids = [...state.sel];
  if (!ids.length) return;
  const title = (id) => (state.books.find((b) => b.id === id) || {}).title || id;
  let message = html`<p>书库记录和导出的文件会一起删除，无法恢复。</p>`;
  let heading = ids.length > 1 ? `删除 ${ids.length} 本书？` : `删除《${title(ids[0])}》？`;
  for (;;) {
    const ok = await confirmBox({ title: heading, body: message, ok: "删除", cancel: "保留", danger: true });
    if (!ok) return;
    let result;
    try {
      result = await api("/api/books/batch", { method: "POST", body: { action: "delete", ids } });
    } catch (err) {
      message = html`<p>删除请求没有完成：${errText(err)}</p>`;
      heading = "没删掉，要重试吗？";
      continue;
    }
    const failures = result.failures || [];
    await loadBooks();
    if (!failures.length) {
      toast(ids.length > 1 ? `已删除 ${ids.length} 本` : `已删除《${title(ids[0])}》`);
      setSelection([]);
      return;
    }
    ids = failures.map((f) => f.id);
    setSelection(ids);
    heading = `剩余的 ${ids.length} 本没删掉`;
    message = html`<div class="rows">${failures.map((f) => html`<div><div class="t"><b>${title(f.id)}</b><small class="bad">${f.error}</small></div></div>`)}</div>`;
  }
}

/* ------------------------------------------------------------ 分组 */

async function askName(title, value = "") {
  const ok = await confirmBox({ title, ok: "好", body: html`<p><input class="field-input" id="nameIn" style="width:100%" maxlength="40" value="${value}" aria-label="分组名"></p>` });
  return ok ? ($("nameIn").value || "").trim() : "";
}
async function addGroup() {
  const name = await askName("新建分组");
  if (name) api("/api/groups", { method: "POST", body: { name } }).then(loadBooks).catch((err) => toast(errText(err)));
}
async function renameGroup(group) {
  if (!group) return;
  const name = await askName("重命名分组", group.name);
  if (name && name !== group.name)
    api(`/api/groups/${group.id}`, { method: "PUT", body: { name } }).then(loadBooks).catch((err) => toast(errText(err)));
}
async function deleteGroup(group) {
  if (!group) return;
  const ok = await confirmBox({ title: `删除分组「${group.name}」？`, body: html`<p>只删除分组，书还在书库里。</p>`, ok: "删除分组", danger: true });
  if (ok) api(`/api/groups/${group.id}`, { method: "DELETE" }).then(() => { state.group = "all"; loadBooks(); }).catch((err) => toast(errText(err)));
}
$("addGroupBtn").addEventListener("click", addGroup);

/* ------------------------------------------------------------ 打开 / 定位 */

/* 打开 = 在内置阅读器里读；交给系统应用另走 openExternal */
function openBook(id, format) {
  if (id) openReader(id, format);
}
function openExternal(id, format) {
  if (!id) return;
  api(`/api/books/${id}/open`, { method: "POST", body: format ? { format } : {} }).catch((err) => toast(errText(err)));
}
function revealBook(id, format) {
  api(`/api/books/${id}/reveal`, { method: "POST", body: format ? { format } : {} }).catch((err) => toast(errText(err)));
}
function revealInLibrary(id) {
  state.search = ""; $("libSearch").value = "";
  go("library", "all");
  setSelection([id], id);
  document.querySelector(`#shelf [data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest" });
}

let searchTimer = null;
$("libSearch").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value.trim(); renderLibrary(); }, 120);
});
$("layoutSeg").addEventListener("click", (e) => {
  const b = e.target.closest("[data-layout]");
  if (!b) return;
  state.layout = b.dataset.layout;
  localStorage.setItem("quire-layout", state.layout);
  $("layoutSeg").querySelectorAll("[data-layout]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  renderLibrary();
});
$("libScroll").addEventListener("scroll", (e) => $("libBar").classList.toggle("scrolled", e.target.scrollTop > 4));
state.layout = localStorage.getItem("quire-layout") === "list" ? "list" : "grid";
$("layoutSeg").querySelectorAll("[data-layout]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.layout === state.layout)));
VIEW_HOOKS.library = () => { renderLibrary(); if (!state.booksLoaded) loadBooks(); };
