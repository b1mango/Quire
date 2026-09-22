/* 队列状态取全部任务；当前详情页结束不代表整个队列结束。 */
"use strict";
const queueStatuses = new Map();
let queueCompleteTimer = null;
let queueRevision = 0;
let queuePolling = false;

function paintQueueStatus() {
  const statuses = [...queueStatuses.values()];
  const active = statuses.some((s) => s === "running" || s === "pending");
  const completed = !active && statuses.length > 0 && statuses.every((s) => s === "done");
  const dot = $("runDot");
  const next = active ? "active" : completed ? "done" : "idle";
  if (dot.dataset.status === next) return;
  clearTimeout(queueCompleteTimer);
  dot.dataset.status = next;
  dot.hidden = next === "idle";
  dot.title = active ? "正在采集" : completed ? "采集完成" : "";
  dot.setAttribute("aria-label", dot.title);
  if (completed) queueCompleteTimer = setTimeout(() => { dot.hidden = true; }, 8000);
}

function updateQueueJob(job) {
  if (!job || !job.id) return;
  queueRevision += 1;
  queueStatuses.set(job.id, job.status);
  paintQueueStatus();
  refreshQueueStatus();
}

async function refreshQueueStatus() {
  if (queuePolling) return;
  queuePolling = true;
  const revision = queueRevision;
  try {
    const data = await api("/api/jobs");
    if (revision !== queueRevision) return;
    queueStatuses.clear();
    (data.jobs || []).forEach((job) => queueStatuses.set(job.id, job.status));
    paintQueueStatus();
  } catch (_) { /* 网络失败时保留最后确认的状态。 */ }
  finally { queuePolling = false; }
}
refreshQueueStatus();
setInterval(refreshQueueStatus, 3000);
