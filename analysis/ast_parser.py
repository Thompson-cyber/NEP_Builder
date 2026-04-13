import ast
import os
import glob
import re
import subprocess
import tokenize
from collections import defaultdict
from io import BytesIO

import networkx as nx
from typing import List, Dict, Tuple, Set, Optional, Any
from core.types import Hunk
from loguru import logger


# ==========================================
# 1. 静态辅助函数
# ==========================================


def get_structural_info(code: str) -> Tuple[Set[int], List[Tuple[int, int]]]:
    """
    分析代码，返回：
    1. ignore_set: 所有非结构化行号的集合 (包括 #, 空行, docstring) -> 用于判断删除行
    2. docstring_ranges: 多行字符串的闭区间列表 [(start, end), ...] -> 用于判断新增行
    """
    ignore_set = set()
    docstring_ranges = []

    if not code:
        return ignore_set, docstring_ranges

    lines = code.splitlines()

    # 1. 识别 # 注释
    try:
        tokens = tokenize.tokenize(BytesIO(code.encode('utf-8')).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT:
                ignore_set.add(token.start[0])
    except Exception:
        pass

    # 2. 识别空行
    for i, line in enumerate(lines, 1):
        if not line.strip():
            ignore_set.add(i)

    # 3. 识别多行字符串 (Docstrings / Block Comments)
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr):
                val = node.value
                is_string_node = False

                if isinstance(val, ast.Str):  # Python < 3.8
                    is_string_node = True
                elif isinstance(val, ast.Constant) and isinstance(val.value, str):  # Python >= 3.8
                    is_string_node = True

                if is_string_node:
                    start = getattr(node, 'lineno', -1)
                    end = getattr(node, 'end_lineno', start)
                    if start != -1:
                        # 记录区间
                        docstring_ranges.append((start, end))
                        # 记录集合
                        for lineno in range(start, end + 1):
                            ignore_set.add(lineno)
    except Exception:
        pass

    return ignore_set, docstring_ranges


# ==========================================
# 2. 核心数据结构
# ==========================================

class Symbol:
    """代表代码中的一个实体（函数、类、Import声明等）"""

    def __init__(self, name: str, file_path: str, node_type: str, start_line: int, end_line: int):
        self.name = name
        self.file_path = file_path
        self.node_type = node_type
        self.start_line = start_line
        self.end_line = end_line


class GlobalSymbolIndex:
    """
    全局符号索引，用于跨文件查找和模糊匹配。
    """

    def __init__(self):
        # short_name -> Set[full_qualified_name]
        self.name_map: Dict[str, Set[str]] = defaultdict(set)

    def register(self, short_name: str, full_name: str):
        self.name_map[short_name].add(full_name)

    def lookup(self, short_name: str) -> Set[str]:
        return self.name_map.get(short_name, set())


# ==========================================
# 3. 增强的 AST 遍历器
# ==========================================

class EnhancedRepoVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str, module_name: str,
                 symbol_table: Dict[str, Symbol],
                 graph: nx.DiGraph,
                 global_index: GlobalSymbolIndex,
                 build_edges: bool = True):
        self.file_path = file_path
        if module_name.endswith('.py'):
            module_name = module_name[:-3]

            # 2. 将路径分隔符 / 或 \ 替换为点 .
        self.module_name = module_name.replace('/', '.').replace('\\', '.')
        self.symbol_table = symbol_table
        self.graph = graph
        self.global_index = global_index
        self.build_edges = build_edges  # 控制是只注册符号(Phase 1)还是构建连线(Phase 2)

        self.scope_stack: List[str] = [module_name]
        self.current_class: Optional[str] = None

        # import_scope: alias -> (real_full_name, import_node_id)
        self.import_scope: Dict[str, Tuple[str, str]] = {}
        self.current_node_id = module_name

    def _get_current_scope(self):
        return ".".join(self.scope_stack)

    def _register_symbol(self, full_name: str, node: ast.AST, type_label: str):
        # 注册节点到符号表和图
        start = getattr(node, 'lineno', 0)
        end = getattr(node, 'end_lineno', 0)
        symbol = Symbol(full_name, self.file_path, type_label, start, end)
        self.symbol_table[full_name] = symbol
        self.graph.add_node(full_name, file=self.file_path, type=type_label, line=start)

        # 注册到全局索引 (Import 节点通常不需要全局索引，除非为了调试)
        if type_label in ('function', 'class'):
            short_name = full_name.split('.')[-1]
            self.global_index.register(short_name, full_name)

        return full_name

    def _resolve_target_info(self, target_name: str) -> Tuple[Set[str], Optional[str]]:
        """
        解析目标名称。
        Returns: (Candidates[Set], ImportNodeID[Optional])
        """
        candidates = set()
        import_source_node = None

        # 1. 尝试 Import 解析
        parts = target_name.split('.')
        root = parts[0]
        if root in self.import_scope:
            resolved_root, import_node_id = self.import_scope[root]
            import_source_node = import_node_id
            # 拼接全名
            full = resolved_root + ('.' + '.'.join(parts[1:]) if len(parts) > 1 else '')
            candidates.add(full)
            return candidates, import_source_node

        # 2. 本地/当前模块定义
        local_full = f"{self.module_name}.{target_name}"
        if local_full in self.symbol_table:
            candidates.add(local_full)
            return candidates, None

        # 3. 类内部调用 (self.method)
        if self.current_class and target_name.startswith('self.'):
            method_name = target_name.split('.')[1]
            class_method = f"{self.current_class}.{method_name}"
            if class_method in self.symbol_table:
                candidates.add(class_method)
                return candidates, None

        # 4. 全局启发式搜索 (Fuzzy Match)
        short_name = parts[-1]
        global_matches = self.global_index.lookup(short_name)
        # 避免匹配到过于通用的名字 (如 get, id) 导致爆炸，限制数量
        if 0 < len(global_matches) < 15:
            candidates.update(global_matches)

        return candidates, None

    def _add_edge(self, target_name: str, edge_type: str):
        if not self.build_edges: return
        if target_name in __builtins__ or len(target_name) < 2: return

        candidates, import_via_node = self._resolve_target_info(target_name)

        # A. 如果是通过 Import 引入的，建立 Current -> ImportNode 的连接
        if import_via_node and import_via_node != self.current_node_id:
            self.graph.add_edge(self.current_node_id, import_via_node, type='use_import')

        # B. 建立指向真实目标的连接
        for resolved_name in candidates:
            # 确保目标在我们的代码库中
            if resolved_name in self.symbol_table and resolved_name != self.current_node_id:
                self.graph.add_edge(self.current_node_id, resolved_name, type=edge_type)

    # --- AST Visitors ---

    def visit_Import(self, node):
        for alias in node.names:
            real_name = alias.name
            asname = alias.asname or alias.name

            # 1. 创建 Import 节点 (关键：让 Diff 能映射到这里)
            import_node_id = f"{self.module_name}.__imports__.{asname}"
            self._register_symbol(import_node_id, node, 'import')

            # 2. 注册到作用域
            self.import_scope[asname] = (real_name, import_node_id)

            # 3. ImportNode -> 外部真实节点 (尝试连接)
            if self.build_edges:
                # 尝试精确匹配
                if real_name in self.symbol_table:
                    self.graph.add_edge(import_node_id, real_name, type='imports')
                # 尝试模糊匹配 (比如 import utils -> utils.py module)
                else:
                    matches = self.global_index.lookup(real_name.split('.')[-1])
                    for m in matches:
                        if m == real_name or m.endswith(f".{real_name}"):
                            self.graph.add_edge(import_node_id, m, type='imports')

    def visit_ImportFrom(self, node):
        # 1. 解析模块路径 (修复 NoneType 错误)
        if node.level > 0:
            # 1. 获取当前模块的全名列表
            # 确保 self.module_name 是 "sklearn.utils.validation"
            parts = self.module_name.split('.')

            # 2. 执行回溯 (处理 ..)
            if node.level >= len(parts):
                # 这种情况通常是出错了，或者在根目录下用了相对导入
                base = ""
            else:
                # 关键步骤：切片保留父级路径
                # parts[:-2] 变成了 ['sklearn']
                base = ".".join(parts[:-node.level])

            # 3. 拼接后缀 (处理 utils.fixes)
            if node.module:
                resolved_module = f"{base}.{node.module}" if base else node.module
            else:
                resolved_module = base
        else:
            resolved_module = node.module

        # 2. 遍历导入的名称
        for alias in node.names:
            # 别名处理：代码中是 as _get_config，所以节点ID应该用 _get_config
            asname = alias.asname or alias.name

            # 生成节点 ID
            import_node_id = f"{self.module_name}.__imports__.{asname}"

            # 注册节点
            self._register_symbol(import_node_id, node, 'import')

            # 记录全名以便后续连线
            full_real = f"{resolved_module}.{alias.name}"
            self.import_scope[asname] = (full_real, import_node_id)

            if self.build_edges:
                # 目标 1: 对方定义的类/函数 (最理想)
                target_def = f"{resolved_module}.{alias.name}"

                # 目标 2: 对方也是导入的这个变量 (Re-export)
                # 注意：对方的导入节点ID格式通常是 module.__imports__.name
                target_reexport = f"{resolved_module}.__imports__.{alias.name}"

                linked = False

                # 尝试连接定义
                if target_def in self.symbol_table:
                    self.graph.add_edge(import_node_id, target_def, type='imports')
                    linked = True

                # 尝试连接转发 (关键修复!)
                elif target_reexport in self.symbol_table:
                    self.graph.add_edge(import_node_id, target_reexport, type='imports_reexport')
                    linked = True

                # 情况 C: 模糊匹配 (保留原有逻辑作为兜底)
                if not linked:
                    candidates, _ = self._resolve_target_info(alias.name)
                    for c in candidates:
                        if resolved_module.split('.')[-1] in c:
                            self.graph.add_edge(import_node_id, c, type='imports_fuzzy')

    def visit_ClassDef(self, node):
        full_name = f"{self._get_current_scope()}.{node.name}"
        self._register_symbol(full_name, node, 'class')

        if self.build_edges:
            for base in node.bases:
                name = self._get_attr_name(base)
                if name: self._add_edge(name, 'inherit')

        prev_class = self.current_class
        self.current_class = full_name

        self.scope_stack.append(node.name)
        prev_node = self.current_node_id
        self.current_node_id = full_name

        self.generic_visit(node)

        self.current_node_id = prev_node
        self.scope_stack.pop()
        self.current_class = prev_class

    def visit_FunctionDef(self, node):
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_func(node)

    def _handle_func(self, node):
        full_name = f"{self._get_current_scope()}.{node.name}"
        self._register_symbol(full_name, node, 'function')

        if self.build_edges:
            for dec in node.decorator_list:
                name = self._get_attr_name(dec)
                if name: self._add_edge(name, 'decorate')

        self.scope_stack.append(node.name)
        prev_node = self.current_node_id
        self.current_node_id = full_name

        self.generic_visit(node)

        self.current_node_id = prev_node
        self.scope_stack.pop()

    def visit_Call(self, node):
        if not self.build_edges: return
        func_name = self._get_attr_name(node.func)
        if func_name:
            self._add_edge(func_name, 'call')
        self.generic_visit(node)

    def visit_Name(self, node):
        if not self.build_edges: return
        if isinstance(node.ctx, ast.Load):
            self._add_edge(node.id, 'read')

    def visit_Attribute(self, node):
        if not self.build_edges: return
        full_name = self._get_attr_name(node)
        if full_name:
            self._add_edge(full_name, 'use')

    def _get_attr_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            val = self._get_attr_name(node.value)
            return f"{val}.{node.attr}" if val else node.attr
        return None


