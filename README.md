<p align="center">
  <img src="docs/assets/quire.svg" width="88" height="88" alt="Quire 图标">
</p>

<h1 align="center">卷帙 / Quire</h1>

<p align="center">把漫画与小说，收进自己的书库。</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-252525?style=flat-square" alt="平台：macOS">
  <img src="https://img.shields.io/badge/status-v1.0_未发布-e3aa43?style=flat-square" alt="状态：v1.0 未发布">
  <img src="https://img.shields.io/badge/storage-local-397565?style=flat-square" alt="数据本地存储">
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-777777?style=flat-square" alt="许可证：待定"></a>
</p>

<p align="center"><strong>简体中文</strong> · <a href="README.en.md">English</a></p>

## 简介

卷帙是一款正在开发的 macOS 本地桌面工具，用于采集漫画与小说，整理成方便离线阅读的文件。以简洁的图形界面完成采集与导出，书籍和数据保存在自己的电脑上。

## 功能

- **漫画采集**：支持公开网页、动态加载的图片和本地图片整理。
- **多格式导出**：漫画支持 PDF、CBZ、图片 ZIP；小说支持 TXT、EPUB、PDF。
- **阅读整理**：图片压缩、长图切页、缺页占位与中断恢复。
- **本地 OCR**：扫描版小说的图片正文可选本地识别。
- **书库与主题**：本地书库管理，四种界面主题。

## 获取应用

桌面版 1.0 已完成，安装包 `quire-1.0.0.dmg` 将通过 [Releases](https://github.com/b1mango/Quire/releases) 提供（首个 Release 尚未发布）。打开 `.dmg`，把 `quire.app` 拖入「应用程序」即可。

应用未经 Apple 公证：从浏览器下载后首次打开，新版 macOS 会弹出「未打开 quire.app —— Apple 无法验证其是否包含恶意软件」，对话框只有「完成」和「移到废纸篓」（右键「打开」也一样）。任选一种方式放行（只需一次，均已实测）：

- 点「完成」关掉对话框，然后打开「系统设置 → 隐私与安全性」，下拉到「安全性」区域，点 quire 旁边的「仍要打开」，验证后重新打开应用；
- 或在终端执行 `xattr -dr com.apple.quarantine /Applications/quire.app`（装在别的位置就换成对应路径），之后双击即可正常打开。

放行后应用首次启动会自动清除自身的下载隔离标记，内嵌核心随后正常运行。

<a id="license"></a>

## 许可证

许可证待确定，确定后将在仓库中公布。
