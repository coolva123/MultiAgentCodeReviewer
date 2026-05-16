# MultiAgent Code Reviewer

基于 LangGraph 多 Agent 架构的自动化代码审查系统，支持安全漏洞扫描、代码质量分析，可集成到 GitHub Actions CI/CD 流水线，在 PR 创建时自动触发并将审查报告作为评论回写到 PR 页面。

---

## 场景一：集成 GitHub Actions（推荐）

### 工作原理

```
开发者推送代码到新分支
        │
        ▼
在 GitHub 页面创建 Pull Request
        │  GitHub 检测到 PR 事件
        ▼
GitHub Actions 自动启动云端虚拟机
  1. 克隆本审查项目代码
  2. 安装 Python 依赖
  3. 调用 main.py 从 GitHub API 获取 PR diff
  4. 多 Agent 并行分析（安全审查 + 质量审查）
  5. 将 Markdown 审查报告回写到 PR 评论
        │
        ▼
开发者在 PR 页面查看审查报告（约 2-5 分钟）
虚拟机自动销毁，不产生持续费用
```

审查流程内部由五个 Agent 节点组成：

| Agent | 职责 |
|-------|------|
| DiffAnalyzer | 解析 diff 结构，识别 PR 性质和风险等级 |
| Coordinator | 决定是否需要安全审查、质量审查，以及重点关注哪些文件 |
| SecurityReviewer | SAST 扫描 + 密钥检测 + LLM 语义分析，识别安全漏洞 |
| QualityReviewer | AST 分析 + Lint + LLM 语义分析，发现代码质量问题 |
| ReportGenerator | 汇总所有 findings，生成结构化 Markdown 报告 |

---

### 快速接入

#### 第一步：在你的目标仓库创建 workflow 文件

在你想要审查的项目中，创建以下文件：

```
你的项目/
└── .github/
    └── workflows/
        └── ai-code-review.yml
```

文件内容（将 `your-username` 替换为你的 GitHub 用户名）：

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
      - name: Clone AI Reviewer
        run: git clone https://github.com/your-username/MultiAgentCodeReviewer.git reviewer

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Cache pip dependencies
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('reviewer/code-review-agent/requirements.txt') }}

      - name: Install dependencies
        run: pip install -r reviewer/code-review-agent/requirements.txt

      - name: Run AI Reviewer
        working-directory: reviewer/code-review-agent
        env:
          LLM_PROVIDER: deepseek
          LLM_MODEL: deepseek-v4-flash
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
          DEEPSEEK_BASE_URL: https://api.deepseek.com
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
          ZHIPU_BASE_URL: https://open.bigmodel.cn/api/paas/v4/
          ZHIPU_EMBED_MODEL: embedding-3
        run: |
          python main.py \
            --pr-url "${{ github.event.pull_request.html_url }}" \
            --post-comment
```

#### 第二步：在目标仓库配置 Secrets

进入目标仓库 GitHub 页面：**Settings → Secrets and variables → Actions → New repository secret**

| Secret 名称 | 说明 | 是否必填 |
|------------|------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key，用于 LLM 分析 | ✅ 必填 |
| `ZHIPU_API_KEY` | 智谱 AI API Key，用于语义向量化 | 可选（不填则无长期记忆） |

> `GITHUB_TOKEN` 由 GitHub 自动注入，**无需手动添加**。

#### 第三步：确认 MultiAgentCodeReviewer 是公开仓库

workflow 使用匿名 `git clone` 拉取本项目，需要仓库设为 **Public**：

**Settings → Danger Zone → Change repository visibility → Public**

#### 第四步：触发测试

在目标仓库推送一个新分支，然后在 GitHub 页面创建 Pull Request。约 2-5 分钟后：

- **Actions Tab**：可以看到 `AI Code Review` job 的实时运行日志
- **PR 评论区**：审查报告会作为一条评论出现

---

### 审查报告示例

报告包含以下部分：

```
# 🔍 Code Review Report
| 仓库 | owner/repo |
| 风险等级 | 🔴 CRITICAL |
| 总计发现 | 13 条（安全 1 / 质量 12）|

## 📋 执行摘要
...

## 🔴 CRITICAL Issues
### [S-1] Hardcoded database password
位置：application-dev.yml line 53
问题：数据库密码明文硬编码...
修复：改用环境变量 ${DB_PASSWORD}

## 🟠 HIGH Issues
...

## 🔧 工具调用记录
| semgrep_scan | HIGH | ✅ 已执行 |
```

---

### 功能说明

**自动降级机制**

| 组件 | 不可用时的行为 |
|------|--------------|
| Semgrep MCP | 自动切换到本地 semgrep subprocess |
| 长期记忆（pgvector） | 跳过历史上下文注入，不影响审查结果 |
| HITL 人工确认 | CI 环境检测到非交互模式，自动批准所有工具调用 |

**支持的语言**

安全扫描：Python、Java、JavaScript、TypeScript、Go、Ruby 等 30+ 语言

质量分析：所有语言（LLM 语义分析）；Python 额外支持 AST 指标和 Ruff lint

---

### 常见问题

**Q：每次 PR 都会触发审查吗？**

是的，workflow 配置了 `opened`（新建 PR）和 `synchronize`（PR 有新 push）两个事件触发。

**Q：审查费用是多少？**

GitHub Actions 免费账户每月有 2000 分钟额度，每次审查约 2-5 分钟。DeepSeek API 费用极低，千次 PR 审查约 1 美元。

**Q：如果 MultiAgentCodeReviewer 是私有仓库怎么办？**

把 workflow 中的 `git clone` 改为带 token 的方式：
```yaml
run: git clone https://${{ secrets.REVIEWER_PAT }}@github.com/your-username/MultiAgentCodeReviewer.git reviewer
```
并在目标仓库添加 `REVIEWER_PAT` Secret（值为有 repo 权限的 Personal Access Token）。

---

## 本地命令行使用

```bash
# 审查本地 diff 文件
python main.py --diff-file path/to/changes.diff --repo owner/repo

# 审查 GitHub PR（需配置 GITHUB_TOKEN）
python main.py --pr-url https://github.com/owner/repo/pull/123

# 审查并回写评论到 PR
python main.py --pr-url https://github.com/owner/repo/pull/123 --post-comment

# 审查结果输出到文件
python main.py --diff-file changes.diff --output report.md
```

---

## 环境配置

复制 `.env.example` 为 `.env` 并填入配置：

```env
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com

ZHIPU_API_KEY=xxxxxxxx
ZHIPU_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
ZHIPU_EMBED_MODEL=embedding-3

GITHUB_TOKEN=ghp_xxxxxxxx

# 可选：长期记忆
PG_DATABASE_URL=postgresql://user:pass@localhost:5432/dbname

# 可选：Semgrep MCP
SEMGREP_APP_TOKEN=xxxxxxxx
```
