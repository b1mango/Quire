/* 任务：真正的队列（进行中 / 最近）+ 详情 + 侧栏迷你卡。依赖 app.js / covers.js / run-events.js。 */
"use strict";

const STATUS_TEXT = { pending: "排队中", running: "进行中", paused: "已暂停", done: "完成",
                      partial: "完成 · 有缺页", failed: "未完成", cancelled: "已取消" };
const hiddenJobs = new Set(JSON.parse(localStorage.getItem("quire-hidden-jobs") || "[]"));
let paintPending = false;

function unitFor(job) { return job.kind === "manga" ? "页" : "章"; }
function visibleJobs() {
  return [...state.jobs.values()].filter((j) => !hiddenJobs.has(j.id))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
}
function activeJobs() { return visibleJobs().filter((j) => LIVE.has(j.status) || j.status === "paused"); }

function loadJobs() {
  return api("/api/jobs").then((data) => {
    (data.jobs || []).forEach((s) => connectJob(jobModel(s)));
    const live = visibleJobs().find((j) => LIVE.has(j.status));
    if (live && !state.job) state.job = live;
    scheduleJobsPaint();
  }).catch(() => {});
}

/* 新提交 / 续抓的任务进入队列；focus=true 时成为任务页当前项 */
function trackJob(snapshot, focus) {
  const job = jobModel(snapshot);
  hiddenJobs.delete(job.id);
  connectJob(job);
  if (focus || !state.job) state.job = job;
  if (typeof updateQueueJob === "function") updateQueueJob(job);
  scheduleJobsPaint();
  return job;
}

function showJob(id) {
  state.job = state.jobs.get(id) || state.job;
  go("tasks");
}

function scheduleJobsPaint() {
  if (paintPending) return;
  paintPending = true;
  requestAnimationFrame(() => {
    paintPending = false;
    paintJobsChrome();
    if (state.view === "tasks") renderTasks();
  });
}

/* ------------------------------------------------------------ 侧栏：徽标与迷你卡 */

function paintJobsChrome() {
  const live = visibleJobs().filter((j) => LIVE.has(j.status));
  $("taskBadge").hidden = !live.length;
  $("taskBadge").textContent = String(live.length);
  const run = live.find((j) => j.status === "running") || live[0];
  $("miniJob").hidden = !run;
  if (!run) return;
  const p = run.total ? (run.done / run.total) * 100 : 0;
  $("miniJob").innerHTML = html`${cover({ title: run.title })}<div class="txt" style="flex:1;min-width:0">
    <div class="t">${run.title}</div><div class="s num">${run.status === "pending" ? "排队中" : run.total ? `${run.done} / ${run.total} ${unitFor(run)}` : "准备中"}</div>
    <div class="bar"><i style="width:${p.toFixed(1)}%"></i></div></div>`;
  $("miniJob").setAttribute("aria-label", `正在采集《${run.title}》，查看任务`);
}

/* ------------------------------------------------------------ 列表 */

function jobLine(j) {
  if (j.status === "running") return j.total ? html`<span class="num">${j.done}/${j.total} ${unitFor(j)}</span>` : "准备中";
  if (j.status === "partial") return html`<span class="warn">完成 · 缺 ${j.failed_pages || 0} 页</span>`;
  if (j.status === "done") return html`<span class="ok">完成</span>${j.result && j.result.bytes ? ` · ${humanSize(j.result.bytes)}` : ""}`;
  if (j.status === "failed") return html`<span style="color:var(--danger)">未完成</span>`;
  return STATUS_TEXT[j.status] || j.status;
}

function renderTasks() {
  const jobs = visibleJobs();
  const act = jobs.filter((j) => LIVE.has(j.status) || j.status === "paused");
  const fin = jobs.filter((j) => ENDED.has(j.status));
  $("taskSub").textContent = act.length ? `${act.length} 个进行中` : "";
  $("clearDone").hidden = !fin.length;
  if (!jobs.length) {
    state.job = null;
    $("tasksWrap").style.gridTemplateColumns = "1fr";
    $("tlist").hidden = true;
    $("tdetail").innerHTML = html`<div class="empty"><div><div class="ghost-books" style="justify-content:center"><i></i><i></i><i></i></div>
      <h3>没有任务</h3><p>采集开始后，进度会显示在这里</p><button class="btn primary" data-go="capture">去采集</button></div></div>`;
    return;
  }
  $("tasksWrap").style.gridTemplateColumns = "";
  $("tlist").hidden = false;
  if (!state.job || hiddenJobs.has(state.job.id) || !state.jobs.has(state.job.id)) state.job = act[0] || jobs[0];
  const item = (j) => html`<button class="titem" role="listitem" data-job="${j.id}" aria-current="${String(j === state.job)}">${cover({ title: j.title })}
    <div style="min-width:0"><div class="t">${j.title}</div><div class="s">${jobLine(j)}</div>
    ${j.status === "running" && j.total ? html`<div class="bar"><i style="width:${((j.done / j.total) * 100).toFixed(1)}%"></i></div>` : ""}</div></button>`;
  $("tlist").innerHTML = html`${act.length ? html`<div class="tgroup">进行中</div>${act.map(item)}` : ""}
    ${fin.length ? html`<div class="tgroup">最近</div>${fin.map(item)}` : ""}`;
  renderJobDetail(state.job);
}

