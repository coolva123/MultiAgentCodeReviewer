# Context Enrichment Agent — Design Spec

**Date:** 2026-05-18  
**Status:** Approved  
**Scope:** Add a `context_enrichment` node to the existing supervisor graph that runs once before the review pipeline, enriching `state.project_context` with three layers of information so all downstream reviewers have richer project understanding.

---

## Problem

Reviewer agents currently operate with only the raw diff and PR metadata. They lack:
1. Knowledge of what the project is (tech stack, security posture, conventions)
2. Context from sibling files in the same directories being changed
3. Historical findings from previous reviews of the same repo

This causes reviewers to produce generic feedback that misses project-specific patterns.

---

## Solution Overview

Insert a `context_enrichment` node into the supervisor graph. On iteration 0, the Supervisor routes here instead of directly to the review pipeline. The node fills `state.project_context` with three layers, then returns to the Supervisor for normal routing.

---

## Graph Structure

```
START → supervisor
            ↓ (iter=0, project_context empty)
    context_enrichment
            ↓ (fixed edge back)
        supervisor
            ↓ (iter=1+, normal routing)
    research_agent / review_pipeline / file_review_pipeline
            ↓
        supervisor
            ↓
    report_generator → END
```

The inner review subgraph (`diff_analyzer → coordinator → [security_reviewer | quality_reviewer]`) is unchanged.

---

## Three Layers

### Layer 1 — Project Profile (LLM + DB cache)

**Goal:** Understand what the project is before reading a single line of diff.

**Cache key:** `(repo_name, readme_sha)` — SHA comes from the GitHub API `/repos/{owner}/{repo}/readme` response (field `sha`). If unavailable, fall back to MD5 of README content.

**Cache hit condition:** record exists in `project_profiles` AND (`readme_sha` matches OR `updated_at > now() - 30 days`).

**On cache miss:**
1. `fetch_repo_readme` → README content + sha
2. `fetch_repo_structure` → top-level directory tree
3. `fetch_file_content("CLAUDE.md")` → skip gracefully on 404
4. LLM `call_structured` → `ProfileModel` (Pydantic)
5. `save_profile` → upsert into `project_profiles`

**ProfileModel fields:** `tech_stack`, `project_type`, `security_level` (`high/medium/low`), `frameworks`, `conventions`, `summary` (≤200 chars).

**Token cost:** ~800 tokens LLM call, at most once per 30 days per repo. Cache hit = 0 LLM tokens.

**DB schema (Supabase, same PG instance):**
```sql
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

### Layer 2 — Related Files (pure heuristic, zero LLM)

**Goal:** Give reviewers the sibling files in changed directories so they can judge consistency and impact.

**Steps:**
1. Extract `changed_files` from `diff_content` via regex: `r'^diff --git a/(.+?) b/'`
2. Collect parent directories of changed files
3. Fetch full file tree via `fetch_repo_structure`
4. Filter candidates: same directory, not in `changed_files`, not test files, not docs/config, is a code file (`.py .js .ts .go .java .rb .rs`)
5. Score candidates:
   - +3 if filename shares a common prefix with any changed file (same module)
   - +2 if filename contains `model/schema/base/core/util/service`
   - +1 if extension matches any changed file
6. Take top 3 by score; fetch each via `fetch_file_content`, truncate to first 80 lines

**Token cost:** ~600 tokens input, no LLM call.

### Layer 3 — Historical Findings (migrated from Supervisor)

**Goal:** Surface recurring patterns from previous reviews of the same repo.

**Change from current:** query text changes from generic `"security vulnerabilities quality issues bugs"` to repo+file-specific: `f"{repo_name} 改动文件: {', '.join(changed_files[:5])}"`.

**Result:** top-5 findings from `review_findings` table via pgvector cosine similarity.

**Migration:** remove the identical query in `supervisor_node` (lines 69–81 of `supervisor.py`) to avoid duplicate DB round-trips.

---

## State Changes

**`ReviewState`** gains one field:
```python
project_context: Dict[str, Any]   # filled by context_enrichment_node
```

**Output shape of `context_enrichment_node`:**
```python
{
    "project_context": {
        "profile": {
            "tech_stack": str,
            "project_type": str,
            "security_level": "high" | "medium" | "low",
            "frameworks": str,
            "conventions": str,
            "summary": str,
            "from_cache": bool,
        },
        "related_files": [
            {"path": str, "content": str},  # content = first 80 lines
        ],
        "historical_findings": str,  # same format as current historical_context
    },
    "agent_messages": ["[ContextEnrichment] profile=cached|fresh | related_files=N | history=M条"],
}
```

---

## Supervisor Routing Change

**Before:** iteration=0 → LLM decides (research or review)  
**After:** iteration=0, `project_context` empty → hardcoded goto `context_enrichment` (no LLM call needed)

```python
# In supervisor_node, before LLM call:
if iteration == 0 and not state.get("project_context"):
    return Command(
        goto="context_enrichment",
        update={"iteration_count": iteration + 1, ...},
    )
```

This short-circuits the LLM decision for the first iteration, saving ~200 tokens on every run.

---

## File Changes

| Type | File | Change |
|------|------|--------|
| New | `src/agents/context_enrichment.py` | Three-layer node |
| New | `src/harness/memory/project_profile.py` | `get_profile` / `save_profile` |
| New | `src/prompts/context_enrichment.py` | Profile generation prompt |
| Modify | `src/tools/research_tools.py` | `fetch_repo_readme` returns `{"content", "sha"}` |
| Modify | `src/graph/state.py` | Add `project_context: Dict[str, Any]` |
| Modify | `src/graph/supervisor_graph.py` | Register node + add `context_enrichment → supervisor` edge |
| Modify | `src/agents/supervisor.py` | Early-exit routing at iter=0 + remove historical query |
| Modify | `main.py` | `"project_context": {}` in initial_state |
| Modify | `server.py` | `"project_context": {}` in `_base_state()` |
| Modify | `tests/test_supervisor_flow.py` | `"project_context": {}` in `_build_initial_state()` |

---

## Token Budget per PR Review

| Source | Tokens | Notes |
|--------|--------|-------|
| fetch_repo_readme | ~1,000 | input only |
| fetch_repo_structure | ~200 | input only |
| fetch_file_content ×3 | ~600 | 80 lines each |
| LLM profile generation | ~800 | cache miss only, ≤once/30d |
| pgvector history query | ~500 | input only |
| **Cache hit total** | **~2,300** | no LLM call |
| **Cache miss total** | **~3,100** | one LLM call |

---

## Out of Scope

- Downstream agents reading `project_context` — they will have access to the field; prompt changes to actually use it are a separate task.
- Dependency reviewer / test coverage reviewer integration — separate agents already in progress.
- Layer 2 for local diff mode (no `repo_url`) — skip gracefully, `related_files` = `[]`.
