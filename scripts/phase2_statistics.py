#!/usr/bin/env python3
"""
JSONL Phase3 Results Statistics
遍历目录下所有 *_phase3_results.jsonl 文件，输出多维度统计报告。

Usage:
    python phase3_stats.py <directory>
"""

import os
import json
import argparse
from pathlib import Path
from collections import defaultdict, Counter


# ─────────────────────────────────────────────
#  工具函数
# ─────────────────────────────────────────────

def find_jsonl_files(root_dir: str) -> list[Path]:
    """递归查找所有 *_phase3_results.jsonl 文件"""
    root = Path(root_dir)
    return sorted(root.rglob("*_phase3_results.jsonl"))


def get_hunk_files(entry: dict) -> set[str]:
    """
    获取一条记录中所有 ordered_hunks 涉及的文件集合
    （即 trigger + ground truth 的所有源文件）
    """
    files = set()
    for hunk in entry.get("ordered_hunks", []):
        fp = hunk.get("file_path", "")
        if fp:
            files.add(fp.replace("\\", "/"))
    return files


def analyze_entry(entry: dict) -> dict:
    """分析单条记录，返回统计维度字典"""
    ca = entry.get("causal_analysis") or {}
    ordered_hunks = entry.get("ordered_hunks") or []
    test_hunks    = entry.get("test_hunks") or []

    # ── 文件维度 ──
    hunk_files = get_hunk_files(entry)
    file_count = len(hunk_files)
    is_single_file = (file_count == 1)

    # ── hunk 数量 ──
    hunk_count = len(ordered_hunks)

    # ── causal_analysis 字段 ──
    change_pattern   = ca.get("change_pattern") or "Unknown"
    is_single_req    = ca.get("is_single_requirement")
    confidence       = ca.get("confidence")
    has_req_summary  = bool(ca.get("requirement_summary"))
    dependency_label = entry.get("dependency_label") or "UNKNOWN"

    # ── 置信度分档 ──
    if confidence is None:
        conf_bucket = "N/A"
    elif confidence >= 0.9:
        conf_bucket = "high(>=0.9)"
    elif confidence >= 0.7:
        conf_bucket = "mid(0.7-0.9)"
    else:
        conf_bucket = "low(<0.7)"

    # ── 是否有 issue ──
    has_issue = bool(entry.get("issue_description"))

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
    if file_count == 1:
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
        "has_issue"       : has_issue,
        "has_req_summary" : has_req_summary,
        "dependency_label": dependency_label,
        "has_test_hunks"  : len(test_hunks) > 0,
    }


# ─────────────────────────────────────────────
#  格式化输出
# ─────────────────────────────────────────────

def pct(n, total):
    return f"{n/total*100:.1f}%" if total else "0.0%"

def print_counter(title: str, counter: Counter, total: int, top_n: int = None):
    print(f"\n  {title}")
    print(f"  {'─'*50}")
    items = counter.most_common(top_n) if top_n else sorted(counter.items(), key=lambda x: -x[1])
    for k, v in items:
        bar = "█" * int(v / total * 40)
        print(f"  {str(k):<30} {v:>6}  {pct(v,total):>7}  {bar}")

def print_section(title: str):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")


# ─────────────────────────────────────────────
#  主统计逻辑
# ─────────────────────────────────────────────

