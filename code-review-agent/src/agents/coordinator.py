import logging
from datetime import datetime, timezone
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

import src.prompts.coordinator as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.tools.llm_utils import call_structured

logger = logging.getLogger(__name__)


# ── Pydantic 结构化输出模型 ────────────────────────────────────────────────────

_DEP_FILENAMES = {"requirements.txt", "pyproject.toml", "package.json", "go.mod", "Pipfile"}


class RoutingDecision(BaseModel):
    run_security: bool = Field(description="是否启动安全审查 Agent")
    run_security_reason: str = Field(description="用中文一句话说明安全审查的决策原因")
    run_quality: bool = Field(description="是否启动质量审查 Agent")
    run_quality_reason: str = Field(description="用中文一句话说明质量审查的决策原因")
    run_dependency: bool = Field(description="是否启动依赖安全审查 Agent（diff 含依赖文件变更时为 true）")
    run_test_coverage: bool = Field(description="是否启动测试覆盖审查 Agent（有功能性代码变更且 PR 中测试文件较少时为 true）")
    priority: Literal["high", "medium", "low"] = Field(description="本次审查的整体优先级")
    focus_files: list[str] = Field(description="最值得重点关注的文件列表（最多 5 个）")
    overall_assessment: str = Field(description="用中文 2-3 句话对本次 PR 做整体评估")


# ── LLM 路由决策 ──────────────────────────────────────────────────────────────

def _llm_routing(state: ReviewState) -> RoutingDecision | None:
    diff_files = state.get("diff_files", [])
    diff_summary = state.get("diff_summary", {})
    repo_name = state.get("repo_name", "unknown")

    # 整理文件语义信息供 LLM 参考
    file_lines = []
    for f in diff_files:
        parts = [f"- {f['filename']} ({f.get('change_category', '?')})"]
        if f.get("is_security_sensitive"):
            parts.append("[SECURITY-SENSITIVE]")
        if f.get("is_complex_logic"):
            parts.append("[COMPLEX-LOGIC]")
        summary = f.get("file_summary", "")
        if summary:
            parts.append(f"→ {summary}")
        file_lines.append(" ".join(parts))

    file_analysis = "\n".join(file_lines) if file_lines else "No file analysis available."

    prompt = ChatPromptTemplate.from_messages([
        ("system", prompt_tmpl.SYSTEM),
        ("human", prompt_tmpl.HUMAN),
    ])

    try:
        llm = get_llm(temperature=0.1)
        messages = prompt.format_messages(
            repo_name=repo_name,
            pr_nature=diff_summary.get("pr_nature", "unknown"),
            estimated_risk=diff_summary.get("estimated_risk", "unknown"),
            overall_summary=diff_summary.get("overall_summary", "No summary available."),
            file_analysis=file_analysis,
        )
        return call_structured(llm, messages, RoutingDecision)
    except Exception as e:
        logger.error("[Coordinator] LLM 调用失败，使用保守默认值: %s", e)
        return None


# ── 节点函数 ──────────────────────────────────────────────────────────────────

def coordinator_node(state: ReviewState) -> dict:
    """
    Coordinator Agent：
      基于 DiffAnalyzer 的语义输出，用 LLM 决策：
      - 启用哪些 Reviewer
      - 审查优先级
      - 重点关注哪些文件
    """
    logger.info("[Coordinator] 开始路由决策")

    decision = _llm_routing(state)

    # 静态判断：diff_files 是否含依赖文件变更（不依赖 LLM，始终准确）
    diff_files = state.get("diff_files", [])
    has_dep_changes = any(
        any(dep in f["filename"] for dep in _DEP_FILENAMES)
        for f in diff_files
    )
    test_files_in_pr = sum(1 for f in diff_files if "test" in f["filename"].lower())
    has_business_changes = any(
        f.get("change_category") in ("feature", "bugfix", "refactor", "security")
        for f in diff_files
    )

    if decision:
        routing_decision = {
            "run_security":      decision.run_security,
            "run_quality":       decision.run_quality,
            "run_dependency":    decision.run_dependency or has_dep_changes,
            "run_test_coverage": decision.run_test_coverage and has_business_changes and test_files_in_pr == 0,
            "priority":          decision.priority,
            "focus_files":       decision.focus_files,
            "security_reason":   decision.run_security_reason,
            "quality_reason":    decision.run_quality_reason,
            "overall_assessment": decision.overall_assessment,
            "decided_at":        datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[Coordinator] 决策完成 | security=%s | quality=%s | dependency=%s | test_coverage=%s | priority=%s",
            decision.run_security, decision.run_quality,
            routing_decision["run_dependency"], routing_decision["run_test_coverage"],
            decision.priority,
        )
    else:
        # LLM 失败时：保守策略
        routing_decision = {
            "run_security":      True,
            "run_quality":       True,
            "run_dependency":    has_dep_changes,
            "run_test_coverage": False,
            "priority":          "medium",
            "focus_files":       [],
            "security_reason":   "降级：LLM 不可用，启用全部审查",
            "quality_reason":    "降级：LLM 不可用，启用全部审查",
            "overall_assessment": "LLM 路由决策失败，使用保守策略继续审查。",
            "decided_at":        datetime.now(timezone.utc).isoformat(),
        }
        logger.warning("[Coordinator] 使用保守默认路由策略")

    msg = (
        f"[Coordinator] security={routing_decision['run_security']} "
        f"| quality={routing_decision['run_quality']} "
        f"| dependency={routing_decision['run_dependency']} "
        f"| test_coverage={routing_decision['run_test_coverage']} "
        f"| priority={routing_decision['priority']} "
        f"| focus={routing_decision['focus_files']}"
    )

    return {
        "routing_decision": routing_decision,
        "current_step": "coordinator_done",
        "agent_messages": [msg],
    }
