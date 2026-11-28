# SourceLoom Phase 0：代码审计与基线冻结

> 审计时间：2026-11-28　基线版本：v0.3.6（commit `7392718`）
> 方法：逐模块读代码 + 交叉核对调用方与测试 + 真实库实测，不采信 README/PRD 中的设计目标。
> 证据格式：`文件:行号`（相对 `core/shiyuan/` 或仓库根）。

---

## 一、基线快照（真实库，本机）

| 项 | 数值 | 来源 |
| --- | --- | --- |
| 已授权数据源 | 5 个（可用文件 13,069） | `GET /api/stats` |
| 正文已索引 | 1,464（OCR 1,903） | 同上 |
| 待解析 / 失败 / 不支持 | 1,052 / 12 / 5,491 | 同上 |
| Chunk 总量 | 602,209 | 同上 |
| 解析产物 | 8,156 | 同上 |
| 向量条数 | 159,205（provider=ollama，bge-m3，top_k=8） | 同上 |
| 数据库体积 | 3,027.1 MB（WAL 已 checkpoint） | `data/shiyuan.db` |
| 项目 / 文档家族 | 1 / 见 `/api/families` | API |
| 单次 `/api/search` 延迟 | 2.1–4.2 s（冷启动 11.9 s） | 5 查询 × 3 次实测 |
| 后端测试 | 304 passed | `cd core; python -m pytest -q` |
| 前端测试 | 18 passed | `cd desktop; npm test` |
| 安装包 | `SourceLoom_0.3.6_x64-setup.exe`（138.6 MiB） | `dist/` |

检索延迟实测明细（`q` / 命中数 / 三次耗时 ms）：`样本量` 75 / 11868·3092·3109；`测序深度` 49 / 4201·4234·3817；`r&d 经费` 25 / 2459·2161·2100；`SSR 分布` 479 / 3360·3324·3331；`技术路线` 179 / 4167·3647·3745。

> 结论：当前规模（60 万 chunk）下检索已进入秒级，2.0 的「智能检索工作台」不能直接沿用现有链路。

---

## 二、现状矩阵

标记：**稳定** / **有问题** / **缺评测** / **部分实现** / **未实现** / **需重构**

### 资产与证据

| 模块 | 状态 | 依据 |
| --- | --- | --- |
| 扫描 | 有问题 | 子目录读取失败即判 MISSING，无宽限与复核（`scanner.py:229-232,451-455,496-500`） |
| 监听 | 部分实现 | 回退为周期全量对账，无 USN/句柄级事件（`watcher.py:75-81,198-208`） |
| 分类 | 缺评测 | 启发式规则 + 显式格式清单，无成功率基准（`inventory.py:239`） |
| 指纹 | 有问题 | >32MB 用伪哈希；哈希不参与去重，mtime 变即重新解析（`scanner.py:208,300-311`） |
| 解析 | 缺评测 | 约 120 扩展名，DOCX/PPTX/XLSX 表格已转 markdown（`extractors.py:31-60,187-212`），无格式级成功率评测 |
| OCR | 有问题 | locked 状态不重排（`indexer.py:166-188,227-228`） |
| 切块 | 需重构 | 纯字符 800/重叠 100/上限 4000，无语义与结构边界（`indexer.py:39-43`） |
| 图片 | 部分实现 | 只存 OCR 文本 + 行 bbox，无图像语义索引（`db.py:162-174`） |
| 向量 | 部分实现 | 全表 BLOB 扫描 + 余弦计算，无 ANN；带项目过滤时全表（`embeddings.py:848-857,887-922`） |
| 预算 | 有问题 | 手动「完整解析」仍受 500/日硬限，建库也记账（`scanner.py:156-160`、`indexer.py:318,340,662`） |

### 存储

| 模块 | 状态 | 依据 |
| --- | --- | --- |
| FTS5 | 稳定 | trigram tokenizer，BM25 排序（`db.py:184-189`、`retrieval.py:527-532`） |
| 中文分词 | 部分实现 | ≥3 字符走 trigram；2 字词回退全表 `instr`，自注 3–4 s（`retrieval.py:533-557`） |
| 正文存储 | 有问题 | `chunks.text` + FTS 双份；低质 PDF 的 L1/L2 整页重复（`indexer.py:594-604`）；家族代表文本再存一份（`db.py:281-288`） |
| 索引一致性 | 有问题 | FTS 靠隐式 rowid 对齐（`retrieval.py:165`）；cleanup 删 chunk 不清 FTS（`maintenance.py:173-176`）；向量不删旧模型与孤儿（`embeddings.py:342-351,395-401`） |
| Schema | 部分实现 | 29 张表；`assets.kind/lifecycle`、`sources.policy` 等只写不读；`content_versions.observed_at`、`index_jobs.updated_at`、`chunks(version_id,ordinal)` 缺索引 |

