# 命名决策记录 — 卷帙 / Quire

**状态：已定名（2026-09）**

| 项 | 值 |
|---|---|
| 中文名 | 卷帙 |
| 英文名 | Quire |
| CLI 命令 | `quire` |
| Python 包名 | `quire`（PyPI 发布前需查重，冲突则用 `quire-cli`） |
| 数据目录 | `~/Library/Application Support/quire/` |
| 应用包 | `Quire.app` |
| Bundle ID | `com.b1mango.quire` |
| micro 单文件 | `quire.pyz` |
| 安装包 | `quire-<version>.dmg` |

## 为什么是「卷帙 / Quire」

### 中文名：卷帙

- **卷** = 卷轴、书卷（scroll, *volumen*）
- **帙** = 包裹一函书卷的布套（a case holding a set of scrolls）
- **卷帙 = 一函书、成套的书**。重心在「成套/成函」，不在「厚」。

与产品的关系是双重吻合：

1. 工具的输出正是「把散落的图页与章节订成成套的书」；
2. `--split-by volume` 的分卷能力（见 DESIGN §6.13）让「帙 = 一套多卷」这层含义真正落到功能上。

### 英文名：Quire

**`quire` 是装帧术语**：一 quire 就是「一沓即将被装订成书页的纸」，是书在被装订成册之前的最小单位。

选它而不是 `Tome` / `Volume` 的理由：

| 候选 | 为什么没选 |
|---|---|
| `Tome` | **误译**。只对应「卷」，且强调「厚重的大书」，把「帙」的成套收纳义整个丢掉。且已被 AI 叙事产品占用。 |
| `Volume` | 语义准确（卷/册），但与「音量」严重撞义，作为命令名容易歧义。 |
| `Omnibus` | 7 字母、地道英文、语义贴（多部作品合订一册），**是最强的备选**。气质偏实用，不如 Quire 雅。 |
| `Codex` | 被 OpenAI Codex 污染。 |
| `Corpus` | 与语料库撞义。 |
| `Libri` | 准确但偏平，缺少「装订」的动作感。 |
| `Scrinium` | 对「帙」的直译最精确（罗马卷轴书箱），但 8 字母且过于生僻。 |
| `Bindery` | 直白好用，但只是「装订坊」，没有「成套」的意思。 |

`quire` 的优势：
- **5 字母，短、好敲、好念**，不会与常见命令行工具撞名；
- **它本身就是书在被装订成册之前的那个单位**——与工具的动词（把散页做成书）同构，比 `Tome` 更贴；
- 生僻但一看就懂是「书」相关的词，辨识度高；
- 中英文都有「书 / 装订」的意象，一组名字同源同义，不是硬凑。

## 词源考据（备查）

罗马人管装卷轴的圆筒书箱叫 **scrinium**（帙），管一卷书叫 **volumen**（卷）。
故「卷帙」最精确的直译是 **a scrinium of volumina**（一函书卷）。

完整候选表与评分见 `DESIGN.md` §0。

## 全局替换清单

定名后需要替换的位置（实现阶段照此执行）：

- [ ] `src/cndl/` → `src/quire/`
- [ ] CLI 入口 `quire`
- [ ] `pyproject.toml` 的 `name`、`[project.scripts]`
- [ ] 数据根常量 `~/Library/Application Support/quire/`
- [ ] `app/` 的 `Info.plist`（`CFBundleName`、`CFBundleIdentifier`、`CFBundleExecutable`）
- [ ] 构建脚本产物名（`quire.pyz`、`Quire.app`、`quire-<v>.dmg`）
- [ ] 环境变量 `CNDL_HOME` → `QUIRE_HOME`
- [ ] README 与用户可见文案
