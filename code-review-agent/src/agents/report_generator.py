"""
Report Generator Agent — Day 6。

流程：
  1. 用 LLM 生成执行摘要（综合 Security + Quality findings）
  2. 调用 formatter.format_report() 生成专业 Markdown
  3. 将所有 findings 写入 pgvector 长期记忆
"""
import logging

from langchain_core.messages import HumanMessage, SystemMessage

import src.prompts.report as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.harness.memory.long_term import get_long_term_memory
from src.output.formatter import format_report

logger = logging.getLogger(__name__)


def _build_finding_summary(findings: list[dict], max_items: int = 10) -> str:
    if not findings:
        return "  (none)"
    lines = []
    for f in findings[:max_items]:
        loc = f.get("file", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"  [{f.get('severity','?').upper()}] {f.get('title','?')} — {loc}")
    if len(findings) > max_items:
        lines.append(f"  ... and {len(findings) - max_items} more")
    return "\n".join(lines)


def _generate_executive_summary(
    security_findings: list[dict],
    quality_findings: list[dict],
    diff_files: list[dict],
    pr_metadata: dict,
    repo_name: str,
) -> str:
    """调用 LLM 生成执行摘要，失败时返回模板摘要。"""
    additions = sum(f.get("additions", 0) for f in diff_files)
    deletions = sum(f.get("deletions", 0) for f in diff_files)

    try:
        llm = get_llm(temperature=0.3)
        messages = [
            SystemMessage(content=prompt_tmpl.SYSTEM),
            HumanMessage(content=prompt_tmpl.HUMAN.format(
                repo_name=repo_name,
                pr_title=pr_metadata.get("title", "N/A"),
                file_count=len(diff_files),
                additions=additions,
                deletions=deletions,
                sec_count=len(security_findings),
                security_summary=_build_finding_summary(security_findings),
                qual_count=len(quality_findings),
                quality_summary=_build_finding_summary(quality_findings),
            )),
        ]
        response = llm.invoke(messages)
        summary = response.content.strip() if hasattr(response, "content") else str(response).strip()
        if summary:
            logger.info("[ReportGenerator] LLM 摘要生成成功 (%d chars)", len(summary))
            return summary
    except Exception as exc:
        logger.warning("[ReportGenerator] LLM 摘要生成失败，使用模板摘要: %s", exc)

    # 模板兜底
    critical_count = sum(1 for f in security_findings if f.get("severity") == "critical")
    high_count = sum(
        1 for f in (security_findings + quality_findings)
        if f.get("severity") == "high"
    )
    return (
        f"本次 PR 共涉及 {len(diff_files)} 个文件（+{additions}/-{deletions} 行），"
        f"发现安全问题 {len(security_findings)} 条、质量问题 {len(quality_findings)} 条。"
        + (f" 其中 Critical 级别安全漏洞 {critical_count} 条，需在合并前立即修复。" if critical_count else "")
        + (f" High 级别问题 {high_count} 条，建议本次 PR 一并处理。" if high_count else "")
    )


def report_generator_node(state: ReviewState) -> dict:
    logger.info("[ReportGenerator] 开始生成报告")

    security_findings     = state.get("security_findings", [])
    quality_findings      = state.get("quality_findings", [])
    dependency_findings   = state.get("dependency_findings", [])
    test_coverage_findings = state.get("test_coverage_findings", [])
    diff_files            = state.get("diff_files", [])
    pr_meta               = state.get("pr_metadata", {})
    repo_name             = state.get("repo_name", "unknown")
    tool_call_log         = state.get("tool_call_log", [])

    # Step 1: LLM 生成执行摘要
    executive_summary = _generate_executive_summary(
        security_findings, quality_findings, diff_files, pr_meta, repo_name
    )

    # Step 2: formatter 生成完整 Markdown
    report = format_report(
        security_findings=security_findings,
        quality_findings=quality_findings,
        diff_files=diff_files,
        pr_metadata=pr_meta,
        repo_name=repo_name,
        executive_summary=executive_summary,
        tool_call_log=tool_call_log,
        dependency_findings=dependency_findings,
        test_coverage_findings=test_coverage_findings,
    )

    msg = f"[ReportGenerator] 报告生成完成 ({len(report)} chars)"
    logger.info(msg)

    # Step 3: 写入长期记忆
    all_findings = (
        list(security_findings) + list(quality_findings)
        + list(dependency_findings) + list(test_coverage_findings)
    )
    if all_findings:
        try:
            stored = get_long_term_memory().store(repo_name=repo_name, findings=all_findings)
            logger.info("[ReportGenerator] 长期记忆已写入 %d 条 findings", stored)
        except Exception as exc:
            logger.warning("[ReportGenerator] 长期记忆写入失败（不影响报告）: %s", exc)

    return {
        "final_report":    report,
        "review_complete": True,
        "current_step":    "done",
        "agent_messages":  [msg],
    }
