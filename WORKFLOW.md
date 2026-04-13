# NEP Dataset Builder — Phase 2 完整工作流程

## 一、总览

```
[INPUT]  candidates.jsonl  (Phase 1 产出的 CommitCandidate 列表)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  main.py  —  调度层                                          │
│  ① 断点恢复 (phase2_checkpoint.json)                         │
│  ② 初始化 CommitProcessor                                    │
│  ③ 逐行读取 → process_single_line()                          │
│  ④ 每 500 行 flush + save_checkpoint                         │
│  ⑤ KeyboardInterrupt → 保存进度，支持续跑                     │
│  ⑥ 完成 → 打印漏斗报告 + 写 phase2_stats.json                │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
[OUTPUT] analyzed_commits.jsonl  +  phase2_stats.json
```

---

## 二、CommitProcessor 七步流水线

### Step 1 · 数据提取
```
candidate.source_changes + candidate.test_changes
    │  遍历每个 FileChange.diff
    │  若 diff 不以 "diff --git" 开头 → 补全文件头
    ▼
raw_diff: str  （拼接后的完整 diff 文本）
```
> 若 raw_diff 为空 → 过滤：`missing_diff`

---

### Step 2 · DiffSlicer（`analysis/slicer.py`）
```
raw_diff
    │
    ├─ 正则匹配 "diff --git a/X b/Y"  → 提取 current_file
    ├─ 正则匹配 "@@ -old_start +new_start @@"  → 提取行号元数据
    │
    │  对每个 Hunk 的 lines：
    ├─ 找 first_change_idx / last_change_idx（首尾变更行）
    ├─ 计算 prefix_context_count（首部上下文行数）
    ├─ 裁剪：trimmed_lines = lines[first..last]
    ├─ 统计 calc_old_len / calc_new_len
    ├─ final_old_start = meta.old_start + prefix_context_count
    ├─ final_new_start = meta.new_start + prefix_context_count
    └─ hunk_id = "filepath:final_new_start"
    ▼
all_hunks: List[Hunk]
```
> 若 all_hunks 为空 → 过滤：`empty_hunks`
> 若抛出异常 → 过滤：`slicing_error`

---

### Step 3 · Hunk 分类
```
all_hunks
    │
    ├─ source_paths = {fc.new_path, fc.old_path for fc in source_changes}
    │
    ├─ source_hunks = [h for h if h.file_path in source_paths]
    └─ test_hunks   = [h for h if h.file_path NOT in source_paths]
```
> 若 len(source_hunks) < MIN_SOURCE_HUNKS → 过滤：`too_few_source_hunks`

---

### Step 4.1 · New State 依赖分析
```
GraphDependencyAnalyzerWrapper(repo_path, commit_hash, mode='new')
    │
    ├─ [prepare_sandbox_repo]
    │   git clone source_repo → sandboxes/{commit_hash}_new/
    │   git checkout -f {commit_hash}
    │
    ├─ [HunkDependencyAnalyzer]
    │   ├─ 检查 graph_cache/graph_{commit_hash}.pkl
    │   │   命中 → 直接 pickle.load
    │   │   未命中 → build_graph(sandbox_path) → pickle.dump
    │   │
    │   └─ [build_graph]  ← 核心图构建（见第三节）
    │
    ├─ [cleanup_sandbox]  删除沙盒目录（图已缓存）
    │
    └─ [analyze(source_hunks, mode='new')]
        ├─ 提取有效 Hunk：new_start_line > 0
        ├─ 每个 Hunk → get_nodes_by_hunk(file, start, end)
        │   对 hunk 内每一行：找包含该行的最小跨度节点（最具体）
        │   优先级：function < class < file（跨度越小越优先）
        ├─ 两两 Hunk 计算最短路径（nx.shortest_path）
        │   同节点集合有交集 → weight=0, type="same_node"
        │   有路径 → weight=hops, type="dependency"
        │   跨文件路径 → cross_file_count++
        └─ 返回 (new_edges, new_metrics, [], valid_hunks_new)
            new_metrics = {n_edges, all_hunks_connected,
                           cross_file_dependencies, max_hops}
```
> 若抛出异常 → 过滤：`analysis_error`

---

### Step 4.2 · Old State 依赖分析
```
git rev-parse {commit_hash}^  → parent_hash
    │
GraphDependencyAnalyzerWrapper(repo_path, parent_hash, mode='old')
    │  （流程同 Step 4.1，checkout 目标改为 parent_hash）
    │  analyze(source_hunks, mode='old')
    │  使用 old_start_line / old_len 计算行号范围
    ▼
old_edges, old_metrics, old_chains
```
> 失败仅 warning，不中断流程

---

### Step 5 · CoChangeValidator 验证
```
validate(valid_hunks_new, new_metrics, old_metrics)
    │
    ├─ len(source_hunks) < MIN_SOURCE_HUNKS → "too_few_source_hunks"
    ├─ len(source_hunks) > MAX_SOURCE_HUNKS → "too_more_source_hunks"
    │
    ├─ REQUIRE_DEPENDENCY=True：
    │   new_metrics.n_edges==0 AND old_metrics.n_edges==0
    │   → "no_dependencies_in_either"
    │
    └─ NO_ISOLATED_HUNKS=True：
        NOT new_metrics.all_hunks_connected
        AND NOT old_metrics.all_hunks_connected
        → "has_isolated_hunks"
```
> 验证失败 → 过滤：`validation_{reason}`

