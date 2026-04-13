from typing import Tuple, Optional, Dict
from loguru import logger

from core.types import CommitCandidate, AnalyzedCommit
from core.llm_ranker import LLMCausalRanker
from analysis.processor import CommitProcessor
from config.settings import MiningConfig


class MiningPipeline:
    def __init__(self, repo_path: str, use_llm: bool = False):
        self.config = MiningConfig()
        # 强制覆盖配置中的 LLM 开关
        self.use_llm = use_llm

        # 1. 初始化静态分析器
        self.processor = CommitProcessor(repo_path)

        # 2. 初始化 LLM Ranker (按需)
        self.ranker = None
        if self.use_llm:
            try:
                self.ranker = LLMCausalRanker(self.config)
                logger.info("LLM Ranker initialized.")
            except Exception as e:
                logger.warning(f"Failed to init LLM Ranker: {e}. LLM step will be skipped.")
                self.use_llm = False

    def run(self, candidate: CommitCandidate) -> Tuple[Optional[AnalyzedCommit], Dict]:
        # Step 1: 静态分析
        analyzed_commit, stats = self.processor.process(candidate)

        # 如果静态分析失败，直接返回
        if not analyzed_commit:
            return None, stats

        # Step 2: LLM 排序
        if self.use_llm and self.ranker and stats.get("stage") == "static_analysis_success":
            try:
                # logger.info(f"Running LLM ranking for {candidate.hash}...")
                analyzed_commit = self.ranker.rank_commit(analyzed_commit)
                stats["stage"] = "llm_ranking_success"
            except Exception as e:
                logger.error(f"LLM ranking failed for {candidate.hash}: {e}")
                # 降级策略：保留静态分析结果，但标记错误
                stats["llm_error"] = str(e)
                stats["stage"] = "static_success_llm_failed"

        return analyzed_commit, stats
