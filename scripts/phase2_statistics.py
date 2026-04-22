#!/usr/bin/env python3
"""
JSONL Phase3 Results Statistics
遍历目录下所有 *_phase3_filtered.jsonl 文件，对每个文件单独输出统计报告。

Usage:
    python phase3_stats.py <directory>
    python phase3_stats.py   # 使用脚本内硬编码路径
"""

import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter


# ─────────────────────────────────────────────
#  工具函数
# ─────────────────────────────────────────────

def find_jsonl_files(root_dir: str) -> list[Path]:
    """递归查找所有 *_phase3_filtered.jsonl 文件"""
    return sorted(Path(root_dir).rglob("*_phase3_filtered.jsonl"))


def get_ordered_hunk_files(entry: dict) -> set[str]:
    """
    只从 ordered_hunks 中提取涉及的文件集合（统一用 / 分隔）。
    跨文件判断依据：去重后文件数 > 1
    """
    files = set()
    for hunk in entry.get("ordered_hunks") or []:
        fp = hunk.get("file_path", "")
        if fp:
            files.add(fp.replace("\\", "/"))
    return files


def analyze_entry(entry: dict) -> dict:
    """分析单条记录，返回各统计维度"""
    ca            = entry.get("causal_analysis") or {}
    ordered_hunks = entry.get("ordered_hunks") or []
    test_hunks    = entry.get("test_hunks") or []

    # ── 文件维度（只看 ordered_hunks）──
    hunk_files     = get_ordered_hunk_files(entry)
    file_count     = len(hunk_files)
    is_single_file = (file_count == 1)

    # ── hunk 数量 ──
    hunk_count = len(ordered_hunks)

    # ── causal_analysis 字段 ──
    change_pattern   = ca.get("change_pattern") or "Unknown"
    is_single_req    = ca.get("is_single_requirement")   # True / False / None
    confidence       = ca.get("confidence")               # float / None
    has_req_summary  = bool(ca.get("requirement_summary"))
    dependency_label = entry.get("dependency_label") or "UNKNOWN"

    # ── 置信度分档 ──
    if confidence is None:
        conf_bucket = "N/A"
    elif confidence >= 0.9:
        conf_bucket = "high(>=0.9)"
    elif confidence >= 0.7:
        conf_bucket = "mid(0.7~0.9)"
    else:
        conf_bucket = "low(<0.7)"

    # ── hunk 数量分档 ──
    if hunk_count == 1:
        hunk_bucket = "1"
    elif hunk_count <= 3:
        hunk_bucket = "2-3"
    elif hunk_count <= 6:
        hunk_bucket = "4-6"
    else:
        hunk_bucket = "7+"

    # ── 文件数量分档 ──
    if file_count == 0:
        file_bucket = "0(empty)"
    elif file_count == 1:
        file_bucket = "1"
    elif file_count == 2:
        file_bucket = "2"
    elif file_count <= 4:
        file_bucket = "3-4"
    else:
        file_bucket = "5+"

    return {
        "repo"            : entry.get("repo", "unknown"),
        "is_single_file"  : is_single_file,
        "file_count"      : file_count,
        "file_bucket"     : file_bucket,
        "hunk_count"      : hunk_count,
        "hunk_bucket"     : hunk_bucket,
        "change_pattern"  : change_pattern,
        "is_single_req"   : is_single_req,
        "conf_bucket"     : conf_bucket,
        "confidence"      : confidence,
        "has_issue"       : bool(entry.get("issue_description")),
        "has_req_summary" : has_req_summary,
        "dependency_label": dependency_label,
        "has_test_hunks"  : len(test_hunks) > 0,
        "test_hunk_count" : len(test_hunks),
    }


# ─────────────────────────────────────────────
#  格式化输出
# ─────────────────────────────────────────────

SEP_WIDE  = "═" * 64
SEP_THIN  = "─" * 52

def pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "0.0%"

def bar(n: int, total: int, width: int = 36) -> str:
    filled = int(n / total * width) if total else 0
    return "█" * filled

