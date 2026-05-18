# 在目标项目中接入 MultiAgent Code Reviewer — 完整操作指南

---

## 一、系统工作原理概览

```
提交 PR
  └─► GitHub Actions 触发
        └─► python main.py --pr-url <PR_URL> --post-comment
              ├─ 获取 PR diff（GitHub API）
              ├─ Supervisor iter=0 → ContextEnrichment
              │     ├─ Layer 1：读取 README + 目录结构，LLM 生成项目画像（结果缓存入 PG）
              │     ├─ Layer 2：识别 diff 改动目录，抓取同目录兄弟文件（纯启发式）
              │     └─ Layer 3：查询历史 findings（pgvector 向量检索）
              ├─ Research Agent（可选：搜索 CVE / 技术文档）
              ├─ Review Pipeline
              │     ├─ Diff Analyzer → Coordinator
              │     ├─ Security Reviewer（semgrep + secrets 扫描）
              │     └─ Quality Reviewer（ruff + AST 分析）
              └─ Report Generator → 回写 PR Comment
```

> **关键约束**：Layer 1 和 Layer 2 需要 `repo_url`，只有 `--pr-url` 模式才携带该值。
> 本地 `--diff-file` 模式下这两层静默跳过，Layer 3（历史记录）不受影响。

---

## 二、你需要准备什么

### 2.1 LLM 服务（必填，选其一）

| 提供商 | 环境变量 |
|--------|---------|
| DeepSeek（推荐） | `DEEPSEEK_API_KEY` |
| ZhiPu | `ZHIPU_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |

### 2.2 长期记忆 / 项目画像缓存（强烈推荐）

需要一个 PostgreSQL 实例（推荐 [Supabase](https://supabase.com) 免费层）。

提供变量：`PG_DATABASE_URL`（格式：`postgresql://user:pass@host:5432/dbname`）

**首次使用前**，在 Supabase SQL Editor 执行以下建表语句：

```sql
-- 开启 pgvector 扩展（已存在则跳过）
CREATE EXTENSION IF NOT EXISTS vector;

-- 长期记忆：存储历次审查的 findings
CREATE TABLE IF NOT EXISTS review_findings (
    id          BIGSERIAL PRIMARY KEY,
    repo_name   TEXT NOT NULL,
    finding     TEXT NOT NULL,
    embedding   vector(2048),
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_review_findings_repo ON review_findings(repo_name);

-- 项目画像缓存（Layer 1）
CREATE TABLE IF NOT EXISTS project_profiles (
    repo_name       TEXT PRIMARY KEY,
    tech_stack      TEXT,
    project_type    TEXT,
    security_level  TEXT DEFAULT 'medium',
    frameworks      TEXT,
    conventions     TEXT,
    summary         TEXT,
    readme_sha      TEXT,
    raw_profile     JSONB,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

不配置 `PG_DATABASE_URL` 时，系统自动降级到内存模式（MemorySaver），功能正常但无持久化。

### 2.3 Embedding 服务（配合 PG 使用，推荐）

用于向量检索历史 findings，提供：`ZHIPU_API_KEY`（使用 ZhiPu `embedding-3` 模型）。

### 2.4 Tavily 搜索（可选）

Research Agent 调用 Tavily 搜索 CVE 和技术文档，提供：`TAVILY_API_KEY`。
不配置则跳过网络搜索，不影响代码审查主流程。

---

## 三、在目标项目中配置 GitHub Actions

### 步骤 1：复制 workflow 文件

将本系统的 workflow 文件复制到你的目标项目（以下称 **被审查项目**）：

```bash
# 在被审查项目根目录执行
mkdir -p .github/workflows
```

创建 `.github/workflows/ai-code-review.yml`，内容如下：

```yaml
name: AI Code Review

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read

    steps:
      - name: Checkout reviewer
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Cache pip dependencies
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('code-review-agent/requirements.txt') }}
          restore-keys: |
            ${{ runner.os }}-pip-

      - name: Install dependencies
        run: pip install -r code-review-agent/requirements.txt

      - name: Run AI Reviewer
        uses: nick-fields/retry@v3
        with:
          timeout_minutes: 20
          max_attempts: 2
          retry_wait_seconds: 30
          retry_on: error
          command: |
            cd code-review-agent && python main.py \
              --pr-url "${{ github.event.pull_request.html_url }}" \
              --post-comment
        env:
          # ── 必填：选择你的 LLM ──────────────────────────────────────
          LLM_PROVIDER: deepseek
          LLM_MODEL: deepseek-v4-flash
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}

          # ── 必填：GitHub（自动注入，无需手动配置）──────────────────
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

          # ── 可选：Embedding + 长期记忆 ──────────────────────────────
          ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
          PG_DATABASE_URL: ${{ secrets.PG_DATABASE_URL }}

          # ── 可选：Tavily 网络搜索 ────────────────────────────────────
          TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}
