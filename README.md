# NEP Pipeline 批量运行指南

## 📖 简介

`batch_run.py` 用于批量对多个代码仓库运行 **NEP Pipeline**，支持两个阶段：

- **Phase 1**：收集 Commit 候选数据（`stage1_collect_commits`）
- **Phase 2**：LLM 分析处理（`stage2_call_graph_analysis`）

> ⚠️ **注意：当前版本必须使用 `--no-graph` 参数禁用调用图分析才能正常运行。**

---

## 🗂️ 目录结构要求

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
└── TypeScript/

OUTPUT_BASE/
├── Python/
│   ├── flask/
│   │   ├── flask_2026-04-30_phase1_candidates.jsonl
│   │   └── flask_2026-04-30_phase2_no_graph.jsonl
│   └── ...
└── ...
```

---

## ⚙️ 环境准备

### 1. 安装依赖

```bash
pip install loguru
# 以及 stage1_collect_commits、stage2_call_graph_analysis 所需依赖
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

> 💡 `LLM_BASE_URL` 可替换为其他兼容 OpenAI 接口的服务地址，例如 `https://api.openai.com`。

### 3. 修改路径配置（按需）

修改 `batch_run.py` 顶部的路径常量：

```python
REPOS_BASE  = r"D:\Data\...\repos"    # 仓库根目录
OUTPUT_BASE = r"D:\Data\...\outputs"  # 输出根目录
```

---

## 🚀 快速开始

> ⚠️ **当前版本所有命令均须添加 `--no-graph` 参数。**

### 全量运行（所有仓库）

```bash
python batch_run.py --no-graph
```

### 常用命令示例

| 场景 | 命令 |
|------|------|
| 全量运行 | `python batch_run.py --no-graph` |
| 跳过 Phase 1 | `python batch_run.py --no-graph --skip-phase1` |
| Phase 2 忽略断点重跑 | `python batch_run.py --no-graph --reset` |
| 只跑某个语言 | `python batch_run.py --no-graph --lang Python` |
| 只跑某个仓库 | `python batch_run.py --no-graph --repo-name flask` |
| 组合使用 | `python batch_run.py --no-graph --lang Go --skip-phase1` |

---

## 🔧 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--repos-base` | 配置中的 `REPOS_BASE` | 仓库根目录路径 |
| `--output-base` | 配置中的 `OUTPUT_BASE` | 输出根目录路径 |
| `--lang` | `None`（全部） | 只处理指定语言，可选：`Python` / `Go` / `Java` / `TypeScript` |
| `--repo-name` | `None`（全部） | 只处理指定仓库名，如 `flask` |
| `--no-graph` | `False` | ⚠️ **必须添加**，禁用调用图分析 |
| `--skip-phase1` | `False` | 跳过 Phase 1，直接使用已有候选文件 |
| `--reset` | `False` | Phase 2 忽略断点，从头重新运行 |

---

## 📦 支持的仓库列表

| 语言 | 仓库 |
|------|------|
| Python | superset, flask, ansible, OpenBB, localstack, keras, pathway |
| Go | minio, memos, rclone, gitea, etcd |
| Java | zxing, kafka, conductor, hutool, xxl-job |
| TypeScript | query, expo, prisma, react-hook-form, twenty |

---

## 📊 运行结果说明

运行结束后，终端会打印汇总信息：

```
============================================================
  汇总: ✅ 成功 N  ⊘ 跳过 N  ❌ 失败 N
============================================================
```

| 状态 | 说明 |
|------|------|
| ✅ 成功 | Phase 1 + Phase 2 均完成，输出 `.jsonl` 文件 |
| ⊘ 跳过 | 仓库目录不存在，或 Phase 1 收集到 0 个候选 |
| ❌ 失败 | 候选文件缺失，或运行时发生未捕获异常 |

### 输出文件命名规则

```
{OUTPUT_BASE}/{lang}/{repo_name}/
├── {repo_name}_{date}_phase1_candidates.jsonl    # Phase 1 输出
└── {repo_name}_{date}_phase2_no_graph.jsonl      # Phase 2 输出（no_graph 模式）
```

---

## ❓ 常见问题

**Q：不加 `--no-graph` 会怎样？**
> 调用图分析功能当前不可用，运行可能报错或产生异常结果，请务必添加 `--no-graph` 参数。

**Q：`.env` 文件配置后 API 仍无法调用？**
> 检查 `LLM_API_KEY` 是否正确填写，`LLM_BASE_URL` 是否可访问，以及项目是否已安装 `python-dotenv` 依赖以自动加载 `.env`。

**Q：提示"仓库目录不存在"？**
> 检查 `--repos-base` 路径及语言子目录是否正确，确保仓库已克隆到对应位置。

**Q：使用 `--skip-phase1` 报错"候选文件不存在"？**
> 需先完整运行一次 Phase 1 生成 `.jsonl` 候选文件，再使用该参数跳过。

**Q：Phase 2 中断后如何续跑？**
> 默认支持断点续跑，直接重新执行相同命令即可。若需强制重跑，添加 `--reset` 参数。