# ==========================================
# 4. 主分析器
# ==========================================

class RobustASTAnalyzer:
    def __init__(self, repo_path: str,commit_hash):
        self.repo_path = os.path.abspath(repo_path)
        self.graph = nx.DiGraph()
        self.symbol_table: Dict[str, Symbol] = {}
        self.global_index = GlobalSymbolIndex()
        self._graph_built = False
        self.commit_hash = commit_hash
    def _is_line_in_ranges(self, line_no: int, ranges: List[Tuple[int, int]]) -> bool:
        """判断行号是否在任意一个多行字符串区间内"""
        for start, end in ranges:
            # 使用 <= end 是因为如果在 docstring 结束行(即闭合引号所在行)之前插入，
            # 它依然属于 docstring 内部。
            # 比如:
            # 10: """
            # 11: content
            # 12: """
            # 在 11 和 12 之间插入，curr_old_line 会指向 12，12 在区间内 -> 是注释。
            if start <= line_no <= end:
                return True
        return False

    def _read_file_content(self, rel_path: str) -> str:
        """
        核心修改：通过 git show 读取指定 commit 的文件内容
        而不是读取磁盘上的当前文件
        """
        try:
            # 使用 git show <hash>:<path> 获取内容
            # 注意：路径需要是相对于 git 根目录的相对路径
            # cmd = ["git", "show", f"{self.commit_hash}:{rel_path}"]
            cmd = ["git", "show", f"{self.commit_hash}:{rel_path.replace(os.sep, '/')}"]

            # 指定 cwd 为仓库路径
            result = subprocess.check_output(
                cmd,
                cwd=self.repo_path,
                stderr=subprocess.DEVNULL
            )
            return result.decode('utf-8', errors='ignore')
        except subprocess.CalledProcessError:
            # 文件在该 commit 中可能不存在（比如是新增文件，或者路径错误）
            return ""

    def _build_graph(self):
        if self._graph_built: return

        # 1. 获取该 Commit 时刻存在的所有 Python 文件
        # 使用 git ls-tree -r <hash> --name-only
        try:
            cmd = ["git", "ls-tree", "-r", self.commit_hash, "--name-only"]
            output = subprocess.check_output(cmd, cwd=self.repo_path).decode('utf-8')
            all_files = [f for f in output.splitlines() if f.endswith('.py')]
        except Exception:
            all_files = []

        logger.info(f"Analyzing {len(all_files)} files at commit {self.commit_hash[:8]}")
        file_asts = {}

        # Phase 1: Indexing (只注册符号，不连线)
        for rel_path in all_files:
            rel_path = rel_path.replace("\\","/")
            try:
                module_name = rel_path.replace(os.sep, '.').replace('.py', '')

                code = self._read_file_content(rel_path)
                if not code: continue

                tree = ast.parse(code)
                file_asts[rel_path] = (module_name, tree)

                # 注册 Module 节点
                self.graph.add_node(module_name, file=rel_path, type='module', line=0)
                self.symbol_table[module_name] = Symbol(module_name, rel_path, 'module', 0, 0)
                if rel_path.endswith('sklearn/utils/validation.py'):
                    pass
                visitor = EnhancedRepoVisitor(rel_path, module_name, self.symbol_table,
                                              self.graph, self.global_index, build_edges=False)
                visitor.visit(tree)
            except Exception as e:
                logger.warning(f"Parse error {rel_path}: {e}")

        # Phase 2: Linking (构建连线)
        logger.info(f"Indexing complete. Building dependency graph...")
        for file_path, (module_name, tree) in file_asts.items():
            try:
                if file_path.endswith('sklearn/utils/validation.py'):
                    pass
                visitor = EnhancedRepoVisitor(file_path, module_name, self.symbol_table,
                                              self.graph, self.global_index, build_edges=True)
                visitor.visit(tree)
            except Exception as e:
                logger.warning(f"Linking error {file_path}: {e}")

        self._graph_built = True
        logger.info(f"Graph built: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")

    def get_valid_hunks(self, hunks: List[Hunk], mode: str = "new") -> List[Hunk]:
        valid_hunks = []
        file_struct_cache: Dict[str, Tuple[Set[int], List[Tuple[int, int]]]] = {}

        # 1. 确定属性字段
        if mode == "new":
            start_attr = "new_start_line"
            len_attr = "new_len"
        else:
            start_attr = "old_start_line"
            len_attr = "old_len"

        for hunk in hunks:
            # === 1. 缓存文件结构信息 ===
            if hunk.file_path not in file_struct_cache:
                # 注意：这里读取的必须是与 mode 对应的文件版本
                # 如果 mode="new"，读当前 commit；如果 mode="old"，读 parent commit
                content = self._read_file_content(hunk.file_path.replace("\\", "/"))

                if content:
                    file_struct_cache[hunk.file_path] = get_structural_info(content)
                else:
                    file_struct_cache[hunk.file_path] = (set(), [])

            ignore_set, doc_ranges = file_struct_cache[hunk.file_path]

            # === 2. 基于行号范围的快速判断 ===
            start_line = getattr(hunk, start_attr)
            length = getattr(hunk, len_attr)

            # 如果长度为 0 (例如纯删除操作在 mode='new' 下)，视为无效或根据需求处理
            is_structural_hunk = False

            # 直接遍历该 Hunk 覆盖的行号区间 [start, start + len)
            for line_no in range(start_line, start_line + length+1):
                # 核心判断逻辑：
                # 1. line_no 不在 ignore_set 中 (不是单行注释，不是空行)
                # 2. line_no 不在 doc_ranges 中 (不是多行字符串/文档注释)
                if (line_no not in ignore_set) and (not self._is_line_in_ranges(line_no, doc_ranges)):
                    is_structural_hunk = True
                    break  # 只要发现一行有效代码，整个 Hunk 即视为有效，停止检查

            if is_structural_hunk:
                valid_hunks.append(hunk)
            else:
                logger.debug(f"Hunk ignored (Comment/Docstring): {hunk.file_path} lines {start_line}-{start_line+length}")
                pass

        return valid_hunks

    def _refine_hunks_granularity(self, hunks: List[Hunk]) -> List[Hunk]:
        """
        将包含上下文的粗粒度 Hunk 拆分为仅包含连续修改的细粒度 Hunk。
        例如：
        Hunk A:
          ctx
        - old1
          ctx
        + new1

        会被拆分为：
        Hunk A_0: - old1
        Hunk A_1: + new1
        """
        refined_hunks = []

        for hunk in hunks:
            lines = hunk.content.splitlines()

            # 追踪当前的行号
            curr_old_line = hunk.old_start_line
            curr_new_line = hunk.new_start_line

            # 缓冲区，用于积累连续的修改行
            buffer_lines = []
            # 记录当前缓冲区的起始行号
            buffer_start_old = curr_old_line
            buffer_start_new = curr_new_line

            sub_index = 0

            for line in lines:
                # 跳过 diff header
                if line.startswith('@@') or line.startswith('diff'):
                    continue

                # 情况 1: 上下文行 (以空格开头) 或 空行
                # 这意味着之前的连续修改结束了
                if line.startswith(' ') or not line:
                    if buffer_lines:
                        # --- 结算缓冲区，创建新的 Sub-Hunk ---
                        new_hunk = self._create_sub_hunk(hunk, buffer_lines, buffer_start_old, buffer_start_new,
                                                         sub_index)
                        refined_hunks.append(new_hunk)
                        sub_index += 1
                        buffer_lines = []

                    # 上下文行同时消耗旧版本和新版本的行号
                    curr_old_line += 1
                    curr_new_line += 1

                    # 重置缓冲区的起始点为下一行
                    buffer_start_old = curr_old_line
                    buffer_start_new = curr_new_line

                # 情况 2: 删除行 (-)
                elif line.startswith('-'):
                    buffer_lines.append(line)
                    curr_old_line += 1

                # 情况 3: 新增行 (+)
                elif line.startswith('+'):
                    buffer_lines.append(line)
                    curr_new_line += 1

                # 情况 4: \ No newline... (忽略)
                elif line.startswith('\\'):
                    pass

            # 循环结束后，如果缓冲区还有内容，结算最后一次
            if buffer_lines:
                new_hunk = self._create_sub_hunk(hunk, buffer_lines, buffer_start_old, buffer_start_new, sub_index)
                refined_hunks.append(new_hunk)

        return refined_hunks

    def _create_sub_hunk(self, parent_hunk: Hunk, lines: List[str], start_old: int, start_new: int,
                         sub_index: int) -> Hunk:
        """辅助函数：构建新的 Hunk 对象"""
        # 计算新 Hunk 的长度
        old_len = sum(1 for line in lines if line.startswith('-'))
        new_len = sum(1 for line in lines if line.startswith('+'))

        # 构建新的 ID
        new_id = f"{parent_hunk.id}_{sub_index}"

        # 这里假设 Hunk 是一个 dataclass 或者有类似的构造函数
        # 如果 Hunk 是不可变的，你需要根据你的 core.types.Hunk 定义来调整
        return Hunk(
            id=new_id,
            file_path=parent_hunk.file_path,
            old_start_line=start_old,
            old_len=old_len,
            new_start_line=start_new,
            new_len=new_len,
            content="\n".join(lines),
            start_line=start_new,
            end_line=start_new + new_len
        )

    def _calculate_topology_metrics(self, hunk_digraph: nx.DiGraph) -> Dict:
        """
        计算 Hunk 依赖图的拓扑指标：
        1. max_depth: Hunk 依赖链的最大长度 (Hunk A -> Hunk B -> Hunk C => depth 2)
        2. layers: 每个 Hunk 所在的层级
        """
        if hunk_digraph.number_of_nodes() == 0:
            return {"max_depth": 0, "layers": {}}

        # 1. 处理循环依赖：将图压缩为 DAG (有向无环图)
        # 强连通分量 (SCC) 中的节点会被视为同一层级
        dag = nx.condensation(hunk_digraph)

        # 2. 计算 DAG 中每个超级节点(SCC)的最长路径层级
        # 使用 topological_generations 或者简单的最长路径算法
        # 这里定义：入度为 0 的节点层级为 0

        node_layers = {}
        max_depth = 0

        try:
            # networkx 的 topological_generations 返回分层列表 [[root_nodes], [level_1], ...]
            generations = list(nx.topological_generations(dag))
            max_depth = len(generations) - 1 if generations else 0

            # 映射回原始 Hunk ID
            hunk_layers = {}
            for level, scc_nodes in enumerate(generations):
                for scc_index in scc_nodes:
                    # 获取该 SCC 包含的原始 Hunk ID
                    original_hunks = dag.nodes[scc_index]['members']
                    for h_id in original_hunks:
                        hunk_layers[h_id] = level

        except Exception as e:
            logger.warning(f"Topology calculation failed: {e}")
            hunk_layers = {n: 0 for n in hunk_digraph.nodes()}
            max_depth = 0

        return {
            "max_dependency_chain_length": max_depth,
            "hunk_layers": hunk_layers  # Dict[hunk_id, layer_index]
        }

    def analyze(self, hunks: List[Hunk], mode: str = "new") -> Tuple[List[Any], Dict, List[Any], List[Hunk]]:
        # === 1. 细粒度拆分 ===
        refined_hunks = self._refine_hunks_granularity(hunks)

        # === 2. 过滤无效修改 ===
        valid_hunks = self.get_valid_hunks(refined_hunks, mode)
        if len(valid_hunks) < 2:
            return [], {"status": "not_enough_hunks"}, [], valid_hunks

        # 构建全局调用图
        self._build_graph()

        # 2. 映射 Hunk 到 Graph Nodes
        hunk_to_nodes = self._map_hunks_to_nodes(valid_hunks, mode=mode)

        edges = set()
        chains = []
        cross_file_deps = 0

        # 用于构建 Hunk 级别的有向图 (Hunk A -> Hunk B)
        hunk_dependency_graph = nx.DiGraph()
        hunk_dependency_graph.add_nodes_from([h.id for h in valid_hunks])

        # 3. 路径搜索
        for i, h_src in enumerate(valid_hunks):
            src_nodes = hunk_to_nodes.get(h_src.id, set())
            if not src_nodes: continue

            for h_tgt in valid_hunks:
                if h_src.id == h_tgt.id: continue
                tgt_nodes = hunk_to_nodes.get(h_tgt.id, set())
                if not tgt_nodes: continue

                shortest_path = None

                # 在所有可能的源节点和目标节点组合中找最短路径
                for s_node in src_nodes:
                    for t_node in tgt_nodes:
                        try:
                            # 查找路径
                            path = nx.shortest_path(self.graph, source=s_node, target=t_node)
                            if path:
                                if shortest_path is None or len(path) < len(shortest_path):
                                    shortest_path = path
                        except nx.NetworkXNoPath:
                            continue

                if shortest_path:
                    # 计算 AST 距离 (节点数 - 1 = 边数)
                    ast_distance = len(shortest_path) - 1

                    edges.add((h_src.id, h_tgt.id, "dependency"))

                    # 记录到 Hunk 依赖图
                    hunk_dependency_graph.add_edge(h_src.id, h_tgt.id, weight=ast_distance)

                    chains.append({
                        "source": h_src.id,
                        "target": h_tgt.id,
                        "distance": ast_distance,  # [新增] AST 层面的深度
                        "path": self._format_chain(shortest_path),
                        "raw_path": shortest_path
                    })
                    if h_src.file_path != h_tgt.file_path:
                        cross_file_deps += 1

        # 4. 计算拓扑指标 (层级、最大深度等)
        topology_metrics = self._calculate_topology_metrics(hunk_dependency_graph)

        # 5. 基础统计
        # 转换为无向图计算连通分量
        hunk_undirected = hunk_dependency_graph.to_undirected()
        is_connected = nx.number_connected_components(hunk_undirected) == 1 if len(valid_hunks) > 0 else True

        metrics = {
            "cross_file_dependencies": cross_file_deps,
            "all_hunks_connected": is_connected,
            "connected_components": [list(c) for c in nx.connected_components(hunk_undirected)],
            # [新增] 包含最大深度和每个 Hunk 的层级信息
            "topology": topology_metrics
        }

        return list(edges), metrics, chains, valid_hunks

    def _map_hunks_to_nodes(self, hunks: List[Hunk], mode: str = "new") -> Dict[str, Set[str]]:
        mapping = {}
        start_attr = "new_start_line" if mode == "new" else "old_start_line"
        len_attr = "new_len" if mode == "new" else "old_len"
        for hunk in hunks:
            start_line = getattr(hunk, start_attr)
            length = getattr(hunk, len_attr) + 1

            end_line = start_line + length - 1
            if end_line < start_line:
                end_line = start_line
            mapping[hunk.id] = set()
            target_path = os.path.normpath(hunk.file_path)
            # 找到该文件下的所有 Symbol
            file_symbols = [s for s in self.symbol_table.values() if os.path.normpath(s.file_path) == target_path]

            # 排序：优先匹配范围更小的 Symbol (比如 Import 语句通常只有 1 行，函数有 10 行)
            # 这样如果一行既属于 Module 也属于 Import，会优先匹配 Import
            file_symbols.sort(key=lambda s: s.end_line - s.start_line)

            matched = False
            for symbol in file_symbols:
                # 检查 Hunk 范围是否与 Symbol 范围有交集
                # Hunk: [start, end], Symbol: [start, end]
                if not (end_line < symbol.start_line or start_line > symbol.end_line):
                    mapping[hunk.id].add(symbol.name)
                    matched = True
                    # 这里不 break，因为一个 hunk 可能跨越多个函数，或者同时修改了 import 和 function

            # 如果什么都没匹配到，回退到 Module 级别
            if not matched:
                rel_path = hunk.file_path
                module_name = rel_path.replace('/', '.').replace('\\', '.').replace('.py', '')
                # module_name = rel_path.replace(os.sep, '.').replace('.py', '')
                if module_name in self.graph:
                    mapping[hunk.id].add(module_name)

        return mapping

    def _format_chain(self, path: List[str]) -> List[str]:
        formatted = []
        for node in path:
            if node in self.symbol_table:
                sym = self.symbol_table[node]
                rel_file = os.path.relpath(sym.file_path, self.repo_path)
                formatted.append(f"[{sym.node_type}] {node} ({rel_file}:{sym.start_line})")
            else:
                formatted.append(node)
        return formatted


# 导出类
AdvancedASTAnalyzer = RobustASTAnalyzer