### 检索

| 模块 | 状态 | 依据 |
| --- | --- | --- |
| 全文检索 | 稳定 | FTS5 + 两字兜底，120 篇合成语料 R@1/R@5/MRR=1.000（`test_retrieval_benchmark.py`） |
| 路径检索 | 有问题 | `instr(lower(path))` 全表扫最多 2 万行 + Python 逐条打分（`retrieval.py:413-466`） |
| 向量检索 | 部分实现 | 可开关；`_is_lookup` 判定为编号查询时直接跳过语义（`search.py:227,40-49`） |
| 多通道融合 | 需重构 | 整数权重相加而非 RRF（`search.py:278-355`），语义分被文件名 1000+ 压死 |
| 查询理解 | 部分实现 | 疑问词/口水词剥离 + 内置同义词，无纠错与消歧（`query.py:240-288`） |
| 查询扩展 | 部分实现 | 2/3-gram + 词表，无伪相关反馈（`query.py:264-275`） |
| 重排 | 部分实现 | 每次仅 12 条、40s 超时后回退，无本地 cross-encoder（`rerank.py:16,161-194`） |
| 多轮检索 | 未实现 | 只有会话追问拼接（`answer.py:1731,1790-1797`），无线索扩展 |
| 层级检索 | 未实现 | 无 Document→Section→Chunk 结构；仅 page/sheet 定位 |
| 主题检索 | 未实现 | 只有规则 facets（`planner.py:85-104`） |
| 证据选择 | 需重构 | 预算/配额/阈值散落在 planner/answer/rerank 四处硬编码（`planner.py:38-45`、`answer.py:306-331,1928`、`rerank.py:16`） |

### 知识、Agent 与安全

| 模块 | 状态 | 依据 |
| --- | --- | --- |
| 无模型运行 | 稳定 | 默认 `extractive`，建库/检索/抽取式问答不触模型（`answer.py:40-50,358`） |
| Chat | 部分实现 | 代码耦合轻（`chat.py` 仅 import store），但会话逻辑硬绑 `/api`（`api.py:711-737,803-840`） |
| 知识抽取 | 部分实现 | summary/entities/relations/FAQ 三类；关系 `evidence` 恒空、FAQ 无实体外键（`knowledge.py:365,388`） |
| 知识生命周期 | 未实现 | 知识本体无 candidate/active/stale/version，只有任务状态（`knowledge.py:783-811`、`db.py:331`） |
| MCP | 有问题 | 工具较全，但权限检查分散 5 处、进程启动后令牌撤销不生效（`mcp_server.py:664,964,1362-1417`） |
| 外部令牌 | 有问题 | HTTP 逐请求校验，MCP 仅启动校验（`external.py:184-190,216-228`） |
| 权限隔离 | 有问题 | 知识注入不过滤项目（`answer.py:525-595`）；`local_only` 只在创建令牌时拦截（`external.py:151-153`） |
| 脱敏 | 未实现 | 无任何字段掩码 |
| 调度 | 有问题 | maintenance 与 knowledge worker 双线程抢任务，pending 无原子 claim（`knowledge.py:461-476`、`maintenance.py:28-39`） |
| 审计日志 | 未实现 | 外部调用只有计数（`external.py:207-213`） |
| 评测覆盖 | 缺评测 | 现有基线为合成唯一标记语料；不覆盖向量、重排、项目隔离、失效证据、精确率 |

---

## 三、高优先级风险（必须先修）

### P0-1 撤销授权不彻底，派生数据仍可暴露
`store.remove_source()` 只清理 `project_assets / chunks / FTS / artifacts / content_versions / assets`，
**不清** `chunk_embeddings`、`document_family_members`、`family_content_sources`、`knowledge_*`
（`store.py:322-336` 对比 `drop_locations` 的 `store.py:298-307`）。孤儿数据只在手动维护清理时删除
（`maintenance.py:173-184`）。同时 MCP 只在进程启动时校验令牌（`mcp_server.py:1362-1417`），
运行中的 stdio 会话在令牌撤销后仍可继续读取。→ 违反验收标准「撤销权限的证据不能继续被访问」。

### P0-2 暂停的数据源仍可被「注入」通道命中
`_fetch_locations` 只取 `l.*`，不过滤 `sources.state`（`search.py:160-174`）；
「最新版本」与「点名旧版本」两条注入路径只检查 `availability`（`search.py:371-376,387-409`），
`families.py:253` 同样。→ 暂停源的文档可能被当作最新版本返回。

