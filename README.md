## 完整字段字典

### 元信息字段

| 字段名 | 类型 | 描述 |
|---|---|---|
| `sample_id` | str | MD5 唯一标识，由 repo+commit+anchor+target 生成 |
| `repo` | str | 仓库全名，如 `apache/dubbo` |
| `commit_sha` | str | commit hash 前8位 |
| `commit_message` | str | 原始 commit message，截断至200字符 |
| `commit_date` | str | 提交日期 `YYYY-MM-DD` |
| `language` | str | 主语言 `Java / Python / TypeScript` |
| `is_negative` | bool | 是否为困难负样本 |
| `quality_score` | float | 综合质量分 0-100 |
| `quality_tier` | str | 质量等级 `high / medium / low` |
| `score_breakdown` | dict | 各维度得分明细 |

---

### 锚点字段

> 锚点 = 已知发生变更的文件，是模型的**核心输入信号**

| 字段名                        | 类型 | 描述                                             |
|----------------------------|---|------------------------------------------------|
| `anchor_file`              | str | 锚点文件相对路径，如 `src/main/service/UserService.java` |
| `anchor_diff`              | str | 锚点文件的原始 git diff 字符串，包含 hunk header            |
| `anchor_diff_size`         | int | 锚点 diff 中新增+删除的总行数                             |
| `anchor_content_before`    | str | 锚点文件**修改前**完整内容，超过512行则截断至变更区域±100行            |
| `anchor_context`           | str | 锚点文件依赖的上下文(有不是目标文件位置的)                         
| `anchor_required_context`  | str | 锚点文件必须依赖依赖的上下文(准确的目标文件位置)                      |
| `anchor_content_after(可选)` | str | 锚点文件**修改后**完整内容，同上截断策略                         |

---

### 目标文件字段，这里应该是一个List，并且按照hunk分割

> 目标 = 需要模型预测「是否需要改、改哪里、改成什么」的文件

| 字段名                      | 类型 | 描述                                 |
|--------------------------|---|------------------------------------|
| `target_file`            | str | 目标文件相对路径                           |
| `target_diff`            | str | 目标文件的原始 git diff 字符串，用于重建所有衍生字段    |
| `target_diff_size`       | int | 目标 diff 中新增+删除的总行数                 |
| `start_line`             | int | 目标hunk修改的起始行                       
| `end_line`               | int | 目标hunk修改的终止行                       |
| `target_content_before`  | str | 目标文件**修改前**完整内容，这是阶段2和阶段3的**直接输入** |
| `target_content_after`   | str | 目标文件**修改后**完整内容，用于阶段3的结果验证和评估      |
| `target_content_context` | str | 目标文件前的上下文                          |

---

### 行级别定位字段

> 阶段2的**输出标签**，同时作为阶段3的**输入**

| 字段名 | 类型 | 描述 |
|---|---|---|
| `changed_lines` | list[list[int]] | 需要修改的行范围列表，格式 `[[start, end], ...]`，行号从1开始 |
| `change_types` | list[str] | 与 `changed_lines` 一一对应的变更类型，取值 `MODIFY / ADD / DELETE` |

**示例**：
```python
"changed_lines": [[43, 45], [78, 78]],
"change_types":  ["MODIFY", "ADD"]
# 含义：第43-45行需要修改，第78行需要新增内容
```

---

### 结构化编辑操作字段

> 阶段3的**直接输出标签**，比 diff 字符串更易于模型学习

| 字段名 | 类型 | 描述 |
|---|---|---|
| `code_edits` | list[dict] | 结构化编辑操作列表，每条对应一个 hunk |
| `code_edits[].edit_type` | str | 操作类型：`REPLACE`（改）/ `INSERT`（增）/ `DELETE`（删） |
| `code_edits[].start_line` | int | 操作起始行号（基于修改前文件） |
| `code_edits[].end_line` | int | 操作结束行号（基于修改前文件） |
| `code_edits[].old_code` | str | 修改前的代码片段，`INSERT` 时为空字符串 |
| `code_edits[].new_code` | str | 修改后的代码片段，`DELETE` 时为空字符串 |

**示例**：
```python
"code_edits": [
    {
        "edit_type":  "REPLACE",
        "start_line": 43,
        "end_line":   45,
        "old_code":   "public User getUserById(int id) {\n    return repo.find(id);\n}",
        "new_code":   "public User getUserById(String id) {\n    return repo.find(id);\n}"
    },
    {
        "edit_type":  "INSERT",
        "start_line": 78,
        "end_line":   78,
        "old_code":   "",
        "new_code":   "if (id == null) throw new IllegalArgumentException(\"id cannot be null\");"
    }
]
```

---

### 局部上下文窗口字段

> 阶段3补全器的关键输入：让模型看到定位行的**局部代码环境**，而不是整个文件

| 字段名 | 类型 | 描述 |
|---|---|---|
| `location_contexts` | list[dict] | 每个定位区域对应一个上下文窗口，与 `changed_lines` 一一对应 |
| `location_contexts[].location_idx` | int | 对应 `changed_lines` 的下标 |
| `location_contexts[].start` | int | 定位区域起始行 |
| `location_contexts[].end` | int | 定位区域结束行 |
| `location_contexts[].change_type` | str | 该区域的变更类型 |
| `location_contexts[].context_before` | str | 定位行**之前** 30 行代码，带行号前缀，格式 `"L42: public class..."` |
| `location_contexts[].context_after` | str | 定位行**之后** 30 行代码，带行号前缀 |
| `location_contexts[].focal_lines_before` | str | 定位行区域**修改前**的代码，带行号，这是阶段3的**局部输入** |
| `location_contexts[].focal_lines_after` | str | 定位行区域**修改后**的代码，带行号，这是阶段3的**局部标签** |

