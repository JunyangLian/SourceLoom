"""MCP (Model Context Protocol) 服务：让本机 Agent 直接检索拾源知识库。

传输使用 stdio（每行一个 JSON-RPC 2.0 消息），只读访问与桌面端相同的 SQLite 数据库。
不扫描、不解析、不改原件；需要新内容时仍由桌面端完成扫描与索引。

启动：
    cd core
    python -m shiyuan.mcp_server

也可以从任意目录直接运行：
    python <repo>/core/mcp_server.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from . import (__version__, config, db, embeddings, external, families, projects,
               search, store)

SERVER_NAME = "shiyuan-mcp"
DEFAULT_PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
DEFAULT_MAX_CHARS = 20_000
MAX_MAX_CHARS = 100_000


class ToolError(RuntimeError):
    """工具参数或数据状态导致的、可以原样返回给 Agent 的错误。"""


def _debug(message: str) -> None:
    if os.environ.get("SHIYUAN_MCP_DEBUG", "").strip().lower() in {"", "0", "false", "off"}:
        return
    line = f"[shiyuan-mcp] {message}\n".encode("utf-8", errors="replace")
    stream = getattr(sys.stderr, "buffer", None) if sys.stderr is not None else None
    try:
        if stream is not None:
            stream.write(line)
            stream.flush()
        else:
            os.write(2, line)
    except OSError:
        pass


def _binary_stream(name: str, fd: int, mode: str) -> BinaryIO:
    """冻结为 --noconsole 时 sys.stdin/stdout 可能为 None，但管道句柄仍然有效。"""
    stream = getattr(sys, name, None)
    buffer = getattr(stream, "buffer", None) if stream is not None else None
    if buffer is not None:
        return buffer
    try:
        return os.fdopen(fd, mode, buffering=0)
    except OSError:
        if os.name != "nt":
            raise
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint]
        handle = kernel32.GetStdHandle(-10 if name == "stdin" else -11)
        if handle in (None, 0, -1) or handle == 2 ** 64 - 1:
            raise RuntimeError(
                f"{name} 不可用；MCP 客户端必须用管道启动 shiyuan-core.exe --mcp"
            )
        flags = os.O_RDONLY if "r" in mode else os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, buffering=0)


def _error(message_id: Any, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _result(message_id: Any, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": payload}


def _tool_text(payload: dict | list) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _string(args: dict, name: str, *, required: bool = True) -> str | None:
    value = args.get(name)
    if value is None:
        if required:
            raise ToolError(f"缺少参数 {name}")
        return None
    text = str(value).strip()
    if not text:
        if required:
            raise ToolError(f"参数 {name} 不能为空")
        return None
    return text


def _integer(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"参数 {name} 必须是整数") from exc
    return max(minimum, min(maximum, number))


def _stage_of(signature: str | None) -> str:
    return "L2" if (signature or "").startswith("l2.") else "L1"


class MCPServer:
    """把知识库能力映射成 MCP 的 tools 与 resources。"""

    def __init__(self, conn: sqlite3.Connection, project_id: str | None = None,
                 client_id: str | None = None) -> None:
        self.conn = conn
        # 外部令牌绑定到项目后，所有读取都必须限制在该项目范围内。
        self.bound_project_id = project_id or None
        self.client_id = client_id
        self.bound_global = (
            projects.is_global(conn, self.bound_project_id)
            if self.bound_project_id else False
        )

    def _ensure_session(self) -> None:
        """每次调用都复核令牌与项目隐私，撤销令牌或改为 local_only 立即生效。"""
        if not self.client_id:
            return
        row = self.conn.execute(
            "SELECT state FROM external_clients WHERE client_id = ?",
            (self.client_id,),
        ).fetchone()
        if row is None or row["state"] != "active":
            raise ToolError("该外部连接已被撤销，请重新创建令牌")
        if not self.bound_project_id:
            return
        project = self.conn.execute(
            "SELECT status, privacy_policy FROM projects WHERE project_id = ?",
            (self.bound_project_id,),
        ).fetchone()
        if project is None or project["status"] != "active":
            raise ToolError("绑定项目不存在或已归档")
        if project["privacy_policy"] != "external_allowed":
            raise ToolError("该项目仅允许本地使用，不能通过 MCP 读取")

    def _bound_scope(self, project_id: str | None,
                     scope: str | None) -> tuple[str | None, str | None]:
        if not self.bound_project_id:
            return project_id, scope
        if project_id and project_id != self.bound_project_id:
            raise ToolError("该接口只能访问绑定的项目")
        if self.bound_global:
            return self.bound_project_id, "global"
        chosen = scope if scope in {"project", "expanded"} else "expanded"
        return self.bound_project_id, chosen

    def _visible_assets(self, scope: str = "expanded") -> set[str] | None:
        if not self.bound_project_id:
            return None
        return projects.asset_filter(self.conn, self.bound_project_id, scope)[0]

    def _ensure_visible(self, asset_id: str) -> None:
        allowed = self._visible_assets()
        if allowed is not None and asset_id not in allowed:
            raise ToolError("该资产不在当前接口的分享范围内")

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        message_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message_id, -32600, "Invalid Request")
        if "id" not in message:  # JSON-RPC 通知不需要响应
            return None
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(message_id, -32602, "Invalid params")

        if method == "initialize":
            return _result(message_id, self._initialize(params))
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": self._tools()})
        if method == "tools/call":
            return _result(message_id, self._call_tool(params))
        if method == "resources/list":
            return _result(message_id, {"resources": self._resources()})
        if method == "resources/templates/list":
            return _result(message_id, {"resourceTemplates": []})
        if method == "resources/read":
            try:
                return _result(message_id, self._read_resource(params))
            except _ResourceNotFound as exc:
                return _error(message_id, -32002, str(exc))
            except ToolError as exc:
                return _tool_error(str(exc))
        return _error(message_id, -32601, f"Method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {
            "protocolVersion": protocol,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": (
                "Read-only access to the local Shiyuan knowledge base. "
                "For ordinary content questions, use search_knowledge first, then "
                "get_document_text with the returned location_id for full context. "
                "Projects are logical knowledge spaces: call list_projects, then pass "
                "project_id and scope (project / expanded / global) to search_knowledge. "
                "For version questions (which version is current, latest, historical, "
                "or what changed), call list_document_families first, then "
                "get_document_family for the version list and compare_versions for a diff. "
                "Search items carry version_label, is_latest_version and "
                "family_latest_version_label: never present an older version as current. "
                "is_latest_version is null when the file has no version label (plain "
                "duplicates), so do not describe such files as the latest version; "
                "for families use newest_asset_id / newest_mtime when labels are absent. "
                "Family items also carry role, latest_version_type, latest_basis and "
                "latest_confidence: when confidence is not high, say which basis "
                "(version label or modified time) the answer uses. "
                "Families are rebuilt after scans: list_document_families returns built_at, "
                "so treat version answers as a snapshot. "
                "Items with weak=true are below the evidence threshold: mention them only "
                "as weak leads, never as the answer. Items whose match is 'name' only "
                "matched the file name or path; for factual questions prefer items with a "
                "snippet and read them with get_document_text. "
                "For corpus-level questions (what files exist, how many files, coverage), "
                "call knowledge_stats and list_sources instead: search_knowledge returns "
                "only a limited top-k sample, and its item count is not the corpus total. "
                "Paths point to original files on this machine."
            ),
        }

    # ---------------------------------------------------------------- tools
    def _tools(self) -> list[dict]:
        readonly = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
        return [
            {
                "name": "search_knowledge",
                "title": "Search knowledge base",
                "description": (
                    "Search indexed file names, paths, parsed text and OCR content. "
                    "Returns ranked documents with snippets, page/sheet locators and an "
                    "evidence range (page number or character offsets). "
                    "This is the recommended first step for content questions. "
                    "Results are a limited top-k sample, not the complete corpus; "
                    "the response includes coverage, truncated and stale flags, and "
                    "knowledge_stats reports file counts and coverage."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords or a natural-language question.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_LIMIT,
                            "default": DEFAULT_LIMIT,
                        },
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 1000,
                            "default": 0,
                        },
                        "ext": {
                            "type": "string",
                            "description": "Optional extension filter, e.g. pdf, docx, md.",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "Optional authorized source id from list_sources.",
                        },
                    },
                    "required": ["query"],
                },
                "annotations": readonly,
            },
            {
                "name": "get_document_text",
                "title": "Read document text",
                "description": (
                    "Read indexed text for one document by location_id or absolute path. "
                    "Use search_knowledge first. Optionally filter by page or sheet."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "location_id": {
                            "type": "string",
                            "description": "location_id returned by search_knowledge.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Absolute original file path, as an alternative.",
                        },
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Only return chunks from this PDF/PPTX page.",
                        },
                        "sheet": {
                            "type": "string",
                            "description": "Only return chunks from this worksheet.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": MAX_MAX_CHARS,
                            "default": DEFAULT_MAX_CHARS,
                        },
                    },
                    "anyOf": [{"required": ["location_id"]}, {"required": ["path"]}],
                },
                "annotations": readonly,
            },
            {
                "name": "list_sources",
                "title": "List authorized sources",
                "description": (
                    "List authorized local folders with scan health and file counts. "
                    "Use source_id values to narrow search_knowledge."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": readonly,
            },
            {
                "name": "knowledge_stats",
                "title": "Knowledge base stats",
                "description": (
                    "Return corpus coverage, file-type distribution, OCR status and "
                    "maintenance counters for the local knowledge base."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": readonly,
            },
            {
                "name": "list_projects",
                "title": "List knowledge projects",
                "description": (
                    "List project knowledge spaces available to external agents. "
                    "Projects marked local-only are omitted."
                ),
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": readonly,
            },
            {
                "name": "get_project",
                "title": "Get project details",
                "description": "Return one project with members, sources and index stats.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string", "description": "Project id."},
                    },
                    "required": ["project_id"],
                },
                "annotations": readonly,
            },
            {
                "name": "search_assets",
                "title": "Search project assets",
                "description": (
                    "Search file names and paths with project scope. Returns asset "
                    "locations and parse state; use search_knowledge for text evidence."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "project_id": {"type": "string"},
                        "scope": {"type": "string",
                                  "enum": ["project", "expanded", "global"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
                                  "default": DEFAULT_LIMIT},
                    },
                    "required": ["query"],
                },
                "annotations": readonly,
            },
            {
                "name": "get_evidence",
                "title": "Get original evidence",
                "description": (
                    "Return the original indexed chunk(s) for a chunk_id or a "
                    "location_id, with page, sheet, path and parse stage."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "chunk_id": {"type": "string"},
                        "location_id": {"type": "string"},
                        "page": {"type": "integer", "minimum": 1},
                        "sheet": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 100,
                                      "maximum": MAX_MAX_CHARS, "default": 4000},
                    },
                    "anyOf": [{"required": ["chunk_id"]}, {"required": ["location_id"]}],
                },
                "annotations": readonly,
            },
            {
                "name": "get_document_family",
                "title": "Get document family",
                "description": (
                    "List all versions that belong to the same document family as "
                    "the given asset. Versions are not ranked or recommended."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"asset_id": {"type": "string"}},
                    "required": ["asset_id"],
                },
                "annotations": readonly,
            },
            {
                "name": "compare_versions",
                "title": "Compare two versions",
                "description": (
                    "Compare two assets in the same document family: shared, left-only, "
                    "right-only and modified content groups. Returns a capped sample per "
                    "category (summary always has the full counts); raise limit or "
                    "max_chars only when the sample is not enough."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "family_id": {"type": "string"},
                        "left_asset_id": {"type": "string"},
                        "right_asset_id": {"type": "string"},
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 100,
                            "default": 10,
                            "description": "Max content groups per category.",
                        },
                        "max_chars": {
                            "type": "integer", "minimum": 100, "maximum": 2000,
                            "default": 400,
                            "description": "Max characters of each paragraph text.",
                        },
                    },
                    "required": ["family_id", "left_asset_id", "right_asset_id"],
                },
                "annotations": readonly,
            },
            {
                "name": "get_related_documents",
                "title": "Get related documents",
                "description": (
                    "Find documents related to one asset: other versions in the same "
                    "document family, and other assets in the same project."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string"},
                        "location_id": {"type": "string"},
                        "path": {"type": "string"},
                        "project_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1,
                                  "maximum": 100, "default": 20},
                    },
                },
                "annotations": readonly,
            },
            {
                "name": "list_document_families",
                "title": "List document families",
                "description": (
                    "List multi-version document families in the shared project, with "
                    "the latest version first. Use this to answer 'which version is "
                    "current' before reading a specific document. Items carry role "
                    "(manuscript / graphical_abstract / cover_letter / highlights / "
                    "supplementary / response), has_versions and latest_version_label. "
                    "latest_basis says whether the choice came from the version label or "
                    "the modified time; latest_confidence is high / medium / low. "
                    "When has_versions is false the family is a duplicate set, so use "
                    "newest_* and never call it the latest version. "
                    "built_at is the family snapshot time."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1,
                                  "maximum": 50, "default": 10},
                    },
                },
                "annotations": readonly,
            },
            {
                "name": "get_source_status",
                "title": "Get source status",
                "description": (
                    "Return authorized folders with scan health, availability and "
                    "file counts, plus index freshness (changed files, pending or "
                    "failed jobs). Optionally filter by source_id."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"source_id": {"type": "string"}},
                },
                "annotations": readonly,
            },
        ]

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _tool_error("无效的工具调用参数")
        started = time.monotonic()
        try:
            self._ensure_session()
            if name == "search_knowledge":
                payload = self._search(arguments)
            elif name == "get_document_text":
                payload = self._document_text(arguments)
            elif name == "list_sources":
                payload = self._list_sources()
            elif name == "knowledge_stats":
                payload = self._stats()
            elif name == "list_projects":
                payload = self._list_projects()
            elif name == "get_project":
                payload = self._get_project(arguments)
            elif name == "search_assets":
                payload = self._search(arguments)
            elif name == "get_evidence":
                payload = self._get_evidence(arguments)
            elif name == "get_document_family":
                payload = self._document_family(arguments)
            elif name == "compare_versions":
                payload = self._compare_versions(arguments)
            elif name == "get_related_documents":
                payload = self._related_documents(arguments)
            elif name == "list_document_families":
                payload = self._list_document_families(arguments)
            elif name == "get_source_status":
                payload = self._source_status(arguments)
            else:
                return _tool_error(f"未知工具：{name}")
        except ToolError as exc:
            return _tool_error(str(exc))
        except Exception as exc:  # 工具执行失败不应终止整个 MCP 进程
            _debug(f"tool {name} failed: {exc}\n{traceback.format_exc()}")
            return _tool_error(f"工具执行失败：{exc}")
        finally:
            _debug(f"tool {name} took {time.monotonic() - started:.3f}s")
        return _tool_text(payload)

    # ---------------------------------------------------------------- tool impl
    def _search(self, args: dict) -> dict:
        query = _string(args, "query") or ""
        limit = _integer(args, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = _integer(args, "offset", 0, 0, 1000)
        ext = _string(args, "ext", required=False)
        source_id = _string(args, "source_id", required=False)
        project_id = _string(args, "project_id", required=False)
        scope = _string(args, "scope", required=False)
        project_id, scope = self._bound_scope(project_id, scope)
        allowed = excluded = priority = None
        used_scope = "global"
        if project_id:
            self._project(project_id, require_external=True)
            used_scope = projects.scope_for(self.conn, project_id, scope)
            allowed, excluded = projects.asset_filter(
                self.conn, project_id, used_scope)
            priority = projects.asset_filter(
                self.conn, project_id, "project")[0]
        result = search.search(
            self.conn, query, limit=limit, offset=offset,
            source_id=source_id, ext=ext,
            allowed_assets=allowed, excluded_assets=excluded,
            priority_assets=priority,
        )
        memberships: dict[str, str] = {}
        if project_id:
            memberships = {
                row["asset_id"]: row["membership_type"]
                for row in self.conn.execute(
                    "SELECT asset_id, membership_type FROM project_assets"
                    " WHERE project_id = ?", (project_id,))
            }
        items = [
            {
                "location_id": item["location_id"],
                "asset_id": item["asset_id"],
                "name": item["name"],
                "path": item["path"],
                "directory": item["directory"],
                "rel_path": item["rel_path"],
                "ext": item["ext"],
                "size": item["size"],
                "mtime": item["mtime"],
                "page": item["page"],
                "sheet": item["sheet"],
                "range": item.get("range"),
                "stale": item.get("stale", False),
                "index_level": item["index_level"],
                "match": item["match"],
                "semantic": item["semantic"],
                "snippet": item["snippet"],
                "source_id": item["source_id"],
                "source_label": item["source_label"],
                "family_id": item.get("family_id"),
                "family_size": item.get("family_size"),
                "family_title": item.get("family_title"),
                "version_label": item.get("version_label"),
                "family_versions": item.get("family_versions"),
                "is_latest_version": item.get("is_latest_version"),
                "family_latest_asset_id": item.get("family_latest_asset_id"),
                "family_latest_version_label": item.get("family_latest_version_label"),
                "family_latest_mtime": item.get("family_latest_mtime"),
                "weak": item.get("weak"),
                "project_membership": memberships.get(item["asset_id"]),
            }
            for item in result["items"]
        ]
        return {
            "query": result["query"],
            "total": result["total"],
            "returned": len(items),
            "offset": offset,
            # 检索是排序采样而非穷尽扫描；截断时调用方应缩小范围或翻页。
            "coverage": "ranked_sample" if not result.get("truncated") else "truncated",
            "truncated": result.get("truncated", False),
            "stale": result.get("stale", False),
            "rewritten": result.get("rewritten"),
            "vector_used": result.get("vector_used", False),
            "vector_error": result.get("vector_error"),
            "scope": {"project_id": project_id, "used": used_scope},
            "items": items,
        }

    def _resolve_location(self, args: dict) -> sqlite3.Row:
        location_id = _string(args, "location_id", required=False)
        path = _string(args, "path", required=False)
        base = (
            "SELECT l.*, s.label AS source_label, s.state AS source_state,"
            " s.root_uri AS source_root FROM asset_locations l"
            " JOIN sources s ON s.source_id = l.source_id"
        )
        row = None
        if location_id:
            row = self.conn.execute(
                f"{base} WHERE l.location_id = ?", (location_id,)
            ).fetchone()
        elif path:
            row = self.conn.execute(f"{base} WHERE l.path = ?", (path,)).fetchone()
            if row is None:
                row = self.conn.execute(
                    f"{base} WHERE lower(l.path) = lower(?) LIMIT 1", (path,)
                ).fetchone()
        else:
            raise ToolError("请提供 location_id 或 path")
        if row is None:
            raise ToolError("未找到该文件；请先用 search_knowledge 获取 location_id")
        if row["availability"] != "AVAILABLE":
            raise ToolError(f"文件当前不可检索：{row['availability']}")
        if row["source_state"] != "active":
            raise ToolError("所属数据源已暂停，恢复后才能读取")
        self._ensure_visible(row["asset_id"])
        return row

    def _document_text(self, args: dict) -> dict:
        location = self._resolve_location(args)
        page = args.get("page")
        if page is not None:
            try:
                page = int(page)
            except (TypeError, ValueError) as exc:
                raise ToolError("参数 page 必须是整数") from exc
            if page < 1:
                raise ToolError("参数 page 必须大于 0")
        sheet = _string(args, "sheet", required=False)
        max_chars = _integer(args, "max_chars", DEFAULT_MAX_CHARS, 100, MAX_MAX_CHARS)

        version = self.conn.execute(
            "SELECT * FROM content_versions WHERE asset_id = ? AND is_current = 1"
            " ORDER BY observed_at DESC LIMIT 1",
            (location["asset_id"],),
        ).fetchone()
        if version is None:
            raise ToolError("该文件还没有内容版本，请先在桌面端扫描")

        conditions = ["c.version_id = ?"]
        params: list[Any] = [version["version_id"]]
        if page is not None:
            conditions.append("c.page = ?")
            params.append(page)
        if sheet:
            conditions.append("c.sheet = ?")
            params.append(sheet)
        rows = self.conn.execute(
            "SELECT c.page, c.sheet, c.ordinal, c.text, c.char_start, c.char_end,"
            " a.parser_signature"
            " FROM chunks c JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
            f" WHERE {' AND '.join(conditions)}",
            params,
        ).fetchall()

        chunks = [
            {
                "page": row["page"],
                "sheet": row["sheet"],
                "ordinal": row["ordinal"],
                "text": row["text"],
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "stage": _stage_of(row["parser_signature"]),
            }
            for row in rows
        ]
        chunks.sort(
            key=lambda item: (
                item["page"] is None,
                item["page"] or 0,
                item["sheet"] or "",
                item["ordinal"],
            )
        )
        selected = self._select_chunks(location["ext"], chunks)

        sections: list[dict] = []
        for chunk in selected:
            key = (chunk["page"], chunk["sheet"])
            text = chunk["text"]
            if sections and sections[-1]["_key"] == key:
                previous = sections[-1]["chunks"][-1]
                # 相邻块的重叠区（CHUNK_OVERLAP）只保留一份，读取结果才与原文件一致。
                if (previous["char_end"] > chunk["char_start"] > 0
                        and chunk["char_start"] >= previous["char_start"]):
                    overlap = previous["char_end"] - chunk["char_start"]
                    if 0 < overlap < len(text):
                        text = text[overlap:]
                sections[-1]["chunks"].append(chunk)
                sections[-1]["parts"].append(text)
                sections[-1]["stages"].add(chunk["stage"])
            else:
                sections.append(
                    {"_key": key, "chunks": [chunk], "parts": [text],
                     "stages": {chunk["stage"]}}
                )

        artifacts = [
            {
                "stage": _stage_of(row["parser_signature"]),
                "state": row["state"],
                "quality": row["quality"],
                "message": row["message"],
            }
            for row in self.conn.execute(
                "SELECT parser_signature, state, quality, message FROM parse_artifacts"
                " WHERE version_id = ? ORDER BY created_at",
                (version["version_id"],),
            )
        ]

        remaining = max_chars
        total_chars = 0
        output_sections: list[dict] = []
        truncated = False
        for section in sections:
            page_number, sheet_name = section["_key"]
            text = "\n".join(section["parts"])
            header_parts = []
            if page_number:
                header_parts.append(f"第 {page_number} 页")
            if sheet_name:
                header_parts.append(f"工作表 {sheet_name}")
            header = f"[{' · '.join(header_parts)}]\n" if header_parts else ""
            body = header + text
            total_chars += len(text)
            if remaining <= 0:
                truncated = True
                break
            if len(body) > remaining:
                body = body[:remaining]
                truncated = True
            remaining -= len(body)
            output_sections.append(
                {
                    "page": page_number,
                    "sheet": sheet_name,
                    "stage": "+".join(sorted(section["stages"])),
                    "text": body,
                }
            )
        return {
            "location": {
                "location_id": location["location_id"],
                "asset_id": location["asset_id"],
                "name": Path(location["path"]).name,
                "path": location["path"],
                "rel_path": location["rel_path"],
                "ext": location["ext"],
                "size": location["size"],
                "mtime": (
                    datetime.fromtimestamp(location["mtime_ns"] / 1e9)
                    .astimezone()
                    .isoformat(timespec="seconds")
                    if location["mtime_ns"] else None
                ),
                "source_id": location["source_id"],
                "source_label": location["source_label"],
                "source_root": location["source_root"],
            },
            "version_id": version["version_id"],
            "observed_at": version["observed_at"],
            "sections": output_sections,
            "text": "\n".join(section["text"] for section in output_sections),
            "total_chars": total_chars,
            "truncated": truncated,
            "artifacts": artifacts,
        }

    @staticmethod
    def _select_chunks(ext: str, chunks: list[dict]) -> list[dict]:
        """PDF 优先使用 OCR 补页，PPTX 合并正文与图片 OCR，其余优先 L1。"""
        if not chunks:
            return []
        if ext == "pptx":
            return sorted(
                chunks,
                key=lambda item: (
                    item["page"] is None,
                    item["page"] or 0,
                    item["stage"] == "L2",
                    item["ordinal"],
                ),
            )
        if ext == "pdf":
            groups: dict[tuple, dict[str, list[dict]]] = {}
            for chunk in chunks:
                key = (chunk["page"], chunk["sheet"])
                groups.setdefault(key, {"L1": [], "L2": []})[chunk["stage"]].append(chunk)
            selected: list[dict] = []
            for key in sorted(groups, key=lambda item: (item[0] is None, item[0] or 0)):
                bucket = groups[key]
                selected.extend(bucket["L2"] or bucket["L1"])
            return selected
        l1 = [chunk for chunk in chunks if chunk["stage"] == "L1"]
        return l1 or [chunk for chunk in chunks if chunk["stage"] == "L2"]

    def _list_sources(self) -> dict:
        allowed_sources = None
        if self.bound_project_id and not self.bound_global:
            allowed_sources = set(projects.source_ids(self.conn, self.bound_project_id))
        items = []
        for row in store.list_sources(self.conn):
            if allowed_sources is not None and row["source_id"] not in allowed_sources:
                continue
            stats = store.source_stats(self.conn, row["source_id"])
            items.append(
                {
                    "source_id": row["source_id"],
                    "label": row["label"],
                    "root_uri": row["root_uri"],
                    "state": row["state"],
                    "health": row["health"],
                    "last_scan_at": row["last_scan_at"],
                    "available": stats["available"],
                    "stale": stats["stale"],
                    "missing": stats["missing"],
                    "offline": stats["offline"],
                    "last_run": stats["last_run"],
                }
            )
        return {"total": len(items), "items": items}

    def _stats(self) -> dict:
        return projects.stats_view(self.conn, self.bound_project_id)

    # ---------------------------------------------------------------- projects
    def _project(self, project_id: str, *, require_external: bool = True):
        row = self.conn.execute(
            "SELECT * FROM projects WHERE project_id = ? AND status = 'active'",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ToolError("项目不存在或已归档")
        if require_external and row["privacy_policy"] != "external_allowed":
            raise ToolError("该项目仅允许本地使用，不能通过 MCP 读取")
        return row

    def _list_projects(self) -> dict:
        if self.bound_project_id:
            self._project(self.bound_project_id, require_external=True)
            data = projects.get_project(self.conn, self.bound_project_id)
            if data is None:
                return {"total": 0, "items": []}
            data.pop("privacy_policy", None)
            return {"total": 1, "items": [data]}
        items = projects.list_projects(self.conn, external_only=True)
        return {"total": len(items), "items": items}

    def _get_project(self, args: dict) -> dict:
        requested = _string(args, "project_id", required=False)
        if not requested and not self.bound_project_id:
            raise ToolError("请提供 project_id")
        project_id, _ = self._bound_scope(requested, None)
        self._project(project_id or "")
        data = projects.get_project(self.conn, project_id or "")
        if data is None:
            raise ToolError("项目不存在")
        data.pop("privacy_policy", None)
        return data

    def _get_evidence(self, args: dict) -> dict:
        chunk_id = _string(args, "chunk_id", required=False)
        location_id = _string(args, "location_id", required=False)
        max_chars = _integer(args, "max_chars", 4000, 100, MAX_MAX_CHARS)
        conditions: list[str] = []
        params: list = []
        if chunk_id:
            conditions.append("c.chunk_id = ?")
            params.append(chunk_id)
        elif location_id:
            location = self.conn.execute(
                "SELECT asset_id FROM asset_locations WHERE location_id = ?",
                (location_id,),
            ).fetchone()
            if location is None:
                raise ToolError("未找到该文件")
            version = self.conn.execute(
                "SELECT version_id FROM content_versions WHERE asset_id = ?"
                " AND is_current = 1 ORDER BY observed_at DESC LIMIT 1",
                (location["asset_id"],),
            ).fetchone()
            if version is None:
                raise ToolError("该文件还没有内容版本，请先在桌面端扫描")
            conditions.append("c.version_id = ?")
            params.append(version["version_id"])
            page = args.get("page")
            if page is not None:
                try:
                    page = int(page)
                except (TypeError, ValueError) as exc:
                    raise ToolError("参数 page 必须是整数") from exc
                conditions.append("c.page = ?")
                params.append(page)
            sheet = _string(args, "sheet", required=False)
            if sheet:
                conditions.append("c.sheet = ?")
                params.append(sheet)
        else:
            raise ToolError("请提供 chunk_id 或 location_id")

        sql = (
            "SELECT c.chunk_id, c.text, c.page, c.sheet, c.asset_id, c.version_id,"
            " l.location_id, l.path, a.parser_signature"
            " FROM chunks c"
            " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
            " JOIN asset_locations l ON l.asset_id = c.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " WHERE " + " AND ".join(conditions)
            + " AND l.availability = 'AVAILABLE' AND s.state = 'active'"
            " ORDER BY (c.page IS NULL), c.page, c.ordinal LIMIT 8"
        )
        rows = self.conn.execute(sql, params).fetchall()

        project_id = _string(args, "project_id", required=False)
        scope = _string(args, "scope", required=False)
        project_id, scope = self._bound_scope(project_id, scope)
        if project_id:
            self._project(project_id, require_external=True)
            allowed, excluded = projects.asset_filter(
                self.conn, project_id,
                projects.scope_for(self.conn, project_id, scope))
            rows = [
                row for row in rows
                if (allowed is None or row["asset_id"] in allowed)
                and row["asset_id"] not in excluded
            ]
        items = []
        used = 0
        for row in rows:
            text = row["text"] or ""
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[:remaining]
            used += len(text)
            items.append({
                "chunk_id": row["chunk_id"],
                "asset_id": row["asset_id"],
                "location_id": row["location_id"],
                "path": row["path"],
                "page": row["page"],
                "sheet": row["sheet"],
                "stage": _stage_of(row["parser_signature"]),
                "text": text,
            })
        return {"total": len(items), "truncated": used >= max_chars, "items": items}

    def _document_family(self, args: dict) -> dict:
        asset_id = _string(args, "asset_id")
        self._ensure_visible(asset_id)
        info = families.family_map(self.conn).get(asset_id)
        if info is None:
            return {"asset_id": asset_id, "family_id": None, "members": []}
        visible = self._visible_assets()
        members = [
            member for member in families.family_members(self.conn, info["family_id"])
            if visible is None or member["asset_id"] in visible
        ]
        latest_ns = info.get("family_latest_mtime_ns") or 0
        newest_id = info.get("family_newest_asset_id")
        newest = next(
            (member for member in members if member["asset_id"] == newest_id), None)
        newest_ns = info.get("family_newest_mtime_ns") or 0
        has_versions = any(
            (member["version_label"] or "").strip() for member in members)
        return {
            "asset_id": asset_id,
            "family_id": info["family_id"],
            "title": info.get("family_title"),
            "role": families._doc_role(members[0]["name"]) if members else None,
            "size": len(members),
            "has_versions": has_versions,
            "latest_asset_id": info.get("family_latest_asset_id"),
            "latest_version_label": info.get("family_latest_version_label"),
            "latest_version_type": info.get("family_latest_version_type"),
            "latest_basis": info.get("family_latest_basis"),
            "latest_confidence": info.get("family_latest_confidence"),
            "latest_mtime": (
                datetime.fromtimestamp(latest_ns / 1e9).astimezone()
                .isoformat(timespec="seconds") if latest_ns else None
            ),
            # 没有版本标签时，用“最后修改”作为唯一可核实的参照。
            "newest_asset_id": newest_id,
            "newest_path": newest["path"] if newest else None,
            "newest_version_label": newest["version_label"] if newest else None,
            "newest_mtime": (
                datetime.fromtimestamp(newest_ns / 1e9).astimezone()
                .isoformat(timespec="seconds") if newest_ns else None
            ),
            "members": members,
        }

    def _compare_versions(self, args: dict) -> dict:
        family_id = _string(args, "family_id")
        left = _string(args, "left_asset_id")
        right = _string(args, "right_asset_id")
        limit = _integer(args, "limit", 10, 1, 100)
        max_chars = _integer(args, "max_chars", 400, 100, 2000)
        self._ensure_visible(left)
        self._ensure_visible(right)
        try:
            result = families.diff_versions(self.conn, family_id, left, right, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 参数错误原样返回
            raise ToolError(f"无法比较版本：{exc}") from exc

        def clip(item: dict) -> dict:
            text = item.get("text") or ""
            if len(text) > max_chars:
                return {**item, "text": text[:max_chars], "text_truncated": True}
            return item

        result["shared"] = [clip(item) for item in result["shared"]]
        result["removed"] = [clip(item) for item in result["removed"]]
        result["added"] = [clip(item) for item in result["added"]]
        result["modified"] = [
            {**item, "left": clip(item["left"]), "right": clip(item["right"])}
            for item in result["modified"]
        ]
        result["returned"] = {
            key: len(result[key]) for key in ("shared", "removed", "added", "modified")
        }
        result["limit"] = limit
        result["max_chars"] = max_chars
        return result

    def _related_documents(self, args: dict) -> dict:
        asset_id = _string(args, "asset_id", required=False)
        location_id = _string(args, "location_id", required=False)
        path = _string(args, "path", required=False)
        project_id = _string(args, "project_id", required=False)
        limit = _integer(args, "limit", 20, 1, 100)
        if not asset_id and not location_id and not path:
            raise ToolError("需要 asset_id、location_id 或 path")
        project_id, _ = self._bound_scope(project_id, "expanded")
        allowed: set[str] | None = None
        excluded: set[str] = set()
        if project_id:
            self._project(project_id, require_external=True)
            allowed, excluded = projects.asset_filter(
                self.conn, project_id, "expanded")
        if not asset_id:
            row = self._resolve_location(
                {"location_id": location_id, "path": path})
            asset_id = row["asset_id"]
        if self.conn.execute(
                "SELECT 1 FROM assets WHERE asset_id = ?", (asset_id,)).fetchone() is None:
            raise ToolError("资产不存在")
        if allowed is not None and (asset_id not in allowed or asset_id in excluded):
            raise ToolError("该资产不在当前项目范围内")

        def visible(candidate: str) -> bool:
            return ((allowed is None or candidate in allowed)
                    and candidate not in excluded)

        items: list[dict] = []
        seen = {asset_id}
        info = families.family_map(self.conn).get(asset_id)
        if info:
            for member in families.family_members(self.conn, info["family_id"]):
                if member["asset_id"] in seen or not visible(member["asset_id"]):
                    continue
                seen.add(member["asset_id"])
                items.append({**member, "reason": "same_family",
                              "family_id": info["family_id"],
                              "family_title": info["family_title"]})
        if project_id:
            rows = self.conn.execute(
                "SELECT pa.asset_id, pa.membership_type, pa.priority,"
                " l.location_id, l.path, l.rel_path"
                " FROM project_assets pa"
                " LEFT JOIN asset_locations l ON l.asset_id = pa.asset_id"
                "  AND l.location_id = (SELECT l2.location_id FROM asset_locations l2"
                "   WHERE l2.asset_id = pa.asset_id AND l2.availability = 'AVAILABLE'"
                "   ORDER BY l2.path LIMIT 1)"
                " WHERE pa.project_id = ? AND pa.asset_id != ? AND pa.status = 'active'"
                " AND pa.membership_type IN ('primary', 'linked')"
                " ORDER BY pa.priority DESC, l.path LIMIT ?",
                (project_id, asset_id, limit),
            ).fetchall()
            for row in rows:
                if row["asset_id"] in seen or not visible(row["asset_id"]):
                    continue
                seen.add(row["asset_id"])
                items.append({
                    "asset_id": row["asset_id"],
                    "location_id": row["location_id"],
                    "path": row["path"],
                    "name": Path(row["path"]).name if row["path"] else None,
                    "rel_path": row["rel_path"],
                    "version_label": "",
                    "reason": "same_project",
                    "membership_type": row["membership_type"],
                    "priority": row["priority"],
                })
        return {
            "asset_id": asset_id,
            "family": info,
            "related": items[:limit],
            "total": len(items),
        }

    def _list_document_families(self, args: dict) -> dict:
        """列出可访问的多版本文档家族，最新版本排在最前。"""
        query = (_string(args, "query", required=False) or "").strip().lower()
        limit = _integer(args, "limit", 10, 1, 50)
        visible = self._visible_assets("expanded")
        info_map = families.family_map(self.conn)
        by_family: dict[str, list[str]] = {}
        for asset_id, info in info_map.items():
            if visible is not None and asset_id not in visible:
                continue
            by_family.setdefault(info["family_id"], []).append(asset_id)
        items: list[dict] = []
        matched: list[dict] = []
        for family_id, asset_ids in by_family.items():
            members = [
                member for member in families.family_members(self.conn, family_id)
                if visible is None or member["asset_id"] in visible
            ]
            if len(members) < 2:
                continue
            has_versions = any(
                (member["version_label"] or "").strip() for member in members)
            title = (info_map.get(asset_ids[0]) or {}).get("family_title") or ""
            family_info = info_map.get(asset_ids[0]) or {}
            newest_id = family_info.get("family_newest_asset_id")
            newest = next(
                (member for member in members if member["asset_id"] == newest_id), None)
            newest_ns = family_info.get("family_newest_mtime_ns") or 0
            latest = next(
                (member for member in members if member["is_latest"]), members[0]
            )
            versions: dict[str, dict] = {}
            for member in members:
                label = member["version_label"] or "未标注"
                bucket = versions.setdefault(label, {
                    "label": label, "count": 0, "latest_mtime_ns": 0,
                    "sample_path": member["path"],
                })
                bucket["count"] += 1
                bucket["latest_mtime_ns"] = max(
                    bucket["latest_mtime_ns"], member["mtime_ns"] or 0)
            latest_ns = latest["mtime_ns"] or 0
            item = {
                "family_id": family_id,
                "title": title,
                "size": len(members),
                "has_versions": has_versions,
                "role": families._doc_role(members[0]["name"]) if members else None,
                "latest_asset_id": latest["asset_id"],
                "latest_version_label": latest["version_label"] or None,
                "latest_version_type": family_info.get("family_latest_version_type"),
                "latest_basis": family_info.get("family_latest_basis"),
                "latest_confidence": family_info.get("family_latest_confidence"),
                "latest_mtime": (
                    datetime.fromtimestamp(latest_ns / 1e9).astimezone()
                    .isoformat(timespec="seconds") if latest_ns else None
                ),
                "latest_mtime_ns": latest_ns,
                "latest_path": latest["path"],
                "newest_asset_id": newest_id,
                "newest_version_label": newest["version_label"] if newest else None,
                "newest_path": newest["path"] if newest else None,
                "newest_mtime": (
                    datetime.fromtimestamp(newest_ns / 1e9).astimezone()
                    .isoformat(timespec="seconds") if newest_ns else None
                ),
                "newest_mtime_ns": newest_ns,
                "versions": [
                    {
                        "label": bucket["label"],
                        "count": bucket["count"],
                        "latest_mtime": (
                            datetime.fromtimestamp(
                                bucket["latest_mtime_ns"] / 1e9).astimezone()
                            .isoformat(timespec="seconds")
                            if bucket["latest_mtime_ns"] else None
                        ),
                        "sample_path": bucket["sample_path"],
                    }
                    for bucket in sorted(
                        versions.values(),
                        key=lambda bucket: -bucket["latest_mtime_ns"])
                ],
            }
            items.append(item)
            if query:
                # 只看家族标题与相对路径，避免数据源根目录名把所有文件都算命中。
                haystack = " ".join(
                    [title, *((member.get("rel_path") or "") for member in members)]
                ).lower()
                if query in haystack:
                    matched.append(item)
        # 指定 query 时只返回命中的家族；不做“查不到就退回全部”的兜底，
        # 否则外部 Agent 会以为过滤生效，实际拿到的是全量列表。
        selected = matched if query else items
        # 有版本标签的家族优先；重复副本族排在后面，避免抢占“最新版本”的位置。
        selected.sort(key=lambda item: (
            not item["has_versions"], -item["size"], -item["latest_mtime_ns"]))
        snapshot = self.conn.execute(
            "SELECT value FROM app_settings WHERE key = 'document_families'").fetchone()
        built_at = None
        if snapshot:
            try:
                built_at = json.loads(snapshot["value"]).get("built_at")
            except ValueError:
                built_at = None
        return {"total": len(selected), "items": selected[:limit], "built_at": built_at}

    def _source_status(self, args: dict) -> dict:
        source_id = _string(args, "source_id", required=False)
        items = self._list_sources()["items"]
        if source_id:
            items = [item for item in items if item["source_id"] == source_id]
        freshness = search.index_freshness(self.conn, self.bound_project_id)
        if source_id:
            freshness = {
                **freshness,
                "sources": [item for item in freshness["sources"]
                            if item["source_id"] == source_id],
            }
        return {"total": len(items), "items": items, "freshness": freshness}

    # ---------------------------------------------------------------- resources
    def _resources(self) -> list[dict]:
        return [
            {
                "uri": "shiyuan://sources",
                "name": "Authorized sources",
                "description": "Authorized folders with scan health and file counts.",
                "mimeType": "application/json",
            },
            {
                "uri": "shiyuan://stats",
                "name": "Knowledge base stats",
                "description": "Coverage, file types, OCR status and maintenance counters.",
                "mimeType": "application/json",
            },
        ]

    def _read_resource(self, params: dict) -> dict:
        self._ensure_session()
        uri = params.get("uri")
        if uri == "shiyuan://sources":
            payload: Any = self._list_sources()
        elif uri == "shiyuan://stats":
            payload = self._stats()
        else:
            raise _ResourceNotFound(f"Unknown resource: {uri}")
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }


class _ResourceNotFound(RuntimeError):
    """resources/read 找不到 URI 时转为 JSON-RPC 错误。"""


def serve(
    conn: sqlite3.Connection,
    reader: BinaryIO | None = None,
    writer: BinaryIO | None = None,
    *,
    once: bool = False,
    bound_project_id: str | None = None,
    client_id: str | None = None,
) -> None:
    """按行读写 JSON-RPC；stdout 只允许协议消息。"""
    server = MCPServer(conn, project_id=bound_project_id, client_id=client_id)
    reader = reader or _binary_stream("stdin", 0, "rb")
    writer = writer or _binary_stream("stdout", 1, "wb")
    for raw in reader:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            payload = _error(None, -32700, "Parse error")
        else:
            payload = server.handle(message)
        if payload is not None:
            writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            writer.flush()
        if once:
            break


def server_command() -> list[str]:
    """MCP 启动命令：源码模式走 python -m，安装版走 shiyuan-core.exe --mcp。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp"]
    return [sys.executable, "-m", "shiyuan.mcp_server"]


