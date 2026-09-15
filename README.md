# 卷帙 / Quire

本地漫画与小说采集工具，当前交付 **0.0.1 / M0 micro**。现阶段可从公开静态网页收集图片，或读取本地图片目录，按页序生成 PDF。安装包、小说和界面仍在后续里程碑中。

## 运行

需要 **Python 3.12 或更新版本**。单文件 `dist/quire.pyz` 不包含 Python，也不需要第三方运行时依赖。它不是最终的 `.app` / `.dmg`。

在项目目录中：

```sh
.venv/bin/python dist/quire.pyz doctor
.venv/bin/python dist/quire.pyz manga 'https://example.org/chapter' --selector '.reader' -o output/book.pdf
.venv/bin/python dist/quire.pyz local /path/to/images -o output/local.pdf
```

`example.org/chapter` 只是命令示例，验证使用下方自带的本地测试站。

```sh
.venv/bin/python scripts/smoke_micro.py
```

生成 `output/smoke/quire-sample.pdf`，共 3 页，其中第 2 页故意缺失，以验证占位页和退出码 4。

## 当前行为

- 支持 `img`、常见懒加载属性、`srcset`、无媒体条件的 `picture/source`、内联背景图和图片直链。同一图片的多个地址按备用顺序尝试，避免重复页。
- `--selector` 限定内容区域。支持标签、类、ID、属性、后代和直接子代选择器；不支持伪类及兄弟选择器。
- 默认保留 DOM 页序。需要按文件名排序时使用 `--order asc` 或 `--order desc`。
- 默认 4 个下载任务、每主机 4 次请求/秒。网络错误及部分 5xx/429 最多重试 3 次；遵守 `Retry-After`，超过 120 秒则返回错误供稍后重试。
- 每站查询并缓存 `robots.txt`；404/410 视作无规则，明确禁止的请求不发送，策略查询失败会中止。无登录、Cookie 导入或绕过访问限制。
- 自动使用来源页面作为 Referer，`--referer` 可覆盖。单个响应默认限制 32 MiB，gzip/deflate 解压后也受限制。
- JPEG 及支持的 PNG 写入 PDF；透明 PNG 合成白底。WebP、GIF、AVIF、隔行 PNG 等暂不转码，作为缺页记录，不静默丢页。
- 默认删除该任务的临时图片，`--keep-images` 保留在输出旁的 `.quire-work/task-*`。本地输入图片始终保留。
- 成品先写临时文件，完成后原子发布。CLI 默认遇到同名成品另存 `(1)`；`--overwrite` 显式覆盖。取消或合成失败保留既有成品。
- 每个成品附带 `.report.json`，记录有效页、缺页和过滤原因；报告及错误信息隐藏 URL 查询参数。`inspect --dump-html` 属于显式保存原网页。

退出码：`0` 完整成功，`1` 参数错误，`2` 网络/文件操作失败，`3` 没找到内容，`4` 已生成但存在占位页，`5` 站点拒绝，`6` 缺少能力。当前不支持断点恢复，取消后需重新运行。

## 开发与验证

```sh
uv sync --locked --extra dev --python 3.12
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/quire
.venv/bin/pytest --cov=quire --cov-report=json:output/coverage.json
.venv/bin/python scripts/check_coverage.py output/coverage.json
.venv/bin/python scripts/build_micro_zipapp.py
.venv/bin/python scripts/smoke_micro.py
swift scripts/render_pdf.swift output/smoke/quire-sample.pdf output/smoke/page
```

`Pillow`、`pypdf`、`lxml` 仅用于开发验证，不进入 micro 包。`uv.lock` 固定开发依赖，CI 工作流已提供，本地工作区尚无 Git 远程，因此未在 GitHub 运行。

2026-09-15 本地 200 页测试：默认 4 req/s、4 并发，**51.0 秒**、子进程峰值 RSS **37.3 MiB**、成品 **2.41 MiB**、缺页 **0**。样本为 400px 左右宽的合成 JPEG，运行于回环 HTTP，不能推算真实漫画画质、目标压缩效果或公网成功率。复现：`.venv/bin/python scripts/benchmark_micro.py`。

## 下一阶段

M1：SQLite 断点账本、连接池下载、Pillow 转码与体积控制、PDF/CBZ/图片 ZIP 同轮输出。最终产品默认目标 50 MB，可由用户调整；当前 micro 保留原编码，**尚不实施 50 MB 压缩上限**。

后续按 [项目设计](DESIGN.md) 实现系统 Chrome 动态页面、小说 TXT/EPUB/PDF、可选本地 OCR、简单书库、四主题 Web UI 和 Swift 本地应用壳。四主题视觉预览仍在 [ui-preview/index.html](ui-preview/index.html)，尚未连接抓取器。
