from .state import ReviewState


def route_after_coordinator(state: ReviewState) -> list[str]:
    """
    Coordinator 完成后的 fan-out 路由。
    返回列表时 LangGraph 并行触发所有目标节点。
    """
    decision = state.get("routing_decision", {})

    # diff 为空且有错误时提前终止
    if not state.get("diff_files") and state.get("errors"):
        return ["__end__"]

    targets = []
    if decision.get("run_security", True):
        targets.append("security_reviewer")
    if decision.get("run_quality", True):
        targets.append("quality_reviewer")
    if decision.get("run_dependency", False):
        targets.append("dependency_reviewer")
    if decision.get("run_test_coverage", False):
        targets.append("test_coverage_reviewer")

    # 全部跳过（纯文档变更）时直接生成报告
    return targets if targets else ["report_generator"]
