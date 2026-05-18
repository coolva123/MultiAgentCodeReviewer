from dotenv import load_dotenv
load_dotenv()
from src.harness.memory.long_term import get_long_term_memory
mem = get_long_term_memory()
mem._ensure_schema()
print("连接成功，表已创建")
