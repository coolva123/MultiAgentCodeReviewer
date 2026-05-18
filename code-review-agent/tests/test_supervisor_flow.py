"""
模拟 GitHub PR 提交触发完整 Supervisor 流程的集成测试。

用法：
    cd code-review-agent
    python tests/test_supervisor_flow.py                        # 使用内置 sample diff
    python tests/test_supervisor_flow.py --diff fixtures/sample.diff
    python tests/test_supervisor_flow.py --diff fixtures/sample.diff --repo myorg/myrepo
    python tests/test_supervisor_flow.py --pr-url https://github.com/owner/repo/pull/123
"""
import argparse
import logging
import sys
import uuid
from pathlib import Path

# 确保项目根目录在 path 里
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_supervisor_flow")

# ── 内置测试 diff（含 SQL 注入 + 硬编码密钥，方便验证安全审查路径）──────────────

_BUILTIN_DIFF = """\
diff --git a/app/auth.py b/app/auth.py
index 3a1b2c3..4d5e6f7 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,3 +1,15 @@
+import hashlib
+import sqlite3
+
 def login(username, password):
-    # TODO: implement real auth
-    return True
+    db = sqlite3.connect("users.db")
+    cursor = db.cursor()
+    # SQL injection vulnerability
+    query = f"SELECT * FROM users WHERE username='{username}' AND password='{password}'"
+    cursor.execute(query)
+    return cursor.fetchone() is not None
+
+SECRET_KEY = "hardcoded_secret_12345"
+
+def hash_password(pw):
+    return hashlib.md5(pw.encode()).hexdigest()
diff --git a/app/utils.py b/app/utils.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/app/utils.py
@@ -0,0 +1,11 @@
+def process_data(data):
+    result = []
+    for item in data:
+        for sub in item:
+            for val in sub:
+                if val:
+                    result.append(val)
+    return result
+
+def unused_func():
+    pass
"""


def _build_initial_state(
    diff_content: str,
    repo_name: str,
    pr_title: str,
    repo_url: str,
    session_id: str,
) -> dict:
    return {
        "diff_content":           diff_content,
        "pr_metadata":            {"title": pr_title},
        "repo_name":              repo_name,
        "repo_url":               repo_url,
        "session_id":             session_id,
        "diff_files":             [],
        "diff_summary":           {},
        "routing_decision":       {},
        "security_findings":      [],
        "quality_findings":       [],
        "dependency_findings":    [],
        "test_coverage_findings": [],
        "final_report":           None,
        "research_context":       "",
        "historical_context":     "",
        "project_context":        {},
        "supervisor_instruction": "",
        "iteration_count":        0,
        "review_pipeline_called": False,
        "tool_call_log":          [],
        "agent_messages":         [],
        "errors":                 [],
        "current_step":           "init",
        "review_complete":        False,
    }


