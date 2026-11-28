from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from shiyuan import db, extractors, indexer, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "content.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _scan_and_index(conn, root: Path) -> str:
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return source["source_id"]


def _make_pdf(path: Path, text: str, blank_pages: int = 0) -> None:
    """用 pypdf 手写一个带文本层的最小 PDF（需要 /Font 资源才能被 extract_text 读到）。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (" + text.encode("latin-1") + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    for _ in range(blank_pages):
        writer.add_blank_page(width=300, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


def test_text_content_is_searchable(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "实验记录.md").write_text(
        "# 实验记录\n本次样本量 42 例，测序平台 NovaSeq\n", encoding="utf-8"
    )
    _scan_and_index(conn, root)

    hits = search.search(conn, "NovaSeq")
    assert hits["total"] == 1
    item = hits["items"][0]
    assert item["match"] == "content"
    assert item["index_level"] == "L1"
    assert "NovaSeq" in item["snippet"]


def test_extra_text_formats_are_searchable(conn, tmp_path):
    """盘点声明可解析的文本类型（.rst/.vue 等）必须真的能搜到。"""
    root = tmp_path / "配置"
    root.mkdir()
    (root / "说明.rst").write_text("测序平台说明：NovaSeq\n", encoding="utf-8")
    (root / "部件.vue").write_text(
        "<template><div>KylinChart</div></template>\n", encoding="utf-8")
    _scan_and_index(conn, root)

    assert search.search(conn, "NovaSeq")["total"] == 1
    assert search.search(conn, "KylinChart")["total"] == 1


@pytest.mark.skipif(extractors.soffice_path() is None, reason="需要 LibreOffice")
def test_rtf_is_converted_and_indexed(conn, tmp_path):
    """RTF 属于通用文档格式：经 LibreOffice 转换后应能检索正文。"""
    root = tmp_path / "旧文档"
    root.mkdir()
    (root / "记录.rtf").write_text(
        r"{\rtf1\ansi Kylin sample size 42}", encoding="ascii")
    _scan_and_index(conn, root)

    hits = search.search(conn, "Kylin")
    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "记录.rtf"


def test_short_cjk_query_uses_like_fallback(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42 例\n", encoding="utf-8")
    _scan_and_index(conn, root)

    assert search.search(conn, "样本")["total"] == 1
    assert search.search(conn, "样本量")["total"] == 1


def test_docx_paragraphs_and_tables_are_indexed(conn, tmp_path):
    import docx

    root = tmp_path / "资料"
    root.mkdir()
    document = docx.Document()
    document.add_paragraph("项目A 的最终样本量为 48 例")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "样本编号"
    table.cell(0, 1).text = "测序深度"
    table.cell(1, 0).text = "S001"
    table.cell(1, 1).text = "30"
    document.save(str(root / "项目报告.docx"))
    _scan_and_index(conn, root)

    assert search.search(conn, "最终样本量")["total"] == 1
    hits = search.search(conn, "测序深度")
    assert hits["total"] == 1
    assert hits["items"][0]["match"] == "content"


def test_docx_tables_keep_markdown_format(conn, tmp_path):
    import docx

    root = tmp_path / "资料"
    root.mkdir()
    document = docx.Document()
    document.add_paragraph("表格前后各有一段说明")
    first = document.add_table(rows=2, cols=2)
    first.cell(0, 0).text = "样本编号"
    first.cell(0, 1).text = "测序深度"
    first.cell(1, 0).text = "S001"
    first.cell(1, 1).text = "30"
    document.add_paragraph("第二张表：质控")
    second = document.add_table(rows=2, cols=2)
    second.cell(0, 0).text = "指标"
    second.cell(0, 1).text = "数值"
    second.cell(1, 0).text = "Q30"
    second.cell(1, 1).text = "95%"
    document.save(str(root / "表格报告.docx"))
    _scan_and_index(conn, root)

    text = conn.execute("SELECT text FROM chunks ORDER BY ordinal LIMIT 1").fetchone()[0]
    assert "| 样本编号 | 测序深度 |" in text
    assert "| --- | --- |" in text
    assert "| S001 | 30 |" in text
    # 表格保持正文顺序，两张表之间有空行与说明段，不会首尾相接
    assert "| S001 | 30 |\n\n第二张表：质控\n\n| 指标 |" in text


def test_xlsx_sheet_keeps_markdown_table(conn, tmp_path):
    import openpyxl

    root = tmp_path / "资料"
    root.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "汇总"
    sheet.append(["样本编号", "测序深度"])
    sheet.append(["S001", 30])
    workbook.save(str(root / "结果表.xlsx"))
    _scan_and_index(conn, root)

    text = conn.execute("SELECT text FROM chunks ORDER BY ordinal LIMIT 1").fetchone()[0]
    assert "| 样本编号 | 测序深度 |" in text
    assert "| --- | --- |" in text
    assert "| S001 | 30 |" in text


def test_docx_header_footer_and_comments_are_indexed(conn, tmp_path):
    import docx

    root = tmp_path / "资料"
    root.mkdir()
    document = docx.Document()
    document.add_paragraph("正文没有页眉页脚里的词")
    section = document.sections[0]
    section.header.paragraphs[0].text = "页眉项目代号 Zebrafish"
    section.footer.paragraphs[0].text = "页脚合同编号 HT2026"
    document.comments.add_comment(text="批注提醒：补充样本量", author="李四")
    document.save(str(root / "项目报告.docx"))
    _scan_and_index(conn, root)

    assert search.search(conn, "Zebrafish")["total"] == 1
    assert search.search(conn, "合同编号")["total"] == 1
    assert search.search(conn, "补充样本量")["total"] == 1


def test_pptx_slides_tables_and_notes_are_indexed(conn, tmp_path):
    from pptx import Presentation
    from pptx.util import Inches

    root = tmp_path / "汇报"
    root.mkdir()
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[1])
    first.shapes.title.text = "测序项目汇报"
    first.placeholders[1].text = "NovaSeq 平台"
    first.notes_slide.notes_text_frame.text = "备注关键词 紫藤计划"
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "样本明细"
    table = second.shapes.add_table(
        2, 2, Inches(1), Inches(1), Inches(4), Inches(2)
    ).table
    table.cell(0, 0).text = "样本编号"
    table.cell(0, 1).text = "测序深度"
    table.cell(1, 0).text = "S001"
    table.cell(1, 1).text = "30"
    presentation.save(str(root / "项目汇报.pptx"))
    _scan_and_index(conn, root)

    notes_hits = search.search(conn, "紫藤计划")
    assert notes_hits["total"] == 1
    assert notes_hits["items"][0]["page"] == 1
    table_hits = search.search(conn, "测序深度")
    assert table_hits["total"] == 1
    assert table_hits["items"][0]["page"] == 2


def test_pdf_reports_page_locator(conn, tmp_path):
    root = tmp_path / "论文"
    root.mkdir()
    _make_pdf(root / "alignment.pdf", "BLAST alignment result")
    _scan_and_index(conn, root)

    hits = search.search(conn, "BLAST alignment")
    assert hits["total"] == 1
    assert hits["items"][0]["page"] == 1


def test_pdf_tracks_missing_text_pages(conn, tmp_path):
    root = tmp_path / "论文"
    root.mkdir()
    _make_pdf(root / "mixed.pdf", "BLAST alignment result", blank_pages=1)
    _scan_and_index(conn, root)

    artifact = conn.execute(
        "SELECT state, page_count, text_pages FROM parse_artifacts"
    ).fetchone()
    assert artifact["state"] == "ok"
    assert artifact["page_count"] == 2
    assert artifact["text_pages"] == 1
    assert search.search(conn, "BLAST alignment")["items"][0]["page"] == 1


def test_excel_reports_sheet_locator(conn, tmp_path):
    import openpyxl

    root = tmp_path / "资料"
    root.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "汇总"
    sheet.append(["样本编号", "测序深度"])
    sheet.append(["S001", 30])
    workbook.save(str(root / "结果表.xlsx"))
    _scan_and_index(conn, root)

    hits = search.search(conn, "测序深度")
    assert hits["total"] == 1
    assert hits["items"][0]["sheet"] == "汇总"


def test_excel_formula_text_is_preserved(conn, tmp_path):
    import openpyxl

    root = tmp_path / "资料"
    root.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "计算"
    sheet.append(["项目", "数值"])
    sheet.append(["A", 1])
    sheet.append(["B", 2])
    sheet.append(["合计", "=SUM(B2:B3)"])
    workbook.save(str(root / "计算表.xlsx"))
    _scan_and_index(conn, root)

    rows = conn.execute("SELECT text FROM chunks WHERE sheet = '计算'").fetchall()
    text = "\n".join(row["text"] for row in rows)
    assert "〔公式：=SUM(B2:B3)〕" in text
    assert search.search(conn, "SUM")["total"] == 1


@pytest.mark.skipif(extractors.soffice_path() is None, reason="未安装 LibreOffice")
def test_legacy_doc_is_converted_and_indexed(conn, tmp_path):
    import docx

    root = tmp_path / "旧档案"
    root.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    modern = staging / "modern.docx"
    document = docx.Document()
    document.add_paragraph("旧版合同关键词 白桦林")
    document.save(str(modern))
    produced = extractors.convert_legacy(modern, "doc", staging)
    produced.rename(root / "旧版合同.doc")

    _scan_and_index(conn, root)
    hits = search.search(conn, "白桦林")
    assert hits["total"] == 1
    assert hits["items"][0]["name"] == "旧版合同.doc"
    assert hits["items"][0]["match"] == "content"


def test_modified_file_replaces_old_chunks(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    target = root / "记录.md"
    target.write_text("旧版本：样本量 42\n", encoding="utf-8")
    _scan_and_index(conn, root)
    assert search.search(conn, "旧版本")["total"] == 1

    time.sleep(0.01)
    target.write_text("新版本：样本量 48\n", encoding="utf-8")
    scanner.scan_source(conn, store.list_sources(conn)[0]["source_id"])
    indexer.index_pending(conn)

    assert search.search(conn, "旧版本")["total"] == 0
    assert search.search(conn, "新版本")["total"] == 1


def test_missing_file_content_leaves_results(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    target = root / "记录.md"
    target.write_text("这是一个会被删除的文件，关键词 罕见词甲\n", encoding="utf-8")
    source_id = _scan_and_index(conn, root)
    assert search.search(conn, "罕见词甲")["total"] == 1

    target.unlink()
    scanner.scan_source(conn, source_id)
    assert search.search(conn, "罕见词甲")["total"] == 0


def test_pdf_without_text_layer_is_marked_empty(conn, tmp_path):
    root = tmp_path / "扫描件"
    root.mkdir()
    _make_pdf_blank(root / "IMG_001.pdf")
    _scan_and_index(conn, root)

    artifact = conn.execute("SELECT state, quality FROM parse_artifacts").fetchone()
    assert artifact["state"] == "empty"
    assert artifact["quality"] == "empty"
    coverage = search.stats(conn)["coverage"]
    assert coverage["content_empty"] == 1
    assert coverage["content_indexed"] == 0


def _make_pdf_blank(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as handle:
        writer.write(handle)


def test_unsupported_types_are_not_indexed(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    _scan_and_index(conn, root)

    assert conn.execute("SELECT COUNT(*) FROM parse_artifacts").fetchone()[0] == 0
    coverage = search.stats(conn)["coverage"]
    # 图片不再算"暂不支持"，而是进入按需 OCR 队列。
    assert coverage["content_unsupported"] == 0
    assert coverage["ocr_pending"] == 1


def test_indexing_is_idempotent(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42\n", encoding="utf-8")
    source_id = _scan_and_index(conn, root)
    chunks_before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    again = indexer.index_pending(conn, source_id)
    assert again["indexed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == chunks_before


def test_remove_source_clears_content_cache(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42 例\n", encoding="utf-8")
    source_id = _scan_and_index(conn, root)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0

    store.remove_source(conn, source_id)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parse_artifacts").fetchone()[0] == 0
    assert search.search(conn, "样本量")["total"] == 0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIYUAN_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("SHIYUAN_WATCH", "0")
    from shiyuan.api import app

    with TestClient(app) as test_client:
        yield test_client


def test_protected_sheet_is_marked_but_still_indexed(conn, tmp_path):
    import openpyxl

    root = tmp_path / "表格"
    root.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet["A1"] = "样本量 42"
    sheet.protection.sheet = True
    workbook.save(root / "受保护.xlsx")

    _scan_and_index(conn, root)

    assert search.search(conn, "工作表受保护")["total"] >= 1
    assert search.search(conn, "样本量 42")["total"] >= 1


def test_office_lock_files_are_skipped(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "~$锁.docx").write_bytes(b"lock")
    (root / "正常.md").write_text("正常内容", encoding="utf-8")

    source = store.add_source(conn, root)
    result = scanner.scan_source(conn, source["source_id"])

    assert result["seen"] == 1
    assert result["skipped"] == 1
    row = conn.execute(
        "SELECT files_skipped, skip_reasons FROM source_scan_runs"
        " WHERE source_id = ? ORDER BY started_at DESC LIMIT 1",
        (source["source_id"],),
    ).fetchone()
    assert row["files_skipped"] == 1
    assert "office_lock" in row["skip_reasons"]


def test_api_scan_indexes_content_before_finishing(client, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    (root / "实验记录.md").write_text("测序平台 NovaSeq\n", encoding="utf-8")
    source_id = client.post("/api/sources", json={"root": str(root)}).json()["source_id"]
    assert client.post(f"/api/sources/{source_id}/scan").status_code == 202

    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    confirmed = client.put("/api/inventory/policies", json={
        "source_id": source_id, "policies": {"md": "standard_parse"},
    })
    assert confirmed.status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        if not client.get("/api/sources").json()["items"][0]["scanning"]:
            break
        time.sleep(0.05)

    hits = client.get("/api/search", params={"q": "NovaSeq"}).json()
    assert hits["total"] == 1
    assert hits["items"][0]["match"] == "content"
    assert client.get("/api/stats").json()["coverage"]["content_indexed"] == 1

    item = client.get(f"/api/sources/{source_id}/parse-results").json()["items"][0]
    preview = client.get("/api/document/text", params={"location_id": item["location_id"]})
    assert preview.status_code == 200
    assert "NovaSeq" in preview.json()["text"]
    assert client.get(
        "/api/document/text", params={"location_id": "missing"}).status_code == 404


def test_location_text_previews_parsed_markdown(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    body = "# 实验记录\n\n" + "样本量 42，重复三次；" * 120 + "\n"
    (root / "实验记录.md").write_text(body, encoding="utf-8")
    _scan_and_index(conn, root)
    location = conn.execute("SELECT location_id FROM asset_locations").fetchone()

    preview = indexer.location_text(conn, location["location_id"])

    assert preview["name"] == "实验记录.md"
    assert preview["ext"] == "md"
    assert preview["text"].startswith("# 实验记录")
    assert "样本量 42，重复三次；" in preview["text"]
    assert preview["chars"] == len(preview["text"])
    assert preview["truncated"] is False

    cut = indexer.location_text(conn, location["location_id"], max_chars=50)
    assert cut["truncated"] is True
    assert len(cut["text"]) == 50

    with pytest.raises(KeyError):
        indexer.location_text(conn, "missing")


def test_large_text_file_is_sampled_instead_of_failed(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(extractors, "MAX_FILE_BYTES", 200)
    monkeypatch.setattr(extractors, "MAX_TEXT_SAMPLE_BYTES", 64)
    root = tmp_path / "资料"
    root.mkdir()
    big = root / "大表.txt"
    big.write_text("样本量 42\n" + "x" * 400, encoding="utf-8")

    sections = extractors.extract(big, "txt")
    assert "已截断" in sections[0].text
    assert "样本量 42" in sections[0].text

    source_id = _scan_and_index(conn, root)
    report = indexer.parse_results(conn, source_id)
    assert report["items"][0]["status"] == "ok"
    texts = [row["text"] for row in conn.execute("SELECT text FROM chunks")]
    assert any("已截断" in text for text in texts)


def test_locked_file_is_retried_on_next_round(conn, tmp_path, monkeypatch):
    """文件被占用不是终态：下一轮解析应重新排队，不能永久停在 locked。"""
    root = tmp_path / "资料"
    root.mkdir()
    (root / "记录.md").write_text("样本量 42\n", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    original = extractors.extract

    def locked(path, ext):
        raise PermissionError(13, "文件正被占用")

    monkeypatch.setattr(extractors, "extract", locked)
    first = indexer.index_pending(conn, source["source_id"])
    assert first["locked"] == 1

    monkeypatch.setattr(extractors, "extract", original)
    again = indexer.index_pending(conn, source["source_id"])
    assert again["indexed"] == 1