def config_snippet(token: str | None = None) -> dict:
    env = {"SHIYUAN_HOME": str(config.app_home())}
    if token:
        env["SHIYUAN_EXT_TOKEN"] = token
    if getattr(sys, "frozen", False):
        server: dict[str, Any] = {
            "command": sys.executable, "args": ["--mcp"], "env": env,
        }
    else:
        server = {
            "command": sys.executable,
            "args": ["-m", "shiyuan.mcp_server"],
            "cwd": str(Path(__file__).resolve().parents[1]),
            "env": env,
        }
    return {"mcpServers": {"shiyuan": server}}


def _verify_token(path: Path, token: str) -> sqlite3.Row:
    """校验外部连接令牌；返回绑定项目等连接信息，非法令牌直接终止进程。"""
    conn = db.connect(path)
    try:
        db.init_db(conn)
        row = external.match_token(conn, token)
        if row is None:
            line = "Shiyuan MCP: invalid external token\n"
            if sys.stderr is not None:
                sys.stderr.write(line)
            else:
                os.write(2, line.encode("utf-8"))
            raise SystemExit(2)
        external.record_usage(conn, row["client_id"])
        return row
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Shiyuan MCP server (stdio, read-only)")
    parser.add_argument("--db", help="SQLite path; defaults to the desktop app database")
    parser.add_argument("--token", help="external client token (or SHIYUAN_EXT_TOKEN)")
    parser.add_argument(
        "--print-config", action="store_true",
        help="print an MCP client JSON snippet and exit",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="handle a single JSON-RPC message and exit (used by connection test)",
    )
    args = parser.parse_args(argv)

    token = (args.token or os.environ.get("SHIYUAN_EXT_TOKEN", "")).strip()

    if args.print_config:
        # 控制台编码因系统而异；Unicode 转义保证中文路径在任何客户端复制后仍然有效。
        text = json.dumps(config_snippet(token or None), ensure_ascii=True, indent=2)
        if sys.stdout is not None:
            print(text)
        else:
            os.write(1, (text + "\n").encode("ascii"))
        return

    path = Path(args.db) if args.db else db.db_path()
    bound_project_id = None
    client_id = None
    if token:
        client = _verify_token(path, token)
        bound_project_id = client["project_id"] or projects.GLOBAL_PROJECT_ID
        client_id = client["client_id"]
    conn = db.connect(path)
    try:
        db.init_db(conn)
        conn.execute("PRAGMA query_only = ON")
        # 后台预热向量矩阵与本地模型，避免 Agent 的第一次检索等冷启动。
        embeddings.warm_cache_async()
        serve(conn, once=args.once, bound_project_id=bound_project_id,
              client_id=client_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
