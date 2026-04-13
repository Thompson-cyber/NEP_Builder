import heapq
import itertools
import networkx as nx
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any
from core.types import Hunk


@dataclass
class SortResult:
    """排序结果，携带完整执行上下文"""
    ordered_hunks: List[Hunk]           # 最终线性执行顺序（先执行的在前）
    execution_layers: List[List[Hunk]]  # 按层分组（Layer 0 最先，同层可并行）
    broken_edges: List[Dict]            # 被破除的循环依赖边
    has_cycle: bool                     # 是否存在循环
    dependency_chains: List[str]        # 可读依赖链（给 LLM Prompt 用）


class DependencyGraph:
    """
    依赖关系图。

    ┌─────────────────────────────────────────────────────┐
    │  边语义约定（贯穿全类，不可更改）                      │
    │                                                       │
    │  输入 edge: source=A, target=B  →  表示 A 依赖 B     │
    │  即 B 必须先于 A 执行                                 │
    │                                                       │
    │  内部存图：add_edge(B, A)  即 target → source        │
    │  这样 B 的入度为 0，拓扑序先输出 B，符合"B先执行"     │
    └─────────────────────────────────────────────────────┘
    """

    # ------------------------------------------------------------------ #
    #  公开接口
    # ------------------------------------------------------------------ #

    def sort(
        self,
        hunks: List[Hunk],
        edges: List[Dict[str, Any]],
    ) -> Tuple[List[Hunk], List[Dict[str, Any]], bool]:
        """
        拓扑排序（兼容旧接口）。

        :param hunks: Hunk 对象列表
        :param edges: 边列表 [{'source': A, 'target': B, 'type': ..., 'weight': ...}]
                      source=A, target=B 表示 A 依赖 B，B 先执行
        :return: (排序后的 hunk 列表, 原始 edges, 是否存在循环)
        """
        result = self.sort_with_context(hunks, edges)
        return result.ordered_hunks, edges, result.has_cycle

    def sort_with_context(
        self,
        hunks: List[Hunk],
        edges: List[Dict[str, Any]],
    ) -> SortResult:
        """
        拓扑排序（增强版），返回完整执行上下文。

        排序优先级（多个合法拓扑序时的裁决规则，分数越小越靠前）：
          1. 执行层（layer）：层数越小越先执行，确保基础依赖最先
          2. 出度（被依赖数）：被依赖越多越先，关键节点优先（取负）
          3. 文件局部性：同文件 Hunk 尽量连续，减少上下文切换
          4. 行号顺序：同文件内从上到下，符合阅读习惯
        """
        if not hunks:
            return SortResult([], [], [], False, [])

        hunk_map = {h.id: h for h in hunks}

        # Step 1: 构建 DAG（处理循环依赖）
        G, broken_edges, has_cycle = self._build_dag(hunks, edges)

        # Step 2: 计算执行层（layer）和优先级
        layer_map = self._compute_layer_map(G)
        priority = self._compute_priority(G, hunk_map, layer_map)

        # Step 3: Kahn 算法 + 优先级裁决，得到最终线性顺序
        ordered_ids = self._kahn_sort(G, priority)
        ordered_hunks = [hunk_map[hid] for hid in ordered_ids]

        # Step 4: 将 layer_map 转换为分层结构
        execution_layers = self._build_execution_layers(layer_map, hunk_map)

        # Step 5: 提取依赖链（复用已构建的 DAG，避免重复构图）
        chains = self._extract_chains_from_graph(G, hunk_map, edges)

        return SortResult(
            ordered_hunks=ordered_hunks,
            execution_layers=execution_layers,
            broken_edges=broken_edges,
            has_cycle=has_cycle,
            dependency_chains=chains,
        )

    def extract_chains(
        self,
        hunks: List[Hunk],
        edges: List[Dict[str, Any]],
    ) -> List[str]:
        """
        提取可读的依赖链路（用于 LLM Prompt，独立调用入口）。
        格式: "FileA.py:10 --[calls]--> FileB.py:50 --[calls]--> FileC.py:80"
        """
        if not edges or not hunks:
            return []
        hunk_map = {h.id: h for h in hunks}
        # 独立调用时构建含完整属性的图（不破环，_find_best_path 内部防环）
        G = self._build_graph(hunks, edges)
        return self._extract_chains_from_graph(G, hunk_map, edges)

    # ------------------------------------------------------------------ #
    #  私有：图构建
    # ------------------------------------------------------------------ #

    def _build_graph(
        self,
        hunks: List[Hunk],
        edges: List[Dict[str, Any]],
    ) -> nx.DiGraph:
        """
        构建有向图（可能含环）。

        关键：输入 edge(source=A, target=B) 表示 A 依赖 B
             存图时反转为 add_edge(B, A)，使 B 入度为 0，拓扑序先出
        """
        G = nx.DiGraph()
        hunk_ids = {h.id for h in hunks}
        G.add_nodes_from(hunk_ids)

        for edge in edges:
            src = edge.get('source')   # A：依赖方（后执行）
            dst = edge.get('target')   # B：被依赖方（先执行）
            if src in hunk_ids and dst in hunk_ids:
                # 存图反转：B → A，保证 B 入度更小，拓扑序先出
                G.add_edge(
                    dst, src,
                    label=edge.get('type', 'dependency'),
                    weight=edge.get('weight', 1),
                )
        return G

    def _build_dag(
        self,
        hunks: List[Hunk],
        edges: List[Dict[str, Any]],
    ) -> Tuple[nx.DiGraph, List[Dict], bool]:
        """
        构建 DAG（有向无环图）。

        若存在环，采用"最大权重破环"策略：
          - 找出所有强连通分量（SCC），大小 > 1 的即为环
          - 在每个 SCC 的内部边中，删除权重最大的边（最弱依赖）
          - 记录被删除的边，供上层感知
          - 循环直到图变为 DAG

        :return: (DAG, 被删除的边列表, 是否存在循环)
        """
        G = self._build_graph(hunks, edges)
        broken_edges: List[Dict] = []
        has_cycle = not nx.is_directed_acyclic_graph(G)

        if has_cycle:
            max_iterations = G.number_of_edges() + 1  # 防御性上限
            for _ in range(max_iterations):
                if nx.is_directed_acyclic_graph(G):
                    break

                # 找第一个大小 > 1 的 SCC（真正的环）
                scc = next(
                    (s for s in nx.strongly_connected_components(G) if len(s) > 1),
                    None,
                )
                if scc is None:
                    break

                # 找该 SCC 内部所有边
                scc_edges = [
                    (u, v, G[u][v])
                    for u in scc
                    for v in G.successors(u)
                    if v in scc
                ]
                if not scc_edges:
                    break

                # 删除权重最大的边（最弱依赖，破坏代价最小）
                u, v, data = max(scc_edges, key=lambda e: e[2].get('weight', 1))
                # 注意：图内存的是反转边 (dst→src)，记录时还原为输入语义 (src→dst)
                broken_edges.append({
                    'source': v,   # 还原：v 是原始的 source（依赖方）
                    'target': u,   # 还原：u 是原始的 target（被依赖方）
                    'type': data.get('label', 'dependency'),
                    'weight': data.get('weight', 1),
                })
                G.remove_edge(u, v)

        return G, broken_edges, has_cycle

    # ------------------------------------------------------------------ #
    #  私有：排序算法
    # ------------------------------------------------------------------ #

    def _compute_layer_map(self, G: nx.DiGraph) -> Dict[str, int]:
        """
        计算每个节点的执行层（Layer）。

        Layer 0：入度为 0 的节点，无任何前置依赖，最先执行。
        Layer N：所有前置节点都在 Layer < N 中。

        实现：按拓扑序遍历，每个节点的 layer = max(前驱 layer) + 1。
        由于图内边是 B→A（B先执行），B 是 A 的前驱（predecessor），
        所以 layer[A] = max(layer[B] for B in predecessors(A)) + 1。
        """
        layer_map: Dict[str, int] = {n: 0 for n in G.nodes()}
        for node in nx.topological_sort(G):
            for successor in G.successors(node):
                # node 先执行（layer 更小），successor 后执行（layer 更大）
                layer_map[successor] = max(
                    layer_map[successor],
                    layer_map[node] + 1,
                )
        return layer_map

    def _compute_priority(
        self,
        G: nx.DiGraph,
        hunk_map: Dict[str, Hunk],
        layer_map: Dict[str, int],
    ) -> Dict[str, Tuple]:
        """
        为每个节点计算排序优先级 tuple（tuple 越小越靠前）。

        四维优先级：
          [0] layer：执行层，越小越先（主键，保证依赖正确性）
          [1] -out_degree：出度取负，被依赖越多越先（关键节点优先）
          [2] file_path：文件路径字典序，同文件 Hunk 聚集
          [3] start_line：行号，同文件内从上到下
        """
        priority: Dict[str, Tuple] = {}
        for node in G.nodes():
            hunk = hunk_map.get(node)
            file_path = hunk.file_path if hunk else ''
            start_line = hunk.start_line if hunk else 0
            # 出度：在反转图中，node 的出度 = 有多少节点依赖 node（即 node 被依赖数）
            out_degree = G.out_degree(node)
            priority[node] = (
                layer_map.get(node, 0),   # 主键：执行层
                -out_degree,              # 次键：被依赖数（越多越先）
                file_path,                # 三键：文件路径
                start_line,               # 四键：行号
            )
        return priority

    def _kahn_sort(
        self,
        G: nx.DiGraph,
        priority: Dict[str, Tuple],
    ) -> List[str]:
        """
        Kahn 算法拓扑排序，使用优先队列（min-heap）进行优先级裁决。

        每次从所有"就绪节点"（入度为 0）中选优先级最小的节点输出，
        保证在多个合法拓扑序中选出最优的一个。

        使用 counter 作为 tiebreaker，避免 priority 相同时比较 node_id 出错。
        """
        in_degree = {n: G.in_degree(n) for n in G.nodes()}
        counter = itertools.count()   # 全局单调递增，保证 heap tuple 可比较
        heap: List[Tuple] = []

        for node, deg in in_degree.items():
            if deg == 0:
                heapq.heappush(heap, (priority[node], next(counter), node))

        result: List[str] = []
        while heap:
            _, _, node = heapq.heappop(heap)
            result.append(node)
            for successor in G.successors(node):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    heapq.heappush(heap, (priority[successor], next(counter), successor))

        # 防御：若有剩余节点（理论上 _build_dag 已保证无环，此处兜底）
        if len(result) < G.number_of_nodes():
            visited = set(result)
            remaining = sorted(
                [n for n in G.nodes() if n not in visited],
                key=lambda n: priority.get(n, (999, 0, '', 0)),
            )
            result.extend(remaining)

        return result

    def _build_execution_layers(
        self,
        layer_map: Dict[str, int],
        hunk_map: Dict[str, Hunk],
    ) -> List[List[Hunk]]:
        """
        将 layer_map 转换为分层的 Hunk 列表。

        Layer 0：最先执行（无前置依赖）
        Layer N：依赖 Layer < N 的所有节点

        层内按文件路径 + 行号排序，保持可读性。
        """
        if not layer_map:
            return []

        max_layer = max(layer_map.values())
        layers: List[List[Hunk]] = [[] for _ in range(max_layer + 1)]

        for node, layer_idx in layer_map.items():
            if node in hunk_map:
                layers[layer_idx].append(hunk_map[node])

        for layer in layers:
            layer.sort(key=lambda h: (h.file_path, h.start_line))

        return layers

    # ------------------------------------------------------------------ #
    #  私有：链路提取
    # ------------------------------------------------------------------ #

    def _extract_chains_from_graph(
        self,
        G: nx.DiGraph,
        hunk_map: Dict[str, Hunk],
        original_edges: List[Dict[str, Any]],
        max_paths: int = 5,
        max_depth: int = 4,
    ) -> List[str]:
        """
        从已构建的图中提取可读依赖链。

        注意：图内边是 B→A（B先执行），入度为 0 的节点是最先执行的节点，
             也是链路展示的起点（最基础的被依赖方）。
        """
        chains_display: List[str] = []

        # 起点：入度为 0 的节点（最先执行，链路的起点）
        sources = [n for n in G.nodes() if G.in_degree(n) == 0]

        # 兜底：全是环时，每个 SCC 取出度最大的节点（影响最广的节点）作为起点
        if not sources and G.number_of_nodes() > 0:
            sources = [
                max(scc, key=lambda n: G.out_degree(n))
                for scc in nx.strongly_connected_components(G)
            ]

        for source in sources:
            if len(chains_display) >= max_paths:
                break
            best_path = self._find_best_path(G, source, max_depth)
            if len(best_path) > 1:
                chains_display.append(
                    self._format_chain(best_path, G, hunk_map)
                )

        # 兜底：无长链时（全孤立节点），列出权重最小的直接依赖
        if not chains_display and original_edges:
            sorted_edges = sorted(
                original_edges,
                key=lambda x: x.get('weight', float('inf')),
            )
            for edge in sorted_edges[:max_paths]:
                u = edge.get('source')
                v = edge.get('target')
                if u in hunk_map and v in hunk_map:
                    # 展示原始语义：A 依赖 B，即 B → A
                    chains_display.append(
                        f"{self._hunk_label(hunk_map[v])} -> {self._hunk_label(hunk_map[u])}"
                    )

        return chains_display

    def _find_best_path(
        self,
        G: nx.DiGraph,
        source: str,
        max_depth: int = 4,
    ) -> List[str]:
        """
        从 source 出发，DFS 找到最长路径（贪心：优先走权重最小的边）。

        - 所有分支全部压栈（不 break），保证找到真正的最长路径
        - path 记录已访问节点，防止环路死循环
        - 深度上限 max_depth，防止路径过长导致 Prompt 膨胀
        """
        best_path: List[str] = [source]
        stack: List[Tuple[str, List[str]]] = [(source, [source])]

        while stack:
            curr, path = stack.pop()

            if len(path) > len(best_path):
                best_path = path

            if len(path) >= max_depth:
                continue

            # 过滤已访问节点（防环），按边权重升序（紧密依赖优先）
            neighbors = sorted(
                [n for n in G.successors(curr) if n not in path],
                key=lambda n: G[curr][n].get('weight', float('inf')),
            )
            for neighbor in neighbors:
                stack.append((neighbor, path + [neighbor]))

        return best_path

    def _format_chain(
        self,
        path: List[str],
        G: nx.DiGraph,
        hunk_map: Dict[str, Hunk],
    ) -> str:
        """
        将路径节点列表格式化为可读字符串。

        路径方向：沿图内边方向（B→A），即先执行的在前，后执行的在后。
        格式：FileB.py:50 --[calls]--> FileA.py:10
        """
        parts: List[str] = []
        for i, node in enumerate(path):
            hunk = hunk_map[node]
            label = self._hunk_label(hunk)
            if i == 0:
                parts.append(label)
            else:
                prev = path[i - 1]
                edge_data = G.get_edge_data(prev, node) or {}
                etype = edge_data.get('label', 'dep')
                etype = 'calls' if etype == 'dependency' else etype
                parts.append(f"--[{etype}]--> {label}")

        return " ".join(parts)

    @staticmethod
    def _hunk_label(hunk: Hunk) -> str:
        """生成 Hunk 的简短可读标签，格式：filename.py:行号"""
        filename = hunk.file_path.split('/')[-1]
        return f"{filename}:{hunk.start_line}"