```

> 注意：该 workflow 假设审查系统代码在同一仓库的 `code-review-agent/` 目录下。
> 如果你是独立部署审查系统（另一个仓库），参见 [附录 A：跨仓库部署](#附录-a跨仓库部署)。

### 步骤 2：在 GitHub 配置 Secrets

进入 **被审查项目** 的 GitHub 页面：
`Settings → Secrets and variables → Actions → New repository secret`

| Secret 名 | 值 | 是否必填 |
|------------|---|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 必填（如用 DeepSeek） |
| `ZHIPU_API_KEY` | ZhiPu API Key | 推荐（用于 Embedding） |
| `PG_DATABASE_URL` | `postgresql://...` | 推荐（用于持久化记忆） |
| `TAVILY_API_KEY` | Tavily API Key | 可选 |

`GITHUB_TOKEN` 由 Actions 自动注入，**不需要手动配置**。

### 步骤 3：提交 workflow 文件

```bash
git add .github/workflows/ai-code-review.yml
git commit -m "ci: add AI code reviewer"
git push origin main
```

---

## 四、触发审查：提交一个 PR

任何推送到 PR 的代码都会自动触发审查。操作流程：

```bash
# 1. 新建功能分支
git checkout -b feature/your-feature

# 2. 修改代码（Layer 1 & 2 会分析这些文件所在目录的上下文）
vim src/your_module.py

# 3. 提交并推送
git add src/your_module.py
git commit -m "feat: your feature"
git push origin feature/your-feature

# 4. 在 GitHub 上开 PR（到 main/master）
# 或使用 gh CLI：
gh pr create --title "feat: your feature" --body "..."
```

PR 创建后，GitHub Actions 自动启动，约 2–4 分钟后在 PR 页面出现审查评论。

---

## 五、审查触发后发生了什么（三层上下文注入详解）

### Layer 1 — 项目画像（LLM + DB 缓存）

**触发条件**：`repo_url` 非空（即 `--pr-url` 模式）

系统读取被审查项目的 README 和目录结构，用 LLM 生成：
- `tech_stack`：主要语言和运行时
- `project_type`：web-api / cli / library 等
- `security_level`：high（涉及认证/支付/PII）/ medium / low
- `frameworks`：主要框架列表
- `conventions`：编码规范备注
- `summary`：一句话项目描述

**缓存机制**：结果存入 `project_profiles` 表，缓存键为 `(repo_name, readme_sha)`。
30 天内不会重复调用 LLM，除非 README 有变化。

**如何验证 Layer 1 生效**：
```
在 Actions 日志中查找：
[ContextEnrichment] Layer1 profile generated | repo=owner/repo
或
[ContextEnrichment] Layer1 cache hit | repo=owner/repo
```

### Layer 2 — 兄弟文件上下文（纯启发式，零 LLM）

**触发条件**：`repo_url` 非空且 diff 不为空

系统识别 PR 改动的目录，从同目录抓取最多 3 个相关文件（前 80 行），让 Reviewer 判断：
- 接口是否与同模块文件一致
- 是否影响了同目录的其他组件
- 命名约定是否与周边代码匹配

**选择算法**（优先级从高到低）：
1. +3 分：与改动文件在同一目录
2. +2 分：文件名含 `model/schema/base/core/util/service`
3. +1 分：与改动文件扩展名相同

支持的文件类型：`.py .js .ts .go .java .rb .rs`，测试文件自动排除。

