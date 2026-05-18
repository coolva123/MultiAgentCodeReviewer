# TODO: Context Enrichment Agent

## 目标

在现有审查流程开始之前，增加一个 **Context Enrichment** 阶段，让后续所有 reviewer 在
分析 diff 之前就已理解：项目是做什么的、改动文件周边有哪些相关文件、历史上这个项目
出现过哪些重要问题。

整体分三层实现，最终把所有上下文合并写入 `state.project_context`，供所有下游 agent 读取。

---

## 涉及改动的文件

### 新建文件

| 文件 | 说明 |
|------|------|
| `src/agents/context_enrichment.py` | 新节点主逻辑（三层上下文填充） |
| `src/harness/memory/project_profile.py` | project_profiles 表的读写封装 |
| `src/prompts/context_enrichment.py` | 生成项目画像时用的 LLM prompt |

### 修改文件

| 文件 | 改动说明 |
|------|---------|
| `src/graph/state.py` | 新增 `project_context: Dict[str, Any]` 字段 |
| `src/graph/supervisor_graph.py` | 注册 `context_enrichment` 节点，加边 |
| `src/agents/supervisor.py` | 第一轮路由到 `context_enrichment`，第二轮才路由到 `review_pipeline` |
| `main.py` | `initial_state` 加 `"project_context": {}` |
| `server.py` | `_base_state()` 加 `"project_context": {}` |
| `tests/test_supervisor_flow.py` | `_build_initial_state()` 加 `"project_context": {}` |

---

## 数据库：新建 project_profiles 表

在 Supabase 执行以下 SQL（同一个 PG 实例，不需要新数据库）：

```sql
CREATE TABLE IF NOT EXISTS project_profiles (
    repo_name       TEXT PRIMARY KEY,
    tech_stack      TEXT,
    project_type    TEXT,
    security_level  TEXT DEFAULT 'medium',   -- high / medium / low
    frameworks      TEXT,                    -- 逗号分隔的框架列表
    conventions     TEXT,                    -- 代码约定与注意事项
    summary         TEXT,                    -- 200 字以内项目简介
    readme_sha      TEXT,                    -- README 的 git SHA，用于检测变化
    raw_profile     JSONB,                   -- 完整结构化画像，备用
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 三层实现细节

---

### 第一层：项目画像（Project Profile）

**文件**：`src/harness/memory/project_profile.py`

实现两个函数：

```
get_profile(repo_name, current_readme_sha) -> dict | None
    查询 project_profiles 表
    缓存命中条件：记录存在 AND (readme_sha == current_readme_sha OR updated_at > now()-30天)
    命中 → 返回画像 dict
    未命中 → 返回 None

save_profile(repo_name, profile_dict, readme_sha)
    upsert 到 project_profiles 表
    更新 updated_at = now()
```

**文件**：`src/prompts/context_enrichment.py`

写一个 SYSTEM + HUMAN prompt，用于从 README + 目录结构 + CLAUDE.md 生成结构化项目画像。

输出要求 LLM 返回 JSON，包含字段：tech_stack / project_type / security_level / frameworks / conventions / summary。

用 `call_structured` + Pydantic 模型接收，避免 JSON 解析异常。

**context_enrichment_node 中第一层的逻辑**：

```
1. 调用 fetch_repo_readme 获取 README 内容
2. 从 README 响应头里取 sha（GitHub API 返回，需在 fetch_repo_readme 工具里一并返回）
   → 如果取不到 sha，用 README 内容的 md5 代替
3. 调用 get_profile(repo_name, readme_sha)
4. 缓存命中 → 直接用，跳过 LLM 调用
5. 缓存未命中 →
     a. 调用 fetch_repo_structure 获取目录树
     b. 调用 fetch_file_content 尝试读取 CLAUDE.md（404 就跳过）
     c. LLM 综合以上内容生成 ProfileModel（call_structured）
     d. 调用 save_profile 写入数据库
     e. 返回 profile dict
