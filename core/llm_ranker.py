import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

import openai
from loguru import logger

from core.types import AnalyzedCommit, Hunk, LLMAnalysisResult
from config.settings import MiningConfig

# ══════════════════════════════════════════════════════════════════════════
# 全局常量（保持不变）
# ══════════════════════════════════════════════════════════════════════════

_TEST_PATH_PATTERNS = re.compile(
    r"(^|[\\/])(test|tests|spec|specs|__tests__|__mocks__)[\\/]"
    r"|[\\/](test|spec)s?[\\/]"
    r"|\.(test|spec)\.(ts|tsx|js|jsx|py|java|go|rb|cs)$"
    r"|(^|[\\/])(conftest|setup_tests?|test_utils?)\.",
    re.IGNORECASE,
)

_COMMENT_LINE_PATTERN = re.compile(
    r"^[+-]\s*(//|#|/\*|\*|<!--|\"\"\"|''')"
)

_VALID_PATTERNS = {
    "Refactoring", "Bug Fix", "Enhancement", "New Feature",
    "Config Change",
    "Performance Optimization", "Security Fix", "Deprecation",
    "Error Handling", "Dependency Update"
}


def _is_test_hunk(hunk: Hunk) -> bool:
    return bool(_TEST_PATH_PATTERNS.search(hunk.file_path))


def _is_comment_only_hunk(hunk: Hunk) -> bool:
    change_lines = [
        line for line in hunk.content.split("\n")
        if line.startswith(("+", "-"))
           and not line.startswith(("+++", "---"))
    ]
    if not change_lines:
        return False
    return all(_COMMENT_LINE_PATTERN.match(line) for line in change_lines)


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 结果数据类
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Stage1Result:
    """Stage 1 质量门控与意图识别的结构化结果"""
    is_analyzable: bool
    is_single_requirement: bool
    disqualification_reason: Optional[str]  # "Category X: ..."
    change_pattern: Optional[str]
    requirement_summary: Optional[str]
    quality_reasoning: str  # 质量审计推理过程（用于日志）


# ══════════════════════════════════════════════════════════════════════════
# Two-Stage LLM Causal Ranker
# ══════════════════════════════════════════════════════════════════════════