/* ------------------------------------------------------------ 详情 */

function stageText(j) {
  const unit = unitFor(j);
  if (j.status === "pending") return "排队中，前面的任务完成后开始";
  if (j.status === "running") {
    if (j.phase === "encoding") return "正在压缩与合成…";
    return j.total ? `正在下载 · 已完成 ${j.done} / ${j.total} ${unit}` : "正在读取目录…";
  }
  if (j.status === "paused") return "已暂停，已抓取的部分保留在缓存里";
  if (j.status === "done") {
    const r = j.result || {};
    return `已完成${r.bytes ? ` · ${humanSize(r.bytes)}` : ""}${r.elapsed_s != null ? ` · 用时 ${Math.round(r.elapsed_s)} 秒` : ""}`;
  }
  if (j.status === "partial") {
    const cancelled = ((j.result && j.result.warnings) || []).some((w) => String(w).includes("已取消"));
    return cancelled ? "已取消：已抓到的部分已导出，重试会从缓存续抓" : "已完成，但有缺页";
  }
  if (j.status === "cancelled") return "已取消，已下载的部分保留在缓存里";
  return "没能完成";
}

function jobActions(j) {
  const b = (act, label, cls = "") => html`<button class="btn ${cls}" data-ja="${act}">${label}</button>`;
  if (j.status === "running") return [b("pause", j.pausing ? "暂停中…" : "暂停"), b("cancel", "取消", "danger")];
  if (j.status === "pending") return b("cancel", "移出队列", "danger");
  if (j.status === "paused") return [b("resubmit", "继续", "primary"), b("hide", "移除")];
  if (j.status === "done") return [j.book_id ? b("open", "打开", "primary") : "", j.book_id ? b("lib", "在书库中查看") : ""];
  if (j.status === "partial") return [j.book_id ? b("open", "打开") : "", b("resubmit", "补抓缺页", "primary")];
  return [b("resubmit", "重试", "primary"), b("hide", "移除")];
}

function chapterRows(j) {
  const rows = [...j.chapters.values()].reverse();
  const pending = j.kind === "novel" && j.total ? Math.max(0, j.total - j.done) : 0;
  return html`${rows.slice(0, 400).map((c) => html`<div class="ch ${c.status === "failed" ? "miss" : c.reused ? "reused done" : "done"}">
      <span class="st"><i></i></span><span class="t">${c.title}</span>
      <span class="x num">${c.status === "failed" ? `缺 ${c.failed_pages}/${c.pages} 页` : j.kind === "manga" ? `${c.pages} 页` : ""}${c.reused ? " · 缓存" : ""}</span></div>`)}
    ${rows.length > 400 ? html`<div class="ch"><span class="st"><i></i></span><span style="color:var(--text-3)">还有 ${rows.length - 400} 章已完成</span></div>` : ""}
    ${LIVE.has(j.status) && pending ? html`<div class="ch"><span class="st"><i></i></span><span style="color:var(--text-3)">还有 ${pending} 章等待下载</span></div>` : ""}`;
}

