# 卷帙 / Quire

本地漫画与小说采集工具，当前版本 **0.0.1**。公开静态网页或本地图片目录可按页序生成 PDF；core 提供连接池和经校验的断点恢复。安装包、小说和界面仍在后续里程碑中。

[项目设计](项目设计.md) · [项目进度](项目进度.md) · [对话记录与续接](对话记录.md) · [测试站点及命令](测试站点.md)

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

### Core 下载与恢复

```sh
uv sync --locked --extra core --python 3.12
.venv/bin/python -m quire manga 'https://example.org/chapter' --core --selector '.reader' -o output/book.pdf
.venv/bin/python -m quire manga 'https://example.org/chapter' --core --resume --selector '.reader' -o output/book.pdf
```

同一任务恢复时重新读取页面，并校验已有图片的大小和 SHA-256，仅重下缺失、损坏或失败资源。清单或内容参数变化会创建独立任务；调整并发、速率、重试等网络参数不影响复用。默认账本和缓存位于输出目录旁 `.quire-core`，输出目录改变时用相同 `--workdir` 指回数据根。首次运行也可带 `--resume`。

完整输出 PDF 和报告后默认删除本任务原图；`--keep-images` 可保留。中断、导出失败或部分成功保留恢复缓存，直到完整成功。默认同名 PDF 另存，明确需要替换时加 `--overwrite`。micro `.pyz` 不含恢复能力；缺失 core 能力返回退出码 6。

## 当前行为

- 支持 `img`、常见懒加载属性、`srcset`、无媒体条件的 `picture/source`、内联背景图和图片直链。同一图片的多个地址按备用顺序尝试，避免重复页。
- `--selector` 限定内容区域。支持标签、类、ID、属性、后代和直接子代选择器；不支持伪类及兄弟选择器。
- 默认保留 DOM 页序。需要按文件名排序时使用 `--order asc` 或 `--order desc`。
- 默认 4 个下载任务、每主机 4 次请求/秒。网络错误及部分 5xx/429 最多重试 3 次；遵守 `Retry-After`，超过 120 秒则返回错误供稍后重试。
- 每站查询并缓存 `robots.txt`；按 Quire 产品标识选择规则，支持最长路径、同长 Allow 优先和 `*` / `$`；404/410 视作无规则，禁止的请求不发送，策略查询失败会中止。不解析 Crawl-delay，仍执行本地限速。无登录或 Cookie 导入。
- 自动使用来源页面作为 Referer，`--referer` 可覆盖。单个响应默认限制 32 MiB，gzip 多成员/deflate 解压后累计也受限制；重定向中间正文不读取。
- JPEG 及支持的 PNG 写入 PDF；透明 PNG 合成白底。WebP、GIF、AVIF、隔行 PNG 等暂不转码，作为缺页记录，不静默丢页。
- PNG 验证压缩流和扫描行；JPEG 验证段结构及扫描数据存在，但不完整解码像素。慢速响应在每次读取间检查取消和期限，等待响应头或无数据阻塞仍受 socket 超时约束。
- micro 默认删除该任务临时图片，`--keep-images` 保留在 `.quire-work/task-*`；core 缓存行为见上方。本地输入图片始终保留。
- 成品先写临时文件，完成后原子发布。CLI 默认遇到同名成品另存 `(1)`；`--overwrite` 显式覆盖。取消或合成失败保留既有成品。
- 每个成品附带 `.report.json`，记录有效页、缺页和过滤原因；报告及错误信息隐藏 URL 查询参数。`inspect --dump-html` 属于显式保存原网页。

退出码：`0` 完整成功，`1` 参数错误，`2` 网络/文件操作失败，`3` 没找到内容，`4` 已生成但存在占位页，`5` 站点拒绝，`6` 缺少能力。core 中断后使用相同参数和数据根加 `--resume`；micro 取消后需重新下载。

## 开发与验证

```sh
uv sync --locked --extra dev --extra core --python 3.12
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/quire
.venv/bin/pytest --cov=quire --cov-report=json:output/coverage.json
.venv/bin/python scripts/check_coverage.py output/coverage.json
.venv/bin/python scripts/build_micro_zipapp.py
.venv/bin/python scripts/smoke_micro.py
swift scripts/render_pdf.swift output/smoke/quire-sample.pdf output/smoke/page
.venv/bin/python scripts/smoke_ledger.py
.venv/bin/python scripts/smoke_session.py
.venv/bin/python scripts/smoke_resume.py
```

`Pillow`、`pypdf`、`lxml` 仅用于开发验证，不进入 micro 包。`uv.lock` 固定开发依赖，2026-09-15 pip-audit 未发现已知漏洞；不覆盖系统组件。CI 工作流已提供，仅有本地 Git 仓库，未在 GitHub 运行。

历史 M0 准则审查：213 项测试通过、micro 48,988 bytes，隔离环境 PDF 与 PDFKit 渲染通过。最新模块测试、构建体积及限制见 [项目进度](项目进度.md)。

2026-09-15 本地 200 页测试：默认 4 req/s、4 并发，**51.0 秒**、子进程峰值 RSS **37.3 MiB**、成品 **2.41 MiB**、缺页 **0**。样本为 400px 左右宽的合成 JPEG，运行于回环 HTTP，不能推算真实漫画画质、目标压缩效果或公网成功率。复现：`.venv/bin/python scripts/benchmark_micro.py`。

## 下一阶段

M1/S1.1–S1.3 提供本地账本、连接池和 CLI 恢复；`scripts/smoke_resume.py` 使用真实本地 HTTP 服务验证强制结束子进程后的恢复，以及损坏缓存按页重下。当前恢复单位是图片，尚未复用已生成书籍或分章成品。

S1.2 的 `AsyncFetcher` 使用 httpx 连接池，作为 `core` 可选依赖；micro 继续使用标准库网络层。`scripts/smoke_session.py` 使用本地 HTTP/1.1 服务核验连接复用、并发、限速和取消。core 安装后的网络依赖文件实测 1,997,423 bytes（排除 pyc，不含 Python），详细清单可通过 `scripts/measure_core_dependencies.py` 重建。

M1 后续：Pillow 转码与体积控制、PDF/CBZ/图片 ZIP 同轮输出及成品清理一致性。最终产品默认目标 50 MB，可由用户调整；当前保留原编码，**尚不实施 50 MB 压缩上限**。

后续按 [项目设计](项目设计.md) 实现系统 Chrome 动态页面、小说 TXT/EPUB/PDF、可选本地 OCR、简单书库、四主题 Web UI 和 Swift 本地应用壳。四主题视觉预览仍在 [ui-preview/index.html](ui-preview/index.html)，尚未连接抓取器。
