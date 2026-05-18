import logging
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from unidiff import PatchSet

import src.prompts.diff_analyzer as prompt_tmpl
from config.settings import get_llm
from src.graph.state import DiffFile, ReviewState
from src.tools.llm_utils import call_structured

logger = logging.getLogger(__name__)

MAX_PATCH_CHARS = 3000  # 单文件 patch 截断阈值，避免超出 token 限制


# ── Pydantic 结构化输出模型 ────────────────────────────────────────────────────

class FileSemanticInfo(BaseModel):
    filename: str = Field(description="diff 中显示的文件路径")
    change_category: Literal["feature", "bugfix", "refactor", "config", "test", "docs", "security"] = Field(
        description="该文件变更的主要类型"
    )
    is_security_sensitive: bool = Field(
        description="若该文件涉及认证、加密、密钥、SQL、权限等安全敏感内容，则为 true"
    )
    is_complex_logic: bool = Field(
        description="若该变更引入了复杂条件分支、递归或并发逻辑，则为 true"
    )
    file_summary: str = Field(description="用中文一句话描述该文件的变更意图")


class DiffSemanticAnalysis(BaseModel):
    files: list[FileSemanticInfo] = Field(description="每个变更文件的语义信息")
    overall_summary: str = Field(description="用中文 2-3 句话概括整个 PR 的目的")
    pr_nature: Literal["feature", "bugfix", "refactor", "mixed"] = Field(
        description="本次 PR 的整体类型"
    )
    estimated_risk: Literal["high", "medium", "low"] = Field(
        description="合并本次 PR 的整体风险等级"
    )


# ── Step 1: 结构解析（unidiff）────────────────────────────────────────────────

def _parse_structure(raw_diff: str) -> list[DiffFile]:
    """用 unidiff 库解析 unified diff，提取结构化字段。"""
    try:
        patch = PatchSet(raw_diff)
    except Exception as e:
        logger.warning("[DiffAnalyzer] unidiff 解析失败，回退到空列表: %s", e)
        return []

    files: list[DiffFile] = []
    for patched_file in patch:
        if patched_file.is_added_file:
            change_type = "added"
        elif patched_file.is_removed_file:
            change_type = "deleted"
        else:
            change_type = "modified"

        # 拼接 hunk 原始文本，超长则截断
        patch_text = "".join(
            str(hunk) for hunk in patched_file
        )
        if len(patch_text) > MAX_PATCH_CHARS:
            patch_text = patch_text[:MAX_PATCH_CHARS] + "\n... [truncated]"

        files.append({
            "filename": patched_file.path,
            "change_type": change_type,
            "additions": patched_file.added,
            "deletions": patched_file.removed,
            "patch": patch_text,
        })

    return files


# ── Step 2: 语义分析（LLM）───────────────────────────────────────────────────

def _analyze_semantics(
    diff_files: list[DiffFile],
    diff_content: str,
    repo_name: str,
    pr_title: str,
) -> DiffSemanticAnalysis | None:
    """调用 LLM，对结构化 diff 做语义理解。"""
    file_list = "\n".join(
        f"- {f['filename']} ({f['change_type']}, +{f['additions']}/-{f['deletions']})"
        for f in diff_files
    )

    # diff 内容整体截断，避免超 token
    truncated_diff = diff_content[:8000] + ("\n... [diff truncated]" if len(diff_content) > 8000 else "")

    prompt = ChatPromptTemplate.from_messages([
        ("system", prompt_tmpl.SYSTEM),
        ("human", prompt_tmpl.HUMAN),
    ])

    try:
        llm = get_llm(temperature=0.1)
        messages = prompt.format_messages(
            repo_name=repo_name,
            pr_title=pr_title,
            file_list=file_list,
            diff_content=truncated_diff,
        )
        return call_structured(llm, messages, DiffSemanticAnalysis)
    except Exception as e:
        logger.error("[DiffAnalyzer] LLM 调用失败: %s", e)
        return None


# ── Step 3: 合并结构 + 语义 ─────────────────────────────────────────────────

def _merge(
    diff_files: list[DiffFile],
    analysis: DiffSemanticAnalysis | None,
) -> list[DiffFile]:
    """将 LLM 语义字段写回对应的 DiffFile。"""
    if analysis is None:
        return diff_files

    semantic_map = {f.filename: f for f in analysis.files}

    merged: list[DiffFile] = []
    for f in diff_files:
        sem = semantic_map.get(f["filename"])
        if sem:
            f = {
                **f,
                "change_category": sem.change_category,
                "is_security_sensitive": sem.is_security_sensitive,
                "is_complex_logic": sem.is_complex_logic,
                "file_summary": sem.file_summary,
            }
        merged.append(f)

    return merged


# ── 节点函数   ──────────────────────────────────────────────────────────────────

def diff_analyzer_node(state: ReviewState) -> dict:
                         #接收完整 State    ↑ 只返回变化的部分
    """             
    Diff Analyzer Agent：
      1. unidiff → 结构化 DiffFile 列表
      2. LLM     → 每个文件的语义分类 + 整体 PR 摘要
      3. 合并输出，写入 State
    """
    logger.info("[DiffAnalyzer] 开始解析 diff")

    diff_content = state.get("diff_content", "")
    repo_name = state.get("repo_name", "unknown")
    pr_title = state.get("pr_metadata", {}).get("title", "")

    # Step 1
    diff_files = _parse_structure(diff_content)
    logger.info(
        "[DiffAnalyzer] 结构解析完成: %d 个文件 | +%d/-%d 行",
        len(diff_files),
        sum(f["additions"] for f in diff_files),
        sum(f["deletions"] for f in diff_files),
    )

    if not diff_files:
        msg = "[DiffAnalyzer] 未解析到任何文件变更，diff 可能为空"
        logger.warning(msg)
        return {
            "diff_files": [],
            "diff_summary": {},
            "current_step": "diff_analyzer_done",
            "agent_messages": [msg],
            "errors": [msg],
        }

    # Step 2
    logger.info("[DiffAnalyzer] 调用 LLM 进行语义分析 ...")
    analysis = _analyze_semantics(diff_files, diff_content, repo_name, pr_title)

    # Step 3
    diff_files = _merge(diff_files, analysis)

    diff_summary = {}
    if analysis:
        diff_summary = {
            "overall_summary": analysis.overall_summary,
            "pr_nature": analysis.pr_nature,
            "estimated_risk": analysis.estimated_risk,
        }
        logger.info(
            "[DiffAnalyzer] 语义分析完成 | pr_nature=%s | risk=%s",
            analysis.pr_nature,
            analysis.estimated_risk,
        )
    else:
        logger.warning("[DiffAnalyzer] LLM 语义分析失败，仅保留结构信息")

    msg = (
        f"[DiffAnalyzer] 完成: {len(diff_files)} 个文件 "
        f"| pr_nature={diff_summary.get('pr_nature', 'unknown')} "
        f"| risk={diff_summary.get('estimated_risk', 'unknown')}"
    )

    return {
        "diff_files": diff_files,
        "diff_summary": diff_summary,
        "current_step": "diff_analyzer_done",
        "agent_messages": [msg],
    }
