"use strict";
let bookMenuTrigger = null;
function closeBookMenu(focus = true) {
  const menu = $("bookActions");
  if (menu.matches(":popover-open")) menu.hidePopover();
  if (bookMenuTrigger) {
    bookMenuTrigger.setAttribute("aria-expanded", "false");
    if (focus && bookMenuTrigger.isConnected) {
      const trigger = bookMenuTrigger;
      setTimeout(() => trigger.focus({ preventScroll: true }), 0);
    }
  }
}
function openLibraryBook(book) {
  closeBookMenu(false);
  api(`/api/books/${book.id}/open`, { method: "POST" }).catch(err => {
    $("libSub").textContent = err.hint ? `${err.message} ${err.hint}` : err.message;
  });
}

/* ------------------------------------------------------------ 分组与批量 */

function visibleBooks() {
  if (!state.filterGroup) return state.books;
  return state.books.filter((b) => b.group === state.filterGroup);
}

function renderLibrary(search) {
  renderFolders();
  const grid = $("grid");
  grid.textContent = "";
  const books = visibleBooks();
  const total = state.books.reduce((sum, b) => sum + b.bytes, 0);
  const group = (state.groups || []).find((g) => g.id === state.filterGroup);
  $("libTitle").textContent = group ? group.name : "书库";
  $("libBackBtn").hidden = !group;
  $("libSub").textContent = search
    ? `“${search}” · ${books.length} 部`
    : group
      ? `${books.length} 部`
      : `${state.books.length} 部作品 · 共 ${humanSize(total)}`;
  syncLibraryEmpty(search, books, group);
  books.forEach((book) => grid.appendChild(bookCard(book)));
  grid.classList.toggle("selecting", state.selected.size > 0);
  syncBatchBar();
}

/* 空态分三种：初次空库（引导去采集台）、搜索无结果（可清除搜索）、
   空分组（可查看全部）；搜索与分组叠加时文案与动作一并说明。 */
function syncLibraryEmpty(search, books, group) {
  const empty = $("libEmpty");
  const text = $("libEmptyText");
  const clear = $("emptyClearBtn");
  if (state.books.length === 0 && search) {
    empty.hidden = false;
    text.textContent = group
      ? `没有找到与“${search}”匹配的书（当前在「${group.name}」分组）`
      : `没有找到与“${search}”匹配的书`;
    $("emptyNewBtn").hidden = true;
    clear.hidden = false;
    clear.textContent = state.filterGroup ? "清除搜索并查看全部" : "清除搜索";
  } else if (state.books.length === 0) {
    empty.hidden = false;
    text.textContent = "还没有书。贴一个链接试试。";
    $("emptyNewBtn").hidden = false;
    clear.hidden = true;
  } else if (books.length === 0 && group) {
    empty.hidden = false;
    text.textContent = `分组「${group.name}」里还没有书`;
    $("emptyNewBtn").hidden = true;
    clear.hidden = false;
    clear.textContent = "查看全部";
  } else {
    empty.hidden = true;
  }
}

/* 分组为文件夹卡片：2×2 封面拼贴 + 组名 + 本数；点击进入分组，
   拖书上去即移入；空组可从「…」删除。 */