### P0-3 `local_only` 在运行时没有兜底
`get_document_text` 不检查项目隐私策略（`mcp_server.py:664-668`）；HTTP 全链路无运行时 privacy 校验
（`api.py:178-189`）；知识抽取把最多 2 万字符正文送外部模型且不按项目过滤
（`knowledge.py:144-165`）。→ `local_only` 只是创建令牌时的提示，不是强制边界。

### P0-4 每日预算与「完整解析不设硬上限」冲突
`SHIYUAN_DAILY_PARSE_BUDGET` 默认 500（`config.py:73-80`），手动「完整解析」同样受限
（`scanner.py:156-160`、`indexer.py:318,662`），且建库过程本身也记账（`indexer.py:340`）。
`locked` 状态永不重排（`indexer.py:166-188,227-228`）。→ 大库会长期停在「待解析」。

### P0-5 索引一致性没有统一机制
FTS 通过隐式 rowid 与 chunks 对齐（`retrieval.py:165`）；清理任务删 chunks 不删 FTS
（`maintenance.py:173-176`）；向量按 chunk_id upsert、不清理旧模型与孤儿
（`embeddings.py:342-351,395-401`）；解析旧版本先删块再写新块（`indexer.py:232`），形成检索空窗。
→ 需要 Outbox 式事件表和一致性检查，否则 2.0 换存储引擎时会把问题放大。

### P1-1 检索性能与融合策略
实测 2–4 s。主因：路径通道全表 `instr` + 2 万行 Python 打分（`retrieval.py:413-466`）；
2 字词 LIKE 扫 60 万 chunk（`retrieval.py:533-557`）；每请求至少两次家族全表 + 逐条 N+1
（`search.py:276,480`、`families.py:786,1081`）；向量全表扫描（`embeddings.py:848-899`）。
融合是整数相加而非 RRF（`search.py:278-355`）。

### P1-2 家族与版本
最新版只按文件名标签 + mtime（`families.py:653-700`）；检索每家族只保留一个代表
（`search.py:444-460`），家族内独有段落只在回答层按配额 1–2 保留（`answer.py:322-331`）。
→ 与「家族独有内容不能被错误过滤」存在冲突，需要验证后再改。

### P2 其他
扫描误判 MISSING（`scanner.py:451-455`）；`assets.kind/lifecycle` 等死字段；`/api/health` 回显 db 路径
（`api.py:282`）；外部令牌可 `POST /api/ask/stream` 触发云端调用且无限流（`api.py:746`）。

---

## 四、可复用 / 需重构判断

**可直接复用（2.0 的基础）**
- SQLite 权威库 + WAL、资产/位置/版本三层模型（`db.py`、`store.py`）
- 显式格式清单 + 多格式解析 + 表格 markdown + OCR 双引擎（`extractors.py`、`ocr.py`）
- FTS5 trigram + BM25 正文召回（`db.py:184-189`、`retrieval.py:527-532`）
- 项目关系表（primary/linked/excluded）与 SQL 下推（`projects.py:522-550`、`retrieval.py:182-208`）
- MCP 工具面与令牌绑定模型（`mcp_server.py`、`external.py`），补权限统一层即可

**需要重构**
- 检索融合与证据选择（改 RRF + 统一 Evidence 结构 + 预算集中管理）
- 预算/调度（区分「用户显式全量解析」与「后台增量」）
- 索引生命周期（事件表 + 一致性检查 + 撤销级联）
- 知识层（证据外键、候选/核验状态、时序字段）
- Chat 退出主入口，替换为检索工作台

**暂不需要**
- 更换数据库（当前 FTS5 在 60 万 chunk 上仍可用，先修查询与融合）
- 引入 Neo4j / 独立图数据库（关系用 SQL JOIN/递归 CTE 足够）

---