def run(
    diff_content: str,
    repo_name: str = "test/repo",
    pr_title: str = "Test PR",
    repo_url: str = "",
    pr_url: str = "",
) -> dict:
    session_id = str(uuid.uuid4())

    # ── PR URL 模式：先拉 diff ──────────────────────────────────────────────────
    if pr_url:
        from config.settings import GITHUB_TOKEN
        from src.tools.github_tools import fetch_pr_diff
        if not GITHUB_TOKEN:
            logger.error("GITHUB_TOKEN 未配置，无法拉取 PR diff")
            sys.exit(1)
        logger.info("拉取 PR diff: %s", pr_url)
        diff_content, repo_name, pr_meta = fetch_pr_diff(pr_url, GITHUB_TOKEN)
        pr_title = pr_meta.get("title", "")
        parts = pr_url.rstrip("/").split("/pull/")
        repo_url = parts[0] if len(parts) == 2 else ""

    initial_state = _build_initial_state(
        diff_content=diff_content,
        repo_name=repo_name,
        pr_title=pr_title,
        repo_url=repo_url,
        session_id=session_id,
    )

    logger.info("=" * 60)
    logger.info("Supervisor 流程测试开始")
    logger.info("session_id : %s", session_id)
    logger.info("repo       : %s", repo_name)
    logger.info("pr_title   : %s", pr_title)
    logger.info("diff_len   : %d chars", len(diff_content))
    logger.info("=" * 60)

    from src.graph.supervisor_graph import supervisor_graph
    config = {"configurable": {"thread_id": session_id}}
    result = supervisor_graph.invoke(initial_state, config=config)

    # ── 结果摘要 ──────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("执行完成")
    logger.info("review_complete   : %s", result.get("review_complete"))
    logger.info("diff_files        : %d 个", len(result.get("diff_files", [])))
    summary = result.get("diff_summary", {})
    logger.info("pr_nature         : %s", summary.get("pr_nature", "-"))
    logger.info("estimated_risk    : %s", summary.get("estimated_risk", "-"))
    logger.info("security_findings : %d 条", len(result.get("security_findings", [])))
    logger.info("quality_findings  : %d 条", len(result.get("quality_findings", [])))
    logger.info("dependency_findings : %d 条", len(result.get("dependency_findings", [])))
    logger.info("test_coverage_findings : %d 条", len(result.get("test_coverage_findings", [])))
    logger.info("tool_calls        : %d 次", len(result.get("tool_call_log", [])))
    logger.info("iteration_count   : %d", result.get("iteration_count", 0))
    logger.info("errors            : %d 条", len(result.get("errors", [])))

    # ── Security findings 明细 ────────────────────────────────────────────────
    sec = result.get("security_findings", [])
    if sec:
        logger.info("── Security findings ──")
        for f in sec:
            logger.info("  [%s] %s — %s:%s",
                        f.get("severity", "?").upper(),
                        f.get("title", ""),
                        f.get("file", ""),
                        f.get("line", "?"))

    # ── Quality findings 明细 ─────────────────────────────────────────────────
    qual = result.get("quality_findings", [])
    if qual:
        logger.info("── Quality findings ──")
        for f in qual:
            logger.info("  [%s] %s — %s:%s",
                        f.get("severity", "?").upper(),
                        f.get("title", ""),
                        f.get("file", ""),
                        f.get("line", "?"))

    # ── Agent 消息链 ──────────────────────────────────────────────────────────
    logger.info("── Agent 消息链 ──")
    for msg in result.get("agent_messages", []):
        logger.info("  %s", msg)

    logger.info("=" * 60)

    # ── 输出报告 ──────────────────────────────────────────────────────────────
    report = result.get("final_report", "")
    if report:
        print("\n" + "=" * 60)
        print(report)
        print("=" * 60)
    else:
        logger.warning("final_report 为空，审查可能未完成")

    return result


# ── CLI 入口 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="模拟 GitHub PR 触发 Supervisor 审查流程")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--diff", metavar="PATH",
                       help="本地 diff 文件路径（相对于 tests/ 目录或绝对路径）")
    group.add_argument("--pr-url", metavar="URL",
                       help="GitHub PR URL（需配置 GITHUB_TOKEN）")
    parser.add_argument("--repo",  default="test/sample-repo", help="仓库名，格式 org/repo")
    parser.add_argument("--title", default="Test PR Review",   help="PR 标题")
    parser.add_argument("--repo-url", default="",              help="仓库 URL（可选，供 Research Agent 使用）")
    args = parser.parse_args()

    if args.pr_url:
        run(diff_content="", pr_url=args.pr_url, repo_name=args.repo)
        return

    if args.diff:
        diff_path = Path(args.diff)
        if not diff_path.is_absolute():
            diff_path = Path(__file__).parent / diff_path
        if not diff_path.exists():
            logger.error("diff 文件不存在: %s", diff_path)
            sys.exit(1)
        diff_content = diff_path.read_text(encoding="utf-8")
    else:
        logger.info("未指定 --diff 或 --pr-url，使用内置测试 diff")
        diff_content = _BUILTIN_DIFF

    run(
        diff_content=diff_content,
        repo_name=args.repo,
        pr_title=args.title,
        repo_url=args.repo_url,
    )


if __name__ == "__main__":
    main()
