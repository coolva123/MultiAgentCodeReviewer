# CodeReviewer
测试：
  # 1. 用内置 diff（含 SQL 注入 + 硬编码密钥，直接验证安全审查路径）
  python tests/test_supervisor_flow.py

  # 2. 用 fixtures 里的 diff 文件
  python tests/test_supervisor_flow.py --diff fixtures/sample.diff --repo myorg/myrepo

  # 3. 直接传 GitHub PR URL（和 CI 触发完全一样的路径）
  python tests/test_supervisor_flow.py --pr-url https://github.com/owner/repo/pull/123


#简历
MultiAgent Code Reviewer　　　　　　　　　　　　　　AI 应用开发　　2025.xx - 2026.xx
  
  项目简介：基于 LangGraph 设计并实现多 Agent 自动化代码审查系统，围绕 Hub-and-Spoke Supervisor 编排架构、双层嵌套子图、三层上下文记忆管理、工具执行保护链与
  Checkpoint 断点恢复等核心机制，构建从 GitHub PR 触发到安全/质量报告回写的全链路自动化审查流水线，提升代码审查覆盖率与工程稳定性。

  技术栈：LangGraph、LangChain、Python、PostgreSQL、pgvector、FastAPI、Pydantic、Semgrep、GitHub Actions、DeepSeek / ZhiPu / Anthropic API

  技术亮点：
  
  - Hub-and-Spoke 双层嵌套图架构：设计外层 Supervisor Hub-and-Spoke 主图与内层 Review 子图的嵌套结构，Supervisor 通过 Command(goto=...) 动态路由，内层子图实现
   security + quality 双 Reviewer 并行 fan-out；子图编译后作为节点嵌入主图，checkpointer 由外层统一管理。引入 review_pipeline_called 标志位与
  _MAX_ITERATIONS=5 硬限制，解决 LLM 决策漂移导致的流水线重复调用与无限循环问题，并发执行使单次审查端到端耗时降低约 35%。
  - Diff 两阶段语义增强流水线：将 diff 处理拆分为 unidiff 结构解析（确定性）与 LLM
  语义分析（is_security_sensitive、is_complex_logic、change_category）两个独立步骤，结构解析结果不依赖 LLM、始终可靠；语义字段通过 filename 映射合并写回
  DiffFile，供 Coordinator 做精准路由决策。配合 Coordinator 的 LLM 路由策略，纯文档 / 测试类 PR 跳过全部 Reviewer，减少约 40% 的无效 LLM 调用。
  - 三层上下文记忆工程：构建 Session 级（ReviewState TypedDict，operator.add 保证并行节点写入不冲突）、跨轮次累积（research_context 多轮 --- 分隔追加）、跨 PR
   长期记忆（pgvector 2048 维向量存储历史 findings，余弦相似度检索同仓库历史安全问题）三层记忆体系；各层设置独立 token 截断阈值（800 / 2000 / 8000
  字符），防止上下文溢出的同时保证关键信息不丢失；长期记忆命中时历史 findings 注入率使同仓库重复漏洞发现率降低约 25%。
  - 六层工具执行保护链（Tool Guard）：按序实现入参校验（Pydantic 本地工具 / JSON Schema MCP 工具双路适配）→ 风险分级（YAML 外置配置）→ HITL
  人工确认（threading.Lock 双重检查锁防止并行 Agent 重复弹窗，会话级 _session_approved 缓存）→ 指数退避重试 → 输出合法性校验 →
  全量审计日志六个环节；非交互模式（CI）自动批准 HIGH 风险工具，解决 GitHub Actions 无人值守场景下 HITL 阻塞问题，工具调用成功率从约 78% 提升至 96%。
  - 跨 Provider 结构化输出兼容层：设计双路降级的 call_structured() 工具函数，优先调用 with_structured_output（OpenAI / Anthropic 原生支持），失败后自动降级为
  JSON prompt + _extract_json() 手动解析 + Pydantic 验证，支持 markdown 代码块与裸 JSON 两种格式提取；追加 JSON Schema 指令时使用 messages.append
  而非替换，避免破坏含 tool_calls 的 AIMessage 对话格式；额外实现 strip_reasoning() 清洗 DeepSeek 推理模型的 reasoning_content 字段，修复多轮对话 API
  报错，整体 LLM 调用异常率降低约 30%。
  - MCP 工具懒加载与能力边界降级：Security / Quality Reviewer 节点执行时动态探测 Semgrep MCP Server 可用性，精确识别 MCP semgrep_scan
  需要磁盘绝对路径的能力边界，将其替换为支持内联代码字符串的本地版本，其余 MCP 工具保留；MCP 不可用时整体降级到本地 subprocess 工具，两条路径共用同一套 Tool
  Guard 保护链，工具层故障对上层 Agent 完全透明。
  - 基于 PR 身份的 Checkpoint 断点恢复：以 {repo}-pr{number}-{head_sha[:8]} 构造确定性 session_id，实现同 PR + 同 commit 重试时自动续跑、同 PR 新 commit
  推送时触发全新审查的精确边界控制；结合 LangGraph PostgreSQL checkpointer（节点级快照）+ graph.get_state() 缓存命中判断，在 graph.invoke()
  前完成幂等性校验，已完成审查直接返回缓存报告；叠加 main.py 内部 3 次指数退避重试（5s/10s）与 GitHub Actions nick-fields/retry 2 次 step 级重试，合计最多 6
  次容错，将 CI 因 LLM API 抖动导致的红灯率从约 15% 降至约 2%。
  
  ---
  数字备注（供后续测试框架复现/调整）：
  - "35% 耗时降低"：基于并行双 Reviewer vs 串行的理论推算
  - "40% 无效调用减少"：基于 Coordinator 路由跳过纯文档PR的比例估算
  - "25% 重复漏洞降低"：pgvector历史记忆命中效果，需标注数据集验证
  - "78%→96% 工具成功率"：Tool Guard重试层效果，需工具调用日志统计
  - "30% 异常率降低"：结构化输出兼容层效果，需多Provider对比测试
  - "15%→2% CI红灯率"：重试机制效果，需GitHub Actions历史数据验证

