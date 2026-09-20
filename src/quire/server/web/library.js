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
  open.append(cover, title); open.addEventListener("click", () => openLibraryBook(book));
  const footer = document.createElement("div"); footer.className = "book-footer";
  const meta = document.createElement("div"); meta.className = "cm";
  meta.textContent = `${book.formats.join(" / ").toUpperCase()} · ${humanSize(book.bytes)}`;
  const more = document.createElement("button"); more.type = "button"; more.className = "book-more";
  more.setAttribute("aria-label", `《${book.title}》的更多操作`);
  more.setAttribute("aria-expanded", "false"); more.setAttribute("aria-controls", "bookActions");
  more.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';
  more.addEventListener("click", () => selectBook(book, more));
  footer.append(meta, more); card.append(open, footer); return card;
}
function selectBook(book, trigger) {
  const menu = $("bookActions");
  const same = bookMenuTrigger === trigger && menu.matches(":popover-open");
  closeBookMenu(false); if (same) return;
  state.selectedBook = book; bookMenuTrigger = trigger;
  $("bookActionsTitle").textContent = book.title;
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

let pendingDeleteBook = null;
$("bookDeleteBtn").addEventListener("click", () => {
  if (!state.selectedBook) return;
  pendingDeleteBook = state.selectedBook;
  closeBookMenu(false);
  $("deleteBookMessage").textContent = `删除《${pendingDeleteBook.title}》？`;
  $("deleteBookError").textContent = "";
  $("deleteBookDialog").showModal();
  $("deleteBookCancel").focus();
});
$("deleteBookCancel").addEventListener("click", () => $("deleteBookDialog").close());
$("deleteBookConfirm").addEventListener("click", async () => {
  if (!pendingDeleteBook) return;
  $("deleteBookConfirm").disabled = true;
  try {
    await api(`/api/books/${pendingDeleteBook.id}`, { method: "DELETE" });
    $("deleteBookDialog").close(); pendingDeleteBook = null; loadBooks();
  } catch (err) { $("deleteBookError").textContent = err.message; }
  finally { $("deleteBookConfirm").disabled = false; }
});
