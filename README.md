# Quire

<p align="center">
  <img src="docs/assets/quire-icon.png" alt="Quire" width="112" height="112" />
</p>

<p align="center">把漫画与小说收进自己的本地书库，整理后离线阅读。</p>

<p align="center">
  <a href="https://github.com/b1mango/Quire/releases/latest"><img src="https://img.shields.io/github/v/release/b1mango/Quire?color=e3aa43" alt="latest release" /></a>
  <img src="https://img.shields.io/badge/platform-macOS-1f6feb" alt="macOS" />
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-777777" alt="license pending" /></a>
</p>

<p align="center"><strong>简体中文</strong> · <a href="README.en.md">English</a></p>

## 功能

- 采集公开网页、动态加载图片和本地图片，整理为漫画或小说；识别常见建站模板（笔趣阁类、苹果 CMS、漫城 CMS、Madara 等）、分页目录与「查看全部章节」。
- 导入本地文件：TXT 自动转码、分章、去广告、修复硬换行并转成 EPUB；EPUB 去广告段；PDF、CBZ、ZIP（含压缩包里的书）与图片文件夹打包成 CBZ，导入前可确认书名作者并同步写入 EPUB 元数据。
- 内置阅读器：小说分页 / 滚动与自定义排版，漫画单页 / 双页 / 条漫连读，图片型 PDF 按漫画阅读。
- 漫画导出 PDF、CBZ 和图片 ZIP；小说导出 TXT、EPUB 和 PDF。
- 图片压缩、长图切页、缺页占位与中断恢复。
- 可选本地 OCR 识别扫描版小说页面。
- 跟随 macOS 系统代理，按站点自动选择代理或直连。
- 本地书库管理，浅色 / 深色与三种强调色。
- 系列按卷、章节数或实际产物大小分卷，完成一卷即可导出。
- 保留原图后可离线重新导出，不会为缺失或损坏的原图联网补抓。

## 安装

从 [Releases](https://github.com/b1mango/Quire/releases/latest) 下载 `quire-<版本>.dmg`，打开后把「quire」拖进「应用程序」。支持 macOS 13 及以上，Intel 与 Apple 芯片通用，内置运行环境，无需另装 Python。

安装包为临时签名、未经苹果公证：首次打开请在「访达」里右键 →「打开」，或在“系统设置 → 隐私与安全性”中选择“仍要打开”；也可以执行 `xattr -dr com.apple.quarantine /Applications/quire.app`。

各版本变化见 [CHANGELOG.md](CHANGELOG.md)。

从源码运行需要 Python 3.12 或更高版本：

```bash
python -m pip install -e '.[core,dev]'
quire ui
```

## 开发

```bash
quire sites new example.com
quire sites test example.com https://example.com/book
quire inspect https://example.com/book --explain --dump-html debug.html
```

<details>
<summary>系列、离线重组与配置命令</summary>

```bash
quire series https://example.com/book --site example.com --split-by volume -o ./书名
quire series https://example.com/book --split-by chapters 20 --from 1 --to 60 -o ./书名
quire series https://example.com/book --split-by size 50MB -o ./书名

quire manga https://example.com/chapter --core --keep-images --workdir ./cache -o book.pdf
quire reassemble TASK_ID --workdir ./cache --compress small --format pdf,cbz -o rebuilt.pdf

quire profile export profile.json
quire profile import profile.json
quire doctor
```

`reassemble` 只使用已保存且校验通过的原图；设置 profile 不携带本机输出目录。按体积分卷以实际产物大小在章节边界切分，单章超过上限时需调低画质或增大上限。大图、多格式和保留原图会增加磁盘占用，请为来源图与成品暂存预留空间。

</details>

## License

<a id="license"></a>

许可证尚未确定，确定后将在仓库中公布。
