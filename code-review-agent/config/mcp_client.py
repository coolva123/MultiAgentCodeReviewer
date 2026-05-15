"""
Semgrep MCP 客户端 — 同步兼容桥接层（stdio 传输）。

传输方式：stdio（进程间通信）
  - 不需要 HTTP 服务器，不需要端口，不受代理影响
  - 不需要 JWT 认证，SEMGREP_APP_TOKEN 直接传给子进程环境变量
  - semgrep 作为子进程由本模块直接管理，无需单独启动

MCP 协议本质是异步的，而项目 Agent 循环是同步代码。
解决方案：
  1. 启动一个后台 daemon 线程，在其中运行一个永久事件循环
  2. 每个 MCP 工具被包装成同步 StructuredTool，调用时通过
     run_coroutine_threadsafe 将协程提交到后台循环执行
  3. 从调用方视角看，MCP 工具和本地 @tool 函数完全一致

环境变量（在 .env 中配置）：
  SEMGREP_APP_TOKEN  — 从 semgrep.dev 获取的 Agent token（传给 semgrep 子进程）
"""
import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# ── 后台事件循环（daemon，主进程退出时自动销毁）────────────────────────────────
_bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_bg_thread: threading.Thread = threading.Thread(
    target=_bg_loop.run_forever,
    daemon=True,
    name="mcp-event-loop",
)
_bg_thread.start()


def _run_async(coro) -> Any:
    """将协程提交到后台事件循环，同步阻塞等待结果（最多 60 秒）。"""
    future = asyncio.run_coroutine_threadsafe(coro, _bg_loop)
    return future.result(timeout=60)


def _find_semgrep_bin() -> str:
    """优先使用项目 venv 里的 semgrep，找不到则退回 PATH。"""
    venv_bin = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "semgrep"
    if venv_bin.exists():
        return str(venv_bin)
    return "semgrep"


# ── MCP 工具列表缓存 ────────────────────────────────────────────────────────────
_raw_tools: list = []
_initialized: bool = False


async def _fetch_tools() -> list:
    """通过 stdio 启动 semgrep mcp 子进程，获取工具列表。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    semgrep_bin = _find_semgrep_bin()
    env = dict(os.environ)  # 传递完整环境变量（含 SEMGREP_APP_TOKEN）

    client = MultiServerMCPClient({
        "semgrep": {
            "command": semgrep_bin,
            "args": ["mcp", "--transport", "stdio"],
            "transport": "stdio",
            "env": env,
        }
    })
    tools = await client.get_tools()
    tool_names = [t.name for t in tools]
    logger.info("[MCPClient] Semgrep MCP (stdio) 连接成功 | 工具数=%d | %s", len(tools), tool_names)
    return tools


def _wrap_as_sync(mcp_tool) -> StructuredTool:
    """
    将 MCP 异步工具包装为同步 StructuredTool。
    每次 invoke 通过后台事件循环执行，调用方视角与本地 @tool 完全一致。
    """
    tool_name = mcp_tool.name

    def _sync_func(**kwargs) -> str:
        async def _invoke():
            from langchain_mcp_adapters.client import MultiServerMCPClient
            semgrep_bin = _find_semgrep_bin()
            env = dict(os.environ)
            client = MultiServerMCPClient({
                "semgrep": {
                    "command": semgrep_bin,
                    "args": ["mcp", "--transport", "stdio"],
                    "transport": "stdio",
                    "env": env,
                }
            })
            tools = await client.get_tools()
            for t in tools:
                if t.name == tool_name:
                    return await t.ainvoke(input=kwargs)
            return f'{{"error": "tool {tool_name} not found"}}'

        return _run_async(_invoke())

    return StructuredTool(
        name=mcp_tool.name,
        description=mcp_tool.description,
        args_schema=getattr(mcp_tool, "args_schema", None),
        func=_sync_func,
    )


def get_semgrep_mcp_tools() -> list:
    """
    返回 Semgrep MCP Server 提供的工具列表（同步兼容版本）。

    首次调用时启动 semgrep stdio 子进程获取工具列表并缓存。
    MCP 不可用时静默返回空列表，调用方自动降级到本地工具。
    """
    global _raw_tools, _initialized

    if _initialized:
        return [_wrap_as_sync(t) for t in _raw_tools]

    try:
        _raw_tools = _run_async(_fetch_tools())
        _initialized = True
        return [_wrap_as_sync(t) for t in _raw_tools]
    except Exception as exc:
        inner = exc
        if hasattr(exc, "exceptions") and exc.exceptions:
            inner = exc.exceptions[0]
        logger.warning(
            "[MCPClient] 无法连接 Semgrep MCP（stdio），降级到本地工具。\n"
            "  原因: %s: %s\n"
            "  请确认 SEMGREP_APP_TOKEN 已在 .env 中配置",
            type(inner).__name__, inner,
        )
        _initialized = True
        return []
