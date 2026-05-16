"""
Code static analysis tools — Day 3 + TODO-SEC-01 + TODO-QUAL-01.

Provides LangChain @tool-decorated functions callable both as agent tools
and directly via .invoke(). Security tools: semgrep_scan + scan_secrets.
Quality tools: ast_analyze + semgrep_scan (p/maintainability) + ruff_check.
"""
import ast
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── Secret / Credential patterns ─────────────────────────────────────────────
_SECRET_PATTERNS = [
    (r'(?i)(password|passwd|pwd)\s*[=:]\s*["\'][^"\']{4,}["\']',   "hardcoded_password"),
    (r'(?i)(api_key|apikey|api[-_]key|auth_token)\s*[=:]\s*["\'][^"\']{8,}["\']', "hardcoded_api_key"),
    (r'AKIA[0-9A-Z]{16}',                                            "aws_access_key_id"),
    (r'(?i)aws_secret_access_key\s*[=:]\s*["\'][^"\']+["\']',       "aws_secret_key"),
    (r'(?i)(secret_key|secretkey)\s*[=:]\s*["\'][^"\']{8,}["\']',   "hardcoded_secret"),
    (r'(?i)(database_url|db_url)\s*[=:]\s*["\'][^"\']*:[^"\']*@[^"\']+["\']', "db_credentials_in_url"),
    (r'sk-[a-zA-Z0-9]{32,}',                                         "openai_api_key"),
    (r'(?i)(redis|postgres|mysql|mongodb)://[^:\s"\']+:[^@\s"\']+@', "db_url_with_credentials"),
    (r'(?i)bearer\s+[a-zA-Z0-9\-._~+/]{20,}',                       "bearer_token"),
    (r'django-insecure-[a-zA-Z0-9]+',                                 "django_insecure_key"),
]

# ── SQL injection patterns ────────────────────────────────────────────────────
_SQL_PATTERNS = [
    (r'f["\'].*\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b.*\{',  "sql_fstring_injection"),
    (r'["\'].*\b(SELECT|INSERT|UPDATE|DELETE)\b.*["\']\s*\+', "sql_string_concat"),
    (r'\.execute\(\s*[^,)]*\s*\+',                            "sql_execute_concat"),
    (r'%\s*[({].*\b(query|sql|stmt)\b',                       "sql_percent_format"),
    (r'format\(.*\b(query|sql)\b',                            "sql_format_call"),
]


def _extract_added_lines(patch: str) -> list[tuple[int, str]]:
    """Return (line_number, content) pairs for all added lines in a unified diff patch."""
    result: list[tuple[int, str]] = []
    line_no = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) - 1 if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            line_no += 1
            result.append((line_no, raw[1:]))
        elif not raw.startswith("-"):
            line_no += 1
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Security tools
# ─────────────────────────────────────────────────────────────────────────────

