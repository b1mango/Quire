"use strict";

const COMPRESS_DESC = {
  lossless: "保留画质；色彩归一化、长图切分可能改变文件",
  archive: "JPEG 质量 95 · 保留尺寸；仍会重新编码，并非无损原图",
  high: "高画质，体积偏大",
  balanced: "JPEG 质量 80 · 最长边 2000px，日常阅读推荐",
  small: "JPEG 质量 70 · 最长边 1600px，优先节省空间",
  tiny: "极限体积，画质损失明显",
};
function syncCompressDesc(seg, descId) {
  const desc = $(descId);
  if (desc) desc.textContent = COMPRESS_DESC[segValue(seg, "compress")] || "";
  if (seg.id === "compressSeg") updateSizeEstimate();
}

function onJobEvent(kind, data) {
  if (kind === "phase") {
    $("runSub").textContent = { fetching: "正在抓取页面……", encoding: "正在压缩与合成……" }[data.phase] || data.phase;
  } else if (kind === "progress") {
    $("statDone").textContent = data.done;
    $("statFailed").textContent = data.failed;
    $("statTotal").textContent = data.total;
    if (data.total) $("meterFill").style.width = `${((data.done + data.failed) / data.total) * 100}%`;
  } else if (kind === "volume") {
    state.bookId = data.book_id;
    $("runSub").textContent = `已交付 ${data.done} 卷 · ${data.title}`;
    $("runDoneActions").hidden = false;
  } else if (kind === "thumb") {
    $("runEmpty").hidden = true;
    const tile = document.createElement("div");
    tile.className = "thumb";
    const img = document.createElement("img");
    img.src = `${data.url}?token=${encodeURIComponent(TOKEN)}`;
    img.alt = "";
    const pg = document.createElement("span");
    pg.className = "pg";
    pg.textContent = data.page;
    tile.append(img, pg);
    thumbnailGroup(data.chapter_id).appendChild(tile);
  } else if (kind === "chapter") {
    appendChapters([data]);
  } else if (kind === "done") {
    finishRunView(data.partial ? "partial" : "done", data);
  } else if (kind === "failed" || kind === "cancelled" || kind === "paused") {
    finishRunView(kind, data);
  }
}


function resetRunView(job) {
  $("runTitle").textContent = job.title || "准备中";
  $("runSub").textContent = "正在连接……";
  $("stream").textContent = "";
  resetChapters();
  $("runEmpty").hidden = false;
  $("partialNote").hidden = true;
  $("failedNote").hidden = true;
  $("runDoneActions").hidden = true;
  $("retryBtn").hidden = true;
  $("resumeBtn").hidden = true;
  $("cancelBtn").hidden = false;
  $("pauseBtn").hidden = false;
  $("pauseBtn").disabled = false;
  $("pauseBtn").textContent = "暂停";
  $("meterFill").style.width = "0";
  $("statDone").textContent = "0";
  $("statFailed").textContent = "0";
  $("statTotal").textContent = "—";
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    $("statElapsed").textContent = ((Date.now() - state.startedAt) / 1000).toFixed(1) + " s";
  }, 200);
}

