/* 任务事件 → 任务模型。每个进行中的任务一条 SSE；事件只改模型，渲染统一节流。
   模型字段沿用服务端快照（id/kind/title/status/phase/done/total/failed_pages/error/hint/book_id/spec），
   另加本地字段：chapters（已落定章节，按 id 去重）、thumbs（漫画缩略图）、result（完成摘要）。 */
"use strict";

const LIVE = new Set(["pending", "running"]);
const ENDED = new Set(["done", "partial", "failed", "cancelled"]);
const EVENT_KINDS = ["phase", "progress", "thumb", "volume", "chapter", "done", "failed", "cancelled", "paused"];
const streams = new Map();

function jobModel(snapshot) {
  const job = state.jobs.get(snapshot.id) || { chapters: new Map(), thumbs: [], result: null, volumes: 0 };
  const { chapters, ...rest } = snapshot;
  Object.assign(job, rest);
  (chapters || []).forEach((c) => job.chapters.set(String(c.id), c));
  state.jobs.set(job.id, job);
  return job;
}

function connectJob(job) {
  if (streams.has(job.id) || !LIVE.has(job.status)) return;
  const source = new EventSource(`/api/jobs/${job.id}/events?token=${encodeURIComponent(TOKEN)}`);
  streams.set(job.id, source);
  EVENT_KINDS.forEach((kind) => source.addEventListener(kind, (e) => {
    onJobEvent(job, kind, JSON.parse(e.data));
    scheduleJobsPaint();
  }));
}

function closeStream(job) {
  const source = streams.get(job.id);
  if (source) { source.close(); streams.delete(job.id); }
}

function onJobEvent(job, kind, data) {
  if (kind === "phase") {
    job.phase = data.phase;
    if (job.status === "pending") job.status = "running";
  } else if (kind === "progress") {
    if (job.status === "pending") job.status = "running";
    job.done = data.done; job.total = data.total; job.failed = data.failed;
  } else if (kind === "thumb") {
    job.thumbs.push(data);
    if (job.thumbs.length > 240) job.thumbs.splice(0, job.thumbs.length - 240);
  } else if (kind === "chapter") {
    job.chapters.set(String(data.id), data);
  } else if (kind === "volume") {
    job.book_id = data.book_id;
    job.volumes = data.done;
  } else {
    const status = kind === "done" ? (data.partial ? "partial" : "done") : kind;
    job.status = status;
    job.result = data;
    if (data.book_id) job.book_id = data.book_id;
    if (data.message) job.error = data.message;
    if (data.hint) job.hint = data.hint;
    if (Number.isFinite(data.failures)) job.failed_pages = data.failures;
    closeStream(job);
    if (typeof updateQueueJob === "function") updateQueueJob(job);
    announceJobEnd(job);
    if (job.book_id) loadBooks();
  }
}

function announceJobEnd(job) {
  const t = `《${job.title}》`;
  if (job.status === "done") toast(`${t}已完成`, job.book_id ? "打开" : "", () => openBook(job.book_id));
  else if (job.status === "partial") toast(`${t}已完成，有缺页`, "查看", () => showJob(job.id));
  else if (job.status === "failed") toast(`${t}没有完成`, "查看", () => showJob(job.id));
}

/* 轮询（queue-status.js）带回的快照：补上不在流里的任务，并为新出现的进行中任务接流 */
function onJobsPolled(jobs) {
  let changed = false;
  jobs.forEach((snapshot) => {
    const known = state.jobs.get(snapshot.id);
    if (known && streams.has(known.id)) return; // 流里的数据更新更及时
    if (known && known.status === snapshot.status && known.done === snapshot.done) return;
    connectJob(jobModel(snapshot));
    changed = true;
  });
  if (changed) scheduleJobsPaint();
}
