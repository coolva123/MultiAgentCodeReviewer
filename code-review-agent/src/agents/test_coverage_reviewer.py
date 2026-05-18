"""
Test Coverage Reviewer Agent。

数据来源：
  - diff_files（已有，含业务文件的 patch）
  - fetch_file_content（GitHub API，检查对应测试文件是否存在）

触发条件（由 Coordinator 路由）：
  routing_decision["run_test_coverage"] == True
  即 diff 中包含功能性代码变更（非纯文档/配置），且 PR 中测试文件变更较少
"""
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

import src.prompts.test_coverage as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewIssue, ReviewState, ToolCallRecord
from src.harness.tool_guard import guarded_call
from src.tools.github_tools import fetch_file_content
from src.tools.llm_utils import call_structured, strip_reasoning

logger = logging.getLogger(__name__)

_MAX_AGENT_ITERATIONS = 4
_MAX_PATCH_CHARS = 1500

_SKIP_PATTERNS = (
    "test_", "/tests/", "/test/", ".md", ".rst", ".txt",
    "requirements", "setup.py", "setup.cfg", "pyproject.toml",
    "migrations/", ".yml", ".yaml", ".json", ".env",
)

_TOOLS = [fetch_file_content]


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _CoverageFinding(BaseModel):
    file: str = Field(description="缺少测试覆盖的业务文件路径")
    line: Optional[int] = Field(None, description="关键新增函数的行号，未知时填 null")
    severity: str = Field(description="严重等级：high | medium | low | info")
    category: str = Field(description="问题类别：missing_tests（无测试文件）| insufficient_coverage（覆盖不足）| missing_edge_cases（缺少边界测试）")
    title: str = Field(description="用中文简洁描述问题，不超过 40 字")
    description: str = Field(description="用中文说明哪些新增代码路径缺少测试")
    suggestion: str = Field(description="用中文给出具体的测试建议，包括应测试的场景")


class _CoverageFindings(BaseModel):
    findings: list[_CoverageFinding] = Field(default_factory=list)
    summary: str = Field(description="用中文一句话概括本次 PR 的测试覆盖状况")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_business_file(filename: str) -> bool:
    return not any(p in filename for p in _SKIP_PATTERNS)


def _get_test_files_in_pr(diff_files: list[dict]) -> list[str]:
    return [f["filename"] for f in diff_files if "test" in f["filename"].lower()]


def _build_file_summaries(diff_files: list[dict]) -> str:
    parts = []
    for f in diff_files:
        if not _is_business_file(f["filename"]):
            continue
        patch = f.get("patch", "")[:_MAX_PATCH_CHARS]
        parts.append(
            f"### {f['filename']}  [{f.get('change_type','?')}]\n"
            f"category={f.get('change_category','?')} | "
            f"complex_logic={f.get('is_complex_logic', False)}\n"
            f"```diff\n{patch}\n```"
        )
    return "\n\n".join(parts) if parts else "（无需检查覆盖的业务文件）"


# ── Agent loop ─────────────────────────────────────────────────────────────────

def _agent_loop(
    diff_files: list[dict],
    routing: dict,
    repo_name: str,
    repo_url: str,
    tool_records: list[ToolCallRecord],
) -> _CoverageFindings | None:
    llm = get_llm(temperature=0.1)
    tool_map = {t.name: t for t in _TOOLS}
    llm_with_tools = llm.bind_tools(_TOOLS)

    test_files_in_pr = _get_test_files_in_pr(diff_files)
    test_files_str = "\n".join(f"  - {f}" for f in test_files_in_pr) if test_files_in_pr else "（PR 中未包含测试文件变更）"

    messages = [
        SystemMessage(content=prompt_tmpl.SYSTEM),
        HumanMessage(content=prompt_tmpl.HUMAN.format(
            repo_name=repo_name,
            repo_url=repo_url or "not provided",
            priority=routing.get("priority", "medium"),
            file_summaries=_build_file_summaries(diff_files),
            test_files_in_pr=test_files_str,
        )),
    ]

    for iteration in range(_MAX_AGENT_ITERATIONS):
        try:
            response = llm_with_tools.invoke(strip_reasoning(messages))
        except Exception as exc:
            logger.debug("[TestCoverageReviewer] LLM 调用失败 iteration=%d: %s", iteration, exc)
            break

        tool_calls = getattr(response, "tool_calls", [])
        if not tool_calls:
            logger.info("[TestCoverageReviewer] Agent loop 完成，共 %d 轮", iteration + 1)
            break

        messages.append(response)
        for tc in tool_calls:
            tool_fn = tool_map.get(tc["name"])
            if tool_fn is None:
                result = json.dumps({"error": f"unknown tool: {tc['name']}"})
            else:
                raw, record = guarded_call(tool_fn, tc["name"], tc["args"])
                tool_records.append(record)
                result = str(raw) if raw is not None else json.dumps({"error": "tool failed"})
            messages.append(ToolMessage(content=result[:3000], tool_call_id=tc["id"]))
    else:
        logger.warning("[TestCoverageReviewer] 达到最大迭代次数 (%d)", _MAX_AGENT_ITERATIONS)

    return call_structured(llm, messages, _CoverageFindings)


# ── Node entry point ───────────────────────────────────────────────────────────

def test_coverage_reviewer_node(state: ReviewState) -> dict:
    logger.info("[TestCoverageReviewer] 开始测试覆盖审查")

    diff_files = state.get("diff_files", [])
    routing    = state.get("routing_decision", {})
    repo_name  = state.get("repo_name", "unknown")
    repo_url   = state.get("repo_url", "")

    business_files = [f for f in diff_files if _is_business_file(f["filename"])]
    if not business_files:
        msg = "[TestCoverageReviewer] 无业务代码变更，跳过覆盖检查"
        logger.info(msg)
        return {"test_coverage_findings": [], "agent_messages": [msg]}

    tool_records: list[ToolCallRecord] = []
    findings_model = _agent_loop(diff_files, routing, repo_name, repo_url, tool_records)

    if findings_model is None:
        return {
            "test_coverage_findings": [],
            "tool_call_log":          tool_records,
            "errors":                 ["[TestCoverageReviewer] 分析失败"],
            "agent_messages":         ["[TestCoverageReviewer] 测试覆盖审查失败"],
        }

    issues: list[ReviewIssue] = [
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

    msg = f"[TestCoverageReviewer] 完成 | findings={len(issues)}"
    logger.info(msg)

    return {
        "test_coverage_findings": issues,
        "tool_call_log":          tool_records,
        "agent_messages":         [msg],
    }
