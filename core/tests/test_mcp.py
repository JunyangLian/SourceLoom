from __future__ import annotations

import io
import json
import sys

import pytest

from shiyuan import db, external, indexer, mcp_server, projects, scanner, store
from shiyuan.mcp_server import MCPServer, serve


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "mcp.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def seeded(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "实验记录.md").write_text(
        "NovaSeq 平台 样本量 42\n" + "长段落内容 " * 300,
        encoding="utf-8",
    )
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, source


@pytest.fixture()
def server(conn, seeded):
    return MCPServer(conn)


def _call(server: MCPServer, name: str, arguments: dict) -> tuple[dict, dict | None]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    result = response["result"]
    payload = None if result["isError"] else json.loads(result["content"][0]["text"])
    return result, payload


def test_initialize_negotiates_protocol_and_capabilities(server):
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["capabilities"]["resources"]["subscribe"] is False
    assert result["serverInfo"]["name"] == "shiyuan-mcp"
    assert "search_knowledge" in result["instructions"]

    unknown = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert unknown["result"]["protocolVersion"] == mcp_server.DEFAULT_PROTOCOL


def test_notifications_get_no_response(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_readonly_tools(server):
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert set(tools) == {
        "search_knowledge", "get_document_text", "list_sources", "knowledge_stats",
        "list_projects", "get_project", "search_assets", "get_evidence",
        "get_document_family", "list_document_families", "compare_versions",
        "get_related_documents", "get_source_status",
    }
    assert tools["search_knowledge"]["annotations"]["readOnlyHint"] is True
    assert "query" in tools["search_knowledge"]["inputSchema"]["required"]
    assert "location_id" in tools["get_document_text"]["inputSchema"]["properties"]


def test_search_and_read_document(server, seeded):
    root, _ = seeded
    result, payload = _call(server, "search_knowledge", {"query": "NovaSeq"})
    assert result["isError"] is False
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["name"] == "实验记录.md"
    assert item["path"] == str(root / "实验记录.md")
    assert "NovaSeq" in item["snippet"]
    # 统一证据接口：覆盖范围 + 字符区间。
    assert payload["coverage"] == "ranked_sample"
    assert payload["truncated"] is False
    assert payload["stale"] is False
    assert item["stale"] is False
    assert item["range"]["kind"] == "text"
    assert item["range"]["endOffset"] > item["range"]["startOffset"]

    result, document = _call(
        server, "get_document_text", {"location_id": item["location_id"]}
    )
    assert result["isError"] is False
    assert document["location"]["name"] == "实验记录.md"
    assert "NovaSeq" in document["text"]
    assert document["sections"][0]["stage"] == "L1"
    assert document["artifacts"][0]["state"] == "ok"


def test_document_path_lookup_and_truncation(server, seeded):
    root, _ = seeded
    _, document = _call(
        server,
        "get_document_text",
        {"path": str(root / "实验记录.md"), "max_chars": 200},
    )
    assert document["truncated"] is True
    assert len(document["text"]) <= 200
    assert document["total_chars"] > 200


def test_list_sources_and_stats(server, seeded):
    _, sources = _call(server, "list_sources", {})
    assert sources["total"] == 1
    source = sources["items"][0]
    assert source["label"] == "资料"
    assert source["available"] == 1
    assert source["state"] == "active"

    _, stats = _call(server, "knowledge_stats", {})
    assert stats["files"]["available"] == 1
    assert stats["coverage"]["content_indexed"] == 1
    assert stats["freshness"]["sources"][0]["source_id"] == source["source_id"]

    _, status = _call(server, "get_source_status", {})
    assert status["freshness"]["stale"] is False
    assert status["freshness"]["sources"][0]["source_id"] == source["source_id"]


def test_resources_list_and_read(server):
    listing = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    uris = [item["uri"] for item in listing["result"]["resources"]]
    assert uris == ["shiyuan://sources", "shiyuan://stats"]

    read = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "shiyuan://sources"},
        }
    )
    payload = json.loads(read["result"]["contents"][0]["text"])
    assert payload["total"] == 1

    missing = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "shiyuan://missing"},
        }
    )
    assert missing["error"]["code"] == -32002


