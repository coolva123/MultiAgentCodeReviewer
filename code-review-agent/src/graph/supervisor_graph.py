"""
外层 Supervisor Graph — 多 Agent 编排主图。

拓扑结构（Hub-and-Spoke，Supervisor 为中心）：

  START
    └─► supervisor ──── Command(goto="research_agent")    ──► research_agent
                   ├─── Command(goto="review_pipeline")   ──► review_pipeline   (子图)
                   ├─── Command(goto="file_review_pipeline") ► file_review_pipeline (子图)
                   └─── Command(goto="report_generator")  ──► report_generator ──► END

  research_agent        ──► supervisor  (固定回边)
  review_pipeline       ──► supervisor  (固定回边)
  file_review_pipeline  ──► supervisor  (固定回边)
"""
import logging

from langgraph.graph import END, START, StateGraph

from src.agents.context_enrichment import context_enrichment_node
from src.agents.report_generator import report_generator_node
from src.agents.research_agent import research_agent_node
from src.agents.supervisor import supervisor_node
from src.graph.review_subgraph import build_file_review_subgraph, build_review_subgraph
from src.graph.state import ReviewState

logger = logging.getLogger(__name__)


def build_supervisor_graph():
    # 编译两个审查子图（不带 checkpointer，由外层管理）
    review_subgraph      = build_review_subgraph()
    file_review_subgraph = build_file_review_subgraph()

    builder = StateGraph(ReviewState)

    # ── 节点注册 ───────────────────────────────────────────────────────────────
    builder.add_node("supervisor",           supervisor_node)
    builder.add_node("research_agent",       research_agent_node)
    builder.add_node("review_pipeline",      review_subgraph)       # 子图作为节点
    builder.add_node("file_review_pipeline", file_review_subgraph)  # 子图作为节点
    builder.add_node("report_generator",     report_generator_node)
    builder.add_node("context_enrichment",   context_enrichment_node)

    # ── 入口 ────────────────────────────────────────────────────────────────────
    builder.add_edge(START, "supervisor")

    # ── 回边：各 Agent/子图完成后回到 Supervisor ────────────────────────────────
    # supervisor 用 Command(goto=...) 控制去向；回边控制"完成后回来"
    builder.add_edge("research_agent",       "supervisor")
    builder.add_edge("review_pipeline",      "supervisor")
    builder.add_edge("file_review_pipeline", "supervisor")
    builder.add_edge("context_enrichment",   "supervisor")

    # ── 终止 ────────────────────────────────────────────────────────────────────
    builder.add_edge("report_generator", END)

    # langgraph dev / LangGraph Cloud 会自己管理持久化，禁止传入自定义 checkpointer；
    # 仅在直接运行（main.py / server.py / pytest）时才挂载 PostgresSaver。
    import sys
    under_langgraph_api = "langgraph_api" in sys.modules

    if under_langgraph_api:
        graph = builder.compile()
        logger.info("[SupervisorGraph] 编译完成（LangGraph API 模式，平台托管持久化）| 节点: %s", list(builder.nodes))
    else:
        try:
            from src.harness.checkpointer import get_checkpointer
            graph = builder.compile(checkpointer=get_checkpointer())
            logger.info("[SupervisorGraph] 编译完成（含 PostgreSQL Checkpointer）| 节点: %s", list(builder.nodes))
        except Exception as exc:
            logger.warning("[SupervisorGraph] Checkpointer 不可用，降级为无持久化模式: %s", exc)
            graph = builder.compile()
            logger.info("[SupervisorGraph] 编译完成（无 Checkpointer）| 节点: %s", list(builder.nodes))
    return graph


supervisor_graph = build_supervisor_graph()