function renderFolders() {
  const row = $("folderRow");
  const groups = state.groups || [];
  row.hidden = state.filterGroup !== null || groups.length === 0;
  row.textContent = "";
  if (row.hidden) return;
  groups.forEach((group) => {
    const folder = document.createElement("button");
    folder.type = "button";
    folder.className = "folder";
    folder.setAttribute("aria-label", `打开分组「${group.name}」,${group.members} 本`);
    const collage = document.createElement("span");
    collage.className = "folder-covers";
    const members = state.books.filter((b) => b.group === group.id).slice(0, 4);
    if (members.length) {
      members.forEach((book) => {
        const img = document.createElement("img");
        img.alt = ""; img.loading = "lazy";
        img.src = book.cover ? `${book.cover}?token=${encodeURIComponent(TOKEN)}` : coverPlaceholder(book.title, book.id);
        collage.append(img);
      });
    } else {
      const empty = document.createElement("span");
      empty.className = "folder-empty";
      empty.textContent = "空";
      collage.append(empty);
    }
    const name = document.createElement("span");
    name.className = "folder-name"; name.textContent = group.name;
    const count = document.createElement("span");
    count.className = "folder-count"; count.textContent = `共 ${group.members} 本`;
    folder.append(collage, name, count);
    folder.addEventListener("click", () => {
      state.filterGroup = group.id;
      renderLibrary($("searchInput").value.trim());
    });
    /* 拖拽入组：书卡拖上文件夹即移动 */
    folder.addEventListener("dragover", (e) => { e.preventDefault(); folder.classList.add("over"); });
    folder.addEventListener("dragleave", () => folder.classList.remove("over"));
    folder.addEventListener("drop", (e) => {
      e.preventDefault();
      folder.classList.remove("over");
      const ids = dragIds(e);
      if (ids.length) moveBooks(ids, group.id);
    });
    /* 空组删除:独立的「…」按钮,键盘可单独到达 */
    if (!group.members) {
      const more = document.createElement("span");
      more.type = "button";
      more.className = "folder-del";
      more.setAttribute("role", "button");
      more.setAttribute("tabindex", "0");
      more.setAttribute("aria-label", `删除空分组「${group.name}」`);
      more.textContent = "…";
      const remove = () => {
        api(`/api/groups/${group.id}`, { method: "DELETE" })
          .then(() => loadBooks())
          .catch((err) => { $("libSub").textContent = err.message; });
      };
      more.addEventListener("click", (e) => { e.stopPropagation(); remove(); });
      more.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.stopPropagation(); remove(); } });
      folder.append(more);
    }
    row.appendChild(folder);
  });
}

function moveBooks(ids, groupId) {
  api("/api/books/batch", { method: "POST", body: { action: "move", ids, group_id: groupId } })
    .then((result) => {
      state.selected = new Set();
      state.lastPickId = null;
      const group = (state.groups || []).find((g) => g.id === groupId);
      const count = result && Number.isFinite(result.moved) ? result.moved : ids.length;
      const message = groupId
        ? `已把 ${count} 本移到「${group ? group.name : "分组"}」`
        : `已把 ${count} 本移出分组`;
      /* 列表重载会重写 libSub，反馈在重载落定后给出 */
      loadBooks().then(() => { $("libSub").textContent = message; });
    })
    .catch((err) => { $("libSub").textContent = err.message; });
}

