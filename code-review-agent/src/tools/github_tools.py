"""
GitHub 集成工具 — Day 6。

fetch_pr_diff() : 通过 PyGitHub 读取 PR diff，返回 (diff_text, repo_name, pr_metadata)
"""
import logging
import re

import requests

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
