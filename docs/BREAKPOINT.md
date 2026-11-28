# 开发断点文档

更新：2026-11-27

## 一、当前基准（已验证）

日常迭代方式（2026-11-22 起）：先只改网页版，用 `cd core && python -m shiyuan` +
`cd desktop && npm run dev`（http://localhost:1420）验证；**仅在用户明确要求时**才重新打
PyInstaller sidecar 与 Tauri 安装包。

本地测试/构建临时目录统一放仓库 `.tmp/`（已 gitignore），不写 C 盘：
`$env:TMP=$env:TEMP="<repo>\\.tmp\\tmp"`，`$env:PYINSTALLER_CONFIG_DIR="<repo>\\.tmp\\pyinstaller"`；
`scan_benchmark.py` 默认也使用 `.tmp/scan-*`。

| 项 | 状态 | 证据 |
| --- | --- | --- |
| PRD 落地 | 完成 | `docs/PRD.md`（v0.1 原文 + 表格化） |
| Core 单元/集成测试 | 322 passed | `cd core && python -m pytest -q` |
| 检索基线 | 140 题 R@1/R@5/MRR = 1.000 | `python -m pytest tests/test_retrieval_benchmark.py -q -s`（合成唯一标记语料） |
| 扫描基线 | 2000 文件首次 2.12s，二次 0.56s | `python benchmarks/scan_benchmark.py --files 2000` |
| 前端类型检查与构建 | 通过 | `cd desktop && npm run build` |
| 前端单元测试 | 21 passed | `cd desktop && npm test`（Vitest + jsdom + Testing Library） |
| Tauri 壳编译 | 通过 | `cd desktop/src-tauri && cargo check` / `cargo build`（需 `CARGO_TARGET_DIR` 指向 ASCII 路径，见 README） |
| 桌面端启动 | 通过 | 安装版窗口显示「本地核心已就绪」，WebView2 经 Tauri `api_token` 命令取令牌后调用 `/api/health`、`/api/stats` |
| 桌面端安装包 | 完成 | `SourceLoom_0.4.1_x64-setup.exe`（约 138 MiB，onedir 核心随包分发，默认检索工作台、统一证据接口与 source 级 freshness，含项目知识空间、多格式解析、OCR 双引擎、MCP `--mcp`、API 令牌与 DPAPI）；安装版数据目录为 `<安装根>\data`；解析签名 `l1.v5`；「打开原件」改走 Rust `open_original` 命令（绕开 `opener:default` 不含 `open_path` 的 ACL，关联缺失时回退「打开方式」） |

## 二、已实现范围（M1）

### 数据层（`core/shiyuan/db.py`）
SQLite WAL + 外键。表：`sources`、`assets`、`asset_locations`、`content_versions`、
`source_scan_runs`、`maintenance_events`。PRD §7.1 中的 `parse_artifacts`、`chunks`、
`index_jobs`、`user_decisions` **故意未建**，避免空表造成"已实现"的错觉。

### 扫描（`core/shiyuan/scanner.py`）
- 手动/启动触发的完整对账，无实时监听。
- 同盘移动/改名：按 `st_dev:st_ino` 复用同一 `asset_id`（`asset_locations` 原地更新路径）。
- 修改：写入新 `content_versions` 行，旧行 `is_current=0`；≤32MB 计算 sha256，更大文件仅存元数据指纹并带 `meta:` 前缀。
- 删除 → `MISSING`；数据源不可达 → 全部 `OFFLINE` 且不删除记录。
- 符号链接 / junction 一律跳过，路径必须先通过 `relative_to(root)` 校验。

### 检索（`core/shiyuan/search.py`）
只用 SQLite `instr(lower(path), token)` 做多关键词 AND 匹配，再在 Python 内打分排序
（文件名精确 > 前缀 > 包含 > 路径包含）。未引入 FTS5：M1 没有内容层，引入只会增加无人使用的表。

### 接口（`core/shiyuan/api.py`）
`GET /api/health`、`GET/POST /api/sources`、`POST /api/sources/{id}/scan`、
`POST /api/sources/{id}/state`、`DELETE /api/sources/{id}`、`GET /api/search`、
`GET /api/stats`、`GET /api/events`。只监听 127.0.0.1，CORS 白名单仅本地端口。

### 桌面端（`desktop/`）
两个一级页面，减少导航层级：
- **检索与对话**：内部分「找文件 / 知识对话」标签；两个视图常驻，切换不会丢失查询结果或对话状态。
  对话输入区用 sticky 固定在视口底部，消息滚动时不会把输入框推走；会话列表固定在列顶。
- **数据源与维护**：内部分「数据源 / 维护」标签，分别承载授权目录操作与统计、覆盖、失败任务、维护日志。

浏览器模式与 Tauri 模式通过 `src/tauri.ts` 分流。`src/App.test.tsx` 覆盖：两个一级导航、
检索/对话与数据源/维护的标签切换、视图常驻不卸载、composer 位于消息列表之后且样式为 sticky、
切回对话时滚动到最新消息。

### 内容层（M3）
- `extractors.py`：TXT/MD/代码按文本解码（utf-8 → gb18030 回退）；DOCX 含表格；PDF 按页用 pypdf
  取文本层；XLSX/XLSM 按工作表取非空行。单文件上限 64MB、总字符上限 200 万。
- `indexer.py`：`parse_artifacts` + `chunks` + `chunk_fts`（FTS5 trigram）。缓存键为
  `version_id + PARSER_SIGNATURE`；同一资产只在当前版本上保留块，重建前清除旧版本缓存。
- 无文本层（扫描件）写入 `state='empty'`，界面显示"无文本层"，绝不伪装成已全文检索。
- 扫描结束后在同一后台线程继续 `index_pending`，L0 结果先可见，L1 随后补齐。
- 检索 `search.py`：关键词全部 ≥3 字符走 FTS5，否则退化为 `instr(lower(...))` LIKE 扫描
  （中文两字词、英文短词可命中，但语料大时更慢）；结果合并后给出 `match`、`snippet`、`page`、`sheet`。
- 撤权时 `store._purge_content_cache` 同步删除 FTS / chunks / artifacts。

### 解析覆盖扩展（2026-11-22）
- PDF 主解析换成 PyMuPDF（pypdf 兜底）。单页有效文本少于 8 字符，或替换字符/私用区字符
  超过 5%，视为异常页并留空，交给 L2 OCR——混合 PDF 因此能逐页补识别。
