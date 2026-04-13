#!/usr/bin/env python3
"""
benchmark_stats.py
──────────────────
自动化评估脚本：读取 JSONL 格式的 Benchmark 数据集，
统计多维度特征分布，输出可读报告并可选保存 JSON。

用法:
    python benchmark_stats.py --input data.jsonl
    python benchmark_stats.py --input data.jsonl --output report.json
"""

import json
import re
import argparse
import statistics
from collections import Counter
from typing import List, Dict, Any, Optional


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def simple_tokenize(text: str) -> List[str]:
    return re.findall(r'\w+|[^\w\s]', text)

def count_tokens(text: str) -> int:
    return len(simple_tokenize(text))

def safe_mean(lst):
    return round(statistics.mean(lst), 3) if lst else 0.0

def safe_median(lst):
    return round(statistics.median(lst), 3) if lst else 0.0

def safe_stdev(lst):
    return round(statistics.stdev(lst), 3) if len(lst) >= 2 else 0.0

def percentile(lst, p):
    if not lst:
        return 0.0
    s = sorted(lst)
    idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (idx - lo), 3)

def distribution_buckets(values, buckets):
    """将数值列表按 buckets 边界分桶，返回各桶计数"""
    result = {}
    edges = list(buckets)
    for i, edge in enumerate(edges):
        lo = edges[i - 1] if i > 0 else float('-inf')
        hi = edge
        label = f"<={hi}" if i == 0 else f"{lo+1}~{hi}"
        result[label] = sum(1 for v in values if lo < v <= hi)
    result[f">{edges[-1]}"] = sum(1 for v in values if v > edges[-1])
    return result


# ══════════════════════════════════════════════════════════════════
# Diff 解析
# ══════════════════════════════════════════════════════════════════

def parse_diff(diff_text: str) -> List[Dict]:
    """解析 unified diff，返回每个 hunk 的结构化字典列表"""
    hunks = []
    hunk_re = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
    cur = None
    for line in diff_text.splitlines():
        m = hunk_re.match(line)
        if m:
            if cur:
                hunks.append(cur)
            cur = dict(
                old_start=int(m.group(1)),
                old_count=int(m.group(2) or 1),
                new_start=int(m.group(3)),
                new_count=int(m.group(4) or 1),
                added=[], deleted=[], context=[]
            )
        elif cur is not None:
            if line.startswith('+') and not line.startswith('+++'):
                cur['added'].append(line[1:])
            elif line.startswith('-') and not line.startswith('---'):
                cur['deleted'].append(line[1:])
            elif line.startswith(' '):
                cur['context'].append(line[1:])
    if cur:
        hunks.append(cur)
    return hunks

def avg_indent(lines: List[str]) -> float:
    if not lines:
        return 0.0
    levels = [(len(l) - len(l.lstrip(' '))) / 4 for l in lines]
    return round(sum(levels) / len(levels), 2)

def extract_api_calls(lines: List[str]) -> List[str]:
    calls = set()
    for line in lines:
        calls.update(re.findall(r'(\w+)\s*\(', line))
    return list(calls)

def has_pattern(lines: List[str], pattern: str) -> bool:
    return bool(re.search(pattern, '\n'.join(lines)))


# ══════════════════════════════════════════════════════════════════
# 单条记录特征提取
# ══════════════════════════════════════════════════════════════════

