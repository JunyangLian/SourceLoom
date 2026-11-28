"""检索：文件名 / 路径（L0）+ 正文片段（L1/L2）混合召回。

正文召回统一交给 retrieval.py：查询先经过 query.analyze（去疑问词、n-gram、同义词），
再按概念命中评分；短于 3 个字符的关键词退化为主键扫描。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import embeddings, families, indexer, ingest, maintenance, query, retrieval

MAX_CANDIDATES = 5000
SNIPPET_WIDTH = 120

# 混合排序权重：词面证据按“查询概念覆盖率”给分，语义证据按本次查询的 z 分数给分。
# 原始相似度跨查询不可比，固定加分会让弱语义命中混进结果。
COVERAGE_WEIGHT = 90
SEMANTIC_BASE = 60
SEMANTIC_Z_WEIGHT = 60
SEMANTIC_Z_CAP = 4.0
STRONG_RATIO = 0.5
# RRF（Reciprocal Rank Fusion）实验开关：只按各通道名次融合，
# 业务先验（编号/家族/成稿/代码降权）仍按原权重叠加。
RRF_K = 60
RRF_SCALE = 20000
# 正文是主要证据来源，权重高于文件名与向量；权重只影响名次融合的倾斜程度。
RRF_WEIGHTS = {"name": 1.0, "content": 2.0, "vector": 1.0}
# 第二轮线索扩展：从第一轮高覆盖片段里抽取编号/版本型桥接词，再检索一次。
BRIDGE_BONUS = 350
_BRIDGE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:PRJ[A-Z]{2}\d{4,}|SAM[A-Z]\d{4,}|SRR\d{5,}|ERR\d{5,}"
    r"|GCA_\d+\.\d+|GCF_\d+\.\d+|CRA\d{5,}|Chr\d{1,2}(?!\d)|v\d+\.\d+)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 代码文件识别在 retrieval 层定义（answer/ask 的证据召回共用同一套口径）。
CODE_EXTS = retrieval.CODE_EXTS
_CODE_QUERY = retrieval.CODE_QUERY
_LATEST_QUERY = re.compile(
    r"(最新|当前版本|最新版|目前版本|latest|current version)", re.IGNORECASE)
_VERSION_HISTORY_QUERY = re.compile(
    r"(历史|旧版|以前|之前|早期|旧稿|哪些版本|各版本|版本历史|对比|差异|改了什么|变化)",
    re.IGNORECASE)
_VERSION_MENTION = re.compile(r"(?:v|版本\s*)(\d+(?:\.\d+)?)", re.IGNORECASE)
_VERSION_MENTION_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)\s*版本", re.IGNORECASE)
# 成稿 / 净稿 / 终稿通常比工作笔记更适合作为事实依据。
_AUTHORITY_PATH = re.compile(
    r"(净稿|定稿|终稿|clean|manuscript|final)", re.IGNORECASE)


def _is_lookup(text: str) -> bool:
    """编号、错误码、文件名式查询：词面精确匹配比语义更可靠。"""
    tokens = re.findall(r"[a-z0-9][a-z0-9_.\-]*", (text or "").lower())
    if not tokens:
        return False
    return all(
        any(ch.isdigit() for ch in token)
        and (any(ch.isalpha() for ch in token) or len(token) >= 4)
        for token in tokens
    )


def _compact(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", (value or "").lower())


def _coverage(text: str, maximal: list[dict]) -> float:
    if not maximal:
        return 0.0
    lowered = (text or "").lower()
    matched = sum(
        1 for concept in maximal
        if any(variant in lowered for variant in concept["variants"]))
    return matched / len(maximal)


def _snippet(text: str, tokens: list[str]) -> str:
    lower = text.lower()
    at = -1
    for token in tokens:
        found = lower.find(token)
        if found >= 0 and (at < 0 or found < at):
            at = found
    if at < 0:
        at = 0
    start = max(0, at - SNIPPET_WIDTH // 2)
    end = min(len(text), start + SNIPPET_WIDTH)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


def _content_hits(conn: sqlite3.Connection, text: str, analysis: dict, *,
                  source_id: str | None, ext: str | None,
                  allowed_assets: set[str] | None = None,
                  excluded_assets: set[str] | None = None,
                  path_index: dict | None = None,
                  identifiers: list[str] | None = None,
                  keep_text: bool = False,
                  mtime_after_ns: int | None = None) -> dict[str, dict]:
    hits: dict[str, dict] = {}
    maximal = query.maximal_terms(analysis)
    total = max(1, len(maximal))
    for hit in retrieval.find_chunks(
            conn, text, analysis=analysis, source_id=source_id, ext=ext,
            allowed_assets=allowed_assets, excluded_assets=excluded_assets,
            path_index=path_index, mtime_after_ns=mtime_after_ns):
        if hit.location_id in hits:
            continue
        entry = {
            "snippet": _snippet(hit.text, analysis["variants"]),
            "page": hit.page,
            "sheet": hit.sheet,
            "level": "L2" if hit.ocr else "L1",
            "bbox": hit.bbox,
            "concepts": hit.concepts,
            "variants": hit.variants,
            "maximal": hit.maximal,
            "maximal_weight": hit.maximal_weight,
            "phrase_hits": hit.phrase_hits,
            "char_start": hit.char_start,
            "char_end": hit.char_end,
            "version_mtime_ns": hit.version_mtime_ns,
            "coverage": hit.maximal / total,
            # 只统计正文命中的编号：文件路径里带 JL1 的分组文件很多，
            # 但真正含数值的是正文恰好命中编号的统计文件。
            "identifier_hits": retrieval.hit_identifier_hits(
                hit.text, "", identifiers or []),
        }
        if keep_text:
            # 第二轮线索提取需要完整一点的正文（snippet 只有 120 字符）；
            # 默认关闭，避免每个候选都多存一份文本。
            entry["text"] = (hit.text or "")[:600]
        hits[hit.location_id] = entry
    return hits


def _bridge_terms(hits: dict[str, dict], analysis: dict) -> list[str]:
    """从第一轮高覆盖片段里抽取编号/版本型线索，用于第二轮检索（不依赖模型）。"""
    known = {variant.lower() for variant in analysis["variants"]}
    seeds = sorted(
        hits.values(),
        key=lambda item: (-(item.get("maximal_weight") or 0),
                          -(item.get("maximal") or 0)),
    )[:5]
    terms: list[str] = []
    for seed in seeds:
        for token in _BRIDGE_TOKEN.findall(seed.get("text") or ""):
            if token.lower() in known or token in terms:
                continue
            terms.append(token)
            if len(terms) >= 4:
                return terms
    return terms


def _evidence_range(content: dict | None) -> dict | None:
    """统一证据区间：PDF/OCR 返回页号，纯文本返回解析后正文的字符偏移。"""
    if not content:
        return None
    page = content.get("page")
    if page is not None:
        return {"kind": "page", "page": int(page)}
    start = content.get("char_start")
    end = content.get("char_end")
    if start is not None and end is not None and int(end) > int(start):
        return {"kind": "text", "startOffset": int(start), "endOffset": int(end)}
    return None


def _to_item(row: sqlite3.Row, *, match: str, content: dict | None,
             family: dict | None = None, matched_folder: str | None = None) -> dict:
    path = row["path"]
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    directory = path[: len(path) - len(name)].rstrip("\\/")
    item = {
        "location_id": row["location_id"],
        "asset_id": row["asset_id"],
        "name": name,
        "path": path,
        "directory": directory,
        "rel_path": row["rel_path"],
        "ext": row["ext"],
        "size": row["size"],
        "mtime": datetime.fromtimestamp(row["mtime_ns"] / 1e9).astimezone().isoformat(timespec="seconds")
        if row["mtime_ns"] else None,
        "availability": row["availability"],
        "index_level": (content["level"] if content else "L0"),
        "semantic": bool(content and content.get("semantic")),
        "match": match,
        "snippet": content["snippet"] if content else None,
        "page": content["page"] if content else None,
        "sheet": content["sheet"] if content else None,
        "bbox": content.get("bbox") if content else None,
        "range": _evidence_range(content),
        "source_id": row["source_id"],
        "source_label": row["source_label"],
        "matched_folder": matched_folder,
    }
    # 文件在解析之后又变过（location mtime 更新、版本 mtime 较旧）时标记可能过期，
    # 或者文件已不可用；调用方应重新索引后再当作强证据。
    version_mtime = int(content.get("version_mtime_ns") or 0) if content else 0
    file_mtime = int(row["mtime_ns"] or 0)
    item["stale"] = bool(
        row["availability"] != "AVAILABLE"
        or (version_mtime and file_mtime and file_mtime > version_mtime)
    )
    if family:
        latest_ns = family.get("family_latest_mtime_ns") or 0
        item.update({
            "family_id": family["family_id"],
            "family_size": family["family_size"],
            "family_title": family["family_title"],
            "version_label": family["version_label"],
            # 没有版本标签的重复副本返回 None（未知），不能当作“最新版本”。
            "is_latest_version": family.get("is_latest_version"),
            "family_latest_asset_id": family.get("family_latest_asset_id"),
            "family_latest_version_label": family.get("family_latest_version_label"),
            "family_latest_mtime": (
                datetime.fromtimestamp(latest_ns / 1e9).astimezone()
                .isoformat(timespec="seconds") if latest_ns else None
            ),
        })
    return item


def _fetch_locations(conn: sqlite3.Connection, location_ids: list[str]) -> dict[str, sqlite3.Row]:
    if not location_ids:
        return {}
    rows: dict[str, sqlite3.Row] = {}
    for start in range(0, len(location_ids), 500):
        batch = location_ids[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT l.*, s.label AS source_label, s.state AS source_state"
            f" FROM asset_locations l"
            f" JOIN sources s ON s.source_id = l.source_id"
            f" WHERE l.location_id IN ({placeholders}) AND s.state = 'active'",
            batch,
        ):
            rows[row["location_id"]] = row
    return rows


def search(conn: sqlite3.Connection, q: str, *, limit: int = 50, offset: int = 0,
           source_id: str | None = None, ext: str | None = None,
           allowed_assets: set[str] | None = None,
           excluded_assets: set[str] | None = None,
           priority_assets: set[str] | None = None,
           use_vector: bool = True, use_family: bool = True,
           fusion: str = "weighted", use_bridge: bool = False,
           use_routing: bool = False,
           mtime_after_ns: int | None = None) -> dict:
    """检索入口。use_vector / use_family / fusion / use_bridge / use_routing 仅用于消融实验。"""
    text = (q or "").strip()
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    if not text:
        return {"query": text, "total": 0, "strong": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "stale": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}
    if allowed_assets is not None and not allowed_assets:
        # 空项目/全部排除：不必再跑 FTS 与向量召回。
        return {"query": text, "total": 0, "strong": 0, "items": [], "index_level": "L1",
                "content_hits": 0, "truncated": False, "stale": False, "rewritten": None,
                "vector_used": False, "vector_model": None, "vector_error": None}

    analysis = query.analyze(text, synonyms=query.synonym_map(conn))
    maximal = query.maximal_terms(analysis)
    identifiers = query.query_identifiers(text)
    # 编号/文件名式查询靠精确词面，语义只会引入近义噪声。
    lookup = _is_lookup(text)
    latest_intent = bool(_LATEST_QUERY.search(text))
    version_mentions = [
        match.group(1) for match in _VERSION_MENTION.finditer(text)
    ] + [
        match.group(1) for match in _VERSION_MENTION_SUFFIX.finditer(text)
    ]
    historical_intent = bool(_VERSION_HISTORY_QUERY.search(text))
    # 默认优先成稿的最新版本；明确问历史 / 指定版本时不这么做。
    prefer_latest = latest_intent or not (historical_intent or version_mentions)
    rewritten = analysis["core"] if analysis["core"] and analysis["core"] != text.lower() else None
    # 按问题类型路由：版本/编号/计数问题依赖家族归并与词面精确；
    # 开放式自然语言问题保留全部版本，避免“最佳语义片段”被代表项替换。
    routing: str | None = None
    if use_routing:
        if lookup or identifiers:
            routing = "lookup"
        elif latest_intent or version_mentions:
            routing = "version"
        elif re.search(r"(多少|数量|几个|多大|多长|几种|几次|哪一年)", text):
            routing = "numeric"
        elif analysis.get("howto") or analysis.get("intro"):
            routing = "howto"
        else:
            routing = "natural"
    skip_family_grouping = routing == "natural"
    # 路径（文件夹名 + 文件名）和正文使用同一套查询概念，避免只搜正文漏掉目录信息。
    path_index = retrieval.path_matches(
        conn, analysis, source_id=source_id, ext=ext,
        allowed_assets=allowed_assets, excluded_assets=excluded_assets,
        mtime_after_ns=mtime_after_ns)
    name_rows = _fetch_locations(conn, list(path_index))

    content_hits = _content_hits(conn, text, analysis, source_id=source_id, ext=ext,
                                 allowed_assets=allowed_assets,
                                 excluded_assets=excluded_assets,
                                 path_index=path_index,
                                 identifiers=identifiers,
                                 keep_text=use_bridge,
                                 mtime_after_ns=mtime_after_ns)

    vector_hits: dict[str, dict] = {}
    vector_used = False
    vector_model: str | None = None
    vector_error: str | None = None
    embed_settings = embeddings.load_settings(conn)
    if embeddings.enabled(embed_settings) and not lookup and use_vector:
        try:
            # 向量模型对自然语言整句更敏感；去掉疑问词后的关键词串反而会丢语境。
            hits = embeddings.semantic_chunks(
                conn, text, source_id=source_id, ext=ext,
                top_k=max(10, embed_settings["top_k"] * 4),
                allowed_assets=allowed_assets,
                excluded_assets=excluded_assets,
                mtime_after_ns=mtime_after_ns)
            vector_used = True
            vector_model = embed_settings["model"]
            for hit in hits:
                if allowed_assets is not None and hit["asset_id"] not in allowed_assets:
                    continue
                if excluded_assets and hit["asset_id"] in excluded_assets:
                    continue
                current = vector_hits.get(hit["location_id"])
                if current and current["z"] >= hit.get("z", 0.0):
                    continue
                vector_hits[hit["location_id"]] = {
                    "snippet": _snippet(hit["text"], analysis["variants"]),
                    "page": hit["page"],
                    "sheet": hit["sheet"],
                    "level": "L2" if hit["ocr"] else "L1",
                    "semantic": True,
                    "similarity": hit["similarity"],
                    "z": hit.get("z", 0.0),
                    "coverage": _coverage(hit["text"], maximal),
                    "bbox": hit.get("bbox"),
                }
        except embeddings.EmbeddingError as exc:
            vector_error = str(exc)

    extra_ids = [location_id for location_id in {**content_hits, **vector_hits}
                 if location_id not in name_rows]
    name_rows.update(_fetch_locations(conn, extra_ids))

    if allowed_assets is not None or excluded_assets:
        name_rows = {
            location_id: row for location_id, row in name_rows.items()
            if (allowed_assets is None or row["asset_id"] in allowed_assets)
            and (not excluded_assets or row["asset_id"] not in excluded_assets)
        }
        path_index = {location_id: match for location_id, match in path_index.items()
                      if location_id in name_rows}
        content_hits = {location_id: value for location_id, value
                        in content_hits.items() if location_id in name_rows}
        vector_hits = {location_id: value for location_id, value
                       in vector_hits.items() if location_id in name_rows}

    if use_bridge and content_hits:
        # SAG2 式「词项桥接」的规则版：编号/版本号常出现在第一轮命中片段里，
        # 用它再检索一次可以把「同主题但没直接命中问题词」的文件找回来。
        bridge_terms = _bridge_terms(content_hits, analysis)
        if bridge_terms:
            bridge_text = " ".join(bridge_terms)
            bridge_hits = _content_hits(
                conn, bridge_text, query.analyze(bridge_text),
                source_id=source_id, ext=ext,
                allowed_assets=allowed_assets, excluded_assets=excluded_assets,
                path_index={},
                identifiers=query.query_identifiers(bridge_text),
                mtime_after_ns=mtime_after_ns)
            name_rows.update(_fetch_locations(
                conn, [loc for loc in bridge_hits if loc not in name_rows]))
            for location_id, info in bridge_hits.items():
                if location_id in content_hits:
                    continue
                row = name_rows.get(location_id)
                if row is None:
                    continue
                if allowed_assets is not None and row["asset_id"] not in allowed_assets:
                    continue
                if excluded_assets and row["asset_id"] in excluded_assets:
                    continue
                merged = dict(info)
                merged["bridge"] = True
                content_hits[location_id] = merged

    family_info = families.family_map(conn) if use_family else {}
    use_rrf = (fusion or "").lower() == "rrf"
    rrf_rank: dict[str, dict[str, int]] = {}
    if use_rrf:
        # 只按名次融合，分数本身不可比：路径分、概念数、向量 z 都只用来排序。
        for channel, entries in (
            ("name", sorted(
                path_index.items(),
                key=lambda item: (-item[1].score, -item[1].concepts,
                                  -item[1].name_strength))),
            ("content", sorted(
                content_hits.items(),
                key=lambda item: (-(item[1].get("maximal_weight") or 0),
                                  -(item[1].get("phrase_hits") or 0),
                                  -(item[1].get("variants") or 0)))),
            ("vector", sorted(
                vector_hits.items(),
                key=lambda item: (-(item[1].get("z") or 0.0),
                                  -(item[1].get("similarity") or 0.0)))),
        ):
            for rank, (location_id, _) in enumerate(entries, start=1):
                rrf_rank.setdefault(location_id, {})[channel] = rank
    scored = []
    for location_id, row in name_rows.items():
        path_match = path_index.get(location_id)
        if use_rrf:
            ranks = rrf_rank.get(location_id, {})
            score = int(RRF_SCALE * sum(
                RRF_WEIGHTS[channel] / (RRF_K + rank)
                for channel, rank in ranks.items()))
        else:
            score = path_match.score if path_match else 0
        if not use_rrf:
            score += retrieval.name_prior(row["path"], analysis)
        if (not use_rrf and path_match is not None and path_match.name_matched
                and path_match.specific and path_match.name_strength >= 3
                and path_match.concepts >= 2):
            # 文件名命中主题词时补分，避免长文档靠正文 gram 数量压过真正的主角文件。
            # 只命中 2 字 gram（如「染色」之于「染色体」）不算，否则染色体论文会抢走
            # 「性染色体」的查询；命中的概念越多，越像“就是这份文件”。
            score += 200 + 80 * min(int(path_match.concepts), 4)
        if (not _CODE_QUERY.search(text)
                and (row["ext"] or "").lower() in CODE_EXTS):
            # 用户没在找代码时，脚本/源码不应压过同名的数据或文档。
            score -= 140
        content = content_hits.get(location_id)
        semantic = vector_hits.get(location_id)
        name_matched = path_match is not None
        if identifiers:
            # 查询带编号（JL1 / hap1 / Chr41）时：正文含编号最可信，
            # 仅路径含编号次之；完全不含编号的文档整体下沉，
            # 避免被「染色体水平基因组组装」这类同主题文件名压住。
            content_ids = (int(content.get("identifier_hits", 0))
                           if content else 0)
            lowered_path = (row["path"] or "").lower()
            path_ids = sum(1 for token in identifiers if token in lowered_path)
            if content_ids or path_ids:
                score += 600 * content_ids + 250 * path_ids
            else:
                score -= 900 * len(identifiers)
        if content and not use_rrf:
            if content.get("bridge"):
                # 桥接命中只保证“同一编号/版本”，不能按原问题覆盖率加分。
                score += BRIDGE_BONUS
            else:
                # 正文候选必须保留 retrieval 的概念/变体命中数，否则同分后会被
                # mtime 排序淹没（例如唯一编号被近期文件挤出前 5）。
                score += 150
                score += 30 * int(content.get("concepts", 0))
                score += 4 * int(content.get("variants", 0))
                # 覆盖率奖励“问题里的概念被答全了”，而不是某个词重复很多次。
                coverage = float(content.get("coverage") or 0.0)
                score += int(COVERAGE_WEIGHT * coverage ** 1.5)
        if semantic and not use_rrf:
            z_score = max(0.0, min(float(semantic.get("z") or 0.0), SEMANTIC_Z_CAP))
            score += SEMANTIC_BASE + int(SEMANTIC_Z_WEIGHT * z_score)
        if lookup:
            name = row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            core = _compact(analysis["core"])
            if core and core in _compact(name):
                score += 400
        if priority_assets and row["asset_id"] in priority_assets:
            # 项目主资料在「扩展检索」里优先于关联资料与全局资料。
            score += 120
        family = family_info.get(row["asset_id"])
        if family:
            if family.get("family_size", 1) > 1:
                # 同一份知识有多个版本时，成稿比零散工作笔记更可能是答案来源。
                score += 25
                label = (family.get("version_label") or "").strip()
                if version_mentions and any(
                        label == mention or label.endswith("." + mention)
                        for mention in version_mentions):
                    score += 300
            if prefer_latest and family.get("is_latest_version"):
                score += 150
                if family.get("family_size", 1) >= 5:
                    # 版本数量多的家族通常是持续维护的主文档。
                    score += 100
                if family.get("family_size", 1) >= 20:
                    score += 80
                if latest_intent:
                    score += 80
        if _AUTHORITY_PATH.search(row["path"] or ""):
            score += 55
        merged = content
        if semantic:
            merged = dict(content or semantic)
            merged["semantic"] = True
        match = "both" if (content and name_matched) else "content" if content \
            else "semantic" if semantic else "name"
        scored.append((score, row, match, merged))
    def _inject(targets: dict[str, dict], base: int = 700) -> None:
        """把漏召回的目标资产补进候选，已作为路径候选出现的则提分。"""
        index_by_asset: dict[str, list[int]] = {}
        for index, entry in enumerate(scored):
            index_by_asset.setdefault(entry[1]["asset_id"], []).append(index)
        for asset_id, info in targets.items():
            bonus = base + 10 * min(int(info.get("family_size", 1)), 30)
            for index in index_by_asset.get(asset_id, []):
                score, row, match, content = scored[index]
                if score < bonus:
                    scored[index] = (bonus, row, match, content)
        missing = [asset_id for asset_id in targets if asset_id not in index_by_asset]
        if not missing:
            return
        marks = ",".join("?" for _ in missing)
        location_ids = [
            row["location_id"] for row in conn.execute(
                f"SELECT MIN(location_id) AS location_id FROM asset_locations"
                f" WHERE asset_id IN ({marks}) AND availability = 'AVAILABLE'"
                " GROUP BY asset_id", missing)
        ]
        ext_filter = ext.lower().lstrip(".") if ext else None
        for row in _fetch_locations(conn, location_ids).values():
            if source_id and row["source_id"] != source_id:
                continue
            if ext_filter and (row["ext"] or "").lower() != ext_filter:
                continue
            info = targets.get(row["asset_id"]) or {}
            bonus = base + 10 * min(int(info.get("family_size", 1)), 30)
            scored.append((bonus, row, "name", None))

    # “最新版本 / v0.3 版本”注入必须限定在同一主题下，否则会把无关家族的最新版
    # 强行塞进前十（问“手稿最新版”却返回毕业论文终稿）。
    topic_stop = {
        "最新", "版本", "哪个", "哪些", "什么", "怎么", "如何",
        "latest", "current", "version", "which", "what",
    }
    topic_variants = [
        variant for concept in analysis["concepts"]
        for variant in concept["variants"]
        if len(variant) >= 2
        and variant not in topic_stop
        and not retrieval.is_generic_term(variant)
    ]

    def _topic_matched(asset_ids: list[str]) -> set[str]:
        if not topic_variants:
            return set(asset_ids)
        lower = {variant.lower() for variant in topic_variants}
        paths: dict[str, str] = {}
        for start in range(0, len(asset_ids), 500):
            batch = asset_ids[start:start + 500]
            marks = ",".join("?" for _ in batch)
            for row in conn.execute(
                    f"SELECT asset_id, MIN(path) AS path FROM asset_locations"
                    f" WHERE asset_id IN ({marks}) GROUP BY asset_id", batch):
                paths[row["asset_id"]] = (row["path"] or "").lower()
        matched: set[str] = set()
        for asset_id in asset_ids:
            title = ((family_info.get(asset_id) or {}).get("family_title") or "").lower()
            haystack = f"{paths.get(asset_id, '')}\n{title}"
            if any(variant in haystack for variant in lower):
                matched.add(asset_id)
        return matched

    if latest_intent and not version_mentions and family_info:
        # “最新版本是哪个”往往没有具体词面命中；直接补上各大家族的最新版本。
        latest_by_asset: dict[str, dict] = {}
        for asset_id, info in family_info.items():
            if allowed_assets is not None and asset_id not in allowed_assets:
                continue
            if excluded_assets and asset_id in excluded_assets:
                continue
            latest_id = info.get("family_latest_asset_id")
            if not latest_id or latest_id in latest_by_asset:
                continue
            # 没有版本标签的重复副本不算“最新版本”，不能补进结果。
            if info.get("family_size", 1) < 2 or not info.get("family_latest_version_label"):
                continue
            latest_by_asset[latest_id] = info
        if topic_variants:
            keep = _topic_matched(list(latest_by_asset))
            latest_by_asset = {
                asset_id: info for asset_id, info in latest_by_asset.items()
                if asset_id in keep
            }
        ranked = sorted(
            latest_by_asset.items(),
            key=lambda item: (
                -item[1].get("family_size", 1),
                -(item[1].get("family_latest_mtime_ns") or 0),
            ),
        )[:5]
        # 明确问“最新版本”时，主题匹配的最新版本应直接排在最前；
        # 普通注入分不足以压过文件名强命中（实测会被“整理图表.zip”挤到第 9）。
        _inject(dict(ranked), base=1200)

    if version_mentions and family_info:
        # 明确点名版本时，词面候选容易被大量同名工作笔记挤出；
        # 直接补上该版本成员，并保留 is_latest_version=false 的历史标记。
        wanted = {
            mention.strip().lower().lstrip("v") for mention in version_mentions
        }
        targets: dict[str, dict] = {}
        for asset_id, info in family_info.items():
            if allowed_assets is not None and asset_id not in allowed_assets:
                continue
            if excluded_assets and asset_id in excluded_assets:
                continue
            label = (info.get("version_label") or "").strip().lower()
            if not label or info.get("family_size", 1) < 2:
                continue
            if any(label == mention or label.endswith("." + mention)
                   for mention in wanted):
                targets[asset_id] = info
        if topic_variants:
            keep = _topic_matched(list(targets))
            targets = {
                asset_id: info for asset_id, info in targets.items()
                if asset_id in keep
            }
        _inject(targets, base=1200)
    # 同分同 mtime 的候选按路径/位置排序，保证两次运行结果可复现
    # （否则依赖 SQL 返回顺序，Benchmark 会在 1–2 题上抖动）。
    def _rank_key(entry):
        return (-entry[0], -entry[1]["mtime_ns"],
                entry[1]["path"] or "", entry[1]["location_id"] or "")

    scored.sort(key=_rank_key)
    if family_info and not skip_family_grouping:
        grouped: dict[str, list[tuple]] = {}
        passthrough: list[tuple] = []
        for entry in scored:
            info = family_info.get(entry[1]["asset_id"])
            if info is None:
                passthrough.append(entry)
            else:
                # 问“v0.12 到 v0.13 改了什么”时按版本分开，不能只留一个代表。
                label = (info.get("version_label") or "").strip()
                key = f"{info['family_id']}::{label}" if version_mentions else info["family_id"]
                grouped.setdefault(key, []).append(entry)
        aggregated = list(passthrough)
        for entries in grouped.values():
            entries.sort(key=_rank_key)
            representative = entries[0]
            if prefer_latest:
                latest = [
                    entry for entry in entries
                    if (family_info.get(entry[1]["asset_id"]) or {}).get(
                        "is_latest_version")
                ]
                # 最新版本分数接近时优先展示；明确问“最新”时无条件优先。
                if latest and (latest_intent or latest[0][0] >= representative[0] * 0.6):
                    representative = latest[0]
            # 代表项优先最新版本；家族全部版本仍可在 family_versions 里展开。
            family_id = (
                family_info.get(representative[1]["asset_id"]) or {}
            ).get("family_id")
            aggregated.append((*representative, family_id))
        scored = sorted(aggregated, key=_rank_key)
    # 证据门槛：问题里出现连续短语时，散落的 2-gram 命中不算证据；
    # 编号、路径中的具体词与正文短语命中才算。用于无答案问题的拒答。
    maximal_terms = query.maximal_terms(analysis)
    total_terms = max(1, len(maximal_terms))
    # 纯数字短语（「2022」）不算主题短语，否则「2022 年冬奥会…」会被
    # 任意含 2022 的成绩单/表格误判为强证据。
    phrase_terms = [concept for concept in maximal_terms
                    if concept.get("phrase")
                    and not retrieval.is_numeric_term(concept["term"])]
    # 长问题需要覆盖足够多的概念才算证据：只命中「2022」「成绩单」这类
    # 单个词面时，主题（冬奥会/短道速滑）其实完全没被验证。
    if total_terms <= 2:
        need_terms = 1
    else:
        need_terms = max(2, int(total_terms * 0.3 + 0.999))

    def _has_evidence(entry) -> bool:
        _, row, _match, content = entry[:4]
        content = content or {}
        if content.get("bridge"):
            # 桥接命中只证明编号一致，不能单独作为原问题的强证据。
            return False
        if int(content.get("identifier_hits") or 0) > 0:
            return True
        path_match = path_index.get(row["location_id"])
        if (path_match is not None and path_match.specific
                and int(path_match.concepts) >= need_terms):
            return True
        if phrase_terms:
            return int(content.get("phrase_hits") or 0) > 0
        coverage = float(content.get("coverage") or 0.0)
        maximal = int(content.get("maximal") or 0)
        if not maximal:
            maximal = int(round(coverage * total_terms))
        return bool(content) and maximal >= need_terms

    evidence = [_has_evidence(entry) for entry in scored]
    top_score = scored[0][0] if scored else 0
    cutoff = top_score * STRONG_RATIO if top_score > 0 else 0
    strong_flags = [
        ok and entry[0] >= cutoff for ok, entry in zip(evidence, scored)
    ]
    # 与最高分差距过大的结果只是“沾边”，单独标记，前端/外部调用可以折叠。
    strong = sum(1 for flag in strong_flags if flag)
    window = scored[offset: offset + limit]
    items = []
    for index, entry in enumerate(window):
        _, row, match, content = entry[:4]
        family = family_info.get(row["asset_id"])
        path_match = path_index.get(row["location_id"])
        item = _to_item(
            row, match=match, content=content, family=family,
            matched_folder=path_match.folder if path_match else None,
        )
        item["weak"] = not strong_flags[offset + index]
        if len(entry) == 5:
            item["family_versions"] = families.family_members(conn, entry[4])
        items.append(item)
    return {
        "query": text,
        "total": len(scored),
        "strong": strong,
        "items": items,
        "index_level": "L1",
        "content_hits": len(content_hits),
        "truncated": len(path_index) >= MAX_CANDIDATES,
        "stale": any(item.get("stale") for item in items),
        "rewritten": rewritten,
        "vector_used": vector_used,
        "vector_model": vector_model,
        "vector_error": vector_error,
    }


def index_freshness(conn: sqlite3.Connection,
                    project_id: str | None = None) -> dict:
    """source 级索引新鲜度：文件改动未重索引、任务积压与失败。

    结果级的 `stale` 只说明单条命中是否过期；这里回答「整个数据源是否落后」，
    供检索工作台和 MCP 在给出证据前提示重新索引。失败任务也算 stale：
    这些文件当前没有可用正文，索引并不完整。
    """
    params: list = []
    project_join = ""
    if project_id:
        project_join = (
            " JOIN project_assets pa ON pa.asset_id = l.asset_id"
            " AND pa.project_id = ? AND pa.membership_type != 'excluded'")
        params.append(project_id)
    stale_rows = conn.execute(
        "SELECT l.source_id, COUNT(*) AS n FROM asset_locations l"
        " JOIN content_versions v ON v.asset_id = l.asset_id AND v.is_current = 1"
        + project_join +
        " WHERE l.availability = 'AVAILABLE' AND l.mtime_ns > v.mtime_ns"
        " GROUP BY l.source_id", params).fetchall()
    stale_by_source = {row["source_id"]: int(row["n"]) for row in stale_rows}

    job_params: list = []
    job_join = ""
    if project_id:
        job_join = (
            " JOIN project_assets pa ON pa.asset_id = j.asset_id"
            " AND pa.project_id = ? AND pa.membership_type != 'excluded'")
        job_params.append(project_id)
    job_rows = conn.execute(
        "SELECT j.source_id, j.status, COUNT(*) AS n FROM index_jobs j"
        + job_join +
        " WHERE j.status IN ('pending', 'running', 'failed')"
        " GROUP BY j.source_id, j.status", job_params).fetchall()
    jobs: dict[tuple[str, str], int] = {
        (row["source_id"], row["status"]): int(row["n"]) for row in job_rows
    }

    indexed_rows = conn.execute(
        "SELECT l.source_id, MAX(a.created_at) AS last_indexed_at"
        " FROM parse_artifacts a"
        " JOIN content_versions v ON v.version_id = a.version_id AND v.is_current = 1"
        " JOIN asset_locations l ON l.asset_id = v.asset_id"
        + project_join +
        " GROUP BY l.source_id", params).fetchall()
    indexed_by_source = {
        row["source_id"]: row["last_indexed_at"] for row in indexed_rows
    }

    source_rows = conn.execute(
        "SELECT source_id, label, state, last_scan_at FROM sources"
        " ORDER BY created_at").fetchall()
    allowed_sources: set[str] | None = None
    if project_id:
        allowed_sources = {
            row["source_id"] for row in conn.execute(
                "SELECT DISTINCT l.source_id FROM asset_locations l"
                " JOIN project_assets pa ON pa.asset_id = l.asset_id"
                " WHERE pa.project_id = ? AND pa.membership_type != 'excluded'",
                (project_id,))
        }

    items = []
    for row in source_rows:
        if allowed_sources is not None and row["source_id"] not in allowed_sources:
            continue
        sid = row["source_id"]
        stale_files = stale_by_source.get(sid, 0)
        pending = jobs.get((sid, "pending"), 0) + jobs.get((sid, "running"), 0)
        failed = jobs.get((sid, "failed"), 0)
        items.append({
            "source_id": sid,
            "label": row["label"],
            "state": row["state"],
            "last_scan_at": row["last_scan_at"],
            "last_indexed_at": indexed_by_source.get(sid),
            "stale_files": stale_files,
            "pending_jobs": pending,
            "failed_jobs": failed,
            "stale": bool(stale_files or pending or failed),
        })
    return {
        "stale": any(item["stale"] for item in items),
        "stale_files": sum(item["stale_files"] for item in items),
        "pending_jobs": sum(item["pending_jobs"] for item in items),
        "failed_jobs": sum(item["failed_jobs"] for item in items),
        "sources": items,
    }


def stats(conn: sqlite3.Connection, project_id: str | None = None) -> dict:
    availability = {
        row["availability"]: row["n"]
        for row in conn.execute(
            "SELECT availability, COUNT(*) AS n FROM asset_locations GROUP BY availability"
        )
    }
    by_ext = [
        {"ext": row["ext"], "count": row["n"]}
        for row in conn.execute(
            "SELECT ext, COUNT(*) AS n FROM asset_locations"
            " WHERE availability = 'AVAILABLE' GROUP BY ext ORDER BY n DESC LIMIT 8"
        )
    ]
    sources = []
    for row in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM asset_locations l"
        "  WHERE l.source_id = s.source_id AND l.availability = 'AVAILABLE') AS available,"
        " (SELECT COUNT(*) FROM asset_locations l"
        "  WHERE l.source_id = s.source_id AND l.availability = 'STALE') AS stale"
        " FROM sources s ORDER BY s.created_at"
    ):
        sources.append({
            "source_id": row["source_id"],
            "label": row["label"],
            "root_uri": row["root_uri"],
            "state": row["state"],
            "health": row["health"],
            "last_scan_at": row["last_scan_at"],
            "l0_completed_at": row["l0_completed_at"],
            "l1_completed_at": row["l1_completed_at"],
            "l2_completed_at": row["l2_completed_at"],
            "full_coverage_at": row["full_coverage_at"],
            "available": row["available"],
            "stale": row["stale"],
        })
    available = availability.get("AVAILABLE", 0)
    content = indexer.coverage(conn)
    return {
        "files": {
            "available": available,
            "stale": availability.get("STALE", 0),
            "missing": availability.get("MISSING", 0),
            "offline": availability.get("OFFLINE", 0),
        },
        "by_ext": by_ext,
        "sources": sources,
        "coverage": {
            "directory_level": "L0",
            "directory_indexed": available,
            "content_level": "L1",
            "content_indexed": content["indexed"],
            "content_pending": content["pending"],
            "content_empty": content["empty"],
            "content_failed": content["failed"],
            "content_locked": content["locked"],
            "content_unsupported": content["unsupported"],
            "low_quality": content["low_quality"],
            "ocr_indexed": content["ocr_indexed"],
            "ocr_empty": content["ocr_empty"],
            "ocr_failed": content["ocr_failed"],
            "ocr_locked": content["ocr_locked"],
            "ocr_pending": content["ocr_pending"],
            "mixed_pdf": content["mixed_pdf"],
            "ocr_policy": content["ocr_policy"],
            "ocr_available": content["ocr_available"],
            "ocr_engine": content["ocr_engine"],
            "ocr_languages": content["ocr_languages"],
            "ocr_message": content["ocr_message"],
        },
        "maintenance": maintenance.stats(conn),
        "ingest": ingest.public_settings(conn),
        "full_coverage": indexer.full_coverage(conn),
        "freshness": index_freshness(conn, project_id),
    }