---

### 跨文件依赖字段

> 帮助模型理解「为什么这两个文件会共同变更」，两个阶段都使用

| 字段名 | 类型 | 描述 |
|---|---|---|
| `repo_context` | dict | 仓库级别的跨文件语义关系 |
| `repo_context.anchor_imports_target` | bool | anchor 文件是否静态 import 了 target 文件中的符号 |
| `repo_context.target_imports_anchor` | bool | target 文件是否静态 import 了 anchor 文件中的符号 |
| `repo_context.shared_package` | bool | 两个文件是否属于同一个包或模块（Java package / Python module） |
| `repo_context.dependency_type` | str | 最强依赖关系类型：`CALLS`（调用）/ `INHERITS`（继承）/ `IMPLEMENTS`（实现接口）/ `USES_TYPE`（使用类型）/ `UNKNOWN` |
| `repo_context.path_distance` | int | 两个文件在目录树中的距离，0 = 同目录，1 = 相差一级，以此类推 |
| `repo_context.common_path_prefix` | str | 最长公共路径前缀，如 `src/main/service/` |
| `repo_context.shared_identifiers` | list[str] | anchor diff 和 target diff 中共同出现的标识符列表，是语义相关性的直接证据 |

---

---

## 各阶段输入输出完整定义

---

### 阶段 0 — 数据准备

```
输入
└── 本地 Git 仓库（裸仓库或完整仓库）

输出（每条样本包含的字段）
├── 全部元信息字段
├── 全部锚点字段
├── 全部目标文件字段
├── 全部行级别定位字段        ← 作为阶段2的训练标签
├── 全部结构化编辑操作字段    ← 作为阶段3的训练标签
├── 全部局部上下文窗口字段    ← 作为阶段3的输入特征
└── 全部跨文件依赖字段        ← 两个阶段共用的辅助特征
```

---

### 阶段 1 — 数据验证与划分

```
输入
└── 阶段0输出的完整 JSONL 文件

输出
├── train.jsonl   80%  high + medium 样本，按仓库维度划分防止泄露
├── val.jsonl     10%  high + medium 样本
└── test.jsonl    10%  仅 high 样本，用于最终评估
```

---

### 阶段 2 — 行级别定位器（微调 Qwen3-7B）

**训练时**：
```
输入 Prompt
├── [INST] commit_message
├── [ANCHOR_PATH] anchor_file
├── [ANCHOR_DIFF] anchor_diff
├── [ANCHOR_CODE] anchor_content_after     # 锚点改完后长什么样
├── [TARGET_PATH] target_file
├── [TARGET_CODE] target_content_before    # 目标文件现在长什么样（带行号）
└── [RELATION] repo_context                # 两文件的关系描述

输出标签
└── JSON 格式的 changed_lines + change_types
    {"locations": [{"start": 43, "end": 45, "type": "MODIFY"}, ...]}
```

**推理时**：
```
输入  同上（target_content_before 是真实文件当前状态）
输出  预测的需要修改的行范围，传递给阶段3
```

**评估指标**：
```
├── 行级别 Precision / Recall / F1
├── 完全匹配率（预测行范围与真实行范围完全一致）
└── 区间 IoU（预测区间与真实区间的交并比）
```

---

### 阶段 3 — 代码补全器（微调 Qwen3-7B）

**训练时**：
```
输入 Prompt
├── [INST] commit_message
├── [ANCHOR_PATH] anchor_file
├── [ANCHOR_DIFF] anchor_diff
├── [ANCHOR_CODE] anchor_content_after
├── [TARGET_PATH] target_file
├── [LOCATIONS] changed_lines + change_types    # 来自阶段2的真实标签（Teacher Forcing）
├── [CONTEXT] location_contexts[]               # 每个定位区域的局部上下文窗口
│   ├── context_before                          # 定位行前30行
│   ├── focal_lines_before                      # 待修改行（修改前）
│   └── context_after                           # 定位行后30行
└── [RELATION] repo_context

输出标签
└── code_edits（结构化编辑操作列表）
    [{"edit_type": "REPLACE", "start_line": 43, "end_line": 45,
      "old_code": "...", "new_code": "..."}, ...]
```

**推理时**：
```
输入  同上，但 [LOCATIONS] 来自阶段2的预测结果（非真实标签）
输出  code_edits，将其 apply 到 target_content_before 上得到最终修改后文件
验证  apply 后的文件内容与 target_content_after 做 exact match / edit distance 对比
```

**评估指标**：
```
├── CodeBLEU                      # 代码语义相似度
├── Exact Match Rate              # 与 target_content_after 完全一致的比例
├── Edit Distance                 # 字符级别编辑距离
└── Compilable Rate               # 修改后代码是否能通过语法检查（AST valid）
```

---

## 字段使用矩阵

| 字段 | 阶段2输入 | 阶段2标签 | 阶段3输入 | 阶段3标签 | 阶段3验证 |
|---|:---:|:---:|:---:|:---:|:---:|
| `commit_message` | ✅ | | ✅ | | |
| `anchor_file` | ✅ | | ✅ | | |
| `anchor_diff` | ✅ | | ✅ | | |
| `anchor_content_after` | ✅ | | ✅ | | |
| `target_file` | ✅ | | ✅ | | |
| `target_content_before` | ✅ | | ✅ | | |
| `target_content_after` | | | | | ✅ |
| `repo_context` | ✅ | | ✅ | | |
| `changed_lines` | | ✅ | ✅ | | |
| `change_types` | | ✅ | ✅ | | |
| `location_contexts` | | | ✅ | | |
| `code_edits` | | | | ✅ | |