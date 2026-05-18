"""
Security Reviewer Agent — Day 3 + TODO-SEC-01 (Semgrep MCP / 本地 subprocess 双模式).

工具加载策略（懒加载，每次节点执行时确定）：
  - 优先：Semgrep MCP Server 工具（security_check + semgrep_scan 等）+ scan_secrets
  - 降级：本地 subprocess semgrep_scan + scan_secrets（MCP 不可用时自动切换）

Agent loop:
  1. 查询长期记忆，将同仓库历史 findings 注入 Prompt 作为额外上下文
  2. LLM with bound tools 决定调用哪些工具
  3. 所有工具调用经由 Tool Guard 路由（风险分级 + HITL）
  4. 工具结果喂回对话
  5. call_structured 提取结构化 findings
  Fallback: 静态运行本地工具 → LLM 分析。
"""
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

import src.prompts.security as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewIssue, ReviewState, ToolCallRecord
from src.harness.memory.long_term import get_long_term_memory
from src.harness.tool_guard import guarded_call
from src.tools.code_analysis import scan_secrets, semgrep_scan
from src.tools.llm_utils import call_structured, strip_reasoning

logger = logging.getLogger(__name__)

_MAX_AGENT_ITERATIONS = 4
_MAX_PATCH_CHARS = 2000


def _get_security_tools() -> tuple[list, dict]:
    """
    懒加载工具列表：优先使用 MCP 工具，MCP 不可用时降级到本地 subprocess 工具。
    返回 (tools_list, tool_map)。

    MCP semgrep_scan 要求磁盘上真实文件的绝对路径，不适合分析 diff patch 中的内联代码。
    因此始终用本地 semgrep_scan（接受 source_code 字符串）替代 MCP 同名工具。
    """
    try:
        from config.mcp_client import get_semgrep_mcp_tools
        mcp_tools = get_semgrep_mcp_tools()
        if mcp_tools:
            # 过滤掉 MCP 的 semgrep_scan（需要绝对路径），保留其余 MCP 工具
            filtered = [t for t in mcp_tools if t.name != "semgrep_scan"]
            tools = filtered + [semgrep_scan, scan_secrets]
            logger.info(
                "[SecurityReviewer] 使用 MCP 工具 (%d 个) + semgrep_scan(本地) + scan_secrets",
                len(filtered),
            )
            return tools, {t.name: t for t in tools}
    except Exception as exc:
        logger.warning("[SecurityReviewer] MCP 工具加载失败: %s", exc)

    # 降级：本地 subprocess 工具
    tools = [semgrep_scan, scan_secrets]
    logger.info("[SecurityReviewer] 使用本地工具（MCP 不可用）")
    return tools, {t.name: t for t in tools}


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _SecurityFinding(BaseModel):
    file: str = Field(description="发现问题的文件路径")
    line: Optional[int] = Field(None, description="问题所在行号，未知时填 null")
    severity: str = Field(description="严重等级：critical | high | medium | low | info")
    category: str = Field(description="问题类别，例如：hardcoded_secret、sql_injection、insecure_deserialization")
    title: str = Field(description="问题标题，请用中文简洁描述（不超过 40 字）")
    description: str = Field(description="用中文详细说明漏洞原因及危害")
    suggestion: str = Field(description="用中文给出具体可执行的修复建议")


