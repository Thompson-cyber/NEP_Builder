# NEP Benchmark Pipeline

> **N**ext-**E**dit **P**rediction — 从 Git 仓库自动构建代码变更因果排序数据集。

给定一个 Git 仓库，流水线自动完成：
1. **挖掘**同时包含源码修改与测试修改的高质量 Commit
2. **分析**每个 Commit 的 Diff Hunk 及其静态依赖关系
3. **排序**：调用 LLM 识别根因 Hunk，建模修改传播顺序

最终输出可直接用于训练/评测的 `trigger_edit → ground_truth` 数据对。

---

## 目录结构

```
.
├── stage1_collect_commits.py          # Phase 1 入口：Commit 挖掘
├── stage2_call_graph_analysis.py          # Phase 2 入口：静态分析（支持断点续跑）
├── stage3_llm_analysis.py         # Phase 2b 入口：LLM 因果排序
│
├── config/
│   └── settings.py         # MiningConfig — 所有阈值与开关
│
├── filters/
│   ├── base.py             # BaseFilter 抽象基类
│   └── benchmark_filters.py# BenchmarkFilter — Phase 1 多维过滤
│
├── mining/
│   └── miner.py            # RepoMiner — 遍历 & 提取 CommitCandidate
│
├── core/
│   ├── types.py            # 全局数据模型（Pydantic）
│   └── llm_ranker.py       # LLMCausalRanker + CausalDatasetExporter
│
└── analysis/
    ├── processor.py        # CommitProcessor — Phase 2 主处理器
    ├── slicer.py           # DiffSlicer — Hunk 切片
    ├── graph.py            # DependencyGraph — 拓扑排序
    └── graph_analyzer.py   # GraphDependencyAnalyzerWrapper — AST 依赖分析
```

---

## 快速开始

### 1. 安装依赖

```bash
pip install pydriller loguru tqdm pydantic openai python-dotenv
```

### 2. 配置环境变量

在项目根目录创建 `.env`：

```env
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.deepseek.com   # 或其他兼容 OpenAI 接口的服务
LLM_MODEL=deepseek-reasoner
LLM_MAX_DIFF_LINES=5000
```

### 3. 运行流水线

```bash
# Phase 1 — 挖掘候选 Commit
python stage1_collect_commits.py \
    --repo      /path/to/your/repo \
    --repo_name pandas \
    --output    output/pandas/candidates.jsonl \
    --limit     500

# Phase 2 — 静态分析（支持 Ctrl+C 后断点续跑）
python stage2_call_graph_analysis.py \
    --input     output/pandas/candidates.jsonl \
    --output    output/pandas/analyzed.jsonl \
    --repo_path /path/to/your/repo

# Phase 2b — LLM 因果排序
python stage3_llm_analysis.py \
    --input     output/pandas/analyzed.jsonl \
    --old_format_output     output/pandas/old_analyzed.jsonl \
    --output    output/pandas/final_dataset.jsonl \
    --error_log output/pandas/llm_failures.jsonl
```

---

## 流水线说明

### Phase 1 — Commit 挖掘

使用 `pydriller` 遍历仓库历史，通过 `BenchmarkFilter` 过滤出满足以下**全部条件**的 Commit：

| 条件 | 说明 |
|------|------|
| 无 ADD / DELETE 文件 | 只关注 MODIFY 类型变更 |
| 必须有新增行 | 每个修改文件都有实际新增代码 |
| 必须含测试文件 | 匹配 `TEST_FILE_PATTERNS` |
| 必须含源码文件 | `.py/.java/.ts` 且不在忽略列表 |
| 源文件数量 | `MIN_SOURCE_FILES` ≤ count ≤ `MAX_SOURCE_FILES` |
| 源码行数变动 | `MIN_SOURCE_LOC` ≤ LOC ≤ `MAX_SOURCE_LOC` |
| 源码 Hunk 数量 | `MIN_SOURCE_HUNKS` ≤ hunks ≤ `MAX_SOURCE_HUNKS` |

**输出**：`CommitCandidate` JSONL，每行一个候选 Commit。

---

### Phase 2 — 静态分析

对每个 `CommitCandidate` 依次执行：

```
Diff 合并
    └─► DiffSlicer.slice()         → List[Hunk]
            └─► 分类 source / test hunks
                    └─► AST 依赖图（新状态 commit）
                    └─► AST 依赖图（旧状态 parent commit）
                            └─► CoChangeValidator 验证
                                    └─► DependencyGraph 拓扑排序
                                            └─► AnalyzedCommit
```

**过滤漏斗**（按顺序）：

