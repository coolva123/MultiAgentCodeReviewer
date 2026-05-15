import logging

from langgraph.graph import END, START, StateGraph

from src.agents.coordinator import coordinator_node
from src.agents.diff_analyzer import diff_analyzer_node
from src.agents.quality_reviewer import quality_reviewer_node
from src.agents.report_generator import report_generator_node
from src.agents.security_reviewer import security_reviewer_node
from src.graph.edges import route_after_coordinator
from src.graph.state import ReviewState
from src.harness.checkpointer import get_checkpointer

logger = logging.getLogger(__name__)


def build_graph():
    """
    节点执行顺序：
      START
        └─► diff_analyzer          # 先解析，生成结构化语义信息
              └─► coordinator      # 再基于语义信息做路由决策
                    ├─► security_reviewer ─┐
                    └─► quality_reviewer  ─┤  (并行 fan-out)
                                           └─► report_generator ─► END
    """
    builder = StateGraph(ReviewState)

    builder.add_node("diff_analyzer",      diff_analyzer_node)
    builder.add_node("coordinator",        coordinator_node)
    builder.add_node("security_reviewer",  security_reviewer_node)
    builder.add_node("quality_reviewer",   quality_reviewer_node)
    builder.add_node("report_generator",   report_generator_node)

    # ── 固定边 ────────────────────────────────────────────
    builder.add_edge(START, "diff_analyzer")
    builder.add_edge("diff_analyzer", "coordinator")

    # ── Coordinator → 条件 fan-out ────────────────────────
    builder.add_conditional_edges(
        "coordinator",
        route_after_coordinator,
        {
            "security_reviewer":  "security_reviewer",
            "quality_reviewer":   "quality_reviewer",
            "report_generator":   "report_generator",
            "__end__": END,
        },
    )

    builder.add_edge("security_reviewer", "report_generator")
    builder.add_edge("quality_reviewer",  "report_generator")
    builder.add_edge("report_generator",  END)

    checkpointer = get_checkpointer()
    graph = builder.compile(checkpointer=checkpointer)
    logger.info("[Graph] 编译完成，节点: %s", list(builder.nodes))
    return graph


review_graph = build_graph()
