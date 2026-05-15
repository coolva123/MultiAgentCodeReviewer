"""
Tool Guard：工具执行保护层 + HITL（Human-in-the-Loop）。

保护层级（按顺序执行）：
  1. 入参校验   — 对照工具的 args_schema 检查参数类型和必填项，拦截 LLM 格式错误
  2. 风险分级   — LOW / MEDIUM / HIGH，来自 tool_risk_config.yaml
  3. HITL 确认  — HIGH 风险工具首次调用时等待用户 y/n（并行 Agent 串行提示）
  4. 重试退避   — 工具失败后按配置重试 N 次，使用指数退避间隔
  5. 输出校验   — 期望返回 JSON 的工具做合法性检查，异常时返回结构化错误
  6. 审计记录   — 所有调用（含失败/拒绝）写入 ToolCallRecord
"""
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from src.graph.state import ToolCallRecord
from src.harness.memory.short_term import make_tool_record

logger = logging.getLogger(__name__)

_RISK_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "tool_risk_config.yaml"

# ── 配置缓存 ───────────────────────────────────────────────────────────────────
_risk_cache: dict[str, dict] = {}

# ── 会话级状态 ─────────────────────────────────────────────────────────────────
# 已批准工具集合：本次运行中批准过的工具不再询问
_session_approved: set[str] = set()

# 全局锁：确保并行 Agent 的 HITL 提示串行输出，防止终端内容交错
_hitl_lock = threading.Lock()


# ── 配置加载 ───────────────────────────────────────────────────────────────────

def _load_risk_config() -> dict[str, dict]:
    global _risk_cache
    if _risk_cache:
        return _risk_cache
    with open(_RISK_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    _risk_cache = raw.get("tools", {})
    return _risk_cache


def _get_tool_config(tool_name: str) -> dict:
    cfg = _load_risk_config().get(tool_name, {})
    return {
        "risk_level":  cfg.get("risk_level", "MEDIUM"),
        "max_retries": cfg.get("max_retries", 1),
        "retry_delay": cfg.get("retry_delay", 1.0),
    }


def get_risk_level(tool_name: str) -> str:
    return _get_tool_config(tool_name)["risk_level"]


# ── 核心入口 ───────────────────────────────────────────────────────────────────

def guarded_call(
    tool_fn,
    tool_name: str,
    args: dict[str, Any],
) -> tuple[Any, ToolCallRecord]:
    """
    执行工具并依次经过所有保护层。
    返回 (result, ToolCallRecord)；被拒绝或彻底失败时 result 为包含诊断信息的 JSON 字符串。
    """
    cfg        = _get_tool_config(tool_name)
    risk_level = cfg["risk_level"]

    # ── 第一层：入参校验 ───────────────────────────────────────────────────────
    validated_args, validation_error = _validate_args(tool_fn, tool_name, args)
    if validation_error:
        logger.warning("[ToolGuard] '%s' 入参校验失败: %s", tool_name, validation_error)
        error_payload = json.dumps({
            "error":   "invalid_arguments",
            "detail":  validation_error,
            "hint":    "请检查参数名称和类型是否符合工具定义",
        })
        record = make_tool_record(tool_name, risk_level, args, result=error_payload, approved=False)
        return error_payload, record

    # ── 第二层：风险分级 + HITL 确认 ──────────────────────────────────────────
    if risk_level == "HIGH":
        if tool_name not in _session_approved:
            with _hitl_lock:  # 串行化 HITL 提示，防止并行 Agent 输出交错
                if tool_name not in _session_approved:  # double-check
                    approved = _prompt_user(tool_name, validated_args)
                    if not approved:
                        logger.warning("[ToolGuard] HIGH-RISK tool '%s' rejected by user", tool_name)
                        error_payload = json.dumps({"error": "rejected_by_user", "tool": tool_name})
                        record = make_tool_record(tool_name, risk_level, args, result=error_payload, approved=False)
                        return error_payload, record
                    _session_approved.add(tool_name)
                    logger.info("[ToolGuard] HIGH-RISK tool '%s' approved by user", tool_name)
        else:
            logger.info("[ToolGuard] HIGH-RISK tool '%s' auto-approved (会话缓存)", tool_name)
    elif risk_level == "MEDIUM":
        logger.info("[ToolGuard] MEDIUM-RISK tool '%s' | args_keys=%s", tool_name, list(validated_args.keys()))

    # ── 第三层：执行 + 重试退避 ────────────────────────────────────────────────
    max_retries = cfg["max_retries"]
    retry_delay = cfg["retry_delay"]
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = retry_delay * attempt  # 指数退避：1x, 2x, 3x …
            logger.info("[ToolGuard] '%s' 第 %d 次重试（等待 %.1fs）", tool_name, attempt, wait)
            time.sleep(wait)

        try:
            result = tool_fn.invoke(validated_args)

            # ── 第四层：输出校验 ───────────────────────────────────────────────
            result_str = _validate_output(tool_name, result)

            record = make_tool_record(tool_name, risk_level, args, result=result_str[:500], approved=True)
            logger.info(
                "[ToolGuard] '%s' 执行成功 | risk=%s | attempt=%d | output_len=%d",
                tool_name, risk_level, attempt, len(result_str),
            )
            return result_str, record

        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[ToolGuard] '%s' 执行异常（attempt %d/%d）: %s",
                tool_name, attempt, max_retries, exc,
            )

    # 所有重试耗尽
    logger.error("[ToolGuard] '%s' 彻底失败（共 %d 次尝试）: %s", tool_name, max_retries + 1, last_exc)
    error_payload = json.dumps({
        "error":    "tool_execution_failed",
        "tool":     tool_name,
        "attempts": max_retries + 1,
        "detail":   str(last_exc),
        "hint":     _error_hint(tool_name, last_exc),
    })
    record = make_tool_record(tool_name, risk_level, args, result=error_payload, approved=True)
    return error_payload, record