- DOCX：正文段落 + 表格 + 页眉/页脚 + 批注（python-docx 1.2 的 comments API）。
- PPTX：按幻灯片提取形状文字、表格与备注，页码即幻灯片序号；图片内文字由 L2 OCR 补齐。
- XLSX/XLSM：计算值与公式原文同时保留（`值〔公式：=...〕`），按行拼接单元格，保留行列关系。
- DOC/PPT/XLS：运行时自动发现 LibreOffice（`SHIYUAN_SOFFICE` 可覆盖，其次 PATH / 注册表 /
  Program Files），转成 OOXML 到系统临时目录后复用上面的解析器；XLS 转换失败时回退 xlrd，
  其余转换失败记 `failed`，不碰原件。本机 LibreOffice：`D:\1Bio_Soft\LibreOffice`。
- PPTX 内嵌图片 OCR：L1 记录 `parse_artifacts.image_count`，L2 按幻灯片识别图片文字
  （单文件最多 20 张、单张 20MB），结果带幻灯片页码，使用独立 OCR 签名，不使已有 PDF/图片 OCR 失效。
- 压缩包 ZIP/7Z/RAR：只在临时目录解压，限制 300 个条目、单文件 64MB、总解压 128MB、
  嵌套 2 层、单包最多解析 20 个文件；拒绝路径穿越、符号链接与加密包，按 SHA-256 去重。
  RAR 通过 unrar 或系统 bsdtar（Windows 自带 `tar.exe`）解压。
- EML/MSG：主题、发件人、收件人、时间、纯文本正文（无纯文本时回退 HTML）、附件与嵌套邮件
  （最多 20 个附件、2 层嵌套）；附件复用压缩包的安全解析与去重逻辑。
- CAD：DXF 提取图层文字、MTEXT、标注与块属性；DWG 需要 ODA File Converter，
  通过 `SHIYUAN_ODA_CONVERTER` 指定后自动转 DXF。
- 音视频：mutagen 读取时长、码率、采样率、分辨率与常用标签，**不转写**；上限 2GB。
  支持 MP3/FLAC/WAV/M4A/AAC/OGG/OPUS/WMA/AIFF 与 MP4/MOV/WMV 等，其他容器显式报错。
- 解析签名升级为 `l1.v5`：DOCX / PPTX / XLSX / XLS 的表格改为 GitHub 风格 markdown 表格
  （首行表头 + `---` 分隔行、空列补齐、单元格内竖线转义与制表符还原成列）。
  DOCX 改为按正文顺序输出段落与表格（不再把表格堆到文末、彼此首尾相接）；PPTX 每个形状
  独立成块并用空行分隔。旧索引在下次「重建正文索引」或重新扫描时自动重解析
  （`pending_rows` 以签名判断）。
- 依赖新增：`pymupdf`、`python-pptx`、`xlrd`、`py7zr`、`rarfile`、`extract-msg`、`ezdxf`、`mutagen`。

### 本机安全与评测基线（2026-11-22）
- **API 访问令牌**：核心启动时用 `secrets.token_urlsafe(32)` 生成（可用 `SHIYUAN_API_TOKEN` 固定），
  写入 `app_home/api-token`；`require_api_token` 中间件对除 OPTIONS 外的 `/api/*` 校验
  `Authorization: Bearer` 或 `X-Shiyuan-Token`，失败返回 401。`SHIYUAN_API_AUTH=0` 仅供测试。
- **CORS 收紧**：只允许 `http://localhost:1420`、`http://127.0.0.1:1420`、
  `http://tauri.localhost`、`https://tauri.localhost`、`tauri://localhost`；CORS 作为最外层，
  401 也带跨域头。测试覆盖无令牌、错令牌、两种头、允许/拒绝来源与 401 的 ACAO。
