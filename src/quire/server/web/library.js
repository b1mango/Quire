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
  renderGroupBar();
  const grid = $("grid");
  grid.textContent = "";
  const books = visibleBooks();
  $("libEmpty").hidden = state.books.length > 0;
  const total = state.books.reduce((sum, b) => sum + b.bytes, 0);
  const group = (state.groups || []).find((g) => g.id === state.filterGroup);
  $("libSub").textContent = search
    ? `“${search}” · ${books.length} 部`
    : group
      ? `${group.name} · ${books.length} 部`
      : `${state.books.length} 部作品 · 共 ${humanSize(total)}`;
  books.forEach((book) => grid.appendChild(bookCard(book)));
  if (state.managing) syncBatchBar();
}

function renderGroupBar() {
  const groups = state.groups || [];
  $("groupBar").hidden = groups.length === 0;
  $("groupAll").setAttribute("aria-pressed", String(!state.filterGroup));
  const wrap = $("groupChips");
  wrap.textContent = "";
  groups.forEach((group) => {
    const chip = document.createElement("button");
    chip.className = "group-chip";
    chip.type = "button";
    chip.setAttribute("aria-pressed", String(state.filterGroup === group.id));
    chip.append(document.createTextNode(`${group.name} ${group.members}`));
    chip.addEventListener("click", () => {
      state.filterGroup = state.filterGroup === group.id ? null : group.id;
      renderLibrary($("searchInput").value.trim());
    });
    /* 拖拽入组：书名卡片拖上分组片即移动 */
    chip.addEventListener("dragover", (e) => { e.preventDefault(); chip.classList.add("over"); });
    chip.addEventListener("dragleave", () => chip.classList.remove("over"));
    chip.addEventListener("drop", (e) => {
      e.preventDefault();
      chip.classList.remove("over");
      const id = e.dataTransfer.getData("text/plain");
      if (id) moveBooks([id], group.id);
    });
    if (!group.members) {
      const remove = document.createElement("span");
      remove.className = "gx";
      remove.textContent = "×";
      remove.title = `删除空分组「${group.name}」`;
      remove.addEventListener("click", (e) => {
        e.stopPropagation();
        api(`/api/groups/${group.id}`, { method: "DELETE" })
          .then(loadBooks)
          .catch((err) => { $("libSub").textContent = err.message; });
      });
      chip.append(remove);
    }
    wrap.appendChild(chip);
  });
}

function moveBooks(ids, groupId) {
  api("/api/books/batch", { method: "POST", body: { action: "move", ids, group_id: groupId } })
    .then(() => {
      if (state.managing) { state.selected = new Set(); }
      loadBooks();
    })
    .catch((err) => { $("libSub").textContent = err.message; });
}

