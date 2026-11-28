"""可选语义检索：调用 OpenAI 兼容或 Ollama 的 embedding 接口，本地做余弦相似度。

默认关闭（provider = none）。云端 provider 必须先在设置里显式允许上传文本片段；
向量按 chunk 存在本机 SQLite，检索时不联网，只有"生成向量"这一步可能调用模型。
"""

from __future__ import annotations

import array
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from . import db, secrets_store, store

SETTINGS_KEY = "embedding"
ERROR_KEY = "embedding_last_error"
PROVIDERS = {"none", "openai", "ollama"}
DEFAULT_SETTINGS = {
    "provider": "none",
    "base_url": "",
    "model": "",
    "api_key": "",
    "allow_cloud": False,
    "top_k": 8,
}
BATCH = 32
MAX_TEXT_CHARS = 1200

# 制表符数据表按精确文本检索即可，语义向量既贵又几乎没有区分度。
SKIP_EXTENSIONS = {"tsv"}
_SEQUENCE_TEXT = re.compile(r"[acgtnACGTN\s\d,;._-]+")


def _embeddable(text: str) -> tuple[bool, str | None]:
    """判断一段正文是否值得做语义向量；返回（是否可向量化，跳过原因）。"""
    sample = (text or "").strip()
    if len(sample) < 5:
        return False, "文本过短"
    if _SEQUENCE_TEXT.fullmatch(sample):
        return False, "序列或数字数据"
    total = len(sample)
    letters = sum(1 for char in sample if char.isalpha())
    digits = sum(1 for char in sample if char.isdigit())
    if letters / total < 0.05:
        return False, "缺少文字"
    if letters / total < 0.12 and digits / total > 0.25:
        return False, "数字表格"
    return True, None


class EmbeddingError(RuntimeError):
    pass


# ---------------------------------------------------------------- 设置

def load_settings(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    settings = dict(DEFAULT_SETTINGS)
    if row is not None:
        try:
            stored = json.loads(row["value"])
        except (TypeError, ValueError):
            stored = {}
        if isinstance(stored, dict):
            settings.update({key: stored[key] for key in DEFAULT_SETTINGS if key in stored})
    if settings["provider"] not in PROVIDERS:
        settings["provider"] = "none"
    settings["allow_cloud"] = bool(settings["allow_cloud"])
    try:
        settings["top_k"] = max(1, min(int(settings["top_k"]), 50))
    except (TypeError, ValueError):
        settings["top_k"] = 8
    if settings.get("api_key"):
        try:
            settings["api_key"] = secrets_store.unprotect(settings["api_key"])
        except secrets_store.SecretError:
            settings["api_key"] = ""
    return settings


# ---------------------------------------------------------------- 错误可见

def _set_last_error(conn: sqlite3.Connection, message: str | None) -> None:
    # MCP 以 query_only 打开数据库，记录错误失败不能影响检索本身。
    try:
        if message:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (ERROR_KEY, str(message)[:500]),
            )
        else:
            # 成功路径每次都写会留下无意义的 WAL；只在确有旧错误时清除。
            if not conn.execute(
                "SELECT 1 FROM app_settings WHERE key = ?", (ERROR_KEY,)
            ).fetchone():
                return
            conn.execute("DELETE FROM app_settings WHERE key = ?", (ERROR_KEY,))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def public_settings(conn: sqlite3.Connection) -> dict:
    settings = load_settings(conn)
    has_key = bool(settings.pop("api_key", ""))
    settings["has_key"] = has_key
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (ERROR_KEY,)).fetchone()
    settings["last_error"] = row["value"] if row else None
    return settings


def update_settings(conn: sqlite3.Connection, payload: dict) -> dict:
    settings = load_settings(conn)
    provider = payload.get("provider")
    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError("未知的向量提供商")
        settings["provider"] = provider
    for key in ("base_url", "model"):
        if key in payload and payload[key] is not None:
            settings[key] = str(payload[key]).strip()
    if payload.get("allow_cloud") is not None:
        settings["allow_cloud"] = bool(payload["allow_cloud"])
    if payload.get("top_k") is not None:
        try:
            settings["top_k"] = max(1, min(int(payload["top_k"]), 50))
        except (TypeError, ValueError) as exc:
            raise ValueError("top_k 必须是数字") from exc
    if payload.get("clear_key"):
        settings["api_key"] = ""
    elif payload.get("api_key"):
        settings["api_key"] = secrets_store.protect(str(payload["api_key"]).strip())
    # 配置已变，上一次的失败原因不再有参考价值。
    conn.execute("DELETE FROM app_settings WHERE key = ?", (ERROR_KEY,))
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)),
    )
    conn.commit()
    return public_settings(conn)


