import json
import os
import re

import openai
from typing import List, Optional, Dict, Any
from loguru import logger
from core.types import AnalyzedCommit, Hunk, LLMAnalysisResult
from config.settings import MiningConfig

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
    "Refactoring+Bug Fix", "Refactoring+Enhancement",
    "Enhancement+Bug Fix", "New Feature+Refactoring",
}


def _is_test_hunk(hunk: Hunk) -> bool:
    """Determine whether a hunk belongs to a test file."""
    return bool(_TEST_PATH_PATTERNS.search(hunk.file_path))


def _is_comment_only_hunk(hunk: Hunk) -> bool:
    """
    Determine whether a hunk contains only comment changes.
    Strategy: if all meaningful change lines (+/-) are comment lines, treat as comment-only.
    """
    change_lines = [
        line for line in hunk.content.split("\n")
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
    ]
    if not change_lines:
        return False
    return all(_COMMENT_LINE_PATTERN.match(line) for line in change_lines)

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
        # ── Pre-filter: remove test files and comment-only hunks ──────
        source_hunks = [
            h for h in commit.ordered_hunks
            if not _is_test_hunk(h) and not _is_comment_only_hunk(h)
        ]

        filtered_count = len(commit.ordered_hunks) - len(source_hunks)
        if filtered_count > 0:
            logger.debug(
                f"[{commit.hash[:7]}] Filtered out {filtered_count} test/comment hunk(s), "
                f"{len(source_hunks)} valid hunk(s) remaining."
            )

        if not source_hunks:
            logger.info(f"[{commit.hash[:7]}] No valid hunks after filtering, skipping.")
            return None

        # ── Early exit: single hunk requires no ranking ───────────────
        if len(source_hunks) == 1:
            source_hunks[0].order_index = 0
            commit.ordered_hunks = source_hunks
            commit.causal_analysis = LLMAnalysisResult(
                root_hunk_id=source_hunks[0].id,
                confidence=1.0,
                reasoning=json.dumps({
                    "root_cause": "Only one valid hunk after filtering.",
                    "coherence_check": "Single hunk — coherence check not applicable.",
                    "summary_basis": "Derived directly from commit message.",
                    "order_rationale": "Single hunk — ordering not applicable."
                }),
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
                            "Your task is to reconstruct the developer's thought process "
                            "using only code semantics and commit message — no static dependency graph is available. "
                            "Identify the Root Cause Hunk, judge requirement coherence, "
                            "generate a requirement summary, and model the hunk modification order. "
                            "Note: ignore the test files and comment-only changes."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )

            content = response.choices[0].message.content
            result_data = json.loads(content)

            # ── Task 2: discard non-single-requirement commits ────────
            is_single = result_data.get("is_single_requirement", False)
            if not is_single:
                reasoning_obj = result_data.get("reasoning", {})
                coherence_msg = (
                    reasoning_obj.get("coherence_check", "")
                    if isinstance(reasoning_obj, dict)
                    else str(reasoning_obj)
                )
                logger.info(
                    f"[{commit.hash[:7]}] Discarded: not a single requirement. "
                    f"Reason: {coherence_msg[:120]}"
                )
                return None

            # ── Parse remaining fields ────────────────────────────────
            chosen_index = int(result_data.get("root_hunk_index", -1))
            hunk_order: List[int] = result_data.get("hunk_order", [])
            requirement_summary: Optional[str] = result_data.get("requirement_summary", None)
            confidence = float(result_data.get("confidence_score", 0.0))
            reasoning = result_data.get("reasoning", {})
            total = len(source_hunks)

            # ── Validate and normalize change_pattern ─────────────────
            raw_pattern = result_data.get("change_pattern", "Unknown")
            change_pattern = (
                raw_pattern if raw_pattern in _VALID_PATTERNS else "Unknown"
            )
            if change_pattern == "Unknown":
                logger.warning(
                    f"[{commit.hash[:7]}] Unknown change_pattern '{raw_pattern}', "
                    f"falling back to Unknown."
                )

            # ── Validate root_hunk_index ──────────────────────────────
            if not (0 <= chosen_index < total):
                logger.warning(
                    f"[{commit.hash[:7]}] Invalid root_hunk_index {chosen_index}, discarding."
                )
                return None

            # ── Validate hunk_order completeness ─────────────────────
            if (
                    len(hunk_order) != total
                    or set(hunk_order) != set(range(total))
                    or hunk_order[0] != chosen_index
            ):
                logger.warning(
                    f"[{commit.hash[:7]}] Invalid hunk_order {hunk_order}, "
                    f"falling back to root-first order."
                )
                hunk_order = [chosen_index] + [i for i in range(total) if i != chosen_index]

            root_hunk = source_hunks[chosen_index]

            # Serialize reasoning dict to string for storage
            reasoning_str = (
                json.dumps(reasoning, ensure_ascii=False)
                if isinstance(reasoning, dict)
                else str(reasoning)
            )

            # ── Save LLM analysis result ──────────────────────────────
            commit.causal_analysis = LLMAnalysisResult(
                root_hunk_id=root_hunk.id,
                confidence=confidence,
                reasoning=reasoning_str,
                change_pattern=change_pattern,
                is_single_requirement=True,
                requirement_summary=requirement_summary,
                hunk_order=hunk_order
            )

            # ── Reorder hunks by hunk_order ───────────────────────────
            commit.ordered_hunks = [source_hunks[i] for i in hunk_order]
            for position, hunk in enumerate(commit.ordered_hunks):
                hunk.order_index = position

            logger.info(
                f"[{commit.hash[:7]}] Root: Hunk#{chosen_index} ({root_hunk.id}) | "
                f"Order: {hunk_order} | Score: {confidence:.2f} | "
                f"Pattern: {change_pattern} | Summary: {requirement_summary}"
            )

        except json.JSONDecodeError as e:
            logger.error(f"[{commit.hash[:7]}] JSON parse failed: {e}")
            return None
        except Exception as e:
            logger.error(f"[{commit.hash[:7]}] LLM analysis failed: {e}")
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
        Goal
        Analyze the following commit and complete FOUR tasks:

        Identify the single "Root Cause Hunk" — the logical origin of this change propagation.
        Determine whether ALL hunks collaboratively solve ONE single requirement.
        If they do, generate a concise requirement summary in English.
        Model the modification order of ALL hunks (starting from the Root), reflecting the logical propagation sequence of the change.
        Note: All test files and comment-only changes have already been excluded from the input.
        You do not need to consider them.

        Context
        Commit Message
        {commit.msg}

        Candidate Hunks ({total_hunks} total, all production code)
        {hunks_display}
        
        Original Diff
        {commit.source_diff}
        
        Reasoning Guidelines
        Task 1 — Root Cause Identification
        Positive signals — the following features point to the Root:

        New symbol definition: A hunk introduces a new function, class, constant, or type definition, while another hunk calls or references it → The definition side is the Root; the call site is a responder.
        Pure new file/block: A hunk tagged "PURE NEW FILE/BLOCK" introduces an entirely new logical unit → If its content directly corresponds to the core intent in the Commit Message, treat it as the Root.
        Abstraction-layer change: A hunk modifies an interface, base class, or core configuration entry → The hunk modifying the abstraction layer is the Root; implementation/adapter hunks are responders.
        Inline logic removal + new encapsulation: One hunk removes hardcoded/inline logic, another introduces an encapsulating function → The encapsulation hunk is the Root (it embodies the core intent); the removal hunk is a responder.
        Semantic density asymmetry: A hunk with fewer changed lines but higher semantic density (introduces a new concept or branch) is likely the Root; a hunk with many mechanical changes (bulk replacement/adaptation) is likely a responder.
        Commit Message alignment: The hunk whose changes most directly correspond to the core intent described in the Commit Message is the Root.
        Elimination method — the following features rule out the Root:

        If a hunk's changes can be fully explained by another hunk's changes (i.e., "this had to change because that changed"), it is a responder, not the Root.
        If removing this hunk leaves the remaining hunks' intent still complete and understandable, it is not the Root.
        A pure call-site adaptation (e.g., replacing an old function call with a new one) is never the Root.
        A pure parameter pass-through (e.g., forwarding a new parameter from an upper layer to a lower layer) is never the Root.
        Root identification rules by change type:

        Refactoring : The hunk modifying the function/class definition is the Root; the hunk modifying call sites is a responder.
        Bug Fix : The hunk correcting the core logic error is the Root; adaptation changes are responders.
        Enhancement : The hunk extending the primary abstraction or core logic is the Root; interface updates are responders.
        New Feature : The hunk introducing the core new logic is the Root; registration/wiring/routing hunks are responders.
        Config Change : The hunk modifying the core configuration entry is the Root; reader-side adaptations are responders.
        Composite type : When a hunk simultaneously embodies a refactoring technique and a fix/enhancement purpose (e.g., extracting a function while introducing new branch logic), prioritize the fix/enhancement intent to locate the Root — the hunk that introduces new behavior is the Root, not the one performing only structural reorganization.
        Task 2 — Single-Requirement Coherence Check
        Mark is_single_requirement: true ONLY when ALL of the following hold:

        Every hunk serves one unified intent (same bug, same feature, same refactoring goal).
        All hunks are semantically connected through causal or adaptation relationships (no isolated nodes).
        The Commit Message describes a single, coherent change objective.
        Mark is_single_requirement: false when ANY of the following is observed:

        Hunks touch completely unrelated modules with no semantic causal connection between them.
        The Commit Message lists multiple unrelated items (e.g., "fix X; also refactor Y").
        An incidental, unrelated change is mixed in (e.g., fixing an unrelated typo or formatting while implementing a feature).
        Multiple hunks each fix a different bug, even if all are of "Bug Fix" type.
        A hunk only updates a version number, changelog, or metadata unrelated to the core logic.
        Some hunks are pure whitespace or formatting changes unrelated to the core requirement.
        Task 3 — Requirement Summary
        Generate ONLY when is_single_requirement is true; otherwise output null.
        Language: Always write in English, regardless of the language of the Commit Message.
        Exactly ONE sentence, no more than 20 words.
        Focus on WHAT was changed and WHY — not HOW.
        Avoid code symbols, file names, or variable names.
        Good example : "Suppress redundant manual approval instructions on platforms with native approval UI."
        Bad example : "Modified buildExecApprovalPromptGuidance in system-prompt.ts to add a channel check."
        Task 4 — Hunk Modification Order
        Output an ordered list of ALL hunk indices (0 to {total_hunks - 1}), including the Root.
        The Root hunk MUST appear FIRST (position 0 of the order list).
        Order the remaining hunks by their logical dependency on prior hunks:
        Hunks that directly depend on the Root (call a newly defined symbol, or adapt to the Root's change) come immediately after the Root.
        Hunks that depend on the second hunk come after the second hunk, and so on (chain propagation takes priority).
        Tie-breaking rules for parallel dependencies (when multiple hunks depend equally on the same prior hunk):
        Prefer hunks in the same file as the prior hunk (file locality first).
        Within the same file, order by ascending line number.
        Across files, prefer core-module hunks (business logic) before peripheral-module hunks (CLI / routing / adapter layer).
        Every hunk index must appear exactly once — no omissions, no duplicates.
        Confidence Score Anchors
        Score strictly according to the anchors below. Do not inflate scores:

        Score Range	Meaning
        0.9 – 1.0	Root is unique and unambiguous; causal chain across all hunks is clear; no isolated nodes
        0.7 – 0.9	Root is largely certain; one parallel dependency or ordering uncertainty exists, but overall logic is coherent
        0.5 – 0.7	Two root candidates exist, or one hunk has a weak semantic connection; inference is required
        0.3 – 0.5	Root is uncertain; multiple hunks have ambiguous semantic relationships; ordering is difficult to determine
        0.0 – 0.3	Hunks have almost no semantic connection; causal chain cannot be reliably modeled
        Output Format
        Respond with a single JSON object — no Markdown fences, no extra text.
        The reasoning field must be an object with exactly four keys,
        each covering the reasoning process for one task:

        {{
        "root_hunk_index"      : <int, 0 to {total_hunks - 1}>,
        "confidence_score"     : <float, 0.0 to 1.0, strictly following the anchors above>,
        "change_pattern"       : "Refactoring" | "Bug Fix" | "Enhancement" | "New Feature" | "Config Change" | "Refactoring+Bug Fix" | "Refactoring+Enhancement" | "Enhancement+Bug Fix" | "New Feature+Refactoring",
        "is_single_requirement": <bool>,
        "requirement_summary"  : "<one-sentence English summary, ≤ 20 words>" | null,
        "hunk_order"           : [<int>, <int>, ...],
        "reasoning": {{
        "root_cause"      : "<positive evidence + elimination reasoning: why this hunk is the Root, and why others are ruled out>",
        "coherence_check" : "<evaluate each condition for single-requirement judgment; explicitly state which conditions pass or fail and why>",
        "summary_basis"   : "<explain which information the summary is derived from: Commit Message and/or which hunk's core change>",
        "order_rationale" : "<for each non-root hunk, explain why it is placed at its current position; for parallel dependencies, state the tie-breaking rationale>"
        }}
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