# SAG2 研究笔记

- 仓库 / commit / 版本：[Zleap-AI/SAG-Benchmark](https://github.com/Zleap-AI/SAG-Benchmark) @ `bb7f82751ba1576219f9d9ebd021128603d130c3`（main，2026-10-19 最后推送）；论文 [arXiv:2610.22588v2](https://arxiv.org/html/2610.22588)（2026-11-10）
- 许可证与分发限制：MIT（GitHub API `license.spdx_id=MIT`，仓库根 `LICENSE`）。可复用代码，但需保留版权与许可声明。
- 核心技术（一句话）：**建库不调用任何模型**的多跳检索——SQLite FTS5/BM25 召回候选，查询时用决策模型判断「是否为答案链的必要证据」，从高分种子激活词项再检索，最后一轮用 DPP 做多样性排序。
- 关键模块（已读）：
  - `sag2/scripts/sag2.py`：分词、FTS5 索引、BM25 查询、DPP 重排、索引签名。
  - `sag2/scripts/chunking.py`：按句切块（默认 2200 UTF-16 字符）+ 标题上下文。
  - `sag2/scripts/run_sag2_benchmark.py`：数据集评测入口（参数默认值见下）。
  - `sag2/scripts/sag2_rank.py`：SQLite 自定义排序扩展（**内容未读，待核实**）。
- 数据结构与索引设计：
  - `chunks(id, document_id, title, heading, start_char, end_char, spans, characters, source_id)`；`chunk_fts` 为 FTS5 `(title, heading, body)`。
  - `metadata(signature)`：签名 = `fingerprint + chunk_chars + INDEX_SCHEMA(=5) + source_dir + CHUNK_POLICY`，签名不一致才重建索引。
  - `index_statistics(signature, data)`：缓存 `title_phrases`、`aliases`、词频。
  - 可选 `source_dir` 模式：FTS 用 `content=''`（不存正文），只存字符 span，检索后回原文件读取；正文变更用 sha256 校验。
- 检索 / 排序 / 融合算法：
  - 默认参数：`candidate-k=100`、`seed-k=10`、`expansions=2`、`expansion-term-limit=20`、`chunk-chars=2200`、`top-k=10`。
  - 分词：jieba（HMM）+ NFKC + 小写 + 分隔符拆分 + 2/3-gram（仅当 jieba 词频 > 0）+ 停用词；查询是去重后最多 80 个词项的 `OR` 短语查询。
  - 召回：`RETRIEVAL_METHOD=per_term_all_expansions_query_bm25_rank_interleave`（逐词项、跨轮次、按 BM25 名次交错）。
  - 最终排序：`FINAL_METHOD=history_top10_scores_local_dpp`；DPP 核 = `jev_score_i * jev_score_j * cos(tfidf_i, tfidf_j)`，本地完成，无额外模型调用。
  - 证据判断 prompt 明确要求「候选本身必须是答案链的必要证据，仅主题相关不算」。
- 模型依赖与资源占用：
  - **建库：0 次模型调用**（论文与 README 一致）。
  - **查询：每轮对候选逐条调用 JEV 决策模型**（默认 OpenRouter `typesafe/jev-1.13`，`noul`）。因此「无 embedding」≠「完全离线」，本项目若要落地必须替换为本地 scorer 或规则。
  - 论文附录 G 声称可换成 reranker / embedding / 生成模型 scorer（具体数值未抄录，待核实）。
- Windows 部署与打包可行性：
  - Python 3.11+、`uv`、C 编译器（MSVC Build Tools）用于编译 SQLite 评分扩展；README 说明 paper-control 脚本当前要求 Unix-like 环境。
  - 结论：评测脚本不适合直接放进 Windows CI；算法与索引设计可以移植。
- 官方评测口径（未由本项目复现）：
  - 每数据集 1,000 题；R@10：MuSiQue **86.62**、2WikiMultiHopQA **97.43**、HotpotQA **99.00**。
  - 同表 BM25 基线：54.41 / 71.50 / 86.90。
  - EnterpriseRAG-Bench：511,962 源文档、470 标注题；README 记文档 R@10 **81.10**，论文摘要写 **81.12**（引用时需注明来源）。
  - 索引成本：MuSiQue 建库+检索 $4.33，对比 SAG $14.85（论文口径，非本机复现）。
- 可复用点（落到 SourceLoom）：
  1. 分词与 2/3-gram 扩词规则 → `query.py` / `retrieval.py`（中文短词召回）。
  2. 「候选池 → 种子 → 词项激活 → 下一轮」循环与预算参数 → `retrieval.py` 的多轮线索（Task 05）。
  3. 句级切块 + 字符 span 回原文 → `ingest.py` / 预览定位。
  4. 索引签名（解析器/schema/切块策略）驱动重建 → 已有 `PARSER_SIGNATURE` 思路可直接扩展。
  5. DPP 多样性排序 → `search.py` 的结果去重（比按家族硬去重更平滑）。
- 不适合 SourceLoom 的部分：
  - 查询时决策模型依赖与「无 LLM 可运行」目标冲突；只能作为可选增强，不能成为默认链路。
  - jieba 与现有 trigram/FTS5 分词口径不同，混用会造成召回不一致。
  - 其评测脚本依赖 Unix 环境与外部 API，不能作为本地回归基线。
- 未验证能力：`sag2_rank.py` 的排序扩展实现；JEV 成本/延迟；Agentic SAG2 细节；附录 G 各 scorer 的精确数值。

## 与 SourceLoom 的对照结论

- 我们已有 BM25（FTS5）+ 路径召回 + 向量，缺的是「多轮线索扩展」和「证据必要性判断」。
- 短期可借鉴的是**索引签名**与**句级 span**；多轮扩展先用规则实现（编号/实体/路径桥接），不引入查询时 LLM。
- 评估方式：在现有 126 题集上对比「单轮 vs 规则化两轮」，并单独报告延迟与无答案准确率。
