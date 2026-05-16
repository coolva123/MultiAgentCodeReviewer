"""
Quality Reviewer Agent — Day 3 + TODO-QUAL-01 (Semgrep MCP / 本地 subprocess 双模式).

工具加载策略（懒加载，每次节点执行时确定）：
  - 优先：Semgrep MCP Server 工具 + ast_analyze + ruff_check
  - 降级：本地 subprocess semgrep_scan + ast_analyze + ruff_check（MCP 不可用时自动切换）

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

import src.prompts.quality as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewIssue, ReviewState, ToolCallRecord
from src.harness.memory.long_term import get_long_term_memory
from src.harness.tool_guard import guarded_call
from src.tools.code_analysis import ast_analyze, ruff_check, semgrep_scan
from src.tools.llm_utils import call_structured, strip_reasoning

logger = logging.getLogger(__name__)

_MAX_AGENT_ITERATIONS = 4
_MAX_PATCH_CHARS = 2000


def _get_quality_tools() -> tuple[list, dict]:
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
            tools = filtered + [semgrep_scan, ast_analyze, ruff_check]
            logger.info(
                "[QualityReviewer] 使用 MCP 工具 (%d 个) + semgrep_scan(本地) + ast_analyze + ruff_check",
                len(filtered),
            )
            return tools, {t.name: t for t in tools}
    except Exception as exc:
        logger.warning("[QualityReviewer] MCP 工具加载失败: %s", exc)

    # 降级：本地 subprocess 工具
    tools = [ast_analyze, semgrep_scan, ruff_check]
    logger.info("[QualityReviewer] 使用本地工具（MCP 不可用）")
    return tools, {t.name: t for t in tools}


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _QualityFinding(BaseModel):
    file: str = Field(description="Filename where the issue was found")
    line: Optional[int] = Field(None, description="Line number, or null if unknown")
    severity: str = Field(description="high | medium | low | info")
    category: str = Field(
        description="e.g. complexity, naming, error_handling, duplication, test_quality, dead_code, performance"
    )
    title: str = Field(description="Short issue title (< 80 chars)")
    description: str = Field(description="Explanation of the quality problem")
    suggestion: str = Field(description="Concrete refactoring or improvement suggestion")


class _QualityFindings(BaseModel):
    findings: list[_QualityFinding] = Field(default_factory=list)
    overall_quality: str = Field(
        description="Overall quality score: poor | fair | good | excellent",
        default="good",
    )
    summary: str = Field(description="One-sentence summary of the code quality of this PR")


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
            f"complex_logic={f.get('is_complex_logic', False)}"
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
            f"\n\n=== HISTORICAL CONTEXT (same repo, similar findings) ===\n"
            f"{context_block}\n"
            "Use the above as reference patterns — do not re-report them unless you see the same issue in the NEW diff."
        )
    return base


def _run_static_tools(
    diff_files: list[dict],
    tool_records: list[ToolCallRecord],
) -> dict[str, dict]:
    """Fallback: run ast_analyze + semgrep_scan (p/maintainability) + ruff_check via Tool Guard."""
    results: dict[str, dict] = {}
    for f in diff_files:
        filename = f["filename"]
        added_code = _extract_added_code(f.get("patch", ""))
        if not added_code.strip():
            results[filename] = {}
            continue

        file_results: dict = {}

        if filename.endswith(".py"):
            raw, record = guarded_call(
                ast_analyze, "ast_analyze",
                {"source_code": added_code, "filename": filename},
            )
            tool_records.append(record)
            file_results["ast"] = json.loads(raw) if raw else {}

        raw, record = guarded_call(
            semgrep_scan, "semgrep_scan",
            {"source_code": added_code, "filename": filename, "config": "p/maintainability"},
        )
        tool_records.append(record)
        file_results["semgrep"] = json.loads(raw) if raw else {}

        if filename.endswith(".py"):
            raw, record = guarded_call(
                ruff_check, "ruff_check",
                {"source_code": added_code, "filename": filename},
            )
            tool_records.append(record)
            file_results["ruff"] = json.loads(raw) if raw else {}

        results[filename] = file_results
        logger.info(
            "[QualityReviewer] static scan: %s  semgrep=%s  ruff=%s",
            filename,
            file_results.get("semgrep", {}).get("total", 0),
            file_results.get("ruff", {}).get("total", 0),
        )
    return results


def _convert_to_review_issues(findings_model: _QualityFindings) -> list[ReviewIssue]:
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
) -> _QualityFindings | None:
    llm = get_llm(temperature=0.1)
    tools, tool_map = _get_quality_tools()
    llm_with_tools = llm.bind_tools(tools)

    system_msg = SystemMessage(content=prompt_tmpl.SYSTEM)
    human_content = _build_human_content(routing_decision, diff_files, historical)
    human_msg = HumanMessage(content=human_content)
    messages = [system_msg, human_msg]

    for iteration in range(_MAX_AGENT_ITERATIONS):
        try:
            response = llm_with_tools.invoke(strip_reasoning(messages))
        except Exception as exc:
            logger.debug("[QualityReviewer] Loop LLM call failed at iteration %d: %s", iteration, exc)
            break

        tool_calls = getattr(response, "tool_calls", [])
        if not tool_calls:
            logger.info("[QualityReviewer] Agent loop finished after %d iteration(s)", iteration + 1)
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
        logger.warning("[QualityReviewer] Reached max agent iterations (%d)", _MAX_AGENT_ITERATIONS)

    return call_structured(llm, messages, _QualityFindings)


# ── Fallback: static tools → LLM ──────────────────────────────────────────────

def _static_plus_llm(
    diff_files: list[dict],
    routing_decision: dict,
    historical: list[str],
    tool_records: list[ToolCallRecord],
) -> _QualityFindings | None:
    static_results = _run_static_tools(diff_files, tool_records)

    file_summaries_parts = []
    for f in diff_files:
        patch = f.get("patch", "")[:_MAX_PATCH_CHARS]
        results = static_results.get(f["filename"], {})
        tool_output = json.dumps(results, indent=2)[:800] if results else "(no static analysis results)"
        file_summaries_parts.append(
            f"### {f['filename']}\n"
            f"```diff\n{patch}\n```\n"
            f"**AST Metrics:**\n```json\n{tool_output}\n```"
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
            f"\n\n=== HISTORICAL CONTEXT ===\n{context_block}\n"
        )

    messages = [
        SystemMessage(content=prompt_tmpl.SYSTEM),
        HumanMessage(content=human_content),
    ]
    return call_structured(llm, messages, _QualityFindings)


# ── Node entry point ───────────────────────────────────────────────────────────

def quality_reviewer_node(state: ReviewState) -> dict:
    logger.info("[QualityReviewer] 开始质量审查 (agent + tools)")

    diff_files = state.get("diff_files", [])
    routing    = state.get("routing_decision", {})
    repo_name  = state.get("repo_name", "unknown")

    if not diff_files:
        logger.warning("[QualityReviewer] 没有文件需要分析")
        return {"quality_findings": [], "agent_messages": ["[QualityReviewer] no files to analyze"]}

    # Day 5: 查询长期记忆
    historical: list[str] = []
    try:
        filenames = [f["filename"] for f in diff_files]
        query_text = f"Code quality issues in {', '.join(filenames)}"
        historical = get_long_term_memory().query(repo_name=repo_name, query_text=query_text, top_k=5)
        if historical:
            logger.info("[QualityReviewer] 获取到 %d 条历史上下文", len(historical))
    except Exception as exc:
        logger.warning("[QualityReviewer] 长期记忆查询失败（跳过）: %s", exc)

    tool_records: list[ToolCallRecord] = []
    findings_model: _QualityFindings | None = None

    try:
        findings_model = _agent_loop(diff_files, routing, historical, tool_records)
        logger.info("[QualityReviewer] Agent loop 完成")
    except Exception as exc:
        logger.warning("[QualityReviewer] Agent loop 失败，切换到静态扫描模式: %s", exc)

    if findings_model is None:
        try:
            findings_model = _static_plus_llm(diff_files, routing, historical, tool_records)
        except Exception as exc:
            logger.error("[QualityReviewer] Fallback also failed: %s", exc)

    if findings_model is None:
        return {
            "quality_findings": [],
            "tool_call_log":    tool_records,
            "errors":           ["[QualityReviewer] Both agent loop and fallback failed"],
            "agent_messages":   ["[QualityReviewer] 审查失败"],
        }

    issues = _convert_to_review_issues(findings_model)
    high_count = sum(1 for i in issues if i["severity"] == "high")
    msg = (
        f"[QualityReviewer] 完成 | quality={findings_model.overall_quality} "
        f"| findings={len(issues)} | high={high_count}"
    )
    logger.info(msg)

    return {
        "quality_findings": issues,
        "tool_call_log":    tool_records,
        "agent_messages":   [msg],
    }
