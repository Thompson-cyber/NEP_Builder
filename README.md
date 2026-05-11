# NEP Pipeline 运行指南

## 简介

NEP (Neural Execution Prediction) Pipeline 分为三个阶段：

- **Phase 1**：收集 Commit 候选数据（`stage1_collect_commits`）
- **Phase 2**：静态分析处理（`stage2_call_graph_analysis`，调用图已完全禁用）
- **Phase 3**：LLM 因果排序（`stage3_llm_analysis`）

---

## 环境准备

### 1. 安装依赖

```bash
pip install -r requirements.txt
# 或用 conda: conda env create -f environment.yml
```

### 2. 配置 `.env` 文件

在项目根目录创建 `.env` 文件，填入以下内容：

```ini
# LLM API 密钥
LLM_API_KEY=your_api_key_here

# API 服务地址（支持兼容 OpenAI 接口的服务）
LLM_BASE_URL=https://api.deepseek.com

# 使用的模型名称
LLM_MODEL=deepseek-reasoner

# Diff 最大行数限制
LLM_MAX_DIFF_LINES=5000
```

---

## 快速开始

### 完整流程（三个阶段）

```bash
# 1. Phase 1 + Phase 2：批量挖掘和分析
python batch_run_stage1\&2.py

# 2. Phase 3：LLM 因果排序（基于 Phase 2 的输出）
python stage3_llm_analysis.py \
    --input /path/to/output/xxxx_phase2_no_graph.jsonl \
    --output ./dataset \
    --repo-name <repo_name>
```

---

## Phase 1 + 2：批量运行

### 目录结构要求

```
REPOS_BASE/
├── Python/
│   ├── superset/
│   ├── flask/
│   └── ...
├── Go/
│   ├── minio/
│   └── ...
├── Java/
├── TypeScript/
└── ...

OUTPUT_BASE/
├── Python/
│   ├── flask/
│   │   ├── flask_2026-04-30_phase1_candidates.jsonl
│   │   └── flask_2026-04-30_phase2_no_graph.jsonl
│   └── ...
└── ...
```

### 配置路径

在 `batch_run_stage1&2.py` 顶部修改路径常量：

```python
REPOS_BASE  = r"D:\Data\...\repos"    # 仓库根目录
OUTPUT_BASE = r"D:\Data\...\outputs"  # 输出根目录
```

### 批量运行命令

```bash
# 全量运行（所有仓库）
python batch_run_stage1\&2.py

# 只跑某个语言
python batch_run_stage1\&2.py --lang Python

# 只跑某个仓库
python batch_run_stage1\&2.py --repo-name flask

# 跳过 Phase 1，只跑 Phase 2
python batch_run_stage1\&2.py --skip-phase1

# Phase 2 忽略断点重跑
python batch_run_stage1\&2.py --reset

# 组合使用
python batch_run_stage1\&2.py --lang Go --skip-phase1
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--repos-base` | 配置中的 `REPOS_BASE` | 仓库根目录路径 |
| `--output-base` | 配置中的 `OUTPUT_BASE` | 输出根目录路径 |
| `--lang` | `None`（全部） | 只处理指定语言，可选：`Python` / `Go` / `Java` / `TypeScript` |
| `--repo-name` | `None`（全部） | 只处理指定仓库名，如 `flask` |
| `--skip-phase1` | `False` | 跳过 Phase 1，直接使用已有候选文件 |
| `--reset` | `False` | Phase 2 忽略断点，从头重新运行 |

### 输出文件

```
{OUTPUT_BASE}/{lang}/{repo_name}/
├── {repo_name}_{date}_phase1_candidates.jsonl    # Phase 1 输出
└── {repo_name}_{date}_phase2_no_graph.jsonl      # Phase 2 输出
```

---

## Phase 3：LLM 因果排序

Phase 3 基于 Phase 2 的输出结果，使用 LLM 对每个提交进行两阶段分析：
1. **Stage 1 — 质量门控**：过滤低质量提交（纯格式化、机械重命名、多需求混杂）
2. **Stage 2 — 根因识别**：识别根因 Hunk 并输出 Hunk 修改顺序

### 运行命令

```bash
# 基本用法
python stage3_llm_analysis.py \
    --input output/flask_2026-04-30_phase2_no_graph.jsonl \
    --output ./dataset \
    --repo-name flask
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|:----:|------|
| `--input` | ✅ | Phase 2 输出的 JSONL 文件路径 |
| `--output` | ✅ | 输出目录路径 |
| `--repo-name` | ✅ | 仓库名称（用于命名输出文件） |

### 输出文件

```
{output}/
├── {repo-name}_{date}_phase3_results.jsonl       # LLM 分析成功的结果
├── {repo-name}_{date}_phase3_filtered.jsonl      # 含分析结果的过滤后数据
└── {repo-name}_{date}_phase3_errors.log          # 分析失败的条目（可重试）
```

### 注意事项

- 需要先在 `.env` 中配置有效的 `LLM_API_KEY`
- 遇到 API 限流（RateLimit / 429）会自动重试
- 支持断点续跑：重复执行同一命令会自动跳过已处理的提交

---

## 支持的语言

| 语言 | 扩展名 | Phase 1 | Phase 2 | Phase 3 |
|------|--------|:-------:|:-------:|:-------:|
| Python | `.py` | ✅ | ✅ | ✅ |
| Java | `.java` | ✅ | ✅ | ✅ |
| TypeScript | `.ts`, `.tsx` | ✅ | ✅ | ✅ |
| JavaScript | `.js`, `.jsx`, `.cjs`, `.mjs` | ✅ | ✅ | ✅ |
| Go | `.go` | ✅ | ✅ | ✅ |
| Rust | `.rs` | ✅ | ✅ | ✅ |
| C | `.c` | ✅ | ✅ | ✅ |
| C++ | `.cpp`, `.cxx`, `.cc`, `.hpp` | ✅ | ✅ | ✅ |

---

## 常见问题

**Q：提示"仓库目录不存在"？**
> 检查 `--repos-base` 路径及语言子目录是否正确，确保仓库已克隆到对应位置。

**Q：使用 `--skip-phase1` 报错"候选文件不存在"？**
> 需先完整运行一次 Phase 1 生成 `.jsonl` 候选文件，再使用该参数跳过。

**Q：Phase 2 中断后如何续跑？**
> 默认支持断点续跑，直接重新执行相同命令即可。若需强制重跑，添加 `--reset` 参数。

**Q：`.env` 文件配置后 API 仍无法调用？**
> 检查 `LLM_API_KEY` 是否正确填写，`LLM_BASE_URL` 是否可访问，以及项目是否已安装 `python-dotenv` 依赖以自动加载 `.env`。
