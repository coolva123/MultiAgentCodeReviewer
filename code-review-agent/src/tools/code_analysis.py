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
    Run the bandit security scanner on Python source code extracted from a diff.
    Returns a JSON string with a list of security issues and severity levels.
    Only effective for .py files; returns empty list for other file types.
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
    Scan a unified diff patch for hardcoded secrets, API keys, passwords, and credentials.
    Checks only added lines (+) using known regex patterns.
    Returns a JSON string listing each detected secret with line number and pattern type.
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
    Scan a unified diff patch for common SQL injection vulnerability patterns such as
    f-string SQL construction, string concatenation in execute(), and format() calls.
    Returns a JSON string of potential SQL injection risks with line numbers.
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
    Analyze Python source code using the AST module to extract quality metrics:
    function count, per-function cyclomatic complexity estimate, function length in lines,
    and maximum control-flow nesting depth. Returns a JSON string with per-function
    metrics and a list of functions that exceed quality thresholds.
    Only effective for .py files.
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
    Run Semgrep static analysis on source code. Supports 30+ languages including
    Python, Java, Go, JavaScript, TypeScript, Ruby, C/C++, and Kotlin.

    config options:
      - "p/security"       : OWASP Top 10, injection, auth flaws (SecurityReviewer)
      - "p/maintainability": code smells, complexity, dead code (QualityReviewer)
      - "p/python"         : Python-specific best practices (QualityReviewer supplement)
      - "auto"             : auto-select rules based on file type

    Returns JSON with findings list and total count.
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
    Run Ruff linter on Python source code. Covers 500+ rules including naming
    conventions, unused imports, error handling patterns, and type annotation
    best practices. Only effective for .py files.
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
