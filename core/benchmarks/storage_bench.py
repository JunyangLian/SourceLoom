"""Task 07：存储后端的同语料对照（SQLite FTS5 / Tantivy / Zvec / LanceDB）。

只用正文做词面检索，排除路径、向量与家族通道，回答一个具体问题：
「同一批 chunk、同一批问句，换存储引擎后构建成本、延迟与召回差多少」。

依赖（可选）：
    tantivy、zvec、lancedb 未安装时对应后端自动跳过。

用法：
    cd core
    python -m benchmarks.storage_bench --db ..\\data\\shiyuan.db --sizes 20000 100000
    python -m benchmarks.storage_bench --db ... --sizes 600000 --backends sqlite tantivy --json out.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shiyuan import query  # noqa: E402


def load_questions(paths: list[Path]) -> list[dict]:
    items: list[dict] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            item = json.loads(line)
            if item.get("type") == "no_answer":
                continue
            items.append(item)
    return items


def load_chunks(db_path: Path, size: int) -> tuple[list[dict], dict[str, str]]:
    """按 rowid 等距抽样，保证跨数据源分布而不是只取最前面的一批文件。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN asset_locations l"
            " ON l.asset_id = c.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " WHERE l.availability = 'AVAILABLE' AND s.state = 'active'"
            " AND length(c.text) >= 40"
        ).fetchone()[0]
        stride = max(1, total // max(1, size))
        rows = conn.execute(
            "SELECT c.chunk_id, c.text, l.path AS path FROM chunks c"
            " JOIN asset_locations l ON l.asset_id = c.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " WHERE l.availability = 'AVAILABLE' AND s.state = 'active'"
            " AND length(c.text) >= 40 AND (c.rowid % ?) = 0"
            " ORDER BY c.rowid LIMIT ?", (stride, size)
        ).fetchall()
    finally:
        conn.close()
    chunks = [{"chunk_id": row["chunk_id"], "text": row["text"]} for row in rows]
    paths = {row["chunk_id"]: row["path"] for row in rows}
    return chunks, paths


def _terms(analysis: dict, minimum: int = 2) -> list[str]:
    units: list[str] = []
    for variant in analysis["variants"]:
        if len(variant) >= minimum and variant not in units:
            units.append(variant)
    return units[:12]


def _fts_query(analysis: dict) -> str:
    units = _terms(analysis, minimum=3)
    if not units:
        units = _terms(analysis, minimum=2)
    return " OR ".join('"' + unit.replace('"', '""') + '"' for unit in units)


def run_sqlite(chunks: list[dict], questions: list[dict], analyses: list[dict],
               top: int, workdir: Path) -> dict:
    db = workdir / "sqlite.db"
    conn = sqlite3.connect(db)
    started = time.perf_counter()
    conn.execute("CREATE VIRTUAL TABLE fts USING fts5(text, chunk_id UNINDEXED,"
                 " tokenize='trigram')")
    conn.executemany("INSERT INTO fts(text, chunk_id) VALUES (?, ?)",
                     [(chunk["text"], chunk["chunk_id"]) for chunk in chunks])
    conn.commit()
    build_s = time.perf_counter() - started
    size_mb = db.stat().st_size / 1024 / 1024

    analyses = list(analyses)
    matches = [_fts_query(analysis) for analysis in analyses]
    for match in matches[:3]:
        try:
            conn.execute("SELECT chunk_id FROM fts WHERE fts MATCH ? LIMIT ?",
                         (match, top)).fetchall()
        except sqlite3.OperationalError:
            pass
    latencies, ranks = [], []
    for item, analysis, match in zip(questions, analyses, matches):
        started = time.perf_counter()
        try:
            rows = conn.execute(
                "SELECT chunk_id FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                (match, top)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        latencies.append((time.perf_counter() - started) * 1000)
        ranks.append([row[0] for row in rows])
    conn.close()
    return {"build_s": round(build_s, 2), "index_mb": round(size_mb, 1),
            "latencies": latencies, "rows": ranks}


def run_tantivy(chunks: list[dict], questions: list[dict], analyses: list[dict],
                top: int, workdir: Path) -> dict:
    import tantivy

    path = workdir / "tantivy"
    path.mkdir(parents=True, exist_ok=True)
    schema = tantivy.SchemaBuilder()
    schema.add_text_field("body", stored=False)
    schema.add_text_field("chunk_id", stored=True)
    index = tantivy.Index(schema.build(), path=str(path))
    analyzer = tantivy.TextAnalyzerBuilder(
        tantivy.Tokenizer.ngram(2, 3, False)).build()
    # 注册成 "default"：建索引与 parse_query 都走同一套 2–3gram 分析。
    index.register_tokenizer("default", analyzer)
    writer = index.writer()
    started = time.perf_counter()
    for position, chunk in enumerate(chunks, start=1):
        writer.add_document(tantivy.Document(body=chunk["text"],
                                             chunk_id=chunk["chunk_id"]))
        if position % 50000 == 0:
            writer.commit()
    writer.commit()
    writer.wait_merging_threads()
    build_s = time.perf_counter() - started
    index.reload()
    searcher = index.searcher()
    size_mb = sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1024 / 1024

    queries = []
    for analysis in analyses:
        units = _terms(analysis, minimum=2)
        queries.append(" OR ".join('"' + unit.replace('"', '""') + '"'
                                  for unit in units) or '""')
    for text in queries[:3]:
        searcher.search(index.parse_query(text, ["body"]), top)
    latencies, ranks = [], []
    for text in queries:
        started = time.perf_counter()
        try:
            result = searcher.search(index.parse_query(text, ["body"]), top)
            rows = []
            for hit in result.hits:
                value = searcher.doc(hit[1])["chunk_id"]
                rows.append(value[0] if isinstance(value, list) else value)
        except Exception:
            rows = []
        latencies.append((time.perf_counter() - started) * 1000)
        ranks.append(rows)
    return {"build_s": round(build_s, 2), "index_mb": round(size_mb, 1),
            "latencies": latencies, "rows": ranks}


def run_zvec(chunks: list[dict], questions: list[dict], analyses: list[dict],
             top: int, workdir: Path) -> dict:
    import zvec

    path = workdir / "zvec"
    schema = zvec.CollectionSchema(
        name="bench",
        fields=[
            zvec.FieldSchema("chunk_id", zvec.DataType.STRING, nullable=False),
            zvec.FieldSchema(
                "content", zvec.DataType.STRING, nullable=False,
                index_param=zvec.FtsIndexParam(
                    tokenizer_name="ngram",
                    filters=["lowercase"],
                    extra_params='{"ngram_min":2,"ngram_max":3}')),
        ],
    )
    coll = zvec.create_and_open(str(path), schema)
    started = time.perf_counter()
    batch: list = []
    for chunk in chunks:
        batch.append(zvec.Doc(chunk["chunk_id"], fields={
            "chunk_id": chunk["chunk_id"], "content": chunk["text"]}))
        if len(batch) >= 1000:
            coll.insert(batch)
            batch = []
    if batch:
        coll.insert(batch)
    coll.flush()
    build_s = time.perf_counter() - started
    size_mb = sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1024 / 1024

    query_texts = [" ".join(_terms(analysis, minimum=2)) for analysis in analyses]
    for text in query_texts[:3]:
        coll.query(queries=zvec.Query(field_name="content",
                                      fts=zvec.Fts(match_string=text)),
                   topk=top, output_fields=["chunk_id"])
    latencies, ranks = [], []
    errors: list[str] = []
    for text in query_texts:
        started = time.perf_counter()
        try:
            result = coll.query(
                queries=zvec.Query(field_name="content",
                                   fts=zvec.Fts(match_string=text)),
                topk=top, output_fields=["chunk_id"])
            rows = []
            for hit in result:
                fields = hit.fields if hasattr(hit, "fields") else hit["fields"]
                rows.append(fields["chunk_id"])
        except Exception as exc:  # noqa: BLE001 - 记录后继续，避免单题失败中断对照
            if len(errors) < 3:
                errors.append(f"{text}: {exc}")
            rows = []
        latencies.append((time.perf_counter() - started) * 1000)
        ranks.append(rows)
    if errors:
        print(f"  zvec errors (first): {errors[0]}")
    coll.close()
    return {"build_s": round(build_s, 2), "index_mb": round(size_mb, 1),
            "latencies": latencies, "rows": ranks}


def run_lancedb(chunks: list[dict], questions: list[dict], analyses: list[dict],
                top: int, workdir: Path) -> dict:
    import lancedb
    import pyarrow as pa
    from lancedb.index import FTS

    path = workdir / "lancedb"
    db = lancedb.connect(str(path))
    started = time.perf_counter()
    table = db.create_table("bench", data=pa.table({
        "chunk_id": [chunk["chunk_id"] for chunk in chunks],
        "text": [chunk["text"] for chunk in chunks],
    }))
    # LanceDB 0.41 已移除 tantivy 后端，改用原生 FTS；ngram 2–3 与 Tantivy 口径一致。
    table.create_index(
        "text",
        config=FTS(base_tokenizer="ngram", ngram_min_length=2,
                   ngram_max_length=3, lower_case=True),
    )
    build_s = time.perf_counter() - started
    size_mb = sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1024 / 1024

    queries = [" OR ".join(_terms(analysis, minimum=2)) for analysis in analyses]
    for text in queries[:3]:
        try:
            table.search(text, query_type="fts", fts_columns="text").limit(top).to_list()
        except Exception:
            pass
    latencies, ranks = [], []
    errors: list[str] = []
    for text in queries:
        started = time.perf_counter()
        try:
            rows = table.search(
                text, query_type="fts", fts_columns="text"
            ).limit(top).select(["chunk_id"]).to_list()
            ranks.append([row["chunk_id"] for row in rows])
        except Exception as exc:  # noqa: BLE001 - 单题失败不中断整组对照
            if len(errors) < 3:
                errors.append(f"{text}: {exc}")
            ranks.append([])
        latencies.append((time.perf_counter() - started) * 1000)
    if errors:
        print(f"  lancedb errors (first): {errors[0]}")
    return {"build_s": round(build_s, 2), "index_mb": round(size_mb, 1),
            "latencies": latencies, "rows": ranks}


def evaluate(result: dict, questions: list[dict], paths: dict[str, str]) -> dict:
    hit1 = recall10 = 0
    reciprocal = 0.0
    for item, rows in zip(questions, result["rows"]):
        expect = item.get("expect") or []
        rank = 0
        for position, chunk_id in enumerate(rows, start=1):
            haystack = (paths.get(chunk_id) or "").replace("\\", "/")
            name = haystack.rsplit("/", 1)[-1]
            if any(token and (token in haystack or token in name) for token in expect):
                rank = position
                break
        if rank == 1:
            hit1 += 1
        if 0 < rank <= 10:
            recall10 += 1
        reciprocal += 1 / rank if rank else 0.0
    total = len(questions) or 1
    latencies = sorted(result["latencies"])
    return {
        "queries": len(questions),
        "hit@1": round(hit1 / total, 4),
        "recall@10": round(recall10 / total, 4),
        "mrr": round(reciprocal / total, 4),
        "nonempty": round(sum(1 for rows in result["rows"] if rows) / total, 4),
        "p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else 0,
        "p95_ms": round(latencies[min(len(latencies) - 1,
                                      int(len(latencies) * 0.95))], 1) if latencies else 0,
        "build_s": result["build_s"],
        "index_mb": result["index_mb"],
    }


BACKENDS = {
    "sqlite": run_sqlite,
    "tantivy": run_tantivy,
    "zvec": run_zvec,
    "lancedb": run_lancedb,
}

BACKEND_MODULES = {
    "sqlite": None,
    "tantivy": "tantivy",
    "zvec": "zvec",
    "lancedb": "lancedb",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SourceLoom 存储后端对照")
    parser.add_argument("--db", required=True)
    parser.add_argument("--dataset", action="append", default=None)
    parser.add_argument("--sizes", type=int, nargs="+", default=[20000])
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--backends", nargs="+", default=list(BACKENDS))
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    dataset_paths = [Path(p) for p in (args.dataset or [
        r"..\data\evals\retrieval_seed.jsonl",
        r"..\data\evals\retrieval_auto.jsonl",
    ])]
    dataset_paths = [p if p.is_absolute() else ROOT / p for p in dataset_paths]
    questions = load_questions(dataset_paths)
    analyses = [query.analyze(item["query"]) for item in questions]

    report = {"db": str(db_path), "questions": len(questions), "results": []}
    for size in args.sizes:
        chunks, paths = load_chunks(db_path, size)
        print(f"\n== {len(chunks)} chunks ==")
        for name in args.backends:
            runner = BACKENDS.get(name)
            if runner is None:
                continue
            module = BACKEND_MODULES.get(name)
            if module:
                try:
                    importlib.import_module(module)
                except Exception as exc:  # noqa: BLE001 - 缺依赖时跳过并记录
                    print(f"  {name}: skipped ({exc})")
                    continue
            workdir = Path(tempfile.mkdtemp(prefix=f"shiyuan-{name}-"))
            try:
                raw = runner(chunks, questions, analyses, args.top, workdir)
                metrics = evaluate(raw, questions, paths)
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
            print(f"  {name}: {json.dumps(metrics, ensure_ascii=False)}")
            report["results"].append({"backend": name, "size": len(chunks),
                                      **metrics})
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n完整结果：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