let groupMenuIds = [];
let groupMenuTrigger = null;
function closeGroupMenu(focus = true) {
  const menu = $("groupMenu");
  if (menu.matches(":popover-open")) menu.hidePopover();
  if (groupMenuTrigger) {
    groupMenuTrigger.setAttribute("aria-expanded", "false");
    if (focus && groupMenuTrigger.isConnected) {
      const trigger = groupMenuTrigger;
      setTimeout(() => trigger.focus({ preventScroll: true }), 0);
    }
  }
}
function openGroupMenu(ids, anchor) {
  groupMenuIds = ids;
  groupMenuTrigger = anchor || null;
  const menu = $("groupMenu");
  menu.querySelectorAll("button").forEach((b) => b.remove());
  (state.groups || []).forEach((group) => {
    const item = document.createElement("button");
    item.setAttribute("role", "menuitem");
    item.textContent = group.name;
    item.addEventListener("click", () => { menu.hidePopover(); moveBooks(groupMenuIds, group.id); });
    menu.appendChild(item);
  });
  const out = document.createElement("button");
  out.setAttribute("role", "menuitem");
  out.textContent = "移出分组";
  out.addEventListener("click", () => { menu.hidePopover(); moveBooks(groupMenuIds, null); });
  menu.appendChild(out);
  menu.showPopover();
  if (groupMenuTrigger) groupMenuTrigger.setAttribute("aria-expanded", "true");
  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 6, innerHeight - menu.offsetHeight - 8))}px`;
  const first = menu.querySelector("button");
  if (first) first.focus();
}
$("groupMenu").addEventListener("toggle", (event) => {
  if (event.newState === "closed" && groupMenuTrigger) {
    groupMenuTrigger.setAttribute("aria-expanded", "false");
    if (document.activeElement === document.body && groupMenuTrigger.isConnected)
      groupMenuTrigger.focus({ preventScroll: true });
  }
});
$("groupMenu").addEventListener("keydown", (event) => {
  const buttons = [...event.currentTarget.querySelectorAll("button")];
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeGroupMenu(); }
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault(); const index = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }
});

function syncBatchBar() {
  const books = visibleBooks();
  const visibleSelected = books.filter((b) => state.selected.has(b.id)).length;
  const hiddenSelected = state.selected.size - visibleSelected;
  /* 切组/搜索不清选择；看不到的已选要明确报数，删除确认以总数为准 */
  $("batchMeta").textContent = `已选 ${state.selected.size} 本` +
    (hiddenSelected > 0 ? `，其中 ${hiddenSelected} 本不在当前结果中` : "");
  const all = books.length > 0 && books.every((b) => state.selected.has(b.id));
  $("selectAllBtn").textContent = all ? "全不选" : "全选当前结果";
  $("batchMoveBtn").disabled = !state.selected.size;
  $("batchDeleteBtn").disabled = !state.selected.size;
  $("batchBar").hidden = state.selected.size === 0;
}

function clearSelection() {
  state.selected = new Set();
  state.lastPickId = null;
  renderLibrary($("searchInput").value.trim());
}

/* 文件管理器式选择：点选只留这本（再点取消）；Cmd/Ctrl 切换个别项；
   Shift 以最近点击为锚点连选；双击打开。框选与拖拽入组见文件底部。 */
function singlePick(id) {
  if (state.selected.size === 1 && state.selected.has(id)) {
    state.selected = new Set();
    state.lastPickId = null;
  } else {
    state.selected = new Set([id]);
    state.lastPickId = id;
  }
  renderLibrary($("searchInput").value.trim());
}

function togglePick(id, dot, btn) {
  if (state.selected.has(id)) { state.selected.delete(id); dot.classList.remove("on"); }
  else { state.selected.add(id); dot.classList.add("on"); }
  state.lastPickId = id;
  if (btn) btn.setAttribute("aria-pressed", String(state.selected.has(id)));
  const card = btn && btn.closest(".card");
  if (card) card.classList.toggle("selected", state.selected.has(id));
  $("grid").classList.toggle("selecting", state.selected.size > 0);
  syncBatchBar();
}

function rangePick(anchorId, targetId) {
  const books = visibleBooks();
  const from = books.findIndex((b) => b.id === anchorId);
  const to = books.findIndex((b) => b.id === targetId);
  if (from < 0 || to < 0) return;
  const [lo, hi] = from < to ? [from, to] : [to, from];
  for (let i = lo; i <= hi; i += 1) state.selected.add(books[i].id);
  renderLibrary($("searchInput").value.trim());
}

/* ------------------------------------------------------------ 书籍卡片 */

function bookCard(book) {
  const card = document.createElement("article");
  card.className = "card";
  card.dataset.bookId = book.id;
  const open = document.createElement("button");
  open.type = "button"; open.className = "book-open";
  open.setAttribute("aria-label", `打开《${book.title}》`);
  const cover = document.createElement("div"); cover.className = "cover";
  const img = document.createElement("img"); img.alt = ""; img.loading = "lazy";
  img.src = book.cover ? `${book.cover}?token=${encodeURIComponent(TOKEN)}` : coverPlaceholder(book.title, book.id);
  cover.append(img);
  const badge = followBadge(book); if (badge) cover.append(badge);
  const title = document.createElement("div"); title.className = "ct"; title.textContent = book.title;
  open.append(cover, title);
  const footer = document.createElement("div"); footer.className = "book-footer";
  const meta = document.createElement("div"); meta.className = "cm";
  meta.textContent = book.author
    ? `${book.author} · ${book.formats.join(" / ").toUpperCase()} · ${humanSize(book.bytes)}`
    : `${book.formats.join(" / ").toUpperCase()} · ${humanSize(book.bytes)}`;
  const dot = document.createElement("span");
  dot.className = "pick" + (state.selected.has(book.id) ? " on" : "");
  cover.append(dot);
  if (state.selected.has(book.id)) card.classList.add("selected");
  open.setAttribute("aria-pressed", String(state.selected.has(book.id)));
  open.addEventListener("click", (event) => {
    if (event.shiftKey && state.lastPickId && state.lastPickId !== book.id) {
      rangePick(state.lastPickId, book.id);
      return;
    }
    if (event.metaKey || event.ctrlKey) { togglePick(book.id, dot, open); return; }
    singlePick(book.id);
  });
  open.addEventListener("dblclick", () => openLibraryBook(book));
  open.addEventListener("keydown", (event) => {
    /* 键盘:已选中的卡按 Enter 打开;未选中时 Enter 走 click 选中 */
    if (event.key === "Enter" && state.selected.has(book.id)) {
      event.preventDefault();
      openLibraryBook(book);
    }
  });
  card.draggable = true;
  card.addEventListener("dragstart", (e) => {
    /* 已选中的书被拖动时整组移动；未选中的书拖动时只带自己 */
    const ids = state.selected.has(book.id) ? [...state.selected] : [book.id];
    e.dataTransfer.setData("text/plain", JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
  });
  const more = document.createElement("button"); more.type = "button"; more.className = "book-more";
  more.setAttribute("aria-label", `《${book.title}》的更多操作`);
  more.setAttribute("aria-expanded", "false"); more.setAttribute("aria-controls", "bookActions");
  more.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';
  more.addEventListener("click", () => selectBook(book, more));
  footer.append(meta, more);
  card.append(open, footer); return card;
}
function selectBook(book, trigger) {
  const menu = $("bookActions");
  const same = bookMenuTrigger === trigger && menu.matches(":popover-open");
  closeBookMenu(false); if (same) return;
  state.selectedBook = book; bookMenuTrigger = trigger;
  $("bookMoveBtn").hidden = !(state.groups || []).length && !book.group;
  updateFollowButtons(book);
  menu.showPopover(); trigger.setAttribute("aria-expanded", "true");
  const rect = trigger.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 6, innerHeight - menu.offsetHeight - 8))}px`;
  menu.querySelector("button:not([hidden])").focus();
}
$("bookActions").addEventListener("toggle", event => {
  if (event.newState === "closed" && bookMenuTrigger) {
    bookMenuTrigger.setAttribute("aria-expanded", "false");
    if (document.activeElement === document.body && bookMenuTrigger.isConnected)
      bookMenuTrigger.focus({ preventScroll: true });
  }
});
$("bookActions").addEventListener("keydown", event => {
  const buttons = [...event.currentTarget.querySelectorAll("button:not([hidden]):not(:disabled)")];
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeBookMenu(); }
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault(); const index = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }
});
window.addEventListener("resize", () => closeBookMenu(false));


