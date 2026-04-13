import argparse
import gzip
import json
import os
from typing import Dict, Any, Tuple, Optional

from loguru import logger
from tqdm import tqdm

from core.types import CommitCandidate
from analysis.processor import CommitProcessor

def _checkpoint_filename(repo_name: str, use_graph: bool) -> str:
    mode = "graph" if use_graph else "no_graph"
    return f"{repo_name}_phase2_{mode}_checkpoint.json"

def _checkpoint_path(output_dir: str, repo_name: str, use_graph: bool) -> str:
    return os.path.join(output_dir, _checkpoint_filename(repo_name, use_graph))

def load_checkpoint(output_dir: str, repo_name: str, use_graph: bool) -> Dict[str, Any]:
    """加载断点文件，返回上次的进度信息。"""
    path = _checkpoint_path(output_dir, repo_name, use_graph)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        logger.info(
            f"[Resume] Checkpoint found. "
            f"Resuming from line {ckpt['processed_lines']} "
            f"(last output: {ckpt['stats']['total_output']} records)."
        )
        return ckpt
    return {"processed_lines": 0, "stats": None}


def save_checkpoint(output_dir: str, processed_lines: int, stats: Dict[str, Any], repo_name: str, use_graph: bool) -> None:
    """将当前进度原子写入断点文件（先写临时文件再 rename，防止写坏）。"""
    path = _checkpoint_path(output_dir, repo_name, use_graph)
    tmp_path = path + ".tmp"
    payload = {"processed_lines": processed_lines, "stats": stats}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)  # 原子替换


def clear_checkpoint(output_dir: str, repo_name: str, use_graph: bool) -> None:
    """处理完成后删除断点文件。"""
    path = _checkpoint_path(output_dir, repo_name, use_graph)
    if os.path.exists(path):
        os.remove(path)
        logger.info("[Resume] Checkpoint cleared after successful completion.")


