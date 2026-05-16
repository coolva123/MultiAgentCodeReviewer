"""
Supervisor Agent — 多 Agent 编排核心。

职责：
  - 读取当前 state，用 LLM 决策下一步行动
  - 通过 Command(goto=...) 路由到目标节点
  - 维护迭代计数，防止无限循环

路由目标：
  - "research_agent"       : 调用研究专家
  - "review_pipeline"      : 调用完整审查子图（PR/diff 模式）
  - "file_review_pipeline" : 调用简化审查子图（文件上传模式）
  - "report_generator"     : 生成最终报告（终止循环）
"""
import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

import src.prompts.supervisor as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.tools.llm_utils import call_structured

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 5


class SupervisorDecision(BaseModel):
    action: Literal["research", "review", "file_review", "report"] = Field(
        description=(
            "Next action: "
            "'research' = call Research Agent, "
            "'review' = call Review Pipeline (PR/diff mode), "
            "'file_review' = call File Review Pipeline (upload mode), "
            "'report' = generate final report (terminates the loop)"
        )
    )
    instruction: str = Field(
        description="Specific, focused instruction for the chosen agent or pipeline (1-3 sentences)."
    )
    reasoning: str = Field(
        description="One-sentence explanation of why this action was chosen."
    )


def supervisor_node(state: ReviewState) -> Command:
    iteration = state.get("iteration_count", 0)
    repo_name = state.get("repo_name", "unknown")
    repo_url = state.get("repo_url", "")
    diff_content = state.get("diff_content", "")
    diff_files = state.get("diff_files", [])
    research_context = state.get("research_context", "")
    security_findings = state.get("security_findings", [])
    quality_findings = state.get("quality_findings", [])
    agent_messages = state.get("agent_messages", [])

    has_diff = bool(diff_content and diff_content.strip())
    has_files = bool(diff_files)
    # review_pipeline_called 用于判断 review 是否已执行（独立于 findings 数量）
    review_called = state.get("review_pipeline_called", False)
    has_findings = bool(security_findings or quality_findings)
    mode = "pr_diff" if has_diff else ("file_upload" if has_files else "unknown")

    logger.info(
        "[Supervisor] 迭代 %d | mode=%s | research=%s | review_called=%s | findings=%s | sec=%d | qual=%d",
        iteration, mode, bool(research_context), review_called, has_findings,
        len(security_findings), len(quality_findings),
    )

    # 硬限制：超过最大迭代直接生成报告
    if iteration >= _MAX_ITERATIONS:
        logger.warning("[Supervisor] 达到最大迭代数 %d，强制生成报告", _MAX_ITERATIONS)
        return Command(
            goto="report_generator",
            update={
                "iteration_count": iteration + 1,
                "agent_messages": [f"[Supervisor] 强制终止循环（iteration={iteration}），生成报告"],
            },
        )

    # 构建 LLM 输入
    recent_msgs = agent_messages[-6:] if len(agent_messages) > 6 else agent_messages
    research_snippet = (
        research_context[:800] + "\n...[truncated]"
        if len(research_context) > 800
        else research_context or "None yet."
    )

    messages = [
        SystemMessage(content=prompt_tmpl.SYSTEM),
        HumanMessage(content=prompt_tmpl.HUMAN.format(
            repo_name=repo_name,
            repo_url=repo_url or "not provided",
            mode=mode,
            iteration_count=iteration,
            research_context=research_snippet,
            sec_count=len(security_findings),
            qual_count=len(quality_findings),
            review_called=review_called,
            has_findings=has_findings,
            recent_messages="\n".join(recent_msgs) if recent_msgs else "None",
        )),
    ]

    decision: SupervisorDecision | None = None
    try:
        llm = get_llm(temperature=0.1)
        decision = call_structured(llm, messages, SupervisorDecision)
    except Exception as exc:
        logger.error("[Supervisor] LLM 决策失败: %s", exc)

    # LLM 失败时的保守回退
    if decision is None:
        if not review_called:
            action = "file_review" if (not has_diff and has_files) else "review"
            instruction = "Perform code review with default settings."
            reasoning = "Fallback: LLM unavailable, defaulting to review."
        else:
            action = "report"
            instruction = "Generate final report from available findings."
            reasoning = "Fallback: LLM unavailable, generating report."
        decision = SupervisorDecision(action=action, instruction=instruction, reasoning=reasoning)  # type: ignore[arg-type]

    logger.info(
        "[Supervisor] 决策: action=%s | reasoning=%s",
        decision.action, decision.reasoning,
    )

    update: dict = {
        "iteration_count": iteration + 1,
        "supervisor_instruction": decision.instruction,
        "agent_messages": [
            f"[Supervisor] iter={iteration} action={decision.action} | {decision.reasoning}"
        ],
    }

    # 派发 review 时打标记，后续轮次不会误判为"未执行"
    if decision.action in ("review", "file_review"):
        update["review_pipeline_called"] = True

    goto_map = {
        "research":    "research_agent",
        "review":      "review_pipeline",
        "file_review": "file_review_pipeline",
        "report":      "report_generator",
    }
    return Command(goto=goto_map[decision.action], update=update)
