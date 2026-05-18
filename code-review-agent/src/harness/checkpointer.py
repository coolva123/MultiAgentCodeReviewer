"""
Checkpointer 工厂 — 返回 PostgreSQL 持久化 Checkpointer。

用途：
  - 每个 superstep 完成后将完整 ReviewState 写入 Supabase PostgreSQL
  - 支持断点续跑（同一 thread_id 重跑时从上次中断节点继续）
  - tool_call_log / agent_messages / errors 等审计字段随 State 一并持久化

降级策略：
  - PG_DATABASE_URL 未配置或连接失败时，静默降级为 MemorySaver（进程内有效）
  - 降级不影响审查流程，仅失去跨进程持久化能力
"""
import logging

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

_checkpointer = None


def get_checkpointer():
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    try:
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        from config.settings import PG_DATABASE_URL

        if not PG_DATABASE_URL or "localhost" in PG_DATABASE_URL:
            raise ValueError("PG_DATABASE_URL 未配置云端地址，降级为 MemorySaver")

        pool = ConnectionPool(
            conninfo=PG_DATABASE_URL,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=True,
        )
        saver = PostgresSaver(pool)
        saver.setup()  # 自动建 checkpoints / checkpoint_blobs / checkpoint_writes 三张表
        _checkpointer = saver
        logger.info("[Checkpointer] PostgresSaver 初始化成功（Supabase）")

    except Exception as exc:
        logger.warning("[Checkpointer] PostgresSaver 不可用，降级为 MemorySaver: %s", exc)
        _checkpointer = MemorySaver()

    return _checkpointer