- **前端取令牌**：Tauri `api_token` 命令按 `SHIYUAN_HOME` → 安装目录 `data\`（release）→
  `LOCALAPPDATA\ShiyuanData`（源码调试）读取文件；`api.ts` 缓存令牌，401 时清缓存重取一次；
  浏览器开发模式由 Vite `/__shiyuan_token` 读取同一文件。首次启动最多重试 30 秒。
- **单实例**：`shiyuan/instance.py` 用命名互斥量 `Local\ShiyuanHttpCore-{port}` 防止两个 HTTP
  核心同时写令牌；sidecar 与 `python -m shiyuan` 共用，MCP 模式不受影响。
- **API Key / DPAPI**：`secrets_store.py` 用 `CryptProtectData`（固定 entropy，绑定当前 Windows 用户）
  加密，密文前缀 `dpapi:v1:`；answer/embedding 读取时解密，保存时加密；启动迁移旧明文并
  `secure_delete` + `VACUUM`。本机真实库已确认两项均为 `dpapi:v1:`。
- **扫描 follow-up**：`request_scan` 在忙碌时写 `_followup[source_id]`，`release_scan` 取到后自动
  再拉起一轮扫描；覆盖「事件晚于本轮枚举」的窗口，测试覆盖忙碌释放与无 pending 两种情况。
- **检索基线**：`test_retrieval_benchmark.py` 生成 120 篇唯一标记文档、140 道题
  （40 文件名、40 正文标记、40 中文两字短词、20 自然语言），当前 R@1=1.000、R@5=1.000、MRR=1.000。
  同时修复了正文命中在最终打分时丢失 `concepts/variants`、同分后被 mtime 排序淹没的问题。
  该语料是合成唯一标记，不能替代真实用户问句；下一步应补充真实标注。
- **扫描基线**：`core/benchmarks/scan_benchmark.py --files 2000` 首次 2.12s（943 files/s）、
  二次 0.56s；默认目录放在仓库 `.tmp/`，不写 C 盘。10 万文件需在本机按需跑。
- 测试：后端 124 passed（新增 API 认证 3、DPAPI 4、单实例 1、检索基线 1）。

## 三、当前状态与已知缺口

1. ~~**Python core 需要手动启动**~~：已解决。`core/shiyuan-core.spec` 产出 onedir 目录，
   经 `tauri.conf.json` 的 `bundle.resources` 随安装包分发；release 启动时探测
   `127.0.0.1:8737`，无监听才从资源目录 `binaries/shiyuan-core/shiyuan-core.exe` 拉起，
   `RunEvent::Exit` 时 `kill`。开发模式仍不托管。
2. ~~**单文件 sidecar 启动有解包延迟**~~：v0.1.7 改为 onedir 分发，核心启动与 MCP 握手约 1 秒，
   onefile 版本不再使用。
3. **强制结束桌面端会残留核心**：任务管理器结束进程不触发 `RunEvent::Exit`，sidecar 会继续
   监听 8737；下次启动会复用它（不重复拉起）。命名互斥量已避免同一端口出现两个 HTTP 核心，
   但用户仍无法从界面关闭该后台进程。
4. ~~**数据目录与安装目录分离**~~：v0.3.3 起改为「数据跟着安装位置走」。安装版
   `config.app_home()` 在冻结环境解析 `<安装根>\data`（`sys.executable` 向上三级），
   Rust `api_token` 同步改读 `resource_dir()\data`；`SHIYUAN_HOME` 仍是最高优先级覆盖。
   实测 Tauri v2 NSIS 卸载器（`/S` 静默）**不递归删除 `$INSTDIR`**，只删安装清单内文件，
   因此 `<安装根>\data` 在卸载/升级中天然保留（测试用 `C:\SLTest` + marker 文件验证）。
   源码运行仍用 `%LOCALAPPDATA%\ShiyuanData`，避免开发时污染安装目录。
5. **扫描进度不可见**：`_running` 与 follow-up 都是进程内状态，界面只能看到"扫描中"与上次结果；
   重启会丢失运行态；若进程在 follow-up 前崩溃，仍要等周期对账补齐。
6. **API 默认端口写死 8737**：仅 `SHIYUAN_PORT` 可改，前端代理与 sidecar 探测未同步。
7. **大文件哈希为元数据指纹**：`meta:size=...;mtime_ns=...` 不能证明内容相同，后续做重复分析时必须排除。
8. **目录级事件回退整源对账**：新建/删除/改名文件夹时无法只看路径，会触发一次完整扫描；
   超大目录或网络盘上那次同步会明显变慢。
9. **网络盘 / SMB 未实测**：watchdog 在共享盘上的事件可靠性未知，目前主要依赖
   `SHIYUAN_RECONCILE_SECONDS`（默认 600 秒）周期对账兜底。
10. **MinGW 不支持中文绝对路径**：`ld.exe` / `windres` 按 ANSI 解析 UTF-8 路径，链接期报
    `cannot find ...rlib: No error`。已用两条措施绕开：构建时把 target 目录放到 ASCII 路径
    （`CARGO_TARGET_DIR`），`build.rs` 把 `icons/icon.ico` 复制到 `OUT_DIR` 再引用。
    彻底方案是换 MSVC 工具链。
11. **短关键词退化为 LIKE 全表扫描**：FTS5 trigram 只索引 ≥3 字符的子串，`样本` 这类两字词
    走 `instr(lower(text))`，块数量大时会明显变慢；后续可加 bigram 或按需短词索引。
12. **预算只按文件数计**：默认 500 / 200 个文件/天（`SHIYUAN_DAILY_*_BUDGET`，0 为不限），
    不统计耗时与字符数；低质量判断是启发式（替换字符占比），只对 PDF 追加 OCR。
13. **解析覆盖边界**：PDF 复杂版面（多栏、公式、表格结构）仍按纯文本处理，未接 Surya/MinerU；
    PPTX 只识别位图文字，SmartArt/图表内文字与 EMF/WMF 图片暂不处理；压缩包内只索引支持的类型，
    不做二进制 MIME 嗅探；MKV/AVI/WebM/FLV 等容器 mutagen 不支持，音视频一律不转写。
    DOC/PPT/XLS 依赖本机 LibreOffice，RAR 依赖 unrar/bsdtar，DWG 依赖 ODA 转换器；
    缺少外部工具时相应文件会显式失败（不静默跳过）。
14. ~~**API Key 明文存储**~~：已用 Windows DPAPI 加密（`dpapi:v1:`），启动时自动迁移旧明文并
    `secure_delete` + `VACUUM`；同一 Windows 用户下的进程仍可调用 DPAPI，不能替代独立账户隔离。
15. ~~**对话仍是单轮检索**~~：v0.1.7 起追问会带上上一轮问题与回答，支持「版本2 / 第二个 /
    它 / 这个」并解析到具体文件；更早的对话与跨会话仍无记忆。
16. **语义召回依赖外部服务**：向量由 OpenAI 兼容接口或本机 Ollama 生成，没有内置模型；
    未开启时行为与纯关键词一致。余弦比对是内存全量扫描，无 ANN 索引，块数上万后会变慢；
    自定义同义词需手动配置，跨语言提问仍偏弱。
17. **引用只校验编号**：能发现"没引用"和"引用了不存在的编号"，但无法证明每句话都忠于证据；
    云端接口失败时直接报错，不会自动降级到本地摘录。
18. **Windows OCR 依赖系统语言包**：图片与文本 PDF 仍用 Windows OCR；没有中文语言包时这部分不可用，
    扫描 PDF 可由随包的 RapidOCR 兜底。
19. **OCR 只保留行坐标**：`chunks.bbox` 有每行 `[x,y,w,h]`，但没有版面/表格结构还原，前端暂不绘制；
    手写与低质量扫描识别率低；图片无页码，只有行坐标。
20. **OCR 有单轮上限**：一次最多 200 个文件、每份 PDF 最多 30 页，超出需再次触发；没有预算调度。
21. **OCR 引擎已可选，但能力仍有限**：`auto` 下整份扫描 PDF 用 PP-OCRv6 small，图片与文本型 PDF 仍是
    Windows OCR；无手写专项、表格结构与版面还原；RapidOCR 约为 Windows 的 12 倍耗时、3 倍内存。
22. **向量补建占用扫描线程**：扫描结束后在同一线程里继续 `index_pending` 与 `index_pending_embeddings`，
    外部接口慢时会明显拉长"扫描中"时间；没有并发限速、失败重试与费用估算。
23. **MCP 已合入核心 exe**：`shiyuan-core.exe --mcp` 提供 stdio，桌面端默认仍走 HTTP；
    `--noconsole` 下依赖客户端用管道启动（本机已验证），不再需要单独 exe；onedir 下握手约 1 秒。
    MCP 只读，要求外部连接令牌（`SHIYUAN_EXT_TOKEN`，非法令牌直接退出），只走本机 stdio，
    不应直接暴露为网络服务。

### 数据源合并
- `store.plan_source_add` 判断新目录与现有授权的关系：
  - `existing`：同一目录，直接复用；
  - `covered`：新目录在已授权目录内，返回最近的上级；
  - `merge`：新目录包含一个或多个已授权子目录，需要合并；
  - `new`：互不包含。
- `store.add_source_merged`：新增上级目录时，把子数据源的 `asset_locations.source_id` 改到上级、
  重算 `rel_path`、把 `index_jobs.source_id` 一并迁移，再删除子 `sources` 行；
  原文件不动，内容缓存与向量继续复用。排除规则取并集。
- API `POST /api/sources` 返回 `created / covered_by / merged`；监听器会取消子目录订阅、
  改为监听上级目录。若子数据源正在扫描，返回 409，避免迁移中并发写库。
- 前端「数据源」在添加后提示“已自动合并 N 个下级目录到「…」”或“已包含在现有授权中”。

### 实时监听与增量同步（M2）
- `watcher.py`：watchdog `Observer` 按数据源订阅；事件进入 `PendingBatch`，安静 1.5 秒或累计
  10 秒后统一处理；目录事件（新建/删除/改名）回退为整源 `scan_source`。
- `scanner.py` 重构出 `_plan_ops`，完整对账与 `sync_paths` 共用同一套归因（新增/修改/移动/恢复）；
  `scan_in_background` 用 `request_scan` 保证同一数据源不并发写库，手动扫描与监听器共用。
- STALE：事件批次处理时先 `store.mark_stale`，同步成功且内容未变则 `restore_location` 恢复
  AVAILABLE（不产生新版本），内容变化则走 update 写新版本；同步失败保持 STALE，检索不会返回旧内容。
- 周期对账线程按 `SHIYUAN_RECONCILE_SECONDS`（默认 600）对所有 active 源触发完整对账；
  `SHIYUAN_WATCH=0` 可整体关闭监听（测试用此开关保证确定性）。
- 维护日志新增 `watch_started / watch_stopped / watch_failed / sync_completed / sync_failed`。

### 问答与引用（M4）
- `answer.py`：把问题拆成英文词 + 中文三元组（FTS5 需要 ≥3 字符），先走 FTS `OR` 召回，
  再在 Python 里按"命中单元数"排序；长问题要求 ≥2 个单元，过严时退回 1 个。
  每文件最多取 2 块，默认 6 条证据。
- 三种回答方式：`extractive`（默认，本地摘录，不联网）、`openai`（OpenAI 兼容 /chat/completions）、
  `ollama`（本机 /api/chat）。云端必须 `allow_cloud=true` 且有 key/model，否则 502 并说明原因。
- 只发送入选证据（每条 ≤900 字、总量 ≤8000 字），不发送完整文件；HTTP 用标准库 urllib，30 秒超时。
- 拒答：没有证据时返回 `refused=true / reason=no_evidence`，并附当前内容覆盖（已解析 / 待解析）。
- 引用校验：正则提取 [n]，缺失时提示"未引用任何证据"，越界编号列入 warning；回答为空视为失败。
- 设置存 `app_settings`（JSON）；`GET /api/settings/answer` 只返回 `has_key`，永不回显密钥。
- 前端「对话」视图：单轮提问、回答正文、来源卡片（文件名/页码/工作表/片段/打开）、回答设置面板。

### 对话管理
- 表：`chat_sessions(session_id, title, created_at, updated_at)`、
  `chat_messages(message_id, session_id, role, content, payload, created_at)`；
  `payload` 存助手完整 `AnswerResult`（含引用、覆盖、警告），删除会话级联删除消息。
- 接口：`GET/POST /api/chat/sessions`、`GET/PATCH/DELETE /api/chat/sessions/{id}`；
  `POST /api/ask` 增加可选 `session_id`，成功后在服务端保存一轮问答并返回 `session_id`。
- 标题：首轮提问自动截取（最多 24 字）；可手动改名。
- 前端「知识对话」：左侧会话列表（新建/切换/改名/删除），右侧按时间渲染历史问答与引用；
  当前会话 ID 存在 `localStorage["shiyuan.chat.active"]`，切换视图或重进页面后保持选中。
- 竞态保护：初始会话加载用请求序号与 `initialLoadRef`，用户先点击时不会被异步初始化覆盖。
- 局限：历史会保存，但提问不带上一轮上下文，仍是单轮检索。

### 多模态与 OCR（M5 补充）
- 双引擎：`Windows OCR`（系统自带）与 `RapidOCR + PP-OCRv6 small`（随包分发，约 30MB 模型 + onnxruntime）。
  系统默认仍是 Windows OCR；RapidOCR 只用于“整份扫描 PDF”，图片与文本型 PDF 继续走 Windows。
- 策略 `app_settings.ocr.policy`：
  - `auto`（默认）：整份扫描 PDF（`text_pages=0`）→ RapidOCR，缺页/文本型 PDF 与图片 → Windows；
  - `windows`：全部 Windows OCR；
  - `rapidocr`：全部 RapidOCR（不可用时回退 Windows）。
  引擎在文件级路由，L2 签名区分：`l2.v2:windows-ocr:...` / `l2.v2:rapidocr-v6-small:...`。
- 图片：`BitmapDecoder` 解码后直接识别；RapidOCR 图片/PDF 也走同一 `Section` 管道。
  PDF：Windows 用 `Windows.Data.Pdf` 渲染，RapidOCR 用 `pypdfium2` 渲染每页，单文件最多 30 页。
- 识别结果按行归一化（去掉汉字间空格、`1 e-5` → `1e-5`），并保留每行像素坐标
  `[x, y, w, h]`（`chunks.bbox`，JSON 数组）。
- 倾斜阅读顺序：RapidOCR 对倾斜长行可能拆成多段并按视觉位置返回；`order_rapid_items`
  用行的平均角度把坐标旋转到水平系，按基线聚类后再按 x 排序。
- 混合版式 PDF：L1 记录 `page_count / text_pages`，OCR 只补“缺少文本层”的页
  （`skip_pages` 为已有文本层的页码）；整份 PDF 文本层质量低时仍全页重识别。
- 触发方式：`POST /api/sources/{id}/ocr`（界面「OCR 深解析」）。与扫描共用 `scanner.request_scan`，
  同一数据源不并发；单轮最多处理 200 个文件，剩余数量写回 stats，可再次触发。
- 设置接口：`GET/PUT /api/settings/ocr`，返回策略与两个引擎的可用性；健康页可切换并显示状态。
- 覆盖率新增 `ocr_indexed / ocr_pending / ocr_empty / ocr_failed / mixed_pdf / ocr_policy /
  ocr_available / ocr_engine / ocr_languages`；`mixed_pdf` 只统计 `text_pages > 0`
  的部分缺页 PDF，整份无文本层的文档归入 `ocr_pending`。
- 离线引擎基准（2026-11-21，6 张合成样本，本机）：Windows OCR 平均 CER 12.3%、55ms/页、166MB 峰值；
  RapidOCR + PP-OCRv6 small 平均 CER 0%、646ms/页、547MB 峰值、冷启动 1.6s。
  Windows OCR 主要输在表格阅读顺序与数字/标点（如 `0.05` → `0·05`）。

### 知识库管理（M6）
- `index_jobs` 任务账本：每个 `(version_id, stage)` 一条记录，stage 为 `l1` / `l2`，
  记录 status（running / done / failed / pending）、attempts、error、updated_at。
  任务只在真正开始解析时 attempts +1。
- 失败自动重试：每次 `index_pending` / `index_ocr_pending` 开始时，删除 attempts < 3 的失败产物
  并重新排队；达到 3 次后保留失败记录，等待手动重试。
- `maintenance.py` 四种操作（均可指定 source_id，也支持全部 active 源）：
  - `reindex`：清空 L1 产物与 l1 任务，按当前文件重建正文索引；
  - `retry`：删除失败产物、attempts 归零，重跑 L1 + L2；
  - `ocr`：清空 L2 产物与 l2 任务，重跑 OCR；
  - `cleanup`：删除非当前版本的 artifacts / chunks / fts 与孤立任务。
  操作在后台单线程串行执行，每个源都先 `scanner.request_scan`，避免与扫描并发写库。
- 接口：`POST /api/maintenance/{reindex|retry|ocr|cleanup}?source_id=`（202），
  状态与缓存统计并入 `GET /api/stats` 的 `maintenance` 字段
  （jobs 计数、chunks、artifacts、db_bytes、running）。
- 前端健康页新增「知识库管理」面板：失败/排队任务、缓存块数、数据库大小、今日预算，
  四个操作按钮；运行期间按 1.5 秒轮询自动刷新。
- 每日预算（`budget.py`）：`app_settings.budget` 存 `{date, l1, l2}`，跨天自动归零；
  `index_pending` / `index_ocr_pending` 开始时按剩余额度处理，超出的留到下一天，并置
  `budget_exhausted` 写进维护日志。默认 500 / 200 个文件/天，`0` 表示不限。
- 低质量复核：L1 结果 `quality='low'` 的 PDF 会进入 OCR 候选（与 `state='empty'` 同等对待），
  OCR 块并入同一检索；覆盖率新增 `low_quality`。
- 任务列表：`GET /api/jobs?status=&stage=&limit=` 返回任务与文件路径；
  `POST /api/jobs/{id}/retry` 单文件重试（先清该版本产物、attempts 归零，再只跑这一条），
  写 `job_retry` 维护日志。健康页「失败任务」面板可逐条重试。
- 撤权时同步删除该源的 `index_jobs`，不残留任务记录。

### 打包与托管（M5）
- `core/sidecar.py`：PyInstaller 入口。`--noconsole` 冻结后 `sys.stdout/stderr` 为 `None`，
  入口先把两者重定向到 `%LOCALAPPDATA%\ShiyuanData\core.log`，否则 uvicorn 日志配置直接崩溃。
- 单文件构建命令（含 OCR 双引擎）：**v0.1.7 起已改用 onedir**——`python -m PyInstaller
  shiyuan-core.spec --noconfirm` 生成 `dist/shiyuan-core/`，整体复制到
  `desktop/src-tauri/binaries/shiyuan-core/`，由 `bundle.resources` 分发。下面这条 onefile
  长命令仅作历史记录：
  `python -m PyInstaller --onefile --noconsole --name shiyuan-core --paths core --hidden-import shiyuan.api --hidden-import shiyuan.mcp_server --collect-submodules uvicorn --collect-submodules winsdk --collect-data rapidocr --hidden-import rapidocr.inference_engine.onnxruntime --hidden-import rapidocr.inference_engine.onnxruntime.main --hidden-import rapidocr.inference_engine.onnxruntime.provider_config --collect-binaries onnxruntime --collect-data onnxruntime --exclude-module onnx --exclude-module torch --exclude-module paddle --exclude-module openvino --exclude-module tensorrt --exclude-module jupyter_client --exclude-module ipykernel --exclude-module zmq --exclude-module PyQt5 --exclude-module matplotlib --exclude-module pandas --exclude-module tkinter --collect-all pypdfium2 core/sidecar.py`
  - **不要用 `--collect-all rapidocr`**：会连带 PyTorch 后端与 CUDA DLL，单文件膨胀到 2.7GB；
  - 不要收 jupyter/ipykernel/zmq：pyzmq 自带的旧版 `msvcp140/vcruntime140` 会导致
    `onnxruntime_pybind11_state` 初始化失败（0x8007045A）。
  - 当前 onedir 目录约 390MB，NSIS 安装包 138MiB（含多格式解析、PP-OCRv6 small、
    onnxruntime 与 MCP 模式）；onefile 版本约 185MB / 180.2MiB。
- 产物复制为 `desktop/src-tauri/binaries/shiyuan-core-x86_64-pc-windows-gnu.exe`，
  `tauri.conf.json` 的 `bundle.externalBin` 引用之（该目录不入库）。
- 安装版前端 API 基址固定为 `http://127.0.0.1:8737`；不要用相对 `/api`，否则 Tauri 资源服务器
  会把请求当页面路由并返回 `index.html`，前端报 `Unexpected token '<'`。