```

注意：fetch_repo_readme 工具需要小改，返回格式从纯文本改为 `{"content": str, "sha": str}`，
供第一层拿 sha 用。

---

### 第二层：改动文件周边上下文（轻量级，不额外调用 LLM）

**完全不调用 LLM**，用纯启发式规则选文件，控制 token 消耗。

**Step 1：从 raw diff_content 提取改动文件路径（正则，不用 unidiff）**

```python
import re
changed_files = re.findall(r'^diff --git a/(.+?) b/', diff_content, re.MULTILINE)
# 结果示例：["src/auth/login.py", "requirements.txt", "tests/test_auth.py"]
```

**Step 2：提取改动文件所在的目录集合**

```python
dirs = set(os.path.dirname(f) for f in changed_files if os.path.dirname(f))
# {"src/auth", "tests"}
```

**Step 3：调用 fetch_repo_structure 获取完整文件树**

已有工具，直接复用。

**Step 4：从文件树中过滤出"候选相关文件"**

过滤规则（按顺序应用，满足全部才进入候选）：

```
a. 文件在 changed_files 已有的目录里（同目录兄弟文件）
b. 不在 changed_files 本身（不重复读已有 diff 的文件）
c. 不是测试文件（不含 test_ / _test. / /tests/）
d. 不是文档（不以 .md .rst .txt 结尾）
e. 不是配置文件（不以 .yml .yaml .json .toml .cfg .ini 结尾）
f. 是代码文件（以 .py .js .ts .go .java .rb .rs 结尾）
```

**Step 5：按优先级排序，取前 N 个**

优先级评分（数字越高越优先）：

```
+3  文件名和某个 changed_file 有公共前缀（同模块）
    例：changed=auth/login.py，候选=auth/models.py → "auth" 匹配
+2  文件名包含 model / schema / base / core / util / service 等关键词
+1  文件扩展名和 changed_file 一致
```

取分数最高的前 **3 个文件**（硬上限，可配置）。

**Step 6：调用 fetch_file_content 读取这 3 个文件**

每个文件只取前 **80 行**（在 fetch_file_content 里截断），避免大文件撑爆 token。

---

### 第三层：历史 findings 检索（增强现有 pgvector 查询）

现有逻辑在 `supervisor.py` 里，需要迁移到 context_enrichment_node 中，并增强查询精度。

**增强点**：查询向量从"通用仓库名"改为"仓库名 + 改动文件路径 + 变更类型"拼接。

```python
# 现有方式（模糊）
query_text = f"security issues in {repo_name}"

# 增强后（更精准）
changed_summary = ", ".join(changed_files[:5])   # 最多 5 个文件名
query_text = f"{repo_name} 改动文件: {changed_summary}"
```

结果依然取 top-5，格式不变，直接复用现有 `get_long_term_memory().query()`。

同时从 `supervisor.py` 里删掉原有的历史 findings 查询逻辑（避免重复查询）。

---

## context_enrichment_node 最终输出

三层内容合并写入 `state.project_context`：

```python
return {
    "project_context": {
        "profile": {                  # 第一层
            "tech_stack": "...",
            "project_type": "...",
            "security_level": "high",
            "frameworks": "...",
            "conventions": "...",
            "summary": "...",
            "from_cache": True/False,
        },
        "related_files": [            # 第二层
            {"path": "src/auth/models.py", "content": "...（前80行）"},
            {"path": "src/auth/utils.py",  "content": "...（前80行）"},
        ],
        "historical_findings": "...", # 第三层，格式同现有 historical_context
    },
    "agent_messages": ["[ContextEnrichment] 完成 | profile=cached | related_files=2 | history=3条"],
}
```

---

## Supervisor 路由改动

`supervisor.py` 里第一轮的路由逻辑调整：

```
现有逻辑：
  iteration=0 → research_agent（可选）→ review_pipeline

新逻辑：
  iteration=0, project_context 为空 → context_enrichment
  iteration=1（context_enrichment 完成后回来）→ review_pipeline（或 research_agent）
```

在 `supervisor_graph.py` 里新增：

```python
builder.add_node("context_enrichment", context_enrichment_node)
builder.add_edge("context_enrichment", "supervisor")   # 完成后回到 supervisor
```

---

## Token 消耗预估（每次 PR）

| 来源 | 消耗 | 备注 |
|------|------|------|
| fetch_repo_readme | ~1000 tokens | 读入，不是 LLM |
| fetch_repo_structure | ~200 tokens | 读入，不是 LLM |
| fetch_file_content × 3 | ~600 tokens | 每文件 80 行 |
| LLM 生成 profile（缓存未命中） | ~800 tokens | 30 天最多一次 |
| pgvector 历史查询 | ~500 tokens | 读入，不是 LLM |
| **合计（缓存命中时）** | **~1300 tokens** | 无 LLM 调用 |
| **合计（缓存未命中时）** | **~2100 tokens** | 含一次 LLM 调用 |

---

## 执行顺序

1. 在 Supabase 手动执行建表 SQL
2. 修改 `fetch_repo_readme` 工具，返回格式加上 sha
3. 新建 `src/harness/memory/project_profile.py`
4. 新建 `src/prompts/context_enrichment.py`
5. 新建 `src/agents/context_enrichment.py`
6. 修改 `src/graph/state.py`，加 `project_context` 字段
7. 修改 `src/graph/supervisor_graph.py`，注册节点和边
8. 修改 `src/agents/supervisor.py`，调整路由逻辑
9. 修改 `src/agents/supervisor.py`，删除原有历史 findings 查询（已迁移到第三层）
10. 修改 `main.py` / `server.py` / `tests/test_supervisor_flow.py`，initial_state 加字段
