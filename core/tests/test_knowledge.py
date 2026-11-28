"""衍生知识层测试：入队、生成、JSON 容错、链式生成、预算与重试、检索注入。

全部用 monkeypatch 假模型，不发起真实网络请求。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from shiyuan import (answer, budget, db, embeddings, indexer, knowledge,
                     planner, scanner, store)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "knowledge.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _source(conn, tmp_path, files: dict[str, str]):
    root = tmp_path / "资料"
    root.mkdir(exist_ok=True)
    for name, content in files.items():
        (root / name).write_text(content, encoding="utf-8")
    return store.add_source(conn, root)


def _fake_model(monkeypatch, payloads: dict[str, str]):
    """把 knowledge._request 换成按 system 提示词分派 JSON 的假模型。"""

    def fake_request(settings, messages, timeout):
        system = messages[0]["content"]
        for marker, payload in payloads.items():
            if marker in system:
                return "额外前缀 " + json.dumps(payload, ensure_ascii=False) + " 后缀"
        raise AssertionError(f"未匹配的提示词：{system[:40]}")

    monkeypatch.setattr(knowledge, "_request", fake_request)


def _enable_model(conn):
    answer.update_settings(conn, {"provider": "ollama", "model": "fake-model"})


def _asset_id(conn) -> str:
    return conn.execute("SELECT asset_id FROM content_versions").fetchone()["asset_id"]


# ---------------------------------------------------------------- 1. 建表与入队

def test_parse_completion_enqueues_document_summary(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    job = conn.execute(
        "SELECT * FROM knowledge_jobs WHERE kind = 'summary' AND scope_type = 'document'"
    ).fetchone()
    assert job is not None
    assert job["scope_id"] == _asset_id(conn)
    assert job["status"] == "pending"


def test_remove_source_purges_knowledge_and_families(conn, tmp_path):
    """撤销授权必须连带清理向量、家族成员与衍生知识，不能留下可读缓存。"""
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    asset_id = _asset_id(conn)
    now = store.now_iso()

    conn.execute(
        "INSERT INTO document_families (family_id, title, rules_version, created_at,"
        " updated_at) VALUES ('fam_1', '记录', 'test', ?, ?)", (now, now))
    conn.execute(
        "INSERT INTO document_family_members (family_id, asset_id) VALUES ('fam_1', ?)",
        (asset_id,))
    conn.execute(
        "INSERT INTO family_content_groups (content_group_id, family_id, normalized_hash,"
        " representative_text, created_at) VALUES ('cg_1', 'fam_1', 'h1', '正文', ?)", (now,))
    conn.execute(
        "INSERT INTO knowledge_summaries (summary_id, scope_type, scope_id, title, summary,"
        " updated_at) VALUES ('s1', 'document', ?, 't', 's', ?)", (asset_id, now))
    # 索引完成时已自动入队文档级摘要任务，这里只补其余类型的知识行。
    conn.execute(
        "INSERT INTO knowledge_entities (entity_id, family_id, name, updated_at)"
        " VALUES ('e1', ?, '甲', ?)", (asset_id, now))
    conn.execute(
        "INSERT INTO knowledge_questions (question_id, family_id, question, answer, updated_at)"
        " VALUES ('q1', ?, '问', '答', ?)", (asset_id, now))
    conn.execute(
        "INSERT INTO knowledge_vectors (knowledge_id, kind, model, dim, vector, updated_at)"
        " VALUES ('q1', 'question', 'm', 2, X'0000', ?)", (now,))
    conn.execute(
        "INSERT INTO chunk_embeddings (chunk_id, asset_id, model, dim, vector, updated_at)"
        " SELECT chunk_id, asset_id, 'm', 2, X'0000', ? FROM chunks LIMIT 1", (now,))
    conn.commit()

    store.remove_source(conn, source["source_id"])

    for table in ("knowledge_summaries", "knowledge_jobs", "knowledge_entities",
                  "knowledge_questions", "knowledge_vectors", "chunk_embeddings",
                  "document_family_members", "family_content_groups",
                  "document_families"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_enqueue_resets_done_job(conn):
    knowledge.enqueue(conn, "summary", "document", "asset_x")
    conn.execute(
        "UPDATE knowledge_jobs SET status = 'done', attempts = 1"
        " WHERE kind = 'summary' AND scope_id = 'asset_x'")
    conn.commit()

    assert knowledge.enqueue(conn, "summary", "document", "asset_x") is True
    job = conn.execute(
        "SELECT * FROM knowledge_jobs WHERE kind = 'summary' AND scope_id = 'asset_x'"
    ).fetchone()
    assert job["status"] == "pending"
    assert job["attempts"] == 0


def test_enqueue_skips_pending_job(conn):
    knowledge.enqueue(conn, "summary", "document", "asset_x")
    assert knowledge.enqueue(conn, "summary", "document", "asset_x") is False


# ---------------------------------------------------------------- 2. 摘要生成

def test_summary_generation_with_json_tolerance(conn, tmp_path, monkeypatch):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    asset_id = _asset_id(conn)
    _enable_model(conn)
    _fake_model(monkeypatch, {
        "归纳助手": {"title": "样本量实验记录", "summary": "记录了样本量 42 的实验。"},
    })

    result = knowledge.run_round(conn)

    assert result["generated"] == 1
    row = conn.execute(
        "SELECT * FROM knowledge_summaries WHERE scope_type = 'document'"
        " AND scope_id = ?", (asset_id,)).fetchone()
    assert row is not None and row["status"] == "ok"
    assert row["title"] == "样本量实验记录"
    job = conn.execute(
        "SELECT status FROM knowledge_jobs WHERE kind = 'summary'").fetchone()
    assert job["status"] == "done"
    # 链式生成：摘要完成后自动入队实体。
    next_job = conn.execute(
        "SELECT * FROM knowledge_jobs WHERE kind = 'entities'"
        " AND scope_id = ?", (asset_id,)).fetchone()
    assert next_job is not None and next_job["status"] == "pending"


def test_run_round_without_model_leaves_jobs_pending(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    result = knowledge.run_round(conn)

    assert "未配置生成模型" in result["message"]
    job = conn.execute("SELECT status FROM knowledge_jobs").fetchone()
    assert job["status"] == "pending"


# ---------------------------------------------------------------- 3. 实体 / 关系

def test_entities_and_relations_parsed_and_unique(conn, tmp_path, monkeypatch):
    source = _source(conn, tmp_path, {"记录.md": "用试剂 A 完成样本量 42 的实验。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    asset_id = _asset_id(conn)
    _enable_model(conn)
    knowledge.enqueue(conn, "entities", "document", asset_id)
    _fake_model(monkeypatch, {
        "实体与关系": {
            "entities": [{"name": "试剂 A", "type": "材料", "description": "实验试剂"},
                         {"name": "样本量", "type": "指标", "description": "42"}],
            "relations": [{"subject": "试剂 A", "predicate": "用于",
                           "object": "样本量", "confidence": 0.9}],
        },
    })

    result = knowledge.run_round(conn)

    assert result["generated"] == 1
    entities = conn.execute(
        "SELECT name FROM knowledge_entities WHERE family_id = ? ORDER BY name",
        (asset_id,)).fetchall()
    assert [row["name"] for row in entities] == ["样本量", "试剂 A"]
    relation = conn.execute(
        "SELECT * FROM knowledge_relations WHERE family_id = ?", (asset_id,)).fetchone()
    assert relation is not None
    assert relation["subject"] == "试剂 A" and relation["object"] == "样本量"
    assert relation["confidence"] == 0.9
    # 重复生成会先清后写，唯一键不冲突。
    knowledge.enqueue(conn, "entities", "document", asset_id)
    assert knowledge.run_round(conn)["generated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0] == 1


# ---------------------------------------------------------------- 4. FAQ 与向量

def test_faq_generation_stores_vectors(conn, tmp_path, monkeypatch):
    source = _source(conn, tmp_path, {"记录.md": "用试剂 A 完成样本量 42 的实验。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    asset_id = _asset_id(conn)
    _enable_model(conn)
    conn.execute(
        "INSERT INTO knowledge_summaries (summary_id, scope_type, scope_id, title,"
        " summary, model, status, updated_at) VALUES ('ks1', 'document', ?, '实验记录',"
        " '用试剂 A 完成样本量 42 的实验。', 'fake', 'ok', '2026-01-01')", (asset_id,))
    conn.execute(
        "INSERT INTO knowledge_entities (entity_id, family_id, name, type, description,"
        " model, updated_at) VALUES ('ke1', ?, '试剂 A', '材料', '', 'fake', '2026-01-01')",
        (asset_id,))
    conn.commit()
    embeddings.update_settings(conn, {"provider": "ollama", "model": "fake-embed"})
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda settings, texts: [[0.1, 0.2, 0.3] for _ in texts])
    knowledge.enqueue(conn, "questions", "document", asset_id)
    _fake_model(monkeypatch, {
        "问答对": {"questions": [
            {"question": "样本量是多少？", "answer": "42。"},
            {"question": "试剂 A 和样本量什么关系？", "answer": "试剂 A 用于样本量实验。"},
        ]},
    })

    result = knowledge.run_round(conn)

    assert result["generated"] == 1
    rows = conn.execute(
        "SELECT question FROM knowledge_questions WHERE family_id = ?", (asset_id,)).fetchall()
    assert len(rows) == 2
    vectors = conn.execute("SELECT * FROM knowledge_vectors").fetchall()
    assert len(vectors) == 2
    assert all(row["kind"] == "question" and row["dim"] == 3 for row in vectors)
    assert all(row["model"] == "fake-embed" for row in vectors)


# ---------------------------------------------------------------- 5. 预算与重试

def test_budget_exhausted_stops_generation(conn, tmp_path, monkeypatch):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    _enable_model(conn)
    _fake_model(monkeypatch, {
        "归纳助手": {"title": "t", "summary": "s"},
    })
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('budget', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"date": date.today().isoformat(), "l1": 0, "l2": 0,
                     "knowledge": 10 ** 9}),),
    )
    conn.commit()

    result = knowledge.run_round(conn)

    assert "预算已用完" in result["message"]
    assert result["generated"] == 0
    job = conn.execute("SELECT status FROM knowledge_jobs").fetchone()
    assert job["status"] == "pending"


def test_failed_after_max_attempts(conn, tmp_path, monkeypatch):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    _enable_model(conn)
    monkeypatch.setattr(knowledge, "_request",
                        lambda settings, messages, timeout: (_ for _ in ()).throw(
                            knowledge.KnowledgeError("接口返回 400")))

    for _ in range(knowledge.MAX_ATTEMPTS):
        knowledge.run_round(conn)
    job = conn.execute("SELECT * FROM knowledge_jobs").fetchone()
    assert job["status"] == "failed"
    assert job["attempts"] == knowledge.MAX_ATTEMPTS
    assert "400" in job["error"]
    # 达上限后不再继续消耗。
    assert knowledge.run_round(conn)["generated"] == 0


# ---------------------------------------------------------------- 6. 检索注入

def _seed_knowledge(conn, asset_id: str) -> None:
    conn.execute(
        "INSERT INTO knowledge_summaries (summary_id, scope_type, scope_id, title,"
        " summary, model, status, updated_at) VALUES ('ks1', 'document', ?, '实验记录',"
        " '本实验用试剂 A 完成，样本量为 42。', 'fake', 'ok', '2026-01-01')", (asset_id,))
    conn.execute(
        "INSERT INTO knowledge_relations (relation_id, family_id, subject, predicate,"
        " object, confidence, evidence, model, updated_at) VALUES"
        " ('kr1', ?, '试剂 A', '用于', '样本量实验', 0.9, '', 'fake', '2026-01-01')",
        (asset_id,))
    conn.execute(
        "INSERT INTO knowledge_questions (question_id, family_id, question, answer,"
        " related_entity_ids, model, updated_at) VALUES"
        " ('kq1', ?, '样本量是多少？', '42。', '[]', 'fake', '2026-01-01')", (asset_id,))
    conn.commit()


def test_retrieve_injects_knowledge_evidence(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    asset_id = _asset_id(conn)
    _seed_knowledge(conn, asset_id)

    plan = planner.make("这个实验整体讲了什么")
    stats: dict = {}
    evidence = answer.retrieve(conn, "这个实验整体讲了什么", plan=plan, stats=stats)

    knowledge_items = [item for item in evidence if item.knowledge == "摘要"]
    assert knowledge_items, "应注入摘要证据"
    assert "样本量" in knowledge_items[0].text
    assert stats["summary_sources"] >= 1
    assert stats["knowledge_pending"] >= 0


def test_summary_matches_strict_ignores_weak_gram_hits(conn):
    for summary_id, scope_id, title, summary in (
            ("s-weak", "asset-x", "高数习题", "最后计算曲线积分。"),
            ("s-strong", "asset-y", "面板数据记录", "面板数据模型选择与检验。")):
        conn.execute(
            "INSERT INTO knowledge_summaries (summary_id, scope_type, scope_id,"
            " title, summary, model, status, updated_at)"
            " VALUES (?, 'document', ?, ?, ?, 'fake', 'ok', '2026-01-01')",
            (summary_id, scope_id, title, summary))
    conn.commit()
    analysis = {
        "concepts": [{"term": "最后", "variants": ["最后"]},
                     {"term": "面板数据", "variants": ["面板数据"]}],
        "core": "",
    }

    strict_ids = {item["summary_id"]
                  for item in knowledge.summary_matches(conn, analysis, [], strict=True)}
    lenient_ids = {item["summary_id"]
                   for item in knowledge.summary_matches(conn, analysis, [])}

    assert "s-strong" in strict_ids
    assert "s-weak" not in strict_ids
    assert {"s-strong", "s-weak"} <= lenient_ids


def test_faq_matches_strict_ignores_weak_gram_hits(conn):
    for question_id, family_id, question, answer_text in (
            ("q-weak", "fam-1", "这个模型怎么用？", "最后再说。"),
            ("q-strong", "fam-2", "面板数据选了哪个模型？", "随机效应。"),
    ):
        conn.execute(
            "INSERT INTO knowledge_questions (question_id, family_id, question,"
            " answer, related_entity_ids, model, updated_at)"
            " VALUES (?, ?, ?, ?, '[]', 'fake', '2026-01-01')",
            (question_id, family_id, question, answer_text))
    conn.commit()
    analysis = {
        "concepts": [{"term": "最后", "variants": ["最后"]},
                     {"term": "面板数据", "variants": ["面板数据"]}],
        "core": "面板数据最后",
    }

    rows = knowledge.faq_matches(conn, analysis, [], strict=True)

    ids = {row["question_id"] for row in rows}
    assert "q-strong" in ids
    assert "q-weak" not in ids


def test_retrieve_relation_intent_injects_relations(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "用试剂 A 完成样本量 42 的实验。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    _seed_knowledge(conn, _asset_id(conn))

    plan = planner.make("试剂 A 和样本量实验什么关系")
    assert plan.intent_hint == "relation"
    stats: dict = {}
    evidence = answer.retrieve(conn, "试剂 A 和样本量实验什么关系", plan=plan, stats=stats)

    relation_items = [item for item in evidence if item.knowledge == "关系"]
    assert relation_items, "关系类问题应注入知识关系证据"
    assert "用于" in relation_items[0].text
    assert stats["relation_sources"] >= 1


def test_retrieve_faq_matches_by_keyword(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    _seed_knowledge(conn, _asset_id(conn))

    plan = planner.make("样本量是什么意思")
    stats: dict = {}
    evidence = answer.retrieve(conn, "样本量是什么意思", plan=plan, stats=stats)

    faq_items = [item for item in evidence if item.knowledge == "问答"]
    assert faq_items, "关键词命中 FAQ 应注入问答证据"
    assert stats["faq_sources"] >= 1


def test_retrieve_falls_back_to_fts_without_knowledge(conn, tmp_path):
    source = _source(conn, tmp_path, {"记录.md": "样本量 42 的实验已经完成。\n"})
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    plan = planner.make("样本量是多少")
    stats: dict = {}
    evidence = answer.retrieve(conn, "样本量是多少", plan=plan, stats=stats)

    assert evidence, "没有衍生知识时应回退本地 FTS 检索"
    assert all(not item.knowledge for item in evidence)


def test_budget_info_includes_knowledge(conn):
    budget.add_knowledge(conn, 1)
    info = budget.info(conn)
    assert info["knowledge_used"] == 1
    assert info["knowledge_limit"] >= 0