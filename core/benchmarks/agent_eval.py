"""Agent 端到端对照：A0/A1 原生文件工具 vs A4/A5 SourceLoom。

Task 10 要求同一 Agent 模型、同一任务、同一语料、同一上下文/时间预算下比较
A0–A5。本环境没有可用的外部 Agent 模型权重与授权，因此：

- A2（QMD）与 A3（Zvec-Grep）未安装，明确标记为 untested，不编造结果；
- 输入/输出 Token 依赖 Agent 模型，标记为未测；用「工具输出字符数」作为可复现的
  上下文成本代理指标；
- 其余指标（证据命中、任务成功、工具调用次数、耗时、无效读取、重复读取、
  越权读取）全部由本地确定性策略产生，可用同一快照复跑。

口径：
- A0：原生文件搜索（ripgrep 关键词并集，按命中词数排序，不读正文）；
- A1：A0 + 读取排名前 3 的文件开头（模拟普通文件读取 MCP）；
- A4：当前 SourceLoom 基线 R0（路径 + FTS，无向量/家族），只调用 search；
- A5：改造后 SourceLoom（默认混合检索 + 家族），并对前 2 条结果调用
  MCP `get_document_text` 读取证据。

用法：
    cd core
    python -m benchmarks.agent_eval --dataset D:/ShiyuanData/evals/retrieval_seed.jsonl \
        --dataset D:/ShiyuanData/evals/retrieval_auto.jsonl --modes a0,a1,a4,a5 --snapshot
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shiyuan import config, query, search  # noqa: E402
from shiyuan.mcp_server import MCPServer  # noqa: E402

from benchmarks.retrieval_eval import _match_rank, _snapshot, load_dataset  # noqa: E402

NATIVE_TOP = 10
NATIVE_READ_TOP = 3
MCP_READ_TOP = 2
TERM_LIMIT = 6
READ_CHARS = 2000


def _roots(conn: sqlite3.Connection) -> tuple[list[Path], list[str]]:
    roots: list[Path] = []
    offline: list[str] = []
    for row in conn.execute(
            "SELECT root_uri FROM sources WHERE state = 'active' ORDER BY created_at"):
        root = Path(row[0])
        if root.is_dir():
            roots.append(root)
        else:
            offline.append(str(root))
    return roots, offline


def _terms(text: str) -> list[str]:
    """关键词提取只做词法分析，不调用任何检索或模型。"""
    variants = query.analyze(text)["variants"]
    terms: list[str] = []
    for variant in variants:
        value = variant.strip()
        if len(value) < 2 or value in terms:
            continue
        terms.append(value)
        if len(terms) >= TERM_LIMIT:
            break
    return terms


def _under_roots(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _native_search(terms: list[str], roots: list[Path], *, top: int,
                   budget_s: float) -> dict:
    """ripgrep 关键词并集：返回排序后的文件、调用次数、输出成本与违规计数。

    用一次 `--count-matches` 调用（多个 -e 词）而不是逐词调用，既贴近真实
    Agent 的一次 grep，也避免在几十万文件的根目录上按词线性叠加超时。
    """
    started = time.perf_counter()
    if not terms:
        return {
            "paths": [], "matched": 0, "calls": 0, "output_chars": 0,
            "elapsed_ms": 0.0, "timeout": False, "permission_violations": 0,
        }
    command = [
        "rg", "--count-matches", "--ignore-case", "--fixed-strings",
        "--no-messages", "--max-filesize", "2M",
        "--glob", "!**/node_modules/**", "--glob", "!**/.git/**",
        "--glob", "!**/__pycache__/**", "--glob", "!**/venv/**",
        "--glob", "!**/.venv/**", "--glob", "!**/site-packages/**",
        "--glob", "!**/target/**", "--glob", "!**/dist/**",
    ]
    for term in terms:
        command.extend(["-e", term])
    command.extend(str(root) for root in roots)
    matches: dict[str, int] = {}
    violations = 0
    timed_out = False
    calls = 1
    output_chars = 0
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=budget_s, check=False)
        output_chars = len(proc.stdout)
        for line in proc.stdout.splitlines():
            path, _, count = line.rpartition(":")
            if not path:
                continue
            try:
                value = int(count)
            except ValueError:
                continue
            if not _under_roots(Path(path), roots):
                violations += 1
                continue
            matches[path] = matches.get(path, 0) + value
    except subprocess.TimeoutExpired:
        timed_out = True
    # 原生搜索也会先看文件名：文件名命中关键词的排在纯正文高频命中之前。
    def rank_key(entry: tuple[str, int]) -> tuple[int, int, str]:
        path, count = entry
        lowered = path.lower()
        name_hits = sum(1 for term in terms if term.lower() in lowered)
        return (-name_hits, -count, path)

    ranked = sorted(matches.items(), key=rank_key)
    return {
        "paths": [path for path, _ in ranked[:top]],
        "matched": len(matches),
        "calls": calls,
        "output_chars": output_chars,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "timeout": timed_out,
        "permission_violations": violations,
    }


def _read_head(path: str, limit: int = READ_CHARS) -> tuple[str, bool]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit), True
    except OSError:
        return "", False


def _native_rank(paths: list[str], expect: list[str]) -> int:
    for rank, path in enumerate(paths, start=1):
        if any(token and token in path for token in expect):
            return rank
    return 0


def run_native(profile: str, dataset: list[dict], roots: list[Path], *,
               top: int = NATIVE_TOP, budget_s: float = 8.0) -> dict:
    rows = []
    for item in dataset:
        terms = _terms(item["query"])
        found = _native_search(terms, roots, top=top, budget_s=budget_s)
        calls = found["calls"]
        output_chars = found["output_chars"]
        invalid_reads = 0
        duplicate_chars = 0
        read_paths: set[str] = set()
        if profile == "a1":
            for path in found["paths"][:NATIVE_READ_TOP]:
                text, ok = _read_head(path)
                calls += 1
                if not ok:
                    invalid_reads += 1
                    continue
                output_chars += len(text)
                if path in read_paths:
                    duplicate_chars += len(text)
                read_paths.add(path)
        expect = item.get("expect") or []
        kind = item.get("type") or "exact"
        if kind == "no_answer":
            passed = found["matched"] == 0
            rank = 0 if passed else 1
        else:
            rank = _native_rank(found["paths"], expect)
            passed = rank > 0
        rows.append({
            "id": item.get("id") or item["query"],
            "type": kind,
            "query": item["query"],
            "rank": rank,
            "passed": passed,
            "calls": calls,
            "output_chars": output_chars,
            "elapsed_ms": round(found["elapsed_ms"], 1),
            "invalid_reads": invalid_reads,
            "duplicate_chars": duplicate_chars,
            "permission_violations": found["permission_violations"],
            "timeout": found["timeout"],
            "top": [Path(path).name for path in found["paths"][:3]],
            "expect": expect,
        })
    return summarize(rows, top, profile)


def run_sourceloom(profile: str, dataset: list[dict], db_path: Path, *,
                   top: int = NATIVE_TOP) -> dict:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    server = MCPServer(conn)
    rows = []
    try:
        try:
            search.search(conn, "检索预热", limit=3)
        except Exception:
            pass
        for item in dataset:
            started = time.perf_counter()
            result = search.search(
                conn, item["query"], limit=top,
                use_vector=(profile == "a5"), use_family=(profile == "a5"))
            items = result.get("items") or []
            calls = 1
            output_chars = sum(len(entry.get("snippet") or "") for entry in items)
            invalid_reads = 0
            duplicate_chars = 0
            read_locations: set[str] = set()
            if profile == "a5":
                for entry in items[:MCP_READ_TOP]:
                    try:
                        payload = server._document_text({
                            "location_id": entry["location_id"],
                            "max_chars": 1200,
                        })
                    except Exception:
                        invalid_reads += 1
                        continue
                    calls += 1
                    text = payload.get("text") or ""
                    output_chars += len(text)
                    if entry["location_id"] in read_locations:
                        duplicate_chars += len(text)
                    read_locations.add(entry["location_id"])
            elapsed = (time.perf_counter() - started) * 1000
            expect = item.get("expect") or []
            kind = item.get("type") or "exact"
            if kind == "no_answer":
                strong = [entry for entry in items if not entry.get("weak")]
                passed = len(strong) == 0
                rank = 0 if passed else 1
            else:
                rank = _match_rank(items, expect)
                passed = rank > 0
            rows.append({
                "id": item.get("id") or item["query"],
                "type": kind,
                "query": item["query"],
                "rank": rank,
                "passed": passed,
                "calls": calls,
                "output_chars": output_chars,
                "elapsed_ms": round(elapsed, 1),
                "invalid_reads": invalid_reads,
                "duplicate_chars": duplicate_chars,
                "permission_violations": 0,
                "timeout": False,
                "top": [entry.get("name") for entry in items[:3]],
                "expect": expect,
            })
    finally:
        conn.close()
    return summarize(rows, top, profile)


def summarize(rows: list[dict], top: int, profile: str) -> dict:
    answerable = [row for row in rows if row["type"] != "no_answer"]
    latencies = sorted(row["elapsed_ms"] for row in rows)

    def ratio(values: list[bool]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    def pick(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, int(len(values) * percentile))
        return round(values[index], 1)

    metrics = {
        "queries": len(rows),
        "task_success": ratio([row["passed"] for row in rows]),
        "evidence_hit@1": ratio([row["rank"] == 1 for row in answerable]),
        f"evidence_hit@{top}": ratio([0 < row["rank"] <= top for row in answerable]),
        "mrr": round(statistics.fmean(
            [1 / row["rank"] if row["rank"] else 0.0 for row in answerable] or [0.0]), 4),
        "no_answer_accuracy": ratio([row["passed"] for row in rows
                                     if row["type"] == "no_answer"]),
        "avg_tool_calls": round(statistics.fmean(
            [row["calls"] for row in rows] or [0.0]), 2),
        "avg_output_chars": round(statistics.fmean(
            [row["output_chars"] for row in rows] or [0.0]), 1),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0,
        "latency_p95_ms": pick(latencies, 0.95),
        "invalid_reads": sum(row["invalid_reads"] for row in rows),
        "duplicate_read_chars": sum(row["duplicate_chars"] for row in rows),
        "permission_violations": sum(row["permission_violations"] for row in rows),
        "timeouts": sum(1 for row in rows if row["timeout"]),
        "input_tokens": None,
        "output_tokens": None,
        "tokens_note": "未测：本环境没有 Agent 模型；output_chars 为上下文成本代理",
    }
    return {"profile": profile, "metrics": metrics, "rows": rows}


def topic_accumulation(rows: list[dict]) -> dict:
    """同一期望文件的多题视为同一主题的连续任务，比较首次与后续。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        expect = row.get("expect") or []
        if row["type"] == "no_answer" or not expect:
            continue
        groups[expect[0]].append(row)
    first: list[dict] = []
    later: list[dict] = []
    for entries in groups.values():
        if len(entries) < 2:
            continue
        first.append(entries[0])
        later.extend(entries[1:])
    if not first:
        return {"groups": 0}

    def stats(entries: list[dict]) -> dict:
        latencies = sorted(entry["elapsed_ms"] for entry in entries)
        return {
            "queries": len(entries),
            "hit_rate": round(sum(1 for entry in entries if entry["passed"]) / len(entries), 4),
            "avg_latency_ms": round(statistics.fmean(latencies), 1),
            "avg_tool_calls": round(statistics.fmean(
                [entry["calls"] for entry in entries]), 2),
            "avg_output_chars": round(statistics.fmean(
                [entry["output_chars"] for entry in entries]), 1),
        }

    return {"groups": len(first), "first": stats(first), "later": stats(later)}