## 五、与任务书 Task 01–10 对照

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| 01 审计与基线冻结 | **完成** | 即本文档 |
| 02 研究 SAG2/QMD/Zvec/Zvec-Grep/MiniRAG/PageIndex | **P0 完成** | SAG2/zvec-grep/Zvec 已有 commit 级笔记；QMD/MiniRAG/PageIndex 仍待核实 |
| 03 真实检索评测集 + Benchmark Runner | **完成** | 126 题（29 curated + 97 auto）、R0/R1/RRF/消融报告、可复现校验 |
| 04 基础建库不依赖生成式 LLM、解除强制向量化 | **完成** | 默认 `provider=none` 全链路不调模型；向量可关；显式全量解析不受日预算 |
| 05 本地 BM25、混合检索、Reranker、多轮线索 | 完成（实验结论） | 编号桥接、类型路由均无 hit@1 增益（默认关闭）；本地 1.5B 模型重排窗口 5/10/20 全部降低 hit@1（默认关闭） |
| 06 检索消融实验 | **完成（4 通道）** | 路径+全文 / +向量 / +家族 / +向量+家族；更大参数矩阵待做 |
| 07 FTS5/Tantivy/Zvec/Qdrant/LanceDB 对照 | 完成（实测） | `storage_bench.py` 跑 2 万/10 万/59 万（FTS5/Tantivy/Zvec/LanceDB）；结论不迁移，Qdrant 因 sidecar 形态不必要 |
| 08 统一证据接口、项目过滤、索引失效 | **完成（结果级 + source 级）** | `range`/`coverage`/`truncated`/`stale` 已进 search 与 MCP；`freshness` 提供 source 级改动/积压/失败水位，检索工作台与 MCP 均可见 |
| 09 检索工作台替代 Chat 主入口 | **完成（工作台）** | 默认页改为检索；项目/范围/数据源/类型/时间过滤、证据定位、家族归并、复制证据、加载更多；Chat 保留为次级入口 |
| 10 Agent MCP 对照评测 | **完成（首轮 A0–A5）** | A0/A1 原生文件工具、A4/A5 SourceLoom 同题实测；A2/QMD、A3/Zvec-Grep 未安装标记 untested；Token 依赖 Agent 模型未测 |

---

## 六、下一步（建议顺序）

1. ~~修 P0-1～P0-4~~（已完成，见第七节）。
2. ~~Task 02（P0 三项）与 Task 03 评测底座~~（已完成）。
3. ~~Task 05 剩余部分~~：桥接、类型路由与本地模型重排窗口均已实验，均无 hit@1 增益（默认关闭）。
4. ~~Task 08 统一证据接口~~：`range`/`coverage`/`truncated`/`stale` 已进 search 与 MCP。
5. ~~Task 07 实际跑分~~：2 万 / 10 万 / 59 万完成（含 LanceDB），结论不迁移（见第八节 Task 07）。
6. **下一步**：2 字词 P95 延迟（bigram 倒排/按需分片）；Task 08/09/10 已完成（见第八节）。
7. **可选复测**：有模型环境时验证 Reranker 窗口；补齐 A2/A3 与 Token 指标。

> 未验证项：外部 Agent 模型下的最终答案/Token、百万级压测、真实用户问句质量、Zvec 增量删除/恢复、Qdrant sidecar。

---

## 七、本轮已执行的修复（2026-11-28，未发布）

| 风险 | 修复 | 代码位置 |
| --- | --- | --- |
| P0-1 撤销不彻底 | `remove_source` / `drop_locations` 统一走 `_purge_asset_derived`：清 chunk 向量、家族成员与段落来源、文档级知识；空家族连同家族级知识/任务/分组一起删除 | `store.py:273-324,349-354,375-381` |
| P0-1 MCP 令牌撤销不生效 | 每次 `tools/call` 与 `resources/read` 复核 client 状态与项目隐私 | `mcp_server.py:139-169,538-546,218-224,1311-1313,1335-1348,1434-1448` |
| P0-2 暂停源可被注入 | `_fetch_locations` 增加 `sources.state = 'active'` 过滤，覆盖最新版本/点名版本注入通道 | `search.py:160-175` |
| P0-3 local_only 非硬边界 | HTTP 外部令牌逐请求校验项目隐私；知识生成遇到远端模型 + local_only 范围直接跳过并记录原因 | `api.py:121-131,179-197`、`knowledge.py:142-186,508-519` |
| P0-4 预算硬限 | 显式扫描/单文件重试使用 `ignore_budget`，不占每日额度；`locked` 产物下一轮无条件重排；失败仍受 3 次上限 | `scanner.py:159-165`、`indexer.py:166-180,340-342,684-685,792-796` |

回归测试：新增 7 项（撤销级联、暂停源注入、运行中撤销、运行时改 local_only、显式解析不占预算、
locked 重试、消融开关），全量 **304 passed**。

## 八、Task 03 评测底座与 R0 基线

- 新增 `core/benchmarks/retrieval_eval.py`：JSONL 问句集 → hit@1 / recall@5 / recall@10 / MRR / 无答案准确率 / 延迟分位，只读数据库。
- 真实问句集保存在 `$SHIYUAN_HOME/evals/`（不入库，含本地个人信息）；仓库内只保留合成示例。
- 检索已加消融开关 `use_vector` / `use_family`（产品默认全开），便于按通道对比。

**消融结果（v0.3.6，12 题种子集）**