---

### Step 6 · DependencyGraph 拓扑排序（`analysis/graph.py`）
```
sort(valid_hunks_new, new_edges)
    │
    ├─ [_build_dag]
    │   ① 构建有向图：edge(A→B) 表示"A依赖B"
    │      存图时反转：add_edge(B, A)，使 B 入度为0先输出
    │   ② 检测环：nx.strongly_connected_components
    │      有环 → 删除 SCC 内权重最大的边（最弱依赖）
    │      循环直到 DAG
    │
    ├─ [_compute_layer_map]
    │   拓扑序遍历：layer[A] = max(layer[B] for B∈predecessors) + 1
    │
    ├─ [_compute_priority]  四维优先级 tuple：
    │   (layer, -out_degree, file_path, start_line)
    │
    ├─ [_kahn_sort]  优先队列 Kahn 算法
    │   每次从就绪节点（入度=0）中取优先级最小者输出
    │
    └─ 返回 (sorted_hunks, edges_debug, has_cycle)
```
> 若抛出异常 → 过滤：`sorting_error`

---

### Step 7 · 构造 AnalyzedCommit
```
AnalyzedCommit(
    hash, repo, msg,
    ordered_hunks    = sorted_hunks,       # 拓扑排序结果
    test_hunks       = test_hunks,
    dependencies     = edges_debug,        # new state 边
    dependency_chains = new_chains,
    old_dependencies  = old_edges,
    old_dependency_chains = old_chains,
    old_metrics / new_metrics,
    dependency_label = "BOTH"|"NEW_ONLY"|"OLD_ONLY"|"NONE"
)
```

---

## 三、build_graph 图构建详解（`build_graph.py`）

```
build_graph(repo_path, fuzzy_search=True, global_import=True)
    │
    ├─ [节点构建阶段]
    │   os.walk(repo_path)
    │   ├─ 跳过 .git / .github 目录
    │   ├─ 添加 directory 节点（不含 .py 文件的目录事后删除）
    │   └─ 对每个 .py 文件：
    │       ├─ 添加 file 节点（存储完整源码）
    │       ├─ CodeAnalyzer(ast.NodeVisitor) 解析 AST：
    │       │   ├─ visit_ClassDef  → class 节点（含 start/end_line）
    │       │   ├─ visit_FunctionDef → function 节点（跳过 __init__）
    │       │   └─ visit_AsyncFunctionDef → function 节点
    │       └─ 添加 contains 边：
    │           dir → file → class/function → 嵌套 class/function
    │
    ├─ [Import 边构建阶段]
    │   find_imports(filepath)：
    │   ├─ "import X"      → EDGE_TYPE_IMPORTS: file → file
    │   ├─ "from X import Y" → 尝试解析为子模块或具体实体
    │   └─ 相对导入 → 根据文件路径计算绝对模块名
    │
    └─ [调用/继承边构建阶段]
        对每个 class / function 节点：
        ├─ find_all_possible_callee(node, graph)
        │   沿 contains 边向上找到 file 节点
        │   收集所有可见的 class/function（含 import 引入的）
        │   fuzzy_search=True → 同名节点全部保留
        │
        ├─ analyze_init (class节点)：
        │   解析 __init__ 方法中的调用 → EDGE_TYPE_INVOKES
        │   解析 bases 继承关系 → EDGE_TYPE_INHERITS
        │
        └─ analyze_invokes (function节点)：
            traverse_call() 递归遍历函数体
            跳过内嵌函数/类定义
            → EDGE_TYPE_INVOKES

图节点格式：
    "src/utils.py"                    → FILE
    "src/utils.py:MyClass"            → CLASS
    "src/utils.py:MyClass.my_method"  → FUNCTION

图边类型：
    contains  → 包含关系（目录→文件→类→方法）
    imports   → 导入关系（文件/类/函数 → 文件/类/函数）
    invokes   → 调用关系（函数/类 → 函数/类）
    inherits  → 继承关系（类 → 类）
```

---

## 四、过滤漏斗汇总

| 阶段 | 过滤原因 | stats.filters 字段 |
|------|---------|-------------------|
| Step 1 | diff 为空 | missing_source |
| Step 2 | DiffSlicer 异常 | slicing_error |
| Step 2 | 切片结果为空 | slicing_empty |
| Step 3 | source_hunks 数量不足 | too_few_hunks |
| Step 4 | 图分析异常 | analysis_error |
| Step 5 | 无依赖边 | no_dependency |
| Step 5 | 其他 validation | validation_fail |
| Step 6 | 拓扑排序异常 | sorting_error |
| 未知 | 其他异常 | unknown |

---

## 五、关键配置参数（MiningConfig）

| 参数 | Single模式 | Multi模式 | 作用 |
|------|-----------|----------|------|
| MIN_SOURCE_HUNKS | 1 | 2 | 最少 Hunk 数 |
| MAX_SOURCE_HUNKS | 1 | 10 | 最多 Hunk 数 |
| REQUIRE_DEPENDENCY | True | True | 必须有依赖边 |
| NO_ISOLATED_HUNKS | True | True | 不允许孤立 Hunk |
| USE_LLM | False | False | LLM排序（当前禁用）|