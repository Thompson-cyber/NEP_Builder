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
    p.add_argument("--output",    default="output/candidates.jsonl", help="Output JSONL file path")
    p.add_argument("--limit",     type=int, default=100,             help="Max number of candidates to collect")
    return p.parse_args()

def main():
    args = parse_args()
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    miner = RepoMiner(args.repo_name,args.repo, [BenchmarkFilter()])

    logger.info(f"Starting Benchmark Mining on {args.repo}")
    logger.info("Strategy: Merge Commits | Source (Data) + Test (Verifier)")

    count = 0
    try:
        with open(args.output, 'w', encoding='utf-8') as f:
            for candidate in miner.mine(limit=args.limit):
                f.write(candidate.model_dump_json() + "\n")
                count += 1
                logger.info(
                    f"[{count}] {candidate.hash[:7]}: Source={candidate.source_files_count}, Test={candidate.test_files_count}")
    except KeyboardInterrupt:
        logger.warning("Interrupted.")

    logger.success(f"Done. Collected {count} commits.")


if __name__ == "__main__":
    main()