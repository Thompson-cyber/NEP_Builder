#!/usr/bin/env python3
"""
批量对多个仓库运行 NEP Pipeline
用法：
    python batch_run.py                        # 全量跑
    python batch_run.py --no-graph             # 禁用图分析
    python batch_run.py --skip-phase1          # 跳过 Phase1
    python batch_run.py --reset                # Phase2 忽略断点
    python batch_run.py --lang Python          # 只跑某个语言
    python batch_run.py --repo-name yt-dlp    # 只跑某个仓库
"""

import argparse
import os
import sys
import traceback
from datetime import date
from pathlib import Path

from loguru import logger

from stage1_collect_commits import run_phase1
from stage2_call_graph_analysis import run_phase2

# ── 仓库列表 ──────────────────────────────────────────────────────────────────
REPOS_BASE = "/home/data/yibowang/multi_completion_datas/repos"
OUTPUT_BASE = "/home/data/yibowang/multi_completion_datas/data_collection/"

REPOS = {
    "Python": [
        "transformers",
        "django",
        "fastapi",
        "core",
        "sherlock",
        "yt-dlp",
        "ComfyUI",
        "pytorch",
    ],
    "Go": [
        "ollama",
        "kubernetes",
        "frp",
        "gin",
        "hugo",
        "syncthing",
        "fzf",
        "caddy",
        "moby",
        "traefik",
    ],
    "Java": [
        "spring-boot",
        "elasticsearch",
        "guava",
        "dbeaver",
        "RxJava",
        "jadx",
        "dubbo",
        "MPAndroidChart",
        "arthas",
        "selenium",
    ],
    "TypeScript": [
        "ant-design",
        "immich",
        "storybook",
        "mermaid",
        "nest",
        "strapi",
        "n8n",
        "ionic-framework",
        "Flowise",
        "DefinitelyTyped",
    ],
}


# ── 参数解析 ──────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="批量运行 NEP Pipeline")
    p.add_argument("--repos-base",  default=REPOS_BASE,  help="仓库根目录")
    p.add_argument("--output-base", default=OUTPUT_BASE, help="输出根目录")
    p.add_argument("--lang",        default=None,        help="只跑指定语言 (Python/Go/Java/TypeScript)")
    p.add_argument("--repo-name",   default=None,        help="只跑指定仓库名")
    p.add_argument("--no-graph",    action="store_true", help="禁用图分析")
    p.add_argument("--skip-phase1", action="store_true", help="跳过 Phase1")
    p.add_argument("--reset",       action="store_true", help="Phase2 忽略断点重跑")
    return p.parse_args()


# ── 单仓库运行 ────────────────────────────────────────────────────────────────
def run_one(repo_name: str, lang: str, args) -> dict:
    today = date.today().strftime('%Y-%m-%d')
    mode  = "no_graph" if args.no_graph else "graph"

    repo_path    = os.path.join(args.repos_base, lang, repo_name)
    output_dir   = os.path.join(args.output_base, lang, repo_name)
    candidates_path = os.path.join(output_dir, f"{repo_name}_{today}_phase1_candidates.jsonl")
    analyzed_path   = os.path.join(output_dir, f"{repo_name}_{today}_phase2_{mode}.jsonl")

    os.makedirs(output_dir, exist_ok=True)

    # ── 检查仓库目录是否存在 ──────────────────────────────
    if not os.path.isdir(repo_path):
        logger.error(f"[{repo_name}] 仓库目录不存在: {repo_path}，跳过")
        return {"repo": repo_name, "status": "skipped", "reason": "repo_not_found"}

    # ── Phase 1 ───────────────────────────────────────────
    if args.skip_phase1:
        if not os.path.exists(candidates_path):
            logger.error(f"[{repo_name}] --skip-phase1 但候选文件不存在: {candidates_path}")
            return {"repo": repo_name, "status": "failed", "reason": "candidates_not_found"}
        logger.info(f"[{repo_name}] 跳过 Phase1，使用已有: {candidates_path}")
    else:
        logger.info(f"[{repo_name}] ▶ Phase 1 开始")
        count = run_phase1(
            repo=repo_path,
            repo_name=repo_name,
            output=candidates_path,
        )
        if count == 0:
            logger.warning(f"[{repo_name}] Phase1 收集到 0 个候选，跳过 Phase2")
            return {"repo": repo_name, "status": "skipped", "reason": "zero_candidates"}
        logger.info(f"[{repo_name}] Phase1 完成，{count} 个候选 → {candidates_path}")

    # ── Phase 2 ───────────────────────────────────────────
    logger.info(f"[{repo_name}] ▶ Phase 2 开始 (mode={mode})")
    stats = run_phase2(
        input=candidates_path,
        output=analyzed_path,
        repo_path=repo_path,
        repo_name=repo_name,
        reset=args.reset,
        use_graph=not args.no_graph,
    )

    total = stats.get("total_output", "?")
    logger.success(f"[{repo_name}] ✅ 完成，输出 {total} 条 → {analyzed_path}")
    return {"repo": repo_name, "status": "success", "total_output": total, "output": analyzed_path}


# ── 主函数 ────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # 过滤任务列表
    tasks = [
        (repo_name, lang)
        for lang, names in REPOS.items()
        for repo_name in names
        if (args.lang      is None or lang      == args.lang)
        if (args.repo_name is None or repo_name == args.repo_name)
    ]

    if not tasks:
        logger.error("没有匹配的仓库，请检查 --lang / --repo-name 参数")
        sys.exit(1)

    total = len(tasks)
    logger.info(f"\n{'='*60}")
    logger.info(f"  共 {total} 个仓库待处理")
    logger.info(f"  repos_base  : {args.repos_base}")
    logger.info(f"  output_base : {args.output_base}")
    logger.info(f"  mode        : {'no_graph' if args.no_graph else 'graph'}")
    logger.info(f"{'='*60}\n")

    results = []
    for idx, (repo_name, lang) in enumerate(tasks, 1):
        logger.info(f"\n[{idx}/{total}] ── {lang} / {repo_name} {'─'*30}")
        try:
            result = run_one(repo_name, lang, args)
        except Exception as e:
            logger.error(f"[{repo_name}] 未捕获异常: {e}\n{traceback.format_exc()}")
            result = {"repo": repo_name, "status": "error", "reason": str(e)}
        results.append(result)

    # ── 最终汇总 ──────────────────────────────────────────
    success = [r for r in results if r["status"] == "success"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed  = [r for r in results if r["status"] in ("failed", "error")]

    logger.info(f"\n{'='*60}")
    logger.info(f"  汇总: ✅ 成功 {len(success)}  ⊘ 跳过 {len(skipped)}  ❌ 失败 {len(failed)}")
    logger.info(f"{'='*60}")

    if failed:
        logger.warning("失败列表:")
        for r in failed:
            logger.warning(f"  ❌ {r['repo']}: {r.get('reason', '?')}")

    if success:
        logger.info("成功列表:")
        for r in success:
            logger.info(f"  ✅ {r['repo']}: {r.get('total_output', '?')} 条")


if __name__ == "__main__":
    main()