document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("bookActions").matches(":popover-open")) {
    event.preventDefault(); event.stopImmediatePropagation(); closeBookMenu();
  }
}, true);

/* ------------------------------------------------------------ 操作绑定 */

$("bookMoveBtn").addEventListener("click", () => {
  if (!state.selectedBook || !bookMenuTrigger) return;
  /* 更多菜单作用在当前选择集（若该书在选择内），否则只作用于它自己 */
  const ids = state.selected.has(state.selectedBook.id) && state.selected.size > 1
    ? [...state.selected]
    : [state.selectedBook.id];
  const anchor = bookMenuTrigger;
  closeBookMenu(false);
  openGroupMenu(ids, anchor);
});

let pendingDeleteBook = null;
let pendingBatchIds = null;
let libMenuIds = null;

/* 拖拽载荷：优先 JSON 数组（批量拖动），兼容早期的单本纯文本 */
function dragIds(event) {
  const raw = event.dataTransfer.getData("text/plain");
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [String(parsed)];
  } catch (_) { return [raw]; }
}

function openLibMenu(ids, x, y) {
  libMenuIds = ids;
  const menu = $("libMenu");
  $("libMenuMeta").textContent = `已选 ${ids.length} 本`;
  $("libMenuMove").hidden = !(state.groups || []).length;
  menu.showPopover();
  menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`;
  menu.querySelector("button:not([hidden])").focus();
}
function closeLibMenu() {
  const menu = $("libMenu");
  if (menu.matches(":popover-open")) menu.hidePopover();
}
$("libMenu").addEventListener("keydown", (event) => {
  const buttons = [...event.currentTarget.querySelectorAll("button:not([hidden])")];
  if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeLibMenu(); }
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }
});
$("libMenuMove").addEventListener("click", () => {
  const ids = libMenuIds || [];
  closeLibMenu();
  if (ids.length) openGroupMenu(ids, $("libMenuMove"));
});
$("libMenuDelete").addEventListener("click", () => {
  if (!libMenuIds || !libMenuIds.length) return;
  pendingBatchIds = [...libMenuIds];
  pendingDeleteBook = null;
  closeLibMenu();
  openDeleteDialog(`删除选中的 ${pendingBatchIds.length} 本书？`);
});
$("libMenuClear").addEventListener("click", () => { closeLibMenu(); clearSelection(); });

function openDeleteDialog(message) {
  /* 复用同一对话框：每次打开先恢复默认按钮文案，部分失败态会被改成「仅重试失败项/关闭」 */
  $("deleteBookConfirm").textContent = "删除";
  $("deleteBookCancel").textContent = "保留";
  $("deleteBookMessage").textContent = message;
  $("deleteBookError").textContent = "";
  $("deleteBookDialog").showModal();
  $("deleteBookCancel").focus();
}
$("bookDeleteBtn").addEventListener("click", () => {
  if (!state.selectedBook) return;
  pendingDeleteBook = state.selectedBook;
  pendingBatchIds = null;
  closeBookMenu(false);
  openDeleteDialog(`删除《${pendingDeleteBook.title}》？`);
});
$("batchDeleteBtn").addEventListener("click", () => {
  if (!state.selected.size) return;
  pendingBatchIds = [...state.selected];
  pendingDeleteBook = null;
  openDeleteDialog(`删除选中的 ${pendingBatchIds.length} 本书？`);
});
$("deleteBookCancel").addEventListener("click", () => $("deleteBookDialog").close());
$("deleteBookConfirm").addEventListener("click", async () => {
  $("deleteBookConfirm").disabled = true;
  try {
    if (pendingBatchIds) {
      const result = await api("/api/books/batch", {
        method: "POST", body: { action: "delete", ids: pendingBatchIds },
      });
      if (result.failures && result.failures.length) {
        /* 部分失败：只保留实际失败项，列出每本原因；成功的不重删 */
        pendingBatchIds = result.failures.map((failure) => failure.id);
        state.selected = new Set(pendingBatchIds);
        const titles = new Map((state.books || []).map((b) => [b.id, b.title]));
        const deleted = Number.isFinite(result.deleted)
          ? result.deleted
          : result.failures.length - pendingBatchIds.length;
        $("deleteBookMessage").textContent =
          `已删除 ${deleted} 本，还有 ${pendingBatchIds.length} 本没删掉`;
        $("deleteBookError").textContent = result.failures
          .map((failure) => `《${titles.get(failure.id) || failure.id}》${failure.error}`)
          .join("；");
        $("deleteBookConfirm").textContent = "仅重试失败项";
        $("deleteBookCancel").textContent = "关闭";
        loadBooks();
        return;
      }
      $("deleteBookDialog").close();
      pendingBatchIds = null;
      state.selected = new Set();
      state.lastPickId = null;
      loadBooks();
      return;
    }
    if (!pendingDeleteBook) return;
    await api(`/api/books/${pendingDeleteBook.id}`, { method: "DELETE" });
    $("deleteBookDialog").close(); pendingDeleteBook = null; loadBooks();
  } catch (err) { $("deleteBookError").textContent = err.message; }
  finally { $("deleteBookConfirm").disabled = false; }
});

$("selectAllBtn").addEventListener("click", () => {
  const books = visibleBooks();
  const all = books.length > 0 && books.every((b) => state.selected.has(b.id));
  if (all) { clearSelection(); return; }
  state.selected = new Set(books.map((b) => b.id));
  renderLibrary($("searchInput").value.trim());
});
$("batchMoveBtn").addEventListener("click", () => {
  if (state.selected.size) openGroupMenu([...state.selected], $("batchMoveBtn"));
});
$("clearSelBtn").addEventListener("click", clearSelection);
$("libBackBtn").addEventListener("click", () => {
  state.filterGroup = null;
  renderLibrary($("searchInput").value.trim());
});
$("newGroupBtn").addEventListener("click", () => {
  $("groupNameInput").value = "";
  $("groupDialogError").textContent = "";
  $("groupDialog").showModal();
  $("groupNameInput").focus();
});
$("groupDialogCancel").addEventListener("click", () => $("groupDialog").close());
$("groupDialogConfirm").addEventListener("click", async () => {
  const name = $("groupNameInput").value.trim();
  if (!name) { $("groupDialogError").textContent = "请输入分组名"; return; }
  $("groupDialogConfirm").disabled = true;
  try {
    await api("/api/groups", { method: "POST", body: { name } });
    $("groupDialog").close();
    loadBooks();
  } catch (err) { $("groupDialogError").textContent = err.message; }
  finally { $("groupDialogConfirm").disabled = false; }
});

/* ------------------------------------------------------------ 框选与右键
   指针拖动区分：落在书籍卡片上 = 拖拽入组（原生 DnD）；
   落在网格空白/间隙 = 橡皮筋框选。 */

(function marqueeSelect() {
  const grid = $("grid");
  const marquee = $("marquee");
  let startX = 0;
  let startY = 0;
  let active = false;

  grid.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const card = event.target.closest(".card");
    if (card && !state.selected.has(card.dataset.bookId)) {
      state.selected = new Set([card.dataset.bookId]);
      state.lastPickId = card.dataset.bookId;
      renderLibrary($("searchInput").value.trim());
    }
    if (!state.selected.size) return;
    openLibMenu([...state.selected], event.clientX, event.clientY);
  });

  grid.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".card")) return;
    active = true;
    startX = event.clientX;
    startY = event.clientY;
    grid.setPointerCapture(event.pointerId);
  });

  grid.addEventListener("pointermove", (event) => {
    if (!active) return;
    const dx = event.clientX - startX;
    const dy = event.clientY - startY;
    if (marquee.hidden && Math.hypot(dx, dy) < 6) return;
    const left = Math.min(startX, event.clientX);
    const top = Math.min(startY, event.clientY);
    const width = Math.abs(dx);
    const height = Math.abs(dy);
    marquee.hidden = false;
    marquee.style.left = `${left}px`;
    marquee.style.top = `${top}px`;
    marquee.style.width = `${width}px`;
    marquee.style.height = `${height}px`;
    const box = { left, right: left + width, top, bottom: top + height };
    visibleBooks().forEach((book) => {
      const el = grid.querySelector(`[data-book-id="${CSS.escape(book.id)}"]`);
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const hit = rect.left < box.right && rect.right > box.left
        && rect.top < box.bottom && rect.bottom > box.top;
      if (hit) state.selected.add(book.id);
    });
    /* 只刷新选中态，不整树重绘 */
    grid.querySelectorAll(".card").forEach((el) => {
      const on = state.selected.has(el.dataset.bookId);
      el.querySelector(".pick").classList.toggle("on", on);
      el.classList.toggle("selected", on);
      el.querySelector(".book-open").setAttribute("aria-pressed", String(on));
    });
    grid.classList.toggle("selecting", state.selected.size > 0);
    syncBatchBar();
  });

  function endMarquee(event) {
    if (!active) return;
    active = false;
    marquee.hidden = true;
    if (grid.hasPointerCapture(event.pointerId)) grid.releasePointerCapture(event.pointerId);
    if (state.selected.size) state.lastPickId = [...state.selected].pop();
  }
  grid.addEventListener("pointerup", endMarquee);
  grid.addEventListener("pointercancel", endMarquee);
})();

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.selected.size
      && !document.querySelector("dialog[open]") && !document.querySelector("[popover]:popover-open")) {
    clearSelection();
  }
});