let groupMenuIds = [];
function openGroupMenu(ids, anchor) {
  groupMenuIds = ids;
  const menu = $("groupMenu");
  menu.querySelectorAll("button").forEach((b) => b.remove());
  (state.groups || []).forEach((group) => {
    const item = document.createElement("button");
    item.className = "btn";
    item.textContent = group.name;
    item.addEventListener("click", () => { menu.hidePopover(); moveBooks(groupMenuIds, group.id); });
    menu.appendChild(item);
  });
  const out = document.createElement("button");
  out.className = "btn";
  out.textContent = "移出分组";
  out.addEventListener("click", () => { menu.hidePopover(); moveBooks(groupMenuIds, null); });
  menu.appendChild(out);
  menu.showPopover();
  const rect = anchor.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(rect.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 6, innerHeight - menu.offsetHeight - 8))}px`;
}

function setManaging(on) {
  state.managing = on;
  state.selected = new Set();
  $("batchBar").hidden = !on;
  $("manageBtn").setAttribute("aria-pressed", String(on));
  renderLibrary($("searchInput").value.trim());
}

function syncBatchBar() {
  $("batchMeta").textContent = `已选 ${state.selected.size} 本`;
  const books = visibleBooks();
  const all = books.length > 0 && books.every((b) => state.selected.has(b.id));
  $("selectAllBtn").textContent = all ? "全不选" : "全选";
  $("batchMoveBtn").disabled = !state.selected.size;
  $("batchDeleteBtn").disabled = !state.selected.size;
}

function togglePick(id, dot) {
  if (state.selected.has(id)) { state.selected.delete(id); dot.classList.remove("on"); }
  else { state.selected.add(id); dot.classList.add("on"); }
  syncBatchBar();
}

/* ------------------------------------------------------------ 书籍卡片 */

function bookCard(book) {
  const card = document.createElement("article");
  card.className = "card";
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
  meta.textContent = `${book.formats.join(" / ").toUpperCase()} · ${humanSize(book.bytes)}`;
  if (state.managing) {
    const dot = document.createElement("span");
    dot.className = "pick" + (state.selected.has(book.id) ? " on" : "");
    cover.append(dot);
    open.setAttribute("aria-label", `选择《${book.title}》`);
    open.addEventListener("click", () => togglePick(book.id, dot));
  } else {
    open.addEventListener("click", () => openLibraryBook(book));
    card.draggable = true;
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", book.id);
      e.dataTransfer.effectAllowed = "move";
    });
    const more = document.createElement("button"); more.type = "button"; more.className = "book-more";
    more.setAttribute("aria-label", `《${book.title}》的更多操作`);
    more.setAttribute("aria-expanded", "false"); more.setAttribute("aria-controls", "bookActions");
    more.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';
    more.addEventListener("click", () => selectBook(book, more));
    footer.append(meta, more);
  }
  card.append(open, footer); return card;
}
function selectBook(book, trigger) {
  const menu = $("bookActions");
  const same = bookMenuTrigger === trigger && menu.matches(":popover-open");
  closeBookMenu(false); if (same) return;
  state.selectedBook = book; bookMenuTrigger = trigger;
  $("bookActionsTitle").textContent = book.title;
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
  const ids = [state.selectedBook.id];
  const anchor = bookMenuTrigger;
  closeBookMenu(false);
  openGroupMenu(ids, anchor);
});

let pendingDeleteBook = null;
let pendingBatchIds = null;
$("bookDeleteBtn").addEventListener("click", () => {
  if (!state.selectedBook) return;
  pendingDeleteBook = state.selectedBook;
  pendingBatchIds = null;
  closeBookMenu(false);
  $("deleteBookMessage").textContent = `删除《${pendingDeleteBook.title}》？`;
  $("deleteBookError").textContent = "";
  $("deleteBookDialog").showModal();
  $("deleteBookCancel").focus();
});
$("batchDeleteBtn").addEventListener("click", () => {
  if (!state.selected.size) return;
  pendingBatchIds = [...state.selected];
  pendingDeleteBook = null;
  $("deleteBookMessage").textContent = `删除选中的 ${pendingBatchIds.length} 本书？`;
  $("deleteBookError").textContent = "";
  $("deleteBookDialog").showModal();
  $("deleteBookCancel").focus();
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
        $("deleteBookError").textContent =
          `${result.failures.length} 本没删掉：${result.failures[0].error}`;
        pendingBatchIds = null;
        loadBooks();
        return;
      }
      $("deleteBookDialog").close();
      pendingBatchIds = null;
      setManaging(false);
      loadBooks();
      return;
    }
    if (!pendingDeleteBook) return;
    await api(`/api/books/${pendingDeleteBook.id}`, { method: "DELETE" });
    $("deleteBookDialog").close(); pendingDeleteBook = null; loadBooks();
  } catch (err) { $("deleteBookError").textContent = err.message; }
  finally { $("deleteBookConfirm").disabled = false; }
});

$("manageBtn").addEventListener("click", () => setManaging(!state.managing));
$("manageDoneBtn").addEventListener("click", () => setManaging(false));
$("selectAllBtn").addEventListener("click", () => {
  const books = visibleBooks();
  const all = books.length > 0 && books.every((b) => state.selected.has(b.id));
  state.selected = all ? new Set() : new Set(books.map((b) => b.id));
  renderLibrary($("searchInput").value.trim());
});
$("batchMoveBtn").addEventListener("click", () => {
  if (state.selected.size) openGroupMenu([...state.selected], $("batchMoveBtn"));
});
$("groupAll").addEventListener("click", () => {
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
