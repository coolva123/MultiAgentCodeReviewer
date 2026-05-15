from langgraph.checkpoint.memory import MemorySaver


def get_checkpointer() -> MemorySaver:
    """
    返回 MemorySaver 实例用于状态持久化。
    Day 1 使用内存版本（进程退出后状态丢失）。
    后续可替换为 SqliteSaver 或 PostgresSaver 实现跨进程持久化：

        from langgraph.checkpoint.sqlite import SqliteSaver
        return SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH)
    """
    return MemorySaver()
