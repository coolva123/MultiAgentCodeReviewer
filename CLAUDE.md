# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MultiAgent Code Reviewer — a LangGraph-based system that orchestrates multiple specialized AI agents to perform automated code review. Accepts a GitHub PR URL or local diff file, runs it through a hub-and-spoke multi-agent graph, and produces a structured Markdown review report.

## Working Directory

All commands below should be run from `code-review-agent/`:

```bash
cd code-review-agent
```

## Environment Setup

Copy `.env.example` to `.env` and fill in required keys:

```
LLM_PROVIDER=deepseek          # deepseek | zhipu | openai | anthropic
LLM_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=...
ZHIPU_API_KEY=...              # required for embeddings
GITHUB_TOKEN=...               # required for PR URL mode
TAVILY_API_KEY=...             # required for Research Agent
PG_DATABASE_URL=...            # optional; falls back to MemorySaver if absent
```

Install dependencies (Python 3.11+ required):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Running the System

**CLI (batch mode):**
```bash
# From a local diff file
python main.py --diff-file tests/fixtures/sample.diff

# From a GitHub PR URL
python main.py --pr-url https://github.com/owner/repo/pull/123

# Save report to file and post back as PR comment
python main.py --pr-url https://github.com/owner/repo/pull/123 --output report.md --post-comment
```

**Web server:**
```bash
uvicorn server:app --host 0.0.0.0 --port 8080 --reload
# API: POST /api/review, GET /api/review/{session_id}, GET /api/health
# UI:  http://localhost:8080
```

## Running Tests

```bash
# Integration test with built-in SQL injection diff (no flags needed)
python tests/test_supervisor_flow.py

# With a local diff file
python tests/test_supervisor_flow.py --diff fixtures/sample.diff --repo myorg/myrepo

# With a real GitHub PR (requires GITHUB_TOKEN)
python tests/test_supervisor_flow.py --pr-url https://github.com/owner/repo/pull/123
```

## Architecture

### Graph Structure (LangGraph)

The system uses two nested graphs:

**Outer Supervisor Graph** (`src/graph/supervisor_graph.py`) — Hub-and-Spoke:
```
START → supervisor → research_agent → supervisor
                   → review_pipeline (subgraph) → supervisor
                   → file_review_pipeline (subgraph) → supervisor
                   → report_generator → END
```
The `supervisor_node` (`src/agents/supervisor.py`) uses an LLM to decide the next action each iteration. It has a hard cap of 5 iterations (`_MAX_ITERATIONS`). On the first iteration it queries long-term memory (PostgreSQL + pgvector) for historical findings from the same repo.

**Inner Review Subgraph** (`src/graph/review_subgraph.py`) — two variants:
- `build_review_subgraph()`: full flow — `diff_analyzer → coordinator → [security_reviewer | quality_reviewer]` in parallel
- `build_file_review_subgraph()`: skip analyzer/coordinator, run security + quality in parallel directly

### Shared State

`ReviewState` (`src/graph/state.py`) is the single TypedDict passed through all nodes. List fields (`security_findings`, `quality_findings`, `tool_call_log`, `agent_messages`, `errors`) use `Annotated[List[...], operator.add]` so parallel nodes can append without overwriting each other.

### Agent Roles

| Agent | File | Responsibility |
|-------|------|----------------|
| Supervisor | `src/agents/supervisor.py` | Orchestrates; routes via `Command(goto=...)` |
| Research Agent | `src/agents/research_agent.py` | Web search + GitHub context gathering |
| Diff Analyzer | `src/agents/diff_analyzer.py` | Parses diff; classifies files and risk |
| Coordinator | `src/agents/coordinator.py` | Decides which reviewers to run and with what focus |
| Security Reviewer | `src/agents/security_reviewer.py` | Finds security vulnerabilities |
| Quality Reviewer | `src/agents/quality_reviewer.py` | Finds code quality issues |
| Report Generator | `src/agents/report_generator.py` | Assembles final Markdown report |

### Key Modules

- **`config/settings.py`** — `get_llm()` factory; reads `LLM_PROVIDER` to return the appropriate LangChain chat model. `get_embeddings()` returns ZhiPu embeddings for vector search.
- **`src/harness/checkpointer.py`** — returns `PostgresSaver` if `PG_DATABASE_URL` points to a cloud DB, else degrades silently to `MemorySaver`.
- **`src/harness/tool_guard.py`** — wraps tool calls to log them into `tool_call_log`.
- **`src/output/github_commenter.py`** — posts the final report as a GitHub PR comment.
- **`src/tools/llm_utils.py`** — `call_structured()` helper for structured LLM output via Pydantic models.

### Persistence

- **Checkpointing**: every LangGraph superstep is checkpointed. PostgreSQL (Supabase) is used in production; MemorySaver is the in-process fallback.
- **Long-term memory**: historical findings per repo stored in PostgreSQL with pgvector embeddings; queried by the Supervisor on the first iteration.

### LLM Provider

Switch providers by setting `LLM_PROVIDER` in `.env`. DeepSeek and ZhiPu use OpenAI-compatible endpoints (`ChatOpenAI` with a custom `base_url`). Anthropic uses `ChatAnthropic`. The `get_llm()` function in `config/settings.py` is the single source of truth.
