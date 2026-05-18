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
