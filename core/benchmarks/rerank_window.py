"""Task 05 补充实验：本地模型重排窗口。

用本机 Ollama 的小模型（默认 `qwen2.5-coder:1.5b-local`）对检索前 N 条候选
做相关性重排，回答「重排窗口取多大值得」：

- window 是交给重排模型的候选数；
- baseline 是检索原始名次，reranked 是模型输出顺序；
- 同时报告 hit@1 变化、重排耗时与 token，避免只看向上的指标。

同一快照、同一问句集可复跑；模型不在本机时明确标记 untested。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shiyuan import config, search  # noqa: E402

from benchmarks.retrieval_eval import _match_rank, _snapshot, load_dataset  # noqa: E402

DEFAULT_MODEL = "qwen2.5-coder:1.5b-local"
OLLAMA = "http://127.0.0.1:11434"


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return [item.get("name", "") for item in payload.get("models", [])]


def _prompt(query: str, items: list[dict]) -> str:
    lines = [
        "你是本地检索重排器。根据问题判断候选片段的相关性，只输出 JSON。",
        f"问题：{query}",
        "候选：",
    ]
    for index, item in enumerate(items, start=1):
        snippet = " ".join((item.get("snippet") or "").split())[:180]
        lines.append(f"[{index}] {item.get('name')}：{snippet}")
    lines.append(
        f'输出格式：{{"order":[最相关编号, ...]}}，必须包含 1..{len(items)} '
        "每个编号一次，不要解释。"
    )
    return "\n".join(lines)


def _parse_order(text: str, size: int) -> list[int]:
    values = None
    try:
        data = json.loads(text)
        values = data.get("order") if isinstance(data, dict) else data
    except ValueError:
        values = [int(number) for number in re.findall(r"\d+", text)]
    order: list[int] = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("id")
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= size and number not in order:
            order.append(number)
    for number in range(1, size + 1):
        if number not in order:
            order.append(number)
    return order


def _rerank(model: str, prompt: str) -> tuple[str, dict]:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "seed": 42, "num_predict": 160},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("response", ""), {
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "prompt_eval_count": payload.get("prompt_eval_count"),
        "eval_count": payload.get("eval_count"),
    }


def _reranked_rank(items: list[dict], order: list[int], expect: list[str]) -> int:
    for position, number in enumerate(order, start=1):
        item = items[number - 1]
        haystack = f"{item.get('name') or ''}\n{item.get('path') or ''}"
        if any(token and token in haystack for token in expect):
            return position
    return 0


def run(conn: sqlite3.Connection, dataset: list[dict], model: str,
        windows: list[int]) -> dict:
    report = {"model": model, "windows": {}}
    for window in windows:
        rows = []
        for item in dataset:
            result = search.search(conn, item["query"], limit=window)
            items = result.get("items") or []
            expect = item.get("expect") or []
            baseline = _match_rank(items, expect)
            prompt = _prompt(item["query"], items)
            try:
                text, usage = _rerank(model, prompt)
                order = _parse_order(text, len(items)) if items else []
                reranked = _reranked_rank(items, order, expect)
            except Exception as exc:  # noqa: BLE001 - 单题失败继续，报告里记录
                usage = {"elapsed_ms": 0, "prompt_eval_count": None, "eval_count": None}
                reranked = 0
                order = []
                text = f"error: {exc}"
            rows.append({
                "id": item.get("id") or item["query"],
                "query": item["query"],
                "baseline_rank": baseline,
                "reranked_rank": reranked,
                "returned": len(items),
                "order": order,
                "raw": text[:400],
                **usage,
            })
        answerable = [row for row in rows if row["baseline_rank"] > -1]
        total = len(answerable) or 1

        def ratio(values: list[bool]) -> float:
            return round(sum(values) / len(values), 4) if values else 0.0

        def mrr(rows_: list[dict], key: str) -> float:
            return round(statistics.fmean(
                [1 / row[key] if row[key] > 0 else 0.0 for row in rows_] or [0.0]), 4)

        latencies = sorted(row["elapsed_ms"] for row in answerable)
        report["windows"][str(window)] = {
            "queries": len(answerable),
            "baseline_hit@1": ratio([row["baseline_rank"] == 1 for row in answerable]),
            "reranked_hit@1": ratio([row["reranked_rank"] == 1 for row in answerable]),
            f"baseline_hit@{window}": ratio(
                [0 < row["baseline_rank"] <= window for row in answerable]),
            "baseline_mrr": mrr(answerable, "baseline_rank"),
            "reranked_mrr": mrr(answerable, "reranked_rank"),
            "improved": sum(1 for row in answerable if 0 < row["reranked_rank"] < row["baseline_rank"]),
            "worsened": sum(1 for row in answerable if row["baseline_rank"] > 0
                            and (row["reranked_rank"] == 0
                                 or row["reranked_rank"] > row["baseline_rank"])),
            "avg_rerank_ms": round(statistics.fmean(latencies), 1) if latencies else 0,
            "p95_rerank_ms": round(latencies[min(len(latencies) - 1,
                                                 int(len(latencies) * 0.95))], 1) if latencies else 0,
            "rows": rows,
        }
    return report


def _sample(dataset: list[dict], limit: int) -> list[dict]:
    if limit <= 0 or limit >= len(dataset):
        return dataset
    step = len(dataset) / limit
    return [dataset[min(len(dataset) - 1, int(index * step))] for index in range(limit)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地模型重排窗口实验")
    parser.add_argument("--dataset", required=True, action="append")
    parser.add_argument("--db", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--windows", type=int, nargs="+", default=[5, 10, 20])
    parser.add_argument("--limit", type=int, default=20, help="分层抽样题数，默认 20")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    models = _ollama_models()
    if not models:
        print("[untested] 本机 Ollama 未运行，重排窗口实验跳过")
        return 0
    if args.model not in models:
        print(f"[untested] Ollama 没有模型 {args.model}；现有：{models}")
        return 0

    dataset: list[dict] = []
    for value in args.dataset:
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        dataset.extend(load_dataset(path))
    dataset = [item for item in _sample(dataset, args.limit)
               if (item.get("type") or "exact") != "no_answer"]
    if not dataset:
        print("[untested] 没有可答问题")
        return 0

    db_path = Path(args.db) if args.db else config.db_path()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    snapshot_dir: Path | None = None
    if args.snapshot:
        db_path, snapshot_dir = _snapshot(db_path)
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            report = run(conn, dataset, args.model, args.windows)
        finally:
            conn.close()
    finally:
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

    print(f"# 重排窗口 · {len(dataset)} 题 · 模型 {args.model} · 库 {db_path.name}")
    for window, metrics in report["windows"].items():
        print(f"\n[window={window}]")
        for key in ("queries", "baseline_hit@1", "reranked_hit@1",
                    f"baseline_hit@{window}", "baseline_mrr", "reranked_mrr",
                    "improved", "worsened", "avg_rerank_ms", "p95_rerank_ms"):
            print(f"  {key}: {metrics[key]}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
