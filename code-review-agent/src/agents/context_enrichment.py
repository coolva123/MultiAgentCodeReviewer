import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import src.prompts.context_enrichment as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.harness.memory.long_term import get_long_term_memory
from src.harness.memory.project_profile import get_project_profile_store
from src.tools.github_tools import fetch_file_content
from src.tools.llm_utils import call_structured
from src.tools.research_tools import fetch_repo_readme, fetch_repo_structure

logger = logging.getLogger(__name__)

_MAX_RELATED_FILES = 3
_RELATED_FILE_LINES = 80
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".java", ".rb", ".rs"}
_PRIORITY_KEYWORDS = {"model", "schema", "base", "core", "util", "service"}
_EXCLUDE_PATTERNS = {"test_", "_test.", "/tests/"}


class ProfileModel(BaseModel):
    tech_stack: str = Field(default="unknown")
    project_type: str = Field(default="other")
    security_level: str = Field(default="medium")
    frameworks: str = Field(default="")
    conventions: str = Field(default="")
    summary: str = Field(default="")


def _extract_changed_files(diff_content: str) -> List[str]:
    return re.findall(r"^diff --git a/(.+?) b/", diff_content, re.MULTILINE)


def _parse_tree_path(line: str) -> str:
    stripped = line.strip()
    m = re.search(r"[a-zA-Z0-9_]", stripped)
    return stripped[m.start():] if m else ""


def _score_candidate(path: str, changed_files: List[str]) -> int:
    score = 0
    name = os.path.basename(path).lower()
    ext = os.path.splitext(path)[1].lower()
    for cf in changed_files:
        cf_dir = os.path.dirname(cf)
        if cf_dir and path.startswith(cf_dir + "/"):
            score += 3
            break
    if any(kw in name for kw in _PRIORITY_KEYWORDS):
        score += 2
    changed_exts = {os.path.splitext(f)[1].lower() for f in changed_files}
    if ext in changed_exts:
        score += 1
    return score


def _select_related_files(diff_content: str, structure_text: str) -> List[str]:
    changed_files = _extract_changed_files(diff_content)
    if not changed_files:
        return []
    changed_set = set(changed_files)
    changed_dirs = {os.path.dirname(f) for f in changed_files if os.path.dirname(f)}

    candidates = []
    for line in structure_text.splitlines():
        path = _parse_tree_path(line)
        if not path or path in changed_set:
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in _CODE_EXTENSIONS:
            continue
        if any(pat in path for pat in _EXCLUDE_PATTERNS):
            continue
        if os.path.dirname(path) not in changed_dirs:
            continue
        candidates.append((_score_candidate(path, changed_files), path))

    candidates.sort(key=lambda x: -x[0])
    return [p for _, p in candidates[:_MAX_RELATED_FILES]]


def context_enrichment_node(state: ReviewState) -> Dict[str, Any]:
    repo_name = state.get("repo_name", "")
    repo_url = state.get("repo_url", "")
    diff_content = state.get("diff_content", "")

    # ── Layer 1: Project Profile ───────────────────────────────────────────────
    profile: dict = {}
    structure_text = ""
    if repo_url:
        try:
            readme_raw = fetch_repo_readme.invoke({"repo_url": repo_url})
            try:
                readme_result = json.loads(readme_raw)
            except (json.JSONDecodeError, TypeError):
                readme_result = {"content": readme_raw, "sha": ""}
            readme_content = readme_result.get("content", "") if isinstance(readme_result, dict) else str(readme_result)
            readme_sha = readme_result.get("sha", "") if isinstance(readme_result, dict) else ""
            if not readme_sha:
                readme_sha = hashlib.md5(readme_content.encode()).hexdigest()

            store = get_project_profile_store()
            cached = store.get_profile(repo_name, readme_sha)

            if cached:
                profile = cached
                logger.info("[ContextEnrichment] Layer1 cache hit | repo=%s", repo_name)
            else:
                structure_text = fetch_repo_structure.invoke({"repo_url": repo_url})
                claude_raw = fetch_file_content.invoke({"repo_url": repo_url, "file_path": "CLAUDE.md"})
                try:
                    claude_result = json.loads(claude_raw)
                    config_content = claude_result.get("content", "") if claude_result.get("found") else ""
                except (json.JSONDecodeError, TypeError):
                    config_content = ""

                messages = [
                    SystemMessage(content=prompt_tmpl.SYSTEM),
                    HumanMessage(content=prompt_tmpl.HUMAN.format(
                        readme=readme_content[:3000],
                        structure=structure_text[:1000],
                        config=config_content[:1000],
                    )),
                ]
                llm = get_llm(temperature=0.0)
                pm = call_structured(llm, messages, ProfileModel)
                profile = pm.model_dump() if pm else {}
                profile["from_cache"] = False

                if profile and repo_name:
                    store.save_profile(repo_name, profile, readme_sha)

                logger.info("[ContextEnrichment] Layer1 profile generated | repo=%s", repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer1 failed: %s", exc)
            profile = {"from_cache": False}

    # ── Layer 2: Related Files ─────────────────────────────────────────────────
    related_files: list = []
    if repo_url and diff_content:
        try:
            if not structure_text:
                structure_text = fetch_repo_structure.invoke({"repo_url": repo_url})
            candidate_paths = _select_related_files(diff_content, structure_text)
            for path in candidate_paths:
                raw = fetch_file_content.invoke({"repo_url": repo_url, "file_path": path})
                try:
                    result = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if result.get("found"):
                    lines = result.get("content", "").splitlines()[:_RELATED_FILE_LINES]
                    related_files.append({"path": path, "content": "\n".join(lines)})
            logger.info("[ContextEnrichment] Layer2 related_files=%d | repo=%s", len(related_files), repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer2 failed: %s", exc)

    # ── Layer 3: Historical Findings ───────────────────────────────────────────
    historical_findings = ""
    if repo_name and repo_name != "unknown":
        try:
            changed_files = _extract_changed_files(diff_content) if diff_content else []
            changed_summary = ", ".join(changed_files[:5]) if changed_files else ""
            query_text = (
                f"{repo_name} 改动文件: {changed_summary}"
                if changed_summary
                else f"security issues in {repo_name}"
            )
            results = get_long_term_memory().query(
                repo_name=repo_name, query_text=query_text, top_k=5
            )
            historical_findings = "\n".join(results) if results else ""
            logger.info("[ContextEnrichment] Layer3 history=%d findings | repo=%s", len(results), repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer3 failed: %s", exc)

    from_cache = profile.get("from_cache", False)
    history_count = historical_findings.count("\n") + 1 if historical_findings else 0
    msg = (
        f"[ContextEnrichment] profile={'cached' if from_cache else 'fresh'} | "
        f"related_files={len(related_files)} | history={history_count}条"
    )
    logger.info(msg)

    return {
        "project_context": {
            "profile": profile,
            "related_files": related_files,
            "historical_findings": historical_findings,
        },
        "historical_context": historical_findings,
        "agent_messages": [msg],
    }
