"""
短期记忆：基于 ReviewState 本身维护 session 级上下文。
LangGraph StateGraph 天然提供状态持久化，本模块提供辅助函数。
"""
from datetime import datetime, timezone
from typing import Any

from src.graph.state import ToolCallRecord


def make_tool_record(
    tool_name: str,
    risk_level: str,
    args: dict[str, Any],
    result: str | None = None,
    approved: bool = True,
) -> ToolCallRecord:
    return {
        "tool_name": tool_name,
        "risk_level": risk_level,
        "args": args,
        "result": result,
        "approved": approved,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