def print_section(title: str):
    print(f"\n  ┌─ {title}")
    print(f"  └{'─' * (len(title) + 3)}")

def print_counter(counter: Counter, total: int, top_n: int = None, indent: int = 4):
    pad = " " * indent
    items = counter.most_common(top_n) if top_n else sorted(counter.items(), key=lambda x: -x[1])
    for k, v in items:
        b = bar(v, total)
        print(f"{pad}{str(k):<32} {v:>6}  {pct(v, total):>7}  {b}")


# ─────────────────────────────────────────────
#  单文件统计
# ─────────────────────────────────────────────

def stats_one_file(fpath: Path):
    """读取并统计单个 JSONL 文件，独立输出报告"""

    # ── 读取 ──
    entries      = []
    parse_errors = 0
    with open(fpath, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                parse_errors += 1
                print(f"    [WARN] Parse error line {lineno}: {e}")

    total = len(entries)

    print(f"\n{SEP_WIDE}")
    print(f"  FILE : {fpath.name}")
    print(f"  PATH : {fpath}")
    print(f"{SEP_WIDE}")

    if total == 0:
        print("  [WARN] No valid entries.\n")
        return

    # ── 分析 ──
    analyzed = [analyze_entry(e) for e in entries]

    # ── 聚合 ──
    c_single_file      = sum(1 for a in analyzed if a["is_single_file"])
    c_multi_file       = total - c_single_file
    c_single_req_true  = sum(1 for a in analyzed if a["is_single_req"] is True)
    c_single_req_false = sum(1 for a in analyzed if a["is_single_req"] is False)
    c_single_req_null  = sum(1 for a in analyzed if a["is_single_req"] is None)
    c_has_issue        = sum(1 for a in analyzed if a["has_issue"])
    c_has_test         = sum(1 for a in analyzed if a["has_test_hunks"])
    c_has_summary      = sum(1 for a in analyzed if a["has_req_summary"])

    cnt_pattern     = Counter(a["change_pattern"]    for a in analyzed)
    cnt_file_bucket = Counter(a["file_bucket"]       for a in analyzed)
    cnt_hunk_bucket = Counter(a["hunk_bucket"]       for a in analyzed)
    cnt_conf        = Counter(a["conf_bucket"]       for a in analyzed)
    cnt_dep_label   = Counter(a["dependency_label"]  for a in analyzed)
    cnt_repo        = Counter(a["repo"]              for a in analyzed)

    hunk_counts     = [a["hunk_count"]     for a in analyzed]
    file_counts     = [a["file_count"]     for a in analyzed]
    test_hunk_cnts  = [a["test_hunk_count"] for a in analyzed]
    conf_vals       = [a["confidence"] for a in analyzed if a["confidence"] is not None]

    # ════════════════════════════════════════
    # 1. 总览
    # ════════════════════════════════════════
    # print_section("1. Overview")
    # print(f"    Total entries          : {total}")
    # print(f"    Parse errors           : {parse_errors}")
    # print(f"    Repos covered          : {len(cnt_repo)}")

    # ════════════════════════════════════════
    # 2. 文件范围（核心）
    # ════════════════════════════════════════
    print_section("2. File Scope  [based on ordered_hunks only]")
    print(f"    Single-file  (1 file)  : {c_single_file:>6}  {pct(c_single_file, total)}")
    print(f"    Cross-file   (2+ files): {c_multi_file:>6}  {pct(c_multi_file, total)}")
    print(f"\n    File count distribution (per entry):")
    print_counter(cnt_file_bucket, total)
    print(f"\n    ordered_hunks file count — "
          f"min={min(file_counts)}  max={max(file_counts)}  "
          f"avg={sum(file_counts)/total:.2f}")
    #
    # # ════════════════════════════════════════
    # # 3. Hunk 数量
    # # ════════════════════════════════════════
    # print_section("3. Hunk Count  [ordered_hunks per entry]")
    # print_counter(cnt_hunk_bucket, total)
    # print(f"\n    min={min(hunk_counts)}  max={max(hunk_counts)}  avg={sum(hunk_counts)/total:.2f}")
    #
    # # ════════════════════════════════════════
    # # 4. 变更模式
    # # ════════════════════════════════════════
    # print_section("4. Change Pattern")
    # print_counter(cnt_pattern, total)
    #
    # # ════════════════════════════════════════
    # # 5. 单需求一致性
    # # ════════════════════════════════════════
    # print_section("5. is_single_requirement")
    # print(f"    true   : {c_single_req_true:>6}  {pct(c_single_req_true, total)}")
    # print(f"    false  : {c_single_req_false:>6}  {pct(c_single_req_false, total)}")
    # print(f"    null   : {c_single_req_null:>6}  {pct(c_single_req_null, total)}")
    #
    # # ════════════════════════════════════════
    # # 6. 置信度
    # # ════════════════════════════════════════
    # print_section("6. Confidence")
    # print_counter(cnt_conf, total)
    # if conf_vals:
    #     print(f"\n    min={min(conf_vals):.3f}  max={max(conf_vals):.3f}  "
    #           f"avg={sum(conf_vals)/len(conf_vals):.3f}  "
    #           f"(N/A count={total - len(conf_vals)})")
    #
    # # ════════════════════════════════════════
    # # 7. 依赖标签
    # # ════════════════════════════════════════
    # print_section("7. Dependency Label")
    # print_counter(cnt_dep_label, total)
    #
    # # ════════════════════════════════════════
    # # 8. 辅助字段
    # # ════════════════════════════════════════
    # print_section("8. Auxiliary Fields")
    # print(f"    Has issue_description  : {c_has_issue:>6}  {pct(c_has_issue, total)}")
    # print(f"    Has test_hunks         : {c_has_test:>6}  {pct(c_has_test, total)}")
    # print(f"    Has requirement_summary: {c_has_summary:>6}  {pct(c_has_summary, total)}")
    # if c_has_test:
    #     print(f"    test_hunks count       : "
    #           f"min={min(test_hunk_cnts)}  max={max(test_hunk_cnts)}  "
    #           f"avg={sum(test_hunk_cnts)/total:.2f}")
    #
    # # ════════════════════════════════════════
    # # 9. 仓库分布
    # # ════════════════════════════════════════
    # print_section("9. Repository Distribution (top 20)")
    # print_counter(cnt_repo, total, top_n=20)
    #
    # # ════════════════════════════════════════
    # # 10. 交叉分析：文件范围 × 变更模式
    # # ════════════════════════════════════════
    # print_section("10. Cross: File Scope × Change Pattern")
    # cross = defaultdict(Counter)
    # for a in analyzed:
    #     scope = "single-file" if a["is_single_file"] else "cross-file "
    #     cross[scope][a["change_pattern"]] += 1
    #
    # for scope in ["single-file", "cross-file "]:
    #     sub = cross[scope]
    #     sub_total = sum(sub.values())
    #     if sub_total == 0:
    #         continue
    #     print(f"\n    [{scope}]  n={sub_total}  {pct(sub_total, total)}")
    #     for pat, cnt in sub.most_common():
    #         print(f"      {pat:<32} {cnt:>5}  {pct(cnt, sub_total)}")
    #

    print(f"\n  {'─'*60}")
    print(f"  End of: {fpath.name}")
    print(f"  {'─'*60}\n")


# ─────────────────────────────────────────────
#  主入口
# ─────────────────────────────────────────────

def run(root_dir: str):
    files = find_jsonl_files(root_dir)
    if not files:
        print(f"[WARN] No *_phase3_filtered.jsonl files found under: {root_dir}")
        return
    print(f"\nFound {len(files)} JSONL file(s) under: {root_dir}")
    for f in files:
        print(f"  • {f}")

    for fpath in files:
        stats_one_file(fpath)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Per-file statistics for *_phase3_filtered.jsonl"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\re_collected_phase1&2\collected_0418",
        help="Root directory to scan (default: hardcoded path)"
    )
    args = parser.parse_args()
    run(args.directory)