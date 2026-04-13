import argparse
import os
from loguru import logger

from stage1_collect_commits import run_phase1
from stage2_call_graph_analysis import run_phase2

'''
# ① 完整跑两个阶段
python run_pipeline.py \
    --repo /path/to/repo \
    --repo_name pandas 

# ② 完整跑，禁用图分析
python run_pipeline.py \
    --repo /path/to/repo \
    --repo_name pandas \
    --no-graph

# ③ Phase1 已跑过，只重跑 Phase2
python run_pipeline.py \
    --repo /path/to/repo \
    --repo_name pandas \
    --skip-phase1

# ④ Phase2 从头重跑（忽略断点）
python run_pipeline.py \
    --repo /path/to/repo \
    --repo_name pandas \
    --skip-phase1 --reset

# ⑤ 单独运行 Phase1（不变）
python run_phase1.py --repo /path/to/repo --repo_name pandas

# ⑥ 单独运行 Phase2（不变）
python run_phase2.py --input output/candidates.jsonl --repo_path /path/to/repo

'''
def parse_args():
    p = argparse.ArgumentParser(
        description="NEP Pipeline — Full Run (Phase 1 + Phase 2)"
    )
    # ── 通用参数 ──────────────────────────────────────
    p.add_argument("--repo",      required=True,  help="本地路径或远程 URL")
    p.add_argument("--repo_name", required=True,  help="仓库短标识符（如 'pandas'）")
    p.add_argument("--output_dir",default="output",help="所有输出文件的根目录")


    # ── Phase 2 参数 ──────────────────────────────────
    p.add_argument("--reset",     action="store_true", help="忽略 Phase2 断点，从头重跑")
    p.add_argument("--no-graph",  action="store_true", help="禁用图分析，走 LLM-filter 模式")

    # ── 跳过控制 ──────────────────────────────────────
    p.add_argument("--skip-phase1", action="store_true",
                   help="跳过 Phase1（使用已有的候选文件直接跑 Phase2）")
    return p.parse_args()


def main():

    args = parse_args()
    from datetime import date
    today = date.today().strftime('%Y-%m-%d')
    mode = "no_graph" if args.no_graph else "graph"

    candidates_path = os.path.join(args.output_dir, f"{args.repo_name}_{today}_phase1_candidates.jsonl")
    analyzed_path = os.path.join(args.output_dir, f"{args.repo_name}_{today}_phase2_{mode}.jsonl")

    os.makedirs(args.output_dir, exist_ok=True)

    # ══════════════════════════════════════════════════
    # Phase 1
    # ══════════════════════════════════════════════════
    if args.skip_phase1:
        if not os.path.exists(candidates_path):
            logger.error(
                f"[Pipeline] --skip-phase1 specified but candidates file not found: "
                f"{candidates_path}"
            )
            return
        logger.info(f"[Pipeline] Skipping Phase 1, using existing: {candidates_path}")
    else:
        logger.info("=" * 50)
        logger.info("[Pipeline] ▶ Starting Phase 1: Commit Mining")
        logger.info("=" * 50)

        count = run_phase1(
            repo=args.repo,
            repo_name=args.repo_name,
            output=candidates_path,
        )

        if count == 0:
            logger.error("[Pipeline] Phase 1 collected 0 candidates. Aborting.")
            return

        logger.info(f"[Pipeline] Phase 1 complete. {count} candidates → {candidates_path}")

    # ══════════════════════════════════════════════════
    # Phase 2
    # ══════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("[Pipeline] ▶ Starting Phase 2: Static Analysis")
    logger.info("=" * 50)

    stats = run_phase2(
        input=candidates_path,
        output=analyzed_path,
        repo_path=args.repo,
        repo_name=args.repo_name,
        reset=args.reset,
        use_graph=not args.no_graph,
    )

    # ── 最终汇总 ──────────────────────────────────────
    logger.info("=" * 50)
    logger.success(
        f"[Pipeline] ✅ All Done!\n"
        f"  Candidates : {candidates_path}\n"
        f"  Analyzed   : {analyzed_path}\n"
        f"  Output     : {stats.get('total_output', '?')} records"
    )
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
