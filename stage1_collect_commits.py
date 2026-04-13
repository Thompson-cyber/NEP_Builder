import argparse
import gzip
import os

from loguru import logger

from filters.benchmark_filters import BenchmarkFilter
from mining.miner import RepoMiner


def parse_args():
    p = argparse.ArgumentParser(description="NEP Pipeline — Phase 1: Commit Mining")
    p.add_argument("--repo",      required=True,  help="Local path or remote URL of the git repository")
    p.add_argument("--repo_name", required=True,  help="Short identifier for the repository (e.g. 'pandas')")
    p.add_argument("--output", default=None, help="Output JSONL file path (default: auto-named)")
    return p.parse_args()

def run_phase1(
    repo: str,
    repo_name: str,
    output: str,
) -> int:
    """
    执行 Phase 1 挖掘。
    Returns:
        采集到的候选提交数量
    """
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)

    miner = RepoMiner(repo_name, repo, [BenchmarkFilter()])
    logger.info(f"[Phase1] Starting Benchmark Mining on {repo}")
    logger.info("[Phase1] Strategy: Merge Commits | Source (Data) + Test (Verifier)")

    count = 0
    try:
        with open(output, 'w', encoding='utf-8') as f:
            for candidate in miner.mine():
                f.write(candidate.model_dump_json() + "\n")
                count += 1
                logger.info(
                    f"[Phase1][{count}] {candidate.hash[:7]}: "
                    f"Source={candidate.source_files_count}, "
                    f"Test={candidate.test_files_count}"
                )
    except KeyboardInterrupt:
        logger.warning("[Phase1] Interrupted.")

    logger.success(f"[Phase1] Done. Collected {count} commits.")
    return count


def main():
    args = parse_args()
    from datetime import date
    output = args.output or f"output/{args.repo_name}_{date.today().strftime('%Y-%m-%d')}_phase1_candidates.jsonl"
    run_phase1(
        repo=args.repo,
        repo_name=args.repo_name,
        output=output,
    )