| 通道组合 | hit@1 | recall@5 | recall@10 | MRR | P50 |
| --- | --- | --- | --- | --- | --- |
| 全开 | 0.50 | 0.70 | 0.80 | 0.58 | 5.2 s |
| 路径+全文 | 0.30 | 0.30 | 0.70 | 0.35 | 4.7 s |
| +向量 | 0.60 | 0.60 | 0.70 | 0.61 | 5.6 s |
| +家族（无向量） | 0.30 | 0.50 | 0.70 | 0.37 | 4.9 s |

> 该基线确认了三件事：无答案问题在检索层没有拒答能力；版本类问题只能排到 5–10 位；
> 当前延迟不适合作为桌面检索工作台。另有两个结论：向量主要提升 hit@1，家族主要提升召回宽度。

**延迟归因（实测，2 条查询）**

| 阶段 | 耗时 |
| --- | --- |
| 查询理解 `query.analyze` | <1 ms |
| 路径召回 `retrieval.path_matches` | 54–73 ms |
| 正文召回 `search._content_hits` | **2,874–7,012 ms** |
| 向量召回（冷 / 热） | 9,093 ms / 310 ms |
| 家族映射 `families.family_map` | 15 ms |

正文召回是主因：FTS5 trigram 只支持 ≥3 字符，两字词回退为 `instr` 全表扫描 60 万 chunk
（`retrieval.py:533-557`，代码注释已标注 3–4 s）。路径通道只有几十毫秒，此前"路径全表扫"
的判断需要修正。

**第一轮优化**：两字兜底按需触发、最多 3 个短词、中文去掉 `lower()`、3 字母缩写改走 FTS。

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| R0 P50 | 5,217 ms | 853 ms |
| R1 P50 | 4,741 ms | 281 ms |
| R1 P95 | 11,249 ms | 2,522 ms |
| R0 hit@1 | 0.50 | 0.60 |
| R1 MRR | 0.352 | 0.437 |

召回未下降（R0 R@5/10 = 0.70/0.80），全量 304 passed。遗留：无答案长句在向量模式仍需约 9.7 s，
原因是加载 159k 向量矩阵后 SQLite 页缓存被挤出、兜底扫描转为磁盘读；Phase 1 用向量分片/按需加载
与拒答门槛解决。

**扩展基线（29 题，标注可接受多个来源文件）**

| 指标 | R1 路径+全文 | R0 全开 |
| --- | --- | --- |
| hit@1 | 0.40 | 0.40 |
| recall@5 | 0.48 | 0.76 |
| recall@10 | 0.64 | 0.80 |
| MRR | 0.460 | 0.560 |
| 无答案准确率 | 0.00 | 0.50 |
| P50 | 319 ms | 804 ms |

向量 + 家族主要提升召回宽度与 MRR，不改善 hit@1。新增**拒答门槛**（第一轮）：
问题含连续短语时，零散 2-gram 命中不再算强证据，必须命中短语、编号或路径具体词；
无答案准确率从 0/4 提升到 2/4（当时 305 passed）。剩余 1 例「2022 年冬奥会…成绩单」
已由下文第二轮概念覆盖门槛解决。

**扩展基线（126 题：29 curated + 97 auto，`gen_questions.py` 自动生成）**

| 指标 | R1 路径+全文 | R0 全开 |
| --- | --- | --- |
| hit@1 | 0.467 | **0.582** |
| recall@5 | 0.607 | **0.812** |
| recall@10 | 0.705 | **0.861** |
| MRR | 0.534 | **0.673** |
| 无答案准确率 | 1.00 | 1.00 |
| P50 / P95 | 150 / 2,744 ms | 501 / 2,961 ms |

> 同一份数据库快照（SQLite backup 冻结；桌面端运行时活库会持续变化）。
> 同一快照重复两次 rank/top 差异为 **0**。
report：`data/evals/reports/snap_default.json`、`snap_r0.json`（本地，不入库）。
按来源（R0 全开，只统计可答题）：curated 29 题（可答 25）hit@1 0.440 / R@10 0.840 / MRR 0.600；
auto 97 题 hit@1 0.619 / R@10 0.866 / MRR 0.691。auto 题是「字段：数值」模板句，分数天然偏高，
**对外结论仍以 curated 为准**；P95 仍是向量模式长问句的 3–5 s 量级，属于已记录的页缓存/向量加载遗留问题。

**Task 06 通道消融**（126 题，同一快照）

| 通道 | hit@1 | R@5 | R@10 | MRR | P50 |
| --- | --- | --- | --- | --- | --- |
| 路径 + 全文 | 0.467 | 0.607 | 0.705 | 0.534 | 150 ms |
| + 向量 | 0.566 | 0.762 | 0.836 | 0.657 | 439 ms |
| + 家族 | 0.500 | 0.746 | 0.803 | 0.593 | 162 ms |
| + 向量 + 家族（默认） | **0.582** | **0.812** | **0.861** | **0.673** | 501 ms |

