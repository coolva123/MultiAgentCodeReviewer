"""
GitHub 集成工具。

fetch_pr_diff()     : 通过 PyGitHub 读取 PR diff，返回 (diff_text, repo_name, pr_metadata)
fetch_file_content(): LangChain Tool，获取仓库指定文件的完整内容（供专业 Reviewer 使用）
"""
import json
import logging
import re

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """
    解析 GitHub PR URL，支持格式：
      https://github.com/owner/repo/pull/123
      https://github.com/owner/repo/pull/123/files
    """
    pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    m = re.search(pattern, pr_url)
    if not m:
        raise ValueError(f"无法解析 GitHub PR URL: {pr_url!r}")
    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    return owner, repo, number


def fetch_pr_diff(pr_url: str, token: str) -> tuple[str, str, dict]:
    """
    获取 PR 的 unified diff + 元数据。

    Returns:
        diff_text   : raw unified diff 字符串（可直接传给 DiffAnalyzer）
        repo_name   : "owner/repo" 格式
        pr_metadata : dict，含 title / number / url / author / base / head
    """
    from github import Github, GithubException

    owner, repo_name, pr_number = _parse_pr_url(pr_url)
    full_name = f"{owner}/{repo_name}"

    try:
        g = Github(token)
        repo = g.get_repo(full_name)
        pr   = repo.get_pull(pr_number)
    except GithubException as exc:
        raise RuntimeError(f"GitHub API 错误（{exc.status}）: {exc.data}") from exc

    pr_metadata = {
        "title":       pr.title,
        "number":      pr.number,
        "url":         pr_url,
        "author":      pr.user.login,
        "base_branch": pr.base.ref,
        "head_branch": pr.head.ref,
        "head_sha":    pr.head.sha,   # 用于构造确定性 session_id
        "state":       pr.state,
    }

    # 用 Accept: application/vnd.github.v3.diff 获取原始 unified diff
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3.diff",
    }
    api_url = f"https://api.github.com/repos/{full_name}/pulls/{pr_number}"
    resp = requests.get(api_url, headers=headers, timeout=30)
    resp.raise_for_status()
    diff_text = resp.text

    logger.info(
        "[GitHubTools] 获取 PR #%d diff 成功 | repo=%s | title=%s | diff_len=%d",
        pr_number, full_name, pr.title, len(diff_text),
    )
    return diff_text, full_name, pr_metadata


@tool
def fetch_file_content(repo_url: str, file_path: str, ref: str = "HEAD") -> str:
    """
    获取 GitHub 仓库中指定文件的完整内容。
    用于在 diff 之外获取完整的文件上下文，例如依赖清单、测试文件等。

    repo_url : GitHub 仓库 URL，格式 https://github.com/owner/repo
    file_path: 仓库内的文件路径，例如 requirements.txt 或 tests/test_auth.py
    ref      : 分支、tag 或 commit SHA，默认 HEAD
    """
    from config.settings import GITHUB_TOKEN

    if not GITHUB_TOKEN:
        return json.dumps({"error": "GITHUB_TOKEN 未配置，无法获取文件内容"})

    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?$", repo_url.rstrip("/"))
    if not m:
        return json.dumps({"error": f"无法解析仓库 URL: {repo_url}"})

    owner, repo = m.group(1), m.group(2)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.raw",
    }

    try:
        resp = requests.get(api_url, headers=headers, timeout=15)
        if resp.status_code == 404:
            return json.dumps({"found": False, "file_path": file_path})
        resp.raise_for_status()
        content = resp.text
        if len(content) > 6000:
            content = content[:6000] + "\n...[truncated]"
        return json.dumps({"found": True, "file_path": file_path, "content": content})
    except Exception as exc:
        return json.dumps({"error": str(exc), "file_path": file_path})
