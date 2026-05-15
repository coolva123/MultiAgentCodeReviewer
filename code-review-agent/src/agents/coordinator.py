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

class RoutingDecision(BaseModel):
    run_security: bool = Field(description="Whether to activate the Security Reviewer")
    run_security_reason: str = Field(description="One-sentence reason for the security decision")
    run_quality: bool = Field(description="Whether to activate the Quality Reviewer")
    run_quality_reason: str = Field(description="One-sentence reason for the quality decision")
    priority: Literal["high", "medium", "low"] = Field(description="Overall review priority")
    focus_files: list[str] = Field(description="Filenames that deserve the most attention (max 5)")
    overall_assessment: str = Field(description="2-3 sentence coordinator assessment of this PR")


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

    if decision:
        routing_decision = {
            "run_security": decision.run_security,
            "run_quality": decision.run_quality,
            "priority": decision.priority,
            "focus_files": decision.focus_files,
            "security_reason": decision.run_security_reason,
            "quality_reason": decision.run_quality_reason,
            "overall_assessment": decision.overall_assessment,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[Coordinator] 决策完成 | security=%s | quality=%s | priority=%s",
            decision.run_security,
            decision.run_quality,
            decision.priority,
        )
    else:
        # LLM 失败时：保守策略，全部启用
        routing_decision = {
            "run_security": True,
            "run_quality": True,
            "priority": "medium",
            "focus_files": [],
            "security_reason": "Fallback: LLM unavailable, enabling all reviewers",
            "quality_reason": "Fallback: LLM unavailable, enabling all reviewers",
            "overall_assessment": "LLM routing failed, proceeding with full review.",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning("[Coordinator] 使用保守默认路由策略")

    msg = (
        f"[Coordinator] security={routing_decision['run_security']} "
        f"| quality={routing_decision['run_quality']} "
        f"| priority={routing_decision['priority']} "
        f"| focus={routing_decision['focus_files']}"
    )

    return {
        "routing_decision": routing_decision,
        "current_step": "coordinator_done",
        "agent_messages": [msg],
    }