class LLMCausalRanker:
    """
    两阶段 LLM 因果分析器：
      Stage 1 — 质量门控 & 意图识别（过滤低质量 Commit，识别变更类型和需求摘要）
      Stage 2 — 根因识别 & Hunk 排序（以 Stage 1 结论为锚点，深度推理因果链）
    """

    def __init__(self, config: MiningConfig):
        self.client = openai.OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL
        )
        self.model = config.LLM_MODEL
        self.max_diff_lines = config.LLM_MAX_DIFF_LINES

    # ──────────────────────────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────────────────────────

    def rank_commit(self, commit: AnalyzedCommit) -> Optional[AnalyzedCommit]:
        """
        两阶段分析 commit，返回排序后的 AnalyzedCommit。
        任意阶段不通过则返回 None。
        """
        # ── 预过滤：移除测试文件和纯注释 Hunk ────────────────────────
        source_hunks = [
            h for h in commit.ordered_hunks
            if not _is_test_hunk(h) and not _is_comment_only_hunk(h)
        ]
        filtered_count = len(commit.ordered_hunks) - len(source_hunks)
        if filtered_count > 0:
            logger.debug(
                f"[{commit.hash[:7]}] Filtered {filtered_count} test/comment "
                f"hunk(s), {len(source_hunks)} remaining."
            )

        if not source_hunks:
            logger.info(f"[{commit.hash[:7]}] No valid hunks after filtering.")
            return None

        # ── 单 Hunk 早退：无需 LLM ────────────────────────────────────
        if len(source_hunks) <= 2:
            return None
            # return self._handle_single_hunk(commit, source_hunks)

        commit.ordered_hunks = source_hunks

        # ══════════════════════════════════════════════════════════════
        # Stage 1: Quality Gate & Intent Recognition
        # ══════════════════════════════════════════════════════════════
        stage1 = self._run_stage1(commit)
        if stage1 is None:
            # LLM 调用或解析失败
            return None

        if not stage1.is_analyzable:
            logger.info(
                f"[{commit.hash[:7]}] [S1] Disqualified — "
                f"{stage1.disqualification_reason}"
            )
            return None

        if not stage1.is_single_requirement:
            logger.info(
                f"[{commit.hash[:7]}] [S1] Not single requirement — "
                f"{stage1.quality_reasoning[:120]}"
            )
            return None

        logger.debug(
            f"[{commit.hash[:7]}] [S1] ✓ Passed | "
            f"Pattern={stage1.change_pattern} | "
            f"Summary={stage1.requirement_summary}"
        )

        # ══════════════════════════════════════════════════════════════
        # Stage 2: Root Cause Identification & Hunk Ordering
        # ══════════════════════════════════════════════════════════════
        return self._run_stage2(commit, stage1)

    # ──────────────────────────────────────────────────────────────────
    # Stage 1 实现
    # ──────────────────────────────────────────────────────────────────

    def _run_stage1(self, commit: AnalyzedCommit) -> Optional[Stage1Result]:
        """
        调用 LLM 完成质量门控与意图识别。
        失败时返回 None。
        """
        prompt = self._build_stage1_prompt(commit)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._stage1_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(response.choices[0].message.content)

            reasoning = data.get("reasoning", {})
            quality_reasoning = (
                reasoning.get("quality_audit", "")
                if isinstance(reasoning, dict)
                else str(reasoning)
            )

            return Stage1Result(
                is_analyzable=bool(data.get("is_analyzable", False)),
                is_single_requirement=bool(data.get("is_single_requirement", False)),
                disqualification_reason=data.get("disqualification_reason"),
                change_pattern=data.get("change_pattern"),
                requirement_summary=data.get("requirement_summary"),
                quality_reasoning=quality_reasoning,
            )

        except json.JSONDecodeError as e:
            logger.error(f"[{commit.hash[:7]}] [S1] JSON parse failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[{commit.hash[:7]}] [S1] LLM call failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────
    # Stage 2 实现
    # ──────────────────────────────────────────────────────────────────

    def _run_stage2(
            self, commit: AnalyzedCommit, stage1: Stage1Result
    ) -> Optional[AnalyzedCommit]:
        """
        在 Stage 1 结论的基础上，调用 LLM 完成根因识别与 Hunk 排序。
        失败或校验不通过时返回 None。
        """
        source_hunks = commit.ordered_hunks
        total = len(source_hunks)
        prompt = self._build_stage2_prompt(commit, stage1)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._stage2_system_prompt()},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(response.choices[0].message.content)

            # ── 解析核心字段 ──────────────────────────────────────────
            chosen_index = int(data.get("root_hunk_index", -1))
            hunk_order: List[int] = data.get("hunk_order", [])
            confidence = float(data.get("confidence_score", 0.0))
            reasoning = data.get("reasoning", {})

            # ── 校验 root_hunk_index ──────────────────────────────────
            if not (0 <= chosen_index < total):
                logger.warning(
                    f"[{commit.hash[:7]}] [S2] Invalid root_hunk_index "
                    f"{chosen_index}, discarding."
                )
                return None

            # ── 校验 hunk_order 完整性 ────────────────────────────────
            if (
                    len(hunk_order) != total
                    or set(hunk_order) != set(range(total))
                    or hunk_order[0] != chosen_index
            ):
                logger.warning(
                    f"[{commit.hash[:7]}] [S2] Invalid hunk_order {hunk_order}, "
                    f"falling back to root-first order."
                )
                hunk_order = [chosen_index] + [
                    i for i in range(total) if i != chosen_index
                ]

            # ── change_pattern：Stage 2 可在 Stage 1 基础上细化 ───────
            raw_pattern = data.get("change_pattern", stage1.change_pattern or "Unknown")
            change_pattern = (
                raw_pattern if raw_pattern in _VALID_PATTERNS else "Unknown"
            )
            if change_pattern == "Unknown" and raw_pattern != "Unknown":
                logger.warning(
                    f"[{commit.hash[:7]}] [S2] Unknown change_pattern "
                    f"'{raw_pattern}', falling back to Unknown."
                )

            root_hunk = source_hunks[chosen_index]
            reasoning_str = (
                json.dumps(reasoning, ensure_ascii=False)
                if isinstance(reasoning, dict)
                else str(reasoning)
            )

            # ── 写入分析结果 ──────────────────────────────────────────
            commit.causal_analysis = LLMAnalysisResult(
                root_hunk_id=root_hunk.id,
                confidence=confidence,
                reasoning=reasoning_str,
                change_pattern=change_pattern,
                is_single_requirement=True,
                requirement_summary=stage1.requirement_summary,
                hunk_order=hunk_order,
            )

            # ── 按 hunk_order 重排 ────────────────────────────────────
            commit.ordered_hunks = [source_hunks[i] for i in hunk_order]
            for position, hunk in enumerate(commit.ordered_hunks):
                hunk.order_index = position

            logger.info(
                f"[{commit.hash[:7]}] [S2] ✓ Root=Hunk#{chosen_index} "
                f"({root_hunk.id}) | Order={hunk_order} | "
                f"Score={confidence:.2f} | Pattern={change_pattern} | "
                f"Summary={stage1.requirement_summary}"
            )
            return commit

        except json.JSONDecodeError as e:
            logger.error(f"[{commit.hash[:7]}] [S2] JSON parse failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[{commit.hash[:7]}] [S2] LLM call failed: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────
    # 单 Hunk 处理（无需 LLM）
    # ──────────────────────────────────────────────────────────────────

    def _handle_single_hunk(
            self, commit: AnalyzedCommit, source_hunks: list
    ) -> AnalyzedCommit:
        source_hunks[0].order_index = 0
        commit.ordered_hunks = source_hunks
        commit.causal_analysis = LLMAnalysisResult(
            root_hunk_id=source_hunks[0].id,
            confidence=1.0,
            reasoning=json.dumps({
                "root_cause": "Only one valid hunk after filtering.",
                "coherence_check": "Single hunk — not applicable.",
                "summary_basis": "Derived directly from commit message.",
                "order_rationale": "Single hunk — not applicable.",
            }),
            change_pattern="Unknown",
            is_single_requirement=True,
            requirement_summary=commit.msg,
            hunk_order=[0],
        )
        return commit

    # ──────────────────────────────────────────────────────────────────
    # System Prompts
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _stage1_system_prompt() -> str:
        return (
            "You are a Dataset Quality Auditor specializing in code change analysis. "
            "Your job is to act as a strict quality gate for a high-value evaluation dataset. "
            "You must identify and reject low-quality commits before any deep analysis occurs. "
            "False positives (letting bad samples through) are far more harmful than "
            "false negatives (rejecting good samples). When in doubt, REJECT. "
            "Note: test files and comment changes need reject."
        )

    @staticmethod
    def _stage2_system_prompt() -> str:
        return (
            "You are a Senior Code Archaeologist. "
            "A commit has already passed strict quality screening and is confirmed to "
            "address a single, well-defined requirement. "
            "Your sole task is to reconstruct the developer's causal reasoning chain: "
            "identify the Root Cause Hunk (the logical origin of the change propagation) "
            "and model the modification order of all hunks. "
            "The requirement context from the quality gate is your semantic anchor — "
            "use it to reason precisely. Be analytical and conservative in confidence scoring."
        )

    # ──────────────────────────────────────────────────────────────────
    # Stage 1 Prompt
    # ──────────────────────────────────────────────────────────────────

    def _build_stage1_prompt(self, commit: AnalyzedCommit) -> str:
        total_hunks = len(commit.ordered_hunks)
        issue_text = commit.issue_description or "N/A"
        hunks_display = self._format_hunks(commit.ordered_hunks)

        return f"""
## Mission
Act as a strict quality gate. Determine whether this commit qualifies as a
HIGH-QUALITY test set sample, then identify its core intent.
This dataset is used for model evaluation — be strict, reject anything ambiguous.

NOTE: Only focus on the source file changes.
---

## Input

### Commit Message
{commit.msg}

### Issue Description
{issue_text}

### Candidate Hunks ({total_hunks} total — test files and comment-only hunks pre-filtered)
{hunks_display}


---

## Task A — Quality Pre-Screening

Check ALL three categories. Set `is_analyzable: false` if ANY single condition is met.

### Category A — Formatting / Style-Only Changes
Disqualify if the dominant change is purely cosmetic with zero semantic effect:
- Indentation, whitespace, or blank-line normalization
- Import statement reordering or grouping
- Trailing whitespace / end-of-file newline cleanup
- Quote style unification (single ↔ double), semicolon addition/removal (JS/TS)
- Bracket / brace placement style changes, code alignment
**Detection rule**: Strip all whitespace from each changed line pair.
If `-` and `+` versions become identical → formatting-only hunk.
If ALL hunks are formatting-only → disqualify.

### Category B — Mechanical Rename / Mass Substitution
Disqualify if ALL changes are fully explained by a single find-and-replace rule
with no logic change:
- Global rename (variable / function / class, logic unchanged)
- Bulk import path updates caused only by file relocation
- Magic number → named constant (behavior unchanged)
- Spelling correction causing bulk symbol rename

### Category C — Multi-Requirement Contamination
Disqualify if the commit mixes independent concerns:
- Two or more hunks fix different, unrelated bugs or intent.
- A feature hunk is mixed with an unrelated cleanup hunk (dead code in a
  different module, unrelated typo fix, unrelated formatting)
- One hunk updates only version number / changelog / metadata while others
  implement logic changes
- A hunk is a "hitchhiker" — could be a separate commit without affecting
  the correctness or completeness of the other hunks

---

## Task B — Intent Recognition
(Only execute if `is_analyzable: true`)

### B1 — Single-Requirement Coherence
Set `is_single_requirement: true` ONLY when ALL four hold:
1. Every hunk serves one unified intent (same bug / feature / refactoring goal)
2. All hunks are semantically connected through causal or adaptation relationships
   — NO isolated nodes
3. No hunk is a "hitchhiker" (could be committed separately without breaking others)

### B2 — Change Pattern Classification
Classify into exactly one of:
"Refactoring" | "Bug Fix" | "Enhancement" | "New Feature" | "Config Change" |
"Performance Optimization" | "Security Fix" | "Deprecation" |
"Error Handling" | "Dependency Update"

### B3 — Requirement Summary
(Only when `is_single_requirement: true`)
ONE English sentence, ≤ 20 words. Focus on WHAT and WHY, not HOW.
No code symbols, file names, or function names.
✅ "Suppress redundant manual approval instructions on platforms with native approval UI."
❌ "Modified buildExecApprovalPromptGuidance in system-prompt.ts to add a channel check."

---

## Output Format
Single JSON object, no Markdown fences, no extra text.

{{
  "is_analyzable"          : <bool>,
  "disqualification_reason": "<Category letter(s) + one-sentence evidence citing specific lines/files>" | null,
  "is_single_requirement"  : <bool> | null,
  "change_pattern"         : "<pattern>" | null,
  "requirement_summary"    : "<≤20-word English sentence>" | null,
  "reasoning": {{
    "quality_audit"   : "<for EACH of the 6 categories: state whether it applies and cite specific evidence>",
    "coherence_check" : "<if analyzable: evaluate each of the 4 single-requirement conditions>" | null,
    "summary_basis"   : "<which commit message / hunk content the summary is derived from>" | null
  }}
}}
"""

    # ──────────────────────────────────────────────────────────────────
    # Stage 2 Prompt
    # ──────────────────────────────────────────────────────────────────

    def _build_stage2_prompt(
            self, commit: AnalyzedCommit, stage1: Stage1Result
    ) -> str:
        total_hunks = len(commit.ordered_hunks)
        hunk_id_to_index = {h.id: i for i, h in enumerate(commit.ordered_hunks)}
        dependency_text = self._format_dependency_chains(
            commit.dependency_chains, hunk_id_to_index
        )
        hunks_display = self._format_hunks(commit.ordered_hunks)

        return f"""
## Mission
A commit has passed quality screening. Reconstruct the developer's causal
reasoning chain with precision: identify the Root Cause Hunk and model the
modification order of all hunks.

NOTE: Only focus on the source file changes.
---

## Pre-established Context (from Quality Gate — treat as ground truth)

| Field               | Value                          |
|---------------------|--------------------------------|
| Requirement Summary | {stage1.requirement_summary}   |
| Change Pattern      | {stage1.change_pattern}        |

Use the Requirement Summary as your **semantic anchor** throughout the analysis.
Every reasoning step should be grounded in this established intent.

---

## Input

### Commit Message
{commit.msg}

### Candidate Hunks ({total_hunks} total)
{hunks_display}




---

## Task 1 — Root Cause Hunk Identification

Identify the single Root Cause Hunk — the logical origin of this change propagation.
The Root is the hunk whose change NECESSITATES all other hunks.

Given the confirmed change pattern **{stage1.change_pattern}**, apply:

| Pattern              | Root Characteristics                             | Responder Characteristics              |
|----------------------|--------------------------------------------------|----------------------------------------|
| Refactoring          | Modifies the definition (function/class/type)    | Modifies call sites                    |
| Bug Fix              | Corrects the core logic error                    | Adaptation changes                     |
| Enhancement          | Extends the primary abstraction or core logic    | Interface / wiring / routing updates   |
| New Feature          | Introduces the core new logic                    | Registration / routing / adapter hunks |
| Config Change        | Modifies the core configuration entry            | Reader-side adaptations                |
| Performance Optimization   | Replaces the bottleneck algorithm, data structure, or I/O pattern                    | Adjusts callers to match new API contract or data shape                      |
| Security Fix               | Removes or neutralizes the vulnerable code path                                      | Adds / tightens validation, sanitization, or auth checks at entry points     |
| Deprecation / Removal      | Deletes the deprecated definition or feature flag                                    | Removes all call sites, references, and dead branches                        |
| Error Handling             | Adds or corrects the fault-tolerance boundary (try/catch, guard, retry)              | Propagates new error type / return value to upstream callers                 |
| Dependency Update          | Bumps the dependency version or swaps the library                                    | Adapts call sites to new API surface or changed behavior                     |

#### Universal Positive Signals
- Introduces a new symbol (function, class, constant, type) referenced by others → Root
- Modifies an abstraction layer (interface, base class, core config) → Root
- Semantic density asymmetry: fewer lines but higher conceptual density → likely Root
- Most directly embodies the Requirement Summary above → Root

#### Universal Elimination Rules
- Change fully explained by another hunk's change → Responder, NOT Root
- Removing this hunk leaves remaining hunks' intent still complete → NOT Root
- Pure call-site adaptation (replacing old call with new) → NEVER Root
- Pure parameter pass-through (forwarding param up/down the stack) → NEVER Root

---

## Task 2 — Hunk Modification Order

Output an ordered list of ALL indices (0 to {total_hunks - 1}).

Rules (strict priority order):
1. Root MUST be first (position 0).
2. Hunks directly depending on Root come next.
3. Chain propagation priority: if B depends on Root and C depends on B,
   order is [Root, B, C, ...] (depth-first, not breadth-first).
4. Tie-breaking for parallel dependencies:
   a. Same file as prior hunk → higher priority (file locality)
   b. Within same file → ascending line number
   c. Across files → core business logic before peripheral (CLI/routing/adapter)
5. Every index appears exactly once — no omissions, no duplicates.

---

## Task 3 — Confidence Score

Score strictly. Round DOWN when uncertain. Do NOT inflate.

| Score      | Meaning                                                                           |
|------------|-----------------------------------------------------------------------------------|
| 0.9 – 1.0  | Root unique and unambiguous; full causal chain clear; ordering has one valid answer |
| 0.75 – 0.9 | Root certain; at most one ordering uncertainty; logic coherent                    |
| 0.6 – 0.75 | Root largely certain but one hunk has weak/indirect connection                    |
| 0.4 – 0.6  | Two root candidates OR ordering has multiple plausible answers                    |
| 0.0 – 0.4  | Root uncertain; causal chain cannot be reliably modeled                           |

---

## Output Format
Single JSON object, no Markdown fences, no extra text.

{{
  "root_hunk_index" : <int, 0 to {total_hunks - 1}>,
  "confidence_score": <float, 0.0 to 1.0>,
  "change_pattern"  : "<confirm or refine Stage 1 classification if new evidence warrants>",
  "hunk_order"      : [<int>, ...],
  "reasoning": {{
    "root_cause"     : "<positive evidence for Root + elimination reasoning for each non-Root hunk>",
    "order_rationale": "<for each non-Root hunk: why at this position; tie-breaking for parallel deps>"
  }}
}}
"""

    # ──────────────────────────────────────────────────────────────────
    # 共用辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _format_hunks(self, hunks: list) -> str:
        result = ""
        for idx, hunk in enumerate(hunks):
            lines = hunk.content.split("\n")
            if len(lines) > self.max_diff_lines:
                content_snippet = (
                        "\n".join(lines[: self.max_diff_lines]) + "\n... (truncated)"
                )
            else:
                content_snippet = "\n".join(lines)
            result += f"""
---
[Hunk Index: {idx}]
File: {hunk.file_path} (Lines {hunk.start_line}-{hunk.end_line})
```diff
{content_snippet}
```
"""
        return result

    def _format_dependency_chains(
            self, chains: List[Any], hunk_id_to_index: Dict[str, int]
    ) -> str:
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
                    f"- Hunk {src_idx} depends on Hunk {tgt_idx} "
                    f"(Path: {path_desc})"
                )
        return (
            "\n".join(lines)
            if lines
            else "No explicit static dependencies detected between these hunks."
        )


