# Context Enrichment Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `context_enrichment` node to the supervisor graph that fires on iteration 0 and fills `state.project_context` with a project profile, related sibling files, and historical findings — giving all downstream reviewers richer context before they read a single line of diff.

**Architecture:** Hub-and-spoke graph unchanged; `context_enrichment` is a new spoke. Supervisor short-circuits at iteration 0 (no LLM call) to route there, then normal LLM-driven routing resumes at iteration 1. Three internal layers: DB-cached LLM profile → heuristic sibling-file selection → enhanced pgvector history query (migrated from supervisor).

**Tech Stack:** Python 3.11, LangGraph, psycopg2, pgvector, pydantic v2, pytest, unittest.mock

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/agents/context_enrichment.py` | Three-layer node + heuristic helpers |
| Create | `src/harness/memory/project_profile.py` | `get_profile` / `save_profile` against `project_profiles` table |
| Create | `src/prompts/context_enrichment.py` | SYSTEM + HUMAN prompt for profile generation |
| Create | `tests/test_context_enrichment.py` | Unit tests (mocked I/O) |
| Modify | `src/tools/research_tools.py` | `fetch_repo_readme` → returns JSON `{"content", "sha"}` |
| Modify | `src/graph/state.py` | Add `project_context: Dict[str, Any]` |
| Modify | `src/graph/supervisor_graph.py` | Register node + `context_enrichment → supervisor` edge |
| Modify | `src/agents/supervisor.py` | iter=0 early-exit route + remove duplicated history query |
| Modify | `main.py` | `"project_context": {}` in initial_state |
| Modify | `server.py` | `"project_context": {}` in `_base_state()` |
| Modify | `tests/test_supervisor_flow.py` | `"project_context": {}` in `_build_initial_state()` |

---

## Pre-requisite: Create DB Table (Manual)

Run the following SQL in your Supabase SQL editor **before starting Task 1**:

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

---

## Task 1: Update `fetch_repo_readme` to return JSON with SHA

**Files:**
- Modify: `src/tools/research_tools.py` (lines 54–83)
- Test: `tests/test_context_enrichment.py` (create file here)

- [ ] **Step 1: Create the test file with the readme tool test**

Create `code-review-agent/tests/test_context_enrichment.py`:

```python
import base64
import json
from unittest.mock import MagicMock, patch

import pytest


# ── Task 1 tests: fetch_repo_readme ──────────────────────────────────────────

