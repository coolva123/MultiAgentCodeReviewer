"""
报告格式化器 — Day 6。

职责：把结构化 findings + 元数据 → 专业 Markdown 报告。
设计：
  - 按严重级别分组（critical → high → medium → low → info）
  - Security / Quality findings 统一排序后混合输出，优先级最高的在最前
  - 每条 finding 给唯一 ID（S-1 安全，Q-1 质量）
  - 顶部统计表格，末尾工具调用记录
"""
from datetime import datetime, timezone
from typing import Any


_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🔵",
    "info":     "⚪",
}

_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]


# ── 统计辅助 ──────────────────────────────────────────────────────────────────

def _count_by_severity(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in _SEVERITY_ORDER}
    for f in findings:
        sev = f.get("severity", "info").lower()
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _risk_badge(sec_findings: list[dict]) -> str:
    if any(f.get("severity") == "critical" for f in sec_findings):
        return "🔴 CRITICAL"
    if any(f.get("severity") == "high" for f in sec_findings):
        return "🟠 HIGH"
    if any(f.get("severity") == "medium" for f in sec_findings):
        return "🟡 MEDIUM"
    return "🟢 LOW"


# ── 单条 finding 格式化 ───────────────────────────────────────────────────────

def _format_finding(finding: dict, fid: str) -> list[str]:
    sev   = finding.get("severity", "info").lower()
    emoji = _EMOJI.get(sev, "⚪")
    file_ = finding.get("file", "unknown")
    line  = finding.get("line")
    loc   = f"`{file_}`" + (f" line {line}" if line else "")

    lines = [
        f"### {emoji} [{fid}] {finding.get('title', '(no title)')}",
        f"",
        f"| 字段 | 内容 |",
        f"|------|------|",
        f"| **位置** | {loc} |",
        f"| **分类** | `{finding.get('category', '-')}` |",
        f"| **严重性** | `{sev.upper()}` |",
        f"",
        f"**问题描述**",
        f"",
        f"> {finding.get('description', '')}",
        f"",
        f"**修复建议**",
        f"",
        f"> {finding.get('suggestion', '')}",
        f"",
    ]
    return lines


# ── 主入口 ────────────────────────────────────────────────────────────────────

def format_report(
    security_findings: list[dict],
    quality_findings: list[dict],
    diff_files: list[dict],
    pr_metadata: dict,
    repo_name: str,
    executive_summary: str,
    tool_call_log: list[dict] | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tool_call_log = tool_call_log or []

    sec_counts  = _count_by_severity(security_findings)
    qual_counts = _count_by_severity(quality_findings)
    total_sec   = len(security_findings)
    total_qual  = len(quality_findings)
    total_all   = total_sec + total_qual
    risk_badge  = _risk_badge(security_findings)

    lines: list[str] = []

    # ── 标题 & 元数据 ──────────────────────────────────────────────────────────
    lines += [
        "# 🔍 Code Review Report",
        "",
        f"| | |",
        f"|---|---|",
        f"| **仓库** | `{repo_name}` |",
        f"| **PR** | {pr_metadata.get('title', 'N/A')} |",
        f"| **风险等级** | {risk_badge} |",
        f"| **生成时间** | {now} |",
        f"| **总计发现** | {total_all} 条（安全 {total_sec} / 质量 {total_qual}）|",
        "",
        "---",
        "",
    ]

    # ── 执行摘要 ───────────────────────────────────────────────────────────────
    lines += [
        "## 📋 执行摘要",
        "",
        executive_summary,
        "",
        "---",
        "",
    ]

    # ── 统计表格 ───────────────────────────────────────────────────────────────
    lines += [
        "## 📊 统计概览",
        "",
        "| 类别 | 🔴 Critical | 🟠 High | 🟡 Medium | 🔵 Low | ⚪ Info | 合计 |",
        "|------|:-----------:|:-------:|:---------:|:------:|:------:|:----:|",
        f"| **安全审查** | {sec_counts['critical']} | {sec_counts['high']} | {sec_counts['medium']} | {sec_counts['low']} | {sec_counts['info']} | {total_sec} |",
        f"| **质量审查** | {qual_counts['critical']} | {qual_counts['high']} | {qual_counts['medium']} | {qual_counts['low']} | {qual_counts['info']} | {total_qual} |",
        "",
        "---",
        "",
    ]

    # ── 变更文件摘要 ───────────────────────────────────────────────────────────
    lines += [
        "## 📁 变更文件",
        "",
        "| 文件 | 类型 | +增 | -删 |",
        "|------|------|----:|----:|",
    ]
    for f in diff_files:
        lines.append(
            f"| `{f['filename']}` | {f.get('change_type', '?')} "
            f"| +{f.get('additions', 0)} | -{f.get('deletions', 0)} |"
        )
    lines += ["", "---", ""]

    # ── 按严重级别分组输出所有 findings ────────────────────────────────────────
    # 合并时标记来源（S=security, Q=quality），然后按 severity 排序
    tagged: list[tuple[str, int, dict]] = []
    for i, f in enumerate(security_findings):
        tagged.append(("S", i + 1, f))
    for i, f in enumerate(quality_findings):
        tagged.append(("Q", i + 1, f))

    def _sort_key(item: tuple) -> int:
        sev = item[2].get("severity", "info").lower()
        return _SEVERITY_ORDER.index(sev) if sev in _SEVERITY_ORDER else 99

    tagged.sort(key=_sort_key)

    # 按 severity 分节输出
    current_sev = None
    for prefix, idx, finding in tagged:
        sev = finding.get("severity", "info").lower()
        if sev != current_sev:
            current_sev = sev
            emoji = _EMOJI.get(sev, "⚪")
            label = sev.upper()
            count = sum(
                1 for _, _, ff in tagged
                if ff.get("severity", "info").lower() == sev
            )
            lines += [
                f"## {emoji} {label} Issues ({count} 条)",
                "",
            ]
        fid = f"{prefix}-{idx}"
        lines += _format_finding(finding, fid)

    lines += ["---", ""]

    # ── 工具调用记录 ───────────────────────────────────────────────────────────
    if tool_call_log:
        lines += [
            "## 🔧 工具调用记录",
            "",
            "| 工具 | 风险等级 | 状态 |",
            "|------|---------|------|",
        ]
        for rec in tool_call_log:
            status = "✅ 已执行" if rec.get("approved") else "❌ 已拒绝"
            lines.append(
                f"| `{rec['tool_name']}` | {rec['risk_level']} | {status} |"
            )
        lines += ["", "---", ""]

    lines += ["", "*本报告由 MultiAgent Code Reviewer 自动生成*"]

    return "\n".join(lines)
