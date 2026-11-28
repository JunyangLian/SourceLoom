"""衍生知识生成：层级摘要 / 实体关系 / FAQ。

所有衍生知识由已配置的生成模型（与回答共用设置）批量产出，解析完成即入队，
后台 worker 逐条消费；每天受 knowledge 预算限制，失败自动重试，超过
MAX_ATTEMPTS 标记 failed。内容重新解析后再次入队会重置任务并重新生成。

生成失败只影响衍生知识，问答在 answer.retrieve 里自动回退本地检索。
"""

from __future__ import annotations

import array
import json
import math
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import budget, config, db, store

KINDS = {"summary", "entities", "questions"}
SCOPE_TYPES = {"document", "family"}
MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT = 60.0
TEXT_LIMIT = 20_000

SUMMARY_SYSTEM = (
    "你是本地知识库的归纳助手。只依据用户消息中的<原文>做归纳，不得新增原文"
    "没有的事实、数字或评价；原文没讲的内容不要推测。\n"
    '只输出 JSON，格式为 {"title": "一句话标题", "summary": "300 字以内的整体归纳"}\n'
    "不要输出解释或 JSON 以外的内容。"
)

ENTITIES_SYSTEM = (
    "你是实体与关系抽取助手。只依据用户消息中的<原文>提取重要实体（人物、组织、"
    "项目、文件、材料、方法、指标、地点等）并判断实体之间的明确关系，不得新增原文"
    "没有的内容；没有明确关系就输出空数组。\n"
    '只输出 JSON，格式为 {"entities": [{"name": "实体名", "type": "类型",'
    ' "description": "一句话说明"}], "relations": [{"subject": "实体A",'
    ' "predicate": "关系", "object": "实体B", "confidence": 0.9}]}'
    "，confidence 取值 0 到 1。\n"
    "不要输出解释或 JSON 以外的内容。"
)

QUESTIONS_SYSTEM = (
    "你是问答对生成助手。根据用户消息中的<归纳>与<实体>，生成用户可能询问的"
    "问题与答案（包含口语化的问法和实体之间的关系类问法）。答案只能来自给定内容，"
    "不得新增事实。\n"
    '只输出 JSON，格式为 {"questions": [{"question": "问法", "answer": "答案"}]}\n'
    "不要输出解释或 JSON 以外的内容。"
)

# 生成链：摘要完成 → 实体+关系 → FAQ；每种知识完成即入队下一种。
KIND_CHAIN = {"summary": "entities", "entities": "questions"}


class KnowledgeError(RuntimeError):
    pass


def generated_enabled(settings: dict) -> bool:
    """与回答模型共用配置；本地摘录模式不生成衍生知识。"""
    provider = settings.get("provider")
    if provider == "openai":
        return bool(settings.get("allow_cloud") and settings.get("api_key")
                    and settings.get("model"))
    if provider == "ollama":
        return bool(settings.get("model"))
    return False


# ---------------------------------------------------------------- 模型调用

