import operator
from typing import Annotated, Any, Dict, List, NotRequired, Optional
from typing_extensions import TypedDict


class DiffFile(TypedDict):
    # ── 结构字段（unidiff 解析，Day 1-2）────────────────────
    filename: str
    change_type: str          # "added" | "modified" | "deleted"
    additions: int
    deletions: int
    patch: str                # raw unified diff hunk for this file

    # ── 语义字段（LLM 分析，Day 2 新增，可选）────────────────
    change_category: NotRequired[str]        # "feature" | "bugfix" | "refactor" | "config" | "test" | "docs" | "security"
    is_security_sensitive: NotRequired[bool]
    is_complex_logic: NotRequired[bool]
    file_summary: NotRequired[str]           # 一句话描述本文件的变更意图


class ReviewIssue(TypedDict):
    file: str
    line: Optional[int]
    severity: str             # "critical" | "high" | "medium" | "low" | "info"
    category: str
    title: str
    description: str
    suggestion: str


class ToolCallRecord(TypedDict):
    tool_name: str
    risk_level: str
    args: Dict[str, Any]
    result: Optional[str]
    approved: bool
    timestamp: str


class ReviewState(TypedDict):
    # ── 输入 ─────────────────────────────────────────────
    diff_content: str
    pr_metadata: Dict[str, Any]
    repo_name: str
    repo_url: str                            # GitHub 仓库 URL（Research Agent 使用）
    session_id: str

    # ── Agent 输出（逐步填充） ────────────────────────────
    diff_files: List[DiffFile]
    diff_summary: Dict[str, Any]            # DiffAnalyzer LLM 的整体摘要
    routing_decision: Dict[str, Any]        # Coordinator 路由决策

    security_findings: Annotated[List[ReviewIssue], operator.add]
    quality_findings: Annotated[List[ReviewIssue], operator.add]

    final_report: Optional[str]

    # ── Supervisor 多 Agent 控制 ──────────────────────────
    research_context: str                   # Research Agent 累积输出
    supervisor_instruction: str             # Supervisor 给下一个 Agent 的指令
    iteration_count: int                    # Supervisor 循环计数（防止无限循环）
    review_pipeline_called: bool            # Review 子图是否已被调用（与 findings 数量无关）

    # ── Harness 层 ────────────────────────────────────────
    tool_call_log: Annotated[List[ToolCallRecord], operator.add]
    agent_messages: Annotated[List[str], operator.add]
    errors: Annotated[List[str], operator.add]

    # ── 控制流 ────────────────────────────────────────────
    current_step: str
    review_complete: bool
