/* 书库检查器：单本（封面、文件、追更、分组、删除）/ 多本（堆叠封面 + 批量操作）。
   选择为空时收起。依赖 app.js / covers.js / follows.js / library.js。 */
"use strict";

const KIND_TEXT = { novel: "小说", manga: "漫画" };
let checkingBook = null;

function selectedBooks() { return state.books.filter((b) => state.sel.has(b.id)); }

function renderInspector() {
  const books = selectedBooks();
  $("libWrap").classList.toggle("insp-open", books.length > 0);
  if (!books.length) { $("inspIn").textContent = ""; return; }
  $("inspIn").innerHTML = books.length === 1 ? singleInspector(books[0]) : multiInspector(books);
}

function followSection(b) {
  const f = b.follow;
  if (!f) return "";
  const checking = checkingBook === b.id;
  const card = f.changed
    ? html`<div class="upd-card"><div class="txt"><b>目录有变动</b><small style="display:block;color:var(--text-3)">建议整本重抓</small></div><button class="btn" data-ia="recapture">去重抓</button></div>`
    : f.update > 0
      ? html`<div class="upd-card"><div class="txt"><b>新增 ${f.update} 章</b></div><button class="btn primary" data-ia="follow">续抓</button></div>` : "";
  return html`<div class="sec"><h4>追更</h4><div class="follow"><div class="txt">已抓到第 ${f.chapters} 章
    <small id="followStage">${checking ? "正在检查…" : "只抓新章节并合进原书"}</small></div>
    <button class="btn" data-ia="check" ${checking ? raw("disabled") : ""}>${checking ? "检查中…" : "检查更新"}</button></div>${card}</div>`;
}

function singleInspector(b) {
  const group = state.groups.find((g) => g.id === b.group);
  const files = b.files || (b.formats || []).map((format) => ({ format, bytes: 0, name: "" }));
  return html`${cover(coverItem(b))}<h2>${b.title}</h2><div class="by">${[b.author, bookHost(b)].filter(Boolean).join(" · ")}</div>
    <div class="acts"><button class="btn primary" data-ia="open">阅读</button><button class="btn" data-ia="external">用系统应用打开</button></div>
    <dl><dt>类型</dt><dd>${KIND_TEXT[b.kind] || b.kind || "—"}</dd>
      <dt>大小</dt><dd class="num">${humanSize(b.bytes)}</dd>
      <dt>加入</dt><dd>${addedOn(b) || "—"}</dd>
      <dt>分组</dt><dd>${group ? group.name : "未分组"}</dd>
      <dt>来源</dt><dd title="${b.source_url || ""}">${bookHost(b) || "—"}</dd></dl>
    ${files.length ? html`<div class="sec"><h4>文件</h4><div class="files">${files.map((f) => html`<button class="file" data-file="${f.format}" title="${f.name ? `阅读 ${f.name}；⌥ 点击在访达中显示` : "阅读"}">
      <span class="ext">${f.format.toUpperCase()}</span><span>${f.name || f.format.toUpperCase()}</span><span class="sz num">${f.bytes ? humanSize(f.bytes) : ""}</span></button>`)}</div></div>` : ""}
    ${followSection(b)}
    <div class="menu-list">
      <button class="btn" data-ia="reveal">在访达中显示</button>
      <button class="btn" data-ia="move">移动到分组…</button>
      ${group ? html`<button class="btn" data-ia="ungroup">移出「${group.name}」</button>` : ""}
      ${b.source_url ? html`<button class="btn" data-ia="recapture">重新采集…</button>` : ""}
      <button class="btn danger" data-ia="delete">删除…</button></div>`;
}

function multiInspector(books) {
  const bytes = books.reduce((sum, b) => sum + (b.bytes || 0), 0);
  const covers = books.slice(0, 3).map((b, i) => html`<div style="transform:translateX(${(i - 1) * 14}px) rotate(${(i - 1) * 4}deg);position:absolute;inset:0">${cover(coverItem(b))}</div>`);
  return html`<div class="stack">${covers}</div><h2>已选 ${books.length} 本</h2><div class="by num">共 ${humanSize(bytes)}</div>
    <div class="menu-list" style="margin-top:18px">
      <button class="btn" data-ia="move">移动到分组…</button>
      ${books.some((b) => b.group) ? html`<button class="btn" data-ia="ungroup">移出分组</button>` : ""}
      ${books.some((b) => b.follow) ? html`<button class="btn" data-ia="checkMany">检查这些书的更新</button>` : ""}
      <button class="btn danger" data-ia="delete">删除 ${books.length} 本…</button></div>
    <p class="note">⌘ 点击增减，Shift 点击连选，Esc 取消选择</p>`;
}

/* ------------------------------------------------------------ 移动到分组 */

async function pickGroup(ids) {
  let chosen;
  const body = html`<div class="rows">${state.groups.map((g) => html`<div><span class="dot" style="width:8px;height:8px;border-radius:50%;background:${groupColor(g)}"></span>
      <div class="t"><b>${g.name}</b></div><button class="btn" data-pick="${g.id}">移到这里</button></div>`)}
    <div><div class="t"><input class="field-input" id="newGroupIn" style="width:100%" maxlength="40" placeholder="新分组名" aria-label="新分组名"></div><button class="btn primary" data-pick="__new__">新建并移入</button></div></div>`;
  await confirmBox({
    title: ids.length > 1 ? `把 ${ids.length} 本移到…` : "移动到分组", body, ok: "", cancel: "取消", wide: true,
    onClick: (e, done) => {
      const b = e.target.closest("[data-pick]");
      if (!b) return false;
      if (b.dataset.pick === "__new__") {
        const name = $("newGroupIn").value.trim();
        if (!name) { $("newGroupIn").focus(); return true; }
        chosen = { name };
      } else chosen = { id: b.dataset.pick };
      done(true);
      return true;
    },
  });
  if (!chosen) return;
  if (chosen.id) { moveBooks(ids, chosen.id); return; }
  try {
    const group = await api("/api/groups", { method: "POST", body: { name: chosen.name } });
    await loadBooks();
    moveBooks(ids, group.id || (state.groups.find((g) => g.name === chosen.name) || {}).id);
  } catch (err) { toast(errText(err)); }
}

/* ------------------------------------------------------------ 操作 */

async function checkSelected(books) {
  for (const b of books.filter((x) => x.follow)) {
    checkingBook = b.id;
    renderInspector();
    try {
      await checkBookUpdate(b, (stage) => { const s = $("followStage"); if (s) s.textContent = `正在检查 · ${stage}`; });
    } catch (err) { toast(`《${b.title}》检查失败：${errText(err)}`); }
  }
  checkingBook = null;
  renderInspector();
}

$("inspIn").addEventListener("click", (e) => {
  const books = selectedBooks();
  const b = books[0];
  const file = e.target.closest("[data-file]");
  if (file && b) { (e.altKey ? revealBook : openBook)(b.id, file.dataset.file); return; }
  const a = e.target.closest("[data-ia]");
  if (!a || !b) return;
  const ids = books.map((x) => x.id);
  switch (a.dataset.ia) {
    case "open": openBook(b.id); break;
    case "reveal": revealBook(b.id); break;
    case "external": openExternal(b.id); break;
    case "check": case "checkMany": checkSelected(books); break;
    case "follow": followBook(b); break;
    case "recapture": resumeInCapture(b); break;
    case "move": pickGroup(ids); break;
    case "ungroup": moveBooks(ids, null); break;
    case "delete": askDelete(); break;
    default: break;
  }
});
