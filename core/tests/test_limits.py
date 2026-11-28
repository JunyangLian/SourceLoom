from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from shiyuan import budget, config, db, extractors, indexer, ocr, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "limits.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _source(conn, tmp_path, files: dict[str, bytes | str]):
    root = tmp_path / "资料"
    root.mkdir(exist_ok=True)
    for name, content in files.items():
        target = root / name
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    return root, source["source_id"]


def test_daily_parse_budget_limits_run(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "daily_parse_budget", lambda: 2)
    _, source_id = _source(conn, tmp_path, {
        "a.md": "样本量 42\n",
        "b.md": "样本量 43\n",
        "c.md": "样本量 44\n",
    })

    result = indexer.index_pending(conn, source_id)

    assert result["indexed"] == 2
    info = budget.info(conn)
    assert info["l1_used"] == 2
    assert info["l1_limit"] == 2
    assert len(indexer.pending_rows(conn, source_id)) == 1

    again = indexer.index_pending(conn, source_id)
    assert again["indexed"] == 0
    assert again["budget_exhausted"] is True


def test_budget_resets_on_new_day(conn):
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('budget', ?)",
        (json.dumps({"date": "2000-01-01", "l1": 999, "l2": 999}),))
    conn.commit()

    info = budget.info(conn)
    assert info["l1_used"] == 0
    assert info["l2_used"] == 0
    assert budget.remaining(conn, "l1") == config.daily_parse_budget()


def test_daily_ocr_budget_limits_run(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "daily_ocr_budget", lambda: 1)
    monkeypatch.setattr(ocr, "engine_info", lambda: {
        "available": True, "engine": "windows-ocr", "languages": ["zh-Hans-CN"], "message": None})
    monkeypatch.setattr(ocr, "extract_image",
                        lambda path: [ocr.Section(text="图片文字", page=None)])
    _, source_id = _source(conn, tmp_path, {
        "一.png": b"\x89PNG\r\n\x1a\n" + b"0" * 8,
        "二.png": b"\x89PNG\r\n\x1a\n" + b"0" * 8,
    })
    indexer.index_pending(conn, source_id)

    result = indexer.index_ocr_pending(conn, source_id)

    assert result["indexed"] == 1
    assert result["remaining"] == 1
    assert budget.info(conn)["l2_used"] == 1


def test_low_quality_pdf_supplemented_by_ocr(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(extractors, "extract",
                        lambda path, ext: [extractors.Section(text="\ufffd" * 200)])
    monkeypatch.setattr(ocr, "engine_info", lambda: {
        "available": True, "engine": "windows-ocr", "languages": ["zh-Hans-CN"], "message": None})
    monkeypatch.setattr(ocr, "extract_pdf",
                        lambda path, **kwargs: [ocr.Section(text="OCR 补充文本 麒麟", page=1)])
    import pypdf
    from pypdf import PdfWriter

    root = tmp_path / "资料"
    root.mkdir()
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = pypdf.generic.DictionaryObject({
        pypdf.generic.NameObject("/Type"): pypdf.generic.NameObject("/Font"),
        pypdf.generic.NameObject("/Subtype"): pypdf.generic.NameObject("/Type1"),
        pypdf.generic.NameObject("/BaseFont"): pypdf.generic.NameObject("/Helvetica"),
    })
    page[pypdf.generic.NameObject("/Resources")] = pypdf.generic.DictionaryObject({
        pypdf.generic.NameObject("/Font"): pypdf.generic.DictionaryObject(
            {pypdf.generic.NameObject("/F1"): writer._add_object(font)})
    })
    stream = pypdf.generic.DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (garbled) Tj ET")
    page[pypdf.generic.NameObject("/Contents")] = writer._add_object(stream)
    with (root / "low.pdf").open("wb") as handle:
        writer.write(handle)

    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])

    artifact = conn.execute("SELECT quality FROM parse_artifacts").fetchone()
    assert artifact["quality"] == "low"
    assert search.stats(conn)["coverage"]["low_quality"] == 1
    assert indexer.ocr_pending_count(conn, source["source_id"]) == 1

    indexer.index_ocr_pending(conn, source["source_id"])
    assert search.search(conn, "麒麟")["total"] == 1


def test_jobs_list_and_single_retry(conn, tmp_path):
    _, source_id = _source(conn, tmp_path, {"坏文档.docx": b"PK\x03\x04fake"})
    indexer.index_pending(conn, source_id)

    jobs = indexer.list_jobs(conn, status="failed")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == "坏文档.docx"
    assert job["stage"] == "l1"
    assert job["error"]

    result = indexer.retry_job(conn, job["job_id"])

    assert result["state"] == "failed"
    refreshed = indexer.get_job(conn, job["job_id"])
    assert refreshed["attempts"] == 1
    assert refreshed["status"] == "failed"
    events = [row["type"] for row in conn.execute(
        "SELECT type FROM maintenance_events ORDER BY event_id DESC LIMIT 5")]
    assert "job_retry" in events


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_api_updates_overview(client):
    overview = client.get("/api/updates/overview").json()
    assert overview["auto_parse"] is True
    assert overview["summary"]["sources"] == 0
    assert overview["summary"]["pending_l1"] == 0
    assert client.post("/api/updates/check").json()["started"] == []


def test_api_jobs_and_retry(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "坏文档.docx").write_bytes(b"PK\x03\x04fake")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    client.post(f"/api/sources/{source_id}/scan")

    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    confirmed = client.put("/api/inventory/policies", json={
        "source_id": source_id, "policies": {"docx": "standard_parse"},
    })
    assert confirmed.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    items = client.get("/api/jobs", params={"status": "failed"}).json()["items"]
    assert items and items[0]["name"] == "坏文档.docx"

    retried = client.post(f"/api/jobs/{items[0]['job_id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["state"] == "failed"

    assert client.post("/api/jobs/missing/retry").status_code == 404
    stats = client.get("/api/stats").json()
    # 用户显式扫描 / 单文件重试不占每日后台预算（预算只约束自动增量解析）。
    assert stats["maintenance"]["budget"]["l1_used"] == 0
    assert "low_quality" in stats["coverage"]
