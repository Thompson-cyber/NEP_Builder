import argparse
import gzip
import json
import os
from typing import Dict, Any, Tuple, Optional

from loguru import logger
from tqdm import tqdm

from core.types import CommitCandidate
from analysis.processor import CommitProcessor



CHECKPOINT_FILENAME = "phase2_checkpoint.json"


def _checkpoint_path(output_dir: str) -> str:
    return os.path.join(output_dir, CHECKPOINT_FILENAME)


def load_checkpoint(output_dir: str) -> Dict[str, Any]:
    """加载断点文件，返回上次的进度信息。"""
    path = _checkpoint_path(output_dir)
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


def save_checkpoint(output_dir: str, processed_lines: int, stats: Dict[str, Any]) -> None:
    """将当前进度原子写入断点文件（先写临时文件再 rename，防止写坏）。"""
    path = _checkpoint_path(output_dir)
    tmp_path = path + ".tmp"
    payload = {"processed_lines": processed_lines, "stats": stats}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)  # 原子替换


def clear_checkpoint(output_dir: str) -> None:
    """处理完成后删除断点文件。"""
    path = _checkpoint_path(output_dir)
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
            local_stats["insights"]["cross_file"] = 1 if insights.get("cross_file_ratio", 0) > 0 else 0
            local_stats["insights"]["single_file"] = 0 if insights.get("cross_file_ratio", 0) > 0 else 1
            local_stats["insights"]["cycles_detected"] = 1 if insights.get("has_cycle", False) else 0
            local_stats["insights"]["has_tests"] = 1 if insights.get("has_tests", False) else 0
            local_stats["insights"]["total_hunks"] = len(result.ordered_hunks)
            local_stats["insights"]["total_dependencies"] = len(result.dependencies)
            return result.model_dump_json(), local_stats, None
        else:
            local_stats["error_msg"] = proc_stats.get("error")
            return None, local_stats, None

    except Exception as e:
        return None, local_stats, str(e)


# ─────────────────────────────────────────────
# 初始化全量 stats 结构（供首次运行 & 类型提示）
# ─────────────────────────────────────────────

def _empty_stats() -> Dict[str, Any]:
    return {
        "total_input": 0,
        "total_output": 0,
        "errors": 0,
        "filters": {
            "missing_source": 0, "slicing_error": 0, "slicing_empty": 0,
            "too_few_hunks": 0, "no_dependency": 0, "validation_fail": 0,
            "analysis_error": 0, "sorting_error": 0, "unknown": 0,
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
            "missing_source":           "missing_source",
            "slicing_error":            "slicing_error",
            "empty_hunks":              "slicing_empty",
            "too_few_source_hunks":     "too_few_hunks",
            "validation_no_dependencies": "no_dependency",
            "analysis_error":           "analysis_error",
            "sorting_error":            "sorting_error",
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

    report = f"""
==================================================
NEP Dataset Phase 2 Analysis Report (SEQUENTIAL)
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
5. No Dependency       : {stats['filters']['no_dependency']}
6. Validation Fail     : {stats['filters']['validation_fail']}
7. Analysis Error      : {stats['filters']['analysis_error']}
8. Sorting Error       : {stats['filters']['sorting_error']}
9. UNKNOWN REASON      : {stats['filters']['unknown']}

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
# 主流程
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NEP Dataset Builder - Phase 2: Analysis (Single Process, Resumable)"
    )
    parser.add_argument("--input",     type=str, required=True,
                        help="Path to Phase 1 output (candidates.jsonl or .gz)")
    parser.add_argument("--output",    type=str,
                        default="output/pandas/analyzed_commits.jsonl",
                        help="Output path")
    parser.add_argument("--repo_path", type=str, required=True,
                        help="Local path to git repository")
    parser.add_argument("--reset",     action="store_true",
                        help="Ignore existing checkpoint and start from scratch")
    args = parser.parse_args()

    output_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(output_dir, exist_ok=True)

    # ── 加载 / 重置 断点 ──────────────────────────────
    if args.reset:
        clear_checkpoint(output_dir)
        logger.info("[Resume] --reset flag detected, starting from scratch.")

    ckpt = load_checkpoint(output_dir)
    skip_lines: int       = ckpt["processed_lines"]   # 已处理行数，需跳过
    stats: Dict[str, Any] = ckpt["stats"] or _empty_stats()
    is_resume: bool       = skip_lines > 0

    # 续跑时用追加模式，否则用覆盖模式
    write_mode = "a" if is_resume else "w"
    if is_resume:
        logger.info(f"[Resume] Appending to existing output file (mode='{write_mode}').")

    # ── 初始化 Processor ─────────────────────────────
    logger.info("Initializing CommitProcessor...")
    try:
        processor = CommitProcessor(repo_path=args.repo_path)
    except Exception as e:
        logger.error(f"Failed to initialize processor: {e}")
        return

    open_func = gzip.open if args.input.endswith(".gz") else open

    logger.info(f"Reading from {args.input}")
    logger.info("Starting sequential processing...")

    # 每处理多少行保存一次 checkpoint（可按需调整）
    CHECKPOINT_INTERVAL = 500
    processed_this_session = 0   # 本次会话新处理的行数

    try:
        with open_func(args.input, "rt", encoding="utf-8") as f_in, \
             open(args.output, write_mode, encoding="utf-8") as f_out:

            pbar = tqdm(f_in, desc="Processing Commits", initial=skip_lines)

            for raw_line in pbar:
                line = raw_line.strip()
                if not line:
                    continue

                # ── 跳过已处理行 ──────────────────────
                if skip_lines > 0:
                    skip_lines -= 1
                    continue

                stats["total_input"] += 1

                # ── 处理 ──────────────────────────────
                result_json, local_stats, error_info = process_single_line(line, processor)

                if error_info:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        logger.error(f"Loop Error: {error_info}")
                elif local_stats["is_success"]:
                    logger.debug("Analyzing Success")
                    f_out.write(result_json + "\n")
                    _update_stats(stats, local_stats)
                else:
                    _update_stats(stats, local_stats)
                    stage = local_stats["stage"]
                    if stage == "slicing_error" and stats["filters"]["slicing_error"] <= 5:
                        logger.error(f"Slicing Error Detail: {local_stats.get('error_msg')}")

                processed_this_session += 1
                # ckpt 里记录的是「输入文件中已跳过+已处理」的总行数
                total_processed = ckpt["processed_lines"] + processed_this_session

                # ── 定期保存 checkpoint ───────────────
                if processed_this_session % CHECKPOINT_INTERVAL == 0:
                    f_out.flush()
                    save_checkpoint(output_dir, total_processed, stats)
                    pbar.set_postfix(
                        out=stats["total_output"],
                        err=stats["errors"],
                        ckpt=total_processed,
                    )

    except FileNotFoundError:
        logger.error(f"Input file not found: {args.input}")
        return

    except KeyboardInterrupt:
        # ── 中断：保存当前进度 ────────────────────────
        total_processed = ckpt["processed_lines"] + processed_this_session
        save_checkpoint(output_dir, total_processed, stats)
        logger.warning(
            f"\n[Resume] Interrupted! Progress saved at line {total_processed}. "
            f"Re-run the same command to continue."
        )
        _print_report(stats)
        return

    # ── 全部完成：保存最终 stats，清理 checkpoint ────
    logger.success("Phase 2 Sequential Processing Complete.")
    clear_checkpoint(output_dir)
    _print_report(stats)

    stats_path = os.path.join(output_dir, "phase2_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


if __name__ == "__main__":
    main()