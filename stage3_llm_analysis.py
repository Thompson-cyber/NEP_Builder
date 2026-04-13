import argparse
import json
import os
import time
from loguru import logger
from tqdm import tqdm

from core.types import AnalyzedCommit
from core.llm_ranker import LLMCausalRanker, CausalDatasetExporter
from config.settings import MiningConfig



def parse_args():
    p = argparse.ArgumentParser(description="NEP Pipeline — Phase 3: LLM Causal Ranking")
    p.add_argument("--input",     required=True,  help="Phase 2 output (analyzed.jsonl)")
    p.add_argument("--old_format_output", required=True, help="Old format dataset output path (.jsonl)")
    p.add_argument("--output",    required=True,  help="Final dataset output path (.jsonl)")
    p.add_argument("--error_log", default="output/llm_failures.jsonl", help="Path for failed items")
    return p.parse_args()


def main():
    args   = parse_args()

    # 初始化配置和 Ranker
    config = MiningConfig()
    if not config.USE_LLM:
        logger.warning("Config.USE_LLM is False! Forcing it to True for this script.")
        config.USE_LLM = True

    try:
        ranker = LLMCausalRanker(config)
        exporter = CausalDatasetExporter(output_file=args.output)

    except Exception as e:
        logger.critical(f"Failed to initialize LLM Ranker: {e}")
        return

    os.makedirs(os.path.dirname(args.old_format_output), exist_ok=True)

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
                open(args.old_format_output, 'w', encoding='utf-8') as f_out, \
                open(args.error_log, 'w', encoding='utf-8') as f_err:

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