**如何验证 Layer 2 生效**：
```
在 Actions 日志中查找：
[ContextEnrichment] Layer2 related_files=N | repo=owner/repo
```
N > 0 说明找到了兄弟文件。

### Layer 3 — 历史记录检索（pgvector）

**触发条件**：`repo_name` 已知（本地模式也支持）

从 `review_findings` 表用向量相似度检索最近 5 条历史发现，帮助识别重复出现的问题。
第一次审查时无历史记录，从第二次开始生效。

---

## 六、本地调试

### 6.1 测试不需要 repo_url 的功能（Layer 3）

```bash
cd code-review-agent
python main.py \
  --diff-file tests/fixtures/sample.diff \
  --repo your-org/your-repo
```

### 6.2 测试完整三层上下文（需要 GitHub Token）

```bash
cd code-review-agent
python main.py \
  --pr-url https://github.com/your-org/your-repo/pull/1 \
  --output review_report.md
```

### 6.3 测试并回写 PR Comment

```bash
cd code-review-agent
python main.py \
  --pr-url https://github.com/your-org/your-repo/pull/1 \
  --post-comment
```

### 6.4 验证 Layer 1 缓存已写入数据库

运行 PR 审查后，在 Supabase SQL Editor 查询：

```sql
SELECT repo_name, tech_stack, project_type, security_level, summary, updated_at
FROM project_profiles
ORDER BY updated_at DESC
LIMIT 5;
```

---

## 七、预期输出

审查完成后，PR 页面会出现类似以下的评论：

```
## AI Code Review Report

**项目背景**：Python FastAPI web-api，安全级别：high

### 安全发现（2 条）
- [Critical] SQL 注入风险：user_input 直接拼接到查询字符串（src/db.py:42）
- [High] 硬编码 Secret：SECRET_KEY 明文写在配置文件（config/settings.py:10）

### 质量发现（5 条）
- [Medium] 函数 process_data 圈复杂度过高（ruff: C901）
...
```

---

## 八、常见问题

**Q：PR Comment 没有出现？**
检查 Actions 日志（PR 页面 → Checks → AI Code Review）。常见原因：
- `GITHUB_TOKEN` 权限不足：确认 workflow 中 `permissions.pull-requests: write` 已设置
- LLM API Key 未配置或额度耗尽

**Q：Layer 1 每次都重新生成，没有命中缓存？**
- 检查 `PG_DATABASE_URL` 是否在 Secrets 中正确配置
- 检查 `project_profiles` 表是否已创建（见第 2.2 节建表语句）

**Q：Layer 2 总是 related_files=0？**
- 确认 PR 改动的文件类型在支持列表中（.py .js .ts .go .java .rb .rs）
- 确认改动目录中还有其他非测试代码文件

**Q：本地 --diff-file 模式下 Layer 1/2 是否可以测试？**
- 不能。这两层需要 `repo_url`，必须用 `--pr-url` 模式。
- 如需快速本地测试 Layer 1/2 而不走 Actions，可直接调用：
  ```bash
  python main.py --pr-url https://github.com/your-org/your-repo/pull/任意PR号
  ```

---

## 附录 A：跨仓库部署

如果审查系统代码在独立仓库（不和被审查项目放一起），使用以下方式：

```yaml
- name: Checkout reviewer system
  uses: actions/checkout@v4
  with:
    repository: your-org/MultiAgentCodeReviewer  # 审查系统仓库
    path: reviewer
    token: ${{ secrets.REVIEWER_REPO_TOKEN }}    # 访问私有仓库需要

- name: Install dependencies
  run: pip install -r reviewer/code-review-agent/requirements.txt

- name: Run AI Reviewer
  run: |
    cd reviewer/code-review-agent && python main.py \
      --pr-url "${{ github.event.pull_request.html_url }}" \
      --post-comment
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
    # ... 其他环境变量
```

---

## 附录 B：不同 LLM 提供商配置对照

| 提供商 | `LLM_PROVIDER` | 需要的 Secret |
|--------|---------------|--------------|
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` |
| ZhiPu | `zhipu` | `ZHIPU_API_KEY` |
| OpenAI | `openai` | `OPENAI_API_KEY` |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` |

---

*最后更新：2026-05-18*