- `src-tauri/src/lib.rs`：release 模式下探测 8737，未监听才 `shell().sidecar("shiyuan-core")`；
  句柄存 `CoreProcess` 状态，`RunEvent::Exit` 时 `kill`，避免遗留后台服务。
- 验证：NSIS 静默安装 → 启动 `Shiyuan.exe` → `/api/health` 返回 200 → 静默卸载。

### MCP 服务（`core/shiyuan/mcp_server.py`）
- stdio JSON-RPC 2.0，支持协议 `2025-06-18 / 2025-03-26 / 2024-11-05`；只读访问桌面端同一个
  SQLite 库，WAL 下可与桌面端同时运行。不扫描、不解析、不改原件。
- Tools：`search_knowledge`、`get_document_text`、`list_sources`、`knowledge_stats`；
  Resources：`shiyuan://sources`、`shiyuan://stats`。
- 取全文时按类型合并：PDF 缺文本层/低质量页优先用 OCR 块，PPTX 合并幻灯片正文与图片 OCR，
  其余类型优先 L1；支持按页码 / 工作表过滤与 `max_chars` 截断。
- 入口：`cd core && python -m shiyuan.mcp_server`；任意目录可用 `python <repo>/core/mcp_server.py`；
  `--print-config` 输出带当前 Python、core 目录与 `SHIYUAN_HOME` 的客户端配置片段。
