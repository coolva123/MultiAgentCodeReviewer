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


# ── Task 2 tests: ProjectProfileStore ────────────────────────────────────────

class TestProjectProfileStore:
    def _make_store(self):
        from src.harness.memory.project_profile import ProjectProfileStore
        store = ProjectProfileStore()
        ProjectProfileStore._schema_ready = True  # skip real DB init
        return store

    @patch("psycopg2.connect")
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

    @patch("psycopg2.connect")
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

    @patch("psycopg2.connect")
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
