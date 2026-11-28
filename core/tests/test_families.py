from __future__ import annotations

import json
from pathlib import Path

import pytest

from shiyuan import db, families, indexer, retrieval, scanner, search, store
from shiyuan.mcp_server import MCPServer


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "families.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _seed_versions(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    shared = (
        "广东省城市科技创新评价研究。"
        + "本文以广东省二十一个地级市为样本，构建综合评价指标体系。" * 30
    )
    (root / "论文初稿.md").write_text(
        shared + "\n初稿内容，样本量为四十二。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "论文修改版.md").write_text(
        shared + "\n修改版增加了稳健性检验。", encoding="utf-8")
    (root / "论文终稿.md").write_text(
        shared + "\n终稿增加了稳健性检验与政策建议。", encoding="utf-8")
    (root / "论文初稿_备份.md").write_text(
        shared + "\n初稿内容，样本量为四十二。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "另一篇论文.md").write_text(
        "海洋微生物基因表达与氮循环，和城市创新评价无关。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, source["source_id"]


def test_latest_version_prefers_final_over_newer_backup(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    final = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path LIKE '%论文终稿.md'"
    ).fetchone()["asset_id"]
    mapping = families.family_map(conn)
    assert mapping[final]["is_latest_version"] is True
    assert mapping[final]["family_latest_version_label"] == "终稿"

    members = families.family_members(conn, mapping[final]["family_id"])
    assert members[0]["name"] == "论文终稿.md"
    assert members[0]["is_latest"] is True

    server = MCPServer(conn)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_document_families",
                   "arguments": {"query": "论文"}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["total"] >= 1
    item = payload["items"][0]
    assert item["latest_version_label"] == "终稿"
    assert item["latest_path"].endswith("论文终稿.md")
    assert {"初稿", "终稿"} <= {version["label"] for version in item["versions"]}


def test_latest_version_injection_is_topic_scoped(conn, tmp_path):
    """问「手稿最新版」时不能把无关家族的最新版本注入到前面。"""
    root = tmp_path / "版本"
    root.mkdir()
    for version in ("v0.1", "v0.2"):
        (root / f"手稿_{version}.md").write_text(
            f"褐篮子鱼手稿 {version} 的内容。", encoding="utf-8")
    for version in ("v0.1", "v0.2", "v0.3", "v0.4", "v0.5"):
        # 版本更多、家族更大的无关文件，旧逻辑会把它排在前面。
        (root / f"图谱_{version}.md").write_text(
            f"与手稿无关的图谱 {version} 内容。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    result = search.search(conn, "手稿最新版本是哪个", use_vector=False)

    assert result["items"][0]["name"] == "手稿_v0.2.md"


def test_routing_preserves_version_answers(conn, tmp_path):
    """按类型路由时，版本问题仍走家族归并与最新版注入。"""
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    result = search.search(conn, "论文最新的版本是哪个", use_vector=False,
                           use_routing=True)

    assert result["items"][0]["name"] == "论文终稿.md"


def test_builds_family_without_recommendation(conn, tmp_path):
    _seed_versions(conn, tmp_path)

    result = families.rebuild(conn)
    assert result["families"] == 1
    assert result["members"] >= 3

    family = conn.execute("SELECT * FROM document_families").fetchone()
    assert family["recommended_asset_id"] is None
    rows = conn.execute(
        "SELECT m.is_recommended, m.version_label, l.path"
        " FROM document_family_members m"
        " LEFT JOIN asset_locations l ON l.asset_id = m.asset_id"
    ).fetchall()
    names = {Path(row["path"]).name: row for row in rows if row["path"]}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names
    assert "论文初稿.md" in names or "论文初稿_备份.md" in names
    assert "另一篇论文.md" not in names
    assert all(row["is_recommended"] == 0 for row in rows)
    assert names["论文终稿.md"]["version_label"] == "终稿"

    groups = families.content_groups(conn, family["family_id"])
    shared = [group for group in groups if "综合评价指标体系" in group["text"]]
    assert shared
    assert any(
        len({source["asset_id"] for source in group["sources"]}) >= 2
        for group in shared
    )
    unique = [group for group in groups if "样本选择存在局限" in group["text"]]
    assert unique
    unique_names = {source["name"] for source in unique[0]["sources"]}
    assert unique_names & {"论文初稿.md", "论文初稿_备份.md"}
    assert "论文终稿.md" not in unique_names


def test_generic_tool_names_are_not_merged(conn, tmp_path):
    root = tmp_path / "图片"
    root.mkdir()
    for index, name in enumerate(
        ("Snipaste_2025-01-01_10-00-00.png", "Snipaste_2025-02-02_11-00-00.png")
    ):
        (root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (16 + index))
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    families.rebuild(conn)
    assert conn.execute("SELECT COUNT(*) FROM document_families").fetchone()[0] == 0


def test_non_document_extensions_are_ignored(conn, tmp_path):
    root = tmp_path / "代码"
    (root / "一").mkdir(parents=True)
    (root / "二").mkdir(parents=True)
    (root / "一" / "shared.css").write_text("body { color: red; }", encoding="utf-8")
    (root / "二" / "shared.css").write_text("body { color: red; }", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    families.rebuild(conn)
    assert conn.execute("SELECT COUNT(*) FROM document_families").fetchone()[0] == 0


def test_search_aggregates_family_versions(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    result = search.search(conn, "城市科技创新")
    family_items = [
        item for item in result["items"] if (item.get("family_size") or 1) > 1
    ]
    assert len(family_items) == 1
    item = family_items[0]
    assert item["family_size"] >= 3
    assert len(item["family_versions"]) >= 3
    assert not item.get("is_recommended")
    names = {version["name"] for version in item["family_versions"]}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names


def test_shared_content_merges_sources(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    hits = retrieval.find_chunks(conn, "综合评价指标体系")
    shared = [
        hit for hit in hits
        if "综合评价指标体系" in hit.text and len(hit.sources) >= 2
    ]
    assert shared, "共享段落应合并为一条并保留多个来源"
    names = {source["name"] for source in shared[0].sources}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names


def test_unique_first_draft_content_is_retrievable(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    hits = retrieval.find_chunks(conn, "样本选择存在局限")
    unique = [hit for hit in hits if "样本选择存在局限" in hit.text]
    assert unique, "仅初稿存在的段落必须仍然可检索"
    sources = {source["name"] for hit in unique for source in hit.sources}
    assert "论文初稿.md" in sources

    result = search.search(conn, "样本选择存在局限")
    assert result["items"]
    assert any(
        "论文初稿.md" in {
            version["name"] for version in item.get("family_versions", [])
        }
        for item in result["items"]
    )


def test_diff_reports_shared_and_modified(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    members = families.family_members(conn, family["family_id"])
    by_name = {member["name"]: member["asset_id"] for member in members}
    result = families.diff_versions(
        conn, family["family_id"],
        by_name["论文初稿.md"], by_name["论文终稿.md"],
    )

    assert result["summary"]["shared"] >= 1
    left_texts = " ".join(item["text"] for item in result["removed"])
    left_texts += " ".join(item["left"]["text"] for item in result["modified"])
    assert "样本选择存在局限" in left_texts
    right_texts = " ".join(item["text"] for item in result["added"])
    right_texts += " ".join(item["right"]["text"] for item in result["modified"])
    assert "政策建议" in right_texts


def test_diff_separates_added_and_removed(conn, tmp_path):
    root = tmp_path / "短稿"
    root.mkdir()
    (root / "城市创新研究初稿.md").write_text(
        "研究背景与问题提出。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "城市创新研究终稿.md").write_text(
        "研究背景与问题提出。\n终稿新增的政策建议：加强跨区域协同。",
        encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    assert family is not None
    members = families.family_members(conn, family["family_id"])
    by_name = {member["name"]: member["asset_id"] for member in members}
    result = families.diff_versions(
        conn, family["family_id"],
        by_name["城市创新研究初稿.md"], by_name["城市创新研究终稿.md"],
    )

    assert any("样本选择存在局限" in item["text"] for item in result["removed"])
    assert any("政策建议" in item["text"] for item in result["added"])


def test_diff_pairs_near_identical_paragraph_as_modified():
    base = "本文使用双向固定效应模型进行回归分析，并控制城市固定效应与年份固定效应。" * 20
    groups = {
        "a": {"content_group_id": "a", "order": 1,
              "text": base + "样本区间为二〇一五年至二〇二三年。", "assets": {}},
        "b": {"content_group_id": "b", "order": 2,
              "text": base + "样本区间为二〇一〇年至二〇二四年。", "assets": {}},
        "c": {"content_group_id": "c", "order": 3,
              "text": "完全不同的内容，讨论海洋微生物与氮循环之间的关系。" * 10,
              "assets": {}},
    }

    modified, removed, added = families._pair_modified(groups, ["a", "c"], ["b"])
    assert [left for _, left, _ in modified] == ["a"]
    assert removed == ["c"]
    assert added == []


def test_diff_rejects_asset_outside_family(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    family = conn.execute("SELECT family_id FROM document_families").fetchone()

    with pytest.raises(ValueError):
        families.diff_versions(
            conn, family["family_id"], "asset_missing", "asset_missing_2",
        )


def test_family_id_is_stable_across_rebuilds(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    first = [row[0] for row in conn.execute("SELECT family_id FROM document_families")]

    families.rebuild(conn)
    second = [row[0] for row in conn.execute("SELECT family_id FROM document_families")]

    assert first == second


def test_explicit_version_query_returns_that_version(conn, tmp_path):
    """点名旧版本时，即使词面候选被大量同名内容挤占，也要返回该版本并标记历史。"""
    root = tmp_path / "版本对比"
    root.mkdir()
    (root / "manuscript_v0.2.md").write_text(
        "旧版本正文，实验部分改写前的记录。", encoding="utf-8")
    (root / "manuscript_v0.3.md").write_text(
        "新版本正文，实验部分已经补充。", encoding="utf-8")
    for index in range(20):
        (root / f"busco_note_{index}.md").write_text(
            "BUSCO 完整度 98.4%，单拷贝 96.4%。\n" * 80, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    result = search.search(conn, "v0.2 的 BUSCO 完整度", limit=5)
    first = result["items"][0]
    assert first["name"] == "manuscript_v0.2.md"
    assert first["version_label"] == "0.2"
    assert first["is_latest_version"] is False
    assert first["family_latest_version_label"] == "0.3"


def test_list_document_families_query_filters(conn, tmp_path):
    """query 必须真正过滤；查不到就返回空，而不是退回全量列表。"""
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    server = MCPServer(conn)

    def call(query: str) -> dict:
        response = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_document_families", "arguments": {"query": query}},
        })
        return json.loads(response["result"]["content"][0]["text"])

    assert call("论文")["total"] >= 1
    assert call("zzzqqq 不存在的关键词")["total"] == 0


def test_compare_versions_output_is_capped(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    members = families.family_members(conn, family["family_id"])
    by_name = {member["name"]: member["asset_id"] for member in members}

    server = MCPServer(conn)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "compare_versions", "arguments": {
            "family_id": family["family_id"],
            "left_asset_id": by_name["论文初稿.md"],
            "right_asset_id": by_name["论文终稿.md"],
            "limit": 1,
            "max_chars": 100,
        }},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["limit"] == 1
    assert payload["max_chars"] == 100
    assert all(len(item["text"]) <= 100 for item in payload["shared"])
    assert all(len(item["text"]) <= 100 for item in payload["removed"])
    assert all(len(item["text"]) <= 100 for item in payload["added"])
    for item in payload["modified"]:
        assert len(item["left"]["text"]) <= 100
        assert len(item["right"]["text"]) <= 100
    assert all(value <= 1 for value in payload["returned"].values())


def test_doc_roles_keep_auxiliary_files_out_of_main_family():
    assert families._doc_role("CBPD_graphical_abstract_v0.13.pdf") == "graphical_abstract"
    assert families._doc_role("CBPD_cover_letter_v0.13.md") == "cover_letter"
    assert families._doc_role("CBPD_highlights_v0.13.docx") == "highlights"
    assert families._doc_role("CBPD_Supplementary_Tables_v0.13.pdf") == "supplementary"
    assert families._doc_role("Siganus_fuscescens_CBPD_clean_v0.13.md") == "main"
    assert families._doc_role("导师批注原件.docx") is None

    assert not families._merge_roles_compatible(
        {"name": "xxx_clean_v1.md"}, {"name": "xxx_graphical_abstract_v1.pdf"})
    assert not families._merge_roles_compatible(
        {"name": "xxx_cover_letter_v1.md"}, {"name": "xxx_highlights_v1.md"})
    # 正文与未标注文件仍可合并（例如导师批注、旧命名稿件）。
    assert families._merge_roles_compatible(
        {"name": "xxx_clean_v1.md"}, {"name": "导师批注原件.docx"})


def test_text_overlap_distinguishes_revision_from_template():
    base = "本文以广东省地级市为样本，构建综合评价指标体系并做稳健性检验。" * 120
    revised = base + "新增政策建议一节，讨论跨区域协同。" * 15
    other = "第9章 排序：快速排序、堆排序与归并排序的时间复杂度分析。" * 120

    assert families._text_overlap(base, revised) >= families.TEXT_OVERLAP_MIN
    assert families._text_overlap(base, other) < families.TEXT_OVERLAP_MIN


def test_graphical_abstract_stays_out_of_manuscript_family(conn, tmp_path):
    root = tmp_path / "论文"
    root.mkdir()
    title = "Improved chromosome-level genome of Siganus fuscescens\n"
    body = "The mottled rabbitfish is a marine teleost with economic value. " * 90
    (root / "paper_clean_v1.md").write_text(title + body, encoding="utf-8")
    (root / "paper_graphical_abstract_v1.md").write_text(
        title + body[: len(body) // 4] + "\nGraphical summary of the assembly.\n",
        encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    mapping = families.family_map(conn)
    manuscript = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path LIKE '%paper_clean_v1.md'"
    ).fetchone()["asset_id"]
    graphic = conn.execute(
        "SELECT asset_id FROM asset_locations WHERE path LIKE '%graphical_abstract_v1.md'"
    ).fetchone()["asset_id"]
    left, right = mapping.get(manuscript), mapping.get(graphic)
    assert not (left and right and left["family_id"] == right["family_id"])


def test_date_labels_resolved_in_family_context():
    types = families._resolve_label_types(["4.22", "4.21", "终稿 10"])
    assert types["4.22"] == "date"
    assert types["4.21"] == "date"
    assert types["终稿 10"] == "final_seq"

    types = families._resolve_label_types(["0.13", "0.9"])
    assert types == {"0.13": "numeric", "0.9": "numeric"}


def test_latest_version_reports_basis_and_confidence():
    records = [
        {"family_id": "f", "asset_id": "a", "version_label": "4.22",
         "mtime_ns": 200, "ext": "doc", "path": "a.doc"},
        {"family_id": "f", "asset_id": "b", "version_label": "终稿 10",
         "mtime_ns": 100, "ext": "doc", "path": "b.doc"},
    ]
    info = families._latest_versions(records)["f"]
    assert info["asset_id"] == "b"
    assert info["version_type"] == "final_seq"
    assert info["basis"] == "version_label"
    assert info["confidence"] == "high"

    unlabeled = [
        {"family_id": "g", "asset_id": "a", "version_label": "",
         "mtime_ns": 10, "ext": "doc", "path": "a.doc"},
        {"family_id": "g", "asset_id": "b", "version_label": "",
         "mtime_ns": 20, "ext": "doc", "path": "b.doc"},
    ]
    info = families._latest_versions(unlabeled)["g"]
    assert info["asset_id"] == "b"
    assert info["basis"] == "modified_time"
    assert info["confidence"] == "low"


def test_family_id_stable_when_new_version_added(conn, tmp_path):
    root = tmp_path / "稳定族"
    root.mkdir()
    for day in ("2024.01.01", "2024.02.02"):
        (root / f"城市创新研究_{day}.md").write_text(
            f"城市创新研究，样本量 42。\n版本日期 {day}\n", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)
    first = conn.execute("SELECT family_id FROM document_families").fetchone()["family_id"]

    (root / "城市创新研究_2024.03.03.md").write_text(
        "城市创新研究，样本量 48。\n版本日期 2024.03.03\n", encoding="utf-8")
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)
    second = conn.execute("SELECT family_id FROM document_families").fetchone()["family_id"]
    assert first == second


def test_audit_reports_no_mixed_role_families(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    report = families.audit(conn)
    assert report["mixed_roles"] == []
    assert report["families"] >= 1


def test_version_rank_prefers_final_and_orders_numbers():
    assert families._version_rank("终稿 10") > families._version_rank("终稿 9")
    assert families._version_rank("终稿 10") > families._version_rank("4.22")
    assert families._version_rank("终稿 10") > families._version_rank("最新")
    assert families._version_rank("4.22") > families._version_rank("最新")
    assert families._version_rank("最新") > families._version_rank("初稿")
    assert families._version_rank("0.13") > families._version_rank("0.9")
    assert families._version_rank("初稿") > families._version_rank("备份")


def test_latest_versions_prefers_explicit_final_over_date_label():
    records = [
        {"family_id": "f", "asset_id": "a", "version_label": "4.22",
         "mtime_ns": 200, "ext": "doc", "path": "a.doc"},
        {"family_id": "f", "asset_id": "b", "version_label": "终稿 10",
         "mtime_ns": 100, "ext": "doc", "path": "b.doc"},
    ]
    latest = families._latest_versions(records)["f"]
    assert latest["asset_id"] == "b"


def test_unlabeled_duplicate_family_has_unknown_latest(conn, tmp_path):
    """只有重复副本、没有版本标记的家族不能声称存在“最新版本”。"""
    root = tmp_path / "重复"
    root.mkdir()
    body = "同一份实验记录的两次导出，内容完全一致。" * 40
    (root / "alpha_notes.md").write_text(body, encoding="utf-8")
    (root / "beta_notes.md").write_text(body, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    assert conn.execute("SELECT COUNT(*) FROM document_families").fetchone()[0] == 1
    mapping = families.family_map(conn)
    assert len(mapping) == 2
    assert all(info["is_latest_version"] is None for info in mapping.values())
    assert all(info["family_latest_version_label"] is None for info in mapping.values())
    # 没有版本标签时仍能给出“最后修改”的成员，供外部 Agent 如实回答。
    assert len({info["family_newest_asset_id"] for info in mapping.values()}) == 1
    assert all(info["family_newest_mtime_ns"] > 0 for info in mapping.values())

    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    members = families.family_members(conn, family["family_id"])
    assert all(member["is_latest"] is None for member in members)

    server = MCPServer(conn)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_document_families", "arguments": {}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["has_versions"] is False
    assert item["latest_version_label"] is None
    assert item["newest_asset_id"]