- 已合入 `core/sidecar.py`：`shiyuan-core.exe --mcp` 进入 stdio 模式，默认不带参数仍启动 HTTP 核心；
  `--noconsole` 冻结后若 `sys.stdin/stdout` 为 None，用 `GetStdHandle` + `open_osfhandle` 兜底管道；
  冻结模式 `--print-config` 输出 `command=<exe>`、`args=["--mcp"]`。
  onefile 首次启动需解包（约 14 秒）；v0.1.7 的 onedir 版本握手约 1 秒。
- stdout 只输出协议消息，调试日志走 `SHIYUAN_MCP_DEBUG=1` 的 stderr。
- 测试 `core/tests/test_mcp.py`：握手、工具、资源、分块选择、stdio 帧、源码/冻结配置（12 项）。

### 召回增强（查询改写 + 可选向量混合召回）
- `query.py`：`analyze(text)` 返回 `{normalized, core, concepts, variants}`。
  - 归一化：全角转半角、小写、压缩空白；
  - 疑问词剥离：先去掉 `？?！!。，,` 等标点，再循环剥离「是多少 / 是什么 / 有哪些 / 怎么 / 如何 / 请问」等尾词；
  - 概念扩展：英文词、数字、中文 2/3-gram 生成变体；内置同义词表（样本量↔样本数↔样本数量、
    阈值↔threshold、质控↔QC 等）叠加用户自定义组，组内两两互指，并过滤「的 / 了 / 是」等停用词。
