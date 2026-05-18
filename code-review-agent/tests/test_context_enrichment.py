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