def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise KnowledgeError(f"生成接口返回 {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise KnowledgeError(f"无法连接生成接口：{exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise KnowledgeError("生成接口返回了非 JSON 内容") from exc


def _content_of(settings: dict, data: dict) -> str:
    if settings.get("provider") == "ollama":
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise KnowledgeError("Ollama 返回结构不符合预期") from exc
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise KnowledgeError("生成接口返回结构不符合 OpenAI 兼容格式") from exc


def _request(settings: dict, messages: list[dict], timeout: float) -> str:
    """调用生成模型，返回文本内容；测试通过 monkeypatch 替换。"""
    if settings.get("provider") == "ollama":
        base = (settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        data = _post_json(f"{base}/api/chat", {
            "model": settings["model"], "stream": False, "format": "json",
            "options": {"temperature": 0}, "messages": messages,
        }, {}, timeout)
    else:
        base = (settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        data = _post_json(f"{base}/chat/completions", {
            "model": settings["model"], "temperature": 0, "max_tokens": 2000,
            "messages": messages,
        }, {"Authorization": f"Bearer {settings['api_key']}"}, timeout)
    return _content_of(settings, data)


def _parse_json(content: str) -> dict:
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("生成结果不是 JSON 对象")
    return data


def timeout() -> float:
    try:
        return float(os.environ.get("SHIYUAN_KNOWLEDGE_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


# ---------------------------------------------------------------- 内容取样

def _uses_remote_model(settings: dict) -> bool:
    """生成模型是否在远端：只有本机地址才算本地模型。"""
    base = (settings.get("base_url") or "").lower()
    if not base:
        base = ("http://127.0.0.1:11434" if settings.get("provider") == "ollama"
                else "https://api.openai.com/v1")
    return not any(host in base for host in ("127.0.0.1", "localhost", "::1"))


def scope_is_local_only(conn: sqlite3.Connection, scope_type: str,
                        scope_id: str) -> bool:
    """该知识范围是否属于 local_only 项目：属于则不得把正文送外部模型。"""
    if scope_type == "document":
        asset_ids = [scope_id]
    else:
        asset_ids = [
            row["asset_id"] for row in conn.execute(
                "SELECT asset_id FROM document_family_members WHERE family_id = ?",
                (scope_id,))
        ]
    if not asset_ids:
        return False
    marks = ",".join("?" for _ in asset_ids)
    direct = conn.execute(
        "SELECT 1 FROM projects p JOIN project_assets pa ON pa.project_id = p.project_id"
        " WHERE p.status = 'active' AND p.privacy_policy = 'local_only'"
        " AND pa.status = 'active' AND pa.membership_type != 'excluded'"
        f" AND pa.asset_id IN ({marks}) LIMIT 1",
        asset_ids,
    ).fetchone()
    if direct is not None:
        return True
    by_source = conn.execute(
        "SELECT 1 FROM projects p JOIN project_sources ps ON ps.project_id = p.project_id"
        " JOIN asset_locations l ON l.source_id = ps.source_id"
        " WHERE p.status = 'active' AND p.privacy_policy = 'local_only'"
        f" AND l.asset_id IN ({marks}) LIMIT 1",
        asset_ids,
    ).fetchone()
    return by_source is not None


def scope_text(conn: sqlite3.Connection, scope_type: str, scope_id: str) -> str:
    """取文档/家族当前版本的正文拼接，上限 TEXT_LIMIT 字符。"""
    if scope_type == "document":
        sql = ("SELECT c.text FROM chunks c"
               " JOIN content_versions v ON v.version_id = c.version_id"
               " WHERE c.asset_id = ? AND v.is_current = 1 ORDER BY c.ordinal")
        params = (scope_id,)
    else:
        sql = ("SELECT c.text FROM chunks c"
               " JOIN document_family_members m ON m.asset_id = c.asset_id"
               " JOIN content_versions v ON v.version_id = c.version_id"
               " WHERE m.family_id = ? AND v.is_current = 1 ORDER BY c.ordinal")
        params = (scope_id,)
    parts: list[str] = []
    total = 0
    for row in conn.execute(sql, params):
        piece = row["text"] or ""
        parts.append(piece)
        total += len(piece)
        if total >= TEXT_LIMIT:
            break
    return "\n".join(parts)[:TEXT_LIMIT]


# ---------------------------------------------------------------- 任务账本

def enqueue(conn: sqlite3.Connection, kind: str, scope_type: str,
            scope_id: str) -> bool:
    """入队一种衍生知识；已 pending/running 的任务不重复，done/failed 会被重置。"""
    if kind not in KINDS:
        raise ValueError(f"未知的知识类型：{kind}")
    if scope_type not in SCOPE_TYPES:
        raise ValueError(f"未知的范围类型：{scope_type}")
    existing = conn.execute(
        "SELECT status FROM knowledge_jobs WHERE kind = ? AND scope_id = ?",
        (kind, scope_id),
    ).fetchone()
    if existing is not None and existing["status"] in ("pending", "running"):
        return False
    now = store.now_iso()
    conn.execute(
        "INSERT INTO knowledge_jobs"
        " (job_id, kind, scope_type, scope_id, model, status, attempts, error, updated_at)"
        " VALUES (?, ?, ?, ?, NULL, 'pending', 0, NULL, ?)"
        " ON CONFLICT(kind, scope_id) DO UPDATE SET scope_type = excluded.scope_type,"
        " status = 'pending', attempts = 0, error = NULL, updated_at = excluded.updated_at",
        (store.new_id("kj"), kind, scope_type, scope_id, now),
    )
    conn.commit()
    return True


def job_counts(conn: sqlite3.Connection) -> dict:
    return {
        f"{row['status']}": row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM knowledge_jobs GROUP BY status")
    }


def counts(conn: sqlite3.Connection) -> dict:
    return {
        "summaries": conn.execute(
            "SELECT COUNT(*) FROM knowledge_summaries WHERE status = 'ok'").fetchone()[0],
        "entities": conn.execute(
            "SELECT COUNT(*) FROM knowledge_entities").fetchone()[0],
        "relations": conn.execute(
            "SELECT COUNT(*) FROM knowledge_relations").fetchone()[0],
        "questions": conn.execute(
            "SELECT COUNT(*) FROM knowledge_questions").fetchone()[0],
        "pending": conn.execute(
            "SELECT COUNT(*) FROM knowledge_jobs WHERE status IN ('pending', 'running')"
        ).fetchone()[0],
        "failed": conn.execute(
            "SELECT COUNT(*) FROM knowledge_jobs WHERE status = 'failed'").fetchone()[0],
    }


def stats(conn: sqlite3.Connection) -> dict:
    return {
        **counts(conn),
        "jobs": job_counts(conn),
        "budget": budget.info(conn),
    }


# ---------------------------------------------------------------- 生成

def _generate_summary(settings: dict, text: str) -> dict:
    if not text.strip():
        raise KnowledgeError("原文为空，无法归纳")
    payload = _parse_json(_request(settings, [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": f"<原文>\n{text}"},
    ], timeout()))
    title = str(payload.get("title") or "").strip()[:200]
    summary = str(payload.get("summary") or "").strip()[:2000]
    if not title or not summary:
        raise KnowledgeError("模型没有返回 title / summary")
    return {"title": title, "summary": summary}


def _generate_entities(settings: dict, text: str) -> dict:
    if not text.strip():
        raise KnowledgeError("原文为空，无法提取实体")
    payload = _parse_json(_request(settings, [
        {"role": "system", "content": ENTITIES_SYSTEM},
        {"role": "user", "content": f"<原文>\n{text}"},
    ], timeout()))
    result = {"entities": [], "relations": []}
    for item in payload.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:120]
        if not name:
            continue
        result["entities"].append({
            "name": name,
            "type": str(item.get("type") or "").strip()[:40],
            "description": str(item.get("description") or "").strip()[:300],
        })
    for item in payload.get("relations") or []:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()[:120]
        predicate = str(item.get("predicate") or "").strip()[:60]
        object_ = str(item.get("object") or "").strip()[:120]
        if not subject or not predicate or not object_:
            continue
        try:
            confidence = max(0.0, min(float(item.get("confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            confidence = 0.5
        result["relations"].append({
            "subject": subject, "predicate": predicate, "object": object_,
            "confidence": confidence,
        })
    return result


def _generate_questions(conn: sqlite3.Connection, settings: dict,
                        scope_type: str, scope_id: str) -> dict:
    row = conn.execute(
        "SELECT title, summary FROM knowledge_summaries"
        " WHERE scope_type = ? AND scope_id = ? AND status = 'ok'",
        (scope_type, scope_id),
    ).fetchone()
    if row is None:
        raise KnowledgeError("缺少该范围的摘要，先生成摘要")
    entities = [
        item["name"] for item in conn.execute(
            "SELECT name FROM knowledge_entities WHERE family_id = ? ORDER BY name",
            (scope_id,),
        ).fetchall()
    ]
    payload = _parse_json(_request(settings, [
        {"role": "system", "content": QUESTIONS_SYSTEM},
        {"role": "user",
         "content": f"<归纳>\n标题：{row['title']}\n{row['summary']}\n\n"
                    f"<实体>\n{'、'.join(entities) if entities else '（无）'}"},
    ], timeout()))
    questions = []
    seen: set[str] = set()
    for item in payload.get("questions") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()[:200]
        answer = str(item.get("answer") or "").strip()[:800]
        if not question or not answer or question in seen:
            continue
        seen.add(question)
        questions.append({"question": question, "answer": answer})
    if not questions:
        raise KnowledgeError("模型没有返回任何问答")
    return {"questions": questions}


def _generate(conn: sqlite3.Connection, settings: dict,
              job: sqlite3.Row) -> dict:
    if job["kind"] == "summary":
        return _generate_summary(settings, scope_text(
            conn, job["scope_type"], job["scope_id"]))
    if job["kind"] == "entities":
        return _generate_entities(settings, scope_text(
            conn, job["scope_type"], job["scope_id"]))
    if job["kind"] == "questions":
        return _generate_questions(conn, settings, job["scope_type"], job["scope_id"])
    raise KnowledgeError(f"暂不支持的知识类型：{job['kind']}")


def _store(conn: sqlite3.Connection, job: sqlite3.Row, settings: dict,
           result: dict) -> None:
    now = store.now_iso()
    scope_id = job["scope_id"]
    if job["kind"] == "summary":
        conn.execute(
            "INSERT INTO knowledge_summaries"
            " (summary_id, scope_type, scope_id, title, summary, model, status, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)"
            " ON CONFLICT(scope_type, scope_id) DO UPDATE SET"
            " title = excluded.title, summary = excluded.summary,"
            " model = excluded.model, status = 'ok', updated_at = excluded.updated_at",
            (store.new_id("ks"), job["scope_type"], scope_id,
             result["title"], result["summary"], settings.get("model"), now),
        )
        return
    if job["kind"] == "entities":
        conn.execute("DELETE FROM knowledge_entities WHERE family_id = ?", (scope_id,))
        conn.execute("DELETE FROM knowledge_relations WHERE family_id = ?", (scope_id,))
        for entity in result["entities"]:
            conn.execute(
                "INSERT INTO knowledge_entities"
                " (entity_id, family_id, name, type, description, model, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (store.new_id("ke"), scope_id, entity["name"], entity["type"],
                 entity["description"], settings.get("model"), now),
            )
        for relation in result["relations"]:
            conn.execute(
                "INSERT INTO knowledge_relations"
                " (relation_id, family_id, subject, predicate, object, confidence,"
                "  evidence, model, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?)",
                (store.new_id("kr"), scope_id, relation["subject"],
                 relation["predicate"], relation["object"], relation["confidence"],
                 settings.get("model"), now),
            )
        return
    if job["kind"] == "questions":
        old_ids = [
            row["question_id"] for row in conn.execute(
                "SELECT question_id FROM knowledge_questions WHERE family_id = ?",
                (scope_id,),
            ).fetchall()
        ]
        for question_id in old_ids:
            conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_id = ?",
                         (question_id,))
        conn.execute("DELETE FROM knowledge_questions WHERE family_id = ?", (scope_id,))
        question_ids = []
        for item in result["questions"]:
            question_id = store.new_id("kq")
            conn.execute(
                "INSERT INTO knowledge_questions"
                " (question_id, family_id, question, answer, related_entity_ids,"
                "  model, updated_at) VALUES (?, ?, ?, ?, '[]', ?, ?)",
                (question_id, scope_id, item["question"], item["answer"],
                 settings.get("model"), now),
            )
            question_ids.append((question_id, item["question"]))
        _store_vectors(conn, settings, question_ids, now)
        return
    raise KnowledgeError(f"暂不支持的知识类型：{job['kind']}")


def _store_vectors(conn: sqlite3.Connection, settings: dict,
                   question_ids: list[tuple[str, str]], now: str) -> None:
    """FAQ 生成后经 embedding 写知识向量；向量未启用则跳过。"""
    if not question_ids:
        return
    from . import embeddings

    embed_settings = embeddings.load_settings(conn)
    if not embeddings.enabled(embed_settings):
        return
    texts = [question for _, question in question_ids]
    try:
        vectors = embeddings.embed_texts(embed_settings, texts)
    except embeddings.EmbeddingError:
        return
    for (question_id, _), vector in zip(question_ids, vectors):
        if len(vector) == 0:
            continue
        conn.execute(
            "INSERT INTO knowledge_vectors (knowledge_id, kind, model, dim, vector, updated_at)"
            " VALUES (?, 'question', ?, ?, ?, ?)",
            (question_id, embed_settings["model"], len(vector),
             embeddings._pack(vector), now),
        )


def _finish(conn: sqlite3.Connection, job: sqlite3.Row, status: str,
            settings: dict | None, error: str | None) -> None:
    model = settings.get("model") if settings is not None else job["model"]
    conn.execute(
        "UPDATE knowledge_jobs SET status = ?, error = ?, model = ?, updated_at = ?"
        " WHERE job_id = ?",
        (status, error, model, store.now_iso(), job["job_id"]),
    )
    conn.commit()
    if status == "done":
        _chain_next(conn, job)


def _chain_next(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    next_kind = KIND_CHAIN.get(job["kind"])
    if next_kind:
        enqueue(conn, next_kind, job["scope_type"], job["scope_id"])


def run_round(conn_or_path) -> dict:
    """处理一轮 pending 任务。参数可以是连接或库文件路径。"""
    conn = conn_or_path
    close = False
    if not isinstance(conn, sqlite3.Connection):
        conn = db.connect(conn_or_path)
        close = True
    try:
        counters = {"generated": 0, "failed": 0, "budget": 0}
        from . import answer

        settings = answer.load_settings(conn)
        if not generated_enabled(settings):
            return {**counters, "message": "未配置生成模型，衍生知识暂不生成"}
        remaining = budget.remaining_knowledge(conn)
        counters["budget"] = remaining
        if remaining <= 0:
            return {**counters, "message": "今日知识预算已用完"}
        rows = conn.execute(
            "SELECT * FROM knowledge_jobs WHERE status = 'pending'"
            " AND attempts < ? ORDER BY updated_at LIMIT ?",
            (MAX_ATTEMPTS, remaining),
        ).fetchall()
        for job in rows:
            if budget.remaining_knowledge(conn) <= 0:
                counters["budget"] = 0
                break
            if _uses_remote_model(settings) and scope_is_local_only(
                    conn, job["scope_type"], job["scope_id"]):
                # local_only 项目的正文不能离开本机；跳过并记录原因，不消耗预算。
                _finish(conn, job, "failed", settings,
                        "该项目仅允许本地使用，已跳过外部模型生成")
                counters["failed"] += 1
                continue
            attempts = int(job["attempts"])
            conn.execute(
                "UPDATE knowledge_jobs SET status = 'running', attempts = ?,"
                " updated_at = ? WHERE job_id = ?",
                (attempts + 1, store.now_iso(), job["job_id"]),
            )
            conn.commit()
            try:
                result = _generate(conn, settings, job)
                _store(conn, job, settings, result)
                budget.add_knowledge(conn)
                _finish(conn, job, "done", settings, None)
                counters["generated"] += 1
            except Exception as exc:
                exhausted = attempts + 1 >= MAX_ATTEMPTS
                _finish(conn, job, "failed" if exhausted else "pending",
                        settings, str(exc)[:300])
                counters["failed"] += 1
        return counters
    finally:
        if close:
            conn.close()


# ---------------------------------------------------------------- 后台 worker

_state = {"running": False}
_lock = threading.Lock()
_stop = threading.Event()
_worker: threading.Thread | None = None


def start_worker(store_path: str | None = None,
                 poll_seconds: float | None = None) -> bool:
    """启动后台生成线程；已在运行则返回 False。"""
    global _worker
    if poll_seconds is None:
        try:
            poll_seconds = float(os.environ.get("SHIYUAN_KNOWLEDGE_POLL", "10"))
        except ValueError:
            poll_seconds = 10.0
    with _lock:
        if _worker is not None and _worker.is_alive():
            return False
        _stop.clear()

        def run() -> None:
            try:
                while not _stop.wait(poll_seconds):
                    try:
                        run_round(store_path)
                    except Exception:
                        # 单轮失败不影响后续轮次；任务本身有重试上限。
                        pass
            finally:
                with _lock:
                    _state["running"] = False

        _worker = threading.Thread(target=run, name="knowledge-worker", daemon=True)
        _worker.start()
        _state["running"] = True
    return True


def stop_worker() -> None:
    _stop.set()


# ---------------------------------------------------------------- 检索注入

def _scope_label(conn: sqlite3.Connection, scope_type: str,
                 scope_id: str) -> tuple[str, str]:
    """返回（展示名, 文件路径）；路径仅文档级有。"""
    if scope_type == "family":
        row = conn.execute(
            "SELECT title FROM document_families WHERE family_id = ?",
            (scope_id,),
        ).fetchone()
        return (f"文档家族「{row['title']}」" if row else "文档家族", "")
    row = conn.execute(
        "SELECT path FROM asset_locations WHERE asset_id = ?"
        " AND availability = 'AVAILABLE' ORDER BY path LIMIT 1",
        (scope_id,),
    ).fetchone()
    path = row["path"] if row else ""
    name = Path(path).name if path else ""
    return (f"文件：{name}", path)


def summary_matches(conn: sqlite3.Connection, analysis: dict,
                    evidence: list, limit: int = 2,
                    strict: bool = False) -> list[dict]:
    """按问题关键词匹配摘要；优先与已命中证据同家族/同文档的摘要。

    问题没有可用关键词（如「这个文档整体讲了什么」）时按更新时间返回
    最近生成的摘要，供综合类规划兜底。

    strict=True 用于「已有原文证据」的场景：必须命中至少一个 ≥3 字的具体词，
    或两个以上短词，或摘要正好属于已命中证据的文档家族。否则「最后」「模型」
    这类两字 gram 会把高数习题、个人简历的摘要拉进证据。
    """
    variants = [
        variant.lower()
        for concept in analysis.get("concepts", [])
        for variant in concept.get("variants", [])
        if len(variant) >= 2
    ]
    if not variants and analysis.get("core"):
        variants = [analysis["core"].lower()]
    family_ids = {item.family_id for item in evidence if item.family_id}
    asset_ids = {item.asset_id for item in evidence}

    scored = []
    for row in conn.execute(
        "SELECT * FROM knowledge_summaries WHERE status = 'ok'"
    ).fetchall():
        blob = f"{row['title']} {row['summary']}".lower()
        matched = [variant for variant in variants if variant in blob]
        strong = [variant for variant in matched if len(variant) >= 3]
        weak = [variant for variant in matched if len(variant) < 3]
        hits = len(matched)
        scope_hit = (
            (row["scope_type"] == "family" and row["scope_id"] in family_ids)
            or (row["scope_type"] == "document" and row["scope_id"] in asset_ids)
        )
        if not variants:
            hits = 1  # 兜底：问题没有关键词时按生成时间取最近摘要
        if strict:
            if not scope_hit and not strong and len(weak) < 2:
                continue
        elif hits == 0 and not scope_hit:
            continue
        label, path = _scope_label(conn, row["scope_type"], row["scope_id"])
        scored.append({
            "summary_id": row["summary_id"],
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "title": row["title"],
            "summary": row["summary"],
            "label": label,
            "path": path,
            "score": hits * 10 + (5 if scope_hit else 0),
            "updated_at": row["updated_at"],
        })
    scored.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:max(1, min(limit, 4))]


def _variants_of(analysis: dict) -> list[str]:
    variants = [
        variant.lower()
        for concept in analysis.get("concepts", [])
        for variant in concept.get("variants", [])
        if len(variant) >= 2
    ]
    if not variants and analysis.get("core"):
        variants = [analysis["core"].lower()]
    return variants


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


def _like_clause(columns: list[str], variants: list[str]) -> tuple[str, list]:
    clauses, params = [], []
    for variant in variants:
        escaped = variant.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        for column in columns:
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
    return " OR ".join(clauses), params


def _vector_faq_scores(conn: sqlite3.Connection, text: str) -> dict[str, float]:
    """FAQ 向量召回；向量未启用或失败返回空表，上层自动退化为 LIKE。"""
    from . import embeddings

    settings = embeddings.load_settings(conn)
    if not embeddings.enabled(settings):
        return {}
    try:
        query_vector = embeddings.embed_texts(settings, [text])[0]
    except embeddings.EmbeddingError:
        return {}
    scores: dict[str, float] = {}
    for row in conn.execute(
        "SELECT knowledge_id, vector FROM knowledge_vectors WHERE kind = 'question'"
        " AND model = ?", (settings["model"],),
    ):
        values = array.array("f")
        values.frombytes(row["vector"])
        similarity = _cosine(query_vector, values)
        if similarity > 0:
            scores[row["knowledge_id"]] = similarity
    return scores


def faq_matches(conn: sqlite3.Connection, analysis: dict,
                evidence: list | None = None, limit: int = 2,
                strict: bool = False) -> list[dict]:
    """FAQ 匹配：优先向量语义召回，向量未启用时退化为 LIKE 关键词检索。

    strict=True（已有原文证据）时，弱词面命中（「模型」「最后」这类两字 gram）
    和低相似度的向量命中都不算数，避免把无关文档的 FAQ 拉进引用。
    """
    variants = _variants_of(analysis)
    if not variants:
        return []
    family_ids = {item.family_id for item in evidence or [] if item.family_id}
    asset_ids = {item.asset_id for item in evidence or []}
    vector_scores = _vector_faq_scores(
        conn, analysis.get("core") or variants[0])
    floor = 0.60 if strict else 0.55
    rows = conn.execute("SELECT * FROM knowledge_questions").fetchall()
    scored = []
    for row in rows:
        blob = f"{row['question']} {row['answer']}".lower()
        matched = [variant for variant in variants if variant in blob]
        strong = [variant for variant in matched if len(variant) >= 3]
        weak = [variant for variant in matched if len(variant) < 3]
        like_hits = len(matched)
        vector_hit = vector_scores.get(row["question_id"], 0.0)
        scope_hit = (row["family_id"] in family_ids
                     or row["family_id"] in asset_ids)
        if scope_hit:
            pass
        elif vector_hit >= floor:
            pass
        elif strong:
            pass
        elif len(weak) < 2:
            continue
        label, path = _any_scope_label(conn, row["family_id"])
        scored.append({
            "question_id": row["question_id"],
            "family_id": row["family_id"],
            "question": row["question"],
            "answer": row["answer"],
            "label": label,
            "path": path,
            "score": vector_hit * 100 + like_hits * 8,
        })
    scored.sort(key=lambda item: -item["score"])
    return scored[:max(1, min(limit, 4))]


def _any_scope_label(conn: sqlite3.Connection, group_id: str) -> tuple[str, str]:
    """知识表的 family_id 列可能是 family_id 或 asset_id，按存在性识别。"""
    if conn.execute(
        "SELECT 1 FROM document_families WHERE family_id = ?", (group_id,)
    ).fetchone():
        return _scope_label(conn, "family", group_id)
    if conn.execute(
        "SELECT 1 FROM assets WHERE asset_id = ?", (group_id,)
    ).fetchone():
        return _scope_label(conn, "document", group_id)
    return ("已删除的内容", "")


def relation_matches(conn: sqlite3.Connection, analysis: dict,
                     evidence: list | None = None, limit: int = 2,
                     strict: bool = False) -> list[dict]:
    """关系类问题：按实体名关键词匹配知识关系表。

    strict=True 时同样要求具体词命中，避免「模型」「最后」这类两字 gram
    把无关文档的实体关系拉进证据。
    """
    variants = _variants_of(analysis)
    if not variants:
        return []
    family_ids = {item.family_id for item in evidence or [] if item.family_id}
    asset_ids = {item.asset_id for item in evidence or []}
    clause, params = _like_clause(["subject", "predicate", "object"], variants)
    rows = conn.execute(
        f"SELECT * FROM knowledge_relations WHERE {clause}", params).fetchall()
    scored = []
    for row in rows:
        blob = f"{row['subject']} {row['predicate']} {row['object']}".lower()
        matched = [variant for variant in variants if variant in blob]
        strong = [variant for variant in matched if len(variant) >= 3]
        weak = [variant for variant in matched if len(variant) < 3]
        hits = len(matched)
        scope_hit = (row["family_id"] in family_ids
                     or row["family_id"] in asset_ids)
        if strict and not scope_hit and not strong and len(weak) < 2:
            continue
        label, path = _any_scope_label(conn, row["family_id"])
        scored.append({
            "relation_id": row["relation_id"],
            "family_id": row["family_id"],
            "text": (f"{row['subject']} —{row['predicate']}→ {row['object']}"
                     f"（置信度 {round(float(row['confidence']), 2)}）"),
            "label": label,
            "path": path,
            "score": hits,
        })
    scored.sort(key=lambda item: (-item["score"], item["relation_id"]))
    return scored[:max(1, min(limit, 4))]


# ---------------------------------------------------------------- 清理

def cleanup_orphans(conn: sqlite3.Connection) -> dict:
    """删除文档已消失（家族/资产都不存在）的知识孤儿行。"""
    alive = ("q.family_id IN (SELECT family_id FROM document_families)"
             " OR q.family_id IN (SELECT asset_id FROM assets)")
    dead_questions = [
        row["question_id"] for row in conn.execute(
            f"SELECT question_id FROM knowledge_questions q WHERE NOT ({alive})")
    ]
    for question_id in dead_questions:
        conn.execute("DELETE FROM knowledge_vectors WHERE knowledge_id = ?",
                     (question_id,))
    removed = {"questions": len(dead_questions), "entities": 0, "relations": 0}
    for table, key in (("knowledge_questions", "questions"),
                       ("knowledge_relations", "relations"),
                       ("knowledge_entities", "entities")):
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE family_id NOT IN"
            " (SELECT family_id FROM document_families)"
            " AND family_id NOT IN (SELECT asset_id FROM assets)")
        removed[key] += cursor.rowcount
    conn.execute(
        "DELETE FROM knowledge_summaries WHERE"
        " (scope_type = 'family' AND scope_id NOT IN (SELECT family_id FROM document_families))"
        " OR (scope_type = 'document' AND scope_id NOT IN (SELECT asset_id FROM assets))")
    conn.execute(
        "DELETE FROM knowledge_jobs WHERE"
        " (scope_type = 'family' AND scope_id NOT IN (SELECT family_id FROM document_families))"
        " OR (scope_type = 'document' AND scope_id NOT IN (SELECT asset_id FROM assets))")
    conn.commit()
    return removed