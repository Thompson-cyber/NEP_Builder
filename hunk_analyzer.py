import os
import re
import json
import stat
import shutil
import pickle
import logging
import subprocess
import networkx as nx
from typing import List, Tuple, Dict, Set

# --- 导入你原有的模块 ---
from analysis.locagent.dependency_graph.build_graph import (
    build_graph,
    NODE_TYPE_FILE, NODE_TYPE_CLASS, NODE_TYPE_FUNCTION,
    EDGE_TYPE_CONTAINS
)
from analysis.locagent.dependency_graph.traverse_graph import (
    RepoEntitySearcher, RepoDependencySearcher
)

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def remove_readonly(func, path, excinfo):
    """处理 Windows 下 shutil.rmtree 无法删除只读文件的问题"""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def prepare_sandbox_repo(source_repo: str, sandbox_root: str, commit_hash: str) -> str:
    """克隆并 checkout 到指定 commit 的父节点 (Base Commit)"""
    target_repo_path = os.path.abspath(os.path.join(sandbox_root, commit_hash))

    if os.path.exists(target_repo_path):
        logger.info(f"🧹 Cleaning up existing sandbox: {target_repo_path}")
        shutil.rmtree(target_repo_path, onerror=remove_readonly)

    os.makedirs(target_repo_path, exist_ok=True)

    try:
        logger.info(f"📋 Cloning from {source_repo} to {target_repo_path}...")
        subprocess.run(
            ["git", "clone", source_repo, target_repo_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )

        checkout_target = f"{commit_hash}^"
        logger.info(f"🔙 Checking out to {checkout_target} (Pre-fix state)...")

        subprocess.run(
            ["git", "checkout", "-f", checkout_target],
            cwd=target_repo_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        return target_repo_path

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git operation failed: {e.stderr.decode().strip()}")
        if os.path.exists(target_repo_path):
            shutil.rmtree(target_repo_path, onerror=remove_readonly)
        raise e


class HunkDependencyAnalyzer:
    def __init__(self, repo_path: str, commit_hash: str, cache_dir: str = "./graph_cache"):
        self.repo_path = os.path.abspath(repo_path)
        if not os.path.exists(self.repo_path):
            raise FileNotFoundError(f"Repository not found at: {self.repo_path}")

        # --- 初始化图数据库 (带缓存) ---
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        # 缓存的 key 现在使用 commit_hash，确保每个 commit 的图是独立的
        cache_file = os.path.join(cache_dir, f"graph_{commit_hash}.pkl")

        if os.path.exists(cache_file):
            logger.info(f"📂 Loading cached graph for commit {commit_hash}...")
            with open(cache_file, 'rb') as f:
                self.graph = pickle.load(f)
        else:
            logger.info(f"🔄 Building graph from scratch for commit {commit_hash}...")
            self.graph = build_graph(self.repo_path, fuzzy_search=True, global_import=True)
            with open(cache_file, 'wb') as f:
                pickle.dump(self.graph, f)

        self.entity_searcher = RepoEntitySearcher(self.graph)
        self.dep_searcher = RepoDependencySearcher(self.graph)

    def _normalize_path(self, file_path: str) -> str:
        """将 Windows 路径分隔符转换为 POSIX 格式"""
        return file_path.replace('\\', '/')

    def extract_old_hunks_from_diff(self, diff_text: str, old_file_path: str) -> List[Tuple[str, int, int]]:
        """
        [关键修改] 从 Diff 中提取修改前 (Base Commit) 的代码块行号。
        返回: [(old_file_path, old_start_line, old_end_line), ...]
        """
        hunks = []
        # 匹配 diff 中的 hunk 头部，例如: @@ -17,7 +17,7 @@
        # 我们现在关心的是修改前的起始行 (-17) 和行数 (7)
        pattern = re.compile(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")

        for line in diff_text.split('\n'):
            match = pattern.match(line)
            if match:
                old_start = int(match.group(1))
                old_count_str = match.group(2)

                if old_count_str is not None:
                    old_count = int(old_count_str)
                else:
                    old_count = 1  # 如果没有逗号，说明只涉及1行

                # 特殊情况：如果是纯新增代码，old_count 为 0。
                # 此时我们在 Base Commit 中找不到这行代码，只能用 old_start 作为上下文锚点。
                old_end = old_start + max(1, old_count) - 1

                hunks.append((self._normalize_path(old_file_path), old_start, old_end))

        return hunks

    #TODO: 这里的提取能力需要优化
    def get_nodes_by_hunk(self, file_path: str, start_line: int, end_line: int) -> Set[str]:
        """根据文件路径和行号范围，找到图中所有涉及的最具体节点集合"""
        hunk_nodes = set()
        file_nodes = []

        for nid in self.graph.nodes():
            if str(nid).startswith(file_path):
                node_data = self.graph.nodes[nid]
                n_start = node_data.get('start_line')
                n_end = node_data.get('end_line')
                n_type = node_data.get('type')
                file_nodes.append((nid, n_start, n_end, n_type))

        for line_number in range(start_line, end_line + 1):
            candidate_nodes = []
            for nid, n_start, n_end, n_type in file_nodes:
                if n_type == NODE_TYPE_FILE:
                    candidate_nodes.append((nid, float('inf')))
                    continue
                if n_start is not None and n_end is not None:
                    if n_start <= line_number <= n_end:
                        span = n_end - n_start
                        candidate_nodes.append((nid, span))

            if candidate_nodes:
                candidate_nodes.sort(key=lambda x: x[1])
                hunk_nodes.add(candidate_nodes[0][0])

        return hunk_nodes

    def analyze_hunk_dependencies(self, hunks: List[Tuple[str, int, int]]) -> List[Dict]:
        """分析多个 hunk 之间的依赖关系"""
        hunk_to_nodes = {}
        for hunk in hunks:
            file_path, start_line, end_line = hunk
            file_path = file_path.replace('/','\\')
            nodes = self.get_nodes_by_hunk(file_path, start_line, end_line)
            hunk_to_nodes[hunk] = nodes

        results = []
        hunk_list = list(hunk_to_nodes.keys())

        for i in range(len(hunk_list)):
            for j in range(len(hunk_list)):
                if i == j:
                    continue

                src_hunk, tgt_hunk = hunk_list[i], hunk_list[j]
                src_nodes, tgt_nodes = hunk_to_nodes[src_hunk], hunk_to_nodes[tgt_hunk]

                if not src_nodes or not tgt_nodes:
                    continue

                intersection = src_nodes.intersection(tgt_nodes)
                if intersection:
                    shared_node = list(intersection)[0]
                    results.append({
                        'source_hunk': src_hunk, 'target_hunk': tgt_hunk,
                        'source_node': shared_node, 'target_node': shared_node,
                        'hops': 0, 'path': [shared_node]
                    })
                    continue

                min_hops = float('inf')
                best_path = None
                best_src, best_tgt = None, None

                for s_node in src_nodes:
                    for t_node in tgt_nodes:
                        try:
                            path = nx.shortest_path(self.graph, source=s_node, target=t_node)
                            hops = len(path) - 1
                            if hops < min_hops:
                                min_hops = hops
                                best_path = path
                                best_src, best_tgt = s_node, t_node
                        except nx.NetworkXNoPath:
                            continue

                if best_path is not None:
                    results.append({
                        'source_hunk': src_hunk, 'target_hunk': tgt_hunk,
                        'source_node': best_src, 'target_node': best_tgt,
                        'hops': min_hops, 'path': best_path
                    })

        return results


def process_commit_record(record: dict, sandbox_root: str = "./sandboxes") -> str:
    """
    处理单行 JSONL 记录的完整流水线：
    1. Clone & Checkout 到 Base Commit
    2. 提取 Old Hunks
    3. 构建图并分析依赖
    """
    source_repo = record.get("repo_url", "")
    commit_hash = record.get("hash", "unknown")
    msg = record.get("msg", "").strip().split('\n')[0]

    logger.info(f"\n{'=' * 50}\n🚀 Starting Analysis for Commit: {commit_hash[:8]} - {msg}\n{'=' * 50}")

    # 1. 准备沙盒环境 (Checkout 到 commit_hash^)
    sandbox_path = prepare_sandbox_repo(source_repo, sandbox_root, commit_hash)

    # 2. 初始化分析器 (基于沙盒路径和 commit_hash)
    analyzer = HunkDependencyAnalyzer(repo_path=sandbox_path, commit_hash=commit_hash)

    # 3. 提取所有的 Hunks (使用 old_path 和旧行号)
    all_hunks = []
    for change in record.get("source_changes", []):
        # 如果是新增文件，old_path 可能是 /dev/null 或 None，在 Base Commit 中不存在，跳过
        old_path = change.get("old_path")
        if not old_path or old_path == "/dev/null":
            continue

        if change.get("change_type") in ["MODIFY", "DELETE"] and "diff" in change:
            hunks = analyzer.extract_old_hunks_from_diff(change["diff"], old_path)
            all_hunks.extend(hunks)

    if not all_hunks:
        return "❌ No valid pre-existing hunks found in this commit to analyze."

    # 4. 计算 Hunk 之间的依赖
    dependencies = analyzer.analyze_hunk_dependencies(all_hunks)

    # 5. 格式化输出报告
    report = []
    report.append(f"### 🎯 Base Commit Dependency Report")
    report.append(f"- **Target Commit**: `{commit_hash}`")
    report.append(f"- **Analyzed State**: `{commit_hash}^` (Pre-fix)")
    report.append(f"\n#### 📦 Extracted Hunks in Base Commit ({len(all_hunks)} total):")
    for h in all_hunks:
        report.append(f"  - `{h[0]}` (Lines {h[1]} to {h[2]})")

    report.append(f"\n#### 🔗 Hunk Dependencies:")
    if not dependencies:
        report.append("  *(No direct code dependencies found between these hunks in the base commit)*")
    else:
        for dep in dependencies:
            src = f"{dep['source_hunk'][0]}:{dep['source_hunk'][1]}-{dep['source_hunk'][2]}"
            tgt = f"{dep['target_hunk'][0]}:{dep['target_hunk'][1]}-{dep['target_hunk'][2]}"
            report.append(f"- **[{src}]** ➡️ depends on ➡️ **[{tgt}]**")
            report.append(f"  - **Hops**: {dep['hops']}")
            report.append(f"  - **Trigger Nodes**: `{dep['source_node']}` -> `{dep['target_node']}`")
            report.append(f"  - **Path**: {' -> '.join(dep['path'])}")

    return "\n".join(report)


# ==========================================
# 使用示例
# ==========================================
if __name__ == "__main__":
    # 假设这是从 JSONL 读取的一行
    json_line = """
    {
      "repo_name": "scikit-learn/scikit-learn",
      "hash": "3258ef8e7ec3c81c33ad7c1262a6e2d3acccfa9f",
      "msg": "FIX: handling pandas missing values in HTML repr",
      "repo_url": "D:\\\\Data\\\\2025\\\\CodeCompletion\\\\Dataset\\\\Repos\\\\scikit-learn",
      "source_changes": [
        {
          "old_path": "sklearn\\\\base.py",
          "new_path": "sklearn\\\\base.py",
          "change_type": "MODIFY",
          "diff": "@@ -17,7 +17,7 @@ from sklearn import __version__\\n from sklearn._config import config_context, get_config\\n from sklearn.exceptions import InconsistentVersionWarning\\n from sklearn.utils._metadata_requests import _MetadataRequester, _routing_enabled\\n-from sklearn.utils._missing import is_scalar_nan\\n+from sklearn.utils._missing import is_pandas_na, is_scalar_nan\\n from sklearn.utils._param_validation import validate_parameter_constraints\\n from sklearn.utils._repr_html.base import ReprHTMLMixin, _HTMLDocumentationLinkMixin\\n from sklearn.utils._repr_html.estimator import estimator_html_repr\\n@@ -304,6 +304,10 @@ class BaseEstimator(ReprHTMLMixin, _HTMLDocumentationLinkMixin, _MetadataRequest\\n                 init_default_params[param_name]\\n             ):\\n                 return True\\n+            if is_pandas_na(param_value) and not is_pandas_na(\\n+                init_default_params[param_name]\\n+            ):\\n+                return True\\n             if not np.array_equal(\\n                 param_value, init_default_params[param_name]\\n             ) and not (\\n"
        }
      ]
    }
    """
    record = json.loads(json_line)

    # 执行分析 (沙盒会生成在 ./sandboxes 目录下)
    report = process_commit_record(record, sandbox_root="./sandboxes")
    print("\n" + report)
