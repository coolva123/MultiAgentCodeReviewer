"""
审查子图（Review Subgraph）— 作为节点嵌入外层 Supervisor Graph。

build_review_subgraph()      : 完整流程（PR/diff 模式）
  START → diff_analyzer → coordinator → [security_reviewer | quality_reviewer] 并行 → END

build_file_review_subgraph() : 简化流程（文件上传模式）
  START → [security_reviewer | quality_reviewer] 并行 → END

两个子图编译后不附加 checkpointer（checkpointer 由外层 Supervisor Graph 管理）。
"""
import logging

from langgraph.graph import END, START, StateGraph

from src.agents.coordinator import coordinator_node
from src.agents.diff_analyzer import diff_analyzer_node
from src.agents.quality_reviewer import quality_reviewer_node
from src.agents.security_reviewer import security_reviewer_node
from src.graph.edges import route_after_coordinator
from src.graph.state import ReviewState

logger = logging.getLogger(__name__)


def build_review_subgraph():
    """完整审查流程：diff_analyzer → coordinator → [security | quality] 并行。"""
    builder = StateGraph(ReviewState)

    builder.add_node("diff_analyzer",     diff_analyzer_node)
    builder.add_node("coordinator",       coordinator_node)
    builder.add_node("security_reviewer", security_reviewer_node)
    builder.add_node("quality_reviewer",  quality_reviewer_node)

    builder.add_edge(START, "diff_analyzer")
    builder.add_edge("diff_analyzer", "coordinator")

    builder.add_conditional_edges(
        "coordinator",
        route_after_coordinator,
        {
            "security_reviewer": "security_reviewer",
            "quality_reviewer":  "quality_reviewer",
            "report_generator":  END,   # 边缘情况：coordinator 决定跳过所有 reviewer
            "__end__":           END,
        },
    )

    builder.add_edge("security_reviewer", END)
    builder.add_edge("quality_reviewer",  END)

    subgraph = builder.compile()
    logger.info("[ReviewSubgraph] 编译完成（完整审查流程）")
    return subgraph


def build_file_review_subgraph():
    """简化审查流程：直接并行跑 security + quality（跳过 diff_analyzer 和 coordinator）。"""
    builder = StateGraph(ReviewState)

    builder.add_node("security_reviewer", security_reviewer_node)
    builder.add_node("quality_reviewer",  quality_reviewer_node)

    def _parallel_dispatch(state: ReviewState) -> list[str]:
        return ["security_reviewer", "quality_reviewer"]

    builder.add_conditional_edges(
        START,
        _parallel_dispatch,
        {
            "security_reviewer": "security_reviewer",
            "quality_reviewer":  "quality_reviewer",
        },
    )

    builder.add_edge("security_reviewer", END)
    builder.add_edge("quality_reviewer",  END)

    subgraph = builder.compile()
    logger.info("[FileReviewSubgraph] 编译完成（简化审查流程）")
    return subgraph