- `retrieval.py`：统一召回层 `find_chunks()`。变体全部 ≥3 字符时走 FTS5 `OR` 查询，含短词时退化
  `instr(lower(text), ?)` LIKE OR；再按"命中的概念数 / 变体数"排序，概念数 ≤2 时阈值 1，否则 2。
  长短变体并存时（如「毒鳍表里有哪些物种」），长词走 FTS、短词（「物种」）再走 LIKE 并合并，
  避免短词被 FTS5 trigram 的 ≥3 字符限制漏掉。
  `search` 与 `answer.retrieve` 共用这一层，避免两套评分漂移。
- 路径加分：`retrieval.path_matches` 把完整路径（文件夹名 + 文件名）纳入召回，按命中概念数、
  文件夹层级和文件名位置评分；文件夹名命中时会以该文件首段正文作为证据，但单靠
  「实验 / 数据 / 记录 / 报告」等通用目录词不会构成证据。`search` 结果新增 `matched_folder`。
  这样「pilot_batches 里有什么」「2026-07 的单据处理结果」能命中对应文件夹，
  而「量子纠缠实验结论」仍会拒答。
- `embeddings.py`：`provider = none / openai / ollama`。OpenAI 走 `/embeddings`（需
  `allow_cloud=true` 与 key），Ollama 默认 `http://127.0.0.1:11434`，优先 `/api/embed`，
  失败回退 `/api/embeddings`。向量以 float32 BLOB 存 `chunk_embeddings(chunk_id, model, dim, vector)`。
  - 补建：扫描结束后自动补建；健康页「补建向量索引」与 `POST /api/maintenance/embed` 手动触发。
  - 清理旧版本时同步删除孤立向量；换模型后旧模型向量不参与检索（按 model 过滤）。
- 混合合并：关键词与向量结果按 location 去重，相似度转成加分（`120 + similarity*100`），
  搜索项新增 `semantic` 布尔、`match="semantic"`；响应新增
  `rewritten / vector_used / vector_model / vector_error`，前端显示"已按改写后的查询「…」检索"。
  问答证据同样可来自语义命中，引用卡标「语义」。
- 设置：`GET/PUT /api/settings/embedding`（key 只回显 `has_key`，与回答设置同一套校验逻辑）。
  默认 `provider=none`，未开启时行为与旧版一致。
- 自定义同义词：`GET/PUT /api/settings/query`，`app_settings.query` 存 `{custom: [[词, …], …]}`；
  每组至少 2 个词、单词语长 ≤32、最多 50 组，空组自动丢弃；`query.synonym_map` 把自定义组与
  内置词表合并（组内互指），搜索与问答共用。前端「对话 → 回答设置 → 自定义同义词」按行编辑。
- 引用版本与时间：`retrieval.ChunkHit` 与语义命中都带 `version_id / mtime_ns`；
  `answer.Evidence` 写入 `version_id / mtime`，证据标题追加「更新于 YYYY-MM-DD HH:MM」，
  引用卡新增 `version_id / mtime`，前端显示「更新于 …」，提示词要求涉及时间条件时说明采用版本。
- 测试：`core/tests/test_recall.py` 12 项（疑问词剥离/同义词、同义词命中、改写提示、自定义同义词、
  引用版本时间、授权与密钥隐藏、向量构建计数、语义-only 命中合并、问答语义证据、API 设置与补建、
  未启用 400、同义词接口校验）。

## 三点五、本轮（v0.1.2）检索与对话修复

- `query.py`：新增疑问结构清理（`写了什么 / 有哪些 / 怎么用 / 放在哪个文件夹` 等），
  按「的」切词，剥句尾方位词；新增 `howto / intro` 意图；证据门槛按「最大词」计算，
  避免 `创新 / 新点 / 创新点` 这类 gram 膨胀把真正的专利文档过滤掉。
- `retrieval.py`：路径打分改用数据源内相对路径，数据源根目录名不再让整源文件加分；
  新增 `name_prior`（README / 使用说明优先，论文模板 / 撰写规范后置）；新增
  `maximal`（最大词命中数）与 `focus`（追问本轮词加权）排序；`path_only=True` 时
  只认具体路径命中，通用目录词不算。
- `answer.py`：多轮追问把上一轮问题并入检索（上一轮只涉及一个数据源时收窄到该源）；
  证据片段截到关键词附近；同一文件最多 1 条、同一家族最多 2 条证据；新增目录概览
  `folder_overview`；提示词注入当天日期，模型超时可用 `SHIYUAN_MODEL_TIMEOUT`
  调整（默认 60 秒）。
- `chat.py`：`recent_questions` / `recent_answer_sources`。
- 实测：「那第三章呢 / 那第四章呢」能回到论文对应章节；「大三下有哪些课」「大二有哪些课程」
  直接列出课程子目录（优先问题里点名的目录）；「专利的创新点」回到两份专利文件，不再被查重
  报告挤占；「怎么运行测试」优先 README；「我的课程表在哪」按路径找，找不到就拒答。
- 测试：后端 172 项通过。

## 三点六、本轮（v0.1.3）继续收敛检索噪声

- `retrieval.name_prior`：新增查重 / 比对报告降权（查重、重复率、简洁报告、全文对照、片段对照、
  AIGC 检测等）。普通问题里这些文件自动后置，只有问题本身在问查重时才正常召回。
- `answer._is_stat_question` / `_stats_answer`：合计 / 平均 / 总额 / 花了多少这类跨行汇总问题，
  不再交给模型从片段里推算，而是直接给出命中的表格与工作表，并明确说明"不做汇总"，避免编数字。
- `answer._folder_overview`：子目录 ≤ 8 时额外展开一层「目录 / 文件」名单，并列出目录直属文件名。
- `answer._messages`：提示词增加"不要跨行汇总或算术推算"约束。
- 实测：「专利的创新点」引用里不再出现查重报告；「上个月单据金额合计多少」「这个月单据花了多少」
  返回表格清单而非伪造总额；「大三下有哪些课」列出 8 门课并展开每门的课件与子目录。
