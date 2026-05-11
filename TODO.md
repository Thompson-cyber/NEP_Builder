# TODO

## 任务 1：克隆仓库

从 `top_github_repos_code_only.xlsx` 中读取仓库列表，将所有仓库克隆到本地。

**步骤：**
- [ ] 解析 xlsx 文件，提取仓库 URL 列表
- [ ] 按语言分类存入 `repos/{language}/` 目录
- [ ] 使用 `git clone` 克隆每个仓库

---

## 任务 2：运行流水线收集数据

对任务 1 克隆的所有仓库，依次运行三个阶段收集数据。

### Phase 1 + 2

```bash
python batch_run_stage1\&2.py --repos-base <repos_dir> --output-base <output_dir>
```

- [ ] 配置 `REPOS_BASE` 指向任务 1 的克隆目录
- [ ] 配置 `OUTPUT_BASE` 指向输出目录
- [ ] 运行批量脚本，检查输出中的 `_phase2_no_graph.jsonl` 文件

### Phase 3

对每个仓库的 Phase 2 输出，调用 LLM 分析：

```bash
python stage3_llm_analysis.py \
    --input <output_dir>/<lang>/<repo>/<repo>_<date>_phase2_no_graph.jsonl \
    --output ./dataset \
    --repo-name <repo>
```

- [ ] 收集所有成功的 `_phase2_no_graph.jsonl` 文件路径
- [ ] 逐仓库运行 Phase 3
- [ ] 确认 `_phase3_results.jsonl` 输出正常

---

## 任务 3：扩充更多语言

### 目标

扩充 JavaScript,Rust, C, C++。每个语言筛选 GitHub 上 star 数前 30 的仓库（排除教程类仓库）。

### 筛选标准

1. 通过 GitHub 的 language 标签筛选仓库
2. 按 star 数降序排列，取 top 30
3. **排除**以下类型的仓库：
   - 教程 / 学习指南（tutorial, learn, course, guide, example, demo, awesome, cheat-sheet）
   - 文档仓库（docs, documentation）
   - 个人配置仓库（dotfiles, config）
   - 列表/集合类仓库（awesome-list, resources, awesome-*）


### 扩充步骤

- [ ] 通过 GitHub API 搜索各语言 top 30 仓库（按 stars 排序）
- [ ] 人工审核排除教程类仓库
- [ ] 将仓库加入 `batch_run_stage1&2.py` 的 `REPOS` 列表
- [ ] 在 `top_github_repos_code_only.xlsx` 中补充新仓库
- [ ] 执行任务 2 的流水线收集数据
