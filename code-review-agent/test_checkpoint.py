"""验证 checkpointer 和 long-term memory 均可连接 Supabase。"""
from dotenv import load_dotenv
load_dotenv()

print("── 测试 long_term memory (psycopg2) ──")
from src.harness.memory.long_term import get_long_term_memory
mem = get_long_term_memory()
mem._ensure_schema()
print("✅ review_findings 表就绪")

print("\n── 测试 checkpointer (psycopg3) ──")
from src.harness.checkpointer import get_checkpointer
cp = get_checkpointer()
print(f"✅ Checkpointer 类型: {type(cp).__name__}")

print("\n── 测试 supervisor_graph 编译 ──")
from src.graph.supervisor_graph import supervisor_graph
print("✅ supervisor_graph 编译完成")
