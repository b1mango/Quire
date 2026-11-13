/* 图片文件夹 → CBZ：拖进窗口的文件夹（或「文件夹」按钮选的）在浏览器里打成不压缩的 ZIP——图片本身已压缩，
   只算 CRC，Blob 直接引用原文件不复制数据——命名「文件夹名.cbz」，再走 import.js 的预览确认流程。
   子文件夹按路径自然序（第1话/第2话…）；文件夹里的 TXT/EPUB/PDF/CBZ/ZIP 另外单独导入。依赖 import.js。 */
"use strict";

const IMAGE_FILE = /\.(jpe?g|png|webp|gif|avif|bmp)$/i;
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

async function crc32(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/* 不压缩（STORE）的 ZIP；文件名 UTF-8（标志位 0x0800）。不支持 ZIP64：总量须 < 4 GiB、条目 < 65535 */
async function packCbz(entries, onStep) {
  const total = entries.reduce((n, e) => n + e.file.size, 0);
  if (entries.length >= 65535 || total >= 0xffffffff - entries.length * 200) throw new Error("文件夹太大，单个漫画包须小于 4 GB");
  const enc = new TextEncoder();
  const d = new Date();
  const time = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
  const date = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
  const body = [], central = [];
  let offset = 0, centralSize = 0;
  for (const [i, { path, file }] of entries.entries()) {
    const name = enc.encode(path), crc = await crc32(file), size = file.size;
    const local = new DataView(new ArrayBuffer(30));
    [[0, 0x04034b50, 4], [4, 20, 2], [6, 0x0800, 2], [10, time, 2], [12, date, 2], [14, crc, 4], [18, size, 4], [22, size, 4], [26, name.length, 2]]
      .forEach(([at, v, n]) => (n === 4 ? local.setUint32(at, v, true) : local.setUint16(at, v, true)));
    body.push(local, name, file);
    const dir = new DataView(new ArrayBuffer(46));
    [[0, 0x02014b50, 4], [4, 20, 2], [6, 20, 2], [8, 0x0800, 2], [12, time, 2], [14, date, 2], [16, crc, 4], [20, size, 4], [24, size, 4], [28, name.length, 2], [42, offset, 4]]
      .forEach(([at, v, n]) => (n === 4 ? dir.setUint32(at, v, true) : dir.setUint16(at, v, true)));
    central.push(dir, name);
    offset += 30 + name.length + size;
    centralSize += 46 + name.length;
    onStep(i + 1, entries.length);
  }
  const end = new DataView(new ArrayBuffer(22));
  [[0, 0x06054b50, 4], [8, entries.length, 2], [10, entries.length, 2], [12, centralSize, 4], [16, offset, 4]]
    .forEach(([at, v, n]) => (n === 4 ? end.setUint32(at, v, true) : end.setUint16(at, v, true)));
  return new Blob([...body, ...central, end], { type: "application/zip" });
}

const naturalPath = (a, b) => a.path.localeCompare(b.path, "zh", { numeric: true });
const hiddenPath = (path) => path.split("/").some((part) => part.startsWith(".") || part === "__MACOSX");

/* folders: Map<文件夹名, [{path: 文件夹内相对路径, file}]> */
async function importFolders(folders) {
  const loose = [];
  for (const [folder, all] of folders) {
    const entries = all.filter((e) => !hiddenPath(e.path));
    loose.push(...entries.filter((e) => IMPORT_EXT.test(e.path)).map((e) => e.file));
    const images = folder === LOOSE ? [] : entries.filter((e) => IMAGE_FILE.test(e.path)).sort(naturalPath);
    if (!images.length) continue;
    const item = { id: ++imports.seq, name: `${folder}.cbz`, size: 0, state: "packing", progress: 0, error: "" };
    imports.items.push(item);
    paintImports();
    try {
      const blob = await packCbz(images, (done, total) => { item.progress = done / total; paintImports(); });
      Object.assign(item, { file: new File([blob], item.name), size: blob.size, state: "queued", progress: 0 });
    } catch (err) {
      Object.assign(item, { state: "failed", error: err.message || "文件夹打包失败" });
    }
    paintImports();
    pumpImports();
  }
  if (loose.length) importFiles(loose);
  else if (![...folders.values()].some((all) => all.some((e) => IMAGE_FILE.test(e.path)))) toast("文件夹里没有图片或可导入的书文件");
}

/* 拖入：webkitGetAsEntry 必须在 drop 事件里同步取，之后再异步遍历 */
function readFolder(entry, prefix = "") {
  if (entry.isFile) return new Promise((ok) => entry.file((file) => ok([{ path: prefix + file.name, file }]), () => ok([])));
  const reader = entry.createReader();
  return new Promise((ok) => {
    const all = [];
    const next = () => reader.readEntries(async (batch) => {
      if (!batch.length) {
        const nested = await Promise.all(all.map((e) => readFolder(e, `${prefix}${e.isDirectory ? `${e.name}/` : ""}`)));
        ok(nested.flat());
        return;
      }
      all.push(...batch);
      next(); // readEntries 每次最多给 100 个
    }, () => ok([]));
    next();
  });
}

const LOOSE = "\u0000"; // 和文件夹一起拖进来的零散文件：只导入书文件，不打包
function droppedFolders(dataTransfer) {
  const entries = [...(dataTransfer.items || [])].map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
  if (!entries.some((e) => e.isDirectory)) return null;
  return Promise.all(entries.map((e) => readFolder(e).then((files) => [e.isDirectory ? e.name : LOOSE, files])))
    .then((pairs) => pairs.reduce((map, [key, files]) => map.set(key, [...(map.get(key) || []), ...files]), new Map()));
}

$("importDirBtn").addEventListener("click", () => $("importDir").click());
$("importDir").addEventListener("change", (e) => {
  const folders = new Map();
  [...e.target.files].forEach((file) => {
    const [top, ...rest] = (file.webkitRelativePath || file.name).split("/");
    if (!folders.has(top)) folders.set(top, []);
    folders.get(top).push({ path: rest.join("/") || file.name, file });
  });
  e.target.value = "";
  if (folders.size) importFolders(folders);
});
