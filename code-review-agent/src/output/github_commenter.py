"""
GitHub PR Comment 回写 — Day 6。

post_review_comment() : 把 Markdown 报告以 PR Review Comment 形式回写到 GitHub。
"""
import logging
import re

logger = logging.getLogger(__name__)

_MAX_COMMENT_CHARS = 65536  # GitHub 单条 comment 上限


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    m = re.search(pattern, pr_url)
    if not m:
        raise ValueError(f"无法解析 GitHub PR URL: {pr_url!r}")
    return m.group(1), m.group(2), int(m.group(3))


def post_review_comment(pr_url: str, token: str, body: str) -> str:
    """
    将报告 body 作为 PR issue comment 发布到 GitHub。
    超长时自动截断并附注说明。

    Returns:
        comment 的 HTML URL
    """
    from github import Github, GithubException

    owner, repo_name, pr_number = _parse_pr_url(pr_url)

    if len(body) > _MAX_COMMENT_CHARS:
        truncated = body[:_MAX_COMMENT_CHARS - 200]
        body = (
            truncated
            + "\n\n---\n"
            + f"*⚠️ 报告超出 GitHub 单条 Comment 上限（{_MAX_COMMENT_CHARS} 字符），已截断。*"
        )

    try:
        g    = Github(token)
        repo = g.get_repo(f"{owner}/{repo_name}")
        pr   = repo.get_pull(pr_number)
        comment = pr.create_issue_comment(body)
        url = comment.html_url
        logger.info("[GitHubCommenter] PR Comment 发布成功: %s", url)
        return url
    except GithubException as exc:
        raise RuntimeError(f"GitHub API 错误（{exc.status}）: {exc.data}") from exc