def test_unknown_method_and_invalid_tool_arguments(server):
    unknown = server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert unknown["error"]["code"] == -32601

    result, payload = _call(server, "search_knowledge", {})
    assert result["isError"] is True
    assert payload is None
    assert "query" in result["content"][0]["text"]


def test_select_chunks_prefers_ocr_for_pdf_and_combines_pptx():
    pdf_chunks = [
        {"page": 1, "sheet": None, "ordinal": 0, "stage": "L1", "text": "乱码"},
        {"page": 1, "sheet": None, "ordinal": 0, "stage": "L2", "text": "OCR"},
        {"page": 2, "sheet": None, "ordinal": 0, "stage": "L1", "text": "正文"},
    ]
    pdf_selected = MCPServer._select_chunks("pdf", pdf_chunks)
    assert [(chunk["page"], chunk["stage"], chunk["text"]) for chunk in pdf_selected] == [
        (1, "L2", "OCR"),
        (2, "L1", "正文"),
    ]

    pptx_chunks = [
        {"page": 1, "sheet": None, "ordinal": 1, "stage": "L2", "text": "图片"},
        {"page": 1, "sheet": None, "ordinal": 0, "stage": "L1", "text": "标题"},
    ]
    pptx_selected = MCPServer._select_chunks("pptx", pptx_chunks)
    assert [(chunk["stage"], chunk["text"]) for chunk in pptx_selected] == [
        ("L1", "标题"),
        ("L2", "图片"),
    ]


def test_stdio_framing_skips_notifications(conn, seeded):
    payload = (
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    )
    output = io.BytesIO()
    serve(conn, io.BytesIO(payload), output)
    lines = output.getvalue().decode("utf-8").strip().splitlines()
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["id"] == 1
    assert response["result"] == {}


def test_config_snippet_points_at_core_and_home():
    snippet = mcp_server.config_snippet("syext_test")
    server = snippet["mcpServers"]["shiyuan"]
    assert server["args"] == ["-m", "shiyuan.mcp_server"]
    assert server["cwd"].endswith("core")
    assert server["env"]["SHIYUAN_HOME"]
    assert server["env"]["SHIYUAN_EXT_TOKEN"] == "syext_test"


def test_frozen_config_uses_merged_core_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "shiyuan-core.exe"))
    snippet = mcp_server.config_snippet()
    server = snippet["mcpServers"]["shiyuan"]
    assert server["command"].endswith("shiyuan-core.exe")
    assert server["args"] == ["--mcp"]
    assert "SHIYUAN_EXT_TOKEN" not in server["env"]


def test_revoked_external_client_is_rejected_at_runtime(conn, seeded):
    """令牌撤销后，已经运行中的 MCP 会话下一次调用就应被拒绝。"""
    client = external.create_client(conn, preset="codex", transport="mcp")
    server = MCPServer(conn, client_id=client["client_id"])

    result, _ = _call(server, "list_sources", {})
    assert result["isError"] is False

    external.delete_client(conn, client["client_id"])
    result, _ = _call(server, "list_sources", {})
    assert result["isError"] is True
    assert "撤销" in result["content"][0]["text"]


def test_project_switched_to_local_only_blocks_running_mcp(conn, seeded):
    """项目改成 local_only 后，运行中的 MCP 会话必须立即失去读取权限。"""
    project = projects.create_project(conn, "分享项目")
    client = external.create_client(conn, preset="codex", transport="mcp",
                                    project_id=project["project_id"])
    server = MCPServer(conn, project_id=project["project_id"],
                       client_id=client["client_id"])

    result, _ = _call(server, "list_sources", {})
    assert result["isError"] is False

    projects.update_project(conn, project["project_id"], privacy_policy="local_only")
    result, _ = _call(server, "list_sources", {})
    assert result["isError"] is True
    assert "本地" in result["content"][0]["text"]
