from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shiyuan import config, db, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "科研资料"
    (root / "2025" / "项目A").mkdir(parents=True)
    (root / "2025" / "BLAST结果表.xlsx").write_bytes(b"blast,result\n" * 10)
    (root / "2025" / "项目A" / "实验记录.md").write_text("# 实验记录\n样本量 42\n", encoding="utf-8")
    (root / "旧的草稿.docx").write_bytes(b"PK\x03\x04fake")
    return root


def _scan(conn, source_id):
    return scanner.scan_source(conn, source_id)


def test_l0_scan_and_search_by_name(conn, tree):
    source = store.add_source(conn, tree)
    result = _scan(conn, source["source_id"])
    assert result["seen"] == 3
    assert result["new"] == 3

    hits = search.search(conn, "blast")
    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "BLAST结果表.xlsx"
    assert hits["items"][0]["index_level"] == "L0"


def test_scan_drops_legacy_office_lock_rows(conn, tmp_path):
    root = tmp_path / "大学"
    root.mkdir()
    (root / "笔记.md").write_text("正常内容，样本量 42\n", encoding="utf-8")
    (root / "~$论文.docx").write_bytes(b"owner-lock")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE path LIKE '%~$%'").fetchone()[0] == 0

    # 模拟旧版本遗留的锁文件记录：它不应在下次扫描时变成 MISSING。
    note = conn.execute("SELECT * FROM asset_locations LIMIT 1").fetchone()
    conn.execute(
        "INSERT INTO asset_locations (location_id, asset_id, source_id, path, rel_path,"
        " native_file_id, availability, size, mtime_ns, ext, first_seen_at, last_seen_at)"
        " VALUES ('loc_legacy_lock', ?, ?, ?, '~$论文.docx', NULL, 'AVAILABLE', 10, 1,"
        " 'docx', ?, ?)",
        (note["asset_id"], source["source_id"],
         str(root / "~$论文.docx"), note["first_seen_at"], note["last_seen_at"]),
    )
    conn.commit()

    result = scanner.scan_source(conn, source["source_id"])
    assert result["missing"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE location_id = 'loc_legacy_lock'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM asset_locations WHERE availability = 'MISSING'").fetchone()[0] == 0


def test_search_supports_cjk_tokens_and_path(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    hits = search.search(conn, "blast 结果")
    assert [item["name"] for item in hits["items"]] == ["BLAST结果表.xlsx"]

    by_dir = search.search(conn, "项目A")
    assert by_dir["total"] == 1
    assert by_dir["items"][0]["rel_path"] == "2025/项目A/实验记录.md"


def test_modified_file_creates_new_version(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    target = tree / "2025" / "项目A" / "实验记录.md"
    target.write_text("# 实验记录\n样本量 48\n", encoding="utf-8")

    result = _scan(conn, source["source_id"])
    assert result["updated"] == 1
    assert result["new"] == 0

    versions = conn.execute(
        "SELECT hash, is_current FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id WHERE l.path = ?"
        " ORDER BY observed_at",
        (str(target),),
    ).fetchall()
    assert len(versions) == 2
    assert sum(row["is_current"] for row in versions) == 1
    assert versions[0]["hash"] != versions[1]["hash"]


def test_rename_is_tracked_as_move_with_stable_asset(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    before = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path = ?",
        (str(tree / "旧的草稿.docx"),),
    ).fetchone()["asset_id"]

    (tree / "旧的草稿.docx").rename(tree / "2025" / "最终草稿.docx")
    result = _scan(conn, source["source_id"])

    assert result["moved"] == 1
    assert result["new"] == 0
    assert result["missing"] == 0
    row = conn.execute(
        "SELECT asset_id, availability FROM asset_locations WHERE path = ?",
        (str(tree / "2025" / "最终草稿.docx"),),
    ).fetchone()
    assert row["asset_id"] == before
    assert row["availability"] == "AVAILABLE"
    assert search.search(conn, "最终草稿")["total"] == 1


def test_deleted_file_becomes_missing_and_leaves_results(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    (tree / "2025" / "BLAST结果表.xlsx").unlink()

    result = _scan(conn, source["source_id"])
    assert result["missing"] == 1
    assert search.search(conn, "blast")["total"] == 0

    status = conn.execute(
        "SELECT availability FROM asset_locations WHERE path LIKE '%BLAST%'"
    ).fetchone()
    assert status["availability"] == "MISSING"


def test_paused_source_is_not_searchable(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    assert search.search(conn, "blast")["total"] == 1

    store.set_source_state(conn, source["source_id"], "paused")
    assert search.search(conn, "blast")["total"] == 0


def test_remove_source_clears_derived_records(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    cleared = store.remove_source(conn, source["source_id"])
    assert cleared["assets_cleared"] == 3
    assert conn.execute("SELECT COUNT(*) FROM asset_locations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert search.search(conn, "blast")["total"] == 0


def test_offline_source_marks_locations_offline(conn, tmp_path):
    root = tmp_path / "移动硬盘"
    root.mkdir()
    (root / "论文.pdf").write_bytes(b"%PDF-1.4")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])

    root.rename(tmp_path / "未挂载")
    result = _scan(conn, source["source_id"])

    assert result["state"] == "failed"
    row = conn.execute("SELECT availability FROM asset_locations").fetchone()
    assert row["availability"] == "OFFLINE"
    assert search.search(conn, "论文")["total"] == 0


def test_rejects_nested_and_duplicate_sources(conn, tree):
    store.add_source(conn, tree)
    with pytest.raises(ValueError):
        store.add_source(conn, tree)
    with pytest.raises(ValueError):
        store.add_source(conn, tree / "2025")


def test_stats_reports_only_l0_coverage(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    data = search.stats(conn)
    assert data["files"]["available"] == 3
    assert data["coverage"]["directory_level"] == "L0"
    assert data["coverage"]["content_indexed"] == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    # 接口测试不启动文件监听，避免后台线程与临时目录清理互相干扰。
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_flow(client, tree):
    created = client.post("/api/sources", json={"root": str(tree)})
    assert created.status_code == 201
    source_id = created.json()["source_id"]

    assert client.post(f"/api/sources/{source_id}/scan").status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        items = client.get("/api/sources").json()["items"]
        if items and not items[0]["scanning"]:
            break
        time.sleep(0.05)

    hits = client.get("/api/search", params={"q": "blast"}).json()
    assert hits["total"] == 1

    progress = client.get("/api/sources").json()["items"][0]["parse"]
    assert progress["total"] == 3
    assert sum(progress[key] for key in (
        "parsed", "failed", "empty", "pending", "locked", "unsupported",
        "metadata_only")) == 3

    again = client.post("/api/sources", json={"root": str(tree)})
    assert again.status_code == 201
    assert again.json()["created"] is False
    assert again.json()["source_id"] == source_id
    assert client.delete(f"/api/sources/{source_id}").status_code == 200
    assert client.get("/api/search", params={"q": "blast"}).json()["total"] == 0


def test_api_search_translates_mtime_days(client, monkeypatch):
    """HTTP 的 mtime_days 必须换算成纳秒时间下界传给检索层。"""
    captured: dict = {}

    def fake_search(conn, q, **kwargs):
        captured.update(kwargs)
        return {
            "query": q, "total": 0, "strong": 0, "items": [], "index_level": "L1",
            "content_hits": 0, "truncated": False, "stale": False, "rewritten": None,
            "vector_used": False, "vector_model": None, "vector_error": None,
        }

    monkeypatch.setattr(search, "search", fake_search)
    before = time.time_ns()
    assert client.get("/api/search", params={"q": "样本量", "mtime_days": 7}).status_code == 200
    after = time.time_ns()
    expected = 7 * 86_400 * 1_000_000_000
    assert before - expected - 1_000_000 <= captured["mtime_after_ns"] <= after - expected + 1_000_000

    captured.clear()
    assert client.get("/api/search", params={"q": "样本量"}).status_code == 200
    assert captured["mtime_after_ns"] is None


def test_api_pause_and_events(client, tree):
    source_id = client.post("/api/sources", json={"root": str(tree)}).json()["source_id"]
    assert client.post(f"/api/sources/{source_id}/state", params={"state": "paused"}).status_code == 200
    events = client.get("/api/events").json()["items"]
    assert events[0]["type"] == "source_state"
    assert client.post(f"/api/sources/{source_id}/state", params={"state": "bogus"}).status_code == 422


def test_phrase_query_requires_phrase_hit_for_strong_evidence(conn, tmp_path):
    """问题里有连续短语时，只命中零散 2-gram 不能算强证据（拒答门槛）。"""
    from shiyuan import indexer

    root = tmp_path / "资料"
    root.mkdir()
    (root / "笔记.md").write_text("这里只讨论土壤，没有别的内容。\n", encoding="utf-8")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    miss = search.search(conn, "火星探测器着陆点的土壤成分分析", use_vector=False)
    assert miss["strong"] == 0
    assert all(item["weak"] for item in miss["items"])

    (root / "笔记.md").write_text("土壤成分分析显示氮含量偏高。\n", encoding="utf-8")
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    hit = search.search(conn, "火星探测器着陆点的土壤成分分析", use_vector=False)
    assert hit["strong"] >= 1
    assert any(not item["weak"] for item in hit["items"])


def test_search_and_index_work_without_embedding_model(conn, tree):
    """默认不配置向量/LLM 时，建库与检索仍可用（Phase 1 的「无 LLM 可运行」）。"""
    from shiyuan import embeddings, indexer

    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    assert embeddings.load_settings(conn)["provider"] == "none"
    assert conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0

    result = search.search(conn, "blast")

    assert result["total"] >= 1
    assert result["vector_used"] is False
    assert result["vector_error"] is None


def test_long_query_with_only_generic_path_match_is_not_strong(conn, tmp_path):
    """长问题只命中「2022」「成绩单」这类通用词面时，不能当作强证据（拒答）。"""
    from shiyuan import indexer

    root = tmp_path / "资料"
    root.mkdir()
    (root / "本科成绩单.txt").write_text(
        "2022 学年成绩：绩点 3.8，专业排名 12。\n", encoding="utf-8")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = search.search(conn, "2022 年冬奥会短道速滑决赛成绩单", use_vector=False)

    assert result["strong"] == 0
    assert all(item["weak"] for item in result["items"])


def test_search_ablation_switches_do_not_change_name_hits(conn, tree):
    """消融开关只影响向量与家族通道，不应丢掉文件名/路径命中。"""
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    base = search.search(conn, "blast")
    ablated = search.search(conn, "blast", use_vector=False, use_family=False)

    assert ablated["total"] == base["total"] == 1
    assert ablated["items"][0]["name"] == "BLAST结果表.xlsx"


def test_rrf_fusion_keeps_exact_name_hit(conn, tree):
    """RRF 只改融合方式，不应丢掉文件名/路径命中。"""
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    fused = search.search(conn, "blast", fusion="rrf")

    assert fused["total"] == 1
    assert fused["items"][0]["name"] == "BLAST结果表.xlsx"


def test_bridge_terms_extract_identifiers():
    """第二轮线索只抽取编号/版本型桥接词，不把普通查询词当线索。"""
    analysis = {"variants": ["项目", "编号"]}
    hits = {
        "a": {
            "text": "项目代号 PRJNA123456，版本 v0.3，染色体 Chr01。",
            "maximal_weight": 20,
            "maximal": 2,
        },
    }

    terms = search._bridge_terms(hits, analysis)

    assert "PRJNA123456" in terms
    assert "v0.3" in terms
    assert "Chr01" in terms
    assert all(term not in ("项目", "编号") for term in terms)


def test_search_marks_stale_when_file_changed_after_index(conn, tmp_path):
    """文件在解析之后又变过时，结果应标记 stale，提示重新索引。"""
    from shiyuan import indexer

    root = tmp_path / "过期"
    root.mkdir()
    (root / "记录.md").write_text("样本量是 42 的记录。\n", encoding="utf-8")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    fresh = search.search(conn, "样本量", use_vector=False)
    assert fresh["stale"] is False
    assert all(item["stale"] is False for item in fresh["items"])

    # 模拟索引后文件再次变化：location mtime 前进，解析版本 mtime 不变。
    conn.execute("UPDATE asset_locations SET mtime_ns = mtime_ns + 1000000000")
    conn.commit()

    changed = search.search(conn, "样本量", use_vector=False)
    assert changed["stale"] is True
    assert any(item["stale"] for item in changed["items"])


def test_search_mtime_filter_applies_to_path_and_content(conn, tmp_path):
    """时间过滤同时约束路径命中与正文命中，旧文件不能从任一通道漏出。"""
    from shiyuan import indexer

    root = tmp_path / "时间"
    root.mkdir()
    old = root / "旧报告.md"
    new = root / "新报告.md"
    old.write_text("样本量 42 的旧报告。\n", encoding="utf-8")
    new.write_text("样本量 42 的新报告。\n", encoding="utf-8")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    conn.execute(
        "UPDATE asset_locations SET mtime_ns = ? WHERE path = ?",
        (time.time_ns() - 30 * 86_400 * 1_000_000_000, str(old)),
    )
    conn.commit()

    recent = search.search(
        conn, "样本量 报告", use_vector=False,
        mtime_after_ns=time.time_ns() - 7 * 86_400 * 1_000_000_000)
    names = [item["name"] for item in recent["items"]]
    assert "新报告.md" in names
    assert "旧报告.md" not in names

    all_time = search.search(conn, "样本量 报告", use_vector=False)
    assert {item["name"] for item in all_time["items"]} >= {"新报告.md", "旧报告.md"}


def test_index_freshness_tracks_changed_files_and_jobs(conn, tmp_path):
    """source 级新鲜度：文件改动未重索引、任务积压都要报 stale。"""
    from shiyuan import indexer

    root = tmp_path / "新鲜度"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42\n", encoding="utf-8")
    source = store.add_source(conn, root)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    fresh = search.index_freshness(conn)
    assert fresh["stale"] is False
    assert fresh["stale_files"] == 0
    assert fresh["pending_jobs"] == 0
    assert fresh["sources"][0]["last_indexed_at"]

    conn.execute("UPDATE asset_locations SET mtime_ns = mtime_ns + 1000000000")
    conn.commit()
    changed = search.index_freshness(conn)
    assert changed["stale"] is True
    assert changed["stale_files"] == 1
    assert changed["sources"][0]["stale"] is True

    conn.execute("UPDATE index_jobs SET status = 'pending'")
    conn.commit()
    pending = search.index_freshness(conn)
    assert pending["pending_jobs"] == 1
    assert pending["stale"] is True

    conn.execute("UPDATE index_jobs SET status = 'failed'")
    conn.commit()
    failed = search.index_freshness(conn)
    assert failed["failed_jobs"] == 1
    assert failed["stale"] is True


def test_stats_includes_source_freshness(conn, tree):
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])

    data = search.stats(conn)

    assert "freshness" in data
    assert data["freshness"]["sources"][0]["source_id"] == source["source_id"]


def test_fetch_locations_hides_paused_sources(conn, tree):
    """暂停的数据源不能经「最新版本 / 点名版本」注入通道重新出现在结果里。"""
    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    location_id = conn.execute(
        "SELECT location_id FROM asset_locations LIMIT 1").fetchone()[0]

    assert location_id in search._fetch_locations(conn, [location_id])
    store.set_source_state(conn, source["source_id"], "paused")
    assert search._fetch_locations(conn, [location_id]) == {}


def test_manual_full_parse_does_not_consume_daily_budget(conn, tree):
    """用户显式触发的完整解析不受每日后台预算限制，也不占用当天额度。"""
    from shiyuan import budget, indexer

    source = store.add_source(conn, tree)
    _scan(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"], ignore_budget=True)

    assert budget.info(conn)["l1_used"] == 0


def test_app_home_prefers_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "home"))
    assert config.app_home() == tmp_path / "home"


def test_app_home_follows_install_dir_when_frozen(tmp_path, monkeypatch):
    """安装版把数据写在 <安装根>\\data，跟随安装位置而不是固定到 LOCALAPPDATA。"""
    monkeypatch.delenv("SHIYUAN_HOME", raising=False)
    install = tmp_path / "SourceLoom"
    exe = install / "binaries" / "shiyuan-core" / "shiyuan-core.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert config.app_home() == install / "data"
    assert (install / "data").is_dir()


def test_app_home_uses_localappdata_for_source_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIYUAN_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.app_home() == tmp_path / "local" / config.APP_DIR_NAME