@tool
def bandit_scan(source_code: str, filename: str) -> str:
    """
    对从 diff 中提取的 Python 源码运行 bandit 安全扫描器。
    返回包含安全问题列表和严重等级的 JSON 字符串。
    仅对 .py 文件有效，其他文件类型返回空列表。
    """
    if not filename.endswith(".py"):
        return json.dumps({"issues": [], "note": "bandit only supports Python files"})

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(source_code)
            tmp_path = tmp.name

        proc = subprocess.run(
            [sys.executable, "-m", "bandit", "-f", "json", "-q", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        Path(tmp_path).unlink(missing_ok=True)

        data = json.loads(proc.stdout or "{}")
        issues = [
            {
                "line":       r.get("line_number"),
                "severity":   r.get("issue_severity", "").lower(),
                "confidence": r.get("issue_confidence", "").lower(),
                "test_id":    r.get("test_id"),
                "title":      r.get("issue_text", ""),
                "more_info":  r.get("more_info", ""),
            }
            for r in data.get("results", [])
        ]
        logger.debug("[bandit_scan] %s → %d issue(s)", filename, len(issues))
        return json.dumps({"issues": issues, "total": len(issues)})

    except subprocess.TimeoutExpired:
        return json.dumps({"issues": [], "error": "bandit timed out"})
    except Exception as exc:
        logger.warning("[bandit_scan] error: %s", exc)
        return json.dumps({"issues": [], "error": str(exc)})


@tool
def scan_secrets(patch: str) -> str:
    """
    扫描 unified diff patch 中的硬编码密钥、API Key、密码和凭据。
    仅检查新增行（以 + 开头），使用已知正则模式匹配。
    返回 JSON 字符串，列出每个检测到的密钥及其行号和模式类型。
    """
    added = _extract_added_lines(patch)
    findings: list[dict] = []

    for line_no, line in added:
        for pattern, pattern_type in _SECRET_PATTERNS:
            if re.search(pattern, line):
                findings.append({
                    "line":         line_no,
                    "pattern_type": pattern_type,
                    "snippet":      line.strip()[:120],
                })
                break  # one finding per line is enough

    logger.debug("[scan_secrets] %d secret(s) found", len(findings))
    return json.dumps({"secrets_found": findings, "total": len(findings)})


@tool
def scan_sql_injection(patch: str) -> str:
    """
    扫描 unified diff patch 中常见的 SQL 注入漏洞模式，包括 f-string 拼接 SQL、
    execute() 中的字符串拼接以及 format() 调用。
    返回包含潜在 SQL 注入风险和行号的 JSON 字符串。
    """
    added = _extract_added_lines(patch)
    findings: list[dict] = []

    for line_no, line in added:
        for pattern, pattern_type in _SQL_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append({
                    "line":         line_no,
                    "pattern_type": pattern_type,
                    "snippet":      line.strip()[:120],
                })
                break

    logger.debug("[scan_sql_injection] %d SQL risk(s) found", len(findings))
    return json.dumps({"sql_risks": findings, "total": len(findings)})


# ─────────────────────────────────────────────────────────────────────────────
# Quality tool
# ─────────────────────────────────────────────────────────────────────────────

@tool
def ast_analyze(source_code: str, filename: str) -> str:
    """
    使用 AST 模块分析 Python 源码，提取质量指标：函数数量、每个函数的圈复杂度估算、
    函数行数以及最大控制流嵌套深度。返回 JSON 字符串，包含每个函数的指标
    以及超出质量阈值的函数列表。仅对 .py 文件有效。
    """
    if not filename.endswith(".py"):
        return json.dumps({"metrics": {}, "note": "AST analysis only supports Python files"})

    try:
        tree = ast.parse(source_code)
    except SyntaxError as exc:
        return json.dumps({"error": f"SyntaxError: {exc}", "functions": []})

    functions: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        start = node.lineno
        end = getattr(node, "end_lineno", start)
        length = end - start + 1

        branch_nodes = (ast.If, ast.While, ast.For, ast.ExceptHandler,
                        ast.With, ast.Assert, ast.comprehension, ast.BoolOp)
        branches = sum(1 for n in ast.walk(node) if isinstance(n, branch_nodes))
        complexity = branches + 1

        max_depth = _max_nesting_depth(node)

        arg_count = len(node.args.args) + len(node.args.posonlyargs)

        functions.append({
            "name":                 node.name,
            "line":                 start,
            "length":               length,
            "cyclomatic_complexity": complexity,
            "max_nesting_depth":    max_depth,
            "arg_count":            arg_count,
            "is_too_long":          length > 50,
            "is_too_complex":       complexity > 10,
            "is_deeply_nested":     max_depth > 4,
            "too_many_args":        arg_count > 7,
        })

    issues = [f for f in functions
              if f["is_too_long"] or f["is_too_complex"]
              or f["is_deeply_nested"] or f["too_many_args"]]

    logger.debug("[ast_analyze] %d function(s), %d issue(s)", len(functions), len(issues))
    return json.dumps({
        "total_functions": len(functions),
        "functions":       functions,
        "quality_issues":  issues,
    })


def _max_nesting_depth(node: ast.AST, depth: int = 0) -> int:
    """Recursively compute max control-flow nesting depth starting from `node`."""
    control = (ast.If, ast.While, ast.For, ast.With, ast.Try, ast.ExceptHandler,
               ast.AsyncFor, ast.AsyncWith)
    best = depth
    for child in ast.iter_child_nodes(node):
        increment = 1 if isinstance(child, control) else 0
        best = max(best, _max_nesting_depth(child, depth + increment))
    return best


# ─────────────────────────────────────────────────────────────────────────────
# TODO-SEC-01 + TODO-QUAL-01: Semgrep multi-language scanner
# ─────────────────────────────────────────────────────────────────────────────

@tool
def semgrep_scan(source_code: str, filename: str, config: str = "p/security") -> str:
    """
    对源码运行 Semgrep 静态分析。支持 30+ 种语言，包括 Python、Java、Go、
    JavaScript、TypeScript、Ruby、C/C++ 和 Kotlin。

    config 选项：
      - "p/security"       : OWASP Top 10、注入、认证缺陷（供 SecurityReviewer 使用）
      - "p/maintainability": 代码异味、复杂度、死代码（供 QualityReviewer 使用）
      - "p/python"         : Python 专属最佳实践（QualityReviewer 补充规则）
      - "auto"             : 根据文件类型自动选择规则

    返回包含 findings 列表和总数的 JSON 字符串。
    """
    suffix = Path(filename).suffix or ".txt"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as tmp:
        tmp.write(source_code)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            ["semgrep", "--config", config, "--json", "--quiet", tmp_path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(proc.stdout or "{}")
        findings = [
            {
                "line":     r.get("start", {}).get("line"),
                "severity": r.get("extra", {}).get("severity", "").lower(),
                "rule_id":  r.get("check_id"),
                "title":    r.get("extra", {}).get("message", ""),
                "fix":      r.get("extra", {}).get("fix"),
            }
            for r in data.get("results", [])
        ]
        logger.debug("[semgrep_scan] %s config=%s → %d finding(s)", filename, config, len(findings))
        return json.dumps({"findings": findings, "total": len(findings)})
    except subprocess.TimeoutExpired:
        return json.dumps({"findings": [], "error": "semgrep timed out"})
    except Exception as exc:
        logger.warning("[semgrep_scan] error: %s", exc)
        return json.dumps({"findings": [], "error": str(exc)})
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# TODO-QUAL-01: Ruff Python quality linter
# ─────────────────────────────────────────────────────────────────────────────

@tool
def ruff_check(source_code: str, filename: str) -> str:
    """
    对 Python 源码运行 Ruff linter。覆盖 500+ 条规则，包括命名规范、
    未使用导入、错误处理模式和类型注解最佳实践。仅对 .py 文件有效。
    """
    if not filename.endswith(".py"):
        return json.dumps({"issues": [], "note": "ruff only supports Python files"})

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
        tmp.write(source_code)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            ["ruff", "check", "--output-format=json", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout or "[]")
        issues = [
            {
                "line":  item.get("location", {}).get("row"),
                "rule":  item.get("code"),
                "title": item.get("message"),
                "fix":   item.get("fix", {}).get("message") if item.get("fix") else None,
            }
            for item in data
        ]
        logger.debug("[ruff_check] %s → %d issue(s)", filename, len(issues))
        return json.dumps({"issues": issues, "total": len(issues)})
    except subprocess.TimeoutExpired:
        return json.dumps({"issues": [], "error": "ruff timed out"})
    except Exception as exc:
        logger.warning("[ruff_check] error: %s", exc)
        return json.dumps({"issues": [], "error": str(exc)})
    finally:
        Path(tmp_path).unlink(missing_ok=True)
