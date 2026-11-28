"""HTTP 接口：桌面端通过 127.0.0.1 与 Knowledge Core 通信。

只监听回环地址；所有接口都只读用户文件，不提供移动/删除原件的操作。
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import (__version__, answer, chat, config, db, embeddings, external, families,
               indexer, ingest, inventory, knowledge, maintenance, ocr, projects, query,
               scanner, search, secrets_store, store, updates, watcher)
from .config import db_path


def _request_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-shiyuan-token", "").strip()


def _write_api_token(token: str) -> None:
    path = config.api_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".api-token.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temp, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.replace(temp, path)


def _prepare_api_token(app: FastAPI) -> None:
    if not config.api_auth_enabled():
        app.state.api_token = None
        return
    token = os.environ.get("SHIYUAN_API_TOKEN", "").strip() or secrets.token_urlsafe(32)
    app.state.api_token = token
    _write_api_token(token)


def _remove_api_token(app: FastAPI) -> None:
    token = getattr(app.state, "api_token", None)
    if not token:
        return
    try:
        path = config.api_token_path()
        if path.read_text(encoding="utf-8").strip() == token:
            path.unlink()
    except OSError:
        pass
    app.state.api_token = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    if os.name == "nt":
        try:
            secrets_store.migrate_app_settings(conn)
        except Exception:
            pass  # 迁移失败不应阻止核心启动；下次启动会重试
    conn.close()
    _prepare_api_token(app)
    manager = None
    if config.watch_enabled():
        manager = watcher.WatcherManager(db_path())
        manager.start()
    app.state.watcher = manager
    # 首次建库未完成的数据源在重启后自动续跑；已有队列在数据库里，不会重复解析。
    scanner.resume_incomplete_sources()
    # 文档家族在后台重建，不阻塞核心启动。
    families.rebuild_async()
    # 衍生知识（摘要/实体/FAQ）后台消费队列，不阻塞核心启动。
    knowledge.start_worker()
    # 语义向量缓存后台预热，避免第一次检索等建缓存。
    embeddings.warm_cache_async()
    try:
        yield
    finally:
        knowledge.stop_worker()
        if manager is not None:
            manager.stop()
        _remove_api_token(app)


app = FastAPI(title="拾源 Knowledge Core", version=__version__, lifespan=lifespan)


_EXTERNAL_GET_PATHS = {
    "/api/health", "/api/sources", "/api/search", "/api/stats",
}
_EXTERNAL_POST_PATHS = {"/api/ask", "/api/ask/stream"}


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """本机 API 默认要求访问令牌；外部 Agent 令牌只能读检索、问答与统计。"""
    if request.method == "OPTIONS" or not config.api_auth_enabled():
        return await call_next(request)
    expected = getattr(request.app.state, "api_token", None)
    supplied = _request_token(request)
    if expected and supplied and hmac.compare_digest(supplied, expected):
        return await call_next(request)

    if supplied:
        client = await run_in_threadpool(external.authenticate, supplied)
        if client is not None:
            # 运行时复核项目隐私：令牌创建后改成 local_only 或归档，外部入口立即关闭。
            project_id = client.get("project_id")
            if project_id and await run_in_threadpool(_project_closed_to_external, project_id):
                return JSONResponse(
                    {"detail": "该项目仅允许本地使用，外部接口已关闭"},
                    status_code=403,
                )
            path = request.url.path
            method = request.method.upper()
            allowed = (
                (method == "GET" and path in _EXTERNAL_GET_PATHS)
                or (method == "POST" and path in _EXTERNAL_POST_PATHS)
            )
            if not allowed:
                return JSONResponse(
                    {"detail": "外部 Agent 令牌只允许只读检索、问答与统计"},
                    status_code=403,
                )
            request.state.external_client = client
            return await call_next(request)

    return JSONResponse(
        {"detail": "未授权：缺少或无效的本地访问令牌"},
        status_code=401,
    )


# CORS 必须最后添加，作为最外层中间件，保证 401 响应也带跨域头。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "tauri://localhost",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Shiyuan-Token"],
    allow_credentials=False,
)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _watcher(request: Request):
    return getattr(request.app.state, "watcher", None)


def _bound_project(request: Request) -> str | None:
    """外部令牌绑定的项目；本机令牌返回 None（不限制）。"""
    client = getattr(request.state, "external_client", None)
    return (client or {}).get("project_id")


def _project_closed_to_external(project_id: str) -> bool:
    """项目是否已不允许外部访问（不存在 / 已归档 / local_only）。"""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status, privacy_policy FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    return row["status"] != "active" or row["privacy_policy"] != "external_allowed"


def _scope_request(request: Request, conn: sqlite3.Connection,
                   project_id: str | None,
                   scope: str | None) -> tuple[str | None, str | None]:
    """外部令牌只能读取绑定项目；不能通过参数切到别的项目或全局。"""
    bound = _bound_project(request)
    if not bound:
        return project_id, scope
    if project_id and project_id != bound:
        raise HTTPException(status_code=403, detail="该令牌只能访问绑定的项目")
    if projects.is_global(conn, bound):
        return bound, "global"
    return bound, scope if scope in {"project", "expanded"} else "expanded"


def _require_project_access(request: Request, project_id: str) -> None:
    bound = _bound_project(request)
    if bound and project_id != bound:
        raise HTTPException(status_code=403, detail="该令牌只能访问绑定的项目")


class SourceCreate(BaseModel):
    root: str = Field(min_length=1)
    label: str | None = None
    excludes: list[str] = Field(default_factory=list)


class AskPayload(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    source_id: str | None = None
    ext: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=12)
    session_id: str | None = None
    project_id: str | None = None
    scope: str | None = None


class ProjectCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    folders: list[str] = Field(default_factory=list)
    retrieval_policy: str = "project_first"
    privacy_policy: str = "external_allowed"


class ProjectUpdatePayload(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    retrieval_policy: str | None = None
    privacy_policy: str | None = None


class ProjectSourcePayload(BaseModel):
    source_id: str | None = None
    root: str | None = None
    relation_type: str = "primary"
    path_prefix: str = ""


class ProjectAssetsPayload(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    membership_type: str = "linked"
    priority: int = 0


class AnswerSettingsPayload(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    clear_key: bool = False
    allow_cloud: bool | None = None
    top_k: int | None = Field(default=None, ge=1, le=12)


class QuerySettingsPayload(BaseModel):
    custom: list[list[str]] | None = None


class OcrSettingsPayload(BaseModel):
    policy: str | None = None


class IngestSettingsPayload(BaseModel):
    profile: str | None = None
    paused: bool | None = None
    auto_parse: bool | None = None


class InventoryPoliciesPayload(BaseModel):
    source_id: str | None = None
    policies: dict[str, str]


class ExternalClientPayload(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    preset: str
    transport: str = "mcp"
    project_id: str | None = None


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "db": str(db_path())}


@app.get("/api/sources")
def list_sources(request: Request,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    bound = _bound_project(request)
    allowed_sources = None
    if bound and not projects.is_global(conn, bound):
        allowed_sources = set(projects.source_ids(conn, bound))
    items = []
    for row in store.list_sources(conn):
        if allowed_sources is not None and row["source_id"] not in allowed_sources:
            continue
        items.append({
            "source_id": row["source_id"],
            "root_uri": row["root_uri"],
            "label": row["label"],
            "state": row["state"],
            "health": row["health"],
            "last_scan_at": row["last_scan_at"],
            "l0_completed_at": row["l0_completed_at"],
            "l1_completed_at": row["l1_completed_at"],
            "l2_completed_at": row["l2_completed_at"],
            "full_coverage_at": row["full_coverage_at"],
            "excludes": row["excludes"],
            "stats": store.source_stats(conn, row["source_id"]),
            "parse": indexer.parse_progress(conn, row["source_id"]),
            "scanning": scanner.is_running(row["source_id"]),
        })
    return {"items": items}


@app.post("/api/sources", status_code=201)
def create_source(payload: SourceCreate, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        plan = store.plan_source_add(conn, payload.root)
        busy = [item for item in plan["merged"] if scanner.is_running(item["source_id"])]
        if busy:
            raise HTTPException(status_code=409,
                                detail="目录内的数据源正在扫描，请稍后再添加上级目录")
        result = store.add_source_merged(conn, payload.root, payload.label, payload.excludes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = result["source"]
    manager = _watcher(request)
    if manager is not None:
        for item in result["merged"]:
            manager.unschedule(item["source_id"])
        manager.schedule(row["source_id"], row["root_uri"])
    return {
        "source_id": row["source_id"],
        "root_uri": row["root_uri"],
        "label": row["label"],
        "created": result["created"],
        "covered_by": result["covered_by"],
        "merged": result["merged"],
    }


@app.post("/api/sources/{source_id}/state")
def change_state(source_id: str, request: Request, state: str = Query(pattern="^(active|paused)$"),
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    row = store.set_source_state(conn, source_id, state)
    if row is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    manager = _watcher(request)
    if manager is not None:
        if state == "active":
            manager.schedule(source_id, row["root_uri"])
        else:
            manager.unschedule(source_id)
    return {"source_id": source_id, "state": row["state"]}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str, request: Request,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if scanner.is_running(source_id):
        raise HTTPException(status_code=409, detail="该数据源正在扫描，请稍后再撤销")
    manager = _watcher(request)
    if manager is not None:
        manager.unschedule(source_id)
    try:
        cleared = store.remove_source(conn, source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="数据源不存在") from exc
    return {"source_id": source_id, **cleared}


@app.post("/api/sources/{source_id}/scan", status_code=202)
def start_scan(source_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if not scanner.scan_in_background(source_id, force_parse=True):
        raise HTTPException(status_code=409, detail="该数据源正在扫描")
    return {"source_id": source_id, "state": "running"}


def _run_ocr(source_id: str) -> None:
    try:
        indexer.index_ocr_in_new_connection(source_id)
    finally:
        scanner.release_scan(source_id)


@app.post("/api/sources/{source_id}/ocr", status_code=202)
def start_ocr(source_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """按策略做深度解析：文本型 PDF 用 Windows OCR，整份扫描 PDF 用 PP-OCRv6 small。"""
    if store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    plan = ocr.plan_engines(conn)
    if not plan["windows"] and not plan["rapidocr"]:
        raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    if not scanner.request_scan(source_id):
        raise HTTPException(status_code=409, detail="该数据源正在同步，请稍后再试")
    threading.Thread(target=_run_ocr, args=(source_id,), name=f"ocr-{source_id}",
                     daemon=True).start()
    info = ocr.engine_info()
    return {"source_id": source_id, "state": "running",
            "pending": indexer.ocr_pending_count(conn, source_id),
            "engine": plan["policy"], "languages": info["languages"]}


@app.get("/api/search")
def run_search(request: Request, q: str = "", limit: int = 50, offset: int = 0,
               source_id: str | None = None, ext: str | None = None,
               project_id: str | None = None, scope: str | None = None,
               mtime_days: int | None = None,
               conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    project_id, scope = _scope_request(request, conn, project_id, scope)
    requested = projects.scope_for(conn, project_id, scope)
    allowed, excluded = projects.asset_filter(conn, project_id, requested)
    priority = (projects.asset_filter(conn, project_id, "project")[0]
                if project_id else None)
    mtime_after_ns = (
        time.time_ns() - max(1, min(mtime_days, 3650)) * 86_400_000_000_000
        if mtime_days else None
    )
    result = search.search(
        conn, q, limit=limit, offset=offset, source_id=source_id, ext=ext,
        allowed_assets=allowed, excluded_assets=excluded,
        priority_assets=priority, mtime_after_ns=mtime_after_ns)
    result["scope"] = {"requested": requested, "used": requested,
                       "expanded": False, "project_id": project_id}
    return result


@app.get("/api/freshness")
def run_freshness(request: Request, project_id: str | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """source 级索引新鲜度：文件改动、任务积压与失败。"""
    project_id, _ = _scope_request(request, conn, project_id, None)
    return search.index_freshness(conn, project_id=project_id)


@app.get("/api/stats")
def run_stats(request: Request,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return projects.stats_view(conn, _bound_project(request))


@app.get("/api/inventory/dry-run")
def inventory_dry_run(source_id: str | None = None, sample_limit: int = 5,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """盘点分类 dry-run：只读现有资产，不写分类或策略。"""
    if source_id and store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return inventory.dry_run(conn, source_id=source_id,
                             sample_limit=max(1, min(sample_limit, 20)))


@app.get("/api/inventory/file-types")
def inventory_file_types(source_id: str | None = None, sample_limit: int = 2,
                         conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """按扩展名聚合，供 File types 选择界面使用。"""
    if source_id and store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return inventory.file_types(conn, source_id=source_id,
                               sample_limit=max(1, min(sample_limit, 10)))


@app.get("/api/inventory/policies")
def get_inventory_policies(source_id: str | None = None,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if source_id and store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {
        "source_id": source_id,
        "policies": inventory.load_policies(conn, source_id),
        "confirmed": inventory.is_confirmed(conn, source_id) if source_id else False,
    }


@app.put("/api/inventory/policies")
def put_inventory_policies(payload: InventoryPoliciesPayload,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if payload.source_id and store.get_source(conn, payload.source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    try:
        normalized = inventory.save_policies(
            conn, payload.policies, source_id=payload.source_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    started = False
    if payload.source_id:
        started = scanner.scan_in_background(payload.source_id, force_parse=True)
    return {
        "source_id": payload.source_id,
        "policies": normalized,
        "confirmed": True,
        "started": started,
    }


# ---------------------------------------------------------------- 项目知识空间

@app.get("/api/projects")
def list_projects(include_archived: bool = False, external_only: bool = False,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    items = projects.list_projects(
        conn, include_archived=include_archived, external_only=external_only)
    return {"items": items, "total": len(items)}


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreatePayload, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        project = projects.create_project(
            conn, payload.name, description=payload.description,
            retrieval_policy=payload.retrieval_policy,
            privacy_policy=payload.privacy_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project_id = project["project_id"]
    linked: list[dict] = []
    for folder in payload.folders:
        try:
            result = projects.add_source_path(conn, project_id, folder)
        except ValueError as exc:
            linked.append({"root": folder, "error": str(exc)})
            continue
        linked.append(result)
        # 复用已授权数据源时不需要重新扫描：现有索引与项目关系直接可用。
        if result.get("created"):
            scanner.scan_in_background(result["source_id"])
        manager = _watcher(request)
        if manager is not None:
            manager.schedule(result["source_id"], result["source"]["root_uri"])
    return {"project": projects.get_project(conn, project_id), "sources": linked}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str,
                conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    project = projects.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdatePayload,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        project = projects.update_project(
            conn, project_id, name=payload.name, description=payload.description,
            status=payload.status, retrieval_policy=payload.retrieval_policy,
            privacy_policy=payload.privacy_policy)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"project": project}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return projects.delete_project(conn, project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/sources", status_code=201)
def add_project_source(project_id: str, payload: ProjectSourcePayload,
                       request: Request,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        if payload.root:
            result = projects.add_source_path(
                conn, project_id, payload.root, payload.relation_type)
        elif payload.source_id:
            result = projects.add_source(
                conn, project_id, payload.source_id, payload.relation_type,
                payload.path_prefix)
        else:
            raise HTTPException(status_code=400, detail="需要提供 root 或 source_id")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("created"):
        scanner.scan_in_background(result["source_id"])
    manager = _watcher(request)
    if manager is not None:
        source = store.get_source(conn, result["source_id"])
        if source is not None:
            manager.schedule(source["source_id"], source["root_uri"])
    return result


@app.delete("/api/projects/{project_id}/sources/{source_id}")
def remove_project_source(project_id: str, source_id: str,
                          conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return projects.remove_source(conn, project_id, source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc


@app.get("/api/projects/{project_id}/assets")
def list_project_assets(project_id: str, membership: str | None = None,
                        q: str | None = None, limit: int = 100, offset: int = 0,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if projects.get_project(conn, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return projects.list_assets(
        conn, project_id, membership=membership, query=q,
        limit=limit, offset=offset)


@app.post("/api/projects/{project_id}/assets")
def add_project_assets(project_id: str, payload: ProjectAssetsPayload,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        if payload.paths and not payload.asset_ids:
            return projects.add_assets_by_paths(
                conn, project_id, payload.paths, payload.membership_type,
                payload.priority)
        return projects.set_membership(
            conn, project_id, payload.asset_ids, payload.membership_type,
            payload.priority)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}/assets/{asset_id}")
def remove_project_asset(project_id: str, asset_id: str,
                         conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return projects.remove_asset(conn, project_id, asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc


@app.get("/api/projects/{project_id}/knowledge")
def project_knowledge(project_id: str, limit: int = 20,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return projects.project_knowledge(
            conn, project_id, limit=max(1, min(limit, 100)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc


@app.get("/api/assets")
def list_assets(project_id: str | None = None, unassigned: bool = False,
                q: str | None = None, limit: int = 100, offset: int = 0,
                conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if project_id:
        if projects.get_project(conn, project_id) is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return projects.list_assets(conn, project_id, query=q,
                                    limit=limit, offset=offset)
    return projects.list_global_assets(
        conn, unassigned=unassigned, query=q, limit=limit, offset=offset)


@app.get("/api/external/clients")
def list_external_clients(project_id: str | None = None,
                          conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"items": external.list_clients(conn, project_id=project_id)}


@app.post("/api/external/clients", status_code=201)
def create_external_client(payload: ExternalClientPayload,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        client = external.create_client(
            conn, preset=payload.preset, transport=payload.transport,
            name=payload.name, project_id=payload.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"client": client}


@app.delete("/api/external/clients/{client_id}")
def delete_external_client(client_id: str,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return external.delete_client(conn, client_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="外部连接不存在") from exc


@app.get("/api/external/clients/{client_id}/config")
def get_external_client_config(client_id: str,
                               conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return external.client_config(conn, client_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="外部连接不存在") from exc


@app.post("/api/external/clients/{client_id}/test")
def test_external_client(client_id: str,
                         conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return external.test_client(conn, client_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="外部连接不存在") from exc


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.post("/api/ask/stream")
def run_ask_stream(payload: AskPayload, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)) -> StreamingResponse:
    """流式问答：先回状态，再逐段推送正文，最后给完整结果。"""
    bound = _bound_project(request)
    session_id = None if bound else payload.session_id
    project_id, scope = _scope_request(request, conn, payload.project_id, payload.scope)
    if session_id and chat.get_session(conn, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    def generate():
        events: queue.Queue = queue.Queue()

        def worker() -> None:
            worker_conn = None
            try:
                worker_conn = db.connect()
                history = (chat.recent_questions(worker_conn, session_id)
                           if session_id else [])
                anchors = (chat.recent_answer_sources(worker_conn, session_id)
                           if session_id else [])
                previous_answer = (chat.recent_answer(worker_conn, session_id)
                                   if session_id else None)
                result = answer.ask(
                    worker_conn, payload.question, source_id=payload.source_id,
                    ext=payload.ext, top_k=payload.top_k, history=history,
                    anchor_sources=anchors, previous_answer=previous_answer,
                    project_id=project_id, scope=scope,
                    on_delta=lambda text: events.put({"type": "delta", "text": text}))
                if session_id:
                    chat.append_exchange(
                        worker_conn, session_id, payload.question, result)
                    result["session_id"] = session_id
                events.put({"type": "done", "result": result})
            except Exception as exc:  # noqa: BLE001 - 错误要作为事件发给前端
                events.put({"type": "error", "message": str(exc)[:500]})
            finally:
                if worker_conn is not None:
                    worker_conn.close()

        threading.Thread(target=worker, daemon=True, name="answer-stream").start()
        yield _sse({"type": "status", "stage": "retrieval",
                    "message": "正在检索并筛选证据…"})
        generating = False
        while True:
            item = events.get()
            if item.get("type") == "delta" and not generating:
                generating = True
                yield _sse({"type": "status", "stage": "generation",
                            "message": "正在生成回答…"})
            yield _sse(item)
            if item.get("type") in {"done", "error"}:
                break

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


@app.post("/api/ask")
def run_ask(payload: AskPayload, request: Request,
            conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    bound = _bound_project(request)
    session_id = None if bound else payload.session_id
    project_id, scope = _scope_request(request, conn, payload.project_id, payload.scope)
    if session_id and chat.get_session(conn, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        history = chat.recent_questions(conn, session_id) if session_id else []
        anchor_sources = (chat.recent_answer_sources(conn, session_id)
                          if session_id else [])
        previous_answer = (chat.recent_answer(conn, session_id)
                           if session_id else None)
        result = answer.ask(conn, payload.question, source_id=payload.source_id,
                            ext=payload.ext, top_k=payload.top_k, history=history,
                            anchor_sources=anchor_sources,
                            previous_answer=previous_answer,
                            project_id=project_id, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except answer.AnswerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if session_id:
        chat.append_exchange(conn, session_id, payload.question, result)
        result["session_id"] = session_id
    return result


class ChatSessionPayload(BaseModel):
    title: str | None = None


class ChatRenamePayload(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@app.get("/api/chat/sessions")
def list_chat_sessions(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"items": chat.list_sessions(conn)}


@app.post("/api/chat/sessions", status_code=201)
def create_chat_session(payload: ChatSessionPayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return chat.create_session(conn, payload.title)


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str,
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    session = chat.get_session(conn, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session": session, "messages": chat.list_messages(conn, session_id)}


@app.patch("/api/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, payload: ChatRenamePayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        session = chat.rename_session(conn, session_id, payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.delete("/api/chat/sessions/{session_id}")
def delete_chat_session(session_id: str,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    if not chat.delete_session(conn, session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "deleted": True}


@app.get("/api/settings/answer")
def get_answer_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return answer.public_settings(conn)


@app.put("/api/settings/answer")
def put_answer_settings(payload: AnswerSettingsPayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return answer.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/embedding")
def get_embedding_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return embeddings.public_settings(conn)


@app.put("/api/settings/embedding")
def put_embedding_settings(payload: AnswerSettingsPayload,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return embeddings.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/query")
def get_query_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return query.public_settings(conn)


@app.put("/api/settings/query")
def put_query_settings(payload: QuerySettingsPayload,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return query.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/ocr")
def get_ocr_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return ocr.public_settings(conn)


@app.put("/api/settings/ocr")
def put_ocr_settings(payload: OcrSettingsPayload,
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        return ocr.update_settings(conn, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/settings/ingest")
def get_ingest_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return ingest.public_settings(conn)


@app.put("/api/settings/ingest")
def put_ingest_settings(payload: IngestSettingsPayload,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    try:
        result = ingest.update(
            conn, profile=payload.profile, paused=payload.paused,
            auto_parse=payload.auto_parse,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("paused") is False:
        scanner.resume_incomplete_sources()
    return ingest.public_settings(conn)


@app.get("/api/updates/overview")
def updates_overview(request: Request,
                     conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    manager = getattr(request.app.state, "watcher", None)
    return updates.overview(
        conn, watcher_running=manager is not None,
        watch_enabled=config.watch_enabled(),
    )


@app.post("/api/updates/check")
def updates_check(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """手动触发一次全量检查；即使关闭自动解析，也立即执行一轮。"""
    started = [
        row["source_id"] for row in store.list_sources(conn)
        if row["state"] == "active"
        and scanner.scan_in_background(row["source_id"], force_parse=True)
    ]
    return {"started": started}


@app.post("/api/families/rebuild")
def rebuild_document_families() -> dict:
    started = families.rebuild_async()
    return {"state": "running" if started else "already_running"}


@app.get("/api/families")
def list_document_families(limit: int = 50,
                           conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT f.family_id, f.title, f.confidence,"
        " COUNT(m.asset_id) AS size"
        " FROM document_families f"
        " JOIN document_family_members m ON m.family_id = f.family_id"
        " GROUP BY f.family_id ORDER BY size DESC, f.title LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        members = conn.execute(
            "SELECT m.asset_id, m.version_label, m.similarity, l.path"
            " FROM document_family_members m"
            " LEFT JOIN asset_locations l ON l.asset_id = m.asset_id AND l.location_id = ("
            "  SELECT l2.location_id FROM asset_locations l2"
            "  WHERE l2.asset_id = m.asset_id AND l2.availability = 'AVAILABLE'"
            "  ORDER BY l2.path LIMIT 1)"
            " WHERE m.family_id = ? ORDER BY l.path",
            (row["family_id"],),
        ).fetchall()
        items.append({
            "family_id": row["family_id"],
            "title": row["title"],
            "confidence": row["confidence"],
            "size": row["size"],
            "members": [dict(member) for member in members],
        })
    return {"items": items}


@app.get("/api/families/{family_id}")
def get_document_family(family_id: str, limit: int = 200,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    row = conn.execute(
        "SELECT family_id, title, confidence, rules_version, created_at, updated_at"
        " FROM document_families WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文档家族不存在")
    return {
        "family": dict(row),
        "members": families.family_members(conn, family_id),
        "content_groups": families.content_groups(
            conn, family_id, max(1, min(limit, 1000))
        ),
    }


@app.get("/api/families/{family_id}/members")
def get_document_family_versions(family_id: str,
                                 conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """只返回家族成员，供引用里的“N 个关联版本”展开查看。"""
    row = conn.execute(
        "SELECT family_id, title, confidence FROM document_families WHERE family_id = ?",
        (family_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文档家族不存在")
    return {
        "family_id": row["family_id"],
        "title": row["title"],
        "confidence": row["confidence"],
        "members": families.family_members(conn, family_id),
    }


@app.get("/api/families/by-asset/{asset_id}/members")
def get_document_family_versions_by_asset(asset_id: str,
                                          conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """按文件解析当前家族，兼容家族重建后旧引用里的 family_id 失效。"""
    row = conn.execute(
        "SELECT f.family_id, f.title, f.confidence"
        " FROM document_family_members m"
        " JOIN document_families f ON f.family_id = m.family_id"
        " WHERE m.asset_id = ?",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="该文件目前不属于任何文档家族")
    return {
        "family_id": row["family_id"],
        "title": row["title"],
        "confidence": row["confidence"],
        "members": families.family_members(conn, row["family_id"]),
    }


@app.get("/api/families/{family_id}/diff")
def diff_document_family(family_id: str,
                         left_asset_id: str = Query(min_length=1),
                         right_asset_id: str = Query(min_length=1),
                         limit: int = 200,
                         conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """对比家族内两个版本：共享、新增、删除、近似修改；不判断版本优劣。"""
    try:
        return families.diff_versions(
            conn, family_id, left_asset_id, right_asset_id, limit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/events")
def list_events(limit: int = 20, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT event_id, type, source_id, detail, outcome, timestamp FROM maintenance_events"
        " ORDER BY event_id DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/jobs")
def list_index_jobs(status: str | None = None, stage: str | None = None, limit: int = 50,
                    conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return {"items": indexer.list_jobs(conn, status=status, stage=stage, limit=limit)}


@app.get("/api/sources/{source_id}/parse-results")
def source_parse_results(source_id: str, status: str | None = None,
                         limit: int = 100, offset: int = 0,
                         conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """按数据源查看逐文件解析结果：成功、失败、无文本、待解析、仅登记、不支持。"""
    if store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    allowed = {"all", "ok", "failed", "empty", "pending", "locked",
               "unsupported", "metadata_only"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="未知的解析状态")
    return indexer.parse_results(
        conn, source_id, status=status,
        limit=max(1, min(limit, 500)), offset=max(0, offset),
    )


@app.get("/api/document/text")
def document_text(location_id: str = Query(min_length=1),
                  max_chars: int = indexer.PREVIEW_MAX_CHARS,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """解析结果预览：返回该文件已解析的正文（按页 / 工作表拼接，去掉分块重叠）。"""
    try:
        return indexer.location_text(
            conn, location_id, max(1000, min(max_chars, 1_000_000)),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry")
def retry_index_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """单文件重试：只重跑这一条任务，不影响同数据源的其他文件。"""
    job = indexer.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["stage"] == "l2":
        plan = ocr.plan_engines(conn)
        if not plan["windows"] and not plan["rapidocr"]:
            raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    if not scanner.request_scan(job["source_id"]):
        raise HTTPException(status_code=409, detail="该数据源正在同步，请稍后再试")
    try:
        return indexer.retry_job(conn, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ocr.OcrUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        scanner.release_scan(job["source_id"])


def _start_maintenance(kind: str, source_id: str | None, conn: sqlite3.Connection) -> dict:
    if source_id and store.get_source(conn, source_id) is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if not maintenance.start(kind, source_id, db_path()):
        raise HTTPException(status_code=409, detail="已有维护任务在运行")
    return {"kind": kind, "source_id": source_id, "state": "running"}


@app.post("/api/maintenance/reindex", status_code=202)
def maintenance_reindex(source_id: str | None = None,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清空正文产物后按当前文件重建（OCR 产物保留）。"""
    return _start_maintenance("reindex", source_id, conn)


