"""
Research Agent 工具集。

- tavily_search           : 搜索 CVE / 技术文档 / 最佳实践
- fetch_repo_readme       : 获取 GitHub 仓库 README
- fetch_repo_structure    : 获取 GitHub 仓库目录结构
- query_long_term_memory  : 查询本地长期记忆历史 findings
"""
import json
import logging

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def tavily_search(query: str, search_depth: str = "basic") -> str:
    """搜索网络，查找 CVE 报告、安全公告、最佳实践和技术文档。

    Args:
        query: 搜索关键词。
        search_depth: "basic"（快速）或 "advanced"（更深入，适合 CVE 查找）。
    """
    try:
        from langchain_tavily import TavilySearch
        from config.settings import TAVILY_API_KEY

        if not TAVILY_API_KEY:
            return "Tavily API key not configured. Set TAVILY_API_KEY in .env"

        search = TavilySearch(
            api_key=TAVILY_API_KEY,
            max_results=5,
            search_depth=search_depth,
        )
        results = search.invoke({"query": query})

        if isinstance(results, list):
            formatted = []
            for r in results:
                url = r.get("url", "")
                content = r.get("content", "")[:600]
                formatted.append(f"[{url}]\n{content}")
            return "\n\n---\n\n".join(formatted) or "No results found."

        return str(results)
    except Exception as exc:
        logger.warning("[tavily_search] 搜索失败: %s", exc)
        return f"Search failed: {exc}"


@tool
def fetch_repo_readme(repo_url: str) -> str:
    """获取 GitHub 仓库的 README，了解项目背景和技术栈。

    Args:
        repo_url: GitHub 仓库 URL，例如 https://github.com/owner/repo
    """
    try:
        from config.settings import GITHUB_TOKEN

        path = repo_url.rstrip("/").replace("https://github.com/", "")
        if "/" not in path:
            return f"Invalid GitHub URL: {repo_url}"

        api_url = f"https://api.github.com/repos/{path}/readme"
        headers = {"Accept": "application/vnd.github.v3.raw"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            text = resp.text
            return text[:4000] + ("\n...[truncated]" if len(text) > 4000 else "")
        elif resp.status_code == 404:
            return "README not found for this repository."
        else:
            return f"Could not fetch README: HTTP {resp.status_code}"
    except Exception as exc:
        logger.warning("[fetch_repo_readme] 失败: %s", exc)
        return f"Error fetching README: {exc}"


@tool
def fetch_repo_structure(repo_url: str) -> str:
    """获取 GitHub 仓库的目录结构，了解项目的整体架构。

    Args:
        repo_url: GitHub 仓库 URL，例如 https://github.com/owner/repo
    """
    try:
        from config.settings import GITHUB_TOKEN

        path = repo_url.rstrip("/").replace("https://github.com/", "")
        if "/" not in path:
            return f"Invalid GitHub URL: {repo_url}"

        api_url = f"https://api.github.com/repos/{path}/git/trees/HEAD?recursive=0"
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            tree = resp.json().get("tree", [])
            items = []
            for item in tree[:80]:
                prefix = "📁 " if item["type"] == "tree" else "📄 "
                items.append(f"{prefix}{item['path']}")
            return "\n".join(items) or "Repository appears to be empty."
        elif resp.status_code == 404:
            return "Repository not found or no HEAD branch."
        else:
            return f"Could not fetch repo structure: HTTP {resp.status_code}"
    except Exception as exc:
        logger.warning("[fetch_repo_structure] 失败: %s", exc)
        return f"Error fetching repo structure: {exc}"


@tool
def query_long_term_memory(repo_name: str, query: str) -> str:
    """查询长期记忆，获取指定仓库历次审查中的安全和质量发现。

    Args:
        repo_name: 仓库名称（与审查会话中使用的名称一致）。
        query: 语义查询描述，说明要查找的内容。
    """
    try:
        from src.harness.memory.long_term import get_long_term_memory

        results = get_long_term_memory().query(
            repo_name=repo_name, query_text=query, top_k=5
        )
        if not results:
            return f"No historical findings found for repository '{repo_name}'."
        return f"Historical findings for '{repo_name}':\n\n" + "\n\n".join(results)
    except Exception as exc:
        logger.warning("[query_long_term_memory] 查询失败: %s", exc)
        return f"Memory query failed: {exc}"
