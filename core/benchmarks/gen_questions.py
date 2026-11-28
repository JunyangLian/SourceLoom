"""从真实语料自动生成检索评测题（数值型精确事实）。

只做一件事：从已解析的正文里挑出「锚点：数值」的行，生成
`<锚点>是多少` 的问句，答案与所在文件都来自同一段正文，不需要模型。

生成结果写到 $SHIYUAN_HOME/evals/ 下（不入库）；仓库里不放真实问句。

    cd core
    python -m benchmarks.gen_questions --count 80
    python -m benchmarks.retrieval_eval --dataset ../data/evals/retrieval_seed.jsonl \
        --dataset ../data/evals/retrieval_auto.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shiyuan import config  # noqa: E402

_NUMBER = re.compile(r"^[0-9][0-9,.\-]*$")
_CONNECTOR = re.compile(r"^(.{2,40}?)(?:是|为)\s*([0-9][0-9,.\-]{1,})$")
_GENERIC = {
    "版本", "年份", "日期", "时间", "编号", "序号", "数量", "总计", "合计",
    "页码", "页数", "行数", "列数", "大小", "体积", "结果", "内容", "备注",
    "id", "no", "number", "total", "count", "size", "value", "version",
}
_METRIC = ("率", "量", "数", "长度", "总数", "个数", "总量", "比例", "占比",
           "分值", "得分", "权重", "评分", "面积", "浓度", "温度", "质量",
           "金额", "费用", "成绩", "分数", "人数", "天数", "值")
# 含个人信息的列名一律跳过，避免把名单类表格变成评测题。
_PRIVATE = re.compile(
    r"姓名|学号|工号|班级|专业|学院|年级|性别|民族|政治面貌|联系方式|电话|手机"
    r"|邮箱|微信|qq|地址|宿舍|身份证|未填写|备注|说明|序号|编号|排名"
)
# 时间/编号类锚点问「是多少」不成句，且数值本身没有检索价值。
_NOT_METRIC = re.compile(
    r"(时间|日期|时刻|年份|年度|号码|编号|序号|学号|工号|代码|排名|邮编|批次)$",
    re.IGNORECASE)
_DATE_VALUE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$")
_THOUSANDS = re.compile(r"^[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$")
# SSR 基序（TGTGT/CAGAAAATAGATG）只有 ACGT，不是可理解的指标名。
_DNA_MOTIF = re.compile(r"^[ACGT]+$")
_MOTIF_PAREN = re.compile(r"[（(]\s*[ACGT]{4,}\s*[)）]")
# 行首标点通常来自 markdown 列表/表格片段（"> 整理日期"、"/X runs"）。
_FRAGMENT_START = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]")
# 句子片段（"的量大数量"）不是字段名。
_FUNCTION_START = re.compile(
    r"^(的|了|是|在|与|和|为|即|其|该|这|那|所|由|于|把|被|将|对|从|以|而|并|或|也|都|就)")
_FUNCTION_END = re.compile(
    r"(的|了|是|在|与|和|为|即|其|该|这|那|所|由|于|把|被|将|对|从|以|而|并|或|也|都|就"
    r"|吗|呢|吧|啊)$")
# 中文序号前缀（"三、营业利润"）是报表行号，不是字段名。
_CN_ORDINAL = re.compile(r"^[一二三四五六七八九十]+、")
# 表格/正文里常见、但不足以定位文件的泛词。
_EXTRA_GENERIC = {
    "对应位置", "对应值", "相关参数", "相关数据", "具体情况", "主要内容",
    "其他", "其它", "以上", "如下", "详情", "详见",
    "other", "others", "unknown", "total", "value", "item", "items",
}
# 文件名 + 体积/行数不是可回答的内容指标。
_FILE_NAME = re.compile(r"\.(?:tsv|csv|xlsx|docx|md|txt|pdf|py|json|doc|pptx)$",
                        re.IGNORECASE)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_anchor(text: str) -> str:
    anchor = text.strip(" -—|:：\t")
    anchor = re.sub(r"^\d+[.、]\s*", "", anchor)
    return anchor


def _split_pair(line: str) -> tuple[str, str] | None:
    """只接受带明确分隔符（：| 制表符）或「是/为」连接的行，避免人名+编号误判。"""
    for separator in ("：", ":", "|", "\t"):
        if separator in line:
            left, _, right = line.partition(separator)
            return left, right.strip()
    match = _CONNECTOR.match(line)
    if match:
        return match.group(1), match.group(2)
    return None


def _sources(conn: sqlite3.Connection, anchor: str, value: str,
             fallback: str, limit: int = 40) -> list[str]:
    """同一事实常同时出现在多个版本/摘录里，只标注一个文件会造成假失败。"""
    if anchor.isascii():
        sql = (
            "SELECT DISTINCT l.path FROM chunks c"
            " JOIN asset_locations l ON l.asset_id = c.asset_id"
            " WHERE l.availability = 'AVAILABLE'"
            " AND instr(lower(c.text), lower(?)) > 0"
            " AND instr(lower(c.text), lower(?)) > 0"
            " LIMIT ?"
        )
    else:
        sql = (
            "SELECT DISTINCT l.path FROM chunks c"
            " JOIN asset_locations l ON l.asset_id = c.asset_id"
            " WHERE l.availability = 'AVAILABLE'"
            " AND instr(c.text, ?) > 0 AND instr(c.text, ?) > 0"
            " LIMIT ?"
        )
    names: list[str] = []
    for row in conn.execute(sql, (anchor, value, limit)):
        name = Path(row[0]).name
        if name not in names:
            names.append(name)
    return names or [fallback]


def build(conn: sqlite3.Connection, count: int) -> list[dict]:
    # 按文件分层采样：每个文件最多 12 段，保证跨 1000+ 文件都有覆盖；
    # 全局 ORDER BY RANDOM() 会把写死 LIMIT 集中在少数大文件上。
    rows = conn.execute(
        "SELECT text, path FROM ("
        "  SELECT c.text, l.path,"
        "         ROW_NUMBER() OVER (PARTITION BY c.asset_id ORDER BY RANDOM()) AS rn"
        "  FROM chunks c JOIN asset_locations l ON l.asset_id = c.asset_id"
        "  WHERE l.availability = 'AVAILABLE'"
        "  AND l.ext IN ('md','txt','docx','xlsx','csv','pdf')"
        "  AND length(c.text) BETWEEN 40 AND 1500"
        ") WHERE rn <= 12"
    ).fetchall()

    candidates: list[dict] = []
    seen_anchor: set[str] = set()
    seen_file: dict[str, int] = {}
    for row in rows:
        name = Path(row["path"]).name
        if seen_file.get(name, 0) >= 5:
            continue
        for line in (row["text"] or "").splitlines():
            line = line.strip()
            if not line or len(line) > 80:
                continue
            pair = _split_pair(line)
            if pair is None:
                continue
            if pair[0].rstrip().endswith(("-", "—")):
                # "Tetra-" 被从中间截断，作为锚点没有意义。
                continue
            anchor = _clean_anchor(pair[0]).replace("\u200b", "").strip()
            value = pair[1].split()[0] if pair[1].split() else ""
            if not _NUMBER.match(value):
                continue
            if (not anchor or anchor.lower() in _GENERIC
                    or anchor.lower() in _EXTRA_GENERIC):
                continue
            if len(anchor) < 3 or len(anchor) > 24:
                continue
            if any(char.isdigit() for char in anchor) or anchor.endswith("_"):
                continue
            if _FRAGMENT_START.match(anchor) or "/" in anchor or ">" in anchor:
                continue
            if _FUNCTION_START.match(anchor) or _FUNCTION_END.search(anchor):
                continue
            if _CN_ORDINAL.match(anchor):
                continue
            if _NOT_METRIC.search(anchor):
                continue
            if len(anchor) >= 3 and _DNA_MOTIF.match(anchor):
                continue
            if _MOTIF_PAREN.search(anchor):
                continue
            if _FILE_NAME.search(anchor):
                continue
            # 纯小写英文多半是表格首行片段（gene / ontigs / unknown）。
            if anchor.isascii() and anchor.islower() and "_" not in anchor:
                continue
            if value.startswith("0"):
                continue
            if _DATE_VALUE.match(value):
                continue
            if "," in value and not _THOUSANDS.match(value):
                # "35,0" 这类是列拼接残留，不是数量。
                continue
            digits = value.replace(",", "").replace(".", "").replace("-", "")
            if len(digits) < 2 or len(digits) >= 9:
                # 过长多半是学号 / accession / 流水号，不是可回答的指标。
                continue
            if anchor in seen_anchor:
                continue
            if _PRIVATE.search(anchor):
                continue
            # 指标类锚点直接问「是多少」；其余要求 ≥4 个字符（中文人名通常 2–3 字），
            # 用「对应的数值是多少」避免生成不通顺的问句。
            metric = anchor.endswith(_METRIC)
            if not metric and len(anchor) < 4:
                continue
            seen_anchor.add(anchor)
            seen_file[name] = seen_file.get(name, 0) + 1
            candidates.append({
                "anchor": anchor,
                "value": value,
                "name": name,
                "line": line,
                "metric": metric,
            })
            break

    # 指标类锚点（…率/量/数/长度）最像真实问句，优先；其余仅作补充。
    candidates.sort(key=lambda item: (not item["metric"], len(item["anchor"])))
    items: list[dict] = []
    for candidate in candidates[:count]:
        query = (f"{candidate['anchor']}是多少" if candidate["metric"]
                 else f"{candidate['anchor']} 对应的数值是多少")
        items.append({
            "id": f"auto-{len(items) + 1:03d}",
            "query": query,
            "type": "exact",
            "source": "auto",
            "expect": _sources(conn, candidate["anchor"], candidate["value"],
                               candidate["name"]),
            "answer": candidate["value"],
            "notes": f"自动生成 · {candidate['line'][:60]}",
        })
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成真实语料数值问句")
    parser.add_argument("--db", default=None, help="知识库路径，默认 app_home")
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--out", default=None,
                        help="输出 JSONL，默认 $SHIYUAN_HOME/evals/retrieval_auto.jsonl")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else config.db_path()
    if not db_path.exists():
        raise SystemExit(f"数据库不存在：{db_path}")
    out = Path(args.out) if args.out else config.app_home() / "evals" / "retrieval_auto.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(db_path)
    try:
        items = build(conn, max(1, args.count))
    finally:
        conn.close()

    out.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
        encoding="utf-8",
    )
    print(f"生成 {len(items)} 题 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
