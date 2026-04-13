import os
import ast
import stat
import shutil
import pickle
import tokenize
import io
from loguru import logger
import subprocess
import networkx as nx
from typing import List, Tuple, Dict, Set, Optional

from analysis.locagent.dependency_graph.build_graph import (
    build_graph, NODE_TYPE_FILE
)
from analysis.locagent.dependency_graph.traverse_graph import (
    RepoEntitySearcher, RepoDependencySearcher
)
from core.types import Hunk


def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def cleanup_sandbox(sandbox_path: str) -> None:
    if os.path.exists(sandbox_path):
        try:
            shutil.rmtree(sandbox_path, onerror=remove_readonly)
            logger.info(f"🗑️  Sandbox cleaned up: {sandbox_path}")
        except Exception as e:
            logger.warning(f"⚠️  Failed to clean up sandbox {sandbox_path}: {e}")


def prepare_sandbox_repo(source_repo: str, sandbox_root: str, sandbox_name: str, checkout_target: str) -> str:
    target_repo_path = os.path.abspath(os.path.join(sandbox_root, sandbox_name))
    if os.path.exists(target_repo_path):
        return target_repo_path
    os.makedirs(target_repo_path, exist_ok=True)
    try:
        logger.info(f"📋 Cloning from {source_repo} to {target_repo_path}...")
        subprocess.run(["git", "clone", source_repo, target_repo_path], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        logger.info(f"🔙 Checking out to {checkout_target} ...")
        subprocess.run(["git", "checkout", "-f", checkout_target], cwd=target_repo_path, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return target_repo_path
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git operation failed: {e.stderr.decode().strip()}")
        if os.path.exists(target_repo_path):
            shutil.rmtree(target_repo_path, onerror=remove_readonly)
        raise e


# ─────────────────────────────────────────────────────────────
#  AST 级注释/Docstring 行号集合构建
# ─────────────────────────────────────────────────────────────

def build_comment_lines(abs_file_path: str) -> Set[int]:
    """
    解析 Python 源文件，返回所有注释/docstring 所占据的行号集合（1-based）。

    Args:
        abs_file_path: 文件的绝对路径

    Returns:
        Set[int]: 注释/docstring 行号集合，文件不存在或解析失败时返回空集合
    """
    comment_lines: Set[int] = set()

    if not os.path.exists(abs_file_path):
        return comment_lines

    try:
        with open(abs_file_path, 'r', encoding='utf-8', errors='replace') as f:
            source = f.read()
    except Exception:
        return comment_lines

    # ── Part 1: # 单行注释（tokenize，AST 不保留注释）──
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, _, tok_start, tok_end, _ in tokens:
            if tok_type == tokenize.COMMENT:
                for ln in range(tok_start[0], tok_end[0] + 1):
                    comment_lines.add(ln)
    except tokenize.TokenError:
        pass

    # ── Part 2: docstring（模块/类/函数 body 首条字符串常量）──
    try:
        tree = ast.parse(source, filename=abs_file_path)
    except SyntaxError:
        return comment_lines

    def _collect_docstring(body):
        if not body:
            return
        first = body[0]
        if (isinstance(first, ast.Expr) and
                isinstance(first.value, ast.Constant) and
                isinstance(first.value.value, str)):
            for ln in range(first.lineno, first.end_lineno + 1):
                comment_lines.add(ln)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                              ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect_docstring(node.body)

    return comment_lines


def has_comment_changes(hunk: Hunk, comment_lines_map: Dict[str, Set[int]]) -> bool:
    """
        判断一个 Hunk 是否为纯注释/docstring 修改。

        Args:
            hunk:              待判断的 Hunk（file_path 为相对路径）
            comment_lines_map: { 相对路径 -> 注释行号集合 }

        Returns:
            True  → 纯注释修改，应过滤
            False → 包含实际逻辑变更，应保留
        """
    file_comment_lines = comment_lines_map.get(hunk.file_path, set())
    if not file_comment_lines:
        return False

    current_line = hunk.new_start_line
    changed_lines_total = []
    changed_lines_in_comment = []

    for raw_line in hunk.content.splitlines():
        if raw_line.startswith('+'):
            changed_lines_total.append(current_line)
            if current_line in file_comment_lines:
                changed_lines_in_comment.append(current_line)
            current_line += 1
        elif raw_line.startswith('-'):
            changed_lines_total.append(current_line)
            if current_line in file_comment_lines:
                changed_lines_in_comment.append(current_line)
            # 删除行不推进 new 侧行号
        else:
            current_line += 1  # 上下文行

    if not changed_lines_total:
        return False

    return len(changed_lines_in_comment) == len(changed_lines_total)


# ─────────────────────────────────────────────────────────────
#  HunkDependencyAnalyzer
# ─────────────────────────────────────────────────────────────

class HunkDependencyAnalyzer:

    def __init__(self, repo_path: str, cache_key: str, cache_dir: str = "./graph_cache"):
        self.repo_path = os.path.abspath(repo_path)
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        cache_file = os.path.join(cache_dir, f"graph_{cache_key}.pkl")
        if os.path.exists(cache_file):
            logger.info(f"📂 Loading cached graph for {cache_key}...")
            with open(cache_file, 'rb') as f:
                self.graph = pickle.load(f)
        else:
            logger.info(f"🔄 Building graph from scratch for {cache_key}...")
            self.graph = build_graph(self.repo_path, fuzzy_search=True, global_import=True)
            with open(cache_file, 'wb') as f:
                pickle.dump(self.graph, f)

        self.entity_searcher = RepoEntitySearcher(self.graph)
        self.dep_searcher = RepoDependencySearcher(self.graph)

        # key: 相对路径（与 hunk.file_path 一致），value: 注释行号集合
        self._comment_lines_cache: Dict[str, Set[int]] = {}

    def build_comment_lines_for_hunks(self, relative_paths: Set[str]) -> None:
        """
        在 sandbox 被删除之前，预构建指定文件的注释行号缓存。

        Args:
            relative_paths: hunk.file_path 的集合（相对路径）
        """
        for rel_path in relative_paths:
            if rel_path in self._comment_lines_cache:
                continue
            abs_path = os.path.join(self.repo_path, rel_path)
            self._comment_lines_cache[rel_path] = build_comment_lines(abs_path)
        logger.debug(f"📝 Pre-built comment lines for {len(relative_paths)} file(s).")

    def get_comment_lines(self, relative_path: str) -> Set[int]:
        """获取指定文件的注释行号集合（需在 cleanup 前调用过 build_comment_lines_for_hunks）"""
        return self._comment_lines_cache.get(relative_path, set())

    def get_nodes_by_hunk(self, file_path: str, start_line: int, end_line: int) -> Set[str]:
        hunk_nodes = set()
        file_nodes = []
        for nid in self.graph.nodes():
            if str(nid).startswith(file_path):
                node_data = self.graph.nodes[nid]
                file_nodes.append((
                    nid,
                    node_data.get('start_line'),
                    node_data.get('end_line'),
                    node_data.get('type')
                ))

        for line_number in range(start_line, end_line + 1):
            candidate_nodes = []
            for nid, n_start, n_end, n_type in file_nodes:
                if n_type == NODE_TYPE_FILE:
                    candidate_nodes.append((nid, float('inf')))
                    continue
                if n_start is not None and n_end is not None and n_start <= line_number <= n_end:
                    candidate_nodes.append((nid, n_end - n_start))
            if candidate_nodes:
                candidate_nodes.sort(key=lambda x: x[1])
                hunk_nodes.add(candidate_nodes[0][0])
        return hunk_nodes


# ─────────────────────────────────────────────────────────────
#  GraphDependencyAnalyzerWrapper
# ─────────────────────────────────────────────────────────────

class GraphDependencyAnalyzerWrapper:

    def __init__(self, repo_path: str, target_hash: str, mode: str = 'new',
                 sandbox_root: str = "./sandboxes", cleanup_after_build: bool = True):
        self.original_repo_path = repo_path
        self.target_hash = target_hash
        self.mode = mode
        self.checkout_target = target_hash
        self.sandbox_name = f"{target_hash}_{mode}"
        self.cleanup_after_build = cleanup_after_build

        self.sandbox_path = prepare_sandbox_repo(
            self.original_repo_path, sandbox_root,
            self.sandbox_name, self.checkout_target
        )
        self.analyzer = HunkDependencyAnalyzer(self.sandbox_path, cache_key=self.target_hash)

    def analyze(self, hunks: List[Hunk], mode: str = 'new') -> Tuple[List[Dict], Dict, List, List[Hunk]]:
        hunk_dict = {}
        valid_hunks = []

        # 1. 提取有效 Hunk 及其行号范围
        for h in hunks:
            if mode == 'new' and getattr(h, 'new_start_line', 1) > 0:
                start = getattr(h, 'new_start_line', 0)
                end = start + getattr(h, 'new_len', 1) - 1
                hunk_dict[h.id] = (h.file_path, start, end)
                valid_hunks.append(h)
            elif mode == 'old' and getattr(h, 'old_start_line', 1) > 0:
                start = getattr(h, 'old_start_line', 0)
                end = start + getattr(h, 'old_len', 1) - 1
                path = h.file_path
                if path and path != "/dev/null":
                    hunk_dict[h.id] = (path, start, end)
                    valid_hunks.append(h)

        # ─────────────────────────────────────────────────────
        # 1.5 在 sandbox 存在时，预构建注释行缓存，然后再 cleanup
        # ─────────────────────────────────────────────────────
        relative_paths = {h.file_path for h in valid_hunks}
        self.analyzer.build_comment_lines_for_hunks(relative_paths)

        if self.cleanup_after_build and self.sandbox_path and os.path.exists(self.sandbox_path):
            cleanup_sandbox(self.sandbox_path)
            self.sandbox_path = None

        # 2. 映射 Hunk 到图节点
        hunk_to_nodes = {}
        for hid, (fpath, start, end) in hunk_dict.items():
            fpath_normalized = fpath.replace('/', '\\')
            nodes = self.analyzer.get_nodes_by_hunk(fpath_normalized, start, end)
            hunk_to_nodes[hid] = nodes

        # ─────────────────────────────────────────────────────
        # 2.5 过滤含注释/docstring 变更的 Hunk（AST 级别）
        #     只要 Hunk 中有任意一行是注释/docstring，整个 Hunk 过滤
        # ─────────────────────────────────────────────────────
        filtered_hids: Set[str] = set()
        for h in valid_hunks:
            comment_lines_map = {h.file_path: self.analyzer.get_comment_lines(h.file_path)}
            if has_comment_changes(h, comment_lines_map):
                filtered_hids.add(h.id)

        if filtered_hids:
            logger.debug(
                f"🧹 Filtered {len(filtered_hids)} hunk(s) containing comment/docstring changes: {filtered_hids}"
            )
            hunk_to_nodes = {hid: v for hid, v in hunk_to_nodes.items() if hid not in filtered_hids}
            hunk_dict     = {hid: v for hid, v in hunk_dict.items()     if hid not in filtered_hids}
            valid_hunks   = [h for h in valid_hunks                     if h.id not in filtered_hids]

        # 3. 计算边 (Edges)
        edges = []
        cross_file_count = 0
        hunk_ids = list(hunk_to_nodes.keys())
        max_hops = 0

        for i in range(len(hunk_ids)):
            for j in range(len(hunk_ids)):
                if i == j:
                    continue
                src_id, tgt_id = hunk_ids[i], hunk_ids[j]
                src_nodes, tgt_nodes = hunk_to_nodes[src_id], hunk_to_nodes[tgt_id]

                if not src_nodes or not tgt_nodes:
                    continue

                if src_nodes.intersection(tgt_nodes):
                    edges.append({"source": src_id, "target": tgt_id, "weight": 0, "type": "same_node"})
                    continue

                min_hops = float('inf')
                for s_node in src_nodes:
                    for t_node in tgt_nodes:
                        try:
                            path = nx.shortest_path(self.analyzer.graph, source=s_node, target=t_node)
                            hops = len(path) - 1
                            if hops > 10:
                                logger.debug(f"🔍 发现超长路径 ({hops} hops):")
                                for p_node in path:
                                    logger.debug(
                                        f"  -> {self.analyzer.graph.nodes[p_node].get('type', 'Unknown')}: {p_node}"
                                    )
                            if hops < min_hops:
                                min_hops = hops
                        except nx.NetworkXNoPath:
                            continue

                if min_hops != float('inf'):
                    if min_hops > max_hops:
                        max_hops = min_hops
                    edges.append({"source": src_id, "target": tgt_id, "weight": min_hops, "type": "dependency"})
                    if hunk_dict[src_id][0] != hunk_dict[tgt_id][0]:
                        cross_file_count += 1

        # 4. 计算 Metrics
        hunk_graph = nx.Graph()
        hunk_graph.add_nodes_from(hunk_ids)
        for e in edges:
            hunk_graph.add_edge(e["source"], e["target"])

        metrics = {
            "n_edges": len(edges),
            "all_hunks_connected": nx.is_connected(hunk_graph) if hunk_ids else True,
            "cross_file_dependencies": cross_file_count,
            "max_hops": max_hops
        }

        return edges, metrics, [], valid_hunks