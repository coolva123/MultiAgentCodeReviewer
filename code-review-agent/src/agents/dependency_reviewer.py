"""
Dependency Security Reviewer Agent。

数据来源：
  - diff_files（已有，含依赖文件的 patch）
  - fetch_file_content（GitHub API，获取完整依赖文件）
  - query_osv（OSV.dev，无需 API Key 的开源漏洞数据库）

触发条件（由 Coordinator 路由）：
  routing_decision["run_dependency"] == True
  即 diff 中包含 requirements.txt / pyproject.toml / package.json / go.mod 等依赖文件变更
"""
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

import src.prompts.dependency as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewIssue, ReviewState, ToolCallRecord
from src.harness.tool_guard import guarded_call
from src.tools.dependency_tools import query_osv
from src.tools.github_tools import fetch_file_content
from src.tools.llm_utils import call_structured, strip_reasoning

logger = logging.getLogger(__name__)

_MAX_AGENT_ITERATIONS = 4
_MAX_PATCH_CHARS = 2000
_DEP_FILENAMES = {"requirements.txt", "pyproject.toml", "package.json", "go.mod", "Pipfile"}

_TOOLS = [fetch_file_content, query_osv]


# ── Pydantic output schema ─────────────────────────────────────────────────────

class _DepFinding(BaseModel):
    file: str = Field(description="发现问题的依赖文件路径")
    line: Optional[int] = Field(None, description="行号，未知时填 null")
    severity: str = Field(description="严重等级：critical | high | medium | low | info")
    category: str = Field(description="问题类别：known_cve（已知漏洞）| outdated（过期版本）| unlicensed（许可证问题）")
    title: str = Field(description="用中文简洁描述问题，不超过 40 字")
    description: str = Field(description="用中文详细说明漏洞信息，包含 CVE 编号和影响范围")
    suggestion: str = Field(description="用中文给出安全版本建议或修复方案")


class _DepFindings(BaseModel):
    findings: list[_DepFinding] = Field(default_factory=list)
    summary: str = Field(description="用中文一句话概括本次 PR 的依赖安全态势")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_dep_changes(diff_files: list[dict]) -> bool:
    return any(
        any(dep in f["filename"] for dep in _DEP_FILENAMES)
        for f in diff_files
    )


def _build_file_summaries(diff_files: list[dict]) -> str:
    parts = []
    for f in diff_files:
        patch = f.get("patch", "")[:_MAX_PATCH_CHARS]
        parts.append(
            f"### {f['filename']}  [{f.get('change_type','?')}]\n"
            f"```diff\n{patch}\n```"
        )
    return "\n\n".join(parts)


# ── Agent loop ─────────────────────────────────────────────────────────────────

def _agent_loop(
    diff_files: list[dict],
    routing: dict,
    repo_name: str,
    repo_url: str,
    tool_records: list[ToolCallRecord],
) -> _DepFindings | None:
    llm = get_llm(temperature=0.1)
    tool_map = {t.name: t for t in _TOOLS}
    llm_with_tools = llm.bind_tools(_TOOLS)

    messages = [
        SystemMessage(content=prompt_tmpl.SYSTEM),
        HumanMessage(content=prompt_tmpl.HUMAN.format(
            repo_name=repo_name,
            repo_url=repo_url or "not provided",
            priority=routing.get("priority", "medium"),
            file_summaries=_build_file_summaries(diff_files),
        )),
    ]

    for iteration in range(_MAX_AGENT_ITERATIONS):
        try:
            response = llm_with_tools.invoke(strip_reasoning(messages))
        except Exception as exc:
            logger.debug("[DepReviewer] LLM 调用失败 iteration=%d: %s", iteration, exc)
            break

        tool_calls = getattr(response, "tool_calls", [])
        if not tool_calls:
            logger.info("[DepReviewer] Agent loop 完成，共 %d 轮", iteration + 1)
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
        logger.warning("[DepReviewer] 达到最大迭代次数 (%d)", _MAX_AGENT_ITERATIONS)

    return call_structured(llm, messages, _DepFindings)


# ── Node entry point ───────────────────────────────────────────────────────────

def dependency_reviewer_node(state: ReviewState) -> dict:
    logger.info("[DepReviewer] 开始依赖安全审查")

    diff_files = state.get("diff_files", [])
    routing    = state.get("routing_decision", {})
    repo_name  = state.get("repo_name", "unknown")
    repo_url   = state.get("repo_url", "")

    if not _has_dep_changes(diff_files):
        msg = "[DepReviewer] 未检测到依赖文件变更，跳过"
        logger.info(msg)
        return {"dependency_findings": [], "agent_messages": [msg]}

    tool_records: list[ToolCallRecord] = []
    findings_model = _agent_loop(diff_files, routing, repo_name, repo_url, tool_records)

    if findings_model is None:
        return {
            "dependency_findings": [],
            "tool_call_log":       tool_records,
            "errors":              ["[DepReviewer] 分析失败"],
            "agent_messages":      ["[DepReviewer] 依赖审查失败"],
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

    critical = sum(1 for i in issues if i["severity"] in ("critical", "high"))
    msg = f"[DepReviewer] 完成 | findings={len(issues)} | critical/high={critical}"
    logger.info(msg)

    return {
        "dependency_findings": issues,
        "tool_call_log":       tool_records,
        "agent_messages":      [msg],
    }