# ══════════════════════════════════════════════════════════════════════════
# Exporter（保持不变）
# ══════════════════════════════════════════════════════════════════════════

class CausalDatasetExporter:
    """将分析后的 AnalyzedCommit 导出为 JSONL 格式。"""

    def __init__(self, output_file: str):
        self.output_file = output_file
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

    def save_commit(self, commit: AnalyzedCommit) -> bool:
        if not commit.ordered_hunks:
            logger.warning(f"Skipping {commit.hash}: No hunks.")
            return False

        analysis = commit.causal_analysis

        if analysis and not analysis.is_single_requirement:
            logger.info(f"Skipping {commit.hash[:7]}: not a single requirement.")
            return False

        if analysis and analysis.confidence < 0.6:
            logger.info(
                f"Skipping {commit.hash[:7]}: low confidence ({analysis.confidence:.2f})."
            )
            return False

        root_hunk = commit.ordered_hunks[0]
        dependent_hunks = commit.ordered_hunks[1:]

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

        record = {
            "repo_path": commit.repo,
            "base_commit": "",
            "commit_hash": commit.hash,
            "commit_message": commit.msg,
            "issue_description": commit.issue_description,
            "requirement_summary": analysis.requirement_summary if analysis else None,
            "trigger_edit": trigger_data,
            "ground_truth": ground_truth_list,
            "llm_analysis": llm_ana,
        }

        self._append_to_jsonl(record)
        return True

    def _process_hunk(self, hunk: Hunk) -> Dict[str, Any]:
        before_code, after_code = self._parse_diff_content(hunk.content)
        node_identifier = f"{hunk.file_path}:{hunk.new_start_line}"
        return {
            "file_path": hunk.file_path,
            "old_start_line": hunk.old_start_line,
            "old_end_line": hunk.old_start_line + hunk.old_len,
            "start_line": hunk.new_start_line,
            "end_line": hunk.new_start_line + hunk.new_len,
            "node_id": node_identifier,
            "order_index": hunk.order_index,
            "before_code": before_code,
            "after_code": after_code,
        }

    def _parse_diff_content(self, content: str) -> tuple[str, str]:
        before_lines, after_lines = [], []
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
