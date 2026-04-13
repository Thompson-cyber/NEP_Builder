import json
import os

import openai
from typing import List, Optional, Dict, Any
from loguru import logger
from core.types import AnalyzedCommit, Hunk, LLMAnalysisResult
from config.settings import MiningConfig


class LLMCausalRanker:
    """
    利用 LLM 结合 Issue、Commit Message 和静态依赖图数据，完成四项任务：
      1. 识别根因 Hunk（Root Cause Hunk）
      2. 判断所有 Hunks 是否协同解决同一个需求
      3. 若是同一需求，生成简短的需求摘要
      4. 建模其他 Hunks 的修改先后顺序（以 Root 为起点的传播链路）
    """

    def __init__(self, config: MiningConfig):
        self.client = openai.OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL
        )
        self.model = config.LLM_MODEL
        self.max_diff_lines = config.LLM_MAX_DIFF_LINES

    def rank_commit(self, commit: AnalyzedCommit) -> Optional[AnalyzedCommit]:
        """
        分析 commit，返回排序后的 AnalyzedCommit。
        若 LLM 判断不是单一需求，或置信度不足，返回 None。
        """

        # ── 预检查：单 Hunk 直接返回 ──────────────────────────────────
        if len(commit.ordered_hunks) == 1:
            commit.ordered_hunks[0].order_index = 0
            commit.causal_analysis = LLMAnalysisResult(
                root_hunk_id=commit.ordered_hunks[0].id,
                confidence=1.0,
                reasoning="Single hunk commit.",
                change_pattern="Unknown",
                is_single_requirement=True,
                requirement_summary=commit.msg,
                hunk_order=[0]
            )
            return commit

        # ── 构造 Prompt ───────────────────────────────────────────────
        prompt = self._build_prompt(commit)

        try:
            # ── 调用 LLM ─────────────────────────────────────────────
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a Senior Code Archaeologist. "
                            "Your task is to reconstruct the developer's thought process, "
                            "identify the Root Cause Hunk, judge requirement coherence, "
                            "generate a requirement summary, and model the hunk modification order."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )

            content = response.choices[0].message.content
            result_data = json.loads(content)

            # ── Task 2：非单一需求直接丢弃 ────────────────────────────
            is_single = result_data.get("is_single_requirement", False)
            if not is_single:
                logger.info(
                    f"Commit {commit.hash[:7]} discarded: not a single requirement. "
                    f"Reasoning: {result_data.get('reasoning', '')[:120]}"
                )
                return None

            # ── 解析其余字段 ──────────────────────────────────────────
            chosen_index = int(result_data.get("root_hunk_index", -1))
            hunk_order: List[int] = result_data.get("hunk_order", [])
            requirement_summary: Optional[str] = result_data.get("requirement_summary", None)
            confidence = float(result_data.get("confidence_score", 0.0))
            change_pattern = result_data.get("change_pattern", "Unknown")
            reasoning = result_data.get("reasoning", "")
            total = len(commit.ordered_hunks)

            # ── 校验 root_hunk_index ──────────────────────────────────
            if not (0 <= chosen_index < total):
                logger.warning(
                    f"LLM returned invalid root_hunk_index {chosen_index} "
                    f"for {commit.hash[:7]}, discarding."
                )
                return None

            # ── 校验 hunk_order 完整性 ────────────────────────────────
            if (
                    len(hunk_order) != total
                    or set(hunk_order) != set(range(total))
                    or hunk_order[0] != chosen_index
            ):
                logger.warning(
                    f"LLM returned invalid hunk_order {hunk_order} "
                    f"for {commit.hash[:7]}, falling back to root-first order."
                )
                # 降级：root 排第一，其余保持原顺序
                hunk_order = [chosen_index] + [i for i in range(total) if i != chosen_index]

            root_hunk = commit.ordered_hunks[chosen_index]

            # ── 保存 LLM 分析结果 ─────────────────────────────────────
            commit.causal_analysis = LLMAnalysisResult(
                root_hunk_id=root_hunk.id,
                confidence=confidence,
                reasoning=reasoning,
                change_pattern=change_pattern,
                is_single_requirement=True,
                requirement_summary=requirement_summary,
                hunk_order=hunk_order
            )

            # ── 按 hunk_order 重排 ordered_hunks ─────────────────────
            original_hunks = list(commit.ordered_hunks)
            commit.ordered_hunks = [original_hunks[i] for i in hunk_order]

            # 更新每个 hunk 的物理位置索引
            for position, hunk in enumerate(commit.ordered_hunks):
                hunk.order_index = position

            logger.info(
                f"[{commit.hash[:7]}] Root: Hunk#{chosen_index} ({root_hunk.id}) | "
                f"Order: {hunk_order} | Score: {confidence:.2f} | "
                f"Pattern: {change_pattern} | Summary: {requirement_summary}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"JSON parse failed for {commit.hash[:7]}: {e}")
            return None
        except Exception as e:
            logger.error(f"LLM analysis failed for {commit.hash[:7]}: {e}")
            return None

        return commit

    # ──────────────────────────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _format_dependency_chains(
            self, chains: List[Any], hunk_id_to_index: Dict[str, int]
    ) -> str:
        """将依赖链对象列表转换为 LLM 易读的文本描述。"""
        if not chains:
            return "No explicit static dependencies detected between these hunks."

        lines = []
        for chain in chains:
            src_idx = hunk_id_to_index.get(chain.source)
            tgt_idx = hunk_id_to_index.get(chain.target)
            if src_idx is not None and tgt_idx is not None:
                path_desc = (
                    " -> ".join(chain.path)
                    if isinstance(chain.path, list)
                    else str(chain.path)
                )
                lines.append(
                    f"- Hunk {src_idx} depends on Hunk {tgt_idx} (Path: {path_desc})"
                )

        return (
            "\n".join(lines)
            if lines
            else "No explicit static dependencies detected between these hunks."
        )

    def _build_prompt(self, commit: AnalyzedCommit) -> str:
        """
        构建发送给 LLM 的 Prompt，要求 LLM 完成四项任务：
          1. 识别根因 Hunk（Root Cause Hunk）
          2. 判断所有 Hunks 是否协同解决同一个需求
          3. 若是同一需求，生成简短的需求摘要
          4. 建模其他 Hunks 的修改先后顺序（以 Root 为起点的传播链路）
        """
        hunk_id_to_index = {h.id: i for i, h in enumerate(commit.ordered_hunks)}
        total_hunks = len(commit.ordered_hunks)
        issue_text = commit.issue_description if commit.issue_description else "N/A"
        dependency_text = self._format_dependency_chains(
            commit.dependency_chains, hunk_id_to_index
        )

        hunks_display = ""
        for idx, hunk in enumerate(commit.ordered_hunks):
            lines = hunk.content.split("\n")
            if len(lines) > self.max_diff_lines:
                content_snippet = (
                        "\n".join(lines[: self.max_diff_lines]) + "\n... (truncated)"
                )
            else:
                content_snippet = "\n".join(lines)

            hunks_display += f"""
---
[Candidate Hunk Index: {idx}]
File: {hunk.file_path} (Lines {hunk.start_line}-{hunk.end_line})
Code Diff:
```diff
{content_snippet}
```
"""

        prompt = f"""
## Goal
Analyze the following commit and complete FOUR tasks:
1. Identify the single "Root Cause" Hunk — the starting point of the change propagation.
2. Determine whether ALL hunks collaboratively solve ONE single requirement.
3. If they do, generate a concise requirement summary based on the commit message.
4. Model the modification order of ALL hunks (starting from the Root), reflecting the
   logical propagation sequence of the change (i.e., which hunk must be changed first
   to necessitate the next).

---

## Context

### Issue Description
{issue_text}

### Commit Message
{commit.msg}

### Static Dependency Graph (Extracted by AST)
The following call/import dependencies were detected between hunks.
"Hunk A depends on Hunk B" means A calls or imports B — B is more likely the Root.

{dependency_text}

### Candidate Hunks
{hunks_display}

---

## Reasoning Guidelines

### Task 1 — Root Cause Identification
Ask: which hunk introduces the *core intent* described in the Issue / Commit Message?
- **Refactoring**   : The hunk that changes the function/class *definition* is the Root.
- **New Feature**   : The hunk introducing the core logic is the Root; UI/CLI wiring is secondary.
- **Bug Fix**       : The hunk that corrects the logic error is the Root. Test hunks are NEVER the Root.
- **Config Change** : The hunk modifying the core config entry is the Root.
- **Enhancement**   : The hunk that extends the primary abstraction is the Root.

### Task 2 — Single-Requirement Coherence Check
Mark `is_single_requirement: true` only when ALL of the following hold:
- Every hunk serves one unified intent (same bug, same feature, same refactoring goal).
- No unrelated cleanup, hotfix, or refactoring hunks are mixed in.
- The dependency graph is connected or star-shaped (no isolated islands).
Mark `is_single_requirement: false` when ANY of the following is observed:
- Hunks touch completely unrelated modules with no dependency path between them.
- The commit message lists multiple unrelated items (e.g., "fix X; also refactor Y").
- Some hunks are pure whitespace / formatting changes unrelated to the feature.

### Task 3 — Requirement Summary
- Generate ONLY when `is_single_requirement` is true; otherwise output `null`.
- Write exactly ONE sentence, no more than 20 words.
- Focus on WHAT was changed and WHY — not HOW.
- Use plain English; avoid code symbols, file names, or variable names.
- Good example : "Add retry mechanism to HTTP client to handle transient network failures."
- Bad example  : "Modified `_retry_count` in `http_client.py` and updated `send()` method."

### Task 4 — Hunk Modification Order
- Output an ordered list of ALL hunk indices (0 to {total_hunks - 1}), including the Root.
- The Root hunk must appear FIRST (index 0 of the order list).
- Order the remaining hunks by their logical dependency on prior hunks:
    * A hunk that is directly called by / depends on the Root comes next.
    * A hunk that adapts to a change in a prior hunk comes after that prior hunk.
    * Test hunks and documentation hunks always come LAST.
- If two hunks have no dependency between them, order by file-level locality
  (hunks in the same file as an earlier hunk come before unrelated files).
- Every hunk index must appear exactly once.

---

## Output Format
Respond with a single JSON object — no markdown fences, no extra text.

{{
  "root_hunk_index"      : <int, 0 to {total_hunks - 1}>,
  "confidence_score"     : <float, 0.0 to 1.0>,
  "change_pattern"       : "Refactoring" | "New Feature" | "Bug Fix" | "Config Change" | "Enhancement",
  "is_single_requirement": <bool>,
  "requirement_summary"  : "<one-sentence summary>" | null,
  "hunk_order"           : [<int>, <int>, ...],
  "reasoning"            : "<concise explanation covering all four tasks, referencing dependency edges where relevant>"
}}
"""
        return prompt


