from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Tuple


class FileContent(BaseModel):
    file_path: str
    content_before: Optional[str]  # 修改前完整内容（带行号）
    content_after: Optional[str]  # 修改后完整内容（带行号）


class Symbol(BaseModel):
    name: str
    kind: str  # 'function', 'class', 'variable', 'import', 'attribute'

    def __hash__(self):
        return hash((self.name, self.kind))


class Relation(BaseModel):
    source_hunk_id: str
    target_hunk_id: str
    reason: str  # 例如 "Defines variable 'x'" 或 "Text similarity 0.8"


class FileChange(BaseModel):
    old_path: Optional[str]
    new_path: Optional[str]
    change_type: str  # ADD, MODIFY, DELETE, RENAME
    diff: str
    source_code: Optional[str]
    is_test: bool = False  # 标记是否为测试文件


class CommitCandidate(BaseModel):
    """
    这是在流水线中流转的核心对象。
    Phase 1 填充基础信息。
    Phase 2 将填充 dependency_graph。
    Phase 3 将填充 scenarios。
    """
    repo_name: str
    hash: str
    msg: str
    author_date: str
    repo_url: str
    is_merge: bool

    issue_ids: List[str] = Field(default_factory=list)

    # 分离存储，方便后续处理
    source_changes: List[FileChange] = []  # 核心数据：参与协同修改分析
    test_changes: List[FileChange] = []  # 验证数据：仅用于测试

    # 统计信息
    source_files_count: int
    test_files_count: int

    metadata: Dict[str, Any] = Field(default_factory=dict)


class Hunk(BaseModel):
    id: str  # 唯一标识: "filepath:start_line"
    file_path: str
    content: str  # Hunk 的实际代码文本

    # 行号字段
    old_start_line: int  # 旧版本起始行
    old_len: int  # 旧版本长度
    new_start_line: int  # 新版本起始行
    new_len: int  # 新版本长度

    # 兼容字段（通常指向新版本，用于排序）
    start_line: int
    end_line: int

    # 排序结果：在最终 ordered_hunks 中的物理位置，0 表示 Root
    order_index: int = -1


class LLMAnalysisResult(BaseModel):
    root_hunk_id: str = Field(..., description="被判定为根因的 Hunk ID")
    confidence: float = Field(..., description="置信度 0.0-1.0")
    reasoning: str = Field(..., description="判定理由（覆盖四项任务）")
    change_pattern: str = Field(..., description="修改模式: Refactoring/New Feature/Bug Fix/Config Change/Enhancement")

    # ── 新增字段 ──────────────────────────────────────────────────────
    is_single_requirement: bool = Field(
        default=True,
        description="所有 Hunks 是否协同解决同一个需求"
    )
    requirement_summary: Optional[str] = Field(
        default=None,
        description="一句话需求摘要（is_single_requirement=True 时有值）"
    )
    hunk_order: List[int] = Field(
        default_factory=list,
        description="Hunk 的修改先后顺序（原始索引列表，第 0 位为 Root）"
    )


class DependencyChain(BaseModel):
    """描述两个 Hunk 之间的依赖路径"""
    source: str
    target: str
    path: List[str]
    raw_path: Optional[List[str]] = None


class AnalyzedCommit(BaseModel):
    hash: str
    repo: str
    msg: str

    # 关联的 Issue 描述（如果能获取到）
    issue_description: Optional[str] = None

    # 经过排序的源码 Hunk 序列（训练目标）
    # 经 LLMCausalRanker 处理后：[0] = Root Hunk，其余按 hunk_order 排列
    ordered_hunks: List[Hunk]

    # 测试 Hunk（验证上下文）
    test_hunks: List[Hunk]

    # 依赖关系
    dependencies: List[Dict[str, Any]]

    # 图中的边：[(source_id, target_id, type), ...]
    dependency_edges: List[Tuple[str, str, str]] = Field(default_factory=list)

    # 完整的依赖链路
    dependency_chains: List[DependencyChain] = Field(default_factory=list)

    # 旧版本（Parent）的依赖信息
    old_dependencies: List[Dict[str, Any]]
    old_dependency_chains: List[DependencyChain] = Field(default_factory=list)

    # 依赖变化标签: "BOTH", "NEW_ONLY", "OLD_ONLY", "NONE"
    dependency_label: str = "NONE"

    # 统计指标
    old_metrics: Dict[str, Any] = Field(default_factory=dict)
    new_metrics: Dict[str, Any] = Field(default_factory=dict)

    # LLM 分析结果（包含根因、需求摘要、修改顺序等）
    causal_analysis: Optional[LLMAnalysisResult] = None