import argparse
import json
import os
import time
from datetime import date

from loguru import logger
from tqdm import tqdm

from core.types import AnalyzedCommit
from core.llm_ranker import LLMCausalRanker, CausalDatasetExporter
from config.settings import MiningConfig



def parse_args():
    p = argparse.ArgumentParser(description="NEP Pipeline — Phase 3: LLM Causal Ranking")
    p.add_argument("--input",     required=True,  help="Phase 2 output (analyzed.jsonl)")
    p.add_argument("--output",    required=True,  help="Final dataset output path")
    p.add_argument("--repo-name", required=True, help="repo-name")

    return p.parse_args()


def main():
    args   = parse_args()
    today = date.today().strftime('%Y-%m-%d')
    filter_out_path = os.path.join(args.output, f"{args.repo_name}_{today}_phase3_filtered.jsonl")
    error_log_path = os.path.join(args.output, f"{args.repo_name}_{today}_phase3_errors.log")
    output_path = os.path.join(args.output, f"{args.repo_name}_{today}_phase3_results.jsonl")

    # 初始化配置和 Ranker
    config = MiningConfig()
    if not config.USE_LLM:
        logger.warning("Config.USE_LLM is False! Forcing it to True for this script.")
        config.USE_LLM = True

    try:
        ranker = LLMCausalRanker(config)
        exporter = CausalDatasetExporter(output_file=output_path)

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
    analyzed_content = ''
    if os.path.exists(filter_out_path):
        with open(filter_out_path, "r",encoding='utf-8') as f:
            analyzed_content = f.read()
    if os.path.exists(error_log_path):
        with open(error_log_path, "r",encoding='utf-8') as f:
            analyzed_content += f.read()
    start_time = time.time()
    logger.info(f"Starting LLM Ranking. Reading from {args.input}")
    try:
        with open(args.input, 'r', encoding='utf-8') as f_in, \
                open(filter_out_path, 'w', encoding='utf-8') as f_out, \
                open(error_log_path, 'w', encoding='utf-8') as f_err:

            for line in tqdm(f_in, desc="LLM Ranking"):
                if not line.strip(): continue
                stats["total_input"] += 1

                try:
                    data = json.loads(line)
                    commit = AnalyzedCommit(**data)
                    if commit.hash in analyzed_content:
                        stats["success"] += 1
                        continue
                    # 检查是否已经包含 ranking
                    if commit.causal_analysis:
                        f_out.write(line)
                        stats["success"] += 1
                        continue

                    # === 核心调用 ===
                    # rank_commit 会修改 commit 对象的 ordered_hunks 顺序并填充 causal_analysis
                    ranked_commit = ranker.rank_commit(commit)
                    if ranked_commit is None:
                        continue
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
        Failed items saved to: {error_log_path}
    """
    logger.success(report)


if __name__ == "__main__":
    main()