def process_single_line(
    line: str, processor: CommitProcessor
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    处理单行 JSON 数据。
    Returns:
        (处理结果JSON字符串, 统计数据增量, 错误信息)
    """
    local_stats = {
        "is_success": False,
        "stage": "unknown",
        "insights": {
            "cross_file": 0, "single_file": 0, "cycles_detected": 0,
            "has_tests": 0, "total_hunks": 0, "total_dependencies": 0,
        },
        "error_msg": None,
    }

    try:
        data = json.loads(line)
        candidate = CommitCandidate(**data)
        result, proc_stats = processor.process(candidate)

        local_stats["stage"] = proc_stats.get("stage", "unknown")

        if result:
            local_stats["is_success"] = True
            insights = proc_stats.get("insights", {})

            cross_file_ratio = insights.get("cross_file_ratio", 0)
            local_stats["insights"]["cross_file"]          = 1 if cross_file_ratio > 0 else 0
            local_stats["insights"]["single_file"]         = 0 if cross_file_ratio > 0 else 1
            local_stats["insights"]["cycles_detected"]     = 1 if insights.get("has_cycle", False) else 0
            local_stats["insights"]["has_tests"]           = 1 if insights.get("has_tests", False) else 0
            local_stats["insights"]["total_hunks"]         = len(result.ordered_hunks)
            local_stats["insights"]["total_dependencies"]  = len(result.dependencies)  # no-graph 时为 0

            return result.model_dump_json(), local_stats, None
        else:
            local_stats["error_msg"] = proc_stats.get("error")
            return None, local_stats, None

    except Exception as e:
        return None, local_stats, str(e)


# ─────────────────────────────────────────────
# 初始化全量 stats 结构
# ─────────────────────────────────────────────

def _empty_stats() -> Dict[str, Any]:
    return {
        "total_input": 0,
        "total_output": 0,
        "errors": 0,
        "no_graph_mode": False,
        "filters": {
            "missing_source": 0, "slicing_error": 0, "slicing_empty": 0,
            "too_few_hunks": 0, "too_many_hunks": 0, "no_dependency": 0,
            "validation_fail": 0, "analysis_error": 0, "sorting_error": 0,
            "unknown": 0,
        },
        "insights": {
            "single_file": 0, "cross_file": 0, "cycles_detected": 0,
            "has_tests": 0, "total_hunks": 0, "total_dependencies": 0,
        },
    }


def _update_stats(stats: Dict[str, Any], local_stats: Dict[str, Any]) -> None:
    """将单条处理结果合并进全局 stats（原地修改）。"""
    if local_stats["is_success"]:
        stats["total_output"] += 1
        for k, v in local_stats["insights"].items():
            stats["insights"][k] += v
    else:
        stage = local_stats["stage"]
        stage_map = {
            "missing_source":             "missing_source",
            "slicing_error":              "slicing_error",
            "empty_hunks":                "slicing_empty",
            "too_few_source_hunks":       "too_few_hunks",
            "too_more_source_hunks":      "too_many_hunks",   # ✅ 补充缺失映射
            "validation_no_dependencies": "no_dependency",
            "analysis_error":             "analysis_error",
            "sorting_error":              "sorting_error",
        }
        if stage in stage_map:
            stats["filters"][stage_map[stage]] += 1
        elif stage.startswith("validation_"):
            stats["filters"]["validation_fail"] += 1
        else:
            stats["filters"]["unknown"] += 1


# ─────────────────────────────────────────────
# 报告打印
# ─────────────────────────────────────────────

def _print_report(stats: Dict[str, Any]) -> None:
    success_count = stats["total_output"]
    avg_hunks = stats["insights"]["total_hunks"] / success_count if success_count else 0
    avg_deps  = stats["insights"]["total_dependencies"] / success_count if success_count else 0
    mode_str  = "NO-GRAPH (LLM-filter)" if stats.get("no_graph_mode") else "GRAPH"

    report = f"""
==================================================
NEP Dataset Phase 2 Analysis Report (SEQUENTIAL)
Mode: {mode_str}
==================================================
[Flow]
Total Input            : {stats['total_input']}
Total Output           : {success_count}
Success Rate           : {(success_count / stats['total_input'] * 100) if stats['total_input'] else 0:.2f}%
Loop Exceptions        : {stats['errors']}

[Filtering Funnel]
1. Missing Source      : {stats['filters']['missing_source']}
2. Slicing Error       : {stats['filters']['slicing_error']}
3. Slicing Empty       : {stats['filters']['slicing_empty']}
4. Too Few Hunks (<2)  : {stats['filters']['too_few_hunks']}
5. Too Many Hunks      : {stats['filters']['too_many_hunks']}
6. No Dependency       : {stats['filters']['no_dependency']}
7. Validation Fail     : {stats['filters']['validation_fail']}
8. Analysis Error      : {stats['filters']['analysis_error']}
9. Sorting Error       : {stats['filters']['sorting_error']}
10. UNKNOWN REASON     : {stats['filters']['unknown']}

[Dataset Insights]
Single File            : {stats['insights']['single_file']}
Cross File             : {stats['insights']['cross_file']}
With Tests             : {stats['insights']['has_tests']}
Avg Hunks              : {avg_hunks:.2f}
Avg Edges              : {avg_deps:.2f}
==================================================
"""
    logger.info(report)


# ─────────────────────────────────────────────
# 核心函数（供 pipeline 调用）
# ─────────────────────────────────────────────

def run_phase2(
    input: str,
    output: str,
    repo_path: str,
    repo_name: str,
    reset: bool = False,
    use_graph: bool = True,
) -> Dict[str, Any]:
    from datetime import date
    mode = "graph" if use_graph else "no_graph"
    output = output or f"output/{repo_name}_{date.today().strftime('%Y-%m-%d')}_phase2_{mode}.jsonl"

    """
    执行 Phase 2 分析。
    Returns:
        最终 stats 字典
    """
    output_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(output_dir, exist_ok=True)

    if reset:
        clear_checkpoint(output_dir,repo_name,use_graph)
        logger.info("[Phase2][Resume] --reset flag detected, starting from scratch.")

    ckpt = load_checkpoint(output_dir,repo_name,use_graph)
    skip_lines = ckpt["processed_lines"]
    stats = ckpt["stats"] or _empty_stats()
    stats["no_graph_mode"] = not use_graph
    is_resume = skip_lines > 0

    write_mode = "a" if is_resume else "w"
    if is_resume:
        logger.info(f"[Phase2][Resume] Appending to existing output (mode='{write_mode}').")

    logger.info("[Phase2] Initializing CommitProcessor...")
    try:
        processor = CommitProcessor(repo_path=repo_path, use_graph=use_graph)
        mode_tag = "NO-GRAPH" if not use_graph else "GRAPH"
        logger.info(f"[Phase2][Config] Processing mode: {mode_tag}")
    except Exception as e:
        logger.error(f"[Phase2] Failed to initialize processor: {e}")
        return stats

    open_func = gzip.open if input.endswith(".gz") else open
    logger.info(f"[Phase2] Reading from {input}")

    CHECKPOINT_INTERVAL = 500
    processed_this_session = 0

    try:
        with open_func(input, "rt", encoding="utf-8") as f_in, \
             open(output, write_mode, encoding="utf-8") as f_out:

            pbar = tqdm(f_in, desc="[Phase2] Processing Commits", initial=skip_lines)

            for raw_line in pbar:
                line = raw_line.strip()
                if not line:
                    continue
                if skip_lines > 0:
                    skip_lines -= 1
                    continue

                stats["total_input"] += 1
                result_json, local_stats, error_info = process_single_line(line, processor)

                if error_info:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        logger.error(f"[Phase2] Loop Error: {error_info}")
                elif local_stats["is_success"]:
                    f_out.write(result_json + "\n")
                    _update_stats(stats, local_stats)
                else:
                    _update_stats(stats, local_stats)
                    stage = local_stats["stage"]
                    if stage == "slicing_error" and stats["filters"]["slicing_error"] <= 5:
                        logger.error(f"[Phase2] Slicing Error: {local_stats.get('error_msg')}")

                processed_this_session += 1
                total_processed = ckpt["processed_lines"] + processed_this_session

                if processed_this_session % CHECKPOINT_INTERVAL == 0:
                    f_out.flush()
                    save_checkpoint(output_dir, total_processed, stats,repo_name,use_graph)
                    pbar.set_postfix(
                        out=stats["total_output"],
                        err=stats["errors"],
                        ckpt=total_processed,
                    )

    except FileNotFoundError:
        logger.error(f"[Phase2] Input file not found: {input}")
        return stats

    except KeyboardInterrupt:
        total_processed = ckpt["processed_lines"] + processed_this_session
        save_checkpoint(output_dir, total_processed, stats,repo_name,use_graph)
        logger.warning(
            f"[Phase2] Interrupted! Progress saved at line {total_processed}."
        )
        _print_report(stats)
        return stats

    logger.success("[Phase2] Processing Complete.")
    clear_checkpoint(output_dir,repo_name,use_graph)
    _print_report(stats)

    stats_path = os.path.join(output_dir, "phase2_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    return stats


# ─────────────────────────────────────────────
# 单独运行入口
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="NEP Pipeline — Phase 2: Static Analysis (Resumable)")
    p.add_argument("--input",     required=True,  help="Phase 1 output (.jsonl or .jsonl.gz)")
    p.add_argument("--repo_path", required=True,  help="Local path to the git repository")
    p.add_argument("--repo_name", required=True, help="仓库短标识符，用于命名输出文件")
    p.add_argument("--output", default=None, help="Output JSONL file path (default: auto-named)")
    p.add_argument("--reset",     action="store_true", help="Ignore checkpoint and restart from scratch")
    p.add_argument(
        "--no-graph",
        action="store_true",
        default=False,
        help="禁用 GraphDependencyAnalyzerWrapper，跳过依赖分析，供后续 LLM 过滤使用。"
    )
    return p.parse_args()


def main():
    args = parse_args()
    run_phase2(
        input=args.input,
        output=args.output,
        repo_path=args.repo_path,
        repo_name=args.repo_name,
        reset=args.reset,
        use_graph=not args.no_graph,
    )

