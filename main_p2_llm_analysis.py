import argparse
import json
import os
import time
from loguru import logger
from tqdm import tqdm

from core.types import AnalyzedCommit
from core.llm_ranker import LLMCausalRanker, CausalDatasetExporter
from config.settings import MiningConfig



def main():
    parser = argparse.ArgumentParser(description="Phase 2b: LLM Causal Ranking")
    parser.add_argument("--input", type=str, default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\extracted_by_hash.jsonl",
                        help="Path to Static Analysis output (intermediate/static_analyzed.jsonl)")
    parser.add_argument("--output", type=str, default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\extracted_by_hash_selected_test_cases.jsonl", help="Final output path")
    parser.add_argument("--error_log", type=str, default="output/sklearn/llm_failures.jsonl",
                        help="Path to save failed items")
    exporter = CausalDatasetExporter(output_file=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\test_json.jsonl")
    args = parser.parse_args()

    # 初始化配置和 Ranker
    config = MiningConfig()
    if not config.USE_LLM:
        logger.warning("Config.USE_LLM is False! Forcing it to True for this script.")
        config.USE_LLM = True

    try:
        ranker = LLMCausalRanker(config)
    except Exception as e:
        logger.critical(f"Failed to initialize LLM Ranker: {e}")
        return

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    stats = {
        "total_input": 0,
        "success": 0,
        "failed": 0,
        "tokens_used": 0,  # 如果你的 Ranker 返回 token 使用情况的话
        "time_elapsed": 0
    }

    start_time = time.time()
    logger.info(f"Starting LLM Ranking. Reading from {args.input}")

    try:
        with open(args.input, 'r', encoding='utf-8') as f_in, \
                open(args.output, 'w', encoding='utf-8') as f_out, \
                open(args.error_log, 'w', encoding='utf-8') as f_err:

            # 使用 tqdm 显示进度
            for line in tqdm(f_in, desc="LLM Ranking"):
                if not line.strip(): continue
                stats["total_input"] += 1

                try:
                    data = json.loads(line)
                    commit = AnalyzedCommit(**data)

                    # 检查是否已经包含 ranking
                    if commit.causal_analysis:
                        f_out.write(line)
                        stats["success"] += 1
                        continue

                    # === 核心调用 ===
                    # rank_commit 会修改 commit 对象的 ordered_hunks 顺序并填充 causal_analysis
                    ranked_commit = ranker.rank_commit(commit)
                    # ranked_commit = commit
                    exporter.save_commit(ranked_commit)

                    logger.info(f"Saved analysis for {commit.hash}")
                    # 写入成功结果
                    f_out.write(ranked_commit.model_dump_json() + "\n")
                    stats["success"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"LLM Failed for commit {data.get('hash', 'unknown')}: {e}")

                    # 将失败的原始数据写入错误日志，方便后续重试
                    error_entry = {
                        "original_data": data,
                        "error": str(e)
                    }
                    f_err.write(json.dumps(error_entry) + "\n")

                    # 可选：遇到 API 速率限制时 sleep
                    if "RateLimit" in str(e) or "429" in str(e):
                        time.sleep(5)

    except FileNotFoundError:
        logger.error(f"Input file not found: {args.input}")
        return
    except KeyboardInterrupt:
        logger.warning("Process interrupted by user. Saving stats...")

    stats["time_elapsed"] = time.time() - start_time

    report = f"""
        ==================================================
        LLM Ranking Report
        ==================================================
        Total Input      : {stats['total_input']}
        Success          : {stats['success']}
        Failed           : {stats['failed']}
        Time Elapsed     : {stats['time_elapsed']:.1f}s
        Avg Time/Item    : {stats['time_elapsed'] / stats['success'] if stats['success'] else 0:.2f}s
        ==================================================
        Failed items saved to: {args.error_log}
    """
    logger.success(report)


if __name__ == "__main__":
    main()