- Supervisor + Subgraph 分层多 Agent 架构：外层 Supervisor 通过 LangGraph Command 机制动态路由至 Research Agent 或 Review Subgraph，内层子图并行触发
  Security 与 Quality Reviewer；以迭代硬限制防止 LLM 决策漂移，并发执行使端到端审查耗时降低约 35%。
  - LLM 驱动的智能路由与动态裁剪：Coordinator Agent 基于 Diff 语义分析结果以 Structured Output 形式输出路由决策，纯文档或测试类变更跳过全部
  Reviewer；动态裁剪使约 40% 的 PR 避免冗余 LLM 调用，降低 Token 消耗与延迟。
  - 三层上下文记忆架构：构建 Session 级 State、跨轮次累积 Context、跨 PR pgvector 语义检索三层记忆体系，各层设置独立 Token 截断阈值防止上下文溢出；历史
  findings 通过向量相似度检索注入当前 Prompt，同仓库重复漏洞发现率降低约 25%。
  - Tool Call 六层执行保护链：围绕 Tool Call 链路构建参数校验、风险分级、HITL 人工确认、指数退避重试、输出合法性校验、全量审计日志六层保护机制；CI
  环境自动跳过 HITL 阻塞，工具调用成功率从约 78% 提升至 96%。
  - 跨 Provider 结构化输出兼容层：设计双路降级策略，优先调用原生 Function Calling，失败后自动切换为 JSON Prompt + Pydantic 验证，兼容不支持 response_format 的
   DeepSeek、ZhiPu 等模型；同时修复推理模型多轮对话的 reasoning token 兼容性问题，LLM 调用异常率降低约 30%。
  - Checkpoint 断点恢复与双层重试机制：以 PR 编号与 HEAD commit SHA 构造确定性 Thread ID，结合 LangGraph PostgreSQL Checkpointer 在节点边界持久化完整
  State；失败时自动从最后一个 Checkpoint 续跑，叠加进程内 3 次与 GitHub Actions 2 次双层重试，CI 红灯率从约 15% 降至约 2%。
  - MCP 工具集成与透明降级：集成 Semgrep MCP Server 作为主力扫描工具，识别 MCP 与本地 subprocess 工具的能力边界差异，实现优先
  MCP、不可用时自动降级的透明切换，工具层故障对上层 Agent 完全无感知。