class _SecurityFindings(BaseModel):
    findings: list[_SecurityFinding] = Field(default_factory=list)
    overall_risk: str = Field(
        description="整体风险等级：critical | high | medium | low | none",
        default="none",
    )
    summary: str = Field(description="用中文一句话概括本次 PR 的安全态势")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_added_code(patch: str) -> str:
    return "\n".join(
        line[1:] for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _build_file_summaries(diff_files: list[dict]) -> str:
    parts = []
    for f in diff_files:
        header = (
            f"### {f['filename']}  "
            f"[{f.get('change_type','?')}]  "
            f"category={f.get('change_category','?')}  "
            f"security_sensitive={f.get('is_security_sensitive', False)}"
        )
        patch = f.get("patch", "")[:_MAX_PATCH_CHARS]
        parts.append(f"{header}\n```diff\n{patch}\n```")
    return "\n\n".join(parts)


def _build_human_content(routing: dict, diff_files: list[dict], historical: list[str]) -> str:
    base = prompt_tmpl.HUMAN.format(
        priority=routing.get("priority", "medium"),
        focus_files=", ".join(routing.get("focus_files", [])) or "all files",
        file_summaries=_build_file_summaries(diff_files),
    )
    if historical:
        context_block = "\n".join(f"  • {h}" for h in historical)
        base += (
            f"\n\n=== 历史审查记录（同仓库，相似发现）===\n"
            f"{context_block}\n"
            "以上为参考模式——除非在本次新增代码中发现完全相同的问题，否则不要重复上报。"
        )
    return base


def _run_static_tools(
    diff_files: list[dict],
    tool_records: list[ToolCallRecord],
) -> dict[str, dict]:
    """Fallback: run semgrep_scan (p/security) + scan_secrets via Tool Guard."""
    results: dict[str, dict] = {}
    for f in diff_files:
        patch = f.get("patch", "")
        filename = f["filename"]
        added_code = _extract_added_code(patch)
        if not added_code.strip():
            results[filename] = {}
            continue

        file_results: dict = {}

        raw, rec = guarded_call(semgrep_scan, "semgrep_scan", {
            "source_code": added_code, "filename": filename, "config": "p/security",
        })
        tool_records.append(rec)
        file_results["semgrep"] = json.loads(raw) if raw else {}

        raw, rec = guarded_call(scan_secrets, "scan_secrets", {"patch": patch})
        tool_records.append(rec)
        file_results["secrets"] = json.loads(raw) if raw else {}

        results[filename] = file_results
        logger.info(
            "[SecurityReviewer] static scan: %s  semgrep=%s  secrets=%s",
            filename,
            file_results.get("semgrep", {}).get("total", 0),
            file_results.get("secrets", {}).get("total", 0),
        )
    return results


def _convert_to_review_issues(findings_model: _SecurityFindings) -> list[ReviewIssue]:
    return [
        {
            "file":        f.file,
            "line":        f.line,
            "severity":    f.severity,
            "category":    f.category,
            "title":       f.title,
            "description": f.description,
            "suggestion":  f.suggestion,
        }
        for f in findings_model.findings
    ]


# ── Agent loop (primary path) ──────────────────────────────────────────────────

def _agent_loop(
    diff_files: list[dict],
    routing_decision: dict,
    historical: list[str],
    tool_records: list[ToolCallRecord],
) -> _SecurityFindings | None:
    llm = get_llm(temperature=0.1)
    tools, tool_map = _get_security_tools()
    llm_with_tools = llm.bind_tools(tools)

    system_msg = SystemMessage(content=prompt_tmpl.SYSTEM)
    human_content = _build_human_content(routing_decision, diff_files, historical)
    human_msg = HumanMessage(content=human_content)
    messages = [system_msg, human_msg]

    for iteration in range(_MAX_AGENT_ITERATIONS):
        try:
            response = llm_with_tools.invoke(strip_reasoning(messages))
        except Exception as exc:
            logger.debug("[SecurityReviewer] Loop LLM call failed at iteration %d: %s", iteration, exc)
            break

        tool_calls = getattr(response, "tool_calls", [])
        if not tool_calls:
            logger.info("[SecurityReviewer] Agent loop finished after %d iteration(s)", iteration + 1)
            break

        messages.append(response)
        for tc in tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                tool_result = json.dumps({"error": f"unknown tool: {tc['name']}"})
            else:
                raw, record = guarded_call(tool_fn, tc["name"], tc["args"])
                tool_records.append(record)
                tool_result = str(raw) if raw is not None else json.dumps({"error": "tool rejected or failed"})

            messages.append(ToolMessage(content=tool_result, tool_call_id=tc["id"]))
    else:
        logger.warning("[SecurityReviewer] Reached max agent iterations (%d)", _MAX_AGENT_ITERATIONS)

    return call_structured(llm, messages, _SecurityFindings)


# ── Fallback: static tools → LLM ──────────────────────────────────────────────

def _static_plus_llm(
    diff_files: list[dict],
    routing_decision: dict,
    historical: list[str],
    tool_records: list[ToolCallRecord],
) -> _SecurityFindings | None:
    static_results = _run_static_tools(diff_files, tool_records)

    file_summaries_parts = []
    for f in diff_files:
        results = static_results.get(f["filename"], {})
        patch = f.get("patch", "")[:_MAX_PATCH_CHARS]
        tool_output = json.dumps(results, indent=2)[:800]
        file_summaries_parts.append(
            f"### {f['filename']}\n"
            f"```diff\n{patch}\n```\n"
            f"**Static Analysis Results:**\n```json\n{tool_output}\n```"
        )

    llm = get_llm(temperature=0.1)
    focus_files = routing_decision.get("focus_files", [])

    human_content = prompt_tmpl.HUMAN_WITH_TOOL_RESULTS.format(
        priority=routing_decision.get("priority", "medium"),
        focus_files=", ".join(focus_files) if focus_files else "all files",
        file_summaries="\n\n".join(file_summaries_parts),
    )
    if historical:
        context_block = "\n".join(f"  • {h}" for h in historical)
        human_content += (
            f"\n\n=== 历史审查记录 ===\n{context_block}\n"
        )

    messages = [
        SystemMessage(content=prompt_tmpl.SYSTEM),
        HumanMessage(content=human_content),
    ]
    return call_structured(llm, messages, _SecurityFindings)


# ── Node entry point ───────────────────────────────────────────────────────────

def security_reviewer_node(state: ReviewState) -> dict:
    logger.info("[SecurityReviewer] 开始安全审查 (agent + tools)")

    diff_files = state.get("diff_files", [])
    routing    = state.get("routing_decision", {})
    repo_name  = state.get("repo_name", "unknown")

    if not diff_files:
        logger.warning("[SecurityReviewer] 没有文件需要分析")
        return {"security_findings": [], "agent_messages": ["[SecurityReviewer] no files to analyze"]}

    # Day 5: 查询长期记忆，获取历史相似 findings
    historical: list[str] = []
    try:
        filenames = [f["filename"] for f in diff_files]
        query_text = f"Security issues in {', '.join(filenames)}"
        historical = get_long_term_memory().query(repo_name=repo_name, query_text=query_text, top_k=5)
        if historical:
            logger.info("[SecurityReviewer] 获取到 %d 条历史上下文", len(historical))
    except Exception as exc:
        logger.warning("[SecurityReviewer] 长期记忆查询失败（跳过）: %s", exc)

    tool_records: list[ToolCallRecord] = []
    findings_model: _SecurityFindings | None = None

    try:
        findings_model = _agent_loop(diff_files, routing, historical, tool_records)
        logger.info("[SecurityReviewer] Agent loop 完成")
    except Exception as exc:
        logger.warning("[SecurityReviewer] Agent loop 失败，切换到静态扫描模式: %s", exc)

    if findings_model is None:
        try:
            findings_model = _static_plus_llm(diff_files, routing, historical, tool_records)
        except Exception as exc:
            logger.error("[SecurityReviewer] Fallback also failed: %s", exc)

    if findings_model is None:
        return {
            "security_findings": [],
            "tool_call_log":     tool_records,
            "errors":            ["[SecurityReviewer] Both agent loop and fallback failed"],
            "agent_messages":    ["[SecurityReviewer] 审查失败"],
        }

    issues = _convert_to_review_issues(findings_model)
    critical_count = sum(1 for i in issues if i["severity"] in ("critical", "high"))
    msg = (
        f"[SecurityReviewer] 完成 | risk={findings_model.overall_risk} "
        f"| findings={len(issues)} | critical/high={critical_count}"
    )
    logger.info(msg)

    return {
        "security_findings": issues,
        "tool_call_log":     tool_records,
        "agent_messages":    [msg],
    }