# ── 入参校验 ───────────────────────────────────────────────────────────────────

def _validate_args(tool_fn, tool_name: str, args: dict[str, Any]) -> tuple[dict, str | None]:
    """
    对照工具的 args_schema 校验参数。
    - 本地 @tool：args_schema 是 Pydantic BaseModel，做完整类型校验
    - MCP 工具：args_schema 是 JSON Schema dict，只做必填字段存在性检查
    返回 (coerced_args, error_message)；error_message 为 None 表示校验通过。
    """
    schema = getattr(tool_fn, "args_schema", None)
    if schema is None:
        return args, None

    # MCP 工具：args_schema 是原始 JSON Schema 字典，不能当函数调用
    if isinstance(schema, dict):
        required = schema.get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            return args, f"缺少必填参数: {missing}"
        return args, None

    # 本地 @tool：args_schema 是 Pydantic BaseModel，做完整类型校验
    try:
        validated = schema(**args)
        return validated.model_dump(), None
    except Exception as exc:
        msg = str(exc).split("\n")[0]
        return args, msg


# ── 输出校验 ───────────────────────────────────────────────────────────────────

# 期望返回合法 JSON 的工具集合
_JSON_OUTPUT_TOOLS = {
    "semgrep_scan", "ruff_check", "ast_analyze",
    "scan_secrets", "scan_sql_injection", "bandit_scan",
    "security_check", "get_abstract_syntax_tree",
}


def _validate_output(tool_name: str, result: Any) -> str:
    """
    将工具输出规范化为字符串。
    对 JSON 工具额外验证可解析性；解析失败时包装为结构化错误返回给 LLM。
    """
    result_str = str(result)

    if tool_name not in _JSON_OUTPUT_TOOLS:
        return result_str

    try:
        json.loads(result_str)
        return result_str
    except json.JSONDecodeError:
        logger.warning("[ToolGuard] '%s' 返回了非法 JSON，已包装为错误对象", tool_name)
        return json.dumps({
            "error":  "invalid_json_output",
            "tool":   tool_name,
            "raw":    result_str[:200],
        })


# ── 错误诊断提示 ────────────────────────────────────────────────────────────────

def _error_hint(tool_name: str, exc: Exception | None) -> str:
    """根据工具类型和异常内容生成面向 LLM 的诊断建议。"""
    if exc is None:
        return ""
    msg = str(exc).lower()
    if "timeout" in msg:
        return "工具执行超时，可尝试缩减 source_code 长度或换用更简单的配置"
    if "not found" in msg or "no such file" in msg:
        return f"工具 {tool_name} 可能未安装，请确认环境依赖"
    if "permission" in msg:
        return "权限不足，请检查文件系统权限"
    if tool_name in ("security_check", "get_abstract_syntax_tree"):
        return "MCP 工具调用失败，请确认 Semgrep MCP Docker 容器正在运行"
    return "请检查入参格式是否正确，或尝试简化输入内容"


# ── HITL 终端提示 ───────────────────────────────────────────────────────────────

def _prompt_user(tool_name: str, args: dict[str, Any]) -> bool:
    """终端 HITL 确认提示。非交互模式下自动批准。"""
    print("\n" + "=" * 60, flush=True)
    print("[ToolGuard]  HIGH-RISK TOOL EXECUTION REQUEST", flush=True)
    print(f"  Tool   : {tool_name}", flush=True)
    print(f"  Args   : {_fmt_args(args)}", flush=True)
    print(f"  Reason : This tool spawns a subprocess on your system.", flush=True)
    print("=" * 60, flush=True)
    print("  (批准后本次运行中该工具不再询问)", flush=True)

    if not sys.stdin.isatty():
        print("[ToolGuard] Non-interactive mode — auto-approving.", flush=True)
        return True

    while True:
        try:
            answer = input("  Approve execution? [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.", flush=True)


def _fmt_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        v_str = str(v)
        parts.append(f"{k}={v_str[:80]}{'...' if len(v_str) > 80 else ''}")
    return ", ".join(parts)
