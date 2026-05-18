"""
Session ID 工厂 — 为不同触发场景生成确定性或随机的 thread_id。

PR 模式（GitHub Actions / PR URL）：
  thread_id = "{owner}-{repo}-pr{number}-{sha8}"
  e.g.  "myorg-myrepo-pr123-a1b2c3d4"

  同一 PR + 同一 commit 的重复触发（断点恢复）→ 相同 thread_id
  同一 PR + 新 commit 推送              → sha 变化 → 新 thread_id
  不同 PR                               → pr_number 不同 → 各自独立

本地 diff / 文件上传模式：
  thread_id = uuid4（无自然重试语义，每次独立）
"""
import re
import uuid


def make_pr_session_id(repo_name: str, pr_number: int, head_sha: str) -> str:
    """
    为 GitHub PR 生成确定性 session_id。

    repo_name : "owner/repo" 格式
    pr_number : PR 编号
    head_sha  : PR HEAD commit SHA（完整或短）
    """
    safe_repo = re.sub(r"[^a-zA-Z0-9\-]", "-", repo_name)
    sha8 = head_sha[:8]
    return f"{safe_repo}-pr{pr_number}-{sha8}"


def make_local_session_id() -> str:
    """本地 diff / 文件上传模式：每次独立 UUID。"""
    return str(uuid.uuid4())
