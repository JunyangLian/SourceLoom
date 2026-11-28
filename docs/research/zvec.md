# Zvec 研究笔记

- 仓库 / commit / 版本：[alibaba/zvec](https://github.com/alibaba/zvec) @ `2b1e274d65303e3c870c8180a557948fa9a51cf3`（main）；当前文档版本 **v0.8.0**
- 许可证与分发限制：Apache-2.0（仓库 `LICENSE`、README badge）。可商用，需保留 NOTICE/许可声明。
- 核心技术（一句话）：进程内（in-process）向量数据库，把 dense/sparse 向量、原生全文检索、标量过滤和混合检索放进同一个嵌入式存储，类 SQLite 的使用方式。
- 关键能力（已核实来源：README、官方 FTS 设计博客、索引文档）：
  - 向量索引：Flat / HNSW / HNSW-RaBitQ / DiskANN / IVF / IVF-RaBitQ。
  - 原生 FTS：`FtsIndexParam(tokenizer_name, filters)` 挂在字符串字段上；`match_string` 走自然语言，`query_string` 支持 `+search -vector`、`"full text"` 短语、`AND NOT`。
  - 中文分词：v0.8.0 内置 Jieba 词典与 HMM 模型，无需额外词表文件；英文用 standard tokenizer + lowercase。
  - 混合检索：一次 `query` 传多条 Query（FTS + 向量），可配 `RrfReRanker(rank_constant=60)` 或加权融合；标量过滤尽量下推。
  - 持久化与并发：WAL；多进程可同时读，写为单进程独占。
  - 平台：Linux / macOS / Windows / Android / iOS；v0.8.0 增加 Intel Mac、Windows ARM64，DiskANN 支持 Windows，RaBitQ 支持 Windows x86_64。
  - SDK：Python（3.10–3.14）、Node、Go、Rust、Java、Dart。
- FTS 存储与查询设计（官方博客，**未读 C++ 源码**）：
  - FTS 作为 Segment 内的新字段索引，不引入外部服务；每个 Segment 可开一个 RocksDB 实例管理多个 FTS 字段。
  - 写阶段：新 doc_id 写成单元素 Roaring Bitmap，经 RocksDB MergeOperator 做 OR 合并，保持追加写、降低写放大。
  - 只读阶段：持久化时一次性转成自定义 BitPacked 倒排（doc_id、tf、文档长度、块内最大分打包在一起）。
  - 维护位置列表（短语匹配）与 Segment 级统计（avgdl 等）用于 BM25。
- 官方评测口径（厂商自测，**未由本项目复现**）：
  - 环境：Apple M3 Pro、11 并发查询线程、topk=10；BEIR 四个数据集（2 中文 + 2 英文）。
  - 相对 Elasticsearch 8.15.0：QPS 提升 1.8x–22.3x；中文数据集提升最大（22.3x / 13.9x）。
  - 相对 SQLite FTS5 3.53.3（`MATCH OR + bm25`）：Quora QPS 1,525 vs 5.7（约 268x），ArguAna 561 vs 10（约 56x）。
  - Recall@10 与两者大致持平：Quora 0.8450 vs SQLite 0.8451；ArguAna 0.7226 vs 0.7383。
  - 延迟（p95）：Quora 18.8 ms vs SQLite 4,023.4 ms；ArguAna 52.8 ms vs 2,661.3 ms。
  - 注意：上述是厂商博客口径，不能直接当作本项目 60 万 chunk 的真实收益；Task 07 需要自测。
- 可复用点（落到 SourceLoom）：
  1. 同一存储内「FTS + 向量 + 过滤」与统一 doc_id 返回 → 可替代目前 SQLite FTS5 + 自建 int8 向量矩阵的双写。
  2. `match_string` / `query_string` 双入口 → 工作台可把自然语言与精确短语分开走不同通道。
  3. 写 Roaring / 读 BitPacked 的分阶段结构 → 即使不引入 Zvec，也可借鉴到我们的向量矩阵分片与倒排布局。
  4. `RrfReRanker(rank_constant=60)` 与我们的 RRF 实验参数一致，可作为默认候选参数。
  5. 过滤下推 → 项目/来源过滤应尽量进入存储层，避免「先全局召回再内存过滤」。
- 不适合 / 风险：
  - C++ 原生依赖，Windows 安装包体积、签名与崩溃隔离都要重新评估；当前 onedir + NSIS 分发会变复杂。
  - 引入第二套存储意味着迁移、备份、恢复、事务边界都要重做；现有「单 SQLite 文件可备份」优势会丢失。
  - 与本项目「本地优先、无需服务」一致，但需要验证中文混合术语、项目过滤、增量删除的实际表现。
- 未验证能力：60 万 chunk 的导入/查询实测；中文 FTS 对中英混排的效果；多进程读写细节；DocIterator 全量遍历接口；与现有 `chunk_id`/证据接口的映射成本。

## 与 SourceLoom 的对照结论

- Zvec 是目前最接近「单库同时做 FTS+向量+过滤」的候选，但迁移成本高，不能为了性能指标直接替换。
- 建议 Task 07 先做**离线对照**：把同一批 chunk 导入 Zvec，测导入时间、索引体积、P50/P95、Recall@10，与现有 SQLite FTS5 + int8 向量矩阵同题对比；只有「延迟或召回有明确代差」时才考虑迁移。
- 在此之前，优先吸收其查询语义（match/query 双入口、RRF k=60、过滤下推）与 BitPacked 只读结构思想。