def _sample(dataset: list[dict], limit: int) -> list[dict]:
    if limit <= 0 or limit >= len(dataset):
        return dataset
    step = len(dataset) / limit
    return [dataset[min(len(dataset) - 1, int(index * step))] for index in range(limit)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SourceLoom Agent A0–A5 对照")
    parser.add_argument("--dataset", required=True, action="append",
                        help="JSONL 问句集，可重复")
    parser.add_argument("--db", default=None, help="知识库路径，默认取 app_home")
    parser.add_argument("--modes", default="a0,a1,a2,a3,a4,a5",
                        help="要运行的对照项，逗号分隔；a2/a3 未安装会自动标记 untested")
    parser.add_argument("--top", type=int, default=10, help="证据取前 k 条，默认 10")
    parser.add_argument("--limit", type=int, default=0, help="只跑分层抽样的前 N 题")
    parser.add_argument("--budget", type=float, default=8.0,
                        help="A0/A1 每题原生搜索时间预算（秒）")
    parser.add_argument("--snapshot", action="store_true",
                        help="先把活库备份成一致性快照再评测")
    parser.add_argument("--json", default=None, help="完整结果写入 JSON")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    args = parser.parse_args(argv)

    dataset: list[dict] = []
    for value in args.dataset:
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        dataset.extend(load_dataset(path))
    dataset = _sample(dataset, args.limit)

    db_path = Path(args.db) if args.db else config.db_path()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")

    requested = [mode.strip().lower() for mode in args.modes.split(",") if mode.strip()]
    report: dict = {
        "dataset": {"queries": len(dataset)},
        "modes": {},
        "untested": [],
    }
    snapshot_dir: Path | None = None
    if args.snapshot:
        db_path, snapshot_dir = _snapshot(db_path)
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            roots, offline = _roots(conn)
        finally:
            conn.close()
        report["corpus"] = {
            "roots": [str(root) for root in roots],
            "offline_roots": offline,
        }
        if any(mode in requested for mode in ("a0", "a1")) and not roots:
            report["untested"].append({
                "mode": "a0/a1", "reason": "没有可访问的 active 数据源根目录"})
            requested = [mode for mode in requested if mode not in ("a0", "a1")]

        for mode in requested:
            if mode in ("a2", "a3"):
                report["untested"].append({
                    "mode": mode,
                    "reason": "QMD / Zvec-Grep 未安装，未运行"})
                continue
            if mode not in {"a0", "a1", "a4", "a5"}:
                report["untested"].append({"mode": mode, "reason": "未知对照项"})
                continue
            started = time.perf_counter()
            if mode in ("a0", "a1"):
                result = run_native(mode, dataset, roots, top=args.top,
                                    budget_s=args.budget)
            else:
                result = run_sourceloom(mode, dataset, db_path, top=args.top)
            result["metrics"]["wall_seconds"] = round(time.perf_counter() - started, 1)
            result["topic_accumulation"] = topic_accumulation(result["rows"])
            report["modes"][mode] = result
    finally:
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

    print(f"# Agent 对照 · {len(dataset)} 题 · 库 {db_path.name} · top={args.top}")
    for mode, result in report["modes"].items():
        metrics = result["metrics"]
        print(f"\n[{mode}] {metrics['queries']} 题")
        for key in ("task_success", "evidence_hit@1", f"evidence_hit@{args.top}",
                    "mrr", "no_answer_accuracy", "avg_tool_calls",
                    "avg_output_chars", "latency_p50_ms", "latency_p95_ms",
                    "invalid_reads", "duplicate_read_chars",
                    "permission_violations", "timeouts"):
            print(f"  {key}: {metrics[key]}")
        topic = result["topic_accumulation"]
        if topic.get("groups"):
            print(f"  同主题 {topic['groups']} 组 · 首次 {topic['first']}")
            print(f"                     后续 {topic['later']}")
        if not args.quiet:
            for row in result["rows"]:
                status = "OK " if row["passed"] else "MISS"
                print(f"    {status} {row['id']} rank={row['rank']} "
                      f"{row['query'][:34]} -> {row['top']}")
    for item in report["untested"]:
        print(f"\n[untested] {item['mode']}: {item['reason']}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
