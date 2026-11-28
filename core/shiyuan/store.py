"""数据访问层：数据源、资产位置、内容版本与维护事件。"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .config import app_home

# 首版拒绝授权整盘根目录与系统目录，避免一次扫描吞掉整个系统盘。
SYSTEM_PREFIXES = (
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def log_event(conn: sqlite3.Connection, kind: str, source_id: str | None = None,
              detail: str | None = None, outcome: str | None = None) -> None:
    conn.execute(
        "INSERT INTO maintenance_events (type, source_id, detail, outcome, timestamp)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, source_id, detail, outcome, now_iso()),
    )


# ---------------------------------------------------------------- 数据源

def normalize_root(root: str | Path) -> Path:
    raw = Path(root).expanduser()
    return Path(os.path.realpath(raw))


def validate_root(root: str | Path) -> Path:
    resolved = normalize_root(root)
    if not resolved.exists():
        raise ValueError("目录不存在或当前不可访问")
    if not resolved.is_dir():
        raise ValueError("请选择一个文件夹，而不是文件")
    if resolved.parent == resolved:
        raise ValueError("不能授权整个磁盘根目录，请选择具体文件夹")
    low = str(resolved).lower().rstrip("\\")
    for prefix in SYSTEM_PREFIXES:
        if low == prefix or low.startswith(prefix + "\\"):
            raise ValueError("不能授权系统目录")
    home = Path(os.path.realpath(app_home())).as_posix().lower()
    if resolved.as_posix().lower() == home or resolved.as_posix().lower().startswith(home + "/"):
        raise ValueError("不能把应用自身的数据目录作为数据源")
    return resolved


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY created_at").fetchall()


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()


_STAGE_COLUMNS = {
    "l0": "l0_completed_at",
    "l1": "l1_completed_at",
    "l2": "l2_completed_at",
}


def mark_ingest_stage(conn: sqlite3.Connection, source_id: str, stage: str) -> None:
    """记录某个数据源首次完成 L0/L1/L2 的时间；重复完成保留第一次。"""
    column = _STAGE_COLUMNS[stage]
    conn.execute(
        f"UPDATE sources SET {column} = COALESCE({column}, ?) WHERE source_id = ?",
        (now_iso(), source_id),
    )
    conn.commit()


def refresh_full_coverage(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    row = get_source(conn, source_id)
    if row is None:
        return None
    if row["l0_completed_at"] and row["l1_completed_at"] and row["l2_completed_at"]:
        conn.execute(
            "UPDATE sources SET full_coverage_at = COALESCE(full_coverage_at, ?)"
            " WHERE source_id = ?",
            (now_iso(), source_id),
        )
        conn.commit()
        return get_source(conn, source_id)
    return row


def list_incomplete_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sources WHERE state = 'active' AND full_coverage_at IS NULL"
        " ORDER BY created_at"
    ).fetchall()


def add_source(conn: sqlite3.Connection, root: str | Path, label: str | None = None,
               excludes: list[str] | None = None) -> sqlite3.Row:
    resolved = validate_root(root)
    existing = list_sources(conn)
    target = resolved.as_posix().lower()
    for row in existing:
        current = Path(row["root_uri"]).as_posix().lower()
        if target == current:
            raise ValueError("该目录已经授权过了")
        if target.startswith(current + "/") or current.startswith(target + "/"):
            raise ValueError("该目录与已授权目录存在包含关系，首版请只授权其中一层")

    source_id = new_id("src")
    conn.execute(
        "INSERT INTO sources (source_id, root_uri, label, policy, excludes, state, health, created_at)"
        " VALUES (?, ?, ?, 'readonly', ?, 'active', 'unknown', ?)",
        (
            source_id,
            str(resolved),
            label or resolved.name or str(resolved),
            json.dumps(excludes or [], ensure_ascii=False),
            now_iso(),
        ),
    )
    log_event(conn, "source_added", source_id, detail=str(resolved), outcome="ok")
    conn.commit()
    return get_source(conn, source_id)


def _norm_root(value: str | Path) -> str:
    return Path(value).as_posix().lower().rstrip("/")


def plan_source_add(conn: sqlite3.Connection, root: str | Path) -> dict:
    """判断新增数据源与现有目录的关系。

    返回 kind：
    - existing：同一目录已授权；
    - covered：新目录在某个已授权目录内，返回最近的上级；
    - merge：新目录包含一个或多个已授权子目录，需要合并；
    - new：互不包含。
    """
    resolved = validate_root(root)
    target = _norm_root(resolved)
    existing = list_sources(conn)
    for row in existing:
        if target == _norm_root(row["root_uri"]):
            return {"kind": "existing", "source": row, "merged": [],
                    "root": resolved, "covered_by": row["source_id"]}
    parents = [
        row for row in existing
        if target.startswith(_norm_root(row["root_uri"]) + "/")
    ]
    if parents:
        parent = max(parents, key=lambda row: len(_norm_root(row["root_uri"])))
        return {"kind": "covered", "source": parent, "merged": [],
                "root": resolved, "covered_by": parent["source_id"]}
    children = [
        row for row in existing
        if _norm_root(row["root_uri"]).startswith(target + "/")
    ]
    return {
        "kind": "merge" if children else "new",
        "source": None,
        "merged": children,
        "root": resolved,
        "covered_by": None,
    }


def _merge_child_source(conn: sqlite3.Connection, child: sqlite3.Row,
                        parent_id: str, parent_root: Path) -> dict:
    child_id = child["source_id"]
    rows = conn.execute(
        "SELECT location_id, path FROM asset_locations WHERE source_id = ?", (child_id,)
    ).fetchall()
    for row in rows:
        try:
            rel_path = Path(row["path"]).relative_to(parent_root).as_posix()
        except ValueError:
            continue
        conn.execute(
            "UPDATE asset_locations SET source_id = ?, rel_path = ? WHERE location_id = ?",
            (parent_id, rel_path, row["location_id"]),
        )
    conn.execute("UPDATE index_jobs SET source_id = ? WHERE source_id = ?", (parent_id, child_id))
    conn.execute("DELETE FROM sources WHERE source_id = ?", (child_id,))
    return {"source_id": child_id, "label": child["label"], "root_uri": child["root_uri"]}


def add_source_merged(conn: sqlite3.Connection, root: str | Path, label: str | None = None,
                      excludes: list[str] | None = None) -> dict:
    """新增数据源；若与现有目录存在包含关系，自动合并为上层目录。"""
    plan = plan_source_add(conn, root)
    if plan["kind"] in {"existing", "covered"}:
        return {"source": plan["source"], "merged": [], "created": False,
                "covered_by": plan["covered_by"]}

    resolved: Path = plan["root"]
    merged_excludes: list[str] = list(excludes or [])
    for child in plan["merged"]:
        try:
            merged_excludes.extend(json.loads(child["excludes"] or "[]"))
        except (TypeError, ValueError):
            continue
    merged_excludes = list(dict.fromkeys(merged_excludes))

    source_id = new_id("src")
    conn.execute(
        "INSERT INTO sources (source_id, root_uri, label, policy, excludes, state, health, created_at)"
        " VALUES (?, ?, ?, 'readonly', ?, 'active', 'unknown', ?)",
        (
            source_id,
            str(resolved),
            label or resolved.name or str(resolved),
            json.dumps(merged_excludes, ensure_ascii=False),
            now_iso(),
        ),
    )
    log_event(conn, "source_added", source_id, detail=str(resolved), outcome="ok")
    merged = [
        _merge_child_source(conn, child, source_id, resolved)
        for child in plan["merged"]
    ]
    for item in merged:
        log_event(conn, "source_merged", source_id,
                  detail=json.dumps(item, ensure_ascii=False), outcome="ok")
    conn.commit()
    return {"source": get_source(conn, source_id), "merged": merged,
            "created": True, "covered_by": None}


def set_source_state(conn: sqlite3.Connection, source_id: str, state: str) -> sqlite3.Row | None:
    if state not in {"active", "paused"}:
        raise ValueError("未知的数据源状态")
    conn.execute("UPDATE sources SET state = ? WHERE source_id = ?", (state, source_id))
    log_event(conn, "source_state", source_id, detail=state, outcome="ok")
    conn.commit()
    return get_source(conn, source_id)


def _purge_content_cache(conn: sqlite3.Connection, asset_id: str) -> None:
    """删除资产的全部派生文本缓存（FTS → chunks → artifacts）。"""
    conn.execute(
        "DELETE FROM chunk_fts WHERE version_id IN"
        " (SELECT version_id FROM content_versions WHERE asset_id = ?)",
        (asset_id,),
    )
    conn.execute("DELETE FROM chunks WHERE asset_id = ?", (asset_id,))
    conn.execute(
        "DELETE FROM parse_artifacts WHERE version_id IN"
        " (SELECT version_id FROM content_versions WHERE asset_id = ?)",
        (asset_id,),
    )
    conn.execute(
        "DELETE FROM index_jobs WHERE version_id IN"
        " (SELECT version_id FROM content_versions WHERE asset_id = ?)",
        (asset_id,),
    )


def _purge_asset_derived(conn: sqlite3.Connection, asset_id: str) -> None:
    """删除资产的全部派生数据：文本缓存、向量、家族成员与文档级知识。

    撤销授权与策略性排除都走这里，保证缓存与衍生知识不能绕过权限继续暴露。
    """
    _purge_content_cache(conn, asset_id)
    conn.execute("DELETE FROM chunk_embeddings WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM document_family_members WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM family_content_sources WHERE asset_id = ?", (asset_id,))
    # 文档级知识把 asset_id 存在 family_id 列里，FAQ 向量按 question_id 单独清理。
    for question_id in [
        row["question_id"] for row in conn.execute(
            "SELECT question_id FROM knowledge_questions WHERE family_id = ?", (asset_id,))
    ]:
        conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_id = ?", (question_id,))
    for table in ("knowledge_questions", "knowledge_entities", "knowledge_relations"):
        conn.execute(f"DELETE FROM {table} WHERE family_id = ?", (asset_id,))
    conn.execute(
        "DELETE FROM knowledge_summaries WHERE scope_type = 'document' AND scope_id = ?",
        (asset_id,))
    conn.execute(
        "DELETE FROM knowledge_jobs WHERE scope_type = 'document' AND scope_id = ?",
        (asset_id,))


def _purge_empty_families(conn: sqlite3.Connection) -> None:
    """成员已全部移除的家族连同家族级知识一起清理。"""
    family_ids = [
        row["family_id"] for row in conn.execute(
            "SELECT f.family_id FROM document_families f WHERE NOT EXISTS"
            " (SELECT 1 FROM document_family_members m WHERE m.family_id = f.family_id)")
    ]
    for family_id in family_ids:
        for question_id in [
            row["question_id"] for row in conn.execute(
                "SELECT question_id FROM knowledge_questions WHERE family_id = ?",
                (family_id,))
        ]:
            conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_id = ?", (question_id,))
        for table in ("knowledge_questions", "knowledge_entities", "knowledge_relations"):
            conn.execute(f"DELETE FROM {table} WHERE family_id = ?", (family_id,))
        conn.execute(
            "DELETE FROM knowledge_summaries WHERE scope_type = 'family' AND scope_id = ?",
            (family_id,))
        conn.execute(
            "DELETE FROM knowledge_jobs WHERE scope_type = 'family' AND scope_id = ?",
            (family_id,))
        conn.execute("DELETE FROM family_content_groups WHERE family_id = ?", (family_id,))
        conn.execute("DELETE FROM document_families WHERE family_id = ?", (family_id,))


def drop_locations(conn: sqlite3.Connection, location_ids: list[str]) -> int:
    """删除指定位置；资产没有其他位置时一并清掉派生缓存。

    用于策略性排除的文件（如 Office 锁文件）从索引中移除。
    """
    if not location_ids:
        return 0
    asset_ids: set[str] = set()
    with conn:
        for location_id in location_ids:
            row = conn.execute(
                "SELECT asset_id FROM asset_locations WHERE location_id = ?",
                (location_id,),
            ).fetchone()
            if row is None:
                continue
            asset_ids.add(row["asset_id"])
            conn.execute(
                "DELETE FROM asset_locations WHERE location_id = ?", (location_id,))
        for asset_id in asset_ids:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM asset_locations WHERE asset_id = ?", (asset_id,)
            ).fetchone()[0]
            if remaining:
                continue
            conn.execute("DELETE FROM project_assets WHERE asset_id = ?", (asset_id,))
            _purge_asset_derived(conn, asset_id)
            conn.execute("DELETE FROM content_versions WHERE asset_id = ?", (asset_id,))
            conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
        _purge_empty_families(conn)
    return len(location_ids)


def remove_source(conn: sqlite3.Connection, source_id: str) -> dict[str, int]:
    """撤销授权：删除该数据源的全部派生记录，缓存不能绕过权限继续暴露。"""
    source = get_source(conn, source_id)
    if source is None:
        raise KeyError(source_id)
    asset_ids = [
        row["asset_id"]
        for row in conn.execute(
            "SELECT DISTINCT asset_id FROM asset_locations WHERE source_id = ?", (source_id,)
        )
    ]
    with conn:
        conn.execute("DELETE FROM project_sources WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM asset_locations WHERE source_id = ?", (source_id,))
        for asset_id in asset_ids:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM asset_locations WHERE asset_id = ?", (asset_id,)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute("DELETE FROM project_assets WHERE asset_id = ?", (asset_id,))
                _purge_asset_derived(conn, asset_id)
                conn.execute("DELETE FROM content_versions WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
        _purge_empty_families(conn)
        conn.execute("DELETE FROM source_scan_runs WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM index_jobs WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
        log_event(conn, "source_removed", source_id, detail=source["root_uri"], outcome="ok")
    return {"assets_cleared": len(asset_ids)}


def source_stats(conn: sqlite3.Connection, source_id: str) -> dict:
    totals = conn.execute(
        "SELECT availability, COUNT(*) AS n FROM asset_locations WHERE source_id = ?"
        " GROUP BY availability",
        (source_id,),
    ).fetchall()
    counts = {row["availability"]: row["n"] for row in totals}
    last_run = conn.execute(
        "SELECT * FROM source_scan_runs WHERE source_id = ? ORDER BY started_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return {
        "counts": counts,
        "available": counts.get("AVAILABLE", 0),
        "stale": counts.get("STALE", 0),
        "missing": counts.get("MISSING", 0),
        "offline": counts.get("OFFLINE", 0),
        "last_run": dict(last_run) if last_run else None,
    }


# ---------------------------------------------------------------- 位置与版本

def list_locations(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM asset_locations WHERE source_id = ?", (source_id,)
    ).fetchall()


def _new_version(conn: sqlite3.Connection, asset_id: str, content_hash: str,
                 size: int, mtime_ns: int) -> str:
    version_id = new_id("ver")
    conn.execute(
        "UPDATE content_versions SET is_current = 0 WHERE asset_id = ? AND is_current = 1",
        (asset_id,),
    )
    conn.execute(
        "INSERT INTO content_versions (version_id, asset_id, hash, size, mtime_ns, observed_at, is_current)"
        " VALUES (?, ?, ?, ?, ?, ?, 1)",
        (version_id, asset_id, content_hash, size, mtime_ns, now_iso()),
    )
    return version_id


def insert_location(conn: sqlite3.Connection, *, source_id: str, path: str, rel_path: str,
                    native_file_id: str | None, size: int, mtime_ns: int, ext: str,
                    content_hash: str) -> str:
    asset_id = new_id("ast")
    location_id = new_id("loc")
    stamp = now_iso()
    conn.execute(
        "INSERT INTO assets (asset_id, kind, lifecycle, created_at) VALUES (?, 'file', 'active', ?)",
        (asset_id, stamp),
    )
    conn.execute(
        "INSERT INTO asset_locations (location_id, asset_id, source_id, path, rel_path,"
        " native_file_id, availability, size, mtime_ns, ext, first_seen_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'AVAILABLE', ?, ?, ?, ?, ?)",
        (location_id, asset_id, source_id, path, rel_path, native_file_id,
         size, mtime_ns, ext, stamp, stamp),
    )
    _new_version(conn, asset_id, content_hash, size, mtime_ns)
    return location_id


def update_location(conn: sqlite3.Connection, location: sqlite3.Row, *, path: str, rel_path: str,
                    native_file_id: str | None, size: int, mtime_ns: int, ext: str,
                    content_hash: str) -> None:
    moved = path != location["path"]
    conn.execute(
        "UPDATE asset_locations SET path = ?, rel_path = ?, native_file_id = ?,"
        " availability = 'AVAILABLE', size = ?, mtime_ns = ?, ext = ?, last_seen_at = ?"
        " WHERE location_id = ?",
        (path, rel_path, native_file_id or location["native_file_id"], size, mtime_ns, ext,
         now_iso(), location["location_id"]),
    )
    if moved:
        log_event(conn, "location_moved", location["source_id"],
                  detail=f"{location['path']} -> {path}", outcome="ok")
    _new_version(conn, location["asset_id"], content_hash, size, mtime_ns)


def mark_missing(conn: sqlite3.Connection, location_ids: list[str]) -> int:
    if not location_ids:
        return 0
    conn.executemany(
        "UPDATE asset_locations SET availability = 'MISSING' WHERE location_id = ?",
        [(location_id,) for location_id in location_ids],
    )
    return len(location_ids)


def _path_placeholders(paths: list[str]) -> str:
    return ",".join("?" for _ in paths)


def mark_stale(conn: sqlite3.Connection, source_id: str, paths: list[str]) -> int:
    """事件到达即标记 STALE：在同步完成前，检索不再把它当作当前事实。"""
    if not paths:
        return 0
    lowered = [path.lower() for path in paths]
    cursor = conn.execute(
        f"UPDATE asset_locations SET availability = 'STALE'"
        f" WHERE source_id = ? AND lower(path) IN ({_path_placeholders(lowered)})"
        f" AND availability = 'AVAILABLE'",
        [source_id, *lowered],
    )
    return cursor.rowcount


def mark_paths_missing(conn: sqlite3.Connection, source_id: str, paths: list[str]) -> int:
    if not paths:
        return 0
    lowered = [path.lower() for path in paths]
    cursor = conn.execute(
        f"UPDATE asset_locations SET availability = 'MISSING'"
        f" WHERE source_id = ? AND lower(path) IN ({_path_placeholders(lowered)})"
        f" AND availability != 'MISSING'",
        [source_id, *lowered],
    )
    return cursor.rowcount


def restore_location(conn: sqlite3.Connection, location_id: str) -> None:
    """内容未变（大小/时间/文件 ID 一致）时，把 STALE 恢复为 AVAILABLE。"""
    conn.execute(
        "UPDATE asset_locations SET availability = 'AVAILABLE', last_seen_at = ?"
        " WHERE location_id = ?",
        (now_iso(), location_id),
    )


def mark_source_offline(conn: sqlite3.Connection, source_id: str) -> int:
    cursor = conn.execute(
        "UPDATE asset_locations SET availability = 'OFFLINE'"
        " WHERE source_id = ? AND availability != 'OFFLINE'",
        (source_id,),
    )
    conn.execute("UPDATE sources SET health = 'offline' WHERE source_id = ?", (source_id,))
    return cursor.rowcount
