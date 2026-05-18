# TODO: LangSmith 评测体系

## 目标

基于 LangSmith SDK 构建完整的 Agent 测试与评测体系，覆盖两类指标：

- **运营指标**：工具调用成功率、重试层效果、节点延迟、token 消耗
- **质量指标**：漏洞检出率、路由准确率、误报率

最终产出可量化、可复现的数据，用于验证简历中的各项数字。

---

## 涉及改动的文件

### 新建文件

| 文件 | 说明 |
|------|------|
| `tests/fixtures/dependency_vuln.diff` | 含已知 CVE 漏洞的依赖升级 diff |
| `tests/fixtures/docs_only.diff` | 纯文档变更，预期跳过所有 Reviewer |
| `tests/fixtures/missing_tests.diff` | 新增业务代码但无测试文件，预期触发 TestCoverageReviewer |
| `tests/eval_suite.py` | 批量测试运行器，执行所有 fixture 并采集指标 |
| `tests/analyze_langsmith.py` | LangSmith SDK 脚本，聚合多次运行的运营指标 |
| `tests/eval_report.py` | 汇总两类指标，输出对比报告 |

### 修改文件

| 文件 | 改动说明 |
|------|---------|
| `src/harness/tool_guard.py` | 记录每次工具调用的尝试次数（attempt_number），写入 ToolCallRecord |
| `.env.example` | 补充 LangSmith 相关环境变量说明 |

---

## 第一步：确认 LangSmith 环境变量

在 `.env` 中补充（已接入过的跳过）：

```bash
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__xxxxxxxxxx      # LangSmith 控制台 → Settings → API Keys
LANGCHAIN_PROJECT=code-reviewer       # 项目名，自定义
```

不需要改任何业务代码，LangGraph 自动上报所有节点的 span。

---

## 第二步：修改 tool_guard.py，记录尝试次数

当前 `ToolCallRecord` 没有记录"第几次尝试成功"，无法区分"首次成功"和"重试后成功"。

在 `ToolCallRecord` TypedDict 里加一个字段：

```python
# src/graph/state.py 的 ToolCallRecord
attempt_number: int    # 第几次尝试（1=首次，2/3=重试后）
```

在 `tool_guard.py` 的重试循环里，把当前 attempt 序号写入 record：

```python
for attempt in range(1, MAX_RETRIES + 1):
    try:
        result = tool_fn.invoke(args)
        record["attempt_number"] = attempt   # 记录是第几次成功的
        record["approved"] = True
        break
    except Exception:
        if attempt == MAX_RETRIES:
            record["attempt_number"] = attempt
            record["approved"] = False
```

这样 `tool_call_log` 里就有了每次调用的完整信息，后续脚本可以精确计算重试效果。

---

## 第三步：新增三个测试 Fixture

### `tests/fixtures/dependency_vuln.diff`

内容：修改 `requirements.txt`，加入含已知 CVE 的包版本。

预期行为：
- Coordinator 检测到依赖文件变更 → `run_dependency=True`
- DependencyReviewer 触发 → query_osv 返回漏洞 → findings 非空
- 报告中有 HIGH 或 CRITICAL 级别的依赖漏洞

具体包版本参考（写入 diff）：
```
+Pillow==9.0.0          # CVE-2023-44271，HIGH
+requests==2.18.0       # CVE-2023-32681，MEDIUM
```

### `tests/fixtures/docs_only.diff`

内容：只修改 `README.md` 和 `docs/setup.md`。

预期行为：
- DiffAnalyzer 识别 `change_category=docs`
- Coordinator 路由：所有 Reviewer 均不触发
- `route_after_coordinator` 返回 `["report_generator"]`
- 报告中 findings 总数为 0，无多余 LLM 调用

### `tests/fixtures/missing_tests.diff`

内容：新增一个业务文件（如 `src/payment/processor.py`，含完整函数逻辑），不含任何测试文件变更。

预期行为：
- Coordinator `run_test_coverage=True`（有业务变更且无测试文件）
- TestCoverageReviewer 触发 → findings 包含"缺少测试覆盖"类型

---

## 第四步：eval_suite.py（批量测试运行器）

批量执行所有 fixture，采集每次运行的关键指标，写入 JSON 结果文件。

```
输入：tests/fixtures/ 下的所有 .diff 文件
输出：tests/results/eval_results_{timestamp}.json
```

每个 fixture 运行后记录：

```json
{
  "fixture": "dependency_vuln.diff",
  "session_id": "xxx",
  "langsmith_run_id": "xxx",       // 用于后续从 LangSmith 拉 span 数据
  "review_complete": true,
  "routing_decision": {
    "run_security": true,
    "run_quality": true,
    "run_dependency": true,
    "run_test_coverage": false
  },
  "findings_count": {
    "security": 2,
    "quality": 1,
    "dependency": 2,
    "test_coverage": 0
  },
  "tool_calls": {
    "total": 8,
    "success": 7,
    "first_attempt_success": 6,
    "retry_success": 1,
    "failed": 1
  },
  "duration_seconds": 42.3,
  "errors": []
}
```