function renderJobDetail(j) {
  const unit = unitFor(j);
  const p = j.total ? Math.min(1, j.done / j.total) : ENDED.has(j.status) && j.status !== "failed" ? 1 : 0;
  const missing = j.failed_pages || j.failed || 0;
  const fmts = ((j.spec && j.spec.formats) || []).map((f) => f.toUpperCase()).join(" + ");
  const showProgress = j.status !== "failed" && j.status !== "cancelled";
  const failure = readableError(j.error);
  const thumbs = j.kind === "manga" && j.thumbs.length ? html`<div class="td-sec"><h4><span>页面</span><span class="num">${j.thumbs.length}</span></h4>
    <div class="pages">${j.thumbs.slice(-120).map((t) => html`<div class="pg img"><img src="${t.url}?token=${encodeURIComponent(TOKEN)}" alt="第 ${t.page} 页" loading="lazy"></div>`)}
    ${LIVE.has(j.status) ? html`<div class="pg skel"><i></i><div class="r"><i></i><i></i></div><i></i></div>` : ""}</div></div>` : "";
  const chapters = j.chapters.size || (LIVE.has(j.status) && j.total) ? html`<div class="td-sec"><h4><span>章节</span><span class="num">${j.chapters.size}${j.kind === "novel" && j.total ? ` / ${j.total}` : ""}</span></h4><div class="chs">${chapterRows(j)}</div></div>` : "";
  $("tdetail").innerHTML = html`<div class="td-head">${cover({ title: j.title })}<div style="min-width:0"><h2>${j.title}</h2>
      <div class="stage" aria-live="polite">${stageText(j)}</div></div><div class="acts">${jobActions(j)}</div></div>
    ${showProgress ? html`<div class="td-prog"><div class="bar" role="progressbar" aria-label="进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p * 100)}"><i style="width:${(p * 100).toFixed(1)}%"></i></div></div>
      <div class="td-stats num"><div>已完成<b>${j.done || 0}</b></div><div class="${missing ? "warn" : ""}">${j.kind === "manga" ? "缺页" : "失败"}<b>${missing}</b></div>
      <div>总数<b>${j.total || "—"}</b></div><div>格式<b style="font-size:15px;line-height:1.6">${fmts || "—"}</b></div></div>` : ""}
    ${j.status === "partial" ? html`<div class="done-card"><div class="txt">缺 ${missing} 页已用占位页补齐<small>补抓只会重新下载缺失的部分</small></div></div>` : ""}
    ${j.status === "failed" ? html`<div class="td-sec"><div class="err"><svg class="i" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 5v3.5M8 11h0"/></svg>
      <div><b>${failure.title}</b><p>${j.hint || "可以直接重试；反复失败时检查链接或网络设置。"}</p>
      ${failure.detail ? html`<p class="num" style="font-size:var(--fs-12);color:var(--text-3)">原始信息：${failure.detail}</p>` : ""}
      ${/403|登录|拒绝|浏览器/.test(`${j.error}${j.hint}`) ? html`<button class="btn" data-go="settings">打开网络设置</button>` : ""}</div></div></div>` : ""}
    ${thumbs}${chapters}`;
}

/* ------------------------------------------------------------ 操作 */

function resubmit(job) {
  if (!job.spec) return;
  api("/api/jobs", { method: "POST", body: job.spec }).then((snapshot) => {
    hideJob(job.id);
    trackJob(snapshot, true);
  }).catch((err) => toast(errText(err)));
}

function hideJob(id) {
  hiddenJobs.add(id);
  localStorage.setItem("quire-hidden-jobs", JSON.stringify([...hiddenJobs].slice(-300)));
  scheduleJobsPaint();
}

$("tdetail").addEventListener("click", (e) => {
  const a = e.target.closest("[data-ja]");
  const j = state.job;
  if (!a || !j) return;
  const act = a.dataset.ja;
  if (act === "pause") {
    j.pausing = true; renderJobDetail(j); // 协作式暂停要几秒，先给反馈
    api(`/api/jobs/${j.id}/pause`, { method: "POST" }).catch((err) => { j.pausing = false; toast(errText(err)); scheduleJobsPaint(); });
  } else if (act === "cancel") {
    api(`/api/jobs/${j.id}/cancel`, { method: "POST" }).catch((err) => toast(errText(err)));
  } else if (act === "resubmit") resubmit(j);
  else if (act === "hide") hideJob(j.id);
  else if (act === "open") openBook(j.book_id);
  else if (act === "lib") revealInLibrary(j.book_id);
});
$("tlist").addEventListener("click", (e) => {
  const t = e.target.closest("[data-job]");
  if (t) { state.job = state.jobs.get(t.dataset.job); renderTasks(); ensureJobDetail(state.job); }
});
$("clearDone").addEventListener("click", () => {
  visibleJobs().filter((j) => ENDED.has(j.status)).forEach((j) => hiddenJobs.add(j.id));
  hideJob("__flush__");
});
/* 列表快照不带章节；查看已结束的任务时补取一次详情 */
function ensureJobDetail(j) {
  if (!j || j.detailLoaded || LIVE.has(j.status)) return;
  j.detailLoaded = true;
  api(`/api/jobs/${j.id}`).then((s) => { jobModel(s); scheduleJobsPaint(); }).catch(() => { j.detailLoaded = false; });
}
VIEW_HOOKS.tasks = () => { renderTasks(); ensureJobDetail(state.job); };