向量是 hit@1/MRR 的主要来源；家族单独使用弱于向量，但叠加后仍能提高 hit@1 +1.6 pt、
R@5 +5.0 pt、R@10 +2.5 pt。两条通道都不是纯冗余，默认全开有数据支撑。

**Task 05 多轮线索实验（`--bridge`，默认关闭）**

规则版编号桥接（accession / `ChrNN` / `vX.Y`，第二轮 +350 分）在同一快照上：
hit@1 0.582 → 0.549、R@5 0.812 → 0.795、MRR 0.673 → 0.648，P50 501 → 820 ms，curated 不变。
桥接召回的是「同编号但非答案」的文件，反而挤占 top1；SAG2 的增益依赖决策模型做线索选择，
纯规则无法复现。保留 `--bridge` 作为可复现实验，产品默认关闭。

**Task 05 类型路由实验（`--routing`，默认关闭）**

开放自然问题不做家族归并、保留全部版本；版本/编号/计数问题仍归并并注入最新版。同一快照：
hit@1/R@5/R@10 仍为 0.582/0.812/0.861，MRR 0.673 → 0.669。没有 hit@1 增益，默认关闭。
结论：Task 05 的三项离线实验均不改变默认排序。Reranker 已在本机 Ollama 上实测（见下）。

**Task 05 重排窗口实验（`benchmarks/rerank_window.py`，默认关闭）**

本机 Ollama `qwen2.5-coder:1.5b-local`、同一快照、20 题分层子集，把检索前 N 条候选交给模型按相关性
重排（temperature 0、seed 42）：

| 窗口 | baseline hit@1 | reranked hit@1 | baseline MRR | reranked MRR | 改善 / 变差 | 平均重排耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 0.50 | 0.30 | 0.638 | 0.468 | 2 / 10 | 573 ms |
| 10 | 0.50 | 0.35 | 0.643 | 0.538 | 1 / 4 | 804 ms |
| 20 | 0.50 | 0.35 | 0.643 | 0.509 | 0 / 6 | 1,107 ms |

结论：1.5B 代码模型不具备稳定的中文相关性判断，三个窗口都降低 hit@1 与 MRR；窗口 10 的损失最小。
因此重排保持默认关闭，产品不引入该模型；若要继续，应换专用 reranker（如 bge-reranker）单独评测。

**Task 07 存储对照实测（`benchmarks/storage_bench.py`）**

正文词面检索、同一抽样语料、同一 122 题（不含路径/家族/向量）：

| 规模 | 后端 | R@10 | P50 / P95 | 构建 | 索引 |
| --- | --- | --- | --- | --- | --- |
| 59.2 万 | SQLite FTS5 | **0.631** | 12.6 / 97.7 ms | 111 s | 1.38 GB |
| 59.2 万 | Tantivy ngram(2,3) | 0.541 | **2.7 / 26.6 ms** | **53 s** | **370 MB** |
| 59.2 万 | LanceDB 原生 FTS ngram(2,3) | 0.566 | 269.5 / 1,410 ms | 49 s | 662 MB |
| 10 万 | LanceDB 原生 FTS ngram(2,3) | 0.549 | 39.2 / 180.3 ms | **9.6 s** | 118 MB |
| 10 万 | Zvec ngram(2,3) | 0.557 | 53.9 / 325.5 ms | 277 s | 2.35 GB |

结论：**不迁移**。SQLite 召回最高且零依赖；Tantivy 延迟/体积约为 SQLite 的 1/4，但 R@10 低 9 pt，
未接 jieba 与路径通道，不满足预设门槛；LanceDB 构建/体积优于 FTS5，但 R@10 低 6.5 pt、
P50 约 21 倍，不满足桌面延迟预算；Zvec 在 10 万规模构建 277 s、索引 2.3 GB，
线性外推 60 万约 28 min / 14 GB，超出桌面端预算。Qdrant 需要独立 sidecar，与单文件桌面形态冲突，
在本轮结果下不构成必要选项。增量删除/并发/恢复未测，迁移门槛未满足。
详见 `docs/research/storage_benchmark.md` 第八、九节。

**修复：版本注入主题化**（`search.py` 的 `_topic_matched` + `_inject(base=1200)`）

原来「手稿目前最新的版本是哪个」会把各家族最新版一起注入，再按家族大小排序，
与查询主题无关的「整理图表.zip / 毕业论文终稿」反而靠前。现在注入前先按查询主题
（同义词变体，排除「最新/版本」）过滤资产路径与家族标题，并提高明确问版本时的注入优先级。
效果：version 类 hit@1 0 → 0.333。同时修正 q22 标注（库内最新为 v0.13，原标注 v0.12 已过时）。

