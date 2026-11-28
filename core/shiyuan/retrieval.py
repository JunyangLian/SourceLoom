"""统一召回层：查询理解后从 chunk_fts / chunks 取候选，并按概念命中评分。

search 与 answer 共用这一层，保证"搜索"和"问答依据"看到的是同一批证据。
向量召回在 embeddings.py 中实现，由上层按 RRF 与这里的结果合并。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import families, query

CANDIDATE_LIMIT = 600
PATH_MATCH_LIMIT = 20000
PATH_ONLY_LIMIT = 8
# LIKE 兜底的候选上限：见 find_chunks 里的说明。
LIKE_FALLBACK_LIMIT = 5000
# 一次兜底最多扫描多少个短词：instr 是全表扫描，短词越多线性越慢
# （20 个短词实测 12 s，1 个约 0.3 s）。多余的 2-gram 通常已被 FTS 的长片段覆盖。
LIKE_FALLBACK_UNITS = 3
# FTS 已有多少条候选时才跳过兜底。原来固定 100：稀有字段名（“成功识别数”）
# 只召回二三十条也会触发 3–5 s 全表扫描，而答案通常已在这二十条里。
FALLBACK_MIN_ROWS = 20

# 这些词出现在文件夹/文件名里太常见，单独命中不足以构成证据。
_GENERIC_PATH_TERMS = {
    "实验", "数据", "记录", "报告", "总结", "笔记", "论文", "文档", "结果",
    "处理", "文件", "资料", "表格", "分析", "研究", "内容", "项目", "工作",
    "附件", "备份", "副本", "新建", "未命名", "图片", "照片",
    "整理", "系统", "企业",
    "课程", "科目", "作业", "考试", "试卷", "课件", "讲义", "习题", "章节",
    # 英文通用词：工具/教程/工程文件里极常见，仅命中这些不应压过语义命中。
    "mapping", "development", "configuration", "config", "tutorial", "tutorials",
    "sample", "samples", "index", "page", "pages", "content", "extension",
    "project", "main", "test", "tests", "docs", "doc", "readme", "license",
    "changelog", "build", "src", "lib", "assets", "static", "output", "out",
    "sex", "chromosome", "exception", "error", "document", "documents",
}


def is_generic_term(term: str) -> bool:
    return term in _GENERIC_PATH_TERMS


# 代码文件扩展名与「代码意图查询」的识别：retrieval 与 search 共用同一套规则，
# 否则搜索结果和问答证据对代码文件的口径会不一致。
CODE_EXTS = {
    "py", "pl", "sh", "bat", "ps1", "js", "ts", "tsx", "jsx", "r", "java",
    "c", "cc", "cpp", "h", "hpp", "go", "rs", "ipynb",
}
CODE_QUERY = re.compile(
    r"(脚本|代码|程序|函数|源码|命令行|code|script|function|notebook|ipynb)",
    re.IGNORECASE,
)
# 非代码查询时代码文件的强降权幅度。要盖过 focus/maximal 的任何组合，
# 否则「package sample;」这类正文命中仍会挤占文档证据位。
CODE_PENALTY = 1200


def _file_ext(path: str) -> str:
    name = (path or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_numeric_term(term: str) -> bool:
    """纯数字（可带小数点/千分位）的语义单元，例如「2022」「3.14」。"""
    cleaned = (term or "").replace(",", "").replace(".", "").strip()
    return bool(cleaned) and cleaned.isdigit()


def hit_identifier_hits(hit_text: str, path: str, identifiers: list[str]) -> int:
    """命中的标识符个数：正文或路径里出现都算。"""
    if not identifiers:
        return 0
    lowered_text = (hit_text or "").lower()
    lowered_path = (path or "").lower()
    return sum(1 for token in identifiers
               if token in lowered_text or token in lowered_path)


def _strong_name_hit(path: str, analysis: dict) -> bool:
    """代码文件的豁免判定：文件名里出现 ≥4 字符的具体查询词。

    英文词必须整词匹配（primer 不能命中 PrimeRing.java、complete 不能命中
    CompleteNumber.java），否则同义词撞名会让降权形同虚设；
    下划线/连字符按空格处理，receipt_sorter 仍可命中 receipt_sorter.py。
    """
    name = re.sub(r"[_\\-]+", " ", (path or "")
                  .rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower())
    for concept in analysis["concepts"]:
        for variant in concept["variants"]:
            if len(variant) < 4:
                continue
            probe = re.sub(r"[_\\-]+", " ", variant)
            if variant.isascii():
                if re.search(r"(?<![a-z0-9])" + re.escape(probe)
                             + r"(?![a-z0-9])", name):
                    return True
            elif probe in name:
                return True
    return False


# 说明类文档（README / 使用说明）在「怎么用」问题里更相关；
# 论文模板/撰写规范这类文件在其他问题里容易挤占正文文档。
_DOC_NAME = re.compile(
    r"(?:^|[\\/_\-\s.])(?:readme|readme\.md|quickstart|usage|guide|manual|docs|documentation)"
    r"(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)
_DOC_HINT = re.compile(
    r"(使用说明|操作说明|快速开始|快速上手|使用手册|操作手册|使用指南|上手教程|入门教程)")
_TEMPLATE_NAME = re.compile(r"(模板|规范|封面|样表|格式要求|撰写)")
_FORMAT_QUERY = re.compile(r"(模板|规范|格式|封面|排版|撰写)")
# 查重/比对报告与正文主题高度重合，但通常不是用户要找的内容。
_DUP_NAME = re.compile(
    r"(查重|重复率|相似度检测|比对报告|对照报告|片段对照|原文对照|全文对照|简洁报告"
    r"|去除引用|报告单|检测结果|格式分析|AIGC|检测报告|降重)")
_DUP_QUERY = re.compile(r"(查重|重复率|相似度|降重|比对|对照|检测报告|格式分析)")
# 试卷/题库/复习资料是资料库里最大的噪声源之一：除非问题本身在找考试材料，
# 否则不应压过正文文档（「共线性」被「线性代数试卷」抢走就是典型）。
_EXAM_NAME = re.compile(
    r"(试卷|试题|参考答案|评分标准|题库|考集|期末|期中|真题|模拟题|习题集"
    r"|选择题|问答题|考试题|答案|[A-Da-d]卷)")
_EXAM_QUERY = re.compile(r"(试卷|试题|答案|题库|复习|期末|期中|真题|考试题|习题|模拟题)")
# 问“结果/报告/汇总”时，处理报告、汇总表应该排在原始图片和脚本之前。
_RESULT_NAME = re.compile(r"(报告|汇总|统计|结果|明细|report|summary|result)", re.IGNORECASE)
# 「多少/数量」不能触发：计数问题（XX是多少）最常见，会给库里所有
# 「报告/结果」命名的文件（课程报告、实习报告……）全体 +200，
# 反而压过真正含答案的正文与数据文件。
_RESULT_QUERY = re.compile(r"(结果|报告|汇总|统计|概况|处理结果|明细)")
# 计数问题（XX 是多少）的答案通常在统计表/汇总表/结果清单里；
# 但「报告」命名的多为课程报告/实习报告，是最大的噪声源，不加分。
_COUNT_QUERY = re.compile(r"(多少|数量|几个)")
_RESULT_NAME_COUNT = re.compile(r"(汇总|统计|结果|明细|report|summary|stat)",
                                re.IGNORECASE)
# 问“支持哪些输入格式”时，扫描/读取模块比命令行入口更有回答价值。
_INPUT_QUERY = re.compile(r"(输入格式|支持格式|支持.{0,4}格式|输入文件|input format|supported)",
                          re.IGNORECASE)
_INPUT_NAME = re.compile(r"(scan|reader|loader|ingest|input|format)", re.IGNORECASE)


def name_prior(path: str, analysis: dict) -> int:
    name = (path or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
    score = 0
    if (analysis.get("howto") or analysis.get("intro")) and (
            _DOC_NAME.search(name) or _DOC_HINT.search(name)):
        score += 240
    if (not _FORMAT_QUERY.search(analysis.get("normalized") or "")
            and _TEMPLATE_NAME.search(name)):
        score -= 200
    if (not _DUP_QUERY.search(analysis.get("normalized") or "")
            and _DUP_NAME.search(name)):
        score -= 260
    if (not _EXAM_QUERY.search(analysis.get("normalized") or "")
            and _EXAM_NAME.search(name)):
        score -= 300
    if (_RESULT_QUERY.search(analysis.get("normalized") or "")
            and _RESULT_NAME.search(name)):
        score += 200
    if (_COUNT_QUERY.search(analysis.get("normalized") or "")
            and _RESULT_NAME_COUNT.search(name)):
        score += 200
    if (_INPUT_QUERY.search(analysis.get("normalized") or "")
            and _INPUT_NAME.search(name)):
        score += 160
    return score

_SELECT = (
    "SELECT c.chunk_id, c.asset_id, c.version_id, c.text, c.page, c.sheet, c.bbox,"
    " c.char_start, c.char_end, cv.mtime_ns AS version_mtime_ns,"
    " l.location_id, l.path, l.mtime_ns, a.parser_signature"
)
_JOINS = (
    " JOIN chunks c ON c.rowid = chunk_fts.rowid"
    " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
    " LEFT JOIN content_versions cv ON cv.version_id = c.version_id"
    " JOIN asset_locations l ON l.asset_id = c.asset_id"
    " JOIN sources s ON s.source_id = l.source_id"
)
_JOINS_NO_FTS = (
    " FROM chunks c"
    " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
    " LEFT JOIN content_versions cv ON cv.version_id = c.version_id"
    " JOIN asset_locations l ON l.asset_id = c.asset_id"
    " JOIN sources s ON s.source_id = l.source_id"
)

# 项目范围直接下推到 FTS：先限定资产再召回，避免项目内文档被全局候选挤出。
# 用 VALUES CTE 而不是临时表，MCP 的 query_only 连接也能用。
_SCOPE_CTE_MAX = 20000


def _scope_sql(allowed_assets: set[str] | None,
               excluded_assets: set[str] | None) -> tuple[str, list[str], str]:
    """返回 (WITH 前缀, 参数, WHERE 片段)；无法用 SQL 表达时返回空串走内存过滤。"""
    if allowed_assets is not None:
        if not allowed_assets:
            return "", [], "0"
        ids = list(allowed_assets)
        if excluded_assets:
            ids = [asset_id for asset_id in ids if asset_id not in excluded_assets]
        if not ids:
            return "", [], "0"
        if len(ids) > _SCOPE_CTE_MAX:
            return "", [], ""
        marks = ",".join("(?)" for _ in ids)
        return (
            f"WITH scope_assets(asset_id) AS (VALUES {marks}) ",
            ids,
            "{alias}.asset_id IN (SELECT asset_id FROM scope_assets)",
        )
    if excluded_assets:
        ids = list(excluded_assets)
        marks = ",".join("(?)" for _ in ids)
        return (
            f"WITH scope_excluded(asset_id) AS (VALUES {marks}) ",
            ids,
            "{alias}.asset_id NOT IN (SELECT asset_id FROM scope_excluded)",
        )
    return "", [], ""


@dataclass
class ChunkHit:
    chunk_id: str
    asset_id: str
    location_id: str
    path: str
    text: str
    page: int | None
    sheet: str | None
    parser_signature: str
    concepts: int
    variants: int
    version_id: str = ""
    mtime_ns: int = 0
    bbox: list[list[int]] | None = None
    # 在解析后正文里的字符区间，用于前端/Agent 精确定位证据。
    char_start: int = 0
    char_end: int = 0
    # 该 chunk 所属内容版本的 mtime；小于文件当前 mtime 说明索引可能过期。
    version_mtime_ns: int = 0
    family_id: str | None = None
    family_size: int = 1
    family_title: str = ""
    version_label: str = ""
    sources: list[dict] = field(default_factory=list)
    path_score: int = 0
    focus: int = 0
    maximal: int = 0
    # 命中的语义单元按词长加权后的总分（maximal 是个数，供覆盖率计算）。
    maximal_weight: int = 0
    # 命中的「连续短语」个数（如「土壤成分分析」「小说手稿」）。
    # 问题里出现短语时，散落的 2-gram 命中不算证据，必须命中短语本身。
    phrase_hits: int = 0
    # 命中的查询标识符个数（JL1/hap1/Chr41），用于区分同主题的不同样本。
    identifier_hits: int = 0

    @property
    def ocr(self) -> bool:
        return (self.parser_signature or "").startswith("l2.")


def _parse_bbox(raw: str | None) -> list[list[int]] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) and value else None


@dataclass
class PathMatch:
    location_id: str
    asset_id: str
    path: str
    score: int
    concepts: int
    variants: int
    name_matched: bool
    folder: str | None
    specific: bool
    folder_path: str | None = None
    folder_specific: bool = False
    specific_folder: str | None = None
    specific_folder_path: str | None = None
    # 文件名里命中的最长关键词长度：2 字 gram 太容易巧合（「染色」之于「染色体」），
    # 不足以说明这个文件就是问题的主体。
    name_strength: int = 0


def _relative_path(path: str, root: str | None) -> str:
    """去掉数据源根目录，避免根目录名把所有文件都算成路径命中。"""
    root = (root or "").rstrip("\\/")
    if not root:
        return path or ""
    for separator in ("\\", "/"):
        prefix = (root + separator).lower()
        if (path or "").lower().startswith(prefix):
            return path[len(root) + 1:]
    return path or ""


def _term_in(value: str, variant: str) -> bool:
    """路径里的词面匹配：短 ASCII 缩写只接受独立成词。

    2-3 字母的缩写（fe/ont/rna）做子串匹配必然撞上普通英文单词
    （fe→different、ont→Context、rna→journal），必须按整词匹配。
    """
    if variant.isascii() and len(variant) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])",
                         value) is not None
    return variant in value


def _path_score(path: str, analysis: dict) -> tuple[int, int, int, bool, str | None, int, bool, bool, str | None, int, int]:
    """路径匹配评分：文件夹名与文件名都参与，越深的文件夹命中越具体。

    path 应当是数据源内的相对路径；根目录名不参与打分。
    """
    lowered = (path or "").lower()
    parts = [part for part in re.split(r"[\\/]+", lowered) if part]
    if not parts:
        return 0, 0, 0, False, None, -1, False, False, None, -1, 0
    # 文件名里的下划线/连字符按空格处理：「cover_letter」要能被「cover letter」命中。
    keys = [re.sub(r"[_\-]+", " ", part) for part in parts]
    name = keys[-1]
    folders = keys[:-1]
    display_folders = parts[:-1]
    lowered_key = re.sub(r"[_\-]+", " ", lowered)

    score = 0
    matched_concepts = 0
    matched_variants = 0
    name_matched = False
    name_strength = 0
    specific = False
    folder_specific = False
    best_folder: str | None = None
    best_folder_index = -1
    best_folder_score = 0.0
    specific_folder: str | None = None
    specific_folder_index = -1
    specific_folder_score = 0.0

    for concept in analysis["concepts"]:
        concept_hit = False
        for variant in concept["variants"]:
            if len(variant) < 2:
                continue
            # 与路径同样把下划线/连字符当成空格，否则「pilot_batches」匹配不上自己的目录。
            probe = re.sub(r"[_\-]+", " ", variant)
            if _term_in(name, probe):
                score += 12 + 2 * len(variant)
                matched_variants += 1
                concept_hit = True
                name_matched = True
                if variant not in _GENERIC_PATH_TERMS:
                    specific = True
                    name_strength = max(name_strength, len(variant))
            for depth, folder in enumerate(folders):
                if not _term_in(folder, probe):
                    continue
                weight = (4 + 2 * len(variant)) * (1 + min(depth, 4) / 4)
                score += int(weight)
                matched_variants += 1
                concept_hit = True
                if variant not in _GENERIC_PATH_TERMS:
                    specific = True
                    folder_specific = True
                    if weight > specific_folder_score:
                        specific_folder_score = weight
                        specific_folder = display_folders[depth]
                        specific_folder_index = depth
                if weight > best_folder_score:
                    best_folder_score = weight
                    best_folder = display_folders[depth]
                    best_folder_index = depth
        if concept_hit:
            matched_concepts += 1

    # 多词问题（如「FE RE」）不能把空格去掉拼成 fere 再去匹配 different；
    # 整句核心词奖励只对单个词/短语生效。
    core_text = (analysis.get("core") or "").strip().lower()
    core_key = re.sub(r"[_\-]+", " ", core_text)
    core = "" if re.search(r"\s", core_key) else re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "", core_key)
    if len(core) >= 2:
        if core == name:
            score += 1000
        elif _term_in(name, core):
            score += 300
        elif _term_in(lowered_key, core_key):
            score += 120
    # 「专利的创新点」这类问法，主体词出现在文件名里应该更靠前。
    for part in re.split(r"[的\s]+", core):
        if len(part) >= 2 and part != core and _term_in(name, part):
            score += 60
    return (score, matched_concepts, matched_variants, name_matched,
            best_folder, best_folder_index, specific, folder_specific,
            specific_folder, specific_folder_index, name_strength)


def path_matches(conn: sqlite3.Connection, analysis: dict, *,
                 source_id: str | None = None, ext: str | None = None,
                 limit: int = PATH_MATCH_LIMIT,
                 allowed_assets: set[str] | None = None,
                 excluded_assets: set[str] | None = None,
                 mtime_after_ns: int | None = None) -> dict[str, PathMatch]:
    """按完整路径召回：文件夹名 + 文件名 + 正文之外的位置信息。"""
    if allowed_assets is not None and not allowed_assets:
        return {}
    variants = [variant for variant in analysis["variants"] if len(variant) >= 2][:48]
    if not variants:
        return {}
    # 词表里的「cover letter」要同时匹配文件名里的 cover_letter / cover-letter。
    terms: list[str] = []
    for variant in variants:
        for form in (variant, variant.replace(" ", "_"), variant.replace(" ", "-")):
            if form not in terms:
                terms.append(form)

    clauses = ["s.state = 'active'", "l.availability = 'AVAILABLE'"]
    params: list[object] = []
    if source_id:
        clauses.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        clauses.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    if mtime_after_ns:
        clauses.append("l.mtime_ns >= ?")
        params.append(int(mtime_after_ns))
    clauses.append(
        "(" + " OR ".join("instr(lower(l.path), ?) > 0" for _ in terms) + ")"
    )
    params.extend(terms)
    scope_prefix, scope_params, scope_clause = _scope_sql(
        allowed_assets, excluded_assets)
    if scope_clause:
        clauses.append(scope_clause.format(alias="l"))
    rows = conn.execute(
        f"{scope_prefix}SELECT l.location_id, l.asset_id, l.path, s.root_uri"
        " FROM asset_locations l"
        " JOIN sources s ON s.source_id = l.source_id"
        " WHERE " + " AND ".join(clauses) + " LIMIT ?",
        [*scope_params, *params, max(1, min(limit, PATH_MATCH_LIMIT))],
    ).fetchall()

    matches: dict[str, PathMatch] = {}
    for row in rows:
        if allowed_assets is not None and row["asset_id"] not in allowed_assets:
            continue
        if excluded_assets and row["asset_id"] in excluded_assets:
            continue
        root = row["root_uri"] or ""
        relative = _relative_path(row["path"], root)
        (score, concepts, matched_variants, name_matched,
         folder, folder_index, specific, folder_specific,
         specific_folder, specific_folder_index, name_strength) = _path_score(
            relative, analysis)
        if score <= 0:
            continue
        parts = [part for part in re.split(r"[\\/]+", relative) if part]

        def folder_abs(index: int) -> str | None:
            if index < 0:
                return None
            return root.rstrip("\\/") + "\\" + "\\".join(parts[:index + 1])

        matches[row["location_id"]] = PathMatch(
            location_id=row["location_id"],
            asset_id=row["asset_id"],
            path=row["path"],
            score=score,
            concepts=concepts,
            variants=matched_variants,
            name_matched=name_matched,
            folder=folder,
            specific=specific,
            folder_path=folder_abs(folder_index),
            folder_specific=folder_specific,
            specific_folder=specific_folder,
            specific_folder_path=folder_abs(specific_folder_index),
            name_strength=name_strength,
        )
    return matches


def _path_only_bonus(match: PathMatch | None, path_only: bool) -> int:
    """位置问题里，文件名/文件夹真正命中主题词的候选优先。

    「结果文件放在哪个目录」这类追问补上上下文后，黄姑鱼小课题下所有文件
    都会命中「黄姑鱼」；若只看路径 gram 总分，结果取决于数据库返回顺序。
    因此文件名命中主题词额外加权，命中主题文件夹再补一档。
    """
    if not path_only or match is None:
        return 0
    bonus = 0
    if match.name_matched and match.specific and match.name_strength >= 3:
        bonus += 240
    if match.folder_specific:
        bonus += 120
    return bonus


def find_chunks(conn: sqlite3.Connection, text: str, *, analysis: dict | None = None,
                source_id: str | None = None, ext: str | None = None,
                limit: int = CANDIDATE_LIMIT,
                focus: list[str] | None = None,
                path_only: bool = False,
                allowed_assets: set[str] | None = None,
                excluded_assets: set[str] | None = None,
                path_index: dict[str, PathMatch] | None = None,
                mtime_after_ns: int | None = None) -> list[ChunkHit]:
    analysis = analysis or query.analyze(text)
    variants: list[str] = analysis["variants"]
    identifiers = query.query_identifiers(text)
    if not variants or (allowed_assets is not None and not allowed_assets):
        return []

    where = ["s.state = 'active'", "l.availability = 'AVAILABLE'"]
    params: list[object] = []
    scope_prefix, scope_params, scope_clause = _scope_sql(
        allowed_assets, excluded_assets)
    if scope_clause:
        where.append(scope_clause.format(alias="c"))
    if source_id:
        where.append("l.source_id = ?")
        params.append(source_id)
    if ext:
        where.append("l.ext = ?")
        params.append(ext.lower().lstrip("."))
    if mtime_after_ns:
        where.append("l.mtime_ns >= ?")
        params.append(int(mtime_after_ns))

    # 2 个字母的缩写（fe/ont/rna）不能进 FTS：trigram 是子串匹配，
    # 「ont」会把所有含 Context/front 的块都召回来。3 个字母的编号（SSR/CDS）
    # 交给 FTS，命中后再用整词边界过滤，避免它们把查询拖进全表兜底。
    def _ascii_short_unit(variant: str) -> bool:
        return variant.isascii() and len(variant) <= 2

    fts_units = [variant for variant in variants
                 if len(variant) >= 3 and not _ascii_short_unit(variant)]
    short_units = [variant for variant in variants
                   if len(variant) < 3 or _ascii_short_unit(variant)]
    rows: list[sqlite3.Row] = []
    if fts_units:
        match = " OR ".join('"' + unit.replace('"', '""') + '"' for unit in fts_units)
        # ORDER BY rank：按 bm25 相关性取前 limit 条。裸 LIMIT 按插入序截断，
        # 最晚索引的数据源会被系统性挤出候选（新源里含答案的块整块消失）。
        sql = (
            f"{scope_prefix}{_SELECT} FROM chunk_fts{_JOINS}"
            " WHERE chunk_fts MATCH ? AND "
            + " AND ".join(where) + f" ORDER BY rank LIMIT {limit}"
        )
        rows = conn.execute(sql, [*scope_params, match, *params]).fetchall()
    if short_units and (not fts_units or len(rows) < FALLBACK_MIN_ROWS):
        # FTS5 trigram 只索引 ≥3 字符的子串；两字词（如“物种”）需要 LIKE 兜底，
        # 否则同时含长短变体的问题会漏掉短词。
        # 兜底是 instr 全表扫描（60 万 chunk，每个短词一次扫），只在**纯两字查询**或
        # FTS 候选没填满窗口时执行；同时限制短词数量，避免长问句十几个 2-gram 线性叠加。
        # 缩写（SSR/CDS）优先保留，它们比通用 2-gram 更有区分度。
        ascii_units = [unit for unit in short_units
                       if re.fullmatch(r"[a-z0-9_]{2,3}", unit)]
        fallback_units = (ascii_units
                          + [unit for unit in short_units if unit not in ascii_units]
                          )[:LIKE_FALLBACK_UNITS]
        # 中文没有大小写：直接 instr(text, ?) 即可，避免对每个短词重复 lower()
        # 整个正文（60 万 chunk × 800 字，实测占兜底耗时的一半以上）。
        clauses = " OR ".join(
            "instr(lower(c.text), ?) > 0" if unit.isascii() else "instr(c.text, ?) > 0"
            for unit in fallback_units
        )
        order = ""
        if analysis.get("howto") or analysis.get("intro"):
            # 说明类文档的候选可能被大量数据文件挤出候选上限，优先保留它们。
            order = (" ORDER BY (instr(lower(l.path), 'readme') > 0"
                     " OR instr(lower(l.path), 'quickstart') > 0) DESC")
        # LIKE 兜底不能用与 FTS 相同的小上限：instr 按插入序返回，前几百条
        # 会被早索引的源占满，晚索引的源里只命中 2 字词的块（如「注释」）
        # 会整块消失。放宽到能覆盖常见 2 字词的全量匹配（实测 ~4k 行、
        # 全表扫描 3-4s、Python 评分 0.04s）；库规模显著增长时需改用索引方案。
        like_limit = max(limit, LIKE_FALLBACK_LIMIT)
        sql = (
            f"{scope_prefix}{_SELECT}{_JOINS_NO_FTS} WHERE "
            + " AND ".join(where + [f"({clauses})"])
            + f"{order} LIMIT {like_limit}"
        )
        extra = conn.execute(
            sql,
            [*scope_params, *params,
             *(unit.lower() if unit.isascii() else unit for unit in fallback_units)],
        ).fetchall()
        if not fts_units:
            rows = extra
        else:
            merged = {(row["chunk_id"], row["location_id"]): row for row in rows}
            for row in extra:
                merged.setdefault((row["chunk_id"], row["location_id"]), row)
            rows = list(merged.values())

    ascii_short = [unit for unit in variants
                   if re.fullmatch(r"[a-z0-9_]{2,3}", unit)]
    if ascii_short:
        boundary = [re.compile(r"(?<![a-z0-9])" + re.escape(unit) + r"(?![a-z0-9])")
                    for unit in ascii_short]
        other_units = [unit for unit in variants if unit not in ascii_short]

        def row_ok(text_value: str) -> bool:
            lowered = (text_value or "").lower()
            if any(unit in lowered for unit in other_units):
                return True
            return any(pattern.search(lowered) for pattern in boundary)

        rows = [row for row in rows if row_ok(row["text"])]

    if allowed_assets is not None or excluded_assets:
        # 项目范围过滤放在候选召回之后：避免把 asset 集合塞进 FTS 的 WHERE，
        # 那会让 SQLite 为凑满 LIMIT 扫描全库（实测单个追问可达一分钟）。
        rows = [
            row for row in rows
            if (allowed_assets is None or row["asset_id"] in allowed_assets)
            and (not excluded_assets or row["asset_id"] not in excluded_assets)
        ]

    threshold = query.minimum_concepts(analysis)
    raw_required = 1 if len(analysis["concepts"]) <= 2 else 2
    maximal = query.maximal_terms(analysis)
    path_index = path_index if path_index is not None else path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets)

    hits: list[ChunkHit] = []
    for row in rows:
        text_value = row["text"] or ""
        concepts, matched = query.rank(text_value, analysis)
        path_match = path_index.get(row["location_id"])
        if path_only and path_match is not None and not (
                path_match.specific or path_match.folder_specific):
            path_match = None
        if path_only and path_match is None:
            continue
        path_ok = bool(path_match and (
            path_match.concepts >= threshold
            or (path_match.name_matched and path_match.specific)
        ))
        lowered = text_value.lower()
        matched_terms = [
            concept for concept in maximal
            if any(variant in lowered for variant in concept["variants"])
        ]
        maximal_hit = len(matched_terms)
        # 纯数字短语（「2022」）不能当作主题证据：它只说明年份出现过，
        # 无答案问题（问 2022 年某赛事成绩单）会被任意含 2022 的表格误判为强命中。
        phrase_hits = sum(
            1 for concept in matched_terms
            if concept.get("phrase") and not is_numeric_term(concept["term"]))
        # 语义单元按词长加权：命中「样本采集自」这样的 5 字短语远强于命中
        # 单个 2 字词，正文证据才有能力与路径命中（≤300）竞争。
        maximal_weight = sum(10 + 10 * (len(concept["term"]) - 2)
                             for concept in matched_terms)
        if not path_ok and maximal_hit < 1:
            # 只有 gram 命中时：说明类问题允许命中一个词，其余问题要求至少两个。
            if analysis.get("howto"):
                if concepts < 1:
                    continue
            elif concepts < raw_required:
                continue
        hits.append(ChunkHit(
            chunk_id=row["chunk_id"],
            asset_id=row["asset_id"],
            location_id=row["location_id"],
            path=row["path"],
            text=text_value,
            page=row["page"],
            sheet=row["sheet"],
            parser_signature=row["parser_signature"] or "",
            concepts=concepts + (path_match.concepts if path_match else 0),
            variants=matched + (path_match.variants if path_match else 0),
            version_id=row["version_id"] or "",
            mtime_ns=row["mtime_ns"] or 0,
            bbox=_parse_bbox(row["bbox"]),
            char_start=int(row["char_start"] or 0),
            char_end=int(row["char_end"] or 0),
            version_mtime_ns=int(row["version_mtime_ns"] or 0),
            path_score=(path_match.score if path_match else 0)
                       + _path_only_bonus(path_match, path_only),
            maximal=maximal_hit,
            maximal_weight=maximal_weight,
            phrase_hits=phrase_hits,
            identifier_hits=hit_identifier_hits(text_value, row["path"], identifiers),
        ))

    seen = {hit.location_id for hit in hits}
    extra_matches = sorted(
        (match for location_id, match in path_index.items() if location_id not in seen),
        key=lambda match: -(match.score + _path_only_bonus(match, path_only)
                            + name_prior(match.path, analysis)),
    )
    added = 0
    for match in extra_matches:
        if added >= PATH_ONLY_LIMIT:
            break
        if allowed_assets is not None and match.asset_id not in allowed_assets:
            continue
        if excluded_assets and match.asset_id in excluded_assets:
            continue
        if path_only and not (match.specific or match.folder_specific):
            continue
        if match.concepts < threshold and not (
                match.name_matched and match.specific):
            continue
        row = conn.execute(
            "SELECT c.chunk_id, c.version_id, c.text, c.page, c.sheet, c.bbox,"
            " c.char_start, c.char_end, cv.mtime_ns AS version_mtime_ns,"
            " l.mtime_ns, a.parser_signature"
            " FROM chunks c"
            " JOIN asset_locations l ON l.location_id = ?"
            " LEFT JOIN parse_artifacts a ON a.artifact_id = c.artifact_id"
            " LEFT JOIN content_versions cv ON cv.version_id = c.version_id"
            " WHERE c.asset_id = ? ORDER BY c.ordinal LIMIT 1",
            (match.location_id, match.asset_id),
        ).fetchone()
        if row is None or not (row["text"] or "").strip():
            continue
        hits.append(ChunkHit(
            chunk_id=row["chunk_id"],
            asset_id=match.asset_id,
            location_id=match.location_id,
            path=match.path,
            text=row["text"],
            page=row["page"],
            sheet=row["sheet"],
            parser_signature=row["parser_signature"] or "",
            concepts=match.concepts,
            variants=match.variants,
            version_id=row["version_id"] or "",
            mtime_ns=row["mtime_ns"] or 0,
            bbox=_parse_bbox(row["bbox"]),
            char_start=int(row["char_start"] or 0),
            char_end=int(row["char_end"] or 0),
            version_mtime_ns=int(row["version_mtime_ns"] or 0),
            path_score=match.score + _path_only_bonus(match, path_only),
            identifier_hits=hit_identifier_hits(
                row["text"] or "", match.path, identifiers),
        ))
        added += 1

    ext_filter = (ext or "").lower().lstrip(".")
    code_query = bool(CODE_QUERY.search(text)) or ext_filter in CODE_EXTS
    for hit in hits:
        hit.path_score += name_prior(hit.path, analysis)
        if not code_query and _file_ext(hit.path) in CODE_EXTS:
            # 非代码查询时代码文件强降权：同义词很容易撞上代码正文
            # （「样本→sample」命中 Java 的 package sample;）。文件名强命中
            # （≥4 字符具体词、英文整词匹配，如 receipt_sorter.py）的代码
            # 文件豁免——问「receipt_sorter 支持什么格式」时答案就在代码里。
            if not _strong_name_hit(hit.path, analysis):
                hit.path_score -= CODE_PENALTY
        if focus:
            lowered = hit.text.lower()
            hit.focus = min(3, sum(1 for term in focus
                                   if len(term) >= 2 and term in lowered))

    if path_only:
        # 位置问题只关心路径：正文里碰巧出现「结果」不该压过文件名命中。
        hits.sort(key=lambda hit: (
            -(hit.focus * 120 + hit.maximal_weight
              + hit.identifier_hits * query.IDENTIFIER_BONUS + hit.path_score),
            -hit.concepts,
            -hit.variants,
            -len(hit.text),
        ))
    else:
        hits.sort(key=lambda hit: (
            -(hit.focus * 120 + hit.maximal_weight
              + hit.identifier_hits * query.IDENTIFIER_BONUS
              + min(hit.path_score, 300)),
            -hit.concepts,
            -hit.variants,
            -len(hit.text),
        ))
    # 重复段落合并为一条并保留全部来源；各版本独有段落继续保留。
    return families.dedupe_chunk_hits(conn, hits, path_only=path_only)