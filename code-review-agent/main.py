#!/usr/bin/env python3
"""
MultiAgent Code Reviewer — 入口

用法:
  # 本地 diff 文件
  python main.py --diff-file tests/fixtures/sample.diff

  # GitHub PR URL（需要 GITHUB_TOKEN）
  python main.py --pr-url https://github.com/owner/repo/pull/123

  # 输出报告到文件
  python main.py --diff-file sample.diff --output report.md

  # 审查完成后回写 GitHub PR Comment
  python main.py --pr-url https://github.com/owner/repo/pull/123 --post-comment
"""
import argparse
import logging
import sys
import uuid
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="MultiAgent Code Reviewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff-file", metavar="PATH", help="本地 unified diff 文件路径")
    source.add_argument("--pr-url",    metavar="URL",  help="GitHub PR URL（需设置 GITHUB_TOKEN）")

    parser.add_argument("--repo",         default="local/repo", help="仓库名（org/repo），--diff-file 模式下使用")
    parser.add_argument("--pr-title",     default="",           help="PR 标题（可选）")
    parser.add_argument("--output",       metavar="PATH",       help="报告输出文件路径（.md）")
    parser.add_argument("--post-comment", action="store_true",  help="审查完成后把报告回写到 GitHub PR Comment")

    return parser.parse_args()


# ── 输入获取 ──────────────────────────────────────────────────────────────────

def _load_from_diff_file(path: str) -> tuple[str, str, dict]:
    """从本地文件读取 diff，返回 (diff_content, repo_name, pr_metadata)。"""
    diff_path = Path(path)
    if not diff_path.exists():
        logger.error("Diff 文件不存在: %s", diff_path)
        sys.exit(1)
    return diff_path.read_text(encoding="utf-8"), None, None


def _load_from_github(pr_url: str) -> tuple[str, str, dict]:
    """从 GitHub API 获取 PR diff，返回 (diff_content, repo_name, pr_metadata)。"""
    from config.settings import GITHUB_TOKEN
    from src.tools.github_tools import fetch_pr_diff

    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN 未设置，无法获取 GitHub PR。请在 .env 中配置。")
        sys.exit(1)

    logger.info("正在从 GitHub 获取 PR diff: %s", pr_url)
    try:
        diff_content, repo_name, pr_metadata = fetch_pr_diff(pr_url, GITHUB_TOKEN)
        return diff_content, repo_name, pr_metadata
    except Exception as exc:
        logger.error("GitHub API 调用失败: %s", exc)
        sys.exit(1)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    session_id = str(uuid.uuid4())

    # ── 获取 diff ──────────────────────────────────────────────────────────────
    if args.diff_file:
        diff_content, repo_name, pr_metadata = _load_from_diff_file(args.diff_file)
        repo_name   = args.repo
        pr_metadata = {"title": args.pr_title or f"Review of {Path(args.diff_file).name}"}
        pr_url      = None
    else:
        diff_content, repo_name, pr_metadata = _load_from_github(args.pr_url)
        if args.repo != "local/repo":
            repo_name = args.repo
        pr_url = args.pr_url

    logger.info("=" * 60)
    logger.info("MultiAgent Code Reviewer")
    logger.info("session_id : %s", session_id)
    logger.info("repo       : %s", repo_name)
    logger.info("pr_title   : %s", pr_metadata.get("title", ""))
    logger.info("diff_len   : %d chars", len(diff_content))
    logger.info("=" * 60)

    # ── 执行图 ─────────────────────────────────────────────────────────────────
    from src.graph.graph import review_graph

    initial_state = {
        "diff_content":    diff_content,
        "pr_metadata":     pr_metadata,
        "repo_name":       repo_name,
        "session_id":      session_id,
        "diff_files":      [],
        "diff_summary":    {},
        "routing_decision": {},
        "security_findings": [],
        "quality_findings":  [],
        "final_report":    None,
        "tool_call_log":   [],
        "agent_messages":  [],
        "errors":          [],
        "current_step":    "init",
        "review_complete": False,
    }

    config = {"configurable": {"thread_id": session_id}}

    logger.info("Graph 开始执行 ...")
    result = review_graph.invoke(initial_state, config=config)

    # ── 输出摘要日志 ───────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Graph 执行完成")
    logger.info("review_complete   : %s", result.get("review_complete"))
    logger.info("diff_files        : %d 个", len(result.get("diff_files", [])))
    summary = result.get("diff_summary", {})
    logger.info("pr_nature         : %s", summary.get("pr_nature", "-"))
    logger.info("estimated_risk    : %s", summary.get("estimated_risk", "-"))
    rd = result.get("routing_decision", {})
    logger.info("routing           : security=%s quality=%s priority=%s",
                rd.get("run_security"), rd.get("run_quality"), rd.get("priority"))
    logger.info("security_findings : %d 条", len(result.get("security_findings", [])))
    logger.info("quality_findings  : %d 条", len(result.get("quality_findings", [])))
    logger.info("tool_calls        : %d 次", len(result.get("tool_call_log", [])))
    logger.info("=" * 60)

    report = result.get("final_report", "")

    # ── 报告输出 ───────────────────────────────────────────────────────────────
    if report:
        # 写入文件
        if args.output:
            out_path = Path(args.output)
            out_path.write_text(report, encoding="utf-8")
            logger.info("报告已写入: %s", out_path.resolve())
        else:
            # 打印到终端
            print("\n" + "=" * 60, flush=True)
            print(report, flush=True)
            print("=" * 60, flush=True)

    # ── GitHub PR Comment 回写 ─────────────────────────────────────────────────
    if args.post_comment and pr_url and report:
        from config.settings import GITHUB_TOKEN
        from src.output.github_commenter import post_review_comment

        if not GITHUB_TOKEN:
            logger.warning("--post-comment 需要 GITHUB_TOKEN，已跳过")
        else:
            logger.info("正在回写 PR Comment ...")
            try:
                comment_url = post_review_comment(pr_url, GITHUB_TOKEN, report)
                logger.info("PR Comment 发布成功: %s", comment_url)
            except Exception as exc:
                logger.error("PR Comment 回写失败: %s", exc)

    # ── Checkpointing 验证 ─────────────────────────────────────────────────────
    saved = review_graph.get_state(config)
    if saved.values.get("review_complete"):
        logger.info("Checkpointing 验证通过 ✓")
    else:
        logger.warning("Checkpointing 验证：review_complete=False")

    # 错误汇总
    errors = result.get("errors", [])
    if errors:
        logger.warning("本次运行出现 %d 个错误:", len(errors))
        for e in errors:
            logger.warning("  %s", e)

    return 0 if result.get("review_complete") else 1


if __name__ == "__main__":
    sys.exit(main())