**修复：拒答门槛第二轮**（`retrieval.is_numeric_term` + `search.py` 的 `need_terms`）

1. 纯数字短语（「2022」）不再算主题短语；
2. 没有可判定短语时，长问题必须命中 `max(2, ceil(30% × 概念数))` 个概念（正文或路径）才算强证据。

无答案准确率 **0/4 → 4/4**，排序不变；新增 `test_long_query_with_only_generic_path_match_is_not_strong`。

**无 LLM 可运行（验证）**

默认 `embedding.provider=none` 时，扫描/解析/切块/FTS5 索引/检索均不调用模型；新增
`test_search_and_index_work_without_embedding_model` 断言索引后 `chunk_embeddings=0`、
检索仍返回结果且 `vector_used=False`。衍生知识未配置模型时返回「暂不生成」而不阻塞检索。
`314 passed`。

**修复：评测可复现性**（`search.py` 的 `_rank_key`）

同分同 mtime 的候选原来依赖 SQL/插入顺序，跨进程会出现 1–2 题 rank 抖动；改为按
`(path, location_id)` 稳定排序后，连续两次 126 题评测的 rank/top 差异为 **0**。
`retrieval_eval` 的 by-source / by-type 指标改为只对可答题计算 hit@1，并输出 `answerable` 题数。

**修复：家族版本计入命中**（`retrieval_eval._match_rank`）

家族归并后只展示一个代表，原评测只看代表文件，导致同一家族里实际含答案的版本被算成未命中
（family 通道的收益被系统性低估）。现在 `family_versions` 展开的版本也算该名次命中：
默认配置 hit@1 0.549 → **0.582**、R@5 0.795 → 0.812（当前快照），curated 不变。
新增 `test_family_versions_count_as_hit_at_same_rank`。

**修复：统一证据接口（Task 08 初版）**

- `search` 每个 item 新增 `range`：PDF/OCR 为页号，纯文本为 `char_start/char_end` 区间
  （`_SELECT` 与 path-only 回退查询都补齐了偏移列）。
- `search` 每个 item 新增 `stale`：文件在当前版本解析后又被修改（location mtime 新于
  `content_versions.mtime_ns`）或不可用时为 true；顶层返回聚合值。
- MCP `search_knowledge` 顶层新增 `coverage`（`ranked_sample`/`truncated`）、`truncated`、`stale`，
  item 透传 `range` 与 `stale`，工具描述同步更新；REST `/api/search` 直接继承。
- source 级新鲜度（Task 08 补齐）：`search.index_freshness` 统计每个数据源「文件改动但未重索引」
  的数量、pending/running 任务与失败任务；`/api/freshness`、`GET /api/stats.freshness`、
  MCP `knowledge_stats` 与 `get_source_status` 均返回；检索工作台在顶部提示索引落后。
- 与 zvec-grep 的 `coverage/range` 契约对齐；项目范围下 freshness 按项目资产过滤。
- 新增 `test_search_marks_stale_when_file_changed_after_index`、
  `test_index_freshness_tracks_changed_files_and_jobs`、`test_stats_includes_source_freshness`；
  全量 **322 passed**。

**RRF 融合实验**（`search(fusion="rrf")` + `retrieval_eval --fusion rrf`，126 题）

| 融合方式 | hit@1 | R@5 | R@10 | MRR |
| --- | --- | --- | --- | --- |
| 整数加权（默认） | **0.582** | **0.812** | 0.861 | **0.673** |
| RRF 正文×2 K=60 | 0.557 | 0.787 | 0.861 | 0.661 |

RRF 只提升 auto 题的 R@10，curated 的 R@10 与 hit@1 反而下降，故默认保持整数加权；
`--fusion rrf` 作为可复现实验开关保留，后续可用于「先扩大召回再精排」。

**Task 09 检索工作台（2026-11-28）**

- 导航默认页从「对话」改为「检索」，检索位于第一项；Chat 不删除、不迁移数据，作为次级入口保留
  （`desktop/src/App.tsx`、`desktop/src/views/SearchView.tsx`）。
- 工作台能力：自然语言/文件名/路径/精确关键词统一查询框；项目 + 范围（主资料/关联/全局）、
  数据源、文件类型、时间范围过滤；结果展示 `range`（页码或正文偏移）、`stale`、弱相关、
  语义标记与同族版本；按文档家族归并；复制结构化证据（Markdown）；`offset` 按需加载更多。
