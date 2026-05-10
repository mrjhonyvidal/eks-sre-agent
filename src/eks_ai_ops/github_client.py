"""
GitHub client — creates a fix branch + PR with files from the agent analysis.

Required env vars:
  GITHUB_TOKEN    — fine-grained PAT with Contents + PR write access
  GITHUB_REPO     — e.g. "my-org/k8s-infra"
  GITHUB_BASE_BRANCH  — default "main"
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self) -> None:
        self.token = os.environ["GITHUB_TOKEN"]
        self.repo = os.environ["GITHUB_REPO"]  # org/repo
        self.base_branch = os.environ.get("GITHUB_BASE_BRANCH", "main")

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def create_fix_pr(
        self, incident_id: str, analysis: dict[str, Any], incident: dict[str, Any]
    ) -> str:
        """
        1. Create a new branch sre/auto-fix-{incident_id}
        2. Commit each file from analysis["pr_files"]
        3. Open a PR with a detailed description
        Returns the PR URL.
        """
        branch_name = f"sre/auto-fix-{incident_id}"
        base_sha = self._get_branch_sha(self.base_branch)

        # Create the fix branch
        self._create_branch(branch_name, base_sha)
        logger.info("Created branch %s from %s @ %s", branch_name, self.base_branch, base_sha[:8])

        # Commit each file
        for file_info in analysis.get("pr_files", []):
            self._upsert_file(
                branch=branch_name,
                path=file_info["path"],
                content=file_info["content"],
                message=f"eks-ai-ops-toolkit: {file_info.get('description', 'auto-fix')}",
            )

        # Open the PR
        pr_body = self._build_pr_body(incident_id, analysis, incident)
        pr = self._create_pr(
            head=branch_name,
            title=f"[SRE Auto-fix] {analysis.get('root_cause', incident_id)[:80]}",
            body=pr_body,
        )
        return pr["html_url"]

    # ------------------------------------------------------------------ #
    #  GitHub API wrappers                                                 #
    # ------------------------------------------------------------------ #

    def _get_branch_sha(self, branch: str) -> str:
        data = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    def _create_branch(self, branch: str, sha: str) -> None:
        self._request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def _upsert_file(self, branch: str, path: str, content: str, message: str) -> None:
        """Create or update a file in the repo."""
        encoded = base64.b64encode(content.encode()).decode()
        payload: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        # Check if file already exists (to get its SHA for updates)
        try:
            existing = self._request("GET", f"/repos/{self.repo}/contents/{path}?ref={branch}")
            payload["sha"] = existing["sha"]
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

        self._request("PUT", f"/repos/{self.repo}/contents/{path}", payload)

    def _create_pr(self, head: str, title: str, body: str) -> dict:
        return self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {
                "title": title,
                "body": body,
                "head": head,
                "base": self.base_branch,
                "draft": False,
            },
        )

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = GITHUB_API + path
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    # ------------------------------------------------------------------ #
    #  PR body builder                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_pr_body(incident_id: str, analysis: dict, incident: dict) -> str:
        severity_emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(analysis.get("severity", "medium"), "⚪")

        runbook = "\n".join(f"- [ ] {step}" for step in analysis.get("runbook_steps", []))

        changed_files = "\n".join(
            f"- `{f['path']}` — {f.get('description', '')}" for f in analysis.get("pr_files", [])
        )

        return f"""## {severity_emoji} SRE Auto-fix — `{incident_id}`

**Root cause:** {analysis.get("root_cause", "N/A")}

**Severity:** `{analysis.get("severity", "unknown")}`

**Service:** `{incident.get("resource_name", "unknown")}` in `{incident.get("namespace", "unknown")}`

---

### What this PR changes

{changed_files or "_No file changes._"}

### Fix description

{analysis.get("fix_description", "N/A")}

### Runbook checklist

{runbook or "_No runbook steps._"}

---

> **⚠️ This PR was created automatically by the SRE AI Agent.**
> Please review the diff carefully before merging.
> Incident ID: `{incident_id}` · Generated at: `{datetime.now(UTC).isoformat()}`
"""
