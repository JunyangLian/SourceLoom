<div align="center">

<img src="docs/assets/sourceloom-mark.png" width="104" alt="SourceLoom" />

# 拾源 SourceLoom

**原位知识工作台 —— 你的文件按原来的方式保存，知识库自己生长。**

[![Release](https://img.shields.io/github/v/release/JunyangLian/SourceLoom)](https://github.com/JunyangLian/SourceLoom/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/JunyangLian/SourceLoom/total)](https://github.com/JunyangLian/SourceLoom/releases)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4)
![Version](https://img.shields.io/badge/version-0.4.1-005E9B)

[下载安装包](https://github.com/JunyangLian/SourceLoom/releases/latest) · [产品需求](docs/PRD.md) · [项目知识空间](docs/PROJECTS.md) · [解析说明](docs/INGESTION.md) · [开发断点](docs/BREAKPOINT.md)

</div>

---

## 这是什么

拾源（SourceLoom）是一个**本地优先**的知识工作台：它索引你已有的文件夹，让你用自然语言在里面找文件、读内容、问问题。

它不是网盘，也不是笔记软件。文件留在原地，拾源只在旁边建一层可检索、可回答的索引。

名字来自两件事：**Source** —— 始终保留原始来源与原件位置；**Loom** —— 把散落在各个目录里的资料编织成一张可检索的网。

从 v0.2.0 起，检索之上增加**项目知识空间**：文件仍是全局资产，项目只是逻辑工作上下文。
同一篇论文可以同时属于多个项目，但底层只解析一次、只切一份 Chunk、只生成一套向量。

三条设计原则：

| 原则 | 含义 |
| --- | --- |
| **不搬文件** | 不移动、不改名、不删除原件；撤销授权即清除索引 |
| **不替你做判断** | 同一份文档的多个版本只做归并，不推荐、不裁定哪个“更好” |
| **不默认联网** | 默认本地摘录回答；云端模型与 Embedding 需显式开启 |

## 核心能力

### 索引与解析

- 授权本地目录后自动扫描，记录文件名、路径、大小、修改时间与稳定文件 ID，支持增量对账
- 实时监听（1.5s 去抖 + 10s 封顶），同盘移动 / 改名沿用同一 `asset_id`；周期对账兜住休眠与事件丢失
- 解析 **TXT / MD / RST / 配置与源码**（ini·cfg·conf·toml·yaml·properties·py·js·ts·java·vue 等）、
  **CSV / TSV**、**DOCX**、**PPTX**、**PDF 文本层**、**XLSX**（值 + 公式）、
  **旧版 Office 与开放文档格式**（DOC / PPT / XLS / ODT / RTF / ODS / ODP / PPTM，经 LibreOffice）、
  **EML / MSG / MBOX**、**DXF**；音视频只取元数据；超过 64MB 的纯文本按前 8MB 头部采样，不再整文件跳过
- **支持格式是显式清单**：未知后缀一律「仅登记」，不按内容猜格式；`.assembly` 这类流程中间产物不会被当成正文。
  生信等专用格式计划以可插拔的「增强包」提供（见 [docs/BREAKPOINT.md](docs/BREAKPOINT.md) 待办）
- 压缩包（ZIP/7Z/RAR）默认「仅登记」：只记录文件名、路径与大小，不解压；确有需要时可在盘点策略里
  改为标准解析（临时目录解压，限制条目数、单文件与总量）
- 解析结果区分「待解析 / 仅登记 / 暂不支持」，策略性跳过不会显示成处理积压
- 解析成功的文件可在应用内直接**预览正文**（按页 / 工作表拼接，去掉分块重叠），不依赖系统文件关联
- **表格保留 markdown 格式**：DOCX / PPTX / XLSX（含 XLS 转换）里的表格输出为带表头与分隔行的
  GitHub 风格表格，多张表之间空行分隔，不再首尾相接挤成一段；单元格内换行与竖线会被转义
- OCR 双引擎：随包 PP-OCRv6 small（扫描 PDF，离线）与 Windows 本机 OCR（图片、混合版式 PDF、PPTX 位图），
  结果带页码与行坐标；无文本层的扫描件会被显式标记，而不是当成“内容为空”

### 检索与问答

- 三路召回：**文件夹路径 + 文件名 + 正文**，目录层级越深权重越高；通用目录词（实验/数据/报告）不单独构成证据
- 查询理解：剥离疑问词与口水词，中文 2/3-gram 展开，内置科研同义词（样本数 ↔ 样本量）
- 目录概览：问「大三下有哪些课」「pilot_batches 里有什么」直接列出子目录与文件数；
  子目录不多时额外展开一层文件名，省掉一次追问
- 位置问句：「放在哪个文件夹」「课程表在哪」只认具体路径命中，找不到就明确拒答
- 统计问句：「单据金额合计多少」这类需要跨行汇总的问题，明确说明不做求和，并列出命中的表格与工作表，不编造数字
- 多轮追问：同一会话里会带上上一轮问题与上一轮回答；「版本2」「第二个」「它 / 这个 / 上述」
  能解析成具体文件，选择版本时直接给出路径、更新时间、正文规模与开头预览
- 问题里点名数据源（「实验记录里…」）时，检索自动限定在该数据源内，避免跨库抢答
- 检索规划：按问题区分「精确查找 / 知识解释 / 综合探索 / 历史追溯」，决定证据预算（4 / 6 / 10 / 6 条）、
  同家族配额与最小覆盖面；回答下方显示候选数、采用数、覆盖家族数与「证据缺口」
- 动态证据选择：先跨家族取样保证覆盖面，再补同家族细节；与已选片段高度重合的内容直接跳过
- 模型重排（可选）：配置生成模型后，「知识解释 / 综合探索」先把前 50 条候选交给模型判断
  「能否直接回答」，只保留相关候选；模型未配置、超时或筛不出结果时自动回退本地排序
- 回答默认本地摘录（不联网），可切换 OpenAI 兼容接口或 Ollama；每条结论带 `[n]` 引用，
  没有证据直接拒答并说明内容覆盖

### 文档家族与版本

- 同一份知识的多版本自动归并为家族，家族 ID 基于成员哈希，重建后引用不失效
- 按**内容段落**去重而非按文件：重复段落合并展示并保留全部来源，各版本独有或修改过的段落仍在
- **版本新旧可判断**：`终稿 / 定稿`（带序号按序号，`终稿 10 > 终稿 9`）高于数字版本，
  数字版本按数字段比较（`0.13 > 0.9 > 0.8`），再往下是 `修订`、`初稿`、`副本`；
  `最新` 只是命名，不等于真的最后修改，压在终稿与数字版本之下
- 问「最新版本 / 当前版本」时补齐各大家族的最新稿；点名旧版本（如「v0.2 的 BUSCO」）时
  即使词面召回被同名笔记挤占也会补回该版本，并标记 `is_latest_version=false`，不会把旧稿当成当前数据
- 没有版本标签的家族只是重复副本：`is_latest_version` 返回 `null`（未知），不冒充“最新版本”
- 版本标签先归类（语义版本 / 日期 / 终稿序号 / 修改阶段 / 副本），同族内同类型才直接比较；
  跨类型时给出 `latest_basis`（`version_label` 或 `modified_time`）与
  `latest_confidence`（high / medium / low），外部 Agent 不会拿到一个“看起来确定”的猜测
- 家族 ID 以「角色 + 规范化文件名」为锚，新增或删除一个版本不会改变 ID，可跨重建引用
- 正文 / 图形摘要 / 投稿信 / 亮点 / 补充材料 / 回复按角色分开；落库前还会按角色再拆一次，
  “正文族里混进图形摘要”是结构性不可能，不依赖某个阈值恰好成立
- 版本差异视图：共享 / 仅左 / 仅右 / 近似修改（SimHash 预筛 + 序列相似度），可对比 `v0.12 → v0.13`
- 引用里同家族的多个版本折叠成一组，点开可看全部成员

### 项目知识空间

- 项目不是文件夹：可以关联一个或多个本地文件夹、其他项目的部分资料、分散文件与全局公共资料
- **全局项目**自动包含数据页录入的所有文件夹；子项目按需「从全局项目拉取」，不复制、不重扫
- 三种关系：主资料（默认优先检索）、关联资料（扩展检索使用）、排除资料（仅本项目不参与）
- 一个文件可属于多个项目；项目关系不会复制资产，也不会重复解析、切块、生成向量或摘要
- 检索三档：项目主资料 → 项目 + 关联 → 全局；证据不足时按确定性规则逐级扩展
- 项目统计：文件夹数、文件总量、已解析、已生成向量、失败与待处理、最近更新、当前检索策略
- 全局资料库保留：不创建项目也能照常浏览全部资产、检索未归属文件
- 隐私策略：`external_allowed` / `local_only`，后者同时拒绝 MCP 与 HTTP 外部令牌

### 外部接口与运维

- 每个项目可创建独立只读令牌（MCP / HTTP），Agent **只能读取该令牌绑定的项目**；换 `project_id` 直接报错
- 内置只读 MCP 服务，直接读取同一个 SQLite 库，不扫描、不解析、不改原件
- 删除项目会一并撤销其外部令牌；`local_only` 项目不产生任何外部入口
- 任务账本与失败重试、每日解析 / OCR 预算、低质量文本自动复核、一键重建索引与清理缓存

## 下载与安装

到 [Releases](https://github.com/JunyangLian/SourceLoom/releases/latest) 下载 `SourceLoom_x.y.z_x64-setup.exe`。
安装包自带 Python 核心与 OCR 模型，无需另装 Python 或数据库。

- 系统：Windows 10 / 11（x64）
- 安装目录：`%LOCALAPPDATA%\SourceLoom\`
- 数据目录：安装目录下的 `data\`（见下节），跟随安装位置，卸载或升级都不会删除
- 安装版启动时自动拉起核心进程，窗口关闭时结束；若 8737 端口已有核心在跑则直接复用

### 数据目录

从 v0.3.6 起，安装版的数据**跟着安装位置走**，不再固定写到 `%LOCALAPPDATA%\ShiyuanData\`：

| 路径 | 内容 |
| --- | --- |
| `data\shiyuan.db` | SQLite 主库：文件资产、正文分块、FTS 索引、向量、文档家族、项目关系与设置（WAL 模式，运行时会伴随 `-wal` / `-shm`） |
| `data\api-token` | 本机进程级访问令牌（核心每次启动重写，桌面端与 MCP 都读这里） |
| `data\core.log` | 核心运行日志 |
| `data\evals\` | 本地评测集（放进去才会用到） |

目录解析按优先级：

1. `SHIYUAN_HOME` 环境变量：指向哪里就用哪里（适合把库放到另一块盘或网盘）
2. 安装版：`<安装目录>\data\`
3. 源码运行（`python -m shiyuan` / `npm run tauri dev`）：`%LOCALAPPDATA%\ShiyuanData\`

> **从 0.3.2 及更早版本升级**：旧库在 `%LOCALAPPDATA%\ShiyuanData\`。升级前退出拾源，
> 把该目录下的 `shiyuan.db`、`shiyuan.db-wal`、`shiyuan.db-shm`（存在才复制）复制到
> `<安装目录>\data\`，或直接设置 `SHIYUAN_HOME` 指向旧目录。
> 备份 / 迁移同理：整体复制 `data\` 即可，路径变化不会改变文件资产的原件位置。

> **命名与兼容**：英文名是 SourceLoom，但为了不破坏已有命令行与 MCP 配置，
> 内部仍沿用 `shiyuan`：核心 `shiyuan-core.exe`、数据库 `shiyuan.db`、MCP 服务名 `shiyuan-mcp`；
> 源码运行的数据目录仍叫 `ShiyuanData`。改名只影响窗口、安装包与品牌展示。

## 五分钟上手

1. **录入资料**：数据页添加一个本地文件夹，等待扫描与解析完成（也可以直接拖入单个文件）
2. **先问一句**：在对话页问「这个文件夹里有什么」「XX 报告结论是什么」，看引用是否落在正确的文件
3. **建项目**：项目页新建子项目，从全局项目拉取需要的文件夹（只建关系，不复制文件）
4. **开外部接口**：在项目内创建只读令牌，复制生成的 MCP 配置到 Trae / Codex / Claude Code
5. **让 Agent 先用项目**：`list_projects` → `search_knowledge` → `get_document_text`；
   版本问题先 `list_document_families`

## 外部 Agent 接入（MCP）

```powershell
cd core
python -m shiyuan.mcp_server                 # stdio 模式
python -m shiyuan.mcp_server --print-config  # 打印客户端配置
```

安装版核心以 onedir 随包分发，路径在安装目录的 `binaries\shiyuan-core\` 下：

```json
{
  "mcpServers": {
    "shiyuan": {
      "command": "C:\\Users\\<你>\\AppData\\Local\\SourceLoom\\binaries\\shiyuan-core\\shiyuan-core.exe",
      "args": ["--mcp"]
    }
  }
}
```

> 项目页的「外部接口」会按实际安装位置生成配置，并可直接测试连通性。
> MCP 进程直接加载目录内依赖，握手约 1 秒。

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| tool | `list_projects` | 列出允许外部访问的项目（绑定令牌时只返回该项目） |
| tool | `get_project` | 项目信息、文件夹与统计 |
| tool | `search_assets` | 检索文件 / 路径，支持 `project_id`、`scope` |
| tool | `search_knowledge` | 检索文件名 / 路径 / 正文 / OCR，返回片段、页码与版本标记 |
| tool | `get_evidence` | 按 `chunk_id` / `location_id` 取原始证据（页码、工作表） |
| tool | `get_document_family` | 文档家族与全部成员版本，最新版本在前 |
| tool | `list_document_families` | 列出项目内多版本文档，含最新版本与全部版本标签 |
| tool | `get_related_documents` | 同一家族的其他版本与同项目内其他资料 |
| tool | `compare_versions` | 家族内两个版本的段落差异 |
| tool | `get_source_status` | 数据源扫描、覆盖与健康状态 |
| tool | `get_document_text` | 按 `location_id` 或绝对路径读取正文，可按页 / 工作表过滤 |
| tool | `list_sources` | 授权目录、扫描健康度与文件数 |
| tool | `knowledge_stats` | 内容覆盖、类型分布、OCR 状态与维护计数 |
| resource | `shiyuan://sources` | 数据源 JSON |
| resource | `shiyuan://stats` | 统计 JSON |

使用约定（也写进了 MCP `instructions`）：

- 令牌绑定项目：普通项目固定 `expanded`，不能切到全局；传入其他 `project_id` 会报错
- `weak=true` 的结果只是弱线索，不能当作答案；`match=name` 只说明文件名/路径命中，事实问题要读正文
- `is_latest_version=null` 表示该文件没有版本标签（普通副本），不能描述成“最新版本”
- MCP 只读：创建项目、调整成员、管理令牌都留在桌面端

## 源码运行

前置：Windows 10/11、Node 20+、Python 3.10+、Rust 1.80+（仅桌面壳需要）。

```powershell
# 1. 本地核心（默认 http://127.0.0.1:8737）
cd core
python -m pip install -r requirements.txt
python -m shiyuan

# 2. 另开终端启动桌面端
cd ..\desktop
npm install
npm run tauri dev
```

只看界面可用浏览器模式：`cd desktop; npm run dev` 后打开 http://localhost:1420
（浏览器无法调用系统目录选择框，会退化为手动输入路径）。

常用环境变量：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `SHIYUAN_HOME` | 安装目录 `data\`（源码运行为 `%LOCALAPPDATA%\ShiyuanData`） | 数据库、向量、令牌与日志位置 |
| `SHIYUAN_WATCH` | `1` | 设为 `0` 关闭实时监听，只保留手动扫描 |
| `SHIYUAN_RECONCILE_SECONDS` | `600` | 周期对账间隔（最小 30 秒） |
| `SHIYUAN_DAILY_PARSE_BUDGET` | `500` | 每日正文解析文件数上限，`0` 为不限 |
| `SHIYUAN_DAILY_OCR_BUDGET` | `200` | 每日 OCR 文件数上限，`0` 为不限 |
| `SHIYUAN_MODEL_TIMEOUT` | `90` | 模型单次回答超时秒数（推理模型可调 120） |
| `SHIYUAN_RERANK_TIMEOUT` | `40` | 候选重排单次超时秒数；超时自动回退本地排序 |

## 架构

```
core/                 Knowledge Core（Python + FastAPI + SQLite）
  shiyuan/            scanner · watcher · ingest · extractors · ocr · indexer
                      projects · query · retrieval · families · rerank · answer · chat
                      external · mcp_server · inventory · maintenance · api
  tests/              297 项用例（扫描 / 监听 / 检索 / 问答 / OCR / 家族 / 召回 / 项目 / 外部接口 / 安全 / 基线）
  benchmarks/         目录级扫描基准
desktop/              Tauri v2 + React + TypeScript
  src/                数据 · 项目（含外部接口）· 对话 · 更新 四个模块
  src-tauri/          Rust 外壳（dialog / opener / shell）
docs/                 PRD、项目知识空间、解析说明、开发断点
```

数据流：`扫描 → 解析/OCR → 正文分块 → 路径索引 + FTS + 家族归并 → 项目范围过滤 → 检索 → 证据 → 回答/引用`。
项目层只维护 `project_sources` / `project_assets` 两张关系表，不进入解析与向量生成链路。

## 检索是怎么打分的

1. **查询理解**：去掉疑问结构与口水词，按「的」切词，剥句尾方位词，展开同义词
2. **检索规划**：判断意图模式，给出证据预算、家族配额、最小覆盖面与需要覆盖的关键词
3. **项目范围**：Project 只查主资料；证据不足时扩展关联资料，再不足才进入全局；排除资料只影响当前项目
4. **路径召回**：按数据源内相对路径打分（根目录名不参与），文件夹按深度加权，文件名权重更高
5. **正文召回**：FTS5 trigram + 两字词 LIKE 兜底；项目范围用 `VALUES` CTE 直接下推到 SQL，
   先限定资产再取候选，避免项目内文档被全局候选挤出
6. **版本判断**：家族按 `0.13 > 0.9`、`终稿 > 修订版 > 初稿` 标记最新版本；
   默认优先最新稿，明确问「历史 / 改了什么 / v0.12」时保留对应版本
7. **证据门槛**：按「最大词」计（避免 `创新 / 新点 / 创新点` 这类 gram 膨胀误杀），纯通用目录词不构成证据
8. **选择与去重**：先每家族取一条凑够覆盖面，再补同家族第二条；4-gram 重合度 ≥ 0.88 的片段跳过；
   同一资产属于多个项目也只返回一次
9. **排序**：`追问词命中 × 120 + 最大词命中 × 10 + min(路径分, 300)`；
   Expanded 范围内主资料额外优先，最新版本与成稿路径额外加权
10. **生成**：只喂入选片段，并把「模式 / 覆盖 / 缺口」一起交给模型；证据不足直接拒答

## 本机安全边界

- **API 令牌**：核心启动生成进程级令牌写入 `api-token`，除 OPTIONS 预检外所有 `/api/*` 需携带
- **CORS**：只允许 `localhost:1420` 与 Tauri 来源
- **密钥加密**：回答 / Embedding 的 Key 用 Windows DPAPI 加密（`dpapi:v1:`）写入 SQLite，旧明文自动迁移
- **外部连接**：每个 Agent 独立令牌（`syext_`），绑定一个项目；可单独撤销；
  HTTP 令牌只放行 `/api/health`、`/api/sources`、`/api/search`、`/api/stats`、`/api/ask`、`/api/ask/stream`
- `SHIYUAN_API_AUTH=0` 仅用于测试排障，不要长期关闭

## 打包安装包

<details>
<summary>PyInstaller sidecar + Tauri NSIS（点击展开）</summary>

```powershell
# 1. 生成核心目录（onedir，约 390MB；需 pip install pyinstaller）
cd core
$root = Split-Path (Get-Location) -Parent
$env:TMP = Join-Path $root ".tmp\tmp"
$env:TEMP = $env:TMP
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root ".tmp\pyinstaller"
New-Item -ItemType Directory -Path $env:TMP, $env:PYINSTALLER_CONFIG_DIR -Force | Out-Null
python -m PyInstaller shiyuan-core.spec --noconfirm
Copy-Item .\dist\shiyuan-core ..\desktop\src-tauri\binaries\shiyuan-core -Recurse -Force

# 2. 构建 NSIS 安装包
$env:CARGO_TARGET_DIR="D:\cargo-target"
cd ..\desktop
npm run tauri build
```

产物：`D:\cargo-target\release\bundle\nsis\SourceLoom_<版本>_x64-setup.exe`（约 138MB）。
核心以 onedir 目录随安装包分发（`tauri.conf.json` 的 `bundle.resources`），Rust 外壳从资源目录
拉起 `binaries\shiyuan-core\shiyuan-core.exe`，启动不再解包，MCP 握手约 1 秒。
`desktop/src-tauri/binaries/` 是构建产物，不入库；新克隆仓库需先执行第 1 步。
桌面端主程序名为 `SourceLoom.exe`（`tauri.conf.json` 的 `mainBinaryName`）。

**Windows + MinGW 注意**：仓库路径含中文时，`ld.exe` 会按 GBK 解析 UTF-8 路径并在链接阶段报
`cannot find ...<xxx>.rlib`。把 `CARGO_TARGET_DIR` 指到纯 ASCII 路径即可。`crate-type` 只保留
`rlib`，因为 MinGW 链接 `cdylib` 会报 `export ordinal too large`（桌面端不需要）。

</details>

## 验证

```powershell
cd core; python -m pytest -q          # 297 passed（另需 pip install pytest httpx）
cd desktop; npm test                  # 18 passed（Vitest + jsdom）
cd desktop; npm run build             # tsc --noEmit + vite build
cd desktop/src-tauri; cargo check     # 先设置 CARGO_TARGET_DIR
```

检索基线（120 篇文档 / 140 道题）：R@1 = 1.000、R@5 = 1.000、MRR = 1.000（合成唯一标记语料）。
扫描基线：2000 个文件首次扫描约 2.1s（约 940 files/s），二次对账约 0.56s。

md 提取全流程评测（真实库 13,012 资产 / 12 道真实问答对，逐板块对账）：

- 解析：218 个 md 全量覆盖，无空块 / 超限块；超长表格行整行入块
- 分块与读取：`get_document_text` 拼接结果与磁盘原文去空白后逐字一致
- 检索：词面可回答 10/12（修复代码文件撞词、跨词 3-gram 遮蔽、候选按插入序截断后）
- 端到端：回答可回答 11/12，12/12 的答案数值都能在被引证据全文里找到（零编造）
- MCP：外部 Agent 流（`search_knowledge` → `get_document_text`）12/12 可读到答案

真实库（13,012 资产 / 382,678 chunk）：Project / Expanded / Global 三档检索中位约 1.9 / 2.0 / 2.3 秒；
空项目直接返回；项目列表 3ms、资料首页 11ms、项目知识 16ms、普通项目统计 29ms。
同一篇论文加入 10 个项目后，资产、Chunk 与向量数量均不变。详见 [docs/PROJECTS.md](docs/PROJECTS.md)。

外部接口有用性（打包版 `shiyuan-core.exe --mcp` + 项目令牌实测）：

- 「最新版本」前 5 条均为 v0.13；`list_document_families` 最新版本标签为 `0.13`
- 「v0.2 版本的 BUSCO」第 1 条即 v0.2 稿，`is_latest_version=false`，家族最新为 `0.6`
- 「v0.12 到 v0.13 改了什么」同时返回两个版本，`compare_versions` 给出共享 / 新增 / 删除 / 修改摘要
- 事实问题可读到稿件正文的具体数值（如 BUSCO 98.4 / 96.4）

## 已知限制

- **OCR 只保留行坐标**，不还原表格结构与版面，没有手写专项
- **不内置本地大模型**：Ollama 需自行安装；语义检索依赖外部 Embedding 服务，未使用 ANN，
  填了不支持 `/embeddings` 的模型时该通道会被静默跳过
- **会话记忆只覆盖上一轮**：能解析「版本N / 第二个 / 它」，但不保留更早的对话状态，跨会话无记忆
- **跨盘移动**识别为「新增 + 失联」，不做自动合并
- **网络盘 / SMB** 的监听可靠性未实测，主要依赖周期对账
- **新建 / 删除 / 改名文件夹**会触发整源重扫，大目录会明显变慢
- **短词检索**（<3 字符）走 LIKE 扫描，语料很大时比 FTS 慢
- **引用只校验编号**：无法证明模型每句话都忠于证据，仍需点开来源核对
- **不做表格汇总**：合计 / 平均 / 总额这类问题只定位表格并说明，不会真的计算
- **压缩包默认不解压**：ZIP/7Z/RAR 只登记文件名与路径；需要内容时在盘点策略里改为标准解析
- **超限文件有取舍**：纯文本超过 64MB 只采样前 8MB 并标注截断；PDF/PPTX 超过 64MB 仍跳过
- **重排依赖外部模型**：只有配置生成模型并允许云端时，「知识解释 / 综合探索」才重排，
  单次约 2–5 秒；精确查找、历史追溯与本地摘录模式仍靠关键词与路径打分
- **全局项目统计需要扫描全部内容版本**：首次约 0.7 秒；普通项目统计已降至几十毫秒
- **全局项目是虚拟的**：不写成员表，因此不能在全局项目里单独排除文件；排除请在子项目里做
- **Local-only 目前只约束外部出口**：字段级脱敏与完整 Privacy Gateway 尚未实现
- **MCP 只读**：不能在 Agent 侧创建项目、调整成员关系或创建外部接口
- **家族自动归并**：同一篇论文若前后版本标题与结构变化过大，可能被识别成两个家族
  （例如 `manuscript_v0.2~v0.7` 与 `clean_v0.8~v0.13`）；需要按最新版本与时间自行判断，
  当前不支持手动合并家族
- **没有版本标记的旧稿**：如 `xxx-new.docx` 与 `xxx.docx` 不会被自动判定谁更新，只能靠内容与时间核对

## 文档

- [docs/PRD.md](docs/PRD.md) — 产品需求与设计
- [docs/PROJECTS.md](docs/PROJECTS.md) — 项目知识空间、范围检索、API / MCP 与迁移
- [docs/INGESTION.md](docs/INGESTION.md) — 解析与 OCR 策略
- [docs/BREAKPOINT.md](docs/BREAKPOINT.md) — 开发断点与已知问题
