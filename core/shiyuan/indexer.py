"""L1 索引流水线：把当前内容版本解析成 chunks 并写入 FTS。

缓存键是 version_id + parser_signature：内容没变就复用产物，解析器升级或
文件变化才重做。解析结果按版本清理，旧版本的块不会继续参与检索。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import budget, db, extractors, ingest, inventory, knowledge, ocr, store

# 解析器与切块策略的版本签名；改动提取逻辑或分块参数时必须同步升级。
PARSER_SIGNATURE = (
    "l1.v5:pdf=pymupdf+fallback,docx=python-docx+header+comment,"
    "pptx=python-pptx,xlsx=openpyxl+formula,office=libreoffice,"
    "archive=zip/7z/rar,email=eml/msg,cad=dxf/dwg,av=metadata,"
    "table=markdown,chunk=800/100"
)
OCR_SIGNATURES = {
    "windows": f"l2.v2:windows-ocr:maxpages={ocr.MAX_PDF_PAGES}:mixed=pages",
    "rapidocr": f"l2.v2:rapidocr-v6-small:maxpages={ocr.MAX_PDF_PAGES}:mixed=pages",
}
# PPTX 内嵌图片单独签名，避免升级后把已有 PDF / 图片 OCR 结果全部作废。
PPTX_OCR_SIGNATURES = {
    "windows": "l2.v1:windows-ocr:pptx=images",
    "rapidocr": "l2.v1:rapidocr-v6-small:pptx=images",
}
OCR_SIGNATURE = OCR_SIGNATURES["windows"]

OCR_BATCH = 50
OCR_RUN_LIMIT = 200
MAX_ATTEMPTS = 3

CHUNK_CHARS = 800
CHUNK_OVERLAP = 100
# 单块上限：超长行（md 表格行可上千字符）整行入块，超过该上限才硬切。
MAX_CHUNK_CHARS = 4000


def _is_file_lock_error(exc: BaseException) -> bool:
    """识别 Windows 文件被其他进程占用/拒绝访问；这类文件本轮直接跳过。"""
    if getattr(exc, "winerror", None) in (5, 32, 33):
        return True
    if getattr(exc, "errno", None) == 13:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "being used by another process",
        "sharing violation",
        "lock violation",
        "另一个程序正在使用",
        "文件正被占用",
    ))


@dataclass
class Chunk:
    text: str
    page: int | None
    sheet: str | None
    start: int
    end: int
    boxes: list[list[int]] | None = None


def chunk_sections(sections) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        text = section.text
        line_boxes: list[tuple[int, int, list[int]]] = []
        boxes = getattr(section, "boxes", None)
        if boxes:
            offset = 0
            for index, line in enumerate(text.split("\n")):
                if index < len(boxes):
                    line_boxes.append((offset, offset + len(line), boxes[index]))
                offset += len(line) + 1
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):
                cut = text.rfind("\n", start + CHUNK_CHARS // 2, end)
                if cut > 0:
                    end = cut + 1
                elif "\n" not in text[start + CHUNK_CHARS // 2:end]:
                    # 超长行（如 md 表格行）中间没有换行：把整行留在一块里，
                    # 避免 FTS/读取时行被拦腰切断。行本身超过上限才硬切。
                    line_end = text.find("\n", end)
                    if 0 <= line_end and line_end - start <= MAX_CHUNK_CHARS:
                        end = line_end + 1
            piece = text[start:end]
            if piece.strip():
                keep = [box for (box_start, box_end, box) in line_boxes
                        if box_end > start and box_start < end]
                chunks.append(Chunk(piece, section.page, section.sheet, start, end,
                                    keep or None))
            if end >= len(text):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _purge_old_versions(conn: sqlite3.Connection, asset_id: str, keep_version: str) -> None:
    rows = conn.execute(
        "SELECT version_id FROM content_versions WHERE asset_id = ? AND version_id != ?",
        (asset_id, keep_version),
    ).fetchall()
    for row in rows:
        version_id = row["version_id"]
        conn.execute("DELETE FROM chunk_fts WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM chunks WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM parse_artifacts WHERE version_id = ?", (version_id,))


def _write_artifact(conn: sqlite3.Connection, row: sqlite3.Row, *, state: str, quality: str,
                    sections, message: str | None, chunks: list[Chunk],
                    signature: str = PARSER_SIGNATURE,
                    page_count: int = 0, text_pages: int = 0,
                    image_count: int = 0) -> str:
    artifact_id = store.new_id("art")
    char_count = sum(len(section.text) for section in sections)
    conn.execute(
        "INSERT INTO parse_artifacts (artifact_id, version_id, parser_signature, state, quality,"
        " char_count, page_count, text_pages, image_count, message, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact_id, row["version_id"], signature, state, quality, char_count,
         page_count, text_pages, image_count, message, store.now_iso()),
    )
    for ordinal, chunk in enumerate(chunks):
        chunk_id = store.new_id("chk")
        bbox = json.dumps(chunk.boxes, ensure_ascii=False) if chunk.boxes else None
        cursor = conn.execute(
            "INSERT INTO chunks (chunk_id, artifact_id, version_id, asset_id, ordinal, text,"
            " page, sheet, bbox, char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chunk_id, artifact_id, row["version_id"], row["asset_id"], ordinal, chunk.text,
             chunk.page, chunk.sheet, bbox, chunk.start, chunk.end),
        )
        conn.execute(
            "INSERT INTO chunk_fts (rowid, text, chunk_id, version_id) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, chunk.text, chunk_id, row["version_id"]),
        )
    return state


def _job_update(conn: sqlite3.Connection, row: sqlite3.Row, stage: str, status: str,
                error: str | None = None, count_attempt: bool = False) -> None:
    """记录/更新任务账本；attempts 只在实际开始解析时加一。"""
    conn.execute(
        "INSERT INTO index_jobs (job_id, source_id, version_id, asset_id, stage, status,"
        " attempts, error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(version_id, stage) DO UPDATE SET"
        " status = excluded.status,"
        " attempts = index_jobs.attempts + excluded.attempts,"
        " error = excluded.error, updated_at = excluded.updated_at",
        (store.new_id("job"), row["source_id"], row["version_id"], row["asset_id"], stage,
         status, 1 if count_attempt else 0, error, store.now_iso()),
    )


def _requeue_failed(conn: sqlite3.Connection, stage: str, signature: str,
                    source_id: str | None = None) -> int:
    """清理未达重试上限的失败产物，让它们在本轮重新排队。

    文件被占用（locked）不是解析失败，下一轮无条件重试；失败仍受次数上限保护。
    """
    sql = [
        "SELECT a.artifact_id FROM parse_artifacts a",
        "JOIN content_versions v ON v.version_id = a.version_id",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "LEFT JOIN index_jobs j ON j.version_id = a.version_id AND j.stage = ?",
        "WHERE a.parser_signature = ? AND v.is_current = 1",
        "AND l.availability = 'AVAILABLE'",
        "AND (a.state = 'locked' OR (a.state = 'failed' AND COALESCE(j.attempts, 0) < ?))",
    ]
    params: list = [stage, signature, MAX_ATTEMPTS]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    artifacts = [row["artifact_id"] for row in conn.execute(" ".join(sql), params)]
    for artifact_id in artifacts:
        conn.execute(
            "DELETE FROM chunk_fts WHERE rowid IN"
            " (SELECT rowid FROM chunks WHERE artifact_id = ?)", (artifact_id,))
        conn.execute("DELETE FROM chunks WHERE artifact_id = ?", (artifact_id,))
        conn.execute("DELETE FROM parse_artifacts WHERE artifact_id = ?", (artifact_id,))
    return len(artifacts)


def index_version(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """解析单个内容版本，返回 ok / empty / failed / skipped。"""
    existing = conn.execute(
        "SELECT state FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], PARSER_SIGNATURE),
    ).fetchone()
    if existing is not None:
        return existing["state"]
    if not extractors.is_supported(row["ext"]):
        return "skipped"

    _job_update(conn, row, "l1", "running", count_attempt=True)
    # 先提交“运行中”，解析期间不持有 SQLite 写锁；否则 LibreOffice 等
    # 长任务会阻塞其他数据源的扫描与设置保存。
    conn.commit()
    sections: list[extractors.Section] = []
    chunks: list[Chunk] = []
    page_count = 0
    text_pages = 0
    image_count = 0
    try:
        sections = extractors.extract(row["path"], row["ext"])
        quality = extractors.quality_of(sections)
        if row["ext"] == "pdf":
            page_count = extractors.pdf_page_count(row["path"])
            text_pages = len({section.page for section in sections if section.page})
        elif row["ext"] == "pptx":
            image_count = extractors.pptx_image_count(row["path"])
        if quality == "empty":
            state, message = "empty", "未提取到文本层（可能是扫描件或空文档）"
        else:
            state, message = "ok", None
            chunks = chunk_sections(sections)
    except extractors.UnsupportedType:
        return "skipped"
    except Exception as exc:
        if _is_file_lock_error(exc):
            state, quality, message = "locked", "empty", "文件被其他程序占用，本轮已跳过"
        else:
            state, quality, message = "failed", "empty", str(exc)[:400]

    _purge_old_versions(conn, row["asset_id"], row["version_id"])
    _write_artifact(conn, row, state=state, quality=quality, sections=sections,
                    message=message, chunks=chunks,
                    page_count=page_count, text_pages=text_pages,
                    image_count=image_count)
    _job_update(conn, row, "l1", "done" if state != "failed" else "failed", error=message)
    if state == "ok":
        # 解析完成即入队：文档级摘要（OCR 阶段完成时会再次重置任务，
        # 用合并后的完整内容重新生成）。
        knowledge.enqueue(conn, "summary", "document", row["asset_id"])
    return state


def pending_rows(conn: sqlite3.Connection, source_id: str | None = None,
                 limit: int = 500,
                 allowed_exts: set[str] | None = None) -> list[sqlite3.Row]:
    exts = sorted(extractors.SUPPORTED_EXTS)
    placeholders = ",".join("?" for _ in exts)
    sql = [
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id",
        "FROM content_versions v",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        f"AND l.ext IN ({placeholders})",
        "AND NOT EXISTS (SELECT 1 FROM parse_artifacts a"
        " WHERE a.version_id = v.version_id AND a.parser_signature = ?)",
    ]
    params: list = [*exts, PARSER_SIGNATURE]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    if allowed_exts is not None:
        if not allowed_exts:
            sql.append("AND 1 = 0")
        else:
            marks = ",".join("?" for _ in allowed_exts)
            sql.append(f"AND l.ext IN ({marks})")
            params.extend(sorted(allowed_exts))
    sql.append("ORDER BY v.observed_at DESC")
    sql.append(f"LIMIT {limit}")
    return conn.execute(" ".join(sql), params).fetchall()


def pending_count(conn: sqlite3.Connection, source_id: str | None = None,
                  allowed_exts: set[str] | None = None) -> int:
    exts = sorted(extractors.SUPPORTED_EXTS)
    placeholders = ",".join("?" for _ in exts)
    sql = [
        "SELECT COUNT(*) FROM content_versions v",
        "JOIN asset_locations l ON l.asset_id = v.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        f"AND l.ext IN ({placeholders})",
        "AND NOT EXISTS (SELECT 1 FROM parse_artifacts a"
        " WHERE a.version_id = v.version_id AND a.parser_signature = ?)",
    ]
    params: list = [*exts, PARSER_SIGNATURE]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    if allowed_exts is not None:
        if not allowed_exts:
            sql.append("AND 1 = 0")
        else:
            marks = ",".join("?" for _ in allowed_exts)
            sql.append(f"AND l.ext IN ({marks})")
            params.extend(sorted(allowed_exts))
    return conn.execute(" ".join(sql), params).fetchone()[0]


def index_pending(conn: sqlite3.Connection, source_id: str | None = None,
                  batch: int = 200, *, ignore_budget: bool = False,
                  pause_check=None, row_delay: float = 0.0,
                  allowed_exts: set[str] | None = None) -> dict:
    """按批处理待解析版本。

    首次建库使用 ignore_budget=True，持续处理到队列为空；日常维护仍按每日
    预算限速。pause_check / row_delay 用于暂停与降低资源占用。
    """
    counters = {"indexed": 0, "empty": 0, "failed": 0, "locked": 0, "skipped": 0,
                "budget_exhausted": False, "paused": False, "remaining": 0}
    failures: list[str] = []
    _requeue_failed(conn, "l1", PARSER_SIGNATURE, source_id)
    conn.commit()

    allow = 10 ** 9 if ignore_budget else budget.remaining(conn, "l1")
    processed = 0
    paused = False
    while processed < allow and not paused:
        rows = pending_rows(conn, source_id, min(batch, allow - processed),
                            allowed_exts=allowed_exts)
        if not rows:
            break
        for row in rows:
            state = index_version(conn, row)
            counters[{"ok": "indexed"}.get(state, state)] += 1
            processed += 1
            if state == "failed":
                failures.append(f"{row['path']}: 解析失败")
            # 逐文件提交：应用中途退出时最多重做当前文件，断点更细。
            conn.commit()
            if pause_check is not None and pause_check():
                paused = True
                break
            if row_delay > 0:
                time.sleep(row_delay)
        conn.commit()
    if not ignore_budget:
        # 用户显式触发的完整解析不占每日后台预算，否则大库会被 500/日卡住。
        budget.add(conn, "l1", processed)
    if not ignore_budget and allow == 0 and pending_rows(
        conn, source_id, 1, allowed_exts=allowed_exts
    ):
        counters["budget_exhausted"] = True
    counters["paused"] = paused
    counters["remaining"] = pending_count(conn, source_id, allowed_exts=allowed_exts)

    if any(counters[key] for key in ("indexed", "empty", "failed")) or counters["budget_exhausted"]:
        store.log_event(
            conn, "index_completed", source_id,
            detail=json.dumps(counters, ensure_ascii=False), outcome="ok",
        )
        if failures:
            store.log_event(conn, "index_failed", source_id,
                            detail="; ".join(failures[:5]), outcome="partial")
        conn.commit()
    return counters


def index_incremental_in_new_connection(source_id: str | None = None,
                                        store_path: str | None = None) -> dict:
    """监听增量：只处理新版本（新增或修改过的文件），并遵守自动解析与策略。

    `pending_rows` 以 version_id + parser_signature 去重，未变化的文件已有
    解析产物，不会再次进入队列。自动解析关闭、全局暂停或策略未确认时直接跳过。
    """
    conn = db.connect(store_path)
    try:
        settings = ingest.load(conn)
        paused = ingest.paused(conn)
        if paused or not settings.get("auto_parse", True):
            return {"skipped": True, "reason": "paused" if paused else "auto_parse_off"}
        if source_id:
            source = store.get_source(conn, source_id)
            if (source is not None and source["full_coverage_at"] is None
                    and not inventory.is_confirmed(conn, source_id)):
                return {"skipped": True, "reason": "awaiting_policy"}
        allowed = inventory.allowed_exts(conn, source_id) if source_id else None
        delay = ingest.delay_for(settings["profile"])
        batch = ingest.batch_for(settings["profile"])
        l1 = index_pending(
            conn, source_id, batch=batch,
            pause_check=lambda: ingest.paused(conn),
            row_delay=delay, allowed_exts=allowed,
        )
        l2 = index_ocr_pending(
            conn, source_id,
            pause_check=lambda: ingest.paused(conn),
            row_delay=delay, allowed_exts=allowed,
        )
        return {"skipped": False, "l1": l1, "l2": l2}
    finally:
        conn.close()


# ---------------------------------------------------------------- L2 OCR

def _route_engine(ext: str, text_pages: int, plan: dict) -> str | None:
    """按策略选择 OCR 引擎；返回 None 表示当前文件没有可用引擎。"""
    if plan["policy"] == "windows":
        return "windows" if plan["windows"] else None
    if plan["policy"] == "rapidocr":
        if plan["rapidocr"]:
            return "rapidocr"
        return "windows" if plan["windows"] else None
    # auto：有 Windows 时，整份扫描 PDF 走 PP-OCRv6，其余走 Windows；
    # 没有 Windows 时全部交给随包的 PP-OCRv6 兜底。
    if plan["windows"]:
        if ext == "pdf" and (text_pages or 0) == 0 and plan["rapidocr"]:
            return "rapidocr"
        return "windows"
    return "rapidocr" if plan["rapidocr"] else None


def _ocr_plan_sql(conn: sqlite3.Connection) -> tuple[dict, str | None, str | None, str, list]:
    """返回（策略, 引擎表达式, 签名表达式, 候选文件条件, 参数）。

    候选条件只覆盖“OCR 有意义的文件”：图片，或文本层为空/质量低/有缺页的 PDF。
    签名表达式与引擎表达式使用同一套路由，保证待处理判断与执行引擎一致。
    """
    plan = ocr.plan_engines(conn)
    images = sorted(ocr.IMAGE_EXTS)
    placeholders = ",".join("?" for _ in images)
    scanned = "(l.ext = 'pdf' AND COALESCE(l1.text_pages, 0) = 0)"
    pdf_gap = ("(l.ext = 'pdf' AND (l1.state IN ('empty', 'ok')"
               " AND (l1.state = 'empty' OR l1.quality = 'low'"
               "  OR l1.page_count > l1.text_pages)))")
    image_part = f"l.ext IN ({placeholders})"
    pptx_images = "(l.ext = 'pptx' AND COALESCE(l1.image_count, 0) > 0)"
    params: list[str] = []

    if plan["policy"] == "windows":
        if not plan["windows"]:
            return plan, None, None, "0", []
        engine_expr = "'windows'"
        sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                    f" ELSE '{OCR_SIGNATURES['windows']}' END")
    elif plan["policy"] == "rapidocr":
        if plan["rapidocr"]:
            engine_expr = "'rapidocr'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['rapidocr']}' END")
        elif plan["windows"]:
            engine_expr = "'windows'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        else:
            return plan, None, None, "0", []
    else:  # auto
        if plan["windows"] and plan["rapidocr"]:
            engine_expr = f"CASE WHEN {scanned} THEN 'rapidocr' ELSE 'windows' END"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" WHEN {scanned} THEN '{OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        elif plan["windows"]:
            engine_expr = "'windows'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['windows']}'"
                        f" ELSE '{OCR_SIGNATURES['windows']}' END")
        elif plan["rapidocr"]:
            # 没有 Windows OCR 时，auto 用随包的 RapidOCR 兜底图片与混合 PDF。
            engine_expr = "'rapidocr'"
            sig_expr = (f"CASE WHEN l.ext = 'pptx' THEN '{PPTX_OCR_SIGNATURES['rapidocr']}'"
                        f" ELSE '{OCR_SIGNATURES['rapidocr']}' END")
        else:
            return plan, None, None, "0", []

    if not (plan["windows"] or plan["rapidocr"]):
        return plan, None, None, "0", []
    eligible = f"({image_part} OR {pdf_gap} OR {pptx_images})"
    return plan, engine_expr, sig_expr, eligible, [*images]


def _ocr_query(conn: sqlite3.Connection, source_id: str | None,
               select: str, tail: str,
               allowed_exts: set[str] | None = None) -> tuple[list[sqlite3.Row], dict]:
    plan, engine_expr, sig_expr, eligible, image_params = _ocr_plan_sql(conn)
    if engine_expr is None or sig_expr is None:
        return [], plan
    where = [
        "v.is_current = 1",
        "l.availability = 'AVAILABLE'",
        "s.state = 'active'",
        eligible,
    ]
    params: list = [PARSER_SIGNATURE, *image_params]
    if source_id:
        where.append("l.source_id = ?")
        params.append(source_id)
    if allowed_exts is not None:
        if not allowed_exts:
            where.append("1 = 0")
        else:
            marks = ",".join("?" for _ in allowed_exts)
            where.append(f"l.ext IN ({marks})")
            params.extend(sorted(allowed_exts))
    sql = (
        f"{select} FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
        "  AND l1.parser_signature = ?"
        f" WHERE {' AND '.join(where)}"
        f" AND NOT EXISTS (SELECT 1 FROM parse_artifacts b"
        f"   WHERE b.version_id = v.version_id AND b.parser_signature = {sig_expr})"
        f" {tail}"
    )
    return conn.execute(sql, params).fetchall(), plan


def ocr_candidate_rows(conn: sqlite3.Connection, source_id: str | None = None,
                       limit: int = OCR_BATCH,
                       allowed_exts: set[str] | None = None) -> list[sqlite3.Row]:
    rows, _ = _ocr_query(
        conn, source_id,
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id",
        f"ORDER BY v.observed_at DESC LIMIT {limit}",
        allowed_exts=allowed_exts,
    )
    if not rows:
        return []
    # 引擎表达式按行计算后再补进结果，避免在 SQL 里重复 CASE。
    _, engine_expr, _, _, _ = _ocr_plan_sql(conn)
    engine_sql = (
        "SELECT v.version_id, "
        f"{engine_expr} AS engine FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
        "  AND l1.parser_signature = ?"
        f" WHERE v.version_id IN ({','.join('?' for _ in rows)})"
    )
    engine_rows = conn.execute(engine_sql, [PARSER_SIGNATURE, *[r["version_id"] for r in rows]]).fetchall()
    engines = {row["version_id"]: row["engine"] for row in engine_rows}
    return [{**dict(row), "engine": engines.get(row["version_id"])} for row in rows]


def ocr_pending_count(conn: sqlite3.Connection, source_id: str | None = None,
                      allowed_exts: set[str] | None = None) -> int:
    rows, _ = _ocr_query(conn, source_id, "SELECT COUNT(*) AS n", "",
                         allowed_exts=allowed_exts)
    return rows[0]["n"] if rows else 0


def _l1_pages(conn: sqlite3.Connection, version_id: str) -> set[int]:
    """该版本已经有可用文本层的页码；OCR 只补这些页之外的页面。"""
    rows = conn.execute(
        "SELECT DISTINCT c.page FROM chunks c"
        " JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
        " WHERE c.version_id = ? AND c.page IS NOT NULL AND a.state = 'ok'",
        (version_id,),
    ).fetchall()
    return {row["page"] for row in rows if row["page"]}


def _row_engine(conn: sqlite3.Connection, row) -> str | None:
    if isinstance(row, dict) and row.get("engine"):
        return row["engine"]
    if not isinstance(row, dict):
        keys = row.keys()
        if "engine" in keys and row["engine"]:
            return row["engine"]
    l1 = conn.execute(
        "SELECT text_pages FROM parse_artifacts"
        " WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], PARSER_SIGNATURE),
    ).fetchone()
    text_pages = l1["text_pages"] if l1 is not None else 0
    return _route_engine(row["ext"], text_pages, ocr.plan_engines(conn))


def index_ocr_version(conn: sqlite3.Connection, row, engine: str | None = None) -> str:
    """按策略选择引擎，对图片或 PDF 缺失文本层的页做 OCR。"""
    engine = engine or _row_engine(conn, row)
    if engine is None:
        raise ocr.OcrUnavailable("当前没有可用的 OCR 引擎")
    signatures = PPTX_OCR_SIGNATURES if row["ext"] == "pptx" else OCR_SIGNATURES
    signature = signatures[engine]
    existing = conn.execute(
        "SELECT state FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], signature),
    ).fetchone()
    if existing is not None:
        return existing["state"]

    _job_update(conn, row, "l2", "running", count_attempt=True)
    conn.commit()
    sections: list[ocr.Section] = []
    chunks: list[Chunk] = []
    try:
        if Path(row["path"]).stat().st_size == 0:
            raise ocr.OcrEmptyFile("文件为空（0 字节）")
        if row["ext"] == "pdf":
            skip_pages: set[int] = set()
            l1 = conn.execute(
                "SELECT state, quality FROM parse_artifacts"
                " WHERE version_id = ? AND parser_signature = ?",
                (row["version_id"], PARSER_SIGNATURE),
            ).fetchone()
            if l1 is not None and l1["state"] == "ok" and l1["quality"] != "low":
                skip_pages = _l1_pages(conn, row["version_id"])
            if engine == "rapidocr":
                sections = ocr.extract_pdf_rapid(row["path"], skip_pages=skip_pages)
            else:
                sections = ocr.extract_pdf(row["path"], skip_pages=skip_pages)
        elif row["ext"] == "pptx":
            sections = ocr.extract_pptx(row["path"], rapid=(engine == "rapidocr"))
        elif engine == "rapidocr":
            sections = ocr.extract_image_rapid(row["path"])
        else:
            sections = ocr.extract_image(row["path"])
        sections = [section for section in sections if section.text.strip()]
        quality = ocr.quality_of(sections)
        if quality == "empty":
            state, message = "empty", "OCR 未识别到文字（可能是空白页或图片质量过低）"
        else:
            state, message = "ok", None
            chunks = chunk_sections(sections)
    except ocr.OcrEmptyFile as exc:
        state, quality, message = "empty", "empty", str(exc)
    except ocr.OcrUnavailable:
        raise
    except Exception as exc:
        if _is_file_lock_error(exc):
            state, quality, message = "locked", "empty", "文件被其他程序占用，本轮已跳过"
        else:
            state, quality, message = "failed", "empty", str(exc)[:400]

    _purge_old_versions(conn, row["asset_id"], row["version_id"])
    _write_artifact(conn, row, state=state, quality=quality, sections=sections,
                    message=message, chunks=chunks, signature=signature)
    _job_update(conn, row, "l2", "done" if state != "failed" else "failed", error=message)
    if state == "ok":
        knowledge.enqueue(conn, "summary", "document", row["asset_id"])
    return state


def index_ocr_pending(conn: sqlite3.Connection, source_id: str | None = None,
                      limit: int = OCR_RUN_LIMIT, *, ignore_budget: bool = False,
                      pause_check=None, row_delay: float = 0.0,
                      allowed_exts: set[str] | None = None) -> dict:
    counters = {"available": True, "indexed": 0, "empty": 0, "failed": 0,
                "locked": 0, "processed": 0, "remaining": 0,
                "budget_exhausted": False, "paused": False}
    plan = ocr.plan_engines(conn)
    windows_info = ocr.engine_info()
    available = plan["windows"]
    if not available and plan["rapidocr"] and plan["policy"] == "rapidocr":
        available = True
    if not available and plan["rapidocr"] and ocr_pending_count(
        conn, source_id, allowed_exts=allowed_exts
    ):
        available = True
    if not available:
        return {**counters, "available": False,
                "message": windows_info["message"] or "没有可用的 OCR 引擎",
                "engine": windows_info["engine"], "languages": windows_info["languages"]}

    for signature in [*OCR_SIGNATURES.values(), *PPTX_OCR_SIGNATURES.values()]:
        _requeue_failed(conn, "l2", signature, source_id)
    conn.commit()

    allow = 10 ** 9 if ignore_budget else min(limit, budget.remaining(conn, "l2"))
    processed = 0
    paused = False
    while processed < allow and not paused:
        rows = ocr_candidate_rows(conn, source_id, min(OCR_BATCH, allow - processed),
                                  allowed_exts=allowed_exts)
        if not rows:
            break
        for row in rows:
            state = index_ocr_version(conn, row)
            counters[{"ok": "indexed"}.get(state, state)] += 1
            processed += 1
            conn.commit()
            if pause_check is not None and pause_check():
                paused = True
                break
            if row_delay > 0:
                time.sleep(row_delay)
        conn.commit()

    if not ignore_budget:
        budget.add(conn, "l2", processed)
    if not ignore_budget and allow == 0 and ocr_candidate_rows(
        conn, source_id, 1, allowed_exts=allowed_exts
    ):
        counters["budget_exhausted"] = True
    counters["processed"] = processed
    counters["paused"] = paused
    counters["remaining"] = ocr_pending_count(conn, source_id,
                                              allowed_exts=allowed_exts)
    counters["engine"] = plan["policy"]
    counters["languages"] = (windows_info["languages"] if plan["windows"]
                             else ["zh-Hans", "en"])
    if processed:
        store.log_event(
            conn, "ocr_completed", source_id,
            detail=json.dumps({key: counters[key] for key in
                               ("indexed", "empty", "failed", "locked", "remaining",
                                "budget_exhausted", "paused")},
                              ensure_ascii=False),
            outcome="ok",
        )
        conn.commit()
    return counters


def index_ocr_in_new_connection(source_id: str | None = None,
                                store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return index_ocr_pending(conn, source_id)
    finally:
        conn.close()


def list_jobs(conn: sqlite3.Connection, *, status: str | None = None,
              stage: str | None = None, limit: int = 50) -> list[dict]:
    sql = [
        "SELECT j.job_id, j.source_id, j.version_id, j.asset_id, j.stage, j.status,"
        " j.attempts, j.error, j.updated_at, l.path, l.ext",
        "FROM index_jobs j",
        "LEFT JOIN content_versions v ON v.version_id = j.version_id",
        "LEFT JOIN asset_locations l ON l.asset_id = v.asset_id",
        "WHERE 1 = 1",
    ]
    params: list = []
    if status:
        sql.append("AND j.status = ?")
        params.append(status)
    if stage:
        sql.append("AND j.stage = ?")
        params.append(stage)
    sql.append("ORDER BY CASE j.status WHEN 'failed' THEN 0 ELSE 1 END, j.updated_at DESC")
    sql.append(f"LIMIT {max(1, min(limit, 200))}")
    items = []
    for row in conn.execute(" ".join(sql), params):
        item = dict(row)
        path = item.get("path") or ""
        item["name"] = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if path else None
        items.append(item)
    return items


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)).fetchone()


def retry_job(conn: sqlite3.Connection, job_id: str) -> dict:
    """单文件重试：清掉该版本的失败产物并立刻重跑一次。"""
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    row = conn.execute(
        "SELECT v.version_id, v.asset_id, l.path, l.ext, l.source_id"
        " FROM content_versions v JOIN asset_locations l ON l.asset_id = v.asset_id"
        " WHERE v.version_id = ?", (job["version_id"],)).fetchone()
    if row is None:
        raise ValueError("该版本已不在资产目录中")

    engine = None
    if job["stage"] == "l1":
        signature = PARSER_SIGNATURE
    else:
        engine = _row_engine(conn, row)
        if engine is None:
            raise ValueError("当前没有可用的 OCR 引擎")
        signature = OCR_SIGNATURES[engine]
    conn.execute(
        "DELETE FROM chunk_fts WHERE rowid IN (SELECT c.rowid FROM chunks c"
        " JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
        " WHERE a.version_id = ? AND a.parser_signature = ?)",
        (row["version_id"], signature))
    conn.execute(
        "DELETE FROM chunks WHERE artifact_id IN (SELECT artifact_id FROM parse_artifacts"
        " WHERE version_id = ? AND parser_signature = ?)",
        (row["version_id"], signature))
    conn.execute(
        "DELETE FROM parse_artifacts WHERE version_id = ? AND parser_signature = ?",
        (row["version_id"], signature))
    conn.execute(
        "UPDATE index_jobs SET attempts = 0, status = 'pending', error = NULL"
        " WHERE job_id = ?", (job_id,))
    conn.commit()

    if job["stage"] == "l1":
        state = index_version(conn, row)
    else:
        state = index_ocr_version(conn, row, engine)
    # 单文件重试是用户显式操作，不占每日后台解析预算。
    store.log_event(
        conn, "job_retry", row["source_id"],
        detail=json.dumps({"job_id": job_id, "stage": job["stage"], "state": state},
                          ensure_ascii=False),
        outcome="ok" if state != "failed" else "partial",
    )
    conn.commit()
    return {"job_id": job_id, "stage": job["stage"], "state": state}


def _classify_parse(row: sqlite3.Row,
                   allowed_exts: set[str] | None = None) -> tuple[str, str | None]:
    """把一个文件的解析产物归为 成功/失败/占用/无文本/仅登记/不支持/待处理。

    allowed_exts 来自盘点策略：解析器支持、但用户选择只登记不解压的类型
    （典型是压缩包）不再算「待解析」，避免看起来像永远处理不完的积压。
    """
    ext = (row["ext"] or "").lower()
    supported = extractors.is_supported(ext)
    ocr_ext = ext in ocr.OCR_EXTS
    l1_state = row["l1_state"]
    l1_chars = int(row["char_count"] or 0)

    if (l1_state == "ok" and l1_chars > 0) or row["ocr_ok"]:
        if l1_state == "ok" and l1_chars > 0 and row["ocr_ok"]:
            return "ok", "L1+L2"
        if l1_state == "ok" and l1_chars > 0:
            return "ok", "L1"
        return "ok", "L2"
    if row["error"] or l1_state == "failed" or row["ocr_failed"]:
        return "failed", "L2" if row["job_stage"] == "l2" or row["ocr_failed"] else "L1"
    if l1_state == "locked" or row["ocr_locked"]:
        return "locked", "L2" if row["ocr_locked"] else "L1"
    if l1_state == "empty" or row["ocr_empty"] or (l1_state == "ok" and l1_chars == 0):
        return "empty", "L1"
    if not supported:
        if not ocr_ext:
            return "unsupported", None
        return "pending", None
    if allowed_exts is not None and ext not in allowed_exts:
        return "metadata_only", None
    return "pending", None


def parse_results(conn: sqlite3.Connection, source_id: str, *,
                  status: str | None = None, limit: int = 100,
                  offset: int = 0) -> dict:
    """按数据源列出每个文件的解析结果：成功、失败、无文本、待处理与不支持。

    成功以 L1 正文或 L2 OCR 产物为准；失败优先取最近一条失败任务。
    一次分类完再分页，保证汇总数字不受分页影响。
    """
    rows = conn.execute(
        "SELECT l.location_id, l.path, l.rel_path, l.ext, l.size,"
        " v.version_id, a.state AS l1_state, a.quality, a.char_count,"
        " a.page_count, a.text_pages, a.image_count, a.message, a.created_at,"
        " j.stage AS job_stage, j.attempts, j.error, j.updated_at,"
        " o.ocr_ok, o.ocr_failed, o.ocr_locked, o.ocr_empty, o.ocr_chars"
        " FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        "  AND a.parser_signature = ?"
        " LEFT JOIN index_jobs j ON j.job_id = ("
        "   SELECT j2.job_id FROM index_jobs j2"
        "   WHERE j2.version_id = v.version_id AND j2.status = 'failed'"
        "   ORDER BY j2.updated_at DESC LIMIT 1)"
        " LEFT JOIN ("
        "   SELECT version_id,"
        "    MAX(CASE WHEN state = 'ok' THEN 1 ELSE 0 END) AS ocr_ok,"
        "    MAX(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS ocr_failed,"
        "    MAX(CASE WHEN state = 'locked' THEN 1 ELSE 0 END) AS ocr_locked,"
        "    MAX(CASE WHEN state = 'empty' THEN 1 ELSE 0 END) AS ocr_empty,"
        "    SUM(CASE WHEN state = 'ok' THEN char_count ELSE 0 END) AS ocr_chars"
        "   FROM parse_artifacts WHERE parser_signature LIKE 'l2.%'"
        "   GROUP BY version_id) o ON o.version_id = v.version_id"
        " WHERE v.is_current = 1 AND l.source_id = ? AND l.availability = 'AVAILABLE'"
        " ORDER BY l.path",
        (PARSER_SIGNATURE, source_id),
    ).fetchall()

    counts = {key: 0 for key in ("ok", "failed", "empty", "pending", "locked",
                                 "unsupported", "metadata_only")}
    allowed = inventory.allowed_exts(conn, source_id)
    items: list[dict] = []
    for row in rows:
        ext = (row["ext"] or "").lower()
        l1_chars = int(row["char_count"] or 0)
        item_status, stage = _classify_parse(row, allowed)
        counts[item_status] += 1
        items.append({
            "location_id": row["location_id"],
            "version_id": row["version_id"],
            "name": Path(row["path"]).name,
            "path": row["path"],
            "rel_path": row["rel_path"],
            "ext": ext,
            "size": int(row["size"] or 0),
            "status": item_status,
            "stage": stage,
            "chars": l1_chars + int(row["ocr_chars"] or 0),
            "pages": int(row["page_count"] or 0),
            "quality": row["quality"],
            "message": row["message"],
            "error": row["error"],
            "attempts": int(row["attempts"] or 0),
            "updated_at": row["updated_at"] or row["created_at"],
        })

    chosen = [
        item for item in items
        if not status or status == "all" or item["status"] == status
    ]
    start = max(0, offset)
    selected = chosen[start:start + max(1, min(limit, 500))]
    return {
        "source_id": source_id,
        "summary": {
            "total": len(items),
            "parsed": counts["ok"],
            "failed": counts["failed"],
            "empty": counts["empty"],
            "pending": counts["pending"],
            "locked": counts["locked"],
            "unsupported": counts["unsupported"],
            "metadata_only": counts["metadata_only"],
        },
        "items": selected,
        "total": len(chosen),
        "limit": max(1, min(limit, 500)),
        "offset": start,
        "generated_at": store.now_iso(),
    }


PREVIEW_MAX_CHARS = 200_000


def location_text(conn: sqlite3.Connection, location_id: str,
                  max_chars: int = PREVIEW_MAX_CHARS) -> dict:
    """读取单个文件的解析正文，供桌面端预览。

    按页 / 工作表顺序拼接，并去掉相邻分块的重叠区，结果与原文基本一致。
    """
    row = conn.execute(
        "SELECT l.location_id, l.asset_id, l.path, l.rel_path, l.ext, l.availability,"
        " s.state AS source_state"
        " FROM asset_locations l JOIN sources s ON s.source_id = l.source_id"
        " WHERE l.location_id = ?",
        (location_id,),
    ).fetchone()
    if row is None:
        raise KeyError("未找到该文件")
    if row["availability"] != "AVAILABLE":
        raise ValueError(f"文件当前无法读取：{row['availability']}")
    if row["source_state"] != "active":
        raise ValueError("所属数据源已暂停，恢复后才能预览")

    version = conn.execute(
        "SELECT version_id FROM content_versions WHERE asset_id = ? AND is_current = 1"
        " ORDER BY observed_at DESC LIMIT 1",
        (row["asset_id"],),
    ).fetchone()
    if version is None:
        raise KeyError("该文件还没有内容版本，请先扫描")
    chunks = conn.execute(
        "SELECT page, sheet, ordinal, text, char_start, char_end FROM chunks"
        " WHERE version_id = ?"
        " ORDER BY COALESCE(page, 0), COALESCE(sheet, ''), ordinal",
        (version["version_id"],),
    ).fetchall()
    if not chunks:
        raise KeyError("该文件还没有可预览的正文")

    parts: list[str] = []
    current: tuple[int | None, str | None] | None = None
    prev_end = 0
    for chunk in chunks:
        key = (chunk["page"], chunk["sheet"])
        text = chunk["text"] or ""
        if key != current:
            if current is not None:
                label = f"第 {chunk['page']} 页" if chunk["page"] else (chunk["sheet"] or "")
                if label:
                    parts.append(f"\n\n—— {label} ——\n\n")
            current = key
            prev_end = 0
        elif prev_end > chunk["char_start"]:
            # 分块之间有 CHUNK_OVERLAP 字重叠，预览时只保留一份。
            text = text[prev_end - chunk["char_start"]:]
        if text:
            parts.append(text)
        prev_end = int(chunk["char_end"] or 0)

    full = "".join(parts)
    truncated = len(full) > max_chars
    return {
        "location_id": row["location_id"],
        "name": Path(row["path"]).name,
        "path": row["path"],
        "rel_path": row["rel_path"],
        "ext": (row["ext"] or "").lower(),
        "chars": len(full),
        "truncated": truncated,
        "text": full[:max_chars] if truncated else full,
    }


def parse_progress(conn: sqlite3.Connection, source_id: str) -> dict:
    """数据源的深度解析进度：只统计文件数，供数据源卡片上的进度条使用。"""
    rows = conn.execute(
        "SELECT l.ext, a.state AS l1_state, a.char_count, j.stage AS job_stage, j.error,"
        " o.ocr_ok, o.ocr_failed, o.ocr_locked, o.ocr_empty"
        " FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        "  AND a.parser_signature = ?"
        " LEFT JOIN index_jobs j ON j.job_id = ("
        "   SELECT j2.job_id FROM index_jobs j2"
        "   WHERE j2.version_id = v.version_id AND j2.status = 'failed'"
        "   ORDER BY j2.updated_at DESC LIMIT 1)"
        " LEFT JOIN ("
        "   SELECT version_id,"
        "    MAX(CASE WHEN state = 'ok' THEN 1 ELSE 0 END) AS ocr_ok,"
        "    MAX(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS ocr_failed,"
        "    MAX(CASE WHEN state = 'locked' THEN 1 ELSE 0 END) AS ocr_locked,"
        "    MAX(CASE WHEN state = 'empty' THEN 1 ELSE 0 END) AS ocr_empty"
        "   FROM parse_artifacts WHERE parser_signature LIKE 'l2.%'"
        "   GROUP BY version_id) o ON o.version_id = v.version_id"
        " WHERE v.is_current = 1 AND l.source_id = ?"
        "  AND l.availability = 'AVAILABLE'",
        (PARSER_SIGNATURE, source_id),
    ).fetchall()
    counts = {key: 0 for key in ("ok", "failed", "empty", "pending", "locked",
                                 "unsupported", "metadata_only")}
    allowed = inventory.allowed_exts(conn, source_id)
    for row in rows:
        status, _ = _classify_parse(row, allowed)
        counts[status] += 1
    return {
        "total": len(rows),
        "parsed": counts["ok"],
        "failed": counts["failed"],
        "empty": counts["empty"],
        "pending": counts["pending"],
        "locked": counts["locked"],
        "unsupported": counts["unsupported"],
        "metadata_only": counts["metadata_only"],
    }


def coverage(conn: sqlite3.Connection) -> dict:
    """当前内容版本的解析覆盖：已解析 / 无文本层 / 失败 / 未支持 / 仅登记 / 待处理。"""
    rows = conn.execute(
        "SELECT v.version_id, l.ext, l.source_id, a.state FROM content_versions v"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
        "  AND a.parser_signature = ?"
        " WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        (PARSER_SIGNATURE,),
    ).fetchall()
    counts = {"indexed": 0, "empty": 0, "failed": 0, "locked": 0,
              "pending": 0, "unsupported": 0, "metadata_only": 0}
    allowed_cache: dict[str, set[str] | None] = {}
    for row in rows:
        ext = (row["ext"] or "").lower()
        if not extractors.is_supported(ext):
            # 图片与 PDF 由 L2 接管，不算"暂不支持"。
            if ext not in ocr.OCR_EXTS:
                counts["unsupported"] += 1
        elif row["state"] is None:
            source_id = row["source_id"]
            if source_id not in allowed_cache:
                allowed_cache[source_id] = inventory.allowed_exts(conn, source_id)
            allowed = allowed_cache[source_id]
            if allowed is not None and ext not in allowed:
                counts["metadata_only"] += 1
            else:
                counts["pending"] += 1
        elif row["state"] == "ok":
            counts["indexed"] += 1
        elif row["state"] == "empty":
            counts["empty"] += 1
        elif row["state"] == "locked":
            counts["locked"] += 1
        else:
            counts["failed"] += 1

    plan, engine_expr, sig_expr, eligible, image_params = _ocr_plan_sql(conn)
    ocr_states: dict = {}
    if sig_expr is not None:
        rows = conn.execute(
            "SELECT a.state, COUNT(*) AS n FROM content_versions v"
            " JOIN asset_locations l ON l.asset_id = v.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " LEFT JOIN parse_artifacts l1 ON l1.version_id = v.version_id"
            "  AND l1.parser_signature = ?"
            f" LEFT JOIN parse_artifacts a ON a.version_id = v.version_id"
            f"  AND a.parser_signature = {sig_expr}"
            " WHERE v.is_current = 1 AND l.availability = 'AVAILABLE'"
            f"  AND s.state = 'active' AND {eligible}"
            " GROUP BY a.state",
            [PARSER_SIGNATURE, *image_params],
        ).fetchall()
        ocr_states = {row["state"]: row["n"] for row in rows}
    counts["low_quality"] = conn.execute(
        "SELECT COUNT(*) FROM parse_artifacts a"
        " JOIN content_versions v ON v.version_id = a.version_id"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE a.parser_signature = ? AND a.quality = 'low' AND v.is_current = 1"
        "  AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        (PARSER_SIGNATURE,),
    ).fetchone()[0]
    info = ocr.engine_info()
    counts.update({
        "ocr_indexed": ocr_states.get("ok", 0),
        "ocr_empty": ocr_states.get("empty", 0),
        "ocr_failed": ocr_states.get("failed", 0),
        "ocr_locked": ocr_states.get("locked", 0),
        "ocr_pending": ocr_pending_count(conn),
        "mixed_pdf": conn.execute(
            "SELECT COUNT(*) FROM parse_artifacts a"
            " JOIN content_versions v ON v.version_id = a.version_id"
            " JOIN asset_locations l ON l.asset_id = v.asset_id"
            " JOIN sources s ON s.source_id = l.source_id"
            " WHERE a.parser_signature = ? AND l.ext = 'pdf' AND a.page_count > a.text_pages"
            "  AND a.text_pages > 0"
            "  AND v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'"
            "  AND NOT EXISTS (SELECT 1 FROM parse_artifacts b"
            "   WHERE b.version_id = a.version_id AND b.parser_signature IN (?, ?))",
            (PARSER_SIGNATURE, OCR_SIGNATURES["windows"], OCR_SIGNATURES["rapidocr"]),
        ).fetchone()[0],
        "ocr_available": info["available"],
        "ocr_engine": plan["policy"],
        "ocr_policy": plan["policy"],
        "ocr_languages": info["languages"],
        "ocr_message": info["message"],
    })
    return counts


def _seconds_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start))
                          .total_seconds()))
    except ValueError:
        return None


def full_coverage(conn: sqlite3.Connection) -> dict:
    """首次建库的 L0/L1/L2 时间线与 Time to Full Coverage。"""
    rows = conn.execute(
        "SELECT source_id, label, root_uri, created_at, l0_completed_at,"
        " l1_completed_at, l2_completed_at, full_coverage_at"
        " FROM sources WHERE state = 'active' ORDER BY created_at"
    ).fetchall()
    content = coverage(conn)
    started_at = min((row["created_at"] for row in rows), default=None)
    stage_ts = {
        "l0": max((row["l0_completed_at"] for row in rows if row["l0_completed_at"]),
                  default=None),
        "l1": max((row["l1_completed_at"] for row in rows if row["l1_completed_at"]),
                  default=None),
        "l2": max((row["l2_completed_at"] for row in rows if row["l2_completed_at"]),
                  default=None),
    }
    stage_complete = {
        stage: all(row[f"{stage}_completed_at"] for row in rows)
        for stage in ("l0", "l1", "l2")
    }
    complete = bool(rows) and all(row["full_coverage_at"] for row in rows)
    completed_at = None
    if complete:
        completed_at = max(row["full_coverage_at"] for row in rows)
    excluded = conn.execute(
        "SELECT COALESCE(SUM(r.files_skipped), 0) FROM source_scan_runs r"
        " JOIN sources s ON s.source_id = r.source_id"
        " WHERE s.state = 'active' AND r.started_at ="
        " (SELECT MAX(r2.started_at) FROM source_scan_runs r2"
        "  WHERE r2.source_id = r.source_id)"
    ).fetchone()[0]
    sources = []
    for row in rows:
        sources.append({
            "source_id": row["source_id"],
            "label": row["label"],
            "root_uri": row["root_uri"],
            "created_at": row["created_at"],
            "l0_completed_at": row["l0_completed_at"],
            "l1_completed_at": row["l1_completed_at"],
            "l2_completed_at": row["l2_completed_at"],
            "full_coverage_at": row["full_coverage_at"],
            "full_coverage_seconds": _seconds_between(
                row["created_at"], row["full_coverage_at"]),
        })
    return {
        "complete": complete,
        "started_at": started_at,
        "completed_at": completed_at,
        "time_to_full_coverage_seconds": _seconds_between(started_at, completed_at),
        "elapsed_seconds": _seconds_between(started_at, store.now_iso()),
        "l0_complete": stage_complete["l0"],
        "l1_complete": stage_complete["l1"],
        "l2_complete": stage_complete["l2"],
        "l0_completed_at": stage_ts["l0"],
        "l1_completed_at": stage_ts["l1"],
        "l2_completed_at": stage_ts["l2"],
        "l0_seconds": _seconds_between(started_at, stage_ts["l0"]),
        "l1_seconds": _seconds_between(started_at, stage_ts["l1"]),
        "l2_seconds": _seconds_between(started_at, stage_ts["l2"]),
        "pending_l1": content["pending"],
        "pending_l2": content["ocr_pending"],
        "failed": content["failed"],
        "ocr_failed": content["ocr_failed"],
        "unsupported": content["unsupported"],
        "excluded": excluded,
        "sources": sources,
    }
