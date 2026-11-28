"""项目知识空间：逻辑组织层，只建立关系，不复制文件、不重复解析。

设计要点：
- 项目不是文件夹：`project_sources` 记录项目引用哪些数据源，`project_assets`
  记录项目引用了哪些资产；同一资产可以被多个项目同时引用。
- 内容索引（解析产物 / chunk / FTS / 向量 / 衍生知识）始终属于全局资产，
  项目关系只影响检索范围与优先级，不触发重复处理。
- 排除只作用于当前项目：`project_assets.membership_type = 'excluded'`，
  不会影响其他项目或全局检索。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import store

PROJECT_STATUSES = {"active", "archived"}
MEMBERSHIPS = {"primary", "linked", "excluded"}
SCOPES = {"project", "expanded", "global"}
RETRIEVAL_POLICIES = {"project_first", "expanded", "global"}
PRIVACY_POLICIES = {"external_allowed", "local_only"}
SOURCE_RELATIONS = {"primary", "linked"}

GLOBAL_PROJECT_ID = "prj_global"
GLOBAL_PROJECT_NAME = "全局项目"
GLOBAL_PROJECT_DESCRIPTION = "数据页录入的文件夹自动归入全局项目；子项目可按需拉取其中资料。"


def is_global(conn: sqlite3.Connection, project_id: str | None) -> bool:
    if not project_id:
        return False
    row = conn.execute(
        "SELECT kind FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    return row is not None and row["kind"] == "global"


def _now() -> str:
    return store.now_iso()


def get_project(conn: sqlite3.Connection, project_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["stats"] = project_stats(conn, project_id)
    data["sources"] = list_sources(conn, project_id)
    return data


def require_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        raise KeyError(project_id)
    return row


def list_projects(conn: sqlite3.Connection, *, include_archived: bool = False,
                  external_only: bool = False) -> list[dict]:
    clauses = []
    params: list = []
    if not include_archived:
        clauses.append("p.status = 'active'")
    if external_only:
        clauses.append("p.privacy_policy = 'external_allowed'")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT p.*,"
        " (SELECT COUNT(*) FROM project_sources ps"
        "   WHERE ps.project_id = p.project_id) AS source_count,"
        " (SELECT COUNT(*) FROM project_assets pa"
        "   WHERE pa.project_id = p.project_id AND pa.membership_type = 'primary')"
        "   AS primary_count,"
        " (SELECT COUNT(*) FROM project_assets pa"
        "   WHERE pa.project_id = p.project_id AND pa.membership_type = 'linked')"
        "   AS linked_count,"
        " (SELECT COUNT(*) FROM project_assets pa"
        "   WHERE pa.project_id = p.project_id AND pa.membership_type = 'excluded')"
        "   AS excluded_count"
        f" FROM projects p{where}"
        " ORDER BY CASE p.kind WHEN 'global' THEN 0 ELSE 1 END,"
        " p.updated_at DESC, p.rowid DESC",
        params,
    ).fetchall()
    items = [dict(row) for row in rows]
    global_total = None
    for item in items:
        if item["kind"] != "global":
            continue
        if global_total is None:
            global_total = _global_available(conn)
        item["primary_count"] = global_total
        item["linked_count"] = 0
        item["excluded_count"] = 0
        item["source_count"] = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE state = 'active'").fetchone()[0]
    return items


def create_project(conn: sqlite3.Connection, name: str, *,
                   description: str = "",
                   retrieval_policy: str = "project_first",
                   privacy_policy: str = "external_allowed") -> dict:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("项目名称不能为空")
    if len(clean) > 80:
        raise ValueError("项目名称不能超过 80 字")
    if retrieval_policy not in RETRIEVAL_POLICIES:
        raise ValueError("未知的检索策略")
    if privacy_policy not in PRIVACY_POLICIES:
        raise ValueError("未知的隐私策略")
    project_id = store.new_id("prj")
    now = _now()
    conn.execute(
        "INSERT INTO projects (project_id, name, description, status,"
        " retrieval_policy, privacy_policy, created_at, updated_at)"
        " VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
        (project_id, clean, (description or "").strip(), retrieval_policy,
         privacy_policy, now, now),
    )
    conn.commit()
    return get_project(conn, project_id)  # type: ignore[return-value]


def update_project(conn: sqlite3.Connection, project_id: str, *,
                   name: str | None = None, description: str | None = None,
                   status: str | None = None,
                   retrieval_policy: str | None = None,
                   privacy_policy: str | None = None) -> dict:
    require_project(conn, project_id)
    global_project = is_global(conn, project_id)
    updates: list[str] = []
    params: list = []
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValueError("项目名称不能为空")
        updates.append("name = ?")
        params.append(clean[:80])
    if description is not None:
        updates.append("description = ?")
        params.append(description.strip())
    if status is not None:
        if status not in PROJECT_STATUSES:
            raise ValueError("未知的项目状态")
        if global_project and status != "active":
            raise ValueError("全局项目不能归档")
        updates.append("status = ?")
        params.append(status)
    if retrieval_policy is not None:
        if retrieval_policy not in RETRIEVAL_POLICIES:
            raise ValueError("未知的检索策略")
        if global_project and retrieval_policy != "global":
            raise ValueError("全局项目固定检索全部资料")
        updates.append("retrieval_policy = ?")
        params.append(retrieval_policy)
    if privacy_policy is not None:
        if privacy_policy not in PRIVACY_POLICIES:
            raise ValueError("未知的隐私策略")
        updates.append("privacy_policy = ?")
        params.append(privacy_policy)
    if updates:
        updates.append("updated_at = ?")
        params.append(_now())
        params.append(project_id)
        conn.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE project_id = ?", params)
        conn.commit()
    return get_project(conn, project_id)  # type: ignore[return-value]


def delete_project(conn: sqlite3.Connection, project_id: str) -> dict:
    """只删除项目关系；数据源与资产保持原样。全局项目不可删除。"""
    require_project(conn, project_id)
    if is_global(conn, project_id):
        raise ValueError("全局项目不能删除")
    assets = conn.execute(
        "DELETE FROM project_assets WHERE project_id = ?", (project_id,)).rowcount
    sources = conn.execute(
        "DELETE FROM project_sources WHERE project_id = ?", (project_id,)).rowcount
    # 项目删除后，绑定它的外部令牌立即失效，避免留下无主接口。
    clients = conn.execute(
        "DELETE FROM external_clients WHERE project_id = ?", (project_id,)).rowcount
    conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    conn.commit()
    return {"project_id": project_id, "deleted": True,
            "assets_unlinked": assets, "sources_unlinked": sources,
            "clients_revoked": clients}


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = ? WHERE project_id = ?",
                 (_now(), project_id))


# ---------------------------------------------------------------- 文件夹关联

def _materialize_source(conn: sqlite3.Connection, project_id: str,
                        source_id: str, relation_type: str,
                        path_prefix: str = "") -> int:
    """把数据源里已经存在的资产补进项目；已存在的行（含排除）保持不动。"""
    now = _now()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO project_assets"
        " (project_id, asset_id, membership_type, priority, status, created_at)"
        " SELECT ?, l.asset_id, ?, 0, 'active', ?"
        " FROM asset_locations l WHERE l.source_id = ?"
        " AND (? = '' OR l.rel_path LIKE ? || '%')",
        (project_id, relation_type, now, source_id, path_prefix, path_prefix),
    )
    return cursor.rowcount


def _prefix_of(root: Path, source_root: str | None) -> str:
    """把「项目文件夹」换算成数据源内的 rel_path 前缀（统一正斜杠）。"""
    if not source_root:
        return ""
    try:
        relative = Path(root).relative_to(Path(source_root))
    except ValueError:
        return ""
    value = relative.as_posix().strip("/")
    return f"{value}/" if value and value != "." else ""


def add_source_path(conn: sqlite3.Connection, project_id: str, root: str | Path,
                    relation_type: str = "primary") -> dict:
    """把一个本地文件夹关联到项目；没有授权过就顺带授权，不复制文件。

    如果文件夹位于已授权数据源内部，只关联该子目录（path_prefix），
    不会把整个数据源都算进项目。
    """
    require_project(conn, project_id)
    if relation_type not in SOURCE_RELATIONS:
        raise ValueError("未知的资料来源类型")
    if is_global(conn, project_id):
        # 全局项目包含所有已授权数据源：这里只保证目录已授权，不写关系表。
        plan = store.plan_source_add(conn, root)
        if plan["kind"] in {"existing", "covered"}:
            source = plan["source"]
            created = False
        else:
            result = store.add_source_merged(conn, root)
            source = result["source"]
            created = bool(result.get("created"))
        available = conn.execute(
            "SELECT COUNT(*) FROM asset_locations WHERE source_id = ?"
            " AND availability = 'AVAILABLE'", (source["source_id"],)).fetchone()[0]
        return {"source_id": source["source_id"], "source": dict(source),
                "created": created, "path_prefix": "", "assets_linked": available}
    plan = store.plan_source_add(conn, root)
    kind = plan["kind"]
    if kind in {"existing", "covered"}:
        source = plan["source"]
        source_id = source["source_id"]
        created = False
        path_prefix = _prefix_of(plan["root"], source["root_uri"])
    else:
        result = store.add_source_merged(conn, root)
        source = result["source"]
        source_id = source["source_id"]
        created = bool(result.get("created"))
        path_prefix = ""
    conn.execute(
        "INSERT INTO project_sources"
        " (project_id, source_id, relation_type, retrieval_policy, path_prefix,"
        "  created_at)"
        " VALUES (?, ?, ?, 'inherit', ?, ?)"
        " ON CONFLICT(project_id, source_id) DO UPDATE SET"
        " relation_type = excluded.relation_type,"
        " path_prefix = excluded.path_prefix",
        (project_id, source_id, relation_type, path_prefix, _now()),
    )
    linked = _materialize_source(
        conn, project_id, source_id, relation_type, path_prefix)
    touch_project(conn, project_id)
    conn.commit()
    return {"source_id": source_id, "source": dict(source), "created": created,
            "path_prefix": path_prefix, "assets_linked": linked}


def add_source(conn: sqlite3.Connection, project_id: str, source_id: str,
               relation_type: str = "primary", path_prefix: str = "") -> dict:
    require_project(conn, project_id)
    source = store.get_source(conn, source_id)
    if source is None:
        raise ValueError("数据源不存在")
    if relation_type not in SOURCE_RELATIONS:
        raise ValueError("未知的资料来源类型")
    if is_global(conn, project_id):
        available = conn.execute(
            "SELECT COUNT(*) FROM asset_locations WHERE source_id = ?"
            " AND availability = 'AVAILABLE'", (source_id,)).fetchone()[0]
        return {"source_id": source_id, "path_prefix": "", "assets_linked": available}
    clean_prefix = (path_prefix or "").strip().replace("\\", "/").lstrip("/")
    if clean_prefix and not clean_prefix.endswith("/"):
        clean_prefix += "/"
    conn.execute(
        "INSERT INTO project_sources"
        " (project_id, source_id, relation_type, retrieval_policy, path_prefix,"
        "  created_at)"
        " VALUES (?, ?, ?, 'inherit', ?, ?)"
        " ON CONFLICT(project_id, source_id) DO UPDATE SET"
        " relation_type = excluded.relation_type,"
        " path_prefix = excluded.path_prefix",
        (project_id, source_id, relation_type, clean_prefix, _now()),
    )
    linked = _materialize_source(
        conn, project_id, source_id, relation_type, clean_prefix)
    touch_project(conn, project_id)
    conn.commit()
    return {"source_id": source_id, "path_prefix": clean_prefix,
            "assets_linked": linked}


def remove_source(conn: sqlite3.Connection, project_id: str,
                  source_id: str) -> dict:
    """移除项目对文件夹的引用；显式关联的资产保留，其余主资料关系一并移除。"""
    require_project(conn, project_id)
    if is_global(conn, project_id):
        raise ValueError("全局项目包含所有数据源；请在数据页撤销授权")
    row = conn.execute(
        "SELECT path_prefix FROM project_sources"
        " WHERE project_id = ? AND source_id = ?", (project_id, source_id)).fetchone()
    prefix = row["path_prefix"] if row else ""
    cursor = conn.execute(
        "DELETE FROM project_sources WHERE project_id = ? AND source_id = ?",
        (project_id, source_id),
    )
    removed = conn.execute(
        "DELETE FROM project_assets WHERE project_id = ?"
        " AND membership_type = 'primary'"
        " AND asset_id IN (SELECT l.asset_id FROM asset_locations l"
        "   WHERE l.source_id = ? AND (? = '' OR l.rel_path LIKE ? || '%'))",
        (project_id, source_id, prefix, prefix),
    ).rowcount
    touch_project(conn, project_id)
    conn.commit()
    return {"source_id": source_id, "removed": cursor.rowcount > 0,
            "assets_removed": removed}


def list_sources(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    if is_global(conn, project_id):
        rows = conn.execute(
            "SELECT s.*, 'primary' AS relation_type, 'global' AS retrieval_policy,"
            " '' AS path_prefix,"
            " (SELECT COUNT(*) FROM asset_locations l"
            "   WHERE l.source_id = s.source_id AND l.availability = 'AVAILABLE')"
            "   AS available"
            " FROM sources s WHERE s.state = 'active' ORDER BY s.created_at",
        ).fetchall()
        return [dict(row) for row in rows]
    rows = conn.execute(
        "SELECT ps.*, s.label, s.root_uri, s.state, s.health, s.last_scan_at,"
        " (SELECT COUNT(*) FROM asset_locations l"
        "   WHERE l.source_id = ps.source_id AND l.availability = 'AVAILABLE')"
        "   AS available"
        " FROM project_sources ps JOIN sources s ON s.source_id = ps.source_id"
        " WHERE ps.project_id = ? ORDER BY ps.created_at",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def source_ids(conn: sqlite3.Connection, project_id: str) -> list[str]:
    if is_global(conn, project_id):
        return [row["source_id"] for row in conn.execute(
            "SELECT source_id FROM sources WHERE state = 'active'")]
    return [row["source_id"] for row in conn.execute(
        "SELECT source_id FROM project_sources WHERE project_id = ?", (project_id,))]


# ---------------------------------------------------------------- 资产成员关系

def _asset_locations(conn: sqlite3.Connection, asset_ids: list[str]) -> list[sqlite3.Row]:
    if not asset_ids:
        return []
    marks = ",".join("?" for _ in asset_ids)
    return conn.execute(
        f"SELECT asset_id FROM assets WHERE asset_id IN ({marks})", asset_ids).fetchall()


def set_membership(conn: sqlite3.Connection, project_id: str,
                   asset_ids: list[str], membership_type: str,
                   priority: int = 0) -> dict:
    require_project(conn, project_id)
    if membership_type not in MEMBERSHIPS:
        raise ValueError("未知的资产关系")
    if is_global(conn, project_id):
        raise ValueError("全局项目包含所有资产；请在子项目中调整关系")
    existing = {row["asset_id"] for row in _asset_locations(conn, asset_ids)}
    missing = [asset_id for asset_id in asset_ids if asset_id not in existing]
    if missing:
        raise ValueError(f"资产不存在：{missing[0]}")
    now = _now()
    for asset_id in asset_ids:
        conn.execute(
            "INSERT INTO project_assets"
            " (project_id, asset_id, membership_type, priority, status, created_at)"
            " VALUES (?, ?, ?, ?, 'active', ?)"
            " ON CONFLICT(project_id, asset_id) DO UPDATE SET"
            " membership_type = excluded.membership_type,"
            " priority = excluded.priority, status = 'active'",
            (project_id, asset_id, membership_type, int(priority), now),
        )
    touch_project(conn, project_id)
    conn.commit()
    return {"project_id": project_id, "updated": len(asset_ids),
            "membership_type": membership_type}


def add_assets_by_paths(conn: sqlite3.Connection, project_id: str,
                        paths: list[str], membership_type: str = "linked",
                        priority: int = 0) -> dict:
    require_project(conn, project_id)
    if membership_type not in MEMBERSHIPS:
        raise ValueError("未知的资产关系")
    found: list[str] = []
    missing: list[str] = []
    for path in paths:
        row = conn.execute(
            "SELECT asset_id FROM asset_locations WHERE path = ? LIMIT 1",
            (str(path),)).fetchone()
        if row is None or row["asset_id"] in found:
            if row is None:
                missing.append(str(path))
            continue
        found.append(row["asset_id"])
    if found:
        set_membership(conn, project_id, found, membership_type, priority)
    return {"updated": len(found), "missing": missing,
            "membership_type": membership_type}


def remove_asset(conn: sqlite3.Connection, project_id: str,
                 asset_id: str) -> dict:
    """来自主资料文件夹的资产改为「本项目排除」，显式关联的资产直接解除。"""
    require_project(conn, project_id)
    if is_global(conn, project_id):
        raise ValueError("全局项目不能排除资产；请在子项目中排除")
    row = conn.execute(
        "SELECT membership_type FROM project_assets"
        " WHERE project_id = ? AND asset_id = ?", (project_id, asset_id)).fetchone()
    if row is None:
        return {"project_id": project_id, "asset_id": asset_id, "removed": False}
    if row["membership_type"] == "linked":
        conn.execute(
            "DELETE FROM project_assets WHERE project_id = ? AND asset_id = ?",
            (project_id, asset_id))
        action = "unlinked"
    else:
        conn.execute(
            "UPDATE project_assets SET membership_type = 'excluded'"
            " WHERE project_id = ? AND asset_id = ?", (project_id, asset_id))
        action = "excluded"
    touch_project(conn, project_id)
    conn.commit()
    return {"project_id": project_id, "asset_id": asset_id,
            "removed": True, "action": action}


# ---------------------------------------------------------------- 扫描联动

def sync_source_assets(conn: sqlite3.Connection, source_id: str) -> int:
    """扫描/新增文件后，把资产补进引用该数据源的项目；已有关系不覆盖。"""
    projects = conn.execute(
        "SELECT project_id, relation_type, path_prefix FROM project_sources"
        " WHERE source_id = ?", (source_id,)).fetchall()
    if not projects:
        return 0
    now = _now()
    total = 0
    for row in projects:
        prefix = row["path_prefix"] or ""
        cursor = conn.execute(
            "INSERT OR IGNORE INTO project_assets"
            " (project_id, asset_id, membership_type, priority, status, created_at)"
            " SELECT ?, l.asset_id, ?, 0, 'active', ?"
            " FROM asset_locations l WHERE l.source_id = ?"
            " AND (? = '' OR l.rel_path LIKE ? || '%')",
            (row["project_id"], row["relation_type"], now, source_id, prefix, prefix),
        )
        total += cursor.rowcount
    if total:
        conn.commit()
    return total


def sync_all(conn: sqlite3.Connection) -> int:
    total = 0
    for row in conn.execute(
            "SELECT DISTINCT source_id FROM project_sources").fetchall():
        total += sync_source_assets(conn, row["source_id"])
    return total


# ---------------------------------------------------------------- 检索范围

def scope_for(conn: sqlite3.Connection, project_id: str | None,
              requested: str | None) -> str:
    if not project_id:
        return "global"
    if is_global(conn, project_id):
        return "global"
    if requested in SCOPES:
        return requested
    row = conn.execute(
        "SELECT retrieval_policy FROM projects WHERE project_id = ?",
        (project_id,)).fetchone()
    if row is None:
        return "global"
    return {"project_first": "project", "expanded": "expanded",
            "global": "global"}.get(row["retrieval_policy"], "project")


def asset_filter(conn: sqlite3.Connection, project_id: str | None,
                 scope: str = "project") -> tuple[set[str] | None, set[str]]:
    """返回 (允许的 asset_id 集合, 本项目排除的 asset_id 集合)。

    allowed 为 None 表示不限制（全局项目或 Global 范围）。
    排除集只在有普通项目上下文时生效。
    """
    if not project_id:
        return None, set()
    row = conn.execute(
        "SELECT kind FROM projects WHERE project_id = ? AND status = 'active'",
        (project_id,)).fetchone()
    if row is None:
        return None, set()
    if row["kind"] == "global":
        return None, set()
    rows = conn.execute(
        "SELECT asset_id, membership_type FROM project_assets"
        " WHERE project_id = ? AND status = 'active'", (project_id,)).fetchall()
    primary = {row["asset_id"] for row in rows if row["membership_type"] == "primary"}
    linked = {row["asset_id"] for row in rows if row["membership_type"] == "linked"}
    excluded = {row["asset_id"] for row in rows if row["membership_type"] == "excluded"}
    if scope == "global":
        allowed: set[str] | None = None
    elif scope == "expanded":
        allowed = (primary | linked) - excluded
    else:
        allowed = primary - excluded
    return allowed, excluded


def allowed_assets(conn: sqlite3.Connection, project_id: str | None,
                   scope: str) -> set[str] | None:
    return asset_filter(conn, project_id, scope)[0]


# ---------------------------------------------------------------- 统计与列表

def _global_available(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        " WHERE l.availability = 'AVAILABLE' AND s.state = 'active'").fetchone()[0]


def _global_stats(conn: sqlite3.Connection) -> dict:
    from . import indexer

    total = _global_available(conn)
    coverage = indexer.coverage(conn)
    parsed = int(coverage.get("indexed", 0))
    failed = int(coverage.get("failed", 0))
    pending = int(coverage.get("pending", 0)) + int(coverage.get("metadata_only", 0))
    embedded = conn.execute(
        "SELECT COUNT(DISTINCT e.asset_id) FROM chunk_embeddings e"
        " JOIN asset_locations l ON l.asset_id = e.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE e.asset_id IS NOT NULL AND l.availability = 'AVAILABLE'"
        " AND s.state = 'active'").fetchone()[0]
    last = conn.execute(
        "SELECT MAX(v.observed_at) FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE v.is_current = 1 AND l.availability = 'AVAILABLE'"
        " AND s.state = 'active'").fetchone()[0]
    return {
        "assets": total, "primary": total, "linked": 0, "excluded": 0,
        "usable": total, "parsed": parsed, "embedded": embedded,
        "failed": failed, "pending": int(coverage.get("pending", 0)),
        "sources": conn.execute(
            "SELECT COUNT(*) FROM sources WHERE state = 'active'").fetchone()[0],
        "last_updated": last,
        "scope": "global",
    }


def project_stats(conn: sqlite3.Connection, project_id: str) -> dict:
    if is_global(conn, project_id):
        return _global_stats(conn)
    counts = {"total": 0, "primary": 0, "linked": 0, "excluded": 0}
    for row in conn.execute(
            "SELECT membership_type, COUNT(*) AS n FROM project_assets"
            " WHERE project_id = ? AND status = 'active' GROUP BY membership_type",
            (project_id,)):
        counts[row["membership_type"]] = row["n"]
        counts["total"] += row["n"]
    usable = counts["total"] - counts["excluded"]
    # 用 EXISTS 按资产短路，避免把项目内全部 chunk 与向量先 JOIN 再 DISTINCT。
    parsed = conn.execute(
        "SELECT COUNT(*) FROM project_assets pa"
        " WHERE pa.project_id = ? AND pa.membership_type != 'excluded'"
        " AND EXISTS (SELECT 1 FROM content_versions v"
        "   JOIN parse_artifacts a ON a.version_id = v.version_id"
        "   WHERE v.asset_id = pa.asset_id AND v.is_current = 1"
        "   AND a.state = 'ok')",
        (project_id,)).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM project_assets pa"
        " WHERE pa.project_id = ? AND pa.membership_type != 'excluded'"
        " AND EXISTS (SELECT 1 FROM content_versions v"
        "   JOIN parse_artifacts a ON a.version_id = v.version_id"
        "   WHERE v.asset_id = pa.asset_id AND v.is_current = 1"
        "   AND a.state = 'failed')",
        (project_id,)).fetchone()[0]
    embedded = conn.execute(
        "SELECT COUNT(DISTINCT e.asset_id) FROM chunk_embeddings e"
        " WHERE e.asset_id IN (SELECT asset_id FROM project_assets"
        "   WHERE project_id = ? AND membership_type != 'excluded')",
        (project_id,)).fetchone()[0]
    last = conn.execute(
        "SELECT MAX(v.observed_at) FROM project_assets pa"
        " JOIN content_versions v ON v.asset_id = pa.asset_id AND v.is_current = 1"
        " WHERE pa.project_id = ? AND pa.membership_type != 'excluded'",
        (project_id,)).fetchone()[0]
    return {
        "assets": counts["total"], "primary": counts["primary"],
        "linked": counts["linked"], "excluded": counts["excluded"],
        "usable": usable, "parsed": parsed, "embedded": embedded,
        "failed": failed, "pending": max(0, usable - parsed - failed),
        "sources": len(source_ids(conn, project_id)),
        "last_updated": last,
    }


def stats_view(conn: sqlite3.Connection, project_id: str | None) -> dict:
    """MCP / HTTP 外部接口使用的统计视图：有项目时只暴露项目范围。"""
    from . import search

    base = search.stats(conn, project_id=project_id)
    if not project_id:
        return base
    stats = project_stats(conn, project_id)
    base["files"] = {
        "available": stats["usable"], "missing": 0, "offline": 0, "stale": 0,
    }
    base["by_ext"] = [
        {"ext": row["ext"], "count": row["n"]}
        for row in conn.execute(
            "SELECT l.ext, COUNT(*) AS n FROM asset_locations l"
            " JOIN project_assets pa ON pa.asset_id = l.asset_id"
            " WHERE pa.project_id = ? AND pa.membership_type != 'excluded'"
            " AND l.availability = 'AVAILABLE'"
            " GROUP BY l.ext ORDER BY n DESC LIMIT 8", (project_id,))
    ]
    base["sources"] = [
        {
            "source_id": row["source_id"],
            "label": row["label"],
            "root_uri": row["root_uri"],
            "state": row["state"],
            "health": row["health"],
            "last_scan_at": row["last_scan_at"],
            "available": row.get("available", 0),
        }
        for row in list_sources(conn, project_id)
    ]
    coverage = dict(base.get("coverage") or {})
    coverage.update({
        "content_indexed": stats["parsed"],
        "content_pending": stats["pending"],
        "content_failed": stats["failed"],
    })
    base["coverage"] = coverage
    base["scope"] = {
        "project_id": project_id,
        "used": "global" if is_global(conn, project_id) else "expanded",
    }
    return base


def list_assets(conn: sqlite3.Connection, project_id: str, *,
                membership: str | None = None, query: str | None = None,
                limit: int = 100, offset: int = 0) -> dict:
    if is_global(conn, project_id):
        listing = list_global_assets(
            conn, unassigned=False, query=query, limit=limit, offset=offset)
        items = [
            {
                "asset_id": item["asset_id"],
                "location_id": item["location_id"],
                "name": item["name"],
                "path": item["path"],
                "rel_path": item.get("rel_path", ""),
                "ext": item["ext"],
                "size": item["size"],
                "membership_type": "primary",
                "priority": 0,
                "source_label": item["source_label"],
                "parse_state": item.get("parse_state"),
                "parse_message": None,
            }
            for item in listing["items"]
        ]
        return {**listing, "items": items}
    clauses = ["pa.project_id = ?", "pa.status = 'active'"]
    params: list = [project_id]
    if membership in MEMBERSHIPS:
        clauses.append("pa.membership_type = ?")
        params.append(membership)
    if query:
        clauses.append("lower(l.path) LIKE ?")
        params.append(f"%{query.strip().lower()}%")
    where = " AND ".join(clauses)
    total = conn.execute(
        "SELECT COUNT(*) FROM project_assets pa"
        " JOIN asset_locations l ON l.asset_id = pa.asset_id"
        f" WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT pa.asset_id, pa.membership_type, pa.priority, pa.created_at,"
        " l.location_id, l.path, l.rel_path, l.ext, l.size, l.mtime_ns,"
        " l.availability, s.label AS source_label,"
        " a.state AS parse_state, a.message AS parse_message"
        " FROM project_assets pa"
        " JOIN asset_locations l ON l.asset_id = pa.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN content_versions v ON v.asset_id = pa.asset_id AND v.is_current = 1"
        " LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        f" WHERE {where}"
        " ORDER BY pa.membership_type, l.path LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    items = [
        {
            "asset_id": row["asset_id"],
            "location_id": row["location_id"],
            "name": row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            "path": row["path"],
            "rel_path": row["rel_path"],
            "ext": row["ext"],
            "size": row["size"],
            "membership_type": row["membership_type"],
            "priority": row["priority"],
            "source_label": row["source_label"],
            "parse_state": row["parse_state"],
            "parse_message": row["parse_message"],
        }
        for row in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def list_global_assets(conn: sqlite3.Connection, *, unassigned: bool = False,
                       query: str | None = None, limit: int = 100,
                       offset: int = 0) -> dict:
    clauses = ["l.availability = 'AVAILABLE'", "s.state = 'active'"]
    params: list = []
    if unassigned:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM project_assets pa WHERE pa.asset_id = l.asset_id)")
    if query:
        clauses.append("lower(l.path) LIKE ?")
        params.append(f"%{query.strip().lower()}%")
    where = " AND ".join(clauses)
    total = conn.execute(
        "SELECT COUNT(*) FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        "SELECT l.asset_id, l.location_id, l.path, l.rel_path, l.ext, l.size,"
        " l.mtime_ns, s.label AS source_label,"
        " (SELECT a.state FROM content_versions v"
        "   LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        "   WHERE v.asset_id = l.asset_id AND v.is_current = 1 LIMIT 1) AS parse_state,"
        " (SELECT GROUP_CONCAT(p.name, '、') FROM project_assets pa"
        "   JOIN projects p ON p.project_id = pa.project_id"
        "   WHERE pa.asset_id = l.asset_id) AS projects"
        " FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        f" WHERE {where} ORDER BY l.mtime_ns DESC LIMIT ? OFFSET ?",
        [*params, max(1, min(limit, 500)), max(0, offset)],
    ).fetchall()
    items = []
    for row in rows:
        items.append({
            "asset_id": row["asset_id"],
            "location_id": row["location_id"],
            "name": row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            "path": row["path"],
            "rel_path": row["rel_path"],
            "ext": row["ext"],
            "size": row["size"],
            "parse_state": row["parse_state"],
            "source_label": row["source_label"],
            "projects": [name for name in (row["projects"] or "").split("、") if name],
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def projects_for_asset(conn: sqlite3.Connection, asset_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT p.project_id, p.name, pa.membership_type FROM project_assets pa"
        " JOIN projects p ON p.project_id = pa.project_id"
        " WHERE pa.asset_id = ? ORDER BY p.name", (asset_id,)).fetchall()
    return [dict(row) for row in rows]


def project_knowledge(conn: sqlite3.Connection, project_id: str,
                      limit: int = 20) -> dict:
    """项目内的摘要 / 实体 / 关系 / FAQ，全部可回溯到资产。"""
    require_project(conn, project_id)
    if is_global(conn, project_id):
        summaries = [dict(row) for row in conn.execute(
            "SELECT * FROM knowledge_summaries ORDER BY updated_at DESC LIMIT ?",
            (limit,))]
        paths = {}
        for item in summaries:
            if item["scope_type"] == "document":
                row = conn.execute(
                    "SELECT path FROM asset_locations WHERE asset_id = ? LIMIT 1",
                    (item["scope_id"],)).fetchone()
                paths[item["scope_id"]] = row["path"] if row else None
            item["path"] = paths.get(item["scope_id"])
        return {
            "summaries": summaries,
            "entities": [dict(row) for row in conn.execute(
                "SELECT * FROM knowledge_entities ORDER BY updated_at DESC LIMIT ?",
                (limit,))],
            "relations": [dict(row) for row in conn.execute(
                "SELECT * FROM knowledge_relations ORDER BY updated_at DESC LIMIT ?",
                (limit,))],
            "questions": [dict(row) for row in conn.execute(
                "SELECT * FROM knowledge_questions ORDER BY updated_at DESC LIMIT ?",
                (limit,))],
        }
    assets = [row["asset_id"] for row in conn.execute(
        "SELECT asset_id FROM project_assets WHERE project_id = ?"
        " AND membership_type != 'excluded'", (project_id,))]
    if not assets:
        return {"summaries": [], "entities": [], "relations": [], "questions": []}
    marks = ",".join("?" for _ in assets)

    def scoped(table: str, key: str) -> list[sqlite3.Row]:
        return conn.execute(
            f"SELECT * FROM {table} WHERE {key} IN ({marks})"
            f" ORDER BY updated_at DESC LIMIT ?", [*assets, limit]).fetchall()

    paths = {row["asset_id"]: row["path"] for row in conn.execute(
        "SELECT asset_id, path FROM asset_locations"
        f" WHERE asset_id IN ({marks})", assets)}
    summaries = []
    for row in scoped("knowledge_summaries", "scope_id"):
        summaries.append({**dict(row), "path": paths.get(row["scope_id"])})
    entities = []
    for row in conn.execute(
            "SELECT * FROM knowledge_entities WHERE family_id IN"
            f" (SELECT asset_id FROM assets WHERE asset_id IN ({marks}))"
            " ORDER BY updated_at DESC LIMIT ?", [*assets, limit]):
        entities.append({**dict(row), "path": paths.get(row["family_id"])})
    relations = []
    for row in conn.execute(
            "SELECT * FROM knowledge_relations WHERE family_id IN"
            f" (SELECT asset_id FROM assets WHERE asset_id IN ({marks}))"
            " ORDER BY updated_at DESC LIMIT ?", [*assets, limit]):
        relations.append({**dict(row), "path": paths.get(row["family_id"])})
    questions = []
    for row in conn.execute(
            "SELECT * FROM knowledge_questions WHERE family_id IN"
            f" (SELECT asset_id FROM assets WHERE asset_id IN ({marks}))"
            " ORDER BY updated_at DESC LIMIT ?", [*assets, limit]):
        questions.append({**dict(row), "path": paths.get(row["family_id"])})
    return {"summaries": summaries, "entities": entities,
            "relations": relations, "questions": questions}