**预期结果定义**（写在 eval_suite.py 里，用于路由准确率判断）：

```python
EXPECTED = {
    "dependency_vuln.diff": {
        "should_trigger": ["security_reviewer", "dependency_reviewer"],
        "min_findings": {"dependency": 1},
    },
    "docs_only.diff": {
        "should_trigger": [],           # 所有 Reviewer 均不触发
        "max_findings": {"total": 0},
    },
    "missing_tests.diff": {
        "should_trigger": ["test_coverage_reviewer"],
        "min_findings": {"test_coverage": 1},
    },
    "sample.diff": {
        "should_trigger": ["security_reviewer", "quality_reviewer"],
        "min_findings": {"security": 1},  # 内置 SQL 注入，至少要发现 1 条
    },
}
```

路由准确率 = 实际触发的 Reviewer 集合与 `should_trigger` 完全匹配的比例。

---

## 第五步：analyze_langsmith.py（LangSmith 运营指标脚本）

从 LangSmith 拉取指定项目的所有工具调用 span，聚合统计：

```python
from langsmith import Client

def collect_tool_metrics(project_name: str, limit: int = 500):
    client = Client()
    
    # 按工具名聚合
    stats = {}   # tool_name → {total, success, retry_success, errors}
    
    # 拉 tool 类型的 run
    runs = client.list_runs(project_name=project_name, run_type="tool", limit=limit)
    
    # 聚合逻辑（见完整实现）
    ...
    
    return stats
```

输出格式：

```
工具调用成功率报告
==========================================
工具名                    总计  成功  成功率  重试成功  首次成功率
------------------------------------------
fetch_file_content         35    33   94.3%    2      88.6%
query_osv                  23    21   91.3%    1      87.0%
semgrep_scan               25    22   88.0%    3      76.0%
tavily_search              24    24  100.0%    0     100.0%
fetch_repo_readme          20    20  100.0%    0     100.0%
------------------------------------------
合计                      127   120   94.5%    6      89.8%

重试层效果：首次成功率 89.8% → 含重试后成功率 94.5%，提升 4.7 个百分点
```

注意：这里"首次成功率"就是 attempt_number=1 的比例，是工具的"原始"成功率，对应简历里的"78% 基准"（需跑足够多次才收敛）。

---

## 第六步：eval_report.py（汇总报告）

整合 eval_suite.py 的结果文件 + analyze_langsmith.py 的数据，输出一份 Markdown 格式的综合评测报告：

```markdown
# Agent 评测报告

## 运营指标
| 指标 | 数值 |
|------|------|
| 工具调用总成功率 | 94.5% |
| 首次尝试成功率（基准） | 89.8% |
| 重试层效果（提升） | +4.7pp |
| 平均端到端耗时 | 38.2s |
| 平均 token 消耗 / 次 | 12,400 |

## 质量指标
| 场景 | 路由正确 | 预期漏洞检出 |
|------|---------|------------|
| SQL 注入 diff | ✅ | ✅ (3/3) |
| 依赖漏洞 diff | ✅ | ✅ (2/2) |
| 纯文档 diff | ✅ | ✅ (0 findings) |
| 缺少测试 diff | ✅ | ✅ (2 findings) |

路由准确率：4/4 = 100%
```

---

## 简历写法参考

待所有脚本跑通、数据收集完成后，可以用以下角度写简历亮点（字数根据实际数据填入）：

**方向一（与 Tool Guard 合并）：**
> 六层工具执行保护链 + LangSmith 量化评测：……（Tool Guard 描述）……；基于 LangSmith SDK
> 构建工具调用成功率采集脚本，通过 attempt_number 字段区分首次成功与重试后成功，精确量化
> 重试层效果；结合预设场景 diff 集（SQL 注入 / CVE 依赖 / 纯文档）定义路由预期，路由准确率
> 达 100%，工具调用成功率从首次 X% 提升至含重试 Y%。

**方向二（独立作为测试体系亮点）：**
> 可量化 Agent 评测体系：基于 LangSmith SDK 设计双维评测框架，运营维度采集工具调用成功率、
> 重试层效果、节点延迟、token 消耗；质量维度通过预设 diff 场景集定义路由预期与漏洞检出预期，
> 自动验证 Coordinator 路由准确率与 Reviewer 检出率；将简历技术数字从理论推算转化为可复现
> 的测量结果。

---

## 执行顺序

1. 确认 `.env` 里 LangSmith 三个变量已配置
2. 修改 `src/graph/state.py`，`ToolCallRecord` 加 `attempt_number` 字段
3. 修改 `src/harness/tool_guard.py`，写入 `attempt_number`
4. 新建三个 fixture diff 文件（`dependency_vuln.diff` / `docs_only.diff` / `missing_tests.diff`）
5. 新建 `tests/eval_suite.py`
6. 新建 `tests/analyze_langsmith.py`
7. 新建 `tests/eval_report.py`
8. 跑 `eval_suite.py`，积累至少 10 次运行数据
9. 跑 `analyze_langsmith.py`，拿到真实的工具调用成功率
10. 跑 `eval_report.py`，生成汇总报告，核对简历数字