def extract_record_features(record: Dict[str, Any]) -> Dict[str, Any]:
    """从单条 benchmark 记录中提取所有统计特征"""
    feat = {}

    # ── 1. Commit 元信息 ──────────────────────────────────────────
    feat['repo_name']         = record.get('repo_name', '')
    feat['commit_hash']       = record.get('hash', '')[:12]
    feat['is_merge']          = record.get('is_merge', False)
    feat['issue_count']       = len(record.get('issue_ids', []))
    feat['has_issue_ref']     = feat['issue_count'] > 0
    msg = record.get('msg', '')
    feat['commit_msg_length'] = len(msg)
    feat['commit_msg_tokens'] = count_tokens(msg)

    # ── 2. Source File 信息 ───────────────────────────────────────
    src_changes = record.get('source_changes', [])
    feat['source_file_count'] = len(src_changes)

    src_total_lines_list  = []
    src_total_tokens_list = []
    all_hunks_meta        = []   # 每个 hunk 的特征字典

    for sc in src_changes:
        src_code    = sc.get('source_code', '')
        file_lines  = src_code.count('\n') + 1 if src_code else 0
        file_tokens = count_tokens(src_code)
        src_total_lines_list.append(file_lines)
        src_total_tokens_list.append(file_tokens)

        # ── 3. Hunk 级别统计 ──────────────────────────────────────
        hunks = parse_diff(sc.get('diff', ''))
        for h in hunks:
            added_text   = '\n'.join(h['added'])
            deleted_text = '\n'.join(h['deleted'])

            fill_lines  = len(h['added'])
            if fill_lines>50:
                pass
            if fill_lines ==0:
                continue
            fill_chars  = len(added_text)
            fill_tokens = count_tokens(added_text)
            del_lines   = len(h['deleted'])
            del_tokens  = count_tokens(deleted_text)
            ctx_lines   = len(h['context'])

            fill_apis = extract_api_calls(h['added'])
            del_apis  = extract_api_calls(h['deleted'])

            hunk_meta = dict(
                # 位置信息
                new_start      = h['new_start'],
                position_ratio = round(h['new_start'] / file_lines, 4) if file_lines else 0,
                # 补全目标统计
                fill_lines     = fill_lines,
                fill_chars     = fill_chars,
                fill_tokens    = fill_tokens,
                fill_avg_line_len = round(fill_chars / fill_lines, 2) if fill_lines else 0,
                fill_indent_lvl   = avg_indent(h['added']),
                # 删除行统计
                del_lines      = del_lines,
                del_tokens     = del_tokens,
                # 净变化
                net_loc        = fill_lines - del_lines,
                # 上下文
                ctx_lines      = ctx_lines,
                # 内容特征标记
                has_condition  = has_pattern(h['added'], r'\b(if|elif|else|not)\b'),
                has_func_def   = has_pattern(h['added'], r'\bdef\s+\w+'),
                has_import     = has_pattern(h['added'], r'\bimport\b'),
                has_comment    = has_pattern(h['added'], r'^\s*#'),
                # API 变化分析
                api_introduced = list(set(fill_apis) - set(del_apis)),
                api_replaced   = list(set(del_apis)  - set(fill_apis)),
                api_overlap    = list(set(fill_apis) & set(del_apis)),
                fill_api_count = len(fill_apis),
                del_api_count  = len(del_apis),
            )
            all_hunks_meta.append(hunk_meta)

    feat['hunk_count'] = len(all_hunks_meta)

    # 汇总补全目标
    feat['total_fill_lines']  = sum(h['fill_lines']  for h in all_hunks_meta)
    feat['total_fill_chars']  = sum(h['fill_chars']  for h in all_hunks_meta)
    feat['total_fill_tokens'] = sum(h['fill_tokens'] for h in all_hunks_meta)
    feat['total_del_lines']   = sum(h['del_lines']   for h in all_hunks_meta)
    feat['total_del_tokens']  = sum(h['del_tokens']  for h in all_hunks_meta)
    feat['net_loc_change']    = sum(h['net_loc']     for h in all_hunks_meta)
    feat['total_ctx_lines']   = sum(h['ctx_lines']   for h in all_hunks_meta)

    # 补全内容特征汇总
    feat['fill_has_condition'] = any(h['has_condition'] for h in all_hunks_meta)
    feat['fill_has_func_def']  = any(h['has_func_def']  for h in all_hunks_meta)
    feat['fill_has_import']    = any(h['has_import']    for h in all_hunks_meta)
    feat['fill_has_comment']   = any(h['has_comment']   for h in all_hunks_meta)

    # API 变化汇总
    all_introduced = list({a for h in all_hunks_meta for a in h['api_introduced']})
    all_replaced   = list({a for h in all_hunks_meta for a in h['api_replaced']})
    feat['api_introduced_count'] = len(all_introduced)
    feat['api_replaced_count']   = len(all_replaced)
    feat['api_change_type'] = (
        'pure_add'     if all_introduced and not all_replaced else
        'pure_replace' if all_replaced   and not all_introduced else
        'mixed'        if all_introduced and all_replaced else
        'refactor'
    )

    # hunk 位置（取第一个 hunk 代表整体）
    feat['hunk_position_ratio'] = all_hunks_meta[0]['position_ratio'] if all_hunks_meta else 0.0
    feat['fill_indent_level']   = all_hunks_meta[0]['fill_indent_lvl'] if all_hunks_meta else 0.0

    # source file 汇总
    feat['src_file_total_lines']  = sum(src_total_lines_list)
    feat['src_file_total_tokens'] = sum(src_total_tokens_list)

    # ── 4. 测试文件统计 ───────────────────────────────────────────
    test_changes = record.get('test_changes', [])
    feat['test_file_count'] = len(test_changes)

    test_added_lines_total = 0
    test_del_lines_total   = 0
    new_test_funcs_all     = []
    test_file_tokens_total = 0
    test_file_lines_total  = 0

    for tc in test_changes:
        diff = tc.get('diff', '')
        src  = tc.get('source_code', '')
        added_lines   = [l[1:] for l in diff.splitlines()
                         if l.startswith('+') and not l.startswith('+++')]
        deleted_lines = [l[1:] for l in diff.splitlines()
                         if l.startswith('-') and not l.startswith('---')]
        test_added_lines_total += len(added_lines)
        test_del_lines_total   += len(deleted_lines)
        # 新增测试函数（diff 中 +def test_ 开头）
        new_funcs = re.findall(r'^\+def\s+(test_\w+)', diff, re.MULTILINE)
        new_test_funcs_all.extend(new_funcs)
        test_file_lines_total  += src.count('\n') + 1 if src else 0
        test_file_tokens_total += count_tokens(src)

    feat['test_added_lines']        = test_added_lines_total
    feat['test_del_lines']          = test_del_lines_total
    feat['new_test_count']          = len(new_test_funcs_all)
    feat['new_test_functions']      = new_test_funcs_all
    feat['test_file_total_lines']   = test_file_lines_total
    feat['test_file_total_tokens']  = test_file_tokens_total

    # ── 5. 上下文信息（TODO）─────────────────────────────────────
    # TODO: 需要访问仓库其他文件，暂不实现
    # feat['cross_file_deps']       = TODO  # 跨文件依赖数量
    # feat['import_graph_depth']    = TODO  # 依赖图深度
    # feat['context_window_tokens'] = TODO  # 实际 prompt 上下文 token 数

    # ── 6. 综合难度评估 ───────────────────────────────────────────
    score  = 0
    score += min(feat['total_fill_lines'], 10)           # 行数，最多 10 分
    score += min(feat['total_fill_tokens'] // 10, 10)    # token 数，最多 10 分
    score += 5 if feat['fill_has_condition']    else 0   # 含条件逻辑
    score += 5 if feat['api_introduced_count'] > 0 else 0  # 引入新 API
    score += 3 if feat['fill_has_import']       else 0   # 含 import
    feat['difficulty_score'] = score
    feat['difficulty_level'] = (
        'easy'   if score <= 10 else
        'medium' if score <= 20 else
        'hard'
    )

    return feat


# ══════════════════════════════════════════════════════════════════
# 聚合统计
# ══════════════════════════════════════════════════════════════════

def aggregate_stats(all_features: List[Dict]) -> Dict:
    """对所有记录的特征进行聚合统计"""
    n = len(all_features)
    if n == 0:
        return {'total_records': 0}

    def collect(key):
        return [f[key] for f in all_features if key in f]

    def num_stats(key):
        vals = collect(key)
        return {
            'mean':   safe_mean(vals),
            'median': safe_median(vals),
            'stdev':  safe_stdev(vals),
            'min':    min(vals) if vals else 0,
            'max':    max(vals) if vals else 0,
            'p25':    percentile(vals, 25),
            'p75':    percentile(vals, 75),
            'p90':    percentile(vals, 90),
        }

    def bool_rate(key):
        vals = collect(key)
        true_cnt = sum(1 for v in vals if v)
        return {'count': true_cnt, 'rate': round(true_cnt / n, 4) if n else 0}

    def counter_dist(key):
        return dict(Counter(collect(key)).most_common())

    report = {'total_records': n}

    # 1. 仓库分布
    report['repos'] = counter_dist('repo_name')

    # 2. Commit 元信息
    report['commit_info'] = {
        'is_merge':      bool_rate('is_merge'),
        'has_issue_ref': bool_rate('has_issue_ref'),
        'issue_count':   num_stats('issue_count'),
        'msg_length':    num_stats('commit_msg_length'),
        'msg_tokens':    num_stats('commit_msg_tokens'),
    }

    # 3. Source File
    report['source_file'] = {
        'total_lines':  num_stats('src_file_total_lines'),
        'total_tokens': num_stats('src_file_total_tokens'),
        'file_count':   num_stats('source_file_count'),
    }

    # 4. Hunk 统计
    report['hunk'] = {
        'hunk_count':     num_stats('hunk_count'),
        'position_ratio': num_stats('hunk_position_ratio'),
        'position_distribution': distribution_buckets(
            collect('hunk_position_ratio'),
            [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        ),
        'indent_level': num_stats('fill_indent_level'),
        'ctx_lines':    num_stats('total_ctx_lines'),
    }

    # 5. 补全目标（核心）
    fill_lines_vals  = collect('total_fill_lines')
    fill_tokens_vals = collect('total_fill_tokens')
    report['fill_target'] = {
        'fill_lines': {
            **num_stats('total_fill_lines'),
            'distribution': distribution_buckets(
                fill_lines_vals, [1, 2, 3, 5, 8, 10, 15, 20]
            )
        },
        'fill_tokens': {
            **num_stats('total_fill_tokens'),
            'distribution': distribution_buckets(
                fill_tokens_vals, [10, 20, 30, 50, 80, 100, 150, 200]
            )
        },
        'fill_chars':     num_stats('total_fill_chars'),
        'del_lines':      num_stats('total_del_lines'),
        'del_tokens':     num_stats('total_del_tokens'),
        'net_loc_change': num_stats('net_loc_change'),
        'net_loc_distribution': counter_dist('net_loc_change'),
    }

    # 6. 补全内容特征
    report['fill_features'] = {
        'has_condition':   bool_rate('fill_has_condition'),
        'has_func_def':    bool_rate('fill_has_func_def'),
        'has_import':      bool_rate('fill_has_import'),
        'has_comment':     bool_rate('fill_has_comment'),
        'api_change_type': counter_dist('api_change_type'),
        'api_introduced':  num_stats('api_introduced_count'),
        'api_replaced':    num_stats('api_replaced_count'),
    }

    # 7. 测试文件
    report['test_file'] = {
        'test_file_count':        num_stats('test_file_count'),
        'test_added_lines':       num_stats('test_added_lines'),
        'test_del_lines':         num_stats('test_del_lines'),
        'new_test_count':         num_stats('new_test_count'),
        'test_file_total_lines':  num_stats('test_file_total_lines'),
        'test_file_total_tokens': num_stats('test_file_total_tokens'),
        'new_test_distribution':  distribution_buckets(
            collect('new_test_count'), [0, 1, 2, 3, 5]
        ),
    }

    # 8. 难度分布
    report['difficulty'] = {
        'score_stats':        num_stats('difficulty_score'),
        'level_distribution': counter_dist('difficulty_level'),
    }

    # 9. 上下文信息（TODO）
    report['context_info'] = {
        '_note': 'TODO: 需要访问仓库其他文件，暂不实现',
        # 'cross_file_deps':       TODO,
        # 'import_graph_depth':    TODO,
        # 'context_window_tokens': TODO,
    }

    return report


# ══════════════════════════════════════════════════════════════════
# 报告渲染
# ══════════════════════════════════════════════════════════════════

def render_num(d: Dict) -> str:
    return (f"mean={d['mean']}, median={d['median']}, stdev={d['stdev']}, "
            f"min={d['min']}, max={d['max']}, "
            f"p25={d['p25']}, p75={d['p75']}, p90={d['p90']}")

def render_bool(d: Dict) -> str:
    return f"{d['count']} 条 ({d['rate']*100:.1f}%)"

def bar_chart(cnt, total, width=20) -> str:
    return '█' * int(cnt / total * width) if cnt and total else ''

def print_report(report: Dict):
    n = report['total_records']
    SEP = "═" * 64

    print(f"\n{SEP}")
    print(f"  📊  Benchmark 数据集统计报告   (共 {n} 条记录)")
    print(SEP)

    # 1. 仓库分布
    print("\n▌ 1. 仓库分布")
    for repo, cnt in report['repos'].items():
        print(f"    {repo:<30} {cnt:>5} 条  ({cnt/n*100:.1f}%)")

    # 2. Commit 元信息
    ci = report['commit_info']
    print("\n▌ 2. Commit 元信息")
    print(f"    Merge Commit:       {render_bool(ci['is_merge'])}")
    print(f"    含 Issue 引用:      {render_bool(ci['has_issue_ref'])}")
    print(f"    Issue 数量:         {render_num(ci['issue_count'])}")
    print(f"    Commit Msg 长度:    {render_num(ci['msg_length'])}")
    print(f"    Commit Msg Tokens:  {render_num(ci['msg_tokens'])}")

    # 3. Source File
    sf = report['source_file']
    print("\n▌ 3. Source File 信息")
    print(f"    文件行数:           {render_num(sf['total_lines'])}")
    print(f"    文件 Token 数:      {render_num(sf['total_tokens'])}")
    print(f"    修改文件数:         {render_num(sf['file_count'])}")

    # 4. Hunk
    hk = report['hunk']
    print("\n▌ 4. Hunk 统计")
    print(f"    Hunk 数量:          {render_num(hk['hunk_count'])}")
    print(f"    Hunk 相对位置:      {render_num(hk['position_ratio'])}")
    print(f"    缩进层级:           {render_num(hk['indent_level'])}")
    print(f"    上下文行数:         {render_num(hk['ctx_lines'])}")
    print(f"    Hunk 位置分布:")
    max_cnt = max(hk['position_distribution'].values(), default=1)
    for bucket, cnt in hk['position_distribution'].items():
        print(f"      {bucket:<12} {cnt:>4} 条  {bar_chart(cnt, max_cnt)}")

    # 5. 补全目标
    ft = report['fill_target']
    print("\n▌ 5. 补全目标统计（核心）")
    print(f"    补全行数:           {render_num(ft['fill_lines'])}")
    print(f"    补全 Token 数:      {render_num(ft['fill_tokens'])}")
    print(f"    补全字符数:         {render_num(ft['fill_chars'])}")
    print(f"    删除行数:           {render_num(ft['del_lines'])}")
    print(f"    删除 Token 数:      {render_num(ft['del_tokens'])}")
    print(f"    净行数变化:         {render_num(ft['net_loc_change'])}")
    print(f"\n    补全行数分布:")
    max_cnt = max(ft['fill_lines']['distribution'].values(), default=1)
    for bucket, cnt in ft['fill_lines']['distribution'].items():
        print(f"      {bucket:<12} {cnt:>4} 条  {bar_chart(cnt, max_cnt)}")
    print(f"\n    补全 Token 分布:")
    max_cnt = max(ft['fill_tokens']['distribution'].values(), default=1)
    for bucket, cnt in ft['fill_tokens']['distribution'].items():
        print(f"      {bucket:<12} {cnt:>4} 条  {bar_chart(cnt, max_cnt)}")
    print(f"\n    净行数变化分布 (Top 10):")
    for delta, cnt in list(ft['net_loc_distribution'].items())[:10]:
        print(f"      {str(delta):<8} {cnt:>4} 条")

    # 6. 补全内容特征
    ff = report['fill_features']
    print("\n▌ 6. 补全内容特征")
    print(f"    含条件逻辑:         {render_bool(ff['has_condition'])}")
    print(f"    含函数定义:         {render_bool(ff['has_func_def'])}")
    print(f"    含 import:          {render_bool(ff['has_import'])}")
    print(f"    含注释:             {render_bool(ff['has_comment'])}")
    print(f"    新引入 API 数量:    {render_num(ff['api_introduced'])}")
    print(f"    被替换 API 数量:    {render_num(ff['api_replaced'])}")
    print(f"    API 变更类型分布:")
    for t, cnt in ff['api_change_type'].items():
        print(f"      {t:<20} {cnt:>4} 条  ({cnt/n*100:.1f}%)")

    # 7. 测试文件
    tf = report['test_file']
    print("\n▌ 7. 测试文件统计")
    print(f"    测试文件数:         {render_num(tf['test_file_count'])}")
    print(f"    测试文件总行数:     {render_num(tf['test_file_total_lines'])}")
    print(f"    测试文件 Token 数:  {render_num(tf['test_file_total_tokens'])}")
    print(f"    测试新增行数:       {render_num(tf['test_added_lines'])}")
    print(f"    测试删除行数:       {render_num(tf['test_del_lines'])}")
    print(f"    新增测试函数数:     {render_num(tf['new_test_count'])}")
    print(f"    新增测试函数数分布:")
    max_cnt = max(tf['new_test_distribution'].values(), default=1)
    for bucket, cnt in tf['new_test_distribution'].items():
        print(f"      {bucket:<12} {cnt:>4} 条  {bar_chart(cnt, max_cnt)}")

    # 8. 难度分布
    df = report['difficulty']
    print("\n▌ 8. 难度评估")
    print(f"    难度分数:           {render_num(df['score_stats'])}")
    print(f"    难度等级分布:")
    for lvl in ['easy', 'medium', 'hard']:
        cnt = df['level_distribution'].get(lvl, 0)
        print(f"      {lvl:<10} {cnt:>4} 条  ({cnt/n*100:.1f}%)  {bar_chart(cnt, n, 30)}")

    # 9. 上下文（TODO）
    print("\n▌ 9. 上下文信息统计")
    print(f"    ⚠️  {report['context_info']['_note']}")
    print(f"    待实现项:")
    print(f"      - cross_file_deps:       跨文件依赖数量")
    print(f"      - import_graph_depth:    依赖图深度")
    print(f"      - context_window_tokens: 实际 Prompt 上下文 Token 数")

    print(f"\n{SEP}\n")


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

def analyze_jsonl(filepath: str, output_json: Optional[str] = None):
    """
    读取 JSONL 文件，提取特征，输出统计报告。

    Args:
        filepath:    输入 JSONL 文件路径
        output_json: 可选，将聚合统计结果保存为 JSON 文件
    Returns:
        (report dict, all_features list)
    """
    all_features = []
    errors = []

    print(f"📂 正在读取: {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                feat = extract_record_features(record)
                all_features.append(feat)
            except Exception as e:
                errors.append({'line': line_no, 'error': str(e)})

    print(f"✅ 成功解析 {len(all_features)} 条，失败 {len(errors)} 条")
    if errors:
        print("⚠️  解析失败的行（前5条）:")
        for e in errors[:5]:
            print(f"   第 {e['line']} 行: {e['error']}")

    report = aggregate_stats(all_features)
    print_report(report)

    if output_json:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"💾 统计结果已保存至: {output_json}")

    return report, all_features


if __name__ == '__main__':
    # parser = argparse.ArgumentParser(
    #     description='Benchmark 数据集多维度统计分析工具'
    # )
    # parser.add_argument(
    #     '--input', '-i', required=True,
    #     help='输入 JSONL 文件路径'
    # )
    # parser.add_argument(
    #     '--output', '-o', default=None,
    #     help='可选：将聚合统计结果保存为 JSON 文件'
    # )
    # args = parser.parse_args()
    input = r'D:\Data\2025\信通院\single_point\single_coarse_autogpt.jsonl'
    output = input+".stats"
    analyze_jsonl(input, output)
