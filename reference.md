---
  多Agent编码审查系统 — 技术架构文档

  ---
  一、系统整体架构
  
  ┌─────────────────────────────────────────────────────────────┐
  │                        Input Layer                          │
  │         GitHub PR Webhook  /  本地 diff 文件  /  CLI         │
  └─────────────────────────┬───────────────────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────────────────┐
  │                   LangGraph Agent Graph                      │
  │                                                             │
  │   ┌─────────────┐     ┌──────────────┐                     │
  │   │ Coordinator │────►│ Diff Analyzer│                     │
  │   │   Agent     │     │    Agent     │                     │
  │   └──────┬──────┘     └──────┬───────┘                     │
  │          │                   │ (parallel fan-out)           │
  │          │         ┌─────────┴──────────┐                  │
  │          │         ▼                    ▼                   │
  │          │  ┌─────────────┐   ┌──────────────────┐         │
  │          │  │  Security   │   │     Quality      │         │
  │          │  │  Reviewer   │   │    Reviewer      │         │
  │          │  │   Agent     │   │     Agent        │         │
  │          │  └──────┬──────┘   └────────┬─────────┘         │
  │          │         └─────────┬──────────┘                  │
  │          │                   ▼                              │
  │          │         ┌──────────────────┐                    │
  │          └────────►│  Report Generator│                    │
  │                    │     Agent        │                    │
  │                    └──────────────────┘                    │
  └─────────────────────────────────────────────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────────────────┐
  │                      Harness 层                              │
  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
  │  │  短期记忆     │  │   长期记忆    │  │   Tool Guard     │  │
  │  │ (Session     │  │ (ChromaDB    │  │ (风险分级 +      │  │
  │  │  State)      │  │  向量存储)    │  │  人工确认 HITL)  │  │
  │  └──────────────┘  └──────────────┘  └──────────────────┘  │
  │  ┌──────────────────────────────────────────────────────┐   │
  │  │          Checkpointing (LangGraph 内置)               │   │
  │  └──────────────────────────────────────────────────────┘   │
  └─────────────────────────────────────────────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────────────────┐
  │                      Tool 层                                 │
  │  file_tools │ code_analysis │ github_tools │ sandbox(低优先级)│
  └─────────────────────────────────────────────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────────────────┐
  │                     Output 层                                │
  │          Markdown 报告  /  GitHub PR Comment                 │
  └─────────────────────────────────────────────────────────────┘

  ---
  二、各Agent职责划分

  ┌───────────────────┬──────────────────────────────────────────┬──────────────────┬────────────────────┐
  │       Agent       │                   职责                   │       输入       │        输出        │
  ├───────────────────┼──────────────────────────────────────────┼──────────────────┼────────────────────┤
  │ Coordinator       │ 任务调度、路由决策、结果整合             │ 原始 PR 信息     │ 路由指令           │
  ├───────────────────┼──────────────────────────────────────────┼──────────────────┼────────────────────┤
  │ Diff Analyzer     │ 解析 diff、提取变更文件、识别变更类型    │ Raw diff         │ 结构化变更摘要     │
  ├───────────────────┼──────────────────────────────────────────┼──────────────────┼────────────────────┤
  │ Security Reviewer │ 检查安全漏洞（注入、硬编码密钥、权限等） │ 结构化变更       │ 安全问题列表       │
  ├───────────────────┼──────────────────────────────────────────┼──────────────────┼────────────────────┤
  │ Quality Reviewer  │ 检查代码质量（复杂度、最佳实践、性能）   │ 结构化变更       │ 质量问题列表       │
  ├───────────────────┼──────────────────────────────────────────┼──────────────────┼────────────────────┤
  │ Report Generator  │ 汇总所有发现，生成结构化报告             │ 各 Reviewer 输出 │ 最终 Markdown 报告 │
  └───────────────────┴──────────────────────────────────────────┴──────────────────┴────────────────────┘

  ---
  三、文件结构

  code-review-agent/
  │
  ├── src/
  │   ├── graph/                        # LangGraph 核心
  │   │   ├── state.py                  # State Schema (TypedDict)
  │   │   ├── graph.py                  # Graph 定义与节点注册
  │   │   └── edges.py                  # 条件路由逻辑
  │   │
  │   ├── agents/                       # 各 Agent 实现
  │   │   ├── coordinator.py
  │   │   ├── diff_analyzer.py
  │   │   ├── security_reviewer.py
  │   │   ├── quality_reviewer.py
  │   │   └── report_generator.py
  │   │
  │   ├── harness/                      # 基础设施层
  │   │   ├── memory/
  │   │   │   ├── short_term.py         # Session 级状态管理
  │   │   │   └── long_term.py          # ChromaDB 向量存储封装
  │   │   ├── tool_guard.py             # 工具执行保护 + HITL
  │   │   └── checkpointer.py           # LangGraph Checkpointer 配置
  │   │
  │   ├── tools/                        # 工具集
  │   │   ├── file_tools.py             # 文件读取、搜索
  │   │   ├── code_analysis.py          # AST解析、bandit/pylint封装
  │   │   ├── github_tools.py           # GitHub API 集成
  │   │   └── sandbox.py                # Docker 沙盒（低优先级）
  │   │
  │   ├── prompts/                      # 各 Agent 的 Prompt 模板
  │   │   ├── coordinator.py
  │   │   ├── security.py
  │   │   ├── quality.py
  │   │   └── report.py
  │   │
  │   └── output/
  │       ├── formatter.py              # 报告格式化
  │       └── github_commenter.py       # 回写 PR Comment
  │
  ├── config/
  │   ├── settings.py                   # 全局配置（模型、API Key等）
  │   └── tool_risk_config.yaml         # 工具风险等级定义
  │
  ├── tests/
  │   ├── fixtures/                     # 测试用的 diff 样本
  │   └── test_agents.py
  │
  ├── main.py                           # 入口
  ├── requirements.txt
  └── README.md

  ---
  四、优先级排序

  P0 ── 必须实现（核心骨架）
    ├── State Schema 设计
    ├── LangGraph Graph 搭建（节点 + 边）
    ├── Diff Analyzer Agent
    ├── Security Reviewer Agent
    └── Quality Reviewer Agent

  P1 ── 应当实现（体现 Harness 深度）
    ├── Tool Guard（风险分级 + HITL 人工确认）
    ├── 短期记忆（Session State 管理）
    └── Report Generator Agent

  P2 ── 建议实现（拉开差距的部分）
    ├── 长期记忆（ChromaDB，跨 session 记住历史问题模式）
    └── GitHub 集成（读 PR diff、回写 Comment）

  P3 ── 可选（时间充裕再做）
    |—— Fix Agent（需要先完成 P0-P2）
    ├── Sandbox（FixAgent的执行在Docker里面做 隔离执行静态分析工具）
    └── Web UI / 更完整的 CLI

  ---
  五、7天开发计划

  ---
  Day 1 — 地基：State + Graph 骨架
  
  目标：整个系统能跑起来，哪怕每个 Agent 只返回占位字符串

  - 搭项目结构，配置 requirements.txt（langgraph, langchain, chromadb, python-dotenv）
  - 设计 state.py：定义 ReviewState（diff内容、各 Agent 输出、最终报告等字段）
  - 在 graph.py 里注册所有节点、连接边，跑通一次完整图执行
  - 配置 MemorySaver checkpointer，验证状态持久化

  交付物：python main.py 能跑，日志显示每个节点被依次调用

  ---
  Day 2 — 核心 Agent：Diff Analyzer + Coordinator
  
  目标：系统能读懂一个真实的 diff 并结构化输出

  - 实现 diff_analyzer.py：解析 unified diff 格式，提取变更文件、增删行、变更类型（新增/修改/删除）
  - 实现 coordinator.py：基于 Diff Analyzer 的输出做路由决策（决定哪些 Reviewer 需要介入）
  - 实现条件边 edges.py：根据 Coordinator 决策 fan-out 到不同 Reviewer
  - 写几个测试 diff 文件放 tests/fixtures/

  交付物：给一个真实 diff，能看到 Coordinator 正确分派任务

  ---
  Day 3 — 审查 Agent：Security + Quality Reviewer

  目标：两个 Reviewer 能产出有意义的审查意见

  - 实现 security_reviewer.py：检查硬编码密钥、SQL拼接、危险函数调用、不安全的反序列化等
  - 实现 quality_reviewer.py：检查函数复杂度、命名规范、重复代码、缺少错误处理等
  - 实现 code_analysis.py 工具：封装 ast 模块做静态解析，可选接 bandit
  - 调整 Prompt 模板，输出结构化 JSON（问题类型、位置、严重级别、建议）

  交付物：输入一段有安全问题的代码 diff，能看到两个 Reviewer 各自输出问题列表

  ---
  Day 4 — Harness：Tool Guard + 短期记忆
  
  目标：开始体现"Harness"的价值，这是项目的技术亮点之一

  - 实现 tool_guard.py：
    - 定义工具风险等级（config/tool_risk_config.yaml）
    - 低风险（读文件）：直接执行
    - 中风险（写文件）：日志记录后执行
    - 高风险（shell命令）：暂停，打印确认提示，等待用户输入 y/n
  - 实现 short_term.py：在 State 里维护本次 session 的 tool_call_log 和 agent_scratchpad
  - 把所有工具调用统一走 Tool Guard 代理

  交付物：执行到高危工具时，终端出现确认提示，拒绝后 Agent 能感知并调整

  ---
  Day 5 — 长期记忆：ChromaDB 跨 Session 历史
  
  目标：系统能"记住"同一仓库以前审查发现过的问题模式

  - 实现 long_term.py：封装 ChromaDB，每次审查完成后把发现的问题摘要存入向量库（按仓库名+文件路径索引）
  - 在 Security Reviewer 和 Quality Reviewer 启动前，先查询长期记忆："这个文件/模块历史上有哪些高频问题？"，注入 Prompt 作为额外上下文
  - 实现记忆更新逻辑：审查完成后自动写入，支持去重

  交付物：对同一个仓库审查两次，第二次的 Prompt 里能看到历史问题上下文

  ---
  Day 6 — Report Generator + GitHub 集成
  
  目标：系统产出一份专业报告，并能对接真实 GitHub PR

  - 实现 report_generator.py：汇总 Security + Quality 输出，生成 Markdown 报告（按严重级别分组、有摘要、有逐条建议）
  - 实现 formatter.py：报告格式美化
  - 实现 github_tools.py：用 PyGitHub 库读取 PR diff（替代本地文件输入）
  - 实现 github_commenter.py：把报告作为 PR Comment 回写（可选，需要 GitHub Token）
  - 完善 main.py CLI：支持 --diff-file 和 --pr-url 两种输入模式

  交付物：python main.py --pr-url https://github.com/xxx/yyy/pull/1 能跑完整流程

  ---
  Day 7 — 收尾 + 可选 Sandbox
  
  目标：项目完整可演示，选择性实现沙盒

  - 补全 README（架构图、使用方式、技术亮点说明）
  - 错误处理：Agent 失败时的重试逻辑、超时处理
  - 优化 Prompt，跑几个真实 PR 验证效果
  - （可选）Sandbox：用 Docker SDK 起一个容器，在容器内执行 bandit/pylint，隔离分析环境，容器用完即销毁

  交付物：项目可以作为 Portfolio 展示，README 里有架构图和示例输出

  ---
  六、关键技术选型
  
  ┌─────────────┬─────────────────────────────┬────────────────────────────┐
  │    组件     │          技术选择           │            理由            │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ Agent 编排  │ LangGraph                   │ 符合目标，图结构清晰       │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ LLM         │ Claude Sonnet / GPT-4o      │ 代码理解能力强             │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ 短期记忆    │ LangGraph StateGraph 内置   │ 零额外依赖                 │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ 长期记忆    │ ChromaDB                    │ 轻量，本地运行，无需云服务 │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ 静态分析    │ bandit（安全）+ ast（结构） │ Python 原生，轻量          │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ GitHub 集成 │ PyGitHub                    │ 官方封装，简单             │
  ├─────────────┼─────────────────────────────┼────────────────────────────┤
  │ 沙盒        │ Docker SDK（P3）            │ 按需引入                   │
  └─────────────┴─────────────────────────────┴────────────────────────────┘

  ---
  七、工具层扩展 TODO（Day 7 完成后执行）

  背景：Day 3 实现的工具层（bandit + regex + ast）仅覆盖 Python 和简单正则匹配，
  不支持 Java/Go/JS/TS 等语言，且质量分析深度有限。
  下列 TODO 在完成 Day 4-7 主线开发后，作为独立扩展任务逐一接入。

  ─────────────────────────────────────────────────────────────────────────────
  TODO-SEC-01：接入 Semgrep MCP Server（替换/增强 SecurityReviewer 工具层）
  ─────────────────────────────────────────────────────────────────────────────

  问题：当前 SecurityReviewer 使用 bandit（Python 专属）+ 两个 regex 工具，
        无法审查 Java、Go、JavaScript、TypeScript 等语言的安全漏洞。

  目标：接入 Semgrep 官方 MCP Server，覆盖 30+ 语言，使用数千条社区安全规则。

  Semgrep MCP Server 信息：
    官方仓库：https://github.com/semgrep/mcp
    官方文档：https://semgrep.dev/docs/mcp
    协议：Model Context Protocol（MCP）
    提供的核心工具：semgrep_scan（扫描代码/文件/目录，返回 JSON findings）

  实现步骤：

  Step 1 — 安装 Semgrep
    pip install semgrep
    # 加入 requirements.txt：semgrep>=1.50.0

  Step 2 — 注册 Semgrep MCP Server（两种方式二选一）

    方式 A：subprocess 直接调用（不走 MCP 协议，最简单）
      在 src/tools/code_analysis.py 新增：

      @tool
      def semgrep_scan(source_code: str, filename: str, language: str = "auto") -> str:
          """
          Run Semgrep security scan on source code. Supports Python, Java, Go,
          JavaScript, TypeScript, Ruby, C/C++, Kotlin and 25+ more languages.
          Uses --config=auto to apply community security ruleset.
          """
          suffix_map = {
              "python": ".py", "java": ".java", "go": ".go",
              "javascript": ".js", "typescript": ".ts",
          }
          suffix = suffix_map.get(language, Path(filename).suffix or ".txt")
          with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as tmp:
              tmp.write(source_code)
              tmp_path = tmp.name
          proc = subprocess.run(
              ["semgrep", "--config=auto", "--json", "--quiet", tmp_path],
              capture_output=True, text=True, timeout=60,
          )
          Path(tmp_path).unlink(missing_ok=True)
          data = json.loads(proc.stdout or "{}")
          findings = [
              {
                  "line":     r.get("start", {}).get("line"),
                  "severity": r.get("extra", {}).get("severity", "").lower(),
                  "rule_id":  r.get("check_id"),
                  "title":    r.get("extra", {}).get("message", ""),
                  "fix":      r.get("extra", {}).get("fix"),
              }
              for r in data.get("results", [])
          ]
          return json.dumps({"findings": findings, "total": len(findings)})

    方式 B：通过 MCP 协议接入（更规范，符合 open-swe 架构）
      参考 LangChain MCP adapter：
        pip install langchain-mcp-adapters
      在 config/mcp_servers.py 配置 Semgrep MCP Server 的 stdio transport，
      用 MultiServerMCPClient 加载工具，替换 _SECURITY_TOOLS 列表。
      适合 Day 7 Sandbox 完成后统一接入。

  Step 3 — 替换 SecurityReviewer 工具列表
    在 src/agents/security_reviewer.py：
      # 旧：
      _SECURITY_TOOLS = [bandit_scan, scan_secrets, scan_sql_injection]
      # 新（保留 scan_secrets 用于凭据检测，Semgrep 替换 bandit）：
      _SECURITY_TOOLS = [semgrep_scan, scan_secrets]

  Step 4 — 更新 System Prompt
    在 src/prompts/security.py 的 SYSTEM 里加入：
      - `semgrep_scan`: Multi-language SAST scanner (30+ languages). Use this
        as the primary scanner for ALL file types.
      - `scan_secrets`: Credential/key detector for any file type.

  预期效果：
    - Java 文件里的 SQL 注入、XXE、反序列化漏洞 → Semgrep 检测
    - Go 的 os/exec 注入 → Semgrep 检测
    - JS/TS 的 XSS、prototype pollution → Semgrep 检测
    - Python 的安全问题 → Semgrep（比 bandit 规则更多）+ scan_secrets


  ─────────────────────────────────────────────────────────────────────────────
  TODO-QUAL-01：接入 SonarQube MCP Server（增强 QualityReviewer 工具层）
  ─────────────────────────────────────────────────────────────────────────────

  问题：当前 QualityReviewer 只有 ast_analyze 一个工具，
        只能做 Python 结构度量（行数、复杂度、嵌套深度），
        发现不了命名问题、重复代码、缺少错误处理等语义质量问题。

  目标：接入工业级代码质量平台，获得多维度质量指标和跨语言支持。

  ── 方案 A：Ruff MCP Server（Python 专项，优先接入，成本最低）──────────────

  Ruff 信息：
    官方仓库：https://github.com/astral-sh/ruff
    MCP Server：https://lobehub.com/mcp/drewsonne-ruff-mcp-server
    速度：比 pylint 快 1000 倍（Rust 实现），覆盖 500+ lint 规则

  实现步骤：

  Step 1 — 安装
    pip install ruff
    # requirements.txt：ruff>=0.4.0

  Step 2 — 新增工具（src/tools/code_analysis.py）
    @tool
    def ruff_check(source_code: str, filename: str) -> str:
        """
        Run Ruff linter on Python source code. Covers 500+ rules including
        naming conventions, unused imports, error handling, type annotations.
        Only effective for .py files.
        """
        if not filename.endswith(".py"):
            return json.dumps({"issues": [], "note": "ruff only supports Python"})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(source_code)
            tmp_path = tmp.name
        proc = subprocess.run(
            ["ruff", "check", "--output-format=json", tmp_path],
            capture_output=True, text=True, timeout=30,
        )
        Path(tmp_path).unlink(missing_ok=True)
        data = json.loads(proc.stdout or "[]")
        issues = [
            {
                "line":    item.get("location", {}).get("row"),
                "rule":    item.get("code"),
                "title":   item.get("message"),
                "fix":     item.get("fix", {}).get("message") if item.get("fix") else None,
            }
            for item in data
        ]
        return json.dumps({"issues": issues, "total": len(issues)})

  Step 3 — 加入 QualityReviewer 工具列表
    # src/agents/quality_reviewer.py
    from src.tools.code_analysis import ast_analyze, ruff_check
    _QUALITY_TOOLS = [ast_analyze, ruff_check]

  ── 方案 B：SonarQube MCP Server（多语言，生产级，成本较高）─────────────────

  SonarQube MCP Server 信息：
    官方仓库：https://github.com/SonarSource/sonarqube-mcp-server
    官方文档：https://docs.sonarsource.com/sonarqube-mcp-server
    支持：30+ 语言，代码异味、重复代码、圈复杂度、测试覆盖率、技术债

  前提条件：
    需要一个运行中的 SonarQube 实例（本地 Docker 或 SonarCloud 免费账户）

  本地启动 SonarQube：
    docker run -d --name sonarqube -p 9000:9000 sonarqube:community
    # 访问 http://localhost:9000，默认账号 admin/admin

  环境变量（加入 .env）：
    SONAR_HOST_URL=http://localhost:9000
    SONAR_TOKEN=sqp_xxxxxxxxxxxx   # 在 SonarQube 界面生成

  MCP 接入方式：
    pip install langchain-mcp-adapters
    # 在 config/mcp_servers.py 配置 SonarQube MCP Server
    # 用 MultiServerMCPClient 获取工具列表，注入到 QualityReviewer

  SonarQube MCP 提供的核心工具：
    - get_issues：获取项目质量问题列表（按类型/严重级别过滤）
    - get_metrics：获取代码度量指标（复杂度、重复率、覆盖率）
    - get_quality_gate_status：检查质量门禁是否通过

  适用场景：
    团队已有 SonarQube 基础设施，或项目需要跨语言质量审查时优先选择。

  ── 方案对比 ──────────────────────────────────────────────────────────────────

  ┌───────────────┬──────────────────┬──────────────────────┬──────────────────┐
  │    方案       │   接入成本       │    覆盖语言          │   推荐场景       │
  ├───────────────┼──────────────────┼──────────────────────┼──────────────────┤
  │ Ruff MCP      │ 低（pip install）│ Python 专属          │ 快速增强 Python  │
  ├───────────────┼──────────────────┼──────────────────────┼──────────────────┤
  │ SonarQube MCP │ 高（需要实例）   │ 30+ 语言             │ 生产级多语言项目 │
  ├───────────────┼──────────────────┼──────────────────────┼──────────────────┤
  │ 两者组合      │ 中               │ 30+ 语言             │ 最完整覆盖       │
  └───────────────┴──────────────────┴──────────────────────┴──────────────────┘

  推荐执行顺序：先做 TODO-SEC-01 方式A（Semgrep subprocess），
  再做 TODO-QUAL-01 方案A（Ruff），两个都是 pip install 级别成本，
  不依赖外部服务，可在 Day 7 Sandbox 完成后用半天时间接入。
  SonarQube 作为可选的进阶扩展。


  ---
  八、触发与集成 TODO（主线开发完成后执行）

  背景：当前系统只能通过 CLI 手动触发（python main.py --diff-file / --pr-url）。
  以下两个 TODO 分别实现"自动触发"和"Web 化"两个方向，互相独立，可按需选择。

  ─────────────────────────────────────────────────────────────────────────────
  TODO-TRIGGER-01：GitHub Actions 自动触发（零部署，推荐优先实现）
  ─────────────────────────────────────────────────────────────────────────────

  目标：在被审查的目标仓库里加一个 workflow 文件，每次有人开 PR 或推新代码时，
        GitHub 自动启动云端虚拟机运行审查，把报告贴到 PR Comment，无需任何服务器。

  工作原理：
    - GitHub Actions 是 GitHub 内置的 CI/CD 云服务
    - on: pull_request 事件触发时，GitHub 启动一台临时 Ubuntu 虚拟机
    - 虚拟机克隆本项目代码，装依赖，跑 main.py，跑完自动销毁
    - GITHUB_TOKEN 由 GitHub 自动生成注入，有权限读 PR diff 和写 Comment
    - 免费账户每月 2000 分钟额度，审查一次约 2-3 分钟

  实现步骤：

  Step 1 — 在目标仓库（被审查的项目）创建 workflow 文件
    路径：.github/workflows/ai-code-review.yml

    内容：
    ────────────────────────────────────────────────────
    name: AI Code Review

    on:
      pull_request:
        types: [opened, synchronize]   # PR 创建或有新 push 时触发

    jobs:
      review:
        runs-on: ubuntu-latest
        steps:
          - name: Checkout reviewer tool
            uses: actions/checkout@v4
            with:
              repository: your-github-username/MultiAgentCodeReviewer
              path: reviewer

          - name: Set up Python
            uses: actions/setup-python@v5
            with:
              python-version: "3.11"

          - name: Install dependencies
            run: pip install -r reviewer/code-review-agent/requirements.txt

          - name: Run AI Reviewer
            working-directory: reviewer/code-review-agent
            env:
              DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
              ZHIPU_API_KEY:    ${{ secrets.ZHIPU_API_KEY }}
              GITHUB_TOKEN:     ${{ secrets.GITHUB_TOKEN }}
              PG_DATABASE_URL:  ${{ secrets.PG_DATABASE_URL }}   # 可选，长期记忆
              LLM_PROVIDER:     deepseek
              LLM_MODEL:        deepseek-v4-flash
            run: |
              python main.py \
                --pr-url "${{ github.event.pull_request.html_url }}" \
                --post-comment
    ────────────────────────────────────────────────────

  Step 2 — 在目标仓库配置 Secrets
    GitHub 仓库 → Settings → Secrets and variables → Actions → New repository secret
    需要添加：
      DEEPSEEK_API_KEY  = sk-xxxxxx
      ZHIPU_API_KEY     = xxxxxx         （embedding 用，长期记忆可选）
      PG_DATABASE_URL   = postgresql://...  （长期记忆可选，不用可省略）
    不需要添加：
      GITHUB_TOKEN      （GitHub 自动注入，有读 PR / 写 Comment 权限）

  Step 3 — 验证
    对目标仓库开一个测试 PR，查看 Actions Tab 是否触发，
    PR Comment 是否出现审查报告。

  注意事项：
    - 如果 MultiAgentCodeReviewer 是私有仓库，Step 1 的 checkout 需要配置
      Personal Access Token 才能跨仓库拉取
    - 长期记忆（pgvector）在 Actions 环境下需要外部可访问的 PostgreSQL
      （如 Supabase / Railway / 本地 pgvector 暂时不可用于 CI）
      可在 .env 里不配置 PG_DATABASE_URL，长期记忆功能会自动降级跳过


  ─────────────────────────────────────────────────────────────────────────────
  TODO-TRIGGER-02：FastAPI Web 服务 + 前端 UI（需要服务器部署）
  ─────────────────────────────────────────────────────────────────────────────

  目标：把命令行工具包装成 HTTP API + Web UI，覆盖不熟悉 git 的非程序员用户。
        在原有 PR URL / diff 粘贴两种模式基础上，新增「直接上传代码文件」模式，
        彻底脱离 git 依赖，让任何人都能对单个或多个文件发起代码审查。

  背景分析：
    当前系统仅支持 git 操作者（需要 PR 流程）。扩展使用场景需要：
      - 非程序员/学生：只有代码文件，不懂 git
      - 代码片段快速审查：不想走完整 PR 流程
      - 企业内网场景：不依赖 GitHub，直接上传文件

    核心突破：semgrep_scan / ruff_check / ast_analyze 工具均接受
    source_code: str 参数，不依赖 diff，可直接对任意文件内容进行扫描。
    文件上传模式跳过 DiffAnalyzer 和 Coordinator，直接进入工具扫描 + LLM 分析。

  ─────────────────────────────────────────────────────────────────────────────
  三种输入模式对比
  ─────────────────────────────────────────────────────────────────────────────

  模式 A — GitHub PR URL（面向有 GitHub PR 流程的开发者）
    输入：{ "pr_url": "https://github.com/owner/repo/pull/123" }
    流程：完整 5 节点 Graph（DiffAnalyzer → Coordinator → Security + Quality → Report）
    优点：结合 PR 上下文，报告最完整

  模式 B — 粘贴 diff 文本（面向有 git 但不用 GitHub 的开发者）
    输入：{ "diff_content": "diff --git a/...", "repo_name": "my-project" }
    流程：完整 5 节点 Graph（与模式 A 相同，只是 diff 来源不同）
    优点：支持 GitLab / Gitea / 本地 git diff 输出

  模式 C — 直接上传代码文件（面向非 git 用户）【新增】
    输入：multipart/form-data，上传 1 个或多个源码文件
    流程：跳过 DiffAnalyzer + Coordinator，直接：
            文件内容 → SecurityReviewer（semgrep_scan + scan_secrets）
                     → QualityReviewer（ast_analyze + ruff_check + semgrep_scan）
                     → ReportGenerator
    实现要点：
      - 把每个上传文件的内容读取为字符串，伪造成 diff_files 格式：
          diff_files = [
            {
              "filename": "auth.py",
              "patch": file_content,       # 全部内容视为"新增"
              "change_type": "added",
              "change_category": "upload",
              "is_complex_logic": False,
            }
          ]
      - routing_decision 写死：{ "run_security": True, "run_quality": True,
                                 "priority": "high", "focus_files": [所有文件名] }
      - 直接从 security_reviewer_node / quality_reviewer_node 开始执行，
        跳过 diff_analyzer_node 和 coordinator_node

  ─────────────────────────────────────────────────────────────────────────────
  后端实现
  ─────────────────────────────────────────────────────────────────────────────

  新建文件：code-review-agent/server.py（与 main.py 同级）

  API 设计：
    POST /api/review            → 提交审查任务（模式 A / B），返回 session_id
    POST /api/review/upload     → 提交文件上传审查（模式 C），返回 session_id
    GET  /api/review/{id}       → 轮询任务状态和结果
    GET  /api/review/{id}/stream → SSE 实时推送日志（可选增强）
    GET  /api/health            → 健康检查

  请求/响应结构：
    POST /api/review 请求体：
      {
        "mode": "pr_url" | "diff_text",
        "pr_url": "https://github.com/...",       // mode=pr_url 时必填
        "diff_content": "diff --git ...",          // mode=diff_text 时必填
        "repo_name": "my-org/my-repo"             // mode=diff_text 时必填
      }

    POST /api/review/upload 请求体：
      multipart/form-data
        files: File[]     → 上传的源码文件（支持多文件）
        repo_name: str    → 项目名称（可选，默认"uploaded-project"）

    GET /api/review/{id} 响应：
      {
        "session_id": "uuid",
        "status": "queued | running | done | error",
        "progress": "正在分析安全漏洞...",      // 当前阶段文字
        "report": "# Code Review Report...",   // status=done 时返回
        "error": "..."                          // status=error 时返回
      }

  任务存储（内存 dict，单进程够用）：
    _tasks: dict[str, dict] = {}
    key = session_id, value = { status, report, progress, created_at }

  注意：
    - 审查耗时 1-3 分钟，POST 立即返回 session_id，前端轮询 GET 获取结果
    - 并发审查任务用 asyncio.to_thread() 在线程池中运行同步的 review_graph.invoke()
    - 模式 C 需要新建 code-review-agent/src/graph/file_review_graph.py，
      构建跳过前两个节点的简化 Graph：
        START → security_reviewer → quality_reviewer → report_generator → END

  ─────────────────────────────────────────────────────────────────────────────
  前端实现
  ─────────────────────────────────────────────────────────────────────────────

  新建文件：code-review-agent/static/index.html（单页 HTML，无需构建工具）

  界面布局（三个 Tab）：

    Tab 1：PR URL 模式
      ┌─────────────────────────────────────────────────┐
      │ 🔍 AI Code Reviewer                              │
      ├─────────────────────────────────────────────────┤
      │ [PR URL] [粘贴 Diff] [上传文件]                  │
      ├─────────────────────────────────────────────────┤
      │ GitHub PR URL:                                   │
      │ [ https://github.com/owner/repo/pull/123      ] │
      │                                                  │
      │                  [ 开始审查 ]                     │
      └─────────────────────────────────────────────────┘

    Tab 2：粘贴 Diff 模式
      ┌─────────────────────────────────────────────────┐
      │ [PR URL] [粘贴 Diff] [上传文件]                  │
      ├─────────────────────────────────────────────────┤
      │ 项目名称：[ my-org/my-repo                     ] │
      │ Diff 内容（粘贴 git diff 输出）：                │
      │ ┌─────────────────────────────────────────────┐  │
      │ │ diff --git a/app.py b/app.py                │  │
      │ └─────────────────────────────────────────────┘  │
      │                  [ 开始审查 ]                     │
      └─────────────────────────────────────────────────┘

    Tab 3：上传文件模式（新增，面向非 git 用户）
      ┌─────────────────────────────────────────────────┐
      │ [PR URL] [粘贴 Diff] [上传文件]                  │
      ├─────────────────────────────────────────────────┤
      │ 项目名称（可选）：[ my-project                 ] │
      │                                                  │
      │ ┌─────────────────────────────────────────────┐  │
      │ │  📁 拖拽文件到此处，或点击选择               │  │
      │ │                                              │  │
      │ │  支持：.py .java .js .ts .go .rb .php 等    │  │
      │ └─────────────────────────────────────────────┘  │
      │  已选择：auth.py  utils.py  controller.java      │
      │                                                  │
      │                  [ 开始审查 ]                     │
      └─────────────────────────────────────────────────┘

    审查进行中（所有模式共用）：
      ┌─────────────────────────────────────────────────┐
      │  ⏳ 正在审查...（通常需要 1-3 分钟）             │
      │                                                  │
      │  ✅ 获取代码内容                                  │
      │  ✅ 解析文件结构                                  │
      │  🔄 安全漏洞扫描中...                             │
      │  ⬜ 代码质量分析                                  │
      │  ⬜ 生成报告                                      │
      └─────────────────────────────────────────────────┘

    审查结果：
      - 用 marked.js 渲染 Markdown 报告
      - 提供「下载 .md」和「复制到剪贴板」按钮
      - 显示工具调用记录（可折叠）

  前端技术：纯 HTML + Vanilla JS + marked.js（CDN 引入，无需构建工具）
  FastAPI 用 StaticFiles 挂载 static/ 目录提供前端文件

  ─────────────────────────────────────────────────────────────────────────────
  新增文件清单
  ─────────────────────────────────────────────────────────────────────────────

  code-review-agent/
  ├── server.py                           ← FastAPI 入口，三种模式 API
  ├── static/
  │   └── index.html                      ← 单页前端（三 Tab 布局）
  └── src/
      └── graph/
          └── file_review_graph.py        ← 模式 C 专用简化 Graph

  ─────────────────────────────────────────────────────────────────────────────
  依赖新增（requirements.txt）
  ─────────────────────────────────────────────────────────────────────────────

  fastapi>=0.110.0
  uvicorn[standard]>=0.29.0
  python-multipart>=0.0.9   ← 文件上传支持

  ─────────────────────────────────────────────────────────────────────────────
  启动与部署
  ─────────────────────────────────────────────────────────────────────────────

  本地启动：
    cd code-review-agent
    uvicorn server:app --host 0.0.0.0 --port 8080 --reload

  访问地址：http://localhost:8080

  云端部署（可选）：
    Railway / Render / Fly.io 均支持一键部署 FastAPI 应用
    长期记忆的 pgvector 改用云端 PostgreSQL（如 Supabase 免费套餐）

  ─────────────────────────────────────────────────────────────────────────────
  三种触发方式最终对比
  ─────────────────────────────────────────────────────────────────────────────

  ┌─────────────────┬──────────────────────┬──────────────────────────┐
  │                 │ GitHub Actions       │ FastAPI Web 服务         │
  ├─────────────────┼──────────────────────┼──────────────────────────┤
  │ 部署成本        │ 零（GitHub 托管）    │ 需要服务器或云平台       │
  │ 触发方式        │ PR 事件自动触发      │ 用户主动访问页面         │
  │ 用户群体        │ 懂 git 的开发者      │ 所有人（含不懂 git）     │
  │ 输入方式        │ PR URL 自动获取      │ PR URL / diff / 上传文件 │
  │ 适合场景        │ 已有 GitHub PR 流程  │ 对外开放的审查平台       │
  │ 实现难度        │ 低（只加 yml 文件）  │ 中（后端 + 前端）        │
  └─────────────────┴──────────────────────┴──────────────────────────┘

  推荐顺序：TODO-TRIGGER-01 已完成；TODO-TRIGGER-02 在需要对外开放时实现。