class TestFetchRepoReadme:
    def _make_response(self, sha: str, content: str, status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {
            "sha": sha,
            "content": base64.b64encode(content.encode()).decode() + "\n",
        }
        return resp

    @patch("src.tools.research_tools.requests.get")
    def test_returns_json_with_content_and_sha(self, mock_get):
        mock_get.return_value = self._make_response("abc123", "# My Project\nA great app.")
        from src.tools.research_tools import fetch_repo_readme
        raw = fetch_repo_readme.invoke({"repo_url": "https://github.com/owner/repo"})
        result = json.loads(raw)
        assert result["sha"] == "abc123"
        assert "My Project" in result["content"]

    @patch("src.tools.research_tools.requests.get")
    def test_404_returns_json_with_empty_sha(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp
        from src.tools.research_tools import fetch_repo_readme
        raw = fetch_repo_readme.invoke({"repo_url": "https://github.com/owner/repo"})
        result = json.loads(raw)
        assert result["sha"] == ""
        assert "not found" in result["content"].lower()

    @patch("src.tools.research_tools.requests.get")
    def test_truncates_long_readme(self, mock_get):
        long_text = "x" * 5000
        mock_get.return_value = self._make_response("sha1", long_text)
        from src.tools.research_tools import fetch_repo_readme
        raw = fetch_repo_readme.invoke({"repo_url": "https://github.com/owner/repo"})
        result = json.loads(raw)
        assert len(result["content"]) < 5000
        assert "truncated" in result["content"]
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py::TestFetchRepoReadme -v 2>&1 | head -30
```

Expected: FAIL (current tool returns plain text, not JSON).

- [ ] **Step 3: Update `fetch_repo_readme` in `src/tools/research_tools.py`**

Replace the entire `fetch_repo_readme` function (lines 54–83) with:

```python
@tool
def fetch_repo_readme(repo_url: str) -> str:
    """获取 GitHub 仓库的 README，了解项目背景和技术栈。
    返回 JSON 字符串：{"content": str, "sha": str}

    Args:
        repo_url: GitHub 仓库 URL，例如 https://github.com/owner/repo
    """
    import base64
    try:
        from config.settings import GITHUB_TOKEN

        path = repo_url.rstrip("/").replace("https://github.com/", "")
        if "/" not in path:
            return json.dumps({"content": f"Invalid GitHub URL: {repo_url}", "sha": ""})

        api_url = f"https://api.github.com/repos/{path}/readme"
        headers = {"Accept": "application/vnd.github+json"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            sha = data.get("sha", "")
            raw = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
            content = raw[:4000] + ("\n...[truncated]" if len(raw) > 4000 else "")
            return json.dumps({"content": content, "sha": sha})
        elif resp.status_code == 404:
            return json.dumps({"content": "README not found for this repository.", "sha": ""})
        else:
            return json.dumps({"content": f"Could not fetch README: HTTP {resp.status_code}", "sha": ""})
    except Exception as exc:
        logger.warning("[fetch_repo_readme] 失败: %s", exc)
        return json.dumps({"content": f"Error fetching README: {exc}", "sha": ""})
```

Also add `import json` at the top of `research_tools.py` if not already present (it is not).

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py::TestFetchRepoReadme -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add code-review-agent/src/tools/research_tools.py code-review-agent/tests/test_context_enrichment.py
git commit -m "feat: fetch_repo_readme returns JSON with content+sha"
```

---

## Task 2: Create `src/harness/memory/project_profile.py`

**Files:**
- Create: `src/harness/memory/project_profile.py`
- Test: `tests/test_context_enrichment.py` (append)

- [ ] **Step 1: Add tests for project_profile store**

Append to `tests/test_context_enrichment.py`:

```python

# ── Task 2 tests: ProjectProfileStore ────────────────────────────────────────

class TestProjectProfileStore:
    def _make_store(self):
        from src.harness.memory.project_profile import ProjectProfileStore
        store = ProjectProfileStore()
        ProjectProfileStore._schema_ready = True  # skip real DB init
        return store

    @patch("src.harness.memory.project_profile.psycopg2.connect")
    def test_get_profile_cache_hit(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (
            "Python", "web-api", "high", "FastAPI", "type hints required", "A web API", "sha1", "{}"
        )
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = conn

        store = self._make_store()
        result = store.get_profile("owner/repo", "sha1")
        assert result is not None
        assert result["tech_stack"] == "Python"
        assert result["from_cache"] is True

    @patch("src.harness.memory.project_profile.psycopg2.connect")
    def test_get_profile_cache_miss(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = conn

        store = self._make_store()
        result = store.get_profile("owner/repo", "sha_unknown")
        assert result is None

    @patch("src.harness.memory.project_profile.psycopg2.connect")
    def test_save_profile_calls_upsert(self, mock_connect):
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = conn

        store = self._make_store()
        store.save_profile("owner/repo", {"tech_stack": "Go"}, "sha2")
        assert cur.execute.called
        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO project_profiles" in sql
        assert "ON CONFLICT" in sql
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py::TestProjectProfileStore -v 2>&1 | head -20
```

Expected: FAIL (module does not exist yet).

- [ ] **Step 3: Create `src/harness/memory/project_profile.py`**

```python
import json
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
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
)
"""


class ProjectProfileStore:
    _schema_ready: bool = False

    @contextmanager
    def _connection(self):
        import psycopg2
        from config.settings import PG_DATABASE_URL
        conn = psycopg2.connect(PG_DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        if ProjectProfileStore._schema_ready:
            return
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_TABLE)
            ProjectProfileStore._schema_ready = True
            logger.info("[ProjectProfileStore] schema ready")
        except Exception as exc:
            logger.error("[ProjectProfileStore] schema init failed: %s", exc)

    def get_profile(self, repo_name: str, current_readme_sha: str) -> Optional[dict]:
        """Return cached profile or None on cache miss."""
        self._ensure_schema()
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT tech_stack, project_type, security_level, frameworks,
                               conventions, summary, readme_sha, raw_profile
                        FROM project_profiles
                        WHERE repo_name = %s
                          AND (readme_sha = %s OR updated_at > NOW() - INTERVAL '30 days')
                        """,
                        (repo_name, current_readme_sha),
                    )
                    row = cur.fetchone()
            if row:
                return {
                    "tech_stack":      row[0] or "",
                    "project_type":    row[1] or "",
                    "security_level":  row[2] or "medium",
                    "frameworks":      row[3] or "",
                    "conventions":     row[4] or "",
                    "summary":         row[5] or "",
                    "readme_sha":      row[6] or "",
                    "from_cache":      True,
                }
            return None
        except Exception as exc:
            logger.warning("[ProjectProfileStore] get_profile failed: %s", exc)
            return None

    def save_profile(self, repo_name: str, profile: dict, readme_sha: str) -> None:
        self._ensure_schema()
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_profiles
                            (repo_name, tech_stack, project_type, security_level,
                             frameworks, conventions, summary, readme_sha, raw_profile, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (repo_name) DO UPDATE SET
                            tech_stack     = EXCLUDED.tech_stack,
                            project_type   = EXCLUDED.project_type,
                            security_level = EXCLUDED.security_level,
                            frameworks     = EXCLUDED.frameworks,
                            conventions    = EXCLUDED.conventions,
                            summary        = EXCLUDED.summary,
                            readme_sha     = EXCLUDED.readme_sha,
                            raw_profile    = EXCLUDED.raw_profile,
                            updated_at     = NOW()
                        """,
                        (
                            repo_name,
                            profile.get("tech_stack", ""),
                            profile.get("project_type", ""),
                            profile.get("security_level", "medium"),
                            profile.get("frameworks", ""),
                            profile.get("conventions", ""),
                            profile.get("summary", ""),
                            readme_sha,
                            json.dumps(profile),
                        ),
                    )
            logger.info("[ProjectProfileStore] saved profile | repo=%s", repo_name)
        except Exception as exc:
            logger.error("[ProjectProfileStore] save_profile failed: %s", exc)


_instance: Optional[ProjectProfileStore] = None


def get_project_profile_store() -> ProjectProfileStore:
    global _instance
    if _instance is None:
        _instance = ProjectProfileStore()
    return _instance
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py::TestProjectProfileStore -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add code-review-agent/src/harness/memory/project_profile.py code-review-agent/tests/test_context_enrichment.py
git commit -m "feat: add ProjectProfileStore with get_profile/save_profile"
```

---

## Task 3: Create `src/prompts/context_enrichment.py`

**Files:**
- Create: `src/prompts/context_enrichment.py`

- [ ] **Step 1: Create the prompt file**

```python
SYSTEM = """\
You are a senior software architect analyzing a GitHub repository.
Given a README, directory structure, and optional project config, produce a concise structured profile.
Respond ONLY with valid JSON matching the requested schema — no markdown fences, no explanation.
"""

HUMAN = """\
## README
{readme}

## Directory Structure (top-level)
{structure}

## Project Config (CLAUDE.md or similar — may be empty)
{config}

Extract a project profile. Return a JSON object with exactly these fields:
- "tech_stack": main programming language(s) and runtime (e.g. "Python 3.11, FastAPI")
- "project_type": one of: web-api, web-app, cli, library, data-pipeline, mobile, infrastructure, other
- "security_level": "high" if the project handles auth/payments/PII; "low" if purely internal tooling; "medium" otherwise
- "frameworks": comma-separated list of major frameworks/libraries used
- "conventions": 1-2 sentences on coding conventions or review notes mentioned in the docs (empty string if none)
- "summary": one sentence (≤200 chars) describing what the project does

Return JSON only, no other text.
"""
```

- [ ] **Step 2: Commit**

```bash
git add code-review-agent/src/prompts/context_enrichment.py
git commit -m "feat: add context_enrichment LLM prompt for project profile"
```

---

## Task 4: Create `src/agents/context_enrichment.py`

**Files:**
- Create: `src/agents/context_enrichment.py`
- Test: `tests/test_context_enrichment.py` (append)

- [ ] **Step 1: Add tests for helper functions and the node**

Append to `tests/test_context_enrichment.py`:

```python

# ── Task 4 tests: context_enrichment helpers ─────────────────────────────────

SAMPLE_DIFF = """\
diff --git a/src/auth/login.py b/src/auth/login.py
index abc..def 100644
--- a/src/auth/login.py
+++ b/src/auth/login.py
@@ -1,3 +1,4 @@
+import hashlib
 def login(user): pass
diff --git a/tests/test_auth.py b/tests/test_auth.py
index abc..def 100644
--- a/tests/test_auth.py
+++ b/tests/test_auth.py
@@ -1 +1,2 @@
+# new test
"""

SAMPLE_STRUCTURE = """\
📁 src
📁 src/auth
📄 src/auth/login.py
📄 src/auth/models.py
📄 src/auth/utils.py
📄 src/auth/test_helpers.py
📄 README.md
📄 requirements.txt
📄 config.yml
"""


class TestExtractChangedFiles:
    def test_extracts_both_files(self):
        from src.agents.context_enrichment import _extract_changed_files
        files = _extract_changed_files(SAMPLE_DIFF)
        assert "src/auth/login.py" in files
        assert "tests/test_auth.py" in files

    def test_empty_diff_returns_empty(self):
        from src.agents.context_enrichment import _extract_changed_files
        assert _extract_changed_files("") == []


class TestSelectRelatedFiles:
    def test_excludes_changed_files(self):
        from src.agents.context_enrichment import _select_related_files
        files = _select_related_files(SAMPLE_DIFF, SAMPLE_STRUCTURE)
        assert "src/auth/login.py" not in files

    def test_excludes_test_files(self):
        from src.agents.context_enrichment import _select_related_files
        files = _select_related_files(SAMPLE_DIFF, SAMPLE_STRUCTURE)
        assert all("test" not in f.lower() for f in files)

    def test_excludes_config_and_docs(self):
        from src.agents.context_enrichment import _select_related_files
        files = _select_related_files(SAMPLE_DIFF, SAMPLE_STRUCTURE)
        for f in files:
            assert not f.endswith((".yml", ".yaml", ".md", ".txt"))

    def test_max_3_files(self):
        from src.agents.context_enrichment import _select_related_files
        files = _select_related_files(SAMPLE_DIFF, SAMPLE_STRUCTURE)
        assert len(files) <= 3

    def test_no_diff_returns_empty(self):
        from src.agents.context_enrichment import _select_related_files
        assert _select_related_files("", SAMPLE_STRUCTURE) == []

    def test_prefers_same_directory_files(self):
        from src.agents.context_enrichment import _select_related_files
        files = _select_related_files(SAMPLE_DIFF, SAMPLE_STRUCTURE)
        # src/auth/models.py and src/auth/utils.py should be top picks
        assert any("src/auth" in f for f in files)


class TestContextEnrichmentNode:
    def _base_state(self, **kwargs):
        state = {
            "repo_name": "owner/repo",
            "repo_url": "https://github.com/owner/repo",
            "diff_content": SAMPLE_DIFF,
            "project_context": {},
            "iteration_count": 0,
        }
        state.update(kwargs)
        return state

    @patch("src.agents.context_enrichment.fetch_repo_readme")
    @patch("src.agents.context_enrichment.fetch_repo_structure")
    @patch("src.agents.context_enrichment.fetch_file_content")
    @patch("src.agents.context_enrichment.get_project_profile_store")
    @patch("src.agents.context_enrichment.get_long_term_memory")
    @patch("src.agents.context_enrichment.get_llm")
    def test_node_returns_project_context(
        self, mock_llm, mock_mem, mock_store, mock_fc, mock_struct, mock_readme
    ):
        import json
        from src.agents.context_enrichment import ProfileModel

        mock_readme.invoke.return_value = json.dumps({"content": "# Repo", "sha": "abc"})
        mock_struct.invoke.return_value = "📄 src/auth/models.py\n📄 src/auth/utils.py"
        mock_fc.invoke.return_value = json.dumps({"found": False})

        store = MagicMock()
        store.get_profile.return_value = None
        mock_store.return_value = store

        pm = ProfileModel(
            tech_stack="Python",
            project_type="web-api",
            security_level="high",
            frameworks="FastAPI",
            conventions="",
            summary="A web API.",
        )
        mock_llm.return_value.invoke = MagicMock()

        with patch("src.agents.context_enrichment.call_structured", return_value=pm):
            mock_mem.return_value.query.return_value = ["[HIGH] login.py — SQL injection"]
            from src.agents.context_enrichment import context_enrichment_node
            result = context_enrichment_node(self._base_state())

        assert "project_context" in result
        ctx = result["project_context"]
        assert ctx["profile"]["tech_stack"] == "Python"
        assert ctx["profile"]["security_level"] == "high"
        assert isinstance(ctx["related_files"], list)
        assert "SQL injection" in ctx["historical_findings"]

    @patch("src.agents.context_enrichment.fetch_repo_readme")
    @patch("src.agents.context_enrichment.fetch_repo_structure")
    @patch("src.agents.context_enrichment.fetch_file_content")
    @patch("src.agents.context_enrichment.get_project_profile_store")
    @patch("src.agents.context_enrichment.get_long_term_memory")
    def test_node_uses_cache_when_available(
        self, mock_mem, mock_store, mock_fc, mock_struct, mock_readme
    ):
        import json

        mock_readme.invoke.return_value = json.dumps({"content": "# Repo", "sha": "abc"})
        mock_struct.invoke.return_value = ""
        mock_fc.invoke.return_value = json.dumps({"found": False})

        cached = {
            "tech_stack": "Go", "project_type": "cli",
            "security_level": "low", "frameworks": "cobra",
            "conventions": "", "summary": "A CLI tool.", "from_cache": True,
        }
        store = MagicMock()
        store.get_profile.return_value = cached
        mock_store.return_value = store
        mock_mem.return_value.query.return_value = []

        from src.agents.context_enrichment import context_enrichment_node
        result = context_enrichment_node(self._base_state())
        assert result["project_context"]["profile"]["from_cache"] is True
        assert result["project_context"]["profile"]["tech_stack"] == "Go"
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py::TestExtractChangedFiles tests/test_context_enrichment.py::TestSelectRelatedFiles tests/test_context_enrichment.py::TestContextEnrichmentNode -v 2>&1 | head -30
```

Expected: FAIL (module not yet created).

- [ ] **Step 3: Create `src/agents/context_enrichment.py`**

```python
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import src.prompts.context_enrichment as prompt_tmpl
from config.settings import get_llm
from src.graph.state import ReviewState
from src.harness.memory.long_term import get_long_term_memory
from src.harness.memory.project_profile import get_project_profile_store
from src.tools.github_tools import fetch_file_content
from src.tools.llm_utils import call_structured
from src.tools.research_tools import fetch_repo_readme, fetch_repo_structure

logger = logging.getLogger(__name__)

_MAX_RELATED_FILES = 3
_RELATED_FILE_LINES = 80
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".java", ".rb", ".rs"}
_PRIORITY_KEYWORDS = {"model", "schema", "base", "core", "util", "service"}
_EXCLUDE_PATTERNS = {"test_", "_test.", "/tests/"}
_EXCLUDE_EXTENSIONS = {".md", ".rst", ".txt", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini"}


class ProfileModel(BaseModel):
    tech_stack: str = Field(default="unknown")
    project_type: str = Field(default="other")
    security_level: str = Field(default="medium")
    frameworks: str = Field(default="")
    conventions: str = Field(default="")
    summary: str = Field(default="")


def _extract_changed_files(diff_content: str) -> List[str]:
    return re.findall(r"^diff --git a/(.+?) b/", diff_content, re.MULTILINE)


def _parse_tree_path(line: str) -> str:
    """Strip emoji/space prefix from fetch_repo_structure output lines."""
    stripped = line.strip()
    m = re.search(r"[a-zA-Z0-9_]", stripped)
    return stripped[m.start():] if m else ""


def _score_candidate(path: str, changed_files: List[str]) -> int:
    score = 0
    name = os.path.basename(path).lower()
    ext = os.path.splitext(path)[1].lower()
    for cf in changed_files:
        cf_dir = os.path.dirname(cf)
        if cf_dir and path.startswith(cf_dir + "/"):
            score += 3
            break
    if any(kw in name for kw in _PRIORITY_KEYWORDS):
        score += 2
    changed_exts = {os.path.splitext(f)[1].lower() for f in changed_files}
    if ext in changed_exts:
        score += 1
    return score


def _select_related_files(diff_content: str, structure_text: str) -> List[str]:
    changed_files = _extract_changed_files(diff_content)
    if not changed_files:
        return []
    changed_set = set(changed_files)
    changed_dirs = {os.path.dirname(f) for f in changed_files if os.path.dirname(f)}

    candidates = []
    for line in structure_text.splitlines():
        path = _parse_tree_path(line)
        if not path or path in changed_set:
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in _CODE_EXTENSIONS:
            continue
        if any(pat in path for pat in _EXCLUDE_PATTERNS):
            continue
        if os.path.dirname(path) not in changed_dirs:
            continue
        candidates.append((_score_candidate(path, changed_files), path))

    candidates.sort(key=lambda x: -x[0])
    return [p for _, p in candidates[:_MAX_RELATED_FILES]]


def context_enrichment_node(state: ReviewState) -> Dict[str, Any]:
    repo_name = state.get("repo_name", "")
    repo_url = state.get("repo_url", "")
    diff_content = state.get("diff_content", "")

    # ── Layer 1: Project Profile ───────────────────────────────────────────────
    profile: dict = {}
    structure_text = ""
    if repo_url:
        try:
            readme_raw = fetch_repo_readme.invoke({"repo_url": repo_url})
            try:
                readme_result = json.loads(readme_raw)
            except (json.JSONDecodeError, TypeError):
                readme_result = {"content": readme_raw, "sha": ""}
            readme_content = readme_result.get("content", "") if isinstance(readme_result, dict) else str(readme_result)
            readme_sha = readme_result.get("sha", "") if isinstance(readme_result, dict) else ""
            if not readme_sha:
                readme_sha = hashlib.md5(readme_content.encode()).hexdigest()

            store = get_project_profile_store()
            cached = store.get_profile(repo_name, readme_sha)

            if cached:
                profile = cached
                logger.info("[ContextEnrichment] Layer1 cache hit | repo=%s", repo_name)
            else:
                structure_text = fetch_repo_structure.invoke({"repo_url": repo_url})
                claude_raw = fetch_file_content.invoke({"repo_url": repo_url, "file_path": "CLAUDE.md"})
                try:
                    claude_result = json.loads(claude_raw)
                    config_content = claude_result.get("content", "") if claude_result.get("found") else ""
                except (json.JSONDecodeError, TypeError):
                    config_content = ""

                messages = [
                    SystemMessage(content=prompt_tmpl.SYSTEM),
                    HumanMessage(content=prompt_tmpl.HUMAN.format(
                        readme=readme_content[:3000],
                        structure=structure_text[:1000],
                        config=config_content[:1000],
                    )),
                ]
                llm = get_llm(temperature=0.0)
                pm = call_structured(llm, messages, ProfileModel)
                profile = pm.model_dump() if pm else {}
                profile["from_cache"] = False

                if profile and repo_name:
                    store.save_profile(repo_name, profile, readme_sha)

                logger.info("[ContextEnrichment] Layer1 profile generated | repo=%s", repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer1 failed: %s", exc)
            profile = {"from_cache": False}

    # ── Layer 2: Related Files ─────────────────────────────────────────────────
    related_files: list = []
    if repo_url and diff_content:
        try:
            if not structure_text:
                structure_text = fetch_repo_structure.invoke({"repo_url": repo_url})
            candidate_paths = _select_related_files(diff_content, structure_text)
            for path in candidate_paths:
                raw = fetch_file_content.invoke({"repo_url": repo_url, "file_path": path})
                try:
                    result = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if result.get("found"):
                    lines = result.get("content", "").splitlines()[:_RELATED_FILE_LINES]
                    related_files.append({"path": path, "content": "\n".join(lines)})
            logger.info("[ContextEnrichment] Layer2 related_files=%d | repo=%s", len(related_files), repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer2 failed: %s", exc)

    # ── Layer 3: Historical Findings ───────────────────────────────────────────
    historical_findings = ""
    if repo_name and repo_name != "unknown":
        try:
            changed_files = _extract_changed_files(diff_content) if diff_content else []
            changed_summary = ", ".join(changed_files[:5]) if changed_files else ""
            query_text = (
                f"{repo_name} 改动文件: {changed_summary}"
                if changed_summary
                else f"security issues in {repo_name}"
            )
            results = get_long_term_memory().query(
                repo_name=repo_name, query_text=query_text, top_k=5
            )
            historical_findings = "\n".join(results) if results else ""
            logger.info("[ContextEnrichment] Layer3 history=%d findings | repo=%s", len(results), repo_name)
        except Exception as exc:
            logger.warning("[ContextEnrichment] Layer3 failed: %s", exc)

    from_cache = profile.get("from_cache", False)
    history_count = historical_findings.count("\n") + 1 if historical_findings else 0
    msg = (
        f"[ContextEnrichment] profile={'cached' if from_cache else 'fresh'} | "
        f"related_files={len(related_files)} | history={history_count}条"
    )
    logger.info(msg)

    return {
        "project_context": {
            "profile": profile,
            "related_files": related_files,
            "historical_findings": historical_findings,
        },
        "historical_context": historical_findings,
        "agent_messages": [msg],
    }
```

- [ ] **Step 4: Run all context_enrichment tests**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add code-review-agent/src/agents/context_enrichment.py code-review-agent/tests/test_context_enrichment.py
git commit -m "feat: implement context_enrichment_node (3-layer context)"
```

---

## Task 5: Update `src/graph/state.py`

**Files:**
- Modify: `src/graph/state.py`

- [ ] **Step 1: Add `project_context` field to `ReviewState`**

In `src/graph/state.py`, add the import for `Any` (already present) and insert the new field after `historical_context`:

Find this block:
```python
    research_context: str                   # Research Agent 累积输出
    historical_context: str                 # 首轮从长期记忆查询的同仓库历史 findings 摘要
    supervisor_instruction: str             # Supervisor 给下一个 Agent 的指令
```

Replace with:
```python
    research_context: str                   # Research Agent 累积输出
    historical_context: str                 # 首轮从长期记忆查询的同仓库历史 findings 摘要
    project_context: Dict[str, Any]         # ContextEnrichment 填充的三层上下文
    supervisor_instruction: str             # Supervisor 给下一个 Agent 的指令
```

- [ ] **Step 2: Verify import is correct**

```bash
cd code-review-agent && python -c "from src.graph.state import ReviewState; print('OK')"
```

Expected output: `OK`

- [ ] **Step 3: Commit**

```bash
git add code-review-agent/src/graph/state.py
git commit -m "feat: add project_context field to ReviewState"
```

---

## Task 6: Register node in `src/graph/supervisor_graph.py`

**Files:**
- Modify: `src/graph/supervisor_graph.py`

- [ ] **Step 1: Add import and node registration**

At the top of `supervisor_graph.py`, add the import alongside the other agent imports:

```python
from src.agents.context_enrichment import context_enrichment_node
```

Inside `build_supervisor_graph()`, after the line `builder.add_node("report_generator", report_generator_node)`, add:

```python
    builder.add_node("context_enrichment", context_enrichment_node)
```

After the line `builder.add_edge("file_review_pipeline", "supervisor")`, add:

```python
    builder.add_edge("context_enrichment", "supervisor")
```

- [ ] **Step 2: Verify graph compiles**

```bash
cd code-review-agent && python -c "from src.graph.supervisor_graph import build_supervisor_graph; g = build_supervisor_graph(); print('nodes:', list(g.nodes))"
```

Expected: output includes `context_enrichment` in the nodes list.

- [ ] **Step 3: Commit**

```bash
git add code-review-agent/src/graph/supervisor_graph.py
git commit -m "feat: register context_enrichment node in supervisor graph"
```

---

## Task 7: Update `src/agents/supervisor.py` — routing + remove duplicate history query

**Files:**
- Modify: `src/agents/supervisor.py`

- [ ] **Step 1: Add iter=0 early-exit routing**

In `supervisor_node`, find the block that begins with the comment `# 首轮：从长期记忆查询同仓库历史 findings 摘要` (lines 67–81 in the current file). **Delete the entire block** (lines 69–81):

```python
    # 首轮：从长期记忆查询同仓库历史 findings 摘要，后续轮次直接复用
    historical_context = state.get("historical_context", "")
    if iteration == 0 and not historical_context and repo_name and repo_name != "unknown":
        try:
            from src.harness.memory.long_term import get_long_term_memory
            results = get_long_term_memory().query(
                repo_name=repo_name,
                query_text="security vulnerabilities quality issues bugs",
                top_k=3,
            )
            historical_context = "\n".join(results) if results else ""
            if historical_context:
                logger.info("[Supervisor] 加载历史记忆 %d 条", len(results))
        except Exception as exc:
            logger.warning("[Supervisor] 历史记忆查询失败（跳过）: %s", exc)
```

Replace the deleted block with just the variable read plus the iter=0 early-exit:

```python
    historical_context = state.get("historical_context", "")
    project_context = state.get("project_context", {})

    # iter=0: always enrich context first (no LLM call needed)
    if iteration == 0 and not project_context:
        return Command(
            goto="context_enrichment",
            update={
                "iteration_count": iteration + 1,
                "agent_messages": [f"[Supervisor] iter=0 → context_enrichment"],
            },
        )
```

- [ ] **Step 2: Add `project_context` to the LLM prompt variables**

In the `messages` construction block, find `historical_snippet` and add `project_context` summary to the human message. Find the existing `HUMAN.format(...)` call and add `project_summary` as a new parameter:

First update the `historical_snippet` section — add after it:
```python
    profile = project_context.get("profile", {})
    project_summary = profile.get("summary", "") or "Not yet enriched."
    security_level = profile.get("security_level", "medium")
```

Then update the `HumanMessage` content call — add two new format args:
```python
        HumanMessage(content=prompt_tmpl.HUMAN.format(
            repo_name=repo_name,
            repo_url=repo_url or "not provided",
            mode=mode,
            iteration_count=iteration,
            historical_context=historical_snippet,
            research_context=research_snippet,
            sec_count=len(security_findings),
            qual_count=len(quality_findings),
            review_called=review_called,
            has_findings=has_findings,
            recent_messages="\n".join(recent_msgs) if recent_msgs else "None",
            project_summary=project_summary,
            security_level=security_level,
        )),
```

- [ ] **Step 3: Update `src/prompts/supervisor.py` to include the new format variables**

In `src/prompts/supervisor.py`, find the `HUMAN` string. Replace the final `---\n决定下一步行动` section:

```python
HUMAN = """## 当前审查状态

**仓库**：{repo_name}
**仓库 URL**：{repo_url}
**输入模式**：{mode}
**Supervisor 迭代次数**：{iteration_count}

### 项目画像（Context Enrichment）
摘要：{project_summary}（安全级别：{security_level}）

### 该仓库历史审查记录（长期记忆）
{historical_context}

### 本轮研究上下文
{research_context}

### 审查发现
- 安全问题数量：{sec_count}
- 质量问题数量：{qual_count}
- 审查流水线是否已调用：{review_called}
- 是否有任何发现：{has_findings}

### 近期 Agent 消息
{recent_messages}

---
决定下一步行动，选择且仅选择一个。"""
```

- [ ] **Step 4: Verify supervisor imports cleanly**

```bash
cd code-review-agent && python -c "from src.agents.supervisor import supervisor_node; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add code-review-agent/src/agents/supervisor.py code-review-agent/src/prompts/supervisor.py
git commit -m "feat: supervisor routes iter=0 to context_enrichment, removes duplicate history query"
```

---

## Task 8: Update initial_state in `main.py`, `server.py`, and `tests/test_supervisor_flow.py`

**Files:**
- Modify: `main.py` (line ~183 `initial_state` dict)
- Modify: `server.py` (line ~47 `_base_state()` dict)
- Modify: `tests/test_supervisor_flow.py` (`_build_initial_state()` or equivalent dict)

- [ ] **Step 1: Add `"project_context": {}` to `main.py` initial_state**

In `main.py`, find the `initial_state = {` block (~line 183). Add after `"historical_context": "",`:

```python
        "project_context":        {},
```

- [ ] **Step 2: Add `"project_context": {}` to `server.py` `_base_state()`**

In `server.py`, find the `_base_state` function (~line 47). Add after `"historical_context": "",`:

```python
        "project_context": {},
```

- [ ] **Step 3: Add `"project_context": {}` to test initial state**

In `tests/test_supervisor_flow.py`, find the dict that builds the initial state (search for `"historical_context"`). Add after it:

```python
        "project_context":        {},
```

- [ ] **Step 4: Smoke-test the graph boots without error**

```bash
cd code-review-agent && python -c "
from src.graph.supervisor_graph import build_supervisor_graph
g = build_supervisor_graph()
print('Graph nodes:', sorted(g.nodes))
"
```

Expected output (order may vary):
```
Graph nodes: ['__end__', 'context_enrichment', 'file_review_pipeline', 'report_generator', 'research_agent', 'review_pipeline', 'supervisor']
```

- [ ] **Step 5: Run full unit test suite**

```bash
cd code-review-agent && python -m pytest tests/test_context_enrichment.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add code-review-agent/main.py code-review-agent/server.py code-review-agent/tests/test_supervisor_flow.py
git commit -m "feat: add project_context to all initial_state dicts"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by task |
|-----------------|-----------------|
| Layer 1: README fetch + SHA | Task 1, Task 4 |
| Layer 1: DB cache (get/save) | Task 2, Task 3 |
| Layer 1: LLM profile generation | Task 4 (ProfileModel + call_structured) |
| Layer 2: regex changed_files | Task 4 (_extract_changed_files) |
| Layer 2: heuristic file selection | Task 4 (_select_related_files, _score_candidate) |
| Layer 2: 80-line truncation | Task 4 (_RELATED_FILE_LINES constant) |
| Layer 3: enhanced pgvector query | Task 4 (changed_summary in query_text) |
| Layer 3: migrate from supervisor | Task 7 (delete old block) |
| project_context output shape | Task 4 (return dict structure) |
| state.project_context field | Task 5 |
| Graph node registration | Task 6 |
| Supervisor iter=0 early-exit | Task 7 |
| initial_state in all entry points | Task 8 |
| DB table DDL | Pre-requisite |

**Placeholder scan:** No TBDs or incomplete sections found.

**Type consistency check:**
- `ProfileModel` defined in Task 4, used in Task 4 only — consistent.
- `get_project_profile_store()` returns `ProjectProfileStore`, called as `.get_profile(repo_name, sha)` and `.save_profile(repo_name, dict, sha)` — signatures match Task 2 definition.
- `_extract_changed_files` returns `List[str]` — used in both Task 4 (Layer 2 and Layer 3) with consistent iteration.
- `historical_context` written by `context_enrichment_node` in Task 4 and read by `supervisor_node` in Task 7 — field name matches `ReviewState` definition in Task 5.