@app.post("/api/maintenance/retry", status_code=202)
def maintenance_retry(source_id: str | None = None,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """把失败任务的尝试次数清零并重新解析（正文与 OCR）。"""
    return _start_maintenance("retry", source_id, conn)


@app.post("/api/maintenance/ocr", status_code=202)
def maintenance_ocr(source_id: str | None = None,
                    conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清空 OCR 产物后对图片与无文本层 PDF 重跑。"""
    plan = ocr.plan_engines(conn)
    if not plan["windows"] and not plan["rapidocr"]:
        raise HTTPException(status_code=400, detail="没有可用的 OCR 引擎")
    return _start_maintenance("ocr", source_id, conn)


@app.post("/api/maintenance/cleanup", status_code=202)
def maintenance_cleanup(source_id: str | None = None,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """清理非当前版本的缓存与孤立块，不动原文件。"""
    return _start_maintenance("cleanup", source_id, conn)


@app.post("/api/maintenance/embed", status_code=202)
def maintenance_embed(source_id: str | None = None,
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """为尚未生成向量的块补建向量索引。"""
    settings = embeddings.load_settings(conn)
    if not embeddings.enabled(settings):
        raise HTTPException(status_code=400, detail="语义检索未启用或缺少配置")
    return _start_maintenance("embed", source_id, conn)


@app.post("/api/maintenance/knowledge", status_code=202)
def maintenance_knowledge(source_id: str | None = None,
                          conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """立即跑一轮衍生知识（摘要/实体/FAQ）生成队列。"""
    return _start_maintenance("knowledge", source_id, conn)
