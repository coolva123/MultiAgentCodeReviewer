"""快速测试 supervisor_graph 全流程。运行：../.venv/bin/python test_graph.py"""
from src.graph.supervisor_graph import supervisor_graph

diff = """\
diff --git a/auth/login.py b/auth/login.py
--- a/auth/login.py
+++ b/auth/login.py
@@ -0,0 +1,10 @@
+import sqlite3
+DB_PASSWORD = "admin123"
+SECRET_KEY = "hardcoded-jwt-secret"
+def login(username, password):
+    query = "SELECT * FROM users WHERE username='" + username + "' AND password='" + password + "'"
+    conn = sqlite3.connect('users.db')
+    conn.execute(query)
+def change_password(user_id, new_pwd):
+    conn.execute(f"UPDATE users SET password='{new_pwd}' WHERE id={user_id}")
+    conn.commit()
"""

result = supervisor_graph.invoke({
    "diff_content": diff,
    "repo_name": "demo-webapp",
    "repo_url": "",
    "session_id": "py-test-001",
    "pr_metadata": {"title": "Add login"},
    "diff_files": [],
    "diff_summary": {},
    "routing_decision": {},
    "security_findings": [],
    "quality_findings": [],
    "final_report": None,
    "research_context": "",
    "supervisor_instruction": "",
    "iteration_count": 0,
    "review_pipeline_called": False,
    "tool_call_log": [],
    "agent_messages": [],
    "errors": [],
    "current_step": "init",
    "review_complete": False,
})

sec = result.get("security_findings", [])
qual = result.get("quality_findings", [])
tools = result.get("tool_call_log", [])
msgs = result.get("agent_messages", [])
errors = result.get("errors", [])

print("\n" + "="*60)
print("📊 执行结果摘要")
print("="*60)
print(f"Security findings : {len(sec)}")
print(f"Quality findings  : {len(qual)}")

print("\n" + "="*60)
print("🔧 工具调用记录")
print("="*60)
for t in tools:
    status = "✅" if t.get("approved") else "❌"
    print(f"  {status} {t['tool_name']} | risk={t['risk_level']} | result_len={len(str(t.get('result','')))} chars")

print("\n" + "="*60)
print("💬 Agent 消息时间线")
print("="*60)
for m in msgs:
    print(f"  {m}")

if errors:
    print("\n" + "="*60)
    print("⚠️  错误记录")
    print("="*60)
    for e in errors:
        print(f"  {e}")

print("\n" + "="*60)
print("🔍 Security Findings 详情")
print("="*60)
for i, f in enumerate(sec, 1):
    print(f"  [{i}] [{f['severity'].upper()}] {f['title']} — {f['file']}")

print("\n" + "="*60)
print("📋 REPORT (前800字)")
print("="*60)
print(result.get("final_report", "NO REPORT")[:800])
