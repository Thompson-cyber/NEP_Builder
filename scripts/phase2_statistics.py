#!/usr/bin/env python3
"""
JSONL Phase3 Results Statistics  —  Enhanced Version
遍历目录下所有 *_phase3_filtered.jsonl 文件，对每个文件单独输出统计报告。

新增维度：
  11. Hunk 行数（增删行数）分布
  12. Root Hunk 在原始顺序中的位置偏移
  13. 跨文件样本的文件扩展名分布
  14. 置信度 × 变更模式 交叉分析
  15. 单/跨文件 × Hunk 数量 交叉分析
  16. 汇总行（全文件 Markdown 表格）

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
    return sorted(Path(root_dir).rglob("*_phase3_filtered.jsonl"))


def get_ordered_hunk_files(entry: dict) -> set[str]:
    files = set()
    for hunk in entry.get("ordered_hunks") or []:
        fp = hunk.get("file_path", "")
        if fp:
            files.add(fp.replace("\\", "/"))
    return files


def get_hunk_line_stats(entry: dict) -> dict:
    """统计 ordered_hunks 中所有 hunk 的增删行数"""
    added = deleted = 0
    for hunk in entry.get("ordered_hunks") or []:
        diff = hunk.get("diff_content", "") or hunk.get("content", "") or ""
        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted += 1
    return {"added": added, "deleted": deleted, "total_changed": added + deleted}


def get_root_hunk_original_position(entry: dict) -> int:
    """
    root hunk 在 ordered_hunks 中 order_index=0，
    但我们想知道它在原始（未排序）commit 中的位置。
    用 hunk_order[0] 即为 root 在原始顺序中的索引。
    """
    ca = entry.get("causal_analysis") or {}
    hunk_order = ca.get("hunk_order") or []
    if hunk_order:
        return hunk_order[0]   # root 在原始顺序中的下标
    return -1


def get_file_extensions(entry: dict) -> list[str]:
    """提取 ordered_hunks 中所有文件的扩展名"""
    exts = []
    for hunk in entry.get("ordered_hunks") or []:
        fp = hunk.get("file_path", "")
        if fp:
            suffix = Path(fp).suffix.lower()
            exts.append(suffix if suffix else "(no ext)")
    return exts


def analyze_entry(entry: dict) -> dict:
    ca            = entry.get("causal_analysis") or {}
    ordered_hunks = entry.get("ordered_hunks") or []
    test_hunks    = entry.get("test_hunks") or []

    hunk_files     = get_ordered_hunk_files(entry)
    file_count     = len(hunk_files)
    is_single_file = (file_count == 1)
    hunk_count     = len(ordered_hunks)

    change_pattern   = ca.get("change_pattern") or "Unknown"
    is_single_req    = ca.get("is_single_requirement")
    confidence       = ca.get("confidence")
    has_req_summary  = bool(ca.get("requirement_summary"))
    dependency_label = entry.get("dependency_label") or "UNKNOWN"

    # 置信度分档
    if confidence is None:
        conf_bucket = "N/A"
    elif confidence >= 0.9:
        conf_bucket = "high(>=0.9)"
    elif confidence >= 0.7:
        conf_bucket = "mid(0.7~0.9)"
    else:
        conf_bucket = "low(<0.7)"

    # hunk 数量分档
    if hunk_count == 1:
        hunk_bucket = "1"
    elif hunk_count <= 3:
        hunk_bucket = "2-3"
    elif hunk_count <= 6:
        hunk_bucket = "4-6"
    else:
        hunk_bucket = "7+"

    # 文件数量分档
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

    # ── 新增维度 ──
    line_stats      = get_hunk_line_stats(entry)
    root_pos        = get_root_hunk_original_position(entry)
    file_exts       = get_file_extensions(entry)

    # root 位置分档
    if root_pos < 0:
        root_pos_bucket = "N/A"
    elif root_pos == 0:
        root_pos_bucket = "0 (already first)"
    elif root_pos == 1:
        root_pos_bucket = "1"
    elif root_pos == 2:
        root_pos_bucket = "2"
    else:
        root_pos_bucket = "3+"

    # 变更行数分档
    tc = line_stats["total_changed"]
    if tc == 0:
        change_lines_bucket = "0"
    elif tc <= 10:
        change_lines_bucket = "1-10"
    elif tc <= 30:
        change_lines_bucket = "11-30"
    elif tc <= 60:
        change_lines_bucket = "31-60"
    elif tc <= 100:
        change_lines_bucket = "61-100"
    else:
        change_lines_bucket = "100+"

    return {
        "repo"                : entry.get("repo", "unknown"),
        "is_single_file"      : is_single_file,
        "file_count"          : file_count,
        "file_bucket"         : file_bucket,
        "hunk_count"          : hunk_count,
        "hunk_bucket"         : hunk_bucket,
        "change_pattern"      : change_pattern,
        "is_single_req"       : is_single_req,
        "conf_bucket"         : conf_bucket,
        "confidence"          : confidence,
        "has_issue"           : bool(entry.get("issue_description")),
        "has_req_summary"     : has_req_summary,
        "dependency_label"    : dependency_label,
        "has_test_hunks"      : len(test_hunks) > 0,
        "test_hunk_count"     : len(test_hunks),
        # 新增
        "added_lines"         : line_stats["added"],
        "deleted_lines"       : line_stats["deleted"],
        "total_changed_lines" : line_stats["total_changed"],
        "change_lines_bucket" : change_lines_bucket,
        "root_pos"            : root_pos,
        "root_pos_bucket"     : root_pos_bucket,
        "file_exts"           : file_exts,
    }


# ─────────────────────────────────────────────
#  格式化输出
# ─────────────────────────────────────────────

SEP_WIDE = "═" * 64
SEP_THIN = "─" * 52

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

def avg(lst):
    return sum(lst) / len(lst) if lst else 0.0


# ─────────────────────────────────────────────
#  单文件统计
# ─────────────────────────────────────────────

def stats_one_file(fpath: Path) -> dict:
    """读取并统计单个 JSONL 文件，独立输出报告，返回汇总行数据"""

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
        return {}

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

    cnt_pattern        = Counter(a["change_pattern"]     for a in analyzed)
    cnt_file_bucket    = Counter(a["file_bucket"]        for a in analyzed)
    cnt_hunk_bucket    = Counter(a["hunk_bucket"]        for a in analyzed)
    cnt_conf           = Counter(a["conf_bucket"]        for a in analyzed)
    cnt_dep_label      = Counter(a["dependency_label"]   for a in analyzed)
    cnt_repo           = Counter(a["repo"]               for a in analyzed)
    cnt_change_lines   = Counter(a["change_lines_bucket"] for a in analyzed)
    cnt_root_pos       = Counter(a["root_pos_bucket"]    for a in analyzed)

    hunk_counts        = [a["hunk_count"]          for a in analyzed]
    file_counts        = [a["file_count"]          for a in analyzed]
    test_hunk_cnts     = [a["test_hunk_count"]     for a in analyzed]
    conf_vals          = [a["confidence"] for a in analyzed if a["confidence"] is not None]
    added_vals         = [a["added_lines"]         for a in analyzed]
    deleted_vals       = [a["deleted_lines"]       for a in analyzed]
    changed_vals       = [a["total_changed_lines"] for a in analyzed]
    root_pos_vals      = [a["root_pos"] for a in analyzed if a["root_pos"] >= 0]

    # 扩展名统计（跨文件样本）
    cross_exts = Counter()
    for a in analyzed:
        if not a["is_single_file"]:
            for ext in a["file_exts"]:
                cross_exts[ext] += 1

    # ════════════════════════════════════════
    # 2. 文件范围
    # ════════════════════════════════════════
    print_section("2. File Scope  [based on ordered_hunks only]")
    print(f"    Single-file  (1 file)  : {c_single_file:>6}  {pct(c_single_file, total)}")
    print(f"    Cross-file   (2+ files): {c_multi_file:>6}  {pct(c_multi_file, total)}")
    print(f"\n    File count distribution (per entry):")
    print_counter(cnt_file_bucket, total)
    print(f"\n    ordered_hunks file count — "
          f"min={min(file_counts)}  max={max(file_counts)}  "
          f"avg={avg(file_counts):.2f}")

    # ════════════════════════════════════════
    # 3. Hunk 数量
    # ════════════════════════════════════════
    print_section("3. Hunk Count  [ordered_hunks per entry]")
    print_counter(cnt_hunk_bucket, total)
    print(f"\n    min={min(hunk_counts)}  max={max(hunk_counts)}  avg={avg(hunk_counts):.2f}")

    # ════════════════════════════════════════
    # 4. 变更模式
    # ════════════════════════════════════════
    print_section("4. Change Pattern")
    print_counter(cnt_pattern, total)

    # ════════════════════════════════════════
    # 5. 单需求一致性
    # ════════════════════════════════════════
    print_section("5. is_single_requirement")
    print(f"    true   : {c_single_req_true:>6}  {pct(c_single_req_true, total)}")
    print(f"    false  : {c_single_req_false:>6}  {pct(c_single_req_false, total)}")
    print(f"    null   : {c_single_req_null:>6}  {pct(c_single_req_null, total)}")

    # ════════════════════════════════════════
    # 6. 置信度
    # ════════════════════════════════════════
    print_section("6. Confidence")
    print_counter(cnt_conf, total)
    if conf_vals:
        print(f"\n    min={min(conf_vals):.3f}  max={max(conf_vals):.3f}  "
              f"avg={avg(conf_vals):.3f}  "
              f"(N/A count={total - len(conf_vals)})")

    # ════════════════════════════════════════
    # 7. 依赖标签
    # ════════════════════════════════════════
    print_section("7. Dependency Label")
    print_counter(cnt_dep_label, total)

    # ════════════════════════════════════════
    # 8. 辅助字段
    # ════════════════════════════════════════
    print_section("8. Auxiliary Fields")
    print(f"    Has issue_description  : {c_has_issue:>6}  {pct(c_has_issue, total)}")
    print(f"    Has test_hunks         : {c_has_test:>6}  {pct(c_has_test, total)}")
    print(f"    Has requirement_summary: {c_has_summary:>6}  {pct(c_has_summary, total)}")
    if c_has_test:
        print(f"    test_hunks count       : "
              f"min={min(test_hunk_cnts)}  max={max(test_hunk_cnts)}  "
              f"avg={avg(test_hunk_cnts):.2f}")

    # ════════════════════════════════════════
    # 9. 仓库分布
    # ════════════════════════════════════════
    print_section("9. Repository Distribution (top 20)")
    print_counter(cnt_repo, total, top_n=20)

    # ════════════════════════════════════════
    # 10. 交叉分析：文件范围 × 变更模式
    # ════════════════════════════════════════
    print_section("10. Cross: File Scope × Change Pattern")
    cross = defaultdict(Counter)
    for a in analyzed:
        scope = "single-file" if a["is_single_file"] else "cross-file "
        cross[scope][a["change_pattern"]] += 1

    for scope in ["single-file", "cross-file "]:
        sub = cross[scope]
        sub_total = sum(sub.values())
        if sub_total == 0:
            continue
        print(f"\n    [{scope}]  n={sub_total}  {pct(sub_total, total)}")
        for pat, cnt in sub.most_common():
            print(f"      {pat:<32} {cnt:>5}  {pct(cnt, sub_total)}")

    # ════════════════════════════════════════
    # 11. 【新增】Hunk 变更行数分布
    # ════════════════════════════════════════
    print_section("11. [NEW] Changed Lines Distribution (added+deleted per entry)")
    print_counter(cnt_change_lines, total)
    if changed_vals:
        print(f"\n    added   — min={min(added_vals)}  max={max(added_vals)}  avg={avg(added_vals):.1f}")
        print(f"    deleted — min={min(deleted_vals)}  max={max(deleted_vals)}  avg={avg(deleted_vals):.1f}")
        print(f"    total   — min={min(changed_vals)}  max={max(changed_vals)}  avg={avg(changed_vals):.1f}")

    # ════════════════════════════════════════
    # 12. 【新增】Root Hunk 原始位置分布
    # ════════════════════════════════════════
    print_section("12. [NEW] Root Hunk Original Position (index in pre-sort order)")
    print_counter(cnt_root_pos, total)
    if root_pos_vals:
        non_zero = sum(1 for p in root_pos_vals if p > 0)
        print(f"\n    Root was NOT already first: {non_zero}/{len(root_pos_vals)} "
              f"({pct(non_zero, len(root_pos_vals))}) — reordering was meaningful")
        print(f"    avg original position: {avg(root_pos_vals):.2f}  "
              f"max: {max(root_pos_vals)}")

    # ════════════════════════════════════════
    # 13. 【新增】跨文件样本的文件扩展名分布
    # ════════════════════════════════════════
    print_section("13. [NEW] File Extensions in Cross-file Entries (top 15)")
    if cross_exts:
        ext_total = sum(cross_exts.values())
        for ext, cnt in cross_exts.most_common(15):
            b = bar(cnt, ext_total)
            print(f"    {ext:<20} {cnt:>6}  {pct(cnt, ext_total):>7}  {b}")
    else:
        print("    (no cross-file entries)")

    # ════════════════════════════════════════
    # 14. 【新增】置信度 × 变更模式 交叉分析
    # ════════════════════════════════════════
    print_section("14. [NEW] Cross: Confidence × Change Pattern")
    conf_cross = defaultdict(Counter)
    for a in analyzed:
        conf_cross[a["conf_bucket"]][a["change_pattern"]] += 1

    for bucket in ["high(>=0.9)", "mid(0.7~0.9)", "low(<0.7)", "N/A"]:
        sub = conf_cross.get(bucket)
        if not sub:
            continue
        sub_total = sum(sub.values())
        print(f"\n    [{bucket}]  n={sub_total}  {pct(sub_total, total)}")
        for pat, cnt in sub.most_common(5):
            print(f"      {pat:<32} {cnt:>5}  {pct(cnt, sub_total)}")

    # ════════════════════════════════════════
    # 15. 【新增】单/跨文件 × Hunk 数量 交叉分析
    # ════════════════════════════════════════
    print_section("15. [NEW] Cross: File Scope × Hunk Count Bucket")
    scope_hunk = defaultdict(Counter)
    for a in analyzed:
        scope = "single-file" if a["is_single_file"] else "cross-file "
        scope_hunk[scope][a["hunk_bucket"]] += 1

    for scope in ["single-file", "cross-file "]:
        sub = scope_hunk[scope]
        sub_total = sum(sub.values())
        if sub_total == 0:
            continue
        print(f"\n    [{scope}]  n={sub_total}  {pct(sub_total, total)}")
        for bucket, cnt in sorted(sub.items()):
            print(f"      {bucket:<10} {cnt:>5}  {pct(cnt, sub_total)}")

    print(f"\n  {'─'*60}")
    print(f"  End of: {fpath.name}")
    print(f"  {'─'*60}\n")

    # 返回用于最终汇总表的数据
    repo_name = fpath.stem.split("_")[0]
    return {
        "repo"        : repo_name,
        "total"       : total,
        "single_pct"  : c_single_file / total * 100,
        "cross_pct"   : c_multi_file  / total * 100,
        "avg_hunk"    : avg(hunk_counts),
        "avg_conf"    : avg(conf_vals),
        "avg_changed" : avg(changed_vals),
        "root_reorder_pct": (sum(1 for p in root_pos_vals if p > 0) / len(root_pos_vals) * 100) if root_pos_vals else 0,
        "patterns"    : cnt_pattern,
    }


# ─────────────────────────────────────────────
#  全局汇总 Markdown 表格
# ─────────────────────────────────────────────

PATTERN_COLS = [
    "Bug Fix", "Enhancement", "New Feature", "Refactoring",
    "Error Handling", "Performance Optimization",
    "Security Fix", "Deprecation", "Config Change", "Dependency Update",
]

def print_summary_table(summaries: list[dict]):
    if not summaries:
        return

    print("\n" + "═" * 80)
    print("  GLOBAL SUMMARY — Markdown Table")
    print("═" * 80)

    # 表头
    base_cols = ["仓库名", "样本数", "单文件%", "跨文件%",
                 "均Hunk数", "平均置信度", "均变更行数", "根因重排%"]
    pat_short = ["BugFix", "Enhance", "NewFeat", "Refactor",
                 "ErrHdl", "PerfOpt", "SecFix", "Deprec", "Config", "DepUpd"]
    all_cols = base_cols + pat_short

    header = "| " + " | ".join(all_cols) + " |"
    sep    = "|" + "|".join(["---"] * len(all_cols)) + "|"
    print(header)
    print(sep)

    # 合计累加
    tot_n = tot_sf = tot_cf = 0
    tot_hunk = tot_conf = tot_changed = tot_reorder = 0
    tot_conf_cnt = 0
    pat_totals = Counter()

    rows = []
    for s in summaries:
        n   = s["total"]
        sf  = s["single_pct"]
        cf  = s["cross_pct"]
        ah  = s["avg_hunk"]
        ac  = s["avg_conf"]
        acl = s["avg_changed"]
        rr  = s["root_reorder_pct"]
        pts = s["patterns"]

        pat_vals = [pts.get(p, 0) for p in PATTERN_COLS]

        row = (f"| {s['repo']} | {n} | {sf:.0f}% | {cf:.0f}% | "
               f"{ah:.2f} | {ac:.3f} | {acl:.1f} | {rr:.0f}% | "
               + " | ".join(str(v) for v in pat_vals) + " |")
        rows.append(row)

        tot_n       += n
        tot_sf      += sf * n
        tot_cf      += cf * n
        tot_hunk    += ah * n
        if ac > 0:
            tot_conf     += ac * n
            tot_conf_cnt += n
        tot_changed += acl * n
        tot_reorder += rr * n
        for p in PATTERN_COLS:
            pat_totals[p] += pts.get(p, 0)

    for r in rows:
        print(r)

    # 合计行
    pat_total_vals = [pat_totals.get(p, 0) for p in PATTERN_COLS]
    print(f"| **合计/均值** | **{tot_n}** | "
          f"{tot_sf/tot_n:.0f}% | {tot_cf/tot_n:.0f}% | "
          f"{tot_hunk/tot_n:.2f} | {tot_conf/tot_conf_cnt:.3f} | "
          f"{tot_changed/tot_n:.1f} | {tot_reorder/tot_n:.0f}% | "
          + " | ".join(f"**{v}**" for v in pat_total_vals) + " |")

    print(f"\n  变更模式全局占比：")
    for p in PATTERN_COLS:
        v = pat_totals[p]
        if v > 0:
            print(f"    {p:<30} {v:>5}  ({v/tot_n*100:.1f}%)")


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

    summaries = []
    for fpath in files:
        s = stats_one_file(fpath)
        if s:
            summaries.append(s)

    print_summary_table(summaries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Per-file statistics for *_phase3_filtered.jsonl  [Enhanced]"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\re_collected_phase1&2\collected_0418",
        help="Root directory to scan (default: hardcoded path)"
    )
    args = parser.parse_args()
    run(args.directory)