- 后端时间过滤：`/api/search?mtime_days=N` 换算为纳秒下界，传入 `search(..., mtime_after_ns=...)`，
  同时约束路径召回、FTS 正文、语义向量；向量全库缓存不参与带时间过滤的查询
  （`api.py`、`search.py`、`retrieval.py`、`embeddings.py`）。
- 核心检索链路不需要任何外部模型：默认 `provider=none`，全文/路径可独立工作，向量与生成均为可选。
- 测试：新增 `test_search_mtime_filter_applies_to_path_and_content` 与
  `test_api_search_translates_mtime_days`；后端 **322 passed**；前端 `vitest` **21 passed**，
  `tsc --noEmit` 与 `vite build` 通过。
- 未做：删除旧 Chat 入口与公共模型接口（按任务书要求先以开关方式停用/降级）。

**Task 10 Agent A0–A5 首轮对照（2026-11-28）**

新增 `core/benchmarks/agent_eval.py`：同一问句集、同一快照下比较工具策略，输出证据命中、
任务成功、工具调用次数、工具输出字符（上下文成本代理）、耗时、无效/重复/越权读取。
A2（QMD）/A3（Zvec-Grep）未安装，明确标记 untested；Token 依赖 Agent 模型，未测。

24 题分层子集（seed + auto 等距抽样；A0/A1 每题 8 s 原生搜索预算）：

| 对照 | 任务成功 | hit@1 | hit@10 | MRR | 平均调用 | 平均输出字符 | P50 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A0 原生文件搜索 | 0.125 | 0.087 | 0.130 | 0.094 | 1.0 | 31,341 | 3,501 ms |
| A1 A0 + 读取前 3 个文件 | 0.125 | 0.087 | 0.130 | 0.094 | 3.83 | 36,140 | 3,069 ms |
| A4 SourceLoom 基线 R0 | 0.667 | 0.348 | 0.652 | 0.450 | 1.0 | 1,102 | 194 ms |
| A5 SourceLoom 混合 + 读证据 | **0.875** | **0.696** | **0.870** | **0.754** | 3.0 | 3,447 | 563 ms |

126 题全量（A4/A5，同一快照）：

| 对照 | 任务成功 | hit@1 | hit@10 | MRR | 无答案准确 | P50 | P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A4 基线 R0 | 0.714 | 0.467 | 0.705 | 0.534 | 1.0 | 191 ms | 2,981 ms |
| A5 改造后 | **0.865** | **0.582** | **0.861** | **0.673** | 1.0 | 472 ms | 2,977 ms |

- 复现：同一快照重复运行，A4/A5 的逐题 `rank` 与 `top` 差异均为 **0**。
- 成本：A5 用 3 次工具调用、约 3.4k 字符换取 +15.1 个百分点的任务成功；
  A0/A1 在原生搜索下证据命中低，且无法对无答案问题拒答。
- 同主题连续任务（25 组）：A4 首次→后续 hit 0.60→0.71，A5 0.84→0.89；
  这只是同主题后续题更集中，**不代表动态知识积累**（本轮不写入新知识）。
- 未测：A2/A3、外部 Agent 的最终答案正确率与 Token、动态知识飞轮写入。

### 回滚路径

- 本轮改动**未提交**（按用户要求），可整体丢弃工作区或按文件回滚：
  `core/shiyuan/{search,retrieval,indexer,scanner,store,knowledge,mcp_server,api,embeddings}.py`、
  `core/benchmarks/`、`core/tests/`、`docs/audit/`、`docs/research/`、
  `desktop/src/{App.tsx,Icon.tsx,api.ts,styles.css,App.test.tsx}`、`desktop/src/views/SearchView.tsx`。
- Task 10 只新增 `core/benchmarks/agent_eval.py` 与测试，不进入产品运行路径；删除即可回滚。
- Task 09 的入口切换集中在 `App.tsx` 的 `NAV` 与 `useState<View>("search")`；
  回滚只需把默认值改回 `chat` 并移除检索项，`SearchView.tsx` 可整体删除。
- 时间过滤是 `mtime_after_ns` 可选参数：`search`/`path_matches`/`find_chunks`/`semantic_chunks`
  在参数为空时 SQL 与缓存路径完全不变，回滚不影响既有检索与 Benchmark。
- RRF 为显式 `fusion="rrf"` 开关，默认 `weighted`，不影响产品链路；删除该分支即可回到原打分。
- 版本注入主题化与拒答门槛均新增独立函数（`_topic_matched`、`need_terms`），
  回滚时只需恢复 `search.py` 的对应块，不涉及数据迁移。
- 评测集与报告均在 `data/evals/`（gitignored），删除不影响仓库；仓库内仅保留合成示例与生成脚本。
