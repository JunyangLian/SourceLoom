# zvec-grep（zg）研究笔记

- 仓库 / commit / 版本：[zvec-ai/zvec-grep](https://github.com/zvec-ai/zvec-grep) @ `08e93f17a517a6bfa2babfe41de86daf62e8a7e4`（main）；npm 包 `@zvec/zvec-grep`
- 许可证与分发限制：Apache-2.0（README/License badge 与仓库 `LICENSE`）。可商用，需保留 NOTICE/许可声明。
- 核心技术（一句话）：本地优先的检索 CLI + MCP 服务，把 ripgrep、BM25、向量检索统一到同一个「带来源位置的证据」接口。
- 关键模块（目录级，已读 `src/mcp`）：
  - `src/mcp/schemas.ts`：全部工具的 zod 输入/输出契约（本笔记主要依据）。
  - `src/mcp/tools.ts`、`result-format.ts`：工具实现与结果格式化（未逐行读）。
  - `src/mcp/stdio-bridge.ts`、`http-transport.ts`：stdio / HTTP 两种 MCP 传输。
  - `src/mcp/input-normalization.ts`、`progress-heartbeat.ts`：输入规范化与索引进度心跳。
  - `src/{engine,daemon,client,cli,prompts}`、`rust/`：检索引擎与 CLI（**职责未逐文件核实**）。
- 工具面（MCP）：
  - `index`（建/删/重建索引，默认后台提交，`wait=false`，可轮询 `index_status`）
  - `search`（混合检索）
  - `rg`（仅接受以 `rg` 开头的命令，按参数解析、不经 shell）
  - `index_status` / `index_drop` / `server_status`
- 输入约束（防 token/资源滥用）：
  - `root` 必须是绝对路径；query 单串 ≤ 4000 字符。
  - `queries` / `fts` / `vector` 每组最多 32 个；`limit` ≤ 50；path filter ≤ 128 条、每条 ≤ 1024 字符。
  - `fuse` 控制「合并成一个排序」还是「按查询组分别返回并保留 group 元数据」。
  - `freshness=eventual|wait_for_fresh`；`autoUpdate` 允许检索触发后台索引更新。
  - `preview=short|full` 只影响返回正文长度，不改变排序。
- 输出结构（`search`）：
  - 顶层：`root`、`freshness(fresh|possibly_stale)`、`indexing{state,completed,total}`、`result`。
  - `result`：`query`、`root`、`source=index|rg|lexical_fallback`、`coverage=ranked_sample|rg_exhaustive|rg_truncated|lexical_exhaustive|lexical_truncated`、`diagnostics{emptyReason,timings[]}`、`items[]`。
  - `item`：`kind=indexed_entity|lexical_match`、`rank`、`file{absolutePath,relativePath,rootPath}`、`range`（`file|text|byte|page|page_text|page_region`）、`content`、`status=fresh|possibly_stale`、`score`、`matchedBy=fts|vector|fts+vector|lexical`、`queryGroups[]`、`selectionReason=coverage|global_fill`。
- 官方评测口径（README，仅图示，**未抄录具体数值、未复现**）：
  - SWE-QA-Bench（Claude Code + Opus 5）与 BrowseComp-Plus（Codex gpt-5.6-sol）；zg 侧使用 Qwen3.7 Text Embedding。
  - 结论是「更少 token、更少工具调用、更少时间」，但没有给出可直接引用的召回/准确率数字。
- 可复用点（落到 SourceLoom）：
  1. **诚实检索契约**：`coverage` 明确区分「排序采样」与「穷尽/截断」，`emptyReason` 解释空结果，`status` 标记索引可能过期 → 我们的 `search` 返回应补 `coverage`/`stale`，而不是只给 `total`。
  2. **证据范围模型**：`range` 支持整文件、行区间、字节、页、页内区域 → 直接对应我们的 PDF OCR bbox 与 docx 段落定位。
  3. **分组检索**：`queries` + `fts` + `vector` 三类通道，`fuse` 开关，结果保留每组名次 → 与我们的 RRF 实验可对齐。
  4. **工具输入上限**：查询组数、路径过滤数、limit 都有硬上限，防止 Agent 一次塞入超大上下文。
  5. **后台索引作业 + 状态轮询**：索引提交与检索解耦，检索返回 `possibly_stale`，与我们的 `locked`/重排语义相近。
- 不适合 SourceLoom 的部分：
  - TypeScript + Rust 双栈，不能直接嵌入 Python 核心；依赖 zvec 原生库，Windows 分发需额外二进制与签名。
  - 其索引目录固定在 `<root>/.zvec-grep/`，与我们的单文件 SQLite 备份模型不一致。
- 未验证能力：SWE-QA-Bench / BrowseComp-Plus 的具体数值；索引更新延迟；`rust/` 与 `src/engine` 的融合实现细节。

## 与 SourceLoom 的对照结论

- 最值得借鉴的是**返回结构**而不是实现：把「覆盖范围、数据新鲜度、空结果原因、证据区间」显式化，能直接改善 Agent 端体验与 Task 10 的 MCP 对照评测。
- 我们已有 `weak` 标记；下一步应在 MCP `search` 返回值中加入 `coverage`、`stale`、`range`（页/bbox/行号），并保持现有 JSON 向后兼容。
