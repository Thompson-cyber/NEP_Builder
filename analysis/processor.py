import subprocess
from typing import List, Optional, Tuple, Dict, Set
from loguru import logger

from core.llm_ranker import LLMCausalRanker
from core.types import CommitCandidate, AnalyzedCommit, Hunk
from config.settings import MiningConfig
from .slicer import DiffSlicer
from .graph import DependencyGraph

from .graph_analyzer import GraphDependencyAnalyzerWrapper


class CoChangeValidator:
    def __init__(self):
        self.config = MiningConfig()

    def validate(self, source_hunks: List[Hunk], new_metrics: Dict, old_metrics: Dict) -> Tuple[bool, str]:
        if len(source_hunks) < self.config.MIN_SOURCE_HUNKS: return False, "too_few_source_hunks"
        if len(source_hunks) > self.config.MAX_SOURCE_HUNKS: return False, "too_more_source_hunks"

        if self.config.REQUIRE_DEPENDENCY:
            has_new_edges = new_metrics.get("n_edges", 0) > 0
            has_old_edges = old_metrics.get("n_edges", 0) > 0
            if not has_new_edges and not has_old_edges:
                return False, "no_dependencies_in_either"

        if self.config.NO_ISOLATED_HUNKS:
            if not new_metrics.get("all_hunks_connected", True) and not old_metrics.get("all_hunks_connected", True):
                return False, "has_isolated_hunks"
        return True, "ok"


class CommitProcessor:
    def __init__(self, repo_path: str):
        self.slicer = DiffSlicer()
        self.graph_sorter = DependencyGraph()
        self.validator = CoChangeValidator()
        self.config = MiningConfig()
        self.llm_ranker = LLMCausalRanker(self.config)
        self.repo_path = repo_path

    def _get_parent_hash(self, commit_hash: str) -> Optional[str]:
        """辅助函数：获取父提交 Hash"""
        try:
            cmd = ["git", "-C", self.repo_path, "rev-parse", f"{commit_hash}^"]
            result = subprocess.check_output(cmd, text=True).strip()
            return result
        except subprocess.CalledProcessError:
            logger.warning(f"Could not find parent commit for {commit_hash}")
            return None

    def _determine_label(self, old_deps, new_deps) -> str:
        if old_deps and new_deps:
            return "BOTH"
        elif new_deps:
            return "NEW_ONLY"
        elif old_deps:
            return "OLD_ONLY"
        else:
            return "NONE"

    def process(self, candidate: CommitCandidate) -> Tuple[Optional[AnalyzedCommit], Dict]:
        stats = {
            "stage": "init", "error": None, "n_source_hunks": 0, "n_test_hunks": 0,
            "n_deps": 0, "cross_file": 0, "debug_diff": None, "n_old_deps": 0,
            "dep_label": "NONE", "insights": dict()
        }

        # === 1. 数据提取 ===
        combined_diff_lines = []
        for fc in candidate.source_changes + candidate.test_changes:
            if not fc.diff: continue
            diff_text = fc.diff.strip()
            path = fc.new_path or fc.old_path or "unknown_test"
            if not diff_text.startswith("diff --git"):
                diff_text = f"diff --git a/{path} b/{path}\n" + diff_text
            combined_diff_lines.append(diff_text)

        raw_diff = "\n".join(combined_diff_lines)
        if not raw_diff:
            stats["stage"] = "missing_diff"
            return None, stats

        # === 2. Slicing ===
        try:
            all_hunks = self.slicer.slice(raw_diff)
        except Exception as e:
            stats["stage"] = "slicing_error";
            stats["error"] = str(e)
            return None, stats

        if not all_hunks:
            stats["stage"] = "empty_hunks";
            return None, stats

        # === 3. 分类 Hunks ===
        source_paths = set(fc.new_path for fc in candidate.source_changes if fc.new_path)
        source_paths.update(fc.old_path for fc in candidate.source_changes if fc.old_path)

        source_hunks = [h for h in all_hunks if h.file_path in source_paths]
        test_hunks = [h for h in all_hunks if h.file_path not in source_paths]

        stats["n_source_hunks"] = len(source_hunks)
        stats["n_test_hunks"] = len(test_hunks)

        if len(source_hunks) < self.config.MIN_SOURCE_HUNKS:
            stats["stage"] = "too_few_source_hunks"
            return None, stats

        # === 4.1 New State Analysis ===
        new_edges, new_metrics, new_chains, valid_hunks_new = [], {}, [], []
        try:
            # 传入当前 commit hash
            new_analyzer = GraphDependencyAnalyzerWrapper(self.repo_path, candidate.hash, mode='new')
            new_edges, new_metrics, new_chains, valid_hunks_new = new_analyzer.analyze(source_hunks, mode='new')
        except Exception as e:
            logger.warning(f"New Graph Analysis failed: {e}")
            stats["new_analysis_error"] = str(e)
            return None, stats

        # === 4.3 Old State Analysis ===
        old_edges, old_metrics, old_chains = [], {}, []
        parent_hash = self._get_parent_hash(candidate.hash)
        if parent_hash:
            try:
                # 【关键修改】传入解析出的真实 parent_hash
                old_analyzer = GraphDependencyAnalyzerWrapper(self.repo_path, parent_hash, mode='old')
                old_edges, old_metrics, old_chains, _ = old_analyzer.analyze(source_hunks, mode='old')
            except Exception as e:
                logger.warning(f"Old Graph Analysis failed: {e}")
                stats["old_analysis_warning"] = str(e)

        # === 5. 生成标签与验证 ===
        is_valid, reason = self.validator.validate(valid_hunks_new, new_metrics, old_metrics)
        if not is_valid:
            stats["stage"] = f"validation_{reason}"
            return None, stats

        dep_label = self._determine_label(old_metrics.get("all_hunks_connected"),
                                          new_metrics.get("all_hunks_connected"))
        stats["dep_label"] = dep_label

        # === 6. Graph Sorting ===
        try:
            hunks_to_sort = valid_hunks_new if valid_hunks_new else source_hunks
            sorted_hunks, edges_debug, has_cycle = self.graph_sorter.sort(hunks_to_sort, new_edges)
        except Exception as e:
            stats["stage"] = "sorting_error";
            stats["error"] = str(e)
            return None, stats

        # === 7. Construct Result ===
        analyzed_commit = AnalyzedCommit(
            hash=candidate.hash, repo=candidate.repo_url, msg=candidate.msg,
            ordered_hunks=sorted_hunks, test_hunks=test_hunks,
            dependencies=edges_debug, dependency_chains=new_chains,
            old_dependencies=old_edges, old_dependency_chains=old_chains,
            old_metrics=old_metrics, new_metrics=new_metrics, dependency_label=dep_label
        )

        stats["stage"] = "success"
        stats["insights"] = {
            "cross_file_ratio": new_metrics.get("cross_file_dependencies", 0) / len(new_edges) if new_edges else 0,
            "has_cycle": has_cycle,
            "label": dep_label
        }

        return analyzed_commit, stats
