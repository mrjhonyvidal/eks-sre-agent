"""Unit tests for sre_agent/github_client.py."""

from __future__ import annotations

import base64
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.github_client import GitHubClient


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch) -> GitHubClient:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("GITHUB_REPO", "testorg/test-repo")
    monkeypatch.setenv("GITHUB_BASE_BRANCH", "main")
    return GitHubClient()


class TestGitHubClientInit:
    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
        monkeypatch.setenv("GITHUB_REPO", "org/repo")
        monkeypatch.setenv("GITHUB_BASE_BRANCH", "develop")
        client = GitHubClient()
        assert client.token == "ghp_abc"
        assert client.repo == "org/repo"
        assert client.base_branch == "develop"

    def test_default_base_branch_is_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
        monkeypatch.setenv("GITHUB_REPO", "org/repo")
        monkeypatch.delenv("GITHUB_BASE_BRANCH", raising=False)
        client = GitHubClient()
        assert client.base_branch == "main"

    def test_raises_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(KeyError):
            GitHubClient()


class TestCreateFixPr:
    def test_creates_branch_commits_files_opens_pr(
        self, gh: GitHubClient, mock_github_request: MagicMock
    ) -> None:
        analysis = {
            "root_cause": "OOMKill in checkout pod",
            "severity": "high",
            "pr_files": [
                {
                    "path": "k8s/checkout/deployment.yaml",
                    "content": "# fix",
                    "description": "Increase memory",
                }
            ],
            "runbook_steps": ["Check logs", "Apply fix"],
        }
        incident = {"resource_name": "checkout", "namespace": "api"}

        pr_url = gh.create_fix_pr("abc123", analysis, incident)
        assert pr_url == "https://github.com/testorg/test-repo/pull/42"

    def test_commit_message_includes_description(self, gh: GitHubClient) -> None:
        calls: list[tuple[str, str, Any]] = []

        def _fake_request(method: str, path: str, payload: dict | None = None) -> Any:
            calls.append((method, path, payload))
            if "/git/ref/" in path:
                return {"object": {"sha": "sha_base"}}
            if "/git/refs" in path:
                return {}
            if "/contents/" in path and method == "GET":
                raise urllib.error.HTTPError(url=path, code=404, msg="Not Found", hdrs={}, fp=None)  # type: ignore[arg-type]
            if "/contents/" in path and method == "PUT":
                return {"content": {"sha": "sha_new"}}
            if "/pulls" in path:
                return {"html_url": "https://github.com/testorg/test-repo/pull/99"}
            return {}

        with patch.object(gh, "_request", side_effect=_fake_request):
            gh.create_fix_pr(
                "xyz789",
                {
                    "root_cause": "OOMKill",
                    "pr_files": [
                        {"path": "fix.yaml", "content": "# fix", "description": "mem fix"}
                    ],
                    "runbook_steps": [],
                },
                {"resource_name": "svc", "namespace": "ns"},
            )

        # Find the PUT /contents/ call
        put_calls = [(m, p, pay) for m, p, pay in calls if m == "PUT" and "/contents/" in p]
        assert len(put_calls) == 1
        assert "mem fix" in put_calls[0][2]["message"]


class TestGetBranchSha:
    def test_returns_sha(self, gh: GitHubClient) -> None:
        with patch.object(gh, "_request", return_value={"object": {"sha": "abc123"}}):
            sha = gh._get_branch_sha("main")
        assert sha == "abc123"


class TestCreateBranch:
    def test_calls_correct_endpoint(self, gh: GitHubClient) -> None:
        with patch.object(gh, "_request") as mock_req:
            gh._create_branch("sre/auto-fix-abc", "sha_base")
        mock_req.assert_called_once_with(
            "POST",
            f"/repos/{gh.repo}/git/refs",
            {"ref": "refs/heads/sre/auto-fix-abc", "sha": "sha_base"},
        )


