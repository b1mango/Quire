/* 题签式书封（签名元素）：布纹底色 + 竖排纸签书名 + 右侧订线。
   有真实封面（漫画首页等）时直接用图。底色色板与 EPUB 文字封面（assemble/cover.py）一致；
   服务端给出 cover_tint 时以它为准，保证书库与导出文件同色。 */
"use strict";

const CLOTH = ["#2E4A62", "#3D5A4C", "#7A2E2A", "#2B3A55", "#8A5A2B",
               "#4A3B52", "#5B6B3A", "#6E2F3F", "#3A4A4A", "#7B6A4A"];
function clothColor(seed) {
  let h = 2166136261; // FNV-1a：逐码点混合，避免 ×31 在模 10 下退化为码点和
  for (const c of String(seed || "书")) h = Math.imul(h ^ c.codePointAt(0), 16777619) >>> 0;
  return CLOTH[h % CLOTH.length];
}

/* item: {title, author?, cover?, cover_tint?, upd?}；cls 追加到 .cv 上 */
function cover(item, cls = "") {
  const dot = item.upd ? html`<i class="upd"></i>` : "";
  if (item.cover) {
    const src = `${item.cover}${item.cover.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}`;
    return html`<div class="cv img ${cls}"><img src="${src}" alt="" loading="lazy" decoding="async">${dot}</div>`;
  }
  const title = String(item.title || "未命名");
  const latin = /^[\x00-\x7F]+$/.test(title);
  const chars = [...title];
  const size = latin ? "" : `font-size:${chars.length <= 5 ? 12.5 : chars.length <= 7 ? 10 : 8.4}cqw`;
  const shown = latin || chars.length <= 9 ? title : chars.slice(0, 8).join("") + "…";
  const tint = /^#[0-9a-f]{6}$/i.test(item.cover_tint || "") ? item.cover_tint : clothColor(title);
  return html`<div class="cv ${cls}" style="--c:${tint}"><i class="stitch"></i
    ><div class="label${latin ? " latin" : ""}" style="${size}"><span>${shown}</span></div
    ><div class="author${latin ? " latin" : ""}">${item.author || ""}</div>${dot}</div>`;
}
