# Task 02：开源方案研究（研究口径与进度）

> 目标：为 SourceLoom 2.0 的检索、存储、知识组织选型提供**可复核**依据，而不是 README 摘抄。
> 状态：骨架已建立；SAG2 / zvec-grep / Zvec 已完成源码或接口契约级研读（见 `sag2.md`、`zvec-grep.md`、`zvec.md`），其余项目待核实。

## 一、研究口径（硬性）

1. **固定版本**：每个项目记录仓库地址、commit/发布版本、许可证；不写"最新版"。
2. **只写验证过的结论**：能力、性能、许可证必须有代码/文档/官方评测出处；未验证写"待核实"。
3. **禁止直接集成**：不整体引入框架；只抽取算法、数据结构与工程设计。
4. **许可证审查前置**：复制代码前确认许可证与分发限制；不复制来源不明片段。
5. **区分"复现"与"参考"**：模型不可获得时标注 `SAG2-inspired` 等，不声称完整复现。
6. **评测驱动**：任何"更好"的结论必须由 `core/benchmarks/` 的对照实验支撑。

## 二、已核实事实（2026-11-28）

| 项目 | 仓库/来源 | 许可证 | 已核实要点 | 状态 |
| --- | --- | --- | --- | --- |
| SAG2 | [arXiv 2610.22588](https://arxiv.org/html/2610.22588) · [Zleap-AI/SAG-Benchmark@bb7f827](https://github.com/Zleap-AI/SAG-Benchmark) | MIT（已核实） | 无 Embedding 建库、查询时决策模型；FTS5 索引签名、jieba+2/3-gram、DPP 重排；MuSiQue/2Wiki/HotpotQA R@10 86.62 / 97.43 / 99.00 | **已读源码** → [sag2.md](sag2.md) |
| Zvec | [alibaba/zvec@2b1e274](https://github.com/alibaba/zvec) · [zvec.org](https://zvec.org) | Apache-2.0（已核实） | v0.8.0；嵌入式向量库 + 原生 FTS；Flat/HNSW/RaBitQ/DiskANN/IVF；中文 Jieba+HMM；Segment 内 RocksDB + BitPacked 倒排 | **已读文档** → [zvec.md](zvec.md) |
| LanceDB | [lancedb/lancedb](https://github.com/lancedb/lancedb) | Apache-2.0（已核实） | v0.41.0；原生 FTS（0.41 已移除 tantivy 后端）；`FTS(base_tokenizer="ngram", 2–3)` 可做中文；列存 + 向量索引 | **本地实测** → [storage_benchmark.md](storage_benchmark.md) |
| zvec-grep（zg） | [zvec-ai/zvec-grep@08e93f1](https://github.com/zvec-ai/zvec-grep) | Apache-2.0（已核实） | CLI + MCP 统一 ripgrep/BM25/向量；工具输入上限、coverage/freshness/range 返回契约 | **已读 MCP 契约** → [zvec-grep.md](zvec-grep.md) |
| QMD | `github.com/tobi/qmd` | 待核实 | 本地模型 + 查询扩展 + 混合检索 + RRF + 重排 | 待读源码 |
| PageIndex | `github.com/VectifyAI/PageIndex` | 待核实 | 文档树 / 章节定位 / 层级检索 | 待读源码 |
| MiniRAG | `github.com/HKUDS/MiniRAG` | 待核实 | 小模型 + 异构图辅助检索 | 待读源码 |
| RAGFlow | `github.com/infiniflow/ragflow` | 待核实 | 结构化解析、切块、知识编译 | 待读源码 |
| Docling | `github.com/docling-project/docling` | 待核实 | 统一文档结构、表格、版面、来源定位 | 待读源码 |
| Recoll | [recoll.org](https://www.recoll.org/) | 待核实 | 本地文件索引、复杂格式、增量处理 | 待读源码 |
| Octop Memory | `github.com/TencentCloud/octop-memory` | 待核实 | 候选知识、原子知识、主动/被动召回 | 待读源码 |
| Graphiti | `github.com/getzep/graphiti` | 待核实 | 时序知识图谱、事实失效、增量更新 | 待读源码 |
| GraphRAG / LazyGraphRAG | `github.com/microsoft/graphrag`；[LazyGraphRAG 博客](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/) | 待核实 | 全局检索、概念网络；按需语义理解 | 待读源码 |

> 许可证"待核实"表示尚未看到仓库 LICENSE 文件，不能据此复制代码，也不得在文档中声称其许可证。

## 三、单项记录模板

研究每个项目时按以下结构落盘（建议 `docs/research/<project>.md`）：

```text
# <项目> 研究笔记
- 仓库 / commit / 版本：
- 许可证与分发限制：
- 核心技术（一句话）：
- 关键模块（文件 → 作用）：
- 数据结构与索引设计：
- 检索/排序/融合算法：
- 模型依赖与资源占用：
- Windows 部署与打包可行性：
- 官方评测口径（数据集、指标、数值）：
- 可复用点（落到 SourceLoom 的哪个模块）：
- 不适合 SourceLoom 的部分：
- 未验证能力：
```

## 四、与 SourceLoom 的映射与优先级

| 优先级 | 研究项 | 对应 SourceLoom 模块 | 期望产出 |
| --- | --- | --- | --- |
| P0 | SAG2（多跳线索扩展） | `retrieval.py` / `search.py` / `planner.py` | 线索提取与停止条件的规则化设计（不依赖 LLM） |
| P0 | zvec-grep（Agent 检索接口 + 评测） | `mcp_server.py` / `benchmarks/` | 工具返回结构、Token 预算、A0–A5 对照口径 |
| P0 | Zvec（嵌入式 FTS + 向量） | `db.py` / `embeddings.py` | 是否可替代 FTS5+Qdrant 的可行性结论 |
| P1 | QMD（混合检索与重排） | `search.py` / `rerank.py` | RRF 权重与重排候选窗口的实验参数 |
| P1 | PageIndex（层级） | `indexer.py` 切块 | 文档树的最小可用实现（标题/书签/表格） |
| P2 | MiniRAG / Foundry IQ / GraphRAG | `knowledge.py` / `families.py` | 主题与实体组织的低成本方案 |
| P2 | Octop Memory / Graphiti | 知识生命周期 | 候选知识、失效与冲突处理模型 |

## 五、本轮已建立的评测底座（Task 03）

- `core/benchmarks/retrieval_eval.py`：JSONL 问句集 → Recall@k / MRR / 无答案准确率 / 延迟分位
- 真实问句集放在 `$SHIYUAN_HOME/evals/`（**不入库**，含本地个人信息）
- 在线示例：`core/benchmarks/datasets/retrieval/example.jsonl`（合成标签）

**R0 基线（v0.3.6，12 题种子集，真实库 60 万 chunk）**

| 通道组合 | hit@1 | recall@5 | recall@10 | MRR | 无答案准确率 | P50 / P95 |
| --- | --- | --- | --- | --- | --- | --- |
| R0 全开（路径+全文+向量+家族） | 0.50 | 0.70 | 0.80 | 0.58 | 0.00 | 5.2 s / 15.6 s |
| R1 路径+全文 | 0.30 | 0.30 | 0.70 | 0.35 | 0.00 | 4.7 s / 11.2 s |
| R2 +向量 | 0.60 | 0.60 | 0.70 | 0.61 | 0.00 | 5.6 s / 13.1 s |
| R3 +家族（无向量） | 0.30 | 0.50 | 0.70 | 0.37 | 0.00 | 4.9 s / 10.6 s |

解读：
- **向量是 hit@1 的主要来源**（0.30 → 0.60），但对 recall@10 没有增益；
- **家族主要提升召回宽度**（R@5 0.30 → 0.50），全开时 R@10 才到 0.80；
- **延迟主因是正文召回的两字词兜底**：`_content_hits` 实测 2.9–7.0 s（`instr` 全表扫 60 万 chunk），
  而路径召回只有 54–73 ms；
- **无答案问题全部误命中**：检索层没有拒答能力，必须在 Phase 1 增加证据门槛。

### 第一轮性能优化：两字词兜底按需触发

`retrieval.find_chunks` 原来只要查询里含两字词就执行 `instr` 全表兜底；实际上 FTS trigram
已经能覆盖查询中的三字及以上片段。四处改动（`retrieval.py:513-590`）：

1. 只有**纯短词查询**、或 FTS 候选明显不足时才兜底；
2. 兜底最多扫 3 个短词（缩写优先），避免长问句十几个 2-gram 线性叠加；
3. 中文没有大小写，兜底直接 `instr(c.text, ?)`，不再对每个短词重复 `lower()` 全表；
4. 3 个字母的编号（SSR/CDS）改走 FTS + 整词边界过滤，不再拖进全表兜底。

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| R0 全开 P50 | 5,217 ms | **853 ms** |
| R1 路径+全文 P50 | 4,741 ms | **281 ms** |
| R1 P95 | 11,249 ms | **2,522 ms** |
| R0 hit@1 | 0.50 | **0.60** |
| R1 hit@1 | 0.30 | **0.40** |
| R1 MRR | 0.352 | **0.437** |

召回未下降（R0 recall@5/10 仍为 0.70 / 0.80），全量测试 304 passed。
**遗留**：无答案长句（q11）在向量模式仍需 9.7 s——不是兜底本身，而是加载 159k 向量矩阵后
SQLite 页缓存被挤出，全表兜底扫描转为磁盘读。该问题在 Phase 1 通过「向量分片/按需加载 +
更强拒答门槛」解决，而不是再建一份倒排索引。

> 种子集只有 12 题，只能作为方向性基线，不能当作统计显著结论；扩到 100–200 题后再复核。

### 扩展集（29 题）与新的 R0

在 12 题基础上，从真实语料里补了 17 题（精确数值、accession、版本、结构化计数、无答案），
标注改为**可接受的多个来源文件**（同一事实常同时出现在 highlights / manuscript / 审稿意见里，
只标一个文件会造成假失败）。

| 指标 | R1 路径+全文 | R0 全开 |
| --- | --- | --- |
| hit@1 | 0.40 | 0.40 |
| recall@5 | 0.48 | **0.76** |
| recall@10 | 0.64 | **0.80** |
| MRR | 0.460 | **0.560** |
| 无答案准确率 | 0.50 | 0.50 |
| P50 | 320–455 ms | 660–1003 ms |

结论：向量 + 家族把 recall@5 从 0.48 拉到 0.76、MRR 提升 0.10，但**不改善 hit@1**。
剩余失败集中在两类：主题相近但不含答案的文档挤占前十、以及纯编号问题（SSR/CDS）。

### 拒答门槛（检索层，两轮）

第一轮：问题里出现**有意义的连续短语**（如「土壤成分分析」「小说手稿」）时，只命中零散 2-gram 不算证据；
纯数字短语（「2022」）不再算主题短语，否则任意含 2022 的表格都会被当成强命中（`retrieval.is_numeric_term`）。

第二轮：没有可判定短语时，按**概念覆盖率**兜底。长问题（>2 个概念）必须命中
`max(2, ceil(30% × 概念数))` 个概念，或路径命中覆盖同样多的概念，才算强证据；
只命中「2022」「成绩单」这类单一通用词面不算（`search.py` 的 `need_terms`）。

效果：无答案准确率 **0/4 → 4/4**，召回与排序不变（309 passed）。新增两条回归测试：
`test_phrase_query_requires_phrase_hit_for_strong_evidence`、
`test_long_query_with_only_generic_path_match_is_not_strong`。

结论：精确事实类 2 题未进前十；版本类已能命中但常在第 5–9 位；延迟仍高于桌面工作台目标（< 1 s），
这三项是 Phase 1 剩余靶子。

### 无 LLM 可运行（默认路径）

- 默认 `embedding.provider = none`：扫描、解析、切块、FTS5 索引与检索全程不调用模型，`vector_used = False`。
- 新增回归测试 `test_search_and_index_work_without_embedding_model`：索引后 `chunk_embeddings` 为 0，
  检索仍返回结果且 `vector_error` 为空。
- 衍生知识（L2）在未配置模型时返回「未配置生成模型，衍生知识暂不生成」，不阻塞检索
  （`knowledge.run_round` 的 `generated_enabled` 分支）。
- SAG2 的查询时决策模型只作为可选增强，不进入默认链路。

### 扩展集（126 题：29 curated + 97 auto）

新增 `core/benchmarks/gen_questions.py`：从真实 chunk 里挑「字段：数值」行生成精确问句，不需要模型；
过滤个人字段、日期/编号、DNA 基序、表格碎片，并把同一事实出现的**全部版本/摘录**写进 `expect`
（只标一个文件会造成假失败）。生成结果只写 `$SHIYUAN_HOME/evals/`，不入库。

| 指标 | R1 路径+全文 | R0 全开 |
| --- | --- | --- |
| hit@1 | 0.467 | **0.582** |
| recall@5 | 0.607 | **0.812** |
| recall@10 | 0.705 | **0.861** |
| MRR | 0.534 | **0.673** |
| 无答案准确率 | 1.00 | 1.00 |
| P50 / P95 | 150 / 2,744 ms | 501 / 2,961 ms |

> 以上数字来自**同一份数据库快照**。桌面端运行时会持续扫描/索引，直接评测活库会出现 1–2 题漂移；
> `--snapshot` 用 SQLite backup 冻结数据后再跑，同一快照重复两次 rank/top 差异为 **0**。

按来源拆分（R0 全开，hit@1/R@10/MRR 只统计可答题）：

| 来源 | 题数 | 可答 | hit@1 | R@10 | MRR |
| --- | --- | --- | --- | --- | --- |
| curated（真实自然问句） | 29 | 25 | 0.440 | 0.840 | 0.600 |
| auto（模板化数值问句） | 97 | 97 | 0.619 | 0.866 | 0.691 |

2026-11-28 追加修复：

1. `最新版本 / 点名版本` 注入只保留与查询主题匹配的家族，并提高注入优先级
   （原来问「手稿最新版」会被「整理图表.zip」挤到第 9）。version 类 hit@1 从 0 提升到 0.333。
2. 评测改为可复现：同分同 mtime 按 `(path, location_id)` 稳定排序；并提供 `--snapshot`
   （SQLite backup 冻结活库）。同一快照连续两次评测 rank/top 差异为 **0**。
3. 评测口径修正：家族归并只展示一个代表，但 `family_versions` 里展开的版本仍可被用户看到，
   因此它们也算该名次命中（`_match_rank`）。修正后 family 通道的增益才被正确计入。
4. 无答案题不再计入 by-source 的 hit@1（`answerable` 字段单独给出），避免正确拒答被算成召回下降。

### Task 06 通道消融（126 题，同一快照）

| 通道 | hit@1 | R@5 | R@10 | MRR | P50 |
| --- | --- | --- | --- | --- | --- |
| 路径 + 全文 | 0.467 | 0.607 | 0.705 | 0.534 | 150 ms |
| + 向量 | 0.566 | 0.762 | 0.836 | 0.657 | 439 ms |
| + 家族 | 0.500 | 0.746 | 0.803 | 0.593 | 162 ms |
| + 向量 + 家族（默认） | **0.582** | **0.812** | **0.861** | **0.673** | 501 ms |

结论：向量是 hit@1/MRR 的主要来源（+9.9/+12.3 pt）；家族单独使用时不如向量，
但叠加在向量之上还能再提高 hit@1 +1.6 pt、R@5 +5.0 pt、R@10 +2.5 pt。
两条通道都不是纯冗余，默认全开有数据支撑；代价是 P50 从 150 ms 升到 501 ms。

结论：向量 + 家族在 126 题上把 hit@1 提升 0.115、R@10 提升 0.156、MRR 提升 0.139，
与 29 题结论一致。auto 题偏「字段名直接可搜」，分数明显高于 curated，**对外结论仍以 curated 为准**，
auto 只用于扩大覆盖面与回归保护。P95 仍在 3–5 s 量级，来源是向量模式下的长问句（见上方遗留问题）。

### Task 05 多轮线索实验（`--bridge`）

规则版「编号桥接」：从第一轮前 5 个高覆盖片段抽取 accession / `ChrNN` / `vX.Y`，
第二轮检索后以固定 +350 分合入，不调用模型。结果（同一快照）：

| 配置 | hit@1 | R@5 | R@10 | MRR | P50 |
| --- | --- | --- | --- | --- | --- |
| 默认 | **0.582** | **0.812** | 0.861 | **0.673** | 501 ms |
| + 编号桥接 | 0.549 | 0.795 | 0.861 | 0.648 | 820 ms |

结论：桥接召回了「同编号但非答案」的文件，挤占了 top1，且延迟翻倍；curated 不变。
SAG2 的多轮增益依赖决策模型判断「哪条线索值得跟」，纯规则无法复现同等的选择性。
因此**默认关闭**，`--bridge` 作为可复现实验保留。

按问题类型路由（`--routing`）：开放式自然问题不做家族归并（保留全部版本），
版本/编号/计数问题仍归并并注入最新版。同一快照：

| 配置 | hit@1 | R@5 | R@10 | MRR |
| --- | --- | --- | --- | --- |
| 默认 | **0.582** | **0.812** | **0.861** | **0.673** |
| + 类型路由 | 0.582 | 0.812 | 0.861 | 0.669 |

hit@1/R@5/R@10 不变，MRR 略降，说明「家族归并」在开放问题上没有挡住最佳答案；
因此默认关闭。

本地模型重排窗口（`benchmarks/rerank_window.py`）：本机 Ollama
`qwen2.5-coder:1.5b-local`、同一快照、20 题分层子集，temperature 0 / seed 42：

| 窗口 | baseline hit@1 | reranked hit@1 | baseline MRR | reranked MRR | 改善 / 变差 | 平均耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 0.50 | 0.30 | 0.638 | 0.468 | 2 / 10 | 573 ms |
| 10 | 0.50 | 0.35 | 0.643 | 0.538 | 1 / 4 | 804 ms |
| 20 | 0.50 | 0.35 | 0.643 | 0.509 | 0 / 6 | 1,107 ms |

结论：1.5B 代码模型的中文相关性判断不稳定，三个窗口都降低 hit@1；窗口 10 损失最小。
Task 05 的三项实验（桥接、类型路由、本地重排）均无正向收益，全部默认关闭；
若要继续，应换专用 reranker 单独评测。

### RRF 混合融合实验（`--fusion rrf`，126 题）

实现：`search(fusion="rrf")` 取路径/正文/向量三路名次，按 `sum(w / (K + rank))` 融合
（默认 K=60，正文权重 ×2），业务先验（编号、家族、成稿、代码降权）仍按原权重叠加；
仅用于消融，产品默认 `weighted`。

| 融合方式 | hit@1 | R@5 | R@10 | MRR | curated hit@1 / R@10 | auto hit@1 / R@10 |
| --- | --- | --- | --- | --- | --- | --- |
| 整数加权（默认） | **0.582** | **0.812** | **0.861** | **0.673** | **0.440** / **0.840** | **0.619** / 0.866 |
| RRF 正文×2 K=60 | 0.557 | 0.787 | **0.861** | 0.661 | 0.400 / 0.800 | 0.598 / **0.876** |

结论：RRF 只在 auto 模板题上提高 R@10，curated 的 R@10 与 hit@1 都下降，因此**不作为默认**。
更合适的用法是 Phase 2 把 RRF 当作候选生成器（先扩大 R@10，再用分类/重排精排），而不是直接替代现有打分。
报告：`data/evals/reports/snap_*.json`（同一快照，本地，不入库）。

### Task 08 统一证据接口（初版）

对齐 zvec-grep 的「证据可定位」契约：

- `search` 的每个 item 新增 `range`：PDF/OCR 为 `{"kind":"page","page":N}`，
  纯文本为 `{"kind":"text","startOffset":N,"endOffset":N}`（来自 `chunks.char_start/char_end`）。
- `search` 每个 item 新增 `stale`：文件在当前版本解析之后又变过（location mtime 新于
  `content_versions.mtime_ns`）或已不可用时为 `true`；顶层同时返回 `stale = any(item.stale)`。
- MCP `search_knowledge` 顶层新增 `coverage`（`ranked_sample` / `truncated`）、`truncated`、`stale`，
  item 透传 `range` 与 `stale`；工具描述同步说明。
- source 级新鲜度：`search.index_freshness` 统计每个数据源的改动未重索引文件、pending/running
  任务与失败任务，经 `/api/freshness`、`/api/stats.freshness`、MCP `knowledge_stats` /
  `get_source_status` 暴露；项目范围按项目资产过滤。
- 未实现：`rg_exhaustive` 类穷尽模式（当前检索仍是排序采样）。
- 测试：`test_search_and_read_document` 断言 `range`/`coverage`，
  `test_search_marks_stale_when_file_changed_after_index` 覆盖 `stale`，
  `test_index_freshness_tracks_changed_files_and_jobs` 覆盖 source 级水位；全量 **322 passed**。

### Task 09 检索工作台（产品层）

检索取代 Chat 成为默认主入口，工作台直接展示证据而不是默认生成答案：

- 查询框同时覆盖自然语言、文件名、路径与精确关键词；过滤支持项目/范围、数据源、类型、时间。
- 结果卡片展示 `range`（页号或正文偏移）、`stale`、弱相关、语义、同族版本，可复制结构化证据。
- 时间过滤为服务端过滤：`mtime_days` 在 API 转成纳秒下界，贯通路径、FTS 与向量三条召回通道。
- 按 `offset` 加载更多；家族归并仅影响展示，不改变后端排序与 Benchmark。
- 顶部提示 source 级索引新鲜度（改动待重索引 / 任务积压 / 失败任务）。
- Chat 与外部模型配置保留为次级能力，核心检索不依赖任何外部模型。

### Task 10 Agent A0–A5 首轮对照

同一快照、同一问句集下比较工具策略，脚本为 `core/benchmarks/agent_eval.py`。
A2（QMD）/A3（Zvec-Grep）未安装，报告标记 untested；Token 依赖 Agent 模型，未测，
用工具输出字符数作为上下文成本代理。

24 题分层子集（A0/A1 每题 8 s 预算）：

| 对照 | 任务成功 | hit@1 | hit@10 | 平均调用 | 平均输出字符 | P50 |
| --- | --- | --- | --- | --- | --- | --- |
| A0 原生文件搜索 | 0.125 | 0.087 | 0.130 | 1.0 | 31,341 | 3,501 ms |
| A1 A0 + 读前 3 个文件 | 0.125 | 0.087 | 0.130 | 3.83 | 36,140 | 3,069 ms |
| A4 SourceLoom R0 | 0.667 | 0.348 | 0.652 | 1.0 | 1,102 | 194 ms |
| A5 SourceLoom 混合 + 证据读取 | 0.875 | 0.696 | 0.870 | 3.0 | 3,447 | 563 ms |

126 题全量（A4/A5）：任务成功 0.714 → 0.865，hit@1 0.467 → 0.582，
hit@10 0.705 → 0.861，MRR 0.534 → 0.673；两次快照逐题 `rank`/`top` 差异均为 0。
结论：混合检索 + 证据读取显著优于原生文件搜索与词法基线，但 A5 的 P95 仍约 3 s，
主要来自两字词 LIKE 兜底。报告保存在 `data/evals/reports/agent_a4a5_126*.json`（本地）。

## 六、下一步

1. ~~按 P0 顺序读 SAG2 / zvec-grep / Zvec 源码~~（已完成，见 `sag2.md`、`zvec-grep.md`、`zvec.md`）。
2. ~~扩真实问句集到 100–200 题~~（已到 126 题）；下一步补 `forbidden_sources` 与 `gold_evidence` 字段。
3. ~~设计并实跑存储横评~~（FTS5 / Tantivy / Zvec / LanceDB，2 万 / 10 万 / 59 万，
   见 `storage_benchmark.md` 第八、九节）；结论不迁移，Tantivy 留作「延迟/体积」备选，
   LanceDB 构建/体积占优但延迟与召回归还不足，Qdrant 因 sidecar 形态不必要。
4. ~~R0/R1/RRF 对照~~（见上文与 PHASE0_AUDIT）；延迟 P95 仍是 2 字词兜底导致的 3 s 量级。
5. ~~Task 09 检索工作台 / Task 10 Agent 对照 / source 级新鲜度~~（已完成，见上文）；
   下一步：2 字词 P95 延迟优化，并在有 Agent 模型时补 A2/A3 与 Token 指标。
6. 延迟改进候选：2 字词 bigram 倒排或按需分片，目标 P95 < 1 s。