1. Missing Source / Diff 为空
2. DiffSlicer 解析异常
3. 切片结果为空
4. Source Hunk 数量不足（< `MIN_SOURCE_HUNKS`）
5. AST 依赖图构建失败
6. 新旧依赖图均无边（`REQUIRE_DEPENDENCY=True`）
7. 存在孤立 Hunk（`NO_ISOLATED_HUNKS=True`）
8. 拓扑排序异常

**断点续跑**：中断后直接重新运行相同命令即可从上次位置继续；`--reset` 参数可强制重头开始。

**输出**：`AnalyzedCommit` JSONL，含拓扑排序后的 `ordered_hunks` 及依赖图信息。

---

### Phase 3 — LLM 因果排序

调用 LLM（默认 `deepseek-reasoner`）完成四项任务：

| 任务 | 说明 |
|------|------|
| 根因识别 | 找出触发本次变更的 Root Hunk |
| 需求一致性 | 判断所有 Hunk 是否协同解决同一需求 |
| 需求摘要 | 一句话描述本次变更的意图（≤ 20 词） |
| 修改顺序 | 建模从 Root 出发的变更传播链路 |

非单一需求或置信度 < 0.6 的 Commit 会被过滤丢弃。

**输出格式**（每行一条）：

```jsonc
{
  "commit_hash": "abc1234...",
  "commit_message": "Fix retry logic in HTTP client",
  "requirement_summary": "Add retry mechanism to handle transient network failures.",
  "trigger_edit": {          // Root Hunk（修改起点）
    "file_path": "src/client.py",
    "start_line": 42,
    "before_code": "...",
    "after_code": "..."
  },
  "ground_truth": [          // 后续 Hunk（有序，供评测）
    { "file_path": "...", "before_code": "...", "after_code": "..." }
  ],
  "llm_analysis": {
    "confidence": 0.92,
    "change_pattern": "Bug Fix",
    "hunk_order": [2, 0, 1]
  }
}
```

---

## 配置参考

所有参数集中在 `config/settings.py` 的 `MiningConfig` 类中，可通过修改类变量或 `.env` 文件调整。

### 规模阈值（`FLAG='Multi'` 模式）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MIN/MAX_SOURCE_LOC` | 3 / 20 | 源码行数变动范围 |
| `MIN/MAX_SOURCE_FILES` | 1 / 5 | 源码文件数量范围 |
| `MIN/MAX_SOURCE_HUNKS` | 2 / 5 | 源码 Hunk 数量范围 |

> 将 `FLAG` 改为 `'Single'` 可切换为单文件单 Hunk 模式（适合更简单的任务）。

### 行为开关

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `REQUIRE_DEPENDENCY` | `True` | 必须存在显式依赖边 |
| `NO_ISOLATED_HUNKS` | `True` | 不允许孤立 Hunk |
| `ALLOW_CYCLES` | `True` | 允许依赖环路 |
| `REQUIRE_TEST_CHANGE` | `True` | 必须包含测试文件修改 |

### 支持的文件类型

| 类型 | 扩展名 / 模式 |
|------|--------------|
| 源码 | `.py` `.java` `.ts` |
| 测试 | `test_*` `*_test.py` `tests/` `.spec.ts` `.test.ts` |
| 忽略 | `setup.py` `__init__.py` `conftest.py` `docs/conf.py` |

---

## 输出文件说明

| 文件 | 来源 | 说明 |
|------|------|------|
| `candidates.jsonl` | Phase 1 | 原始候选 Commit |
| `analyzed.jsonl` | Phase 2 | 含依赖图的分析结果 |
| `phase2_stats.json` | Phase 2 | 过滤漏斗统计数据 |
| `phase2_checkpoint.json` | Phase 2 | 断点续跑进度文件（完成后自动删除）|
| `final_dataset.jsonl` | Phase 2b | 最终训练/评测数据集 |
| `llm_failures.jsonl` | Phase 2b | LLM 处理失败的条目（供重试）|

---

## 注意事项

- **Phase 2 断点续跑**：中断后重新运行相同命令即可；若需重头开始请加 `--reset`。
- **LLM 费用**：Phase 2b 每个 Commit 调用一次 LLM，建议先在小数据集上测试。
- **仓库路径**：Phase 1 与 Phase 2 需传入**同一个**本地仓库路径，且该仓库需有完整 Git 历史。
- **Java / TypeScript 支持**：`SOURCE_EXTENSIONS` 已包含 `.java` 和 `.ts`，但 AST 分析器（`graph_analyzer.py`）需确认支持对应语言。