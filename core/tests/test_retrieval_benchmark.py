"""检索基线：120 篇带唯一标记的文档 + 140 道有明确目标文件的题。

这不是"真实用户语料"的替代品，但能稳定衡量召回是否退化：
文件名、正文长词、中文两字短词（LIKE 兜底）和自然语言问句各占一部分。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from shiyuan import db, indexer, scanner, search, store

CHARS = "紫杉银杏松柏槐柳杨桐枫桦楠樟榕桂梅兰竹菊荷莲菱藻蕨苔藓藤萝"
SHORT_TOKENS = [
    CHARS[i] + CHARS[j]
    for i in range(len(CHARS))
    for j in range(len(CHARS))
    if i != j
][:120]


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "benchmark.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _build_corpus(conn, tmp_path: Path) -> list[tuple[str, str]]:
    root = tmp_path / "检索基线"
    root.mkdir()
    cases: list[tuple[str, str]] = []
    for index, token in enumerate(SHORT_TOKENS):
        platform = "NovaSeq" if index % 2 == 0 else "BGI-T7"
        sample = 100 + index
        marker = f"标记{index:03d}"
        name = f"项目{index:03d}-{token}.md"
        (root / name).write_text(
            f"# 项目{index:03d} 实验记录\n"
            f"物种：{token}物种{index:03d}\n"
            f"样本量：{sample}\n"
            f"测序平台：{platform}\n"
            f"{marker}：本项目的样本量为 {sample}，平台为 {platform}。\n",
            encoding="utf-8",
        )
        cases.append((name, name))

    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return cases


def test_retrieval_benchmark_recall(conn, tmp_path):
    stem_cases = _build_corpus(conn, tmp_path)

    queries: list[tuple[str, str]] = []
    for index in range(40):
        queries.append((f"项目{index:03d}-{SHORT_TOKENS[index]}", stem_cases[index][1]))
    for index in range(40, 80):
        queries.append((f"标记{index:03d}", stem_cases[index][1]))
    for index in range(80, 120):
        queries.append((SHORT_TOKENS[index], stem_cases[index][1]))
    for index in range(20):
        queries.append((f"标记{index:03d} 的样本量是多少？", stem_cases[index][1]))

    ranks: list[int | None] = []
    for query, expected in queries:
        result = search.search(conn, query, limit=5)
        names = [item["name"] for item in result["items"]]
        ranks.append(names.index(expected) + 1 if expected in names else None)

    total = len(ranks)
    recall_at_1 = sum(1 for rank in ranks if rank == 1) / total
    recall_at_5 = sum(1 for rank in ranks if rank and rank <= 5) / total
    mrr = sum(1.0 / rank for rank in ranks if rank) / total
    print(
        f"retrieval benchmark: n={total} R@1={recall_at_1:.3f} "
        f"R@5={recall_at_5:.3f} MRR={mrr:.3f}"
    )

    assert total == 140
    assert recall_at_5 >= 0.95, f"R@5 退化：{recall_at_5:.3f}"
    assert recall_at_1 >= 0.80, f"R@1 退化：{recall_at_1:.3f}"
    assert mrr >= 0.80, f"MRR 退化：{mrr:.3f}"
    assert math.isfinite(mrr)


def test_storage_bench_builds_lexical_query():
    """存储对照脚本复用同一套查询切分：FTS5 只取 ≥3 字符，其余后端取 ≥2。"""
    from benchmarks import storage_bench

    analysis = {"variants": ["SSR", "ss", "CDS", "区域"]}

    assert storage_bench._terms(analysis, minimum=2)[:2] == ["SSR", "ss"]
    assert '"CDS"' in storage_bench._fts_query(analysis)
    assert '"SSR"' in storage_bench._fts_query(analysis)
    assert '"ss"' not in storage_bench._fts_query(analysis)


def test_family_versions_count_as_hit_at_same_rank():
    """家族归并只展示一个代表，但展开的版本仍算该名次命中。"""
    from benchmarks import retrieval_eval

    items = [{
        "name": "论文终稿.md",
        "path": "资料/论文终稿.md",
        "family_versions": [
            {"name": "论文初稿.md", "path": "资料/论文初稿.md"},
        ],
    }]

    assert retrieval_eval._match_rank(items, ["论文初稿.md"]) == 1


def test_agent_eval_native_search_prefers_name_hits(monkeypatch, tmp_path):
    """A0/A1 的原生搜索：文件名命中优先，且拒绝根目录之外的路径。"""
    from benchmarks import agent_eval

    root = tmp_path / "语料"
    root.mkdir()
    stdout = "\n".join([
        f"{root / '正文高频.txt'}:9",
        f"{root / 'SSR_CDS_result.csv'}:2",
        f"{tmp_path / 'outside.txt'}:3",
    ])

    class Done:
        def __init__(self) -> None:
            self.stdout = stdout
            self.returncode = 0

    monkeypatch.setattr(
        agent_eval.subprocess, "run", lambda *args, **kwargs: Done())

    found = agent_eval._native_search(["SSR", "CDS"], [root], top=5, budget_s=1)

    assert Path(found["paths"][0]).name == "SSR_CDS_result.csv"
    assert found["matched"] == 2
    assert found["permission_violations"] == 1
    assert found["calls"] == 1


def test_agent_eval_sample_is_deterministic_and_unique():
    from benchmarks import agent_eval

    dataset = [{"id": f"q{index}"} for index in range(10)]
    picked = agent_eval._sample(dataset, 4)

    assert [item["id"] for item in picked] == ["q0", "q2", "q5", "q7"]
    assert len({item["id"] for item in picked}) == len(picked)


def test_agent_eval_topic_accumulation_compares_first_and_later():
    from benchmarks import agent_eval

    rows = [
        {"type": "exact", "expect": ["a.md"], "passed": False,
         "elapsed_ms": 100, "calls": 1, "output_chars": 10},
        {"type": "exact", "expect": ["a.md"], "passed": True,
         "elapsed_ms": 50, "calls": 1, "output_chars": 10},
        {"type": "exact", "expect": ["b.md"], "passed": True,
         "elapsed_ms": 30, "calls": 1, "output_chars": 10},
    ]

    result = agent_eval.topic_accumulation(rows)

    assert result["groups"] == 1
    assert result["first"]["hit_rate"] == 0.0
    assert result["later"]["hit_rate"] == 1.0


def test_rerank_window_parses_model_order_and_ranks():
    from benchmarks import rerank_window

    assert rerank_window._parse_order('{"order":[2,1]}', 2) == [2, 1]
    assert rerank_window._parse_order('{"order":[{"id":2},{"id":1}]}', 2) == [2, 1]
    assert rerank_window._parse_order("不是 JSON：3 1", 3) == [3, 1, 2]

    items = [{"name": "a.md", "path": "a.md"}, {"name": "b.md", "path": "b.md"}]
    assert rerank_window._reranked_rank(items, [2, 1], ["b.md"]) == 1