# ══════════════════════════════════════════════════════════════════════════
# Exporter
# ══════════════════════════════════════════════════════════════════════════

class CausalDatasetExporter:
    """
    将分析后的 AnalyzedCommit 导出为训练/评测用的 JSONL 格式。
    ordered_hunks[0] 为 Root（trigger_edit），其余按 hunk_order 排列为 ground_truth。
    """

    def __init__(self, output_file: str):
        self.output_file = output_file
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

    def save_commit(self, commit: AnalyzedCommit) -> bool:
        """
        处理单个 Commit 并追加写入 JSONL 文件。
        返回 True 表示成功写入，False 表示被过滤跳过。
        """
        # ── 基础校验 ──────────────────────────────────────────────────
        if not commit.ordered_hunks:
            logger.warning(f"Skipping {commit.hash}: No hunks.")
            return False

        analysis = commit.causal_analysis

        # ── 过滤非单一需求 ────────────────────────────────────────────
        if analysis and not analysis.is_single_requirement:
            logger.info(f"Skipping {commit.hash[:7]}: not a single requirement.")
            return False

        # ── 置信度过滤 ────────────────────────────────────────────────
        if analysis and analysis.confidence < 0.6:
            logger.info(
                f"Skipping {commit.hash[:7]}: low confidence ({analysis.confidence:.2f})."
            )
            return False

        # ── 分离 Root 和后续 Hunks ────────────────────────────────────
        # rank_commit 已按 hunk_order 重排，ordered_hunks[0] 即为 Root
        root_hunk = commit.ordered_hunks[0]
        dependent_hunks = commit.ordered_hunks[1:]

        # ── 构建各部分数据 ────────────────────────────────────────────
        trigger_data = self._process_hunk(root_hunk)
        ground_truth_list = [self._process_hunk(h) for h in dependent_hunks]

        llm_ana = None
        if analysis:
            llm_ana = {
                "confidence": analysis.confidence,
                "change_pattern": analysis.change_pattern,
                "reasoning": analysis.reasoning,
                "root_hunk_id": analysis.root_hunk_id,
                "hunk_order": analysis.hunk_order,
            }

        # ── 组装完整记录 ──────────────────────────────────────────────
        record = {
            # 基础仓库信息
            "repo_path": commit.repo,
            "base_commit": "",
            "commit_hash": commit.hash,
            # 需求信息
            "commit_message": commit.msg,
            "issue_description": commit.issue_description,
            "requirement_summary": analysis.requirement_summary if analysis else None,
            # 核心数据对
            "trigger_edit": trigger_data,
            "ground_truth": ground_truth_list,
            # LLM 分析增强信息
            "llm_analysis": llm_ana,
        }

        self._append_to_jsonl(record)
        return True

    def _process_hunk(self, hunk: Hunk) -> Dict[str, Any]:
        """将 Hunk 对象转换为目标 JSON 格式。"""
        before_code, after_code = self._parse_diff_content(hunk.content)
        node_identifier = f"{hunk.file_path}:{hunk.new_start_line}"

        return {
            "file_path": hunk.file_path,
            "old_start_line": hunk.old_start_line,
            "old_end_line": hunk.old_start_line + hunk.old_len,
            "start_line": hunk.new_start_line,
            "end_line": hunk.new_start_line + hunk.new_len,
            "node_id": node_identifier,
            "order_index": hunk.order_index,  # 在序列中的物理位置
            "before_code": before_code,
            "after_code": after_code,
        }

    def _parse_diff_content(self, content: str) -> tuple[str, str]:
        """解析 Git Diff 文本，分离 before / after 代码。"""
        before_lines = []
        after_lines = []

        for line in content.split("\n"):
            if line.startswith(("---", "+++", "@@")):
                continue
            if line.startswith("-"):
                before_lines.append(line[1:])
            elif line.startswith("+"):
                after_lines.append(line[1:])
            else:
                clean_line = line[1:] if line.startswith(" ") else line
                before_lines.append(clean_line)
                after_lines.append(clean_line)

        return "\n".join(before_lines), "\n".join(after_lines)

    def _append_to_jsonl(self, data: Dict[str, Any]):
        try:
            with open(self.output_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write to {self.output_file}: {e}")