- 测试：后端 175 项通过。

## 三点七、本轮（v0.1.4）检索规划与证据选择

- 新增 `planner.py`：四类意图（精确查找 / 知识解释 / 综合探索 / 历史追溯），
  给出证据预算（4 / 6 / 10 / 6）、同家族配额、最小家族覆盖与需要覆盖的关键词；纯规则，不调模型。
- `answer.retrieve` 按计划选择证据：
  - 先每家族取一条，凑够最小覆盖面，再补同家族第二条；
  - 4-gram 重合度 ≥ 0.88 的片段视为重复跳过；
  - 显式 `top_k` 优先于计划预算。
- `/api/ask` 新增 `plan` 字段：intent / label / budget / candidates / used / families / facets / missing。
- 提示词加入「检索模式 / 覆盖 / 缺口」，关键词无证据时要求明确说明资料不足。
- 前端回答下方显示「模式 · 候选/采用/家族 · 证据缺口」。
- 模型超时默认 60 → 90 秒，减少长证据集下的 502。
- 实测：BUSCO 完整度 → 精确查找 4 条并标注「完整度」无证据；专利创新点 → 6 条、2/2 覆盖；
  论文修改 → 历史追溯 6 条 / 5 家族。
- 已知：宽泛主题（如「实验室做过哪些鱼类研究」）在语料没有归纳材料时仍会召回相近文档，
  模型会拒答；下一步需要 Reranker / IDF 权重或主题聚类，而不是继续扩大 budget。
- 测试：后端 180、前端 14。

## 三点八、本轮（v0.1.4）模型重排

- 新增 `rerank.py`：可选模型重排。把前 50 条候选的文件名 + 200 字片段交给已授权的生成模型，
  要求返回 `{"relevant": [...]}`；只保留模型判为相关的候选，形成「最小充分证据集」。
- 只在「知识解释 / 综合探索」触发：精确查找预算只有 4 条、本地排序已足够；历史追溯需要保留多个版本，
  过滤反而会丢掉版本对照。未配置模型时完全不调用。
- 失败回退：超时（`SHIYUAN_RERANK_TIMEOUT` 默认 40s）、HTTP 错误、非 JSON、或模型返回空数组，
  都保持本地顺序，不阻断问答；空结果在界面显示「模型未筛出，按本地排序」。
- 模型特性：智谱 `glm-5.3-flash` 是常思考模型，`max_tokens` 低于 1000 时会被 `reasoning_content`
  吃光、`content` 为空；显式关闭思考会被 400 拒绝。重排请求固定给 2000 token。
- `/api/ask` 的 plan 增加 `reranked / rerank_kept / rerank_empty / rerank_error`；
  前端显示「候选 → 相关 → 采用」和「模型重排」标记。
- 实测（真实语料）：
  - 「实验室之前做过哪些鱼类研究」：51 候选 → 只留 1 条《褐蓝子鱼染色体水平基因组组装…》，5.9s；
    此前 10 条全是招生名单。
  - 「专利的创新点是什么」：63 → 留 2 条专利文档，12.1s；回答并列两个版本，并说明「创新点」无原文证据。
  - 「论文中 BUSCO 的完整度是多少」：精确查找不重排，仍 4 条 / 0.4s。
- 测试：后端 184、前端 14。

## 三点九、本轮 UI 换成 Stripe 风格（只换皮肤，不动排版）

- 全局 token：冷白画布 `#ffffff`、雾面侧栏 `#f8fafd`、Midnight Ink `#061b31`、
  Slate `#64748d`、Steel `#50617a`；唯一强调色 Indigo `#533afd`（hover `#7389ff`），
  淡紫底 `#e8e9ff`、描边 `#b9b9f9 / #d6d9fc`；边线统一 `#e5edf5`。
- 字体：Inter Tight → Inter → Segoe UI；正文 400，一级标题 300，字距 -0.01em ~ -0.02em，
  开启 `ss01 / tnum`。
- 形状：卡片、输入框、按钮统一 4px，标签保留 9999px；移除所有投影（含家族弹窗），
  层级只靠 1px 边线、雾面底和留白。
- 组件：主按钮 indigo 实心；次按钮 indigo 文字 + 淡紫描边；分段控件由胶囊改为 4px；
  导航与会话选中态改为 indigo 实心；用户消息气泡用 `#e8e9ff`。
- 保留：警告 / 错误仍用琥珀与红色语义色；布局、间距、栅格与页面结构未改。
- 验证：`npm test` 14 passed、`npm run build` 通过；Edge 无头截图检查对话 / 数据 /
  外部调用 / 更新四个页面。

## 三点十、本轮（v0.1.7）追问上下文、大文本与启动优化

- 追问上下文：`chat.recent_answer` 读取上一轮助手回答；`answer._resolve_reference` 支持
  「版本二（file.docx）」与「1. `path`」两种列举形式，以及「版本2 / 第二个 / v2」指代；
  `_selected_version_answer` 直接返回路径、更新时间、正文字数、片段数与开头预览。
  追问提示词加入上一轮回答（明确标注只用于理解指代、不作为证据），
  实测「Table S1 比较了什么」→「它有多少行」可落到 95,992 行。
- 大文本：`extractors.MAX_TEXT_SAMPLE_BYTES = 8MB`；纯文本超过 64MB 改为头部采样，
  正文末尾标注「已截断」。真实库中 86MB 的 BSA 结果表与 168MB 的 SNP 表恢复可检索，
  搜索「黄姑鱼 BSA 结果表」时表格文件排到第 1、2 位。
- 数据源点名：`answer._named_source` 只认「X 里/中/内/数据源」或「X 有哪些文件」，
  「A 论文有没有提到 B」不限定范围，避免把实验记录里的同名文档挡在外面。
- 解析状态：`indexer._classify_parse` 新增 `metadata_only`（仅登记）；
  压缩包、html、log 等按盘点策略只登记的类型不再计入「待解析」，
  前端解析结果页新增「仅登记」筛选、汇总卡与说明文案。
- 性能：`embeddings.warm_cache_async` 预热向量模型与 int8 矩阵；新增查询向量缓存
  （10 分钟 / 128 条）；重排与回答请求带 `reasoning_effort=low`。
- 分发：PyInstaller 改 onedir，Tauri 用 `bundle.resources` 分发并由 Rust 直接拉起，
  安装包 180MB → 138MB，MCP 握手 18s → 约 1s。
- 测试：后端 242、前端 17；问答评测主集 27/28、第三集 10/10（唯一失分为 Table S1 的
  `Contig` 细节，模型偶发不提）。

## 三点十一、本轮（v0.2.0）项目知识空间与范围检索