def run_stats(root_dir: str):
    files = find_jsonl_files(root_dir)

    if not files:
        print(f"[WARN] No *_phase3_results.jsonl files found under: {root_dir}")
        return

    print(f"\n{'═'*60}")
    print(f"  JSONL Phase3 Statistics")
    print(f"  Root: {root_dir}")
    print(f"{'═'*60}")
    print(f"\n  Found {len(files)} JSONL file(s):")
    for f in files:
        print(f"    • {f}")

    # ── 读取所有数据 ──
    all_entries  = []
    file_stats   = {}
    parse_errors = 0

    for fpath in files:
        count = 0
        with open(fpath, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry["_source_file"] = str(fpath)
                    all_entries.append(entry)
                    count += 1
                except json.JSONDecodeError as e:
                    parse_errors += 1
                    print(f"  [WARN] Parse error {fpath}:{lineno} — {e}")
        file_stats[str(fpath)] = count

    total = len(all_entries)
    if total == 0:
        print("\n  [WARN] No valid entries found.")
        return

    # ── 分析每条记录 ──
    analyzed = [analyze_entry(e) for e in all_entries]

    # ── 聚合计数器 ──
    c_single_file      = sum(1 for a in analyzed if a["is_single_file"])
    c_multi_file       = total - c_single_file
    c_single_req_true  = sum(1 for a in analyzed if a["is_single_req"] is True)
    c_single_req_false = sum(1 for a in analyzed if a["is_single_req"] is False)
    c_single_req_null  = sum(1 for a in analyzed if a["is_single_req"] is None)
    c_has_issue        = sum(1 for a in analyzed if a["has_issue"])
    c_has_test         = sum(1 for a in analyzed if a["has_test_hunks"])
    c_has_summary      = sum(1 for a in analyzed if a["has_req_summary"])

    cnt_pattern    = Counter(a["change_pattern"]    for a in analyzed)
    cnt_file_bucket= Counter(a["file_bucket"]       for a in analyzed)
    cnt_hunk_bucket= Counter(a["hunk_bucket"]       for a in analyzed)
    cnt_conf       = Counter(a["conf_bucket"]        for a in analyzed)
    cnt_dep_label  = Counter(a["dependency_label"]   for a in analyzed)
    cnt_repo       = Counter(a["repo"]               for a in analyzed)

    # ── 1. 总览 ──
    print_section("1. Overview")
    print(f"  Total entries          : {total}")
    print(f"  Source files scanned   : {len(files)}")
    print(f"  Parse errors           : {parse_errors}")
    print(f"\n  Per-file breakdown:")
    for fp, cnt in file_stats.items():
        print(f"    {Path(fp).name:<45} {cnt:>6} entries")

    # ── 2. 文件范围 ──
    print_section("2. File Scope (single-file vs cross-file)")
    print(f"  Single-file  (all hunks in 1 file) : {c_single_file:>6}  {pct(c_single_file, total)}")
    print(f"  Cross-file   (hunks span 2+ files) : {c_multi_file:>6}  {pct(c_multi_file, total)}")
    print_counter("File count distribution", cnt_file_bucket, total)

    # ── 3. Hunk 数量 ──
    print_section("3. Hunk Count Distribution")
    print_counter("Hunks per entry", cnt_hunk_bucket, total)
    hunk_counts = [a["hunk_count"] for a in analyzed]
    print(f"\n  Min / Max / Avg hunks  : {min(hunk_counts)} / {max(hunk_counts)} / {sum(hunk_counts)/total:.2f}")

    # ── 4. 变更模式 ──
    print_section("4. Change Pattern Distribution")
    print_counter("change_pattern", cnt_pattern, total)

    # ── 5. 单需求一致性 ──
    print_section("5. Single-Requirement Coherence")
    print(f"  is_single_requirement = true  : {c_single_req_true:>6}  {pct(c_single_req_true, total)}")
    print(f"  is_single_requirement = false : {c_single_req_false:>6}  {pct(c_single_req_false, total)}")
    print(f"  is_single_requirement = null  : {c_single_req_null:>6}  {pct(c_single_req_null, total)}")

    # ── 6. 置信度 ──
    print_section("6. Confidence Distribution")
    print_counter("confidence bucket", cnt_conf, total)
    conf_vals = [a["confidence"] for a in analyzed if a["confidence"] is not None]
    if conf_vals:
        print(f"\n  Min / Max / Avg confidence : {min(conf_vals):.3f} / {max(conf_vals):.3f} / {sum(conf_vals)/len(conf_vals):.3f}")

    # ── 7. 依赖标签 ──
    print_section("7. Dependency Label Distribution")
    print_counter("dependency_label", cnt_dep_label, total)

    # ── 8. 辅助字段 ──
    print_section("8. Auxiliary Fields")
    print(f"  Has issue_description  : {c_has_issue:>6}  {pct(c_has_issue, total)}")
    print(f"  Has test_hunks         : {c_has_test:>6}  {pct(c_has_test, total)}")
    print(f"  Has requirement_summary: {c_has_summary:>6}  {pct(c_has_summary, total)}")

    # ── 9. 仓库分布 ──
    print_section("9. Repository Distribution (top 20)")
    print_counter("repo", cnt_repo, total, top_n=20)

    # ── 10. 交叉分析 ──
    print_section("10. Cross-Analysis: File Scope × Change Pattern")
    cross = defaultdict(Counter)
    for a in analyzed:
        scope = "single-file" if a["is_single_file"] else "cross-file"
        cross[scope][a["change_pattern"]] += 1
    for scope in ["single-file", "cross-file"]:
        sub_total = sum(cross[scope].values())
        print(f"\n  [{scope}]  total={sub_total}  {pct(sub_total, total)}")
        for pat, cnt in cross[scope].most_common():
            print(f"    {pat:<35} {cnt:>5}  {pct(cnt, sub_total)}")

    print(f"\n{'═'*60}")
    print("  Done.")
    print(f"{'═'*60}\n")


# ─────────────────────────────────────────────
#  入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    directory = r"D:\Data\2025\CodeCompletion\Dataset\Outputs\re_collected_phase1&2"
    run_stats(directory)