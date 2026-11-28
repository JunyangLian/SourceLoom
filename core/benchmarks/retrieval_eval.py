"""真实问句检索基线：Recall@k / MRR / 延迟分位。

数据集是 JSONL，每行一条问句：

    {"id": "q01", "query": "SSR 在 CDS 区域有多少个",
     "type": "exact", "expect": ["SSR_distribution_result.csv"], "notes": ""}

`expect` 是答案所在文件的名字或路径子串；`type=no_answer` 表示库里没有答案，
期望检索不返回强结果。评测只读数据库，不改动任何索引。

用法：
    cd core
    python -m benchmarks.retrieval_eval --dataset benchmarks/datasets/retrieval/seed.jsonl
    python -m benchmarks.retrieval_eval --dataset ... --top 10 --json report.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shiyuan import config, search  # noqa: E402


def load_dataset(path: Path) -> list[dict]:
    items = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise SystemExit(f"{path}:{number} 不是合法 JSON：{exc}") from exc
        if not item.get("query") or "expect" not in item:
            raise SystemExit(f"{path}:{number} 缺少 query 或 expect")
        items.append(item)
    return items


def _snapshot(db_path: Path) -> tuple[Path, Path]:
    """用 SQLite backup 做一致性快照。

    桌面端运行时会持续扫描/索引，直接对活库评测会得到漂移的数字；
    快照把「本次评测看到的数据」冻结下来，报告才可复现。
    """
    workdir = Path(tempfile.mkdtemp(prefix="shiyuan-bench-"))
    target = workdir / "snapshot.db"
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target, workdir


def _match_rank(items: list[dict], expect: list[str]) -> int:
    """返回第一个命中期望文件的位置（1 起）；没有命中返回 0。

    家族归并后同一份文档只保留一个代表，但 `family_versions` 里仍可展开
    其他版本；检索界面能直接看到这些版本，因此它们也算该名次命中。
    """
    for rank, item in enumerate(items, start=1):
        candidates = [item, *(item.get("family_versions") or [])]
        for entry in candidates:
            haystack = f"{entry.get('name') or ''}\n{entry.get('path') or ''}"
            if any(token and token in haystack for token in expect):
                return rank
    return 0


def run(dataset: list[dict], db_path: Path, top: int, *,
        use_vector: bool = True, use_family: bool = True,
        warm: bool = True, fusion: str = "weighted",
        use_bridge: bool = False, use_routing: bool = False) -> dict:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if warm and use_vector:
            # 应用启动时会在后台预热模型与向量矩阵；这里同步预热一次，
            # 让测得的延迟反映稳态，而不是首查询的冷启动。
            try:
                search.search(conn, "检索预热", limit=3)
            except Exception:
                pass
        rows = []
        for item in dataset:
            started = time.perf_counter()
            result = search.search(conn, item["query"], limit=top,
                                   use_vector=use_vector, use_family=use_family,
                                   fusion=fusion, use_bridge=use_bridge,
                                   use_routing=use_routing)
            elapsed = (time.perf_counter() - started) * 1000
            items = result.get("items") or []
            expect = item.get("expect") or []
            kind = item.get("type") or "exact"
            if kind == "no_answer":
                # 无答案问题：不返回强命中即算正确。
                strong = [entry for entry in items if not entry.get("weak")]
                passed = len(strong) == 0
                rank = 0 if passed else 1
            else:
                rank = _match_rank(items, expect)
                passed = rank > 0
            rows.append({
                "id": item.get("id") or item["query"],
                "type": kind,
                "source": item.get("source") or "curated",
                "query": item["query"],
                "rank": rank,
                "passed": passed,
                "latency_ms": round(elapsed, 1),
                "top": [entry.get("name") for entry in items[:3]],
                "expect": expect,
                "answer": item.get("answer") or "",
                "notes": item.get("notes") or "",
            })
    finally:
        conn.close()
    return summarize(rows, top)


def summarize(rows: list[dict], top: int) -> dict:
    answered = [row for row in rows if row["type"] != "no_answer"]
    latencies = sorted(row["latency_ms"] for row in rows)

    def ratio(values: list[bool]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    metrics = {
        "queries": len(rows),
        "hit@1": ratio([row["rank"] == 1 for row in answered]),
        f"recall@{min(5, top)}": ratio([0 < row["rank"] <= 5 for row in answered]),
        f"recall@{top}": ratio([0 < row["rank"] <= top for row in answered]),
        "mrr": round(statistics.fmean(
            [1 / row["rank"] if row["rank"] else 0.0 for row in answered] or [0.0]), 4),
        "no_answer_accuracy": ratio([row["passed"] for row in rows
                                     if row["type"] == "no_answer"]),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else 0,
        "latency_p95_ms": latencies[min(len(latencies) - 1,
                                        int(len(latencies) * 0.95))] if latencies else 0,
    }
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[row["type"]].append(row)
    metrics["by_type"] = {}
    for kind, group in sorted(by_type.items()):
        answerable = [row for row in group if row["type"] != "no_answer"]
        metrics["by_type"][kind] = {
            "queries": len(group),
            "hit@1": ratio([row["rank"] == 1 for row in answerable]),
            "passed": ratio([row["passed"] for row in group]),
        }
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_source[row.get("source") or "curated"].append(row)
    metrics["by_source"] = {}
    for source, group in sorted(by_source.items()):
        # 无答案题没有正确答案文件，rank 恒为 0；把它算进 hit@1 会低估召回。
        answerable = [row for row in group if row["type"] != "no_answer"]
        metrics["by_source"][source] = {
            "queries": len(group),
            "answerable": len(answerable),
            "hit@1": ratio([row["rank"] == 1 for row in answerable]),
            "recall@10": ratio([0 < row["rank"] <= 10 for row in answerable]),
            "mrr": round(statistics.fmean(
                [1 / row["rank"] if row["rank"] else 0.0
                 for row in answerable] or [0.0]), 4),
            "passed": ratio([row["passed"] for row in group]),
        }
    return {"metrics": metrics, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SourceLoom 真实问句检索基线")
    parser.add_argument("--dataset", required=True, action="append",
                        help="JSONL 问句集，可重复传入多个文件")
    parser.add_argument("--db", default=None, help="知识库路径，默认取 app_home")
    parser.add_argument("--top", type=int, default=10, help="返回条数，默认 10")
    parser.add_argument("--json", default=None, help="把完整结果写入 JSON 文件")
    parser.add_argument("--quiet", action="store_true", help="只打印汇总")
    parser.add_argument("--no-vector", action="store_true", help="消融：关闭向量通道")
    parser.add_argument("--no-family", action="store_true", help="消融：关闭家族归并与注入")
    parser.add_argument("--no-warm", action="store_true",
                        help="不做向量预热，测量首查询冷启动（默认预热）")
    parser.add_argument("--fusion", choices=("weighted", "rrf"), default="weighted",
                        help="融合方式：默认整数加权，rrf 为名次倒数融合")
    parser.add_argument("--bridge", action="store_true",
                        help="实验：开启编号/版本型第二轮线索扩展")
    parser.add_argument("--routing", action="store_true",
                        help="实验：按问题类型路由家族归并（开放问题保留全部版本）")
    parser.add_argument("--snapshot", action="store_true",
                        help="先把活库备份成一致性快照再评测（桌面端运行时可复现）")
    args = parser.parse_args(argv)

    dataset_paths = []
    for value in args.dataset:
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        dataset_paths.append(path)
    db_path = Path(args.db) if args.db else config.db_path()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")

    dataset: list[dict] = []
    for path in dataset_paths:
        dataset.extend(load_dataset(path))
    snapshot_dir: Path | None = None
    if args.snapshot:
        db_path, snapshot_dir = _snapshot(db_path)
    try:
        report = run(dataset, db_path, max(1, args.top),
                     use_vector=not args.no_vector, use_family=not args.no_family,
                     warm=not args.no_warm, fusion=args.fusion,
                     use_bridge=args.bridge, use_routing=args.routing)
    finally:
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
    metrics = report["metrics"]
    channels = ("+".join(name for name, on in (
        ("path/name", True), ("fulltext", True),
        ("vector", not args.no_vector), ("family", not args.no_family)) if on))
    names = "+".join(path.name for path in dataset_paths)
    if args.fusion != "weighted":
        channels += f" · fusion={args.fusion}"
    if args.bridge:
        channels += " · bridge"
    if args.routing:
        channels += " · routing"
    print(f"# {names} · {metrics['queries']} 题 · 库 {db_path.name} · 通道 {channels}")
    for key in ("hit@1", f"recall@{min(5, args.top)}", f"recall@{args.top}",
                "mrr", "no_answer_accuracy", "latency_p50_ms", "latency_p95_ms"):
        print(f"{key}: {metrics[key]}")
    for kind, stats in metrics["by_type"].items():
        print(f"  [{kind}] {stats['queries']} 题 · hit@1={stats['hit@1']} · "
              f"passed={stats['passed']}")
    for source, stats in metrics.get("by_source", {}).items():
        print(f"  <{source}> {stats['queries']} 题（可答 {stats['answerable']}）· "
              f"hit@1={stats['hit@1']} · R@10={stats['recall@10']} · MRR={stats['mrr']}")
    if not args.quiet:
        print("\n失败与明细：")
        for row in report["rows"]:
            status = "OK " if row["passed"] else "MISS"
            print(f"{status} {row['id']} rank={row['rank']} {row['query'][:40]} "
                  f"-> {row['top']} (期望 {row['expect']})")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