class TestUpsertFile:
    def test_creates_new_file_without_sha(self, gh: GitHubClient) -> None:
        calls: list[tuple[str, str, Any]] = []

        def _fake(method: str, path: str, payload: dict | None = None) -> Any:
            calls.append((method, path, payload))
            if method == "GET":
                raise urllib.error.HTTPError(url=path, code=404, msg="Not Found", hdrs={}, fp=None)  # type: ignore[arg-type]
            return {"content": {"sha": "newsha"}}

        with patch.object(gh, "_request", side_effect=_fake):
            gh._upsert_file("sre/fix", "k8s/fix.yaml", "# content", "add fix")

        put_call = next(c for c in calls if c[0] == "PUT")
        assert "sha" not in put_call[2]
        assert put_call[2]["content"] == base64.b64encode(b"# content").decode()

    def test_updates_existing_file_with_sha(self, gh: GitHubClient) -> None:
        calls: list[tuple[str, str, Any]] = []

        def _fake(method: str, path: str, payload: dict | None = None) -> Any:
            calls.append((method, path, payload))
            if method == "GET":
                return {"sha": "existing_sha"}
            return {"content": {"sha": "newsha"}}

        with patch.object(gh, "_request", side_effect=_fake):
            gh._upsert_file("sre/fix", "k8s/fix.yaml", "# updated", "update fix")

        put_call = next(c for c in calls if c[0] == "PUT")
        assert put_call[2]["sha"] == "existing_sha"

    def test_raises_on_non_404_http_error(self, gh: GitHubClient) -> None:
        def _fake(method: str, path: str, payload: dict | None = None) -> Any:
            if method == "GET":
                raise urllib.error.HTTPError(
                    url=path, code=500, msg="Server Error", hdrs={}, fp=None
                )  # type: ignore[arg-type]
            return {}

        with patch.object(gh, "_request", side_effect=_fake):
            with pytest.raises(urllib.error.HTTPError):
                gh._upsert_file("branch", "path.yaml", "content", "msg")


class TestCreatePr:
    def test_returns_pr_data(self, gh: GitHubClient) -> None:
        with patch.object(
            gh, "_request", return_value={"html_url": "https://github.com/org/repo/pull/5"}
        ):
            result = gh._create_pr("sre/fix", "SRE Fix", "# Body")
        assert result["html_url"] == "https://github.com/org/repo/pull/5"


class TestBuildPrBody:
    def test_contains_severity_and_root_cause(self) -> None:
        analysis = {
            "severity": "high",
            "root_cause": "OOMKill",
            "fix_description": "Increase memory",
            "runbook_steps": ["Step 1", "Step 2"],
            "pr_files": [{"path": "k8s/fix.yaml", "description": "mem fix"}],
        }
        incident = {"resource_name": "checkout", "namespace": "api"}
        body = GitHubClient._build_pr_body("abc123", analysis, incident)

        assert "OOMKill" in body
        assert "high" in body
        assert "Step 1" in body
        assert "abc123" in body
        assert "checkout" in body
        assert "⚠️" in body or "warning" in body.lower() or "auto" in body.lower()

    def test_severity_emoji_mapping(self) -> None:
        for severity, emoji in [
            ("critical", "🔴"),
            ("high", "🟠"),
            ("medium", "🟡"),
            ("low", "🟢"),
        ]:
            body = GitHubClient._build_pr_body(
                "id1",
                {
                    "severity": severity,
                    "root_cause": "RC",
                    "fix_description": "FD",
                    "runbook_steps": [],
                    "pr_files": [],
                },
                {"resource_name": "svc", "namespace": "ns"},
            )
            assert emoji in body

    def test_unknown_severity_uses_fallback_emoji(self) -> None:
        body = GitHubClient._build_pr_body(
            "id1",
            {
                "severity": "unknown",
                "root_cause": "RC",
                "fix_description": "FD",
                "runbook_steps": [],
                "pr_files": [],
            },
            {"resource_name": "svc", "namespace": "ns"},
        )
        assert "⚪" in body