def enabled(settings: dict) -> bool:
    if settings["provider"] == "none":
        return False
    if not settings["model"]:
        return False
    if settings["provider"] == "openai":
        return settings["allow_cloud"] and bool(settings["api_key"])
    return True


# ---------------------------------------------------------------- 向量生成

def _post_json(url: str, payload: dict, headers: dict, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    box: dict = {}

    def worker() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                box["body"] = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            box["error"] = EmbeddingError(f"向量接口返回 {exc.code}：{detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            box["error"] = EmbeddingError(f"无法连接向量接口：{exc}")
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须带回主线程
            box["error"] = EmbeddingError(f"向量接口异常：{exc}")

    # 向量服务卡住时不能把整次检索拖死：超过墙钟上限直接回退词面检索。
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if thread.is_alive():
        raise EmbeddingError("向量接口超时（超过墙钟上限）")
    if "error" in box:
        raise box["error"]
    try:
        return json.loads(box.get("body", ""))
    except ValueError as exc:
        raise EmbeddingError("向量接口返回了非 JSON 内容") from exc


def embed_texts(settings: dict, texts: list[str]) -> list[list[float]]:
    if not enabled(settings):
        raise EmbeddingError("语义检索尚未启用或缺少配置")
    if not texts:
        return []
    trimmed = [text[:MAX_TEXT_CHARS] for text in texts]
    if settings["provider"] == "openai":
        base = (settings["base_url"] or "https://api.openai.com/v1").rstrip("/")
        data = _post_json(f"{base}/embeddings",
                          {"model": settings["model"], "input": trimmed},
                          {"Authorization": f"Bearer {settings['api_key']}"})
        try:
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            return [list(item["embedding"]) for item in items]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError("向量接口返回结构不符合 OpenAI 兼容格式") from exc

    base = (settings["base_url"] or "http://127.0.0.1:11434").rstrip("/")
    data = _post_json(f"{base}/api/embed", {"model": settings["model"], "input": trimmed}, {})
    if isinstance(data.get("embeddings"), list):
        return [list(vector) for vector in data["embeddings"]]
    # 兼容旧版 Ollama：逐条 /api/embeddings
    vectors = []
    for text in trimmed:
        single = _post_json(f"{base}/api/embeddings",
                            {"model": settings["model"], "prompt": text}, {})
        if "embedding" not in single:
            raise EmbeddingError("Ollama 返回结构不符合预期")
        vectors.append(list(single["embedding"]))
    return vectors


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def _cosine(left, right) -> float:
    if len(left) != len(right):
        return -1.0
    dot = norm_left = norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left == 0 or norm_right == 0:
        return -1.0
    return dot / (math.sqrt(norm_left) * math.sqrt(norm_right))


# ---------------------------------------------------------------- 索引

def _pending_sql(model: str, source_id: str | None) -> tuple[str, list]:
    sql = [
        "SELECT c.chunk_id, c.asset_id, c.text FROM chunks c",
        "JOIN content_versions v ON v.version_id = c.version_id",
        "JOIN asset_locations l ON l.asset_id = c.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE v.is_current = 1 AND l.availability = 'AVAILABLE' AND s.state = 'active'",
        "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e"
        " WHERE e.chunk_id = c.chunk_id AND e.model = ?)",
        "AND NOT EXISTS (SELECT 1 FROM chunk_embedding_skips k"
        " WHERE k.chunk_id = c.chunk_id AND k.model = ?)",
    ]
    params: list = [model, model]
    if SKIP_EXTENSIONS:
        placeholders = ",".join("?" for _ in SKIP_EXTENSIONS)
        sql.append(f"AND l.ext NOT IN ({placeholders})")
        params.extend(sorted(SKIP_EXTENSIONS))
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    return " ".join(sql), params


def _pending_chunks(conn: sqlite3.Connection, model: str, source_id: str | None,
                    limit: int) -> list[sqlite3.Row]:
    sql, params = _pending_sql(model, source_id)
    return conn.execute(f"{sql} LIMIT {limit}", params).fetchall()


def pending_count(conn: sqlite3.Connection, model: str | None = None,
                  source_id: str | None = None) -> int:
    settings = load_settings(conn)
    model = model or settings["model"]
    if not model:
        return 0
    sql, params = _pending_sql(model, source_id)
    row = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()
    return int(row[0]) if row else 0


def index_pending(conn: sqlite3.Connection, source_id: str | None = None,
                  limit: int = 400) -> dict:
    settings = load_settings(conn)
    counters = {"available": False, "indexed": 0, "skipped": 0, "remaining": 0,
                "model": settings["model"] or None, "message": None}
    if not enabled(settings):
        counters["message"] = "语义检索未启用"
        return counters
    counters["available"] = True

    processed = 0
    skipped = 0
    scanned = 0
    while scanned < limit:
        rows = _pending_chunks(conn, settings["model"], source_id,
                               min(BATCH, limit - scanned))
        if not rows:
            break
        scanned += len(rows)
        embeddable = []
        skips = []
        for row in rows:
            ok, reason = _embeddable(row["text"])
            if ok:
                embeddable.append(row)
            else:
                skips.append((row["chunk_id"], settings["model"], reason or "不适合向量",
                              store.now_iso()))
        if skips:
            conn.executemany(
                "INSERT INTO chunk_embedding_skips (chunk_id, model, reason, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(chunk_id) DO UPDATE SET model = excluded.model,"
                " reason = excluded.reason, created_at = excluded.created_at",
                skips)
            conn.commit()
            skipped += len(skips)
        if not embeddable:
            continue
        try:
            vectors = embed_texts(settings, [row["text"] for row in embeddable])
            if len(vectors) != len(embeddable):
                raise EmbeddingError("向量数量与文本数量不一致")
        except EmbeddingError as exc:
            _set_last_error(conn, str(exc))
            raise
        _set_last_error(conn, None)
        for row, vector in zip(embeddable, vectors):
            conn.execute(
                "INSERT INTO chunk_embeddings"
                " (chunk_id, asset_id, model, dim, vector, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(chunk_id) DO UPDATE SET asset_id = excluded.asset_id,"
                " model = excluded.model, dim = excluded.dim, vector = excluded.vector,"
                " updated_at = excluded.updated_at",
                (row["chunk_id"], row["asset_id"], settings["model"], len(vector),
                 _pack(vector), store.now_iso()),
            )
        conn.commit()
        processed += len(embeddable)

    counters["indexed"] = processed
    counters["skipped"] = skipped
    counters["remaining"] = len(_pending_chunks(conn, settings["model"], source_id, 1))
    return counters


def index_all_pending(conn: sqlite3.Connection, source_id: str | None = None,
                      batch: int = 400) -> dict:
    """循环补建直到没有待建 chunk；单轮 index_pending 只处理 batch 条。"""
    settings = load_settings(conn)
    counters = {"available": False, "indexed": 0, "skipped": 0, "remaining": 0,
                "model": settings["model"] or None, "message": None}
    if not enabled(settings):
        counters["message"] = "语义检索未启用"
        return counters
    counters["available"] = True

    total = 0
    skipped = 0
    while True:
        result = index_pending(conn, source_id, limit=batch)
        total += result["indexed"]
        skipped += result["skipped"]
        if result["indexed"] == 0 and result["skipped"] == 0:
            break
    counters["indexed"] = total
    counters["skipped"] = skipped
    counters["remaining"] = len(_pending_chunks(conn, settings["model"], source_id, 1))
    return counters


def index_in_new_connection(source_id: str | None = None,
                            store_path: str | None = None) -> dict:
    conn = db.connect(store_path)
    try:
        return index_pending(conn, source_id, limit=200)
    finally:
        conn.close()


def clear(conn: sqlite3.Connection, model: str | None = None) -> int:
    if model:
        cursor = conn.execute("DELETE FROM chunk_embeddings WHERE model = ?", (model,))
    else:
        cursor = conn.execute("DELETE FROM chunk_embeddings")
    conn.commit()
    return cursor.rowcount


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]


def skipped_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunk_embedding_skips").fetchone()[0]


# ---------------------------------------------------------------- 语义召回

def _numpy_module():
    """可选加速：有 numpy 时向量点积走 C 实现，十几秒的全库扫描降到 1~2 秒。"""
    try:
        import numpy  # noqa: PLC0415 - 可选依赖，缺失时回退纯 Python
        return numpy
    except Exception:
        return None


# 全库向量缓存：float32 向量在内存里太大（约 450MB），这里量化成 int8（约 112MB）。
# 增量策略：新向量只追加到缓存尾部；删除/覆盖积累到阈值才后台压缩重建。
_VECTOR_CACHE_LOCK = threading.RLock()
_VECTOR_CACHE: dict = {
    "model": None,
    "dim": 0,
    "ids": [],
    "matrix": None,     # int8，预留 capacity 行，便于追加
    "scales": None,
    "norms": None,
    "rows": 0,          # 矩阵里实际有效的向量行数
    "count": 0,         # 上次同步时数据库里的向量总数
    "max_rowid": 0,     # 已纳入缓存的最高 rowid（增量水位）
}
_VECTOR_REBUILD = {"running": False}

_APPEND_LIMIT = 20000   # 单次追加上限，超出则交给后台整库重建
_STALE_MIN = 2000       # 失效向量下限
_STALE_RATIO = 0.02     # 失效向量占比阈值

# 语义命中门槛：把余弦相似度换算成"这次查询自己的分布"里的 z 分数。
# 原始相似度跨查询不可比（有的问题整体偏高），固定阈值会把无关片段也收进来。
Z_GATE = 2.0            # 高于均值两个标准差才算语义命中
Z_MIN_POPULATION = 200  # 向量太少时分布不稳定，退回相似度判断


def _new_cache(model: str | None = None, dim: int = 0) -> dict:
    return {
        "model": model, "dim": dim, "ids": [], "asset_ids": [], "matrix": None,
        "scales": None, "norms": None, "rows": 0, "count": 0, "max_rowid": 0,
    }


# 查询向量缓存：同一句话反复检索时省掉一次模型调用（Agent 经常会重复问）。
_QUERY_CACHE: dict[tuple[str, str], tuple[float, list[float]]] = {}
_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_CACHE_TTL = 600.0
_QUERY_CACHE_MAX = 128


def _query_vector(settings: dict, text: str) -> list[float]:
    key = (settings.get("model") or "", text)
    now = time.monotonic()
    with _QUERY_CACHE_LOCK:
        hit = _QUERY_CACHE.get(key)
        if hit is not None and now - hit[0] < _QUERY_CACHE_TTL:
            return hit[1]
        if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
            expired = [item for item, value in _QUERY_CACHE.items()
                       if now - value[0] >= _QUERY_CACHE_TTL]
            for item in expired:
                _QUERY_CACHE.pop(item, None)
            if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
                _QUERY_CACHE.clear()
    vector = embed_texts(settings, [text])[0]
    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE[key] = (time.monotonic(), vector)
    return vector


def _vector_cache_key(conn: sqlite3.Connection, model: str) -> tuple:
    """只查总数与 rowid 水位；MAX(updated_at) 会退化成全表扫描，约 0.6 秒。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(rowid), 0) AS r"
        " FROM chunk_embeddings WHERE model = ?", (model,)).fetchone()
    return (model, int(row["n"]), int(row["r"]))


def _quantize(numpy, values):
    """把 float32 向量压成 int8 + 缩放系数；零向量返回 None。"""
    norm = float(numpy.linalg.norm(values))
    peak = float(numpy.max(numpy.abs(values))) if values.size else 0.0
    if norm <= 0 or peak <= 0:
        return None
    scale = peak / 127.0
    quantized = numpy.rint(values / scale)
    numpy.clip(quantized, -127, 127, out=quantized)
    return quantized.astype(numpy.int8), scale, norm


def _ensure_capacity(numpy, cache: dict, need: int) -> None:
    capacity = 0 if cache["matrix"] is None else int(cache["matrix"].shape[0])
    if capacity >= need:
        return
    grown = max(need, int(capacity * 1.5) + 1024)
    matrix = numpy.empty((grown, cache["dim"]), dtype=numpy.int8)
    scales = numpy.ones(grown, dtype=numpy.float32)
    norms = numpy.ones(grown, dtype=numpy.float32)
    if cache["rows"]:
        matrix[:cache["rows"]] = cache["matrix"][:cache["rows"]]
        scales[:cache["rows"]] = cache["scales"][:cache["rows"]]
        norms[:cache["rows"]] = cache["norms"][:cache["rows"]]
    cache["matrix"], cache["scales"], cache["norms"] = matrix, scales, norms


def _append_rows(numpy, cache: dict, rows: list[sqlite3.Row]) -> int | None:
    """把 rowid 水位之后的新向量追加进缓存；维度不一致返回 None 让调用方重建。"""
    if not rows:
        return cache["max_rowid"]
    for row in rows:
        if int(row["dim"]) != cache["dim"]:
            return None
        if numpy.frombuffer(row["vector"], dtype=numpy.float32).size != cache["dim"]:
            return None
    _ensure_capacity(numpy, cache, cache["rows"] + len(rows))
    index = cache["rows"]
    last_rowid = cache["max_rowid"]
    for row in rows:
        last_rowid = int(row["rowid"])
        values = numpy.frombuffer(row["vector"], dtype=numpy.float32)
        quantized = _quantize(numpy, values)
        if quantized is None:
            continue
        cache["matrix"][index] = quantized[0]
        cache["scales"][index] = quantized[1]
        cache["norms"][index] = quantized[2]
        cache["ids"].append(row["chunk_id"])
        cache["asset_ids"].append(row["asset_id"])
        index += 1
    cache["rows"] = index
    return last_rowid


def _build_cache(conn: sqlite3.Connection, model: str) -> dict | None:
    """整库重建（只在冷启动、换模型或失效片段过多时发生）。"""
    numpy = _numpy_module()
    if numpy is None:
        return None
    key = _vector_cache_key(conn, model)
    cache = _new_cache(model)
    cache["count"] = key[1]
    cache["max_rowid"] = key[2]
    if key[1] == 0:
        return cache
    dim_row = conn.execute(
        "SELECT dim FROM chunk_embeddings WHERE model = ? LIMIT 1", (model,)).fetchone()
    if dim_row is None or not dim_row["dim"]:
        return cache
    cache["dim"] = int(dim_row["dim"])
    _ensure_capacity(numpy, cache, max(1, key[1]))
    for row in conn.execute(
            "SELECT e.rowid AS rowid, e.chunk_id AS chunk_id, e.asset_id AS asset_id,"
            " e.dim AS dim, e.vector AS vector"
            " FROM chunk_embeddings e"
            " WHERE e.model = ? ORDER BY e.rowid", (model,)):
        if int(row["dim"]) != cache["dim"]:
            continue
        values = numpy.frombuffer(row["vector"], dtype=numpy.float32)
        if values.size != cache["dim"]:
            continue
        quantized = _quantize(numpy, values)
        if quantized is None:
            continue
        index = cache["rows"]
        if index >= cache["matrix"].shape[0]:
            _ensure_capacity(numpy, cache, index + 1)
        cache["matrix"][index] = quantized[0]
        cache["scales"][index] = quantized[1]
        cache["norms"][index] = quantized[2]
        cache["ids"].append(row["chunk_id"])
        cache["asset_ids"].append(row["asset_id"])
        cache["rows"] = index + 1
        cache["max_rowid"] = max(cache["max_rowid"], int(row["rowid"]))
    return cache


def _connection_path(conn: sqlite3.Connection) -> str | None:
    """重建线程需要单独连接；沿用当前连接指向的数据库文件。"""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    path = row["file"] if row is not None else ""
    return path or None


def _start_rebuild(model: str, store_path: str | None) -> None:
    """后台整库重建；查询继续用旧缓存，不把两秒重建塞进请求路径。"""
    with _VECTOR_CACHE_LOCK:
        if _VECTOR_REBUILD["running"]:
            return
        _VECTOR_REBUILD["running"] = True

    def worker() -> None:
        conn = None
        try:
            conn = db.connect(store_path) if store_path else db.connect()
            built = _build_cache(conn, model)
            if built is not None:
                with _VECTOR_CACHE_LOCK:
                    if _VECTOR_CACHE["model"] in (None, model):
                        _VECTOR_CACHE.clear()
                        _VECTOR_CACHE.update(built)
        except Exception:  # noqa: BLE001 - 重建失败时下次查询会再触发
            return
        finally:
            if conn is not None:
                conn.close()
            with _VECTOR_CACHE_LOCK:
                _VECTOR_REBUILD["running"] = False

    threading.Thread(target=worker, daemon=True, name="embedding-rebuild").start()


def _refresh_cache(conn: sqlite3.Connection, model: str) -> dict:
    """确保缓存跟上数据库；新增走追加，过期片段过多才后台重建。调用方需持锁。"""
    key = _vector_cache_key(conn, model)
    cache = _VECTOR_CACHE
    if (cache["model"] == model and cache["count"] == key[1]
            and cache["max_rowid"] == key[2]):
        return cache
    if key[1] == 0:
        cache.clear()
        cache.update(_new_cache(model))
        return cache
    # 换模型、无缓存，或 rowid 水位回退（VACUUM 重排）时只能整库重建。
    if cache["model"] != model or cache["matrix"] is None or key[2] < cache["max_rowid"]:
        built = _build_cache(conn, model)
        if built is None:
            return cache
        cache.clear()
        cache.update(built)
        return cache

    added = conn.execute(
        "SELECT COUNT(*) FROM chunk_embeddings WHERE model = ? AND rowid > ?",
        (model, cache["max_rowid"])).fetchone()[0]
    if added:
        if added > _APPEND_LIMIT:
            _start_rebuild(model, _connection_path(conn))
            return cache
        rows = conn.execute(
            "SELECT e.rowid AS rowid, e.chunk_id AS chunk_id, e.asset_id AS asset_id,"
            " e.dim AS dim, e.vector AS vector"
            " FROM chunk_embeddings e"
            " WHERE e.model = ? AND e.rowid > ? ORDER BY e.rowid",
            (model, cache["max_rowid"])).fetchall()
        last = _append_rows(_numpy_module(), cache, rows)
        if last is None:
            _start_rebuild(model, _connection_path(conn))
            return cache
        cache["max_rowid"] = last

    # 改文件、删数据源会在库里留下失效 chunk；矩阵里的旧行先保留，超过阈值再压缩。
    cache["count"] = _vector_cache_key(conn, model)[1]
    stale = cache["rows"] - cache["count"]
    if stale > max(_STALE_MIN, int(cache["count"] * _STALE_RATIO)):
        _start_rebuild(model, _connection_path(conn))
    return cache


def _score_gate(numpy, similarities):
    """把相似度换算成本次查询内的 z 分数，并按门槛筛出候选。

    向量太少时分布没有参考价值，退回“相似度为正”的旧口径。
    """
    count = len(similarities)
    if count == 0:
        return []
    if count < Z_MIN_POPULATION:
        mean, deviation = 0.0, 1.0
        keep = similarities > 0
    else:
        mean = float(similarities.mean())
        deviation = max(float(similarities.std()), 0.02)
        keep = (similarities - mean) / deviation >= Z_GATE
    z_values = (similarities - mean) / max(deviation, 0.02)
    z_values[~keep] = -numpy.inf
    return z_values


def _cached_scores(conn: sqlite3.Connection, model: str, query_vector: list[float],
                   top_k: int, allowed_assets: set[str] | None = None,
                   excluded_assets: set[str] | None = None):
    """返回 [(similarity, z, chunk_id)]；numpy 不可用时返回 None 走原 SQL 扫描。

    项目范围在内存矩阵上做掩码后再算 z 分数：既不重读数据库，也保证小项目
    有自己的候选分布，不会被全局高相似片段挤掉。
    """
    numpy = _numpy_module()
    if numpy is None:
        return None
    with _VECTOR_CACHE_LOCK:
        cache = _refresh_cache(conn, model)
        rows = cache["rows"]
        if not rows:
            return []
        matrix = cache["matrix"][:rows]
        query = numpy.asarray(query_vector, dtype=numpy.float32)
        query_norm = float(numpy.linalg.norm(query))
        query_peak = float(numpy.max(numpy.abs(query))) if query.size else 0.0
        if query_norm <= 0 or query_peak <= 0:
            return []
        query_scale = query_peak / 127.0
        quantized = numpy.rint(query / query_scale)
        numpy.clip(quantized, -127, 127, out=quantized)
        quantized = quantized.astype(numpy.float32)
        scores = numpy.empty(rows, dtype=numpy.float32)
        # int8 矩阵分块转 float32 再乘，内存占用小，整库约 0.2 秒。
        for start in range(0, rows, 16384):
            block = matrix[start:start + 16384].astype(numpy.float32)
            scores[start:start + 16384] = block @ quantized
        scores *= (query_scale / query_norm)
        scores /= cache["norms"][:rows]
        scores *= cache["scales"][:rows]

        scoped = allowed_assets is not None or bool(excluded_assets)
        if not scoped:
            z_values = _score_gate(numpy, scores)
            candidates = numpy.flatnonzero(z_values > -numpy.inf)
            if not candidates.size:
                return []
            take = min(len(candidates), max(1, int(top_k)))
            if take < len(candidates):
                picked = numpy.argpartition(-z_values[candidates], take - 1)[:take]
                candidates = candidates[picked]
            order = candidates[numpy.argsort(-z_values[candidates])]
            return [(float(scores[index]), float(z_values[index]), cache["ids"][index])
                    for index in order]

        assets = cache["asset_ids"][:rows]
        blocked = excluded_assets or set()
        if allowed_assets is None:
            visible = numpy.asarray(
                [asset not in blocked for asset in assets], dtype=bool)
        else:
            visible = numpy.asarray(
                [asset in allowed_assets and asset not in blocked
                 for asset in assets], dtype=bool)
        if not visible.any():
            return []
        indexes = numpy.flatnonzero(visible)
        z_values = _score_gate(numpy, scores[indexes])
        keep = z_values > -numpy.inf
        if not keep.any():
            return []
        candidates = indexes[keep]
        z_values = z_values[keep]
        take = min(len(candidates), max(1, int(top_k)))
        if take < len(candidates):
            picked = numpy.argpartition(-z_values, take - 1)[:take]
            candidates = candidates[picked]
            z_values = z_values[picked]
        order = numpy.argsort(-z_values)
        return [(float(scores[candidates[index]]), float(z_values[index]),
                 cache["ids"][candidates[index]]) for index in order]


def warm_cache_async() -> None:
    """后台预热向量缓存与本地向量模型；有新增向量时顺带追加，不重复整库重建。"""
    def worker() -> None:
        conn = None
        try:
            conn = db.connect()
            settings = load_settings(conn)
            if not enabled(settings):
                return
            if settings["provider"] == "ollama":
                # 让本地服务先把模型加载进内存，首个检索不必等冷启动。
                try:
                    embed_texts(settings, ["检索预热"])
                except EmbeddingError:
                    pass
            with _VECTOR_CACHE_LOCK:
                _refresh_cache(conn, settings["model"])
        except Exception:  # noqa: BLE001 - 预热失败不影响功能，查询时会再处理
            return
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=worker, daemon=True, name="embedding-warm").start()


_SCOPE_ALLOWED = "_shiyuan_scope_allowed"
_SCOPE_EXCLUDED = "_shiyuan_scope_excluded"


def _scope_clauses(conn: sqlite3.Connection, allowed_assets: set[str] | None,
                   excluded_assets: set[str] | None) -> list[str]:
    """把项目范围放进 SQL：先过滤再算相似度，避免全局 Top-K 挤掉项目内命中的片段。

    资产集合可能很大，用临时表传入，避免拼接超长 IN 列表。
    """
    clauses: list[str] = []
    if allowed_assets is not None:
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_SCOPE_ALLOWED}"
                     " (asset_id TEXT PRIMARY KEY)")
        conn.execute(f"DELETE FROM {_SCOPE_ALLOWED}")
        conn.executemany(f"INSERT OR IGNORE INTO {_SCOPE_ALLOWED} (asset_id) VALUES (?)",
                         [(asset_id,) for asset_id in allowed_assets])
        clauses.append(f"AND e.asset_id IN (SELECT asset_id FROM {_SCOPE_ALLOWED})")
    if excluded_assets:
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_SCOPE_EXCLUDED}"
                     " (asset_id TEXT PRIMARY KEY)")
        conn.execute(f"DELETE FROM {_SCOPE_EXCLUDED}")
        conn.executemany(f"INSERT OR IGNORE INTO {_SCOPE_EXCLUDED} (asset_id) VALUES (?)",
                         [(asset_id,) for asset_id in excluded_assets])
        clauses.append(f"AND e.asset_id NOT IN (SELECT asset_id FROM {_SCOPE_EXCLUDED})")
    return clauses


def _drop_scope_tables(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_SCOPE_ALLOWED}")
    conn.execute(f"DROP TABLE IF EXISTS {_SCOPE_EXCLUDED}")


def semantic_chunks(conn: sqlite3.Connection, text: str, *, source_id: str | None = None,
                    ext: str | None = None, top_k: int = 30,
                    allowed_assets: set[str] | None = None,
                    excluded_assets: set[str] | None = None,
                    mtime_after_ns: int | None = None) -> list[dict]:
    settings = load_settings(conn)
    if not enabled(settings):
        return []
    if allowed_assets is not None and not allowed_assets:
        return []
    try:
        query_vector = _query_vector(settings, text)
    except EmbeddingError as exc:
        _set_last_error(conn, str(exc))
        raise
    # 查询成功说明接口已恢复，不能让旧的“无法连接”一直挂在设置页上。
    _set_last_error(conn, None)

    # 两段式：先只取 chunk_id + 向量做相似度，最后只回表取前 top_k 条的正文与路径。
    sql = [
        "SELECT e.chunk_id, e.vector",
        "FROM chunk_embeddings e",
        "JOIN chunks c ON c.chunk_id = e.chunk_id",
        "JOIN content_versions v ON v.version_id = c.version_id",
        "JOIN asset_locations l ON l.asset_id = c.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        "WHERE e.model = ? AND v.is_current = 1"
        " AND l.availability = 'AVAILABLE' AND s.state = 'active'",
    ]
    params: list = [settings["model"]]
    if source_id:
        sql.append("AND l.source_id = ?")
        params.append(source_id)
    if ext:
        sql.append("AND l.ext = ?")
        params.append(ext.lower().lstrip("."))
    if mtime_after_ns:
        # 时间过滤必须进 SQL：全库向量缓存不区分 mtime，不能直接复用。
        sql.append("AND l.mtime_ns >= ?")
        params.append(int(mtime_after_ns))

    scoped = allowed_assets is not None or bool(excluded_assets)
    scored: list[tuple[float, float, str]] | None = None
    if source_id is None and ext is None and mtime_after_ns is None:
        cached = _cached_scores(
            conn, settings["model"], query_vector, top_k * 2,
            allowed_assets, excluded_assets)
        if cached is not None:
            # 缓存里可能含已失效片段，回表时会过滤，所以多取一倍候选。
            scored = cached
    if scored is None:
        # 带数据源/类型过滤时无法走全库缓存：同样按本次候选分布做 z 校准。
        similarities: list[float] = []
        chunk_ids: list[str] = []
        numpy = _numpy_module()
        sql.extend(_scope_clauses(conn, allowed_assets, excluded_assets))
        try:
            if numpy is not None:
                query = numpy.asarray(query_vector, dtype=numpy.float32)
                query_norm = float(numpy.linalg.norm(query))
                if query_norm <= 0:
                    return []
                for row in conn.execute(" ".join(sql), params):
                    vector = numpy.frombuffer(row["vector"], dtype=numpy.float32)
                    if vector.size != query.size:
                        continue
                    norm = float(numpy.linalg.norm(vector))
                    if norm <= 0:
                        continue
                    similarities.append(float(numpy.dot(query, vector)) / (query_norm * norm))
                    chunk_ids.append(row["chunk_id"])
            else:
                for row in conn.execute(" ".join(sql), params):
                    similarities.append(_cosine(query_vector, _unpack(row["vector"])))
                    chunk_ids.append(row["chunk_id"])
        finally:
            if scoped:
                _drop_scope_tables(conn)
        if not similarities:
            return []
        if numpy is not None:
            z_values = _score_gate(numpy, numpy.asarray(similarities, dtype=numpy.float32))
            scored = [
                (similarities[index], float(z_values[index]), chunk_ids[index])
                for index in range(len(chunk_ids))
                if z_values[index] > -numpy.inf
            ]
        else:
            count = len(similarities)
            mean = sum(similarities) / count
            deviation = math.sqrt(sum((value - mean) ** 2 for value in similarities) / count)
            deviation = max(deviation, 0.02)
            threshold = mean + Z_GATE * deviation if count >= Z_MIN_POPULATION else 0.0
            scored = [
                (similarity, (similarity - mean) / deviation, chunk_ids[index])
                for index, similarity in enumerate(similarities)
                if (similarity >= threshold if count >= Z_MIN_POPULATION else similarity > 0)
            ]
        scored.sort(key=lambda item: (-item[1], item[2]))
        scored = scored[:top_k]
    if not scored:
        return []

    ids = [chunk_id for _, _, chunk_id in scored]
    placeholders = ",".join("?" for _ in ids)
    meta_sql = [
        "SELECT c.chunk_id, c.text, c.page, c.sheet, c.bbox, c.asset_id, c.version_id,"
        " l.location_id, l.path, l.mtime_ns, a.parser_signature",
        "FROM chunks c",
        "LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id",
        "JOIN content_versions v ON v.version_id = c.version_id",
        "JOIN asset_locations l ON l.asset_id = c.asset_id",
        "JOIN sources s ON s.source_id = l.source_id",
        f"WHERE c.chunk_id IN ({placeholders}) AND v.is_current = 1"
        " AND l.availability = 'AVAILABLE' AND s.state = 'active'",
    ]
    meta_params: list = [*ids]
    if source_id:
        meta_sql.append("AND l.source_id = ?")
        meta_params.append(source_id)
    if ext:
        meta_sql.append("AND l.ext = ?")
        meta_params.append(ext.lower().lstrip("."))
    rows: dict[str, sqlite3.Row] = {}
    for row in conn.execute(" ".join(meta_sql), meta_params):
        rows.setdefault(row["chunk_id"], row)

    hits = []
    for similarity, z_score, chunk_id in scored:
        if len(hits) >= top_k:
            break
        row = rows.get(chunk_id)
        if row is None:
            continue
        try:
            bbox = json.loads(row["bbox"]) if row["bbox"] else None
        except (TypeError, ValueError):
            bbox = None
        hits.append({
            "chunk_id": row["chunk_id"],
            "asset_id": row["asset_id"],
            "location_id": row["location_id"],
            "path": row["path"],
            "text": row["text"] or "",
            "page": row["page"],
            "sheet": row["sheet"],
            "parser_signature": row["parser_signature"] or "",
            "similarity": similarity,
            "z": z_score,
            "version_id": row["version_id"] or "",
            "mtime_ns": row["mtime_ns"] or 0,
            "bbox": bbox,
            "ocr": (row["parser_signature"] or "").startswith("l2."),
        })
    return hits