- 数据模型：新增 `projects` / `project_sources` / `project_assets`；`project_sources.path_prefix`
  支持只关联数据源内子目录。`SCHEMA_VERSION = 2`，升级前自动在线备份，
  只增表 / 补列，不删除旧表；真实库迁移 18.5 秒，计数一致、外键 0 违规、`quick_check` ok。
- 资产与索引复用：项目只建关系，不复制资产、不重复解析 / 切块 / 向量 / 摘要。
  同一论文加入 10 个项目后 `assets` / `chunks` / `chunk_embeddings` 均不变（自动化测试 + 真实库验证）。
- 扫描联动：`scanner` 完成位置更新后调用 `projects.sync_source_assets`，按前缀补主资料 /
  关联资料，保留已有排除；新增文件夹若已在授权数据源内则直接复用，不再重扫。
- 检索范围：Project / Expanded / Global；`answer` 以确定性条件逐级扩展
  （Project 证据 < 2 时扩到 Expanded，再扩 Global），只有真正补到证据才更新 `used`。
  路径召回、正文召回、语义召回都在项目范围内进行；语义查询先过滤再取 Top-K，
  并复用 int8 向量缓存做内存掩码，避免小项目漏召回。
- 修复：`find_chunks` 内部路径召回此前未带项目过滤，可能把项目外同名文件带入证据；
  空项目 / 全部排除时直接返回，不再空跑 FTS 与向量扫描。
- 性能：真实库 13,012 资产 / 382,678 chunk，三档检索中位约 2.1 秒；空项目 0ms；
  项目统计 2.5s → 0.61s（`chunks(asset_id, chunk_id)` 复合索引 + 改写统计 SQL）。
- API / UI：项目 CRUD、文件夹、资料分类、知识浏览、范围检索；桌面端新增「项目」页，
  对话页可选择项目与范围并显示实际使用范围；全局资料库与未归属文件保留。
- MCP：新增 `list_projects`、`get_project`、`search_assets`、`get_evidence`、
  `get_document_family`、`get_related_documents`、`compare_versions`、`get_source_status`；
  `project_id` 只允许 `external_allowed` 项目，`local_only` 对外部 Agent 不可见；MCP 保持只读。
- 测试：后端 273（新增项目模块 12 项，覆盖复用、子目录、多项目、三种范围、排除、
  语义范围、路径泄漏、关联资料、迁移幂等、数据源删除、API 全链路；另含全局项目、
  令牌绑定与版本新旧）；前端 17。
- 外部接口有用性（v3 补强）：
  - 版本新旧：`families._version_rank` 按数字段比较（0.13 > 0.9），
    `终稿 > 修订版 > 初稿`，无标签才退回 mtime；家族成员按版本新旧排序，
    返回 `is_latest_version` / `family_latest_version_label` / `family_latest_mtime`。
  - 检索：项目范围用 `VALUES` CTE 下推到 FTS 与路径召回（1564 资产约 20ms），
    修复“项目内英文稿件被全局中文笔记挤出候选”的召回问题；实测
    “BUSCO 完整度”前三条均为 v0.13，稿件正文可读到 98.4 / 96.4。
  - “最新版本”问句会补齐各大家族最新版本；指定版本 / 历史问句按版本分开，
    不再只留一个代表。点名旧版本时会把该版本成员补回候选（即使词面召回被挤占），
    并保留 `is_latest_version=false` 与 `family_latest_version_label`。
  - 无版本标签的家族只是重复副本：`is_latest_version` 返回 `null`，
    `list_document_families` 不再把它们列成“多版本文档”；`weak=true` 的结果
    只是弱线索，MCP instructions 已要求不得当作答案。
  - 新增 MCP `list_document_families`：列出多版本文档、最新版本与全部版本标签；
    `get_document_family` 标题修正、成员最新在前；`compare_versions` 可直接给出
    v0.12 → v0.13 的共享 / 新增 / 删除 / 修改摘要。
  - 评测发现：`manuscript_EN_v0.2~v0.7` 与 `CBPD_clean_v0.8~v0.13` 会被识别成两个家族，
    当前不自动合并；`list_document_families` 会同时列出，需按最新版本与时间判断。
- 全局项目与外部接口（v3）：
  - `projects.kind` 新增 `global`，固定 `prj_global`，不可删除 / 归档 / 排除；
    数据页新增文件夹天然属于全局项目，不写成员表。
  - 子项目详情新增「从全局项目拉取」，只建 `project_sources` 关系，复用现有扫描与索引。
  - `external_clients.project_id` 绑定项目；MCP 启动时解析令牌并注入 `MCPServer`，
    `list_projects` / 检索 / 证据 / 正文 / 家族 / 版本对比全部按绑定项目过滤；
    普通项目固定 `expanded`，不能通过 `project_id` 或 `scope=global` 越权。
  - HTTP 外部令牌同样按项目限权；`/api/events`、`/api/jobs` 不再向外部令牌开放。
  - 桌面端「外部调用」页合并进项目详情，导航由 5 项减为 4 项。
  - `chunk_embeddings.asset_id` 回填并加索引，项目统计从 0.61s 降到 25ms；
    全局项目统计约 0.69s。
  - 真实库 v2 → v3 迁移 52 秒，数据量不变，外键 0 违规。

## 四、禁止操作

- 不要在扫描中删除或重命名用户的原始文件；本项目对用户文件只读。
- 不要把 `parse_artifacts` / `chunks` 等未实现表提前建出来充数。
- 不要在 M1 引入向量库或嵌入式模型，先保证目录层可测。
- 不要绕过 `store.validate_root` 直接写 `sources` 表。

## 五、下一步（建议顺序）

1. 项目层：Privacy Gateway（数据分类、字段脱敏、统一出站检查）、项目统计增量计数表、
   项目归档 / 模板与批量成员调整。
2. 检索可靠性：补充 100–200 道真实用户问句与目标文件标注，再依据基线决定 ANN、Reranker
   或本地小模型；当前合成基线已满分，不能据此判断真实语料。
3. OCR 效果提升：手写识别、表格结构与版面还原、真实扫描件基准；按耗时/预算调度引擎。
4. 对话增强：更早轮次的记忆、引用跳转与对话导出。
5. 预算与任务：按耗时/字符数计量、任务取消与优先级、低质量文本的二次解析器。
6. 打包优化：按需下载 OCR 组件、安装体积与冷启动进一步压缩。
7. 格式增强包（DLC，暂缓）：默认只解析通用文件格式；生信等专用格式（fai/bed/gff/vcf/paf 等）
   以 `%SHIYUAN_HOME%\packs\<name>\pack.json` 形式声明 `扩展名 → 解析方式`，支持启停，
   内核不内置、不执行包内代码。`.assembly` 这类流程中间产物不纳入。
