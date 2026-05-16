"""
Research Expert Agent。

工具：tavily_search / fetch_repo_readme / fetch_repo_structure / query_long_term_memory
流程：
  1. 读取 Supervisor 指令
  2. ReAct 循环：最多 5 次工具调用
  3. 生成结构化研究摘要
  4. 将结果追加到 state.research_context
"""
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

import src.prompts.research as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.tools.llm_utils import strip_reasoning
from src.tools.research_tools import (
    fetch_repo_readme,
    fetch_repo_structure,
    query_long_term_memory,
    tavily_search,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 5
_RESEARCH_TOOLS = [tavily_search, fetch_repo_readme, fetch_repo_structure, query_long_term_memory]


def research_agent_node(state: ReviewState) -> dict:
    """Research Expert Agent: gathers background context using web search and memory."""
    instruction = state.get("supervisor_instruction") or "Gather general background on this repository and its tech stack."
    repo_name = state.get("repo_name", "unknown")
    repo_url = state.get("repo_url", "")

    logger.info("[ResearchAgent] 开始 | instruction=%s", instruction[:80])

    llm = get_llm(temperature=0.1)
    tool_map = {t.name: t for t in _RESEARCH_TOOLS}
    llm_with_tools = llm.bind_tools(_RESEARCH_TOOLS)

    system_msg = SystemMessage(content=prompt_tmpl.SYSTEM)
    human_msg = HumanMessage(content=prompt_tmpl.HUMAN.format(
        instruction=instruction,
        repo_name=repo_name,
        repo_url=repo_url or "not provided",
    ))
    messages = [system_msg, human_msg]

    for iteration in range(_MAX_ITERATIONS):
        try:
            response = llm_with_tools.invoke(strip_reasoning(messages))
        except Exception as exc:
            logger.error("[ResearchAgent] LLM 调用失败 iteration=%d: %s", iteration, exc)
            break

        tool_calls = getattr(response, "tool_calls", [])
        if not tool_calls:
            logger.info("[ResearchAgent] Agent loop 完成，共 %d 轮", iteration + 1)
            break

        messages.append(response)
        for tc in tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                tool_result = json.dumps({"error": f"unknown tool: {tc['name']}"})
            else:
                try:
                    raw = tool_fn.invoke(tc["args"])
                    tool_result = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                except Exception as exc:
                    tool_result = f"Tool error: {exc}"

            logger.info("[ResearchAgent] 工具 %s 调用完成，结果长度=%d", tc["name"], len(tool_result))
            messages.append(ToolMessage(content=tool_result[:2000], tool_call_id=tc["id"]))
    else:
        logger.warning("[ResearchAgent] 达到最大迭代次数 (%d)", _MAX_ITERATIONS)

    # 生成研究摘要
    messages.append(HumanMessage(content=(
        "Based on the tool results above, write a concise structured research report. "
        "Use clear sections (## Tech Stack, ## Known Issues, ## CVE References, ## Security Recommendations). "
        "Keep it under 800 words."
    )))
    try:
        summary_response = get_llm(temperature=0.2).invoke(strip_reasoning(messages))
        research_result = (
            summary_response.content
            if hasattr(summary_response, "content")
            else str(summary_response)
        )
    except Exception as exc:
        logger.warning("[ResearchAgent] 摘要生成失败: %s", exc)
        research_result = f"Research summary failed: {exc}"

    msg = f"[ResearchAgent] 完成 | result_length={len(research_result)}"
    logger.info(msg)

    # 累积追加到 research_context
    existing = state.get("research_context", "")
    task_label = instruction[:60].replace("\n", " ")
    if existing:
        new_context = f"{existing}\n\n---\n### Research Round — {task_label}\n{research_result}"
    else:
        new_context = f"### Research Context — {task_label}\n{research_result}"

    return {
        "research_context": new_context,
        "agent_messages": [msg],
    }
