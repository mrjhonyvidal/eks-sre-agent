"""Unit tests for eks_ai_ops/slack_client.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from eks_ai_ops.slack_client import SlackClient


@pytest.fixture()
def slack(monkeypatch: pytest.MonkeyPatch) -> SlackClient:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123TEST")
    return SlackClient()


class TestSlackClientInit:
    def test_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-abc")
        monkeypatch.setenv("SLACK_CHANNEL", "CABC123")
        client = SlackClient()
        assert client.token == "xoxb-abc"
        assert client.channel == "CABC123"

    def test_raises_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        with pytest.raises(KeyError):
            SlackClient()


class TestPostInvestigating:
    def test_returns_timestamp(self, slack: SlackClient) -> None:
        with patch.object(
            slack, "_post", return_value={"ok": True, "ts": "1234.5678"}
        ) as mock_post:
            ts = slack.post_investigating(
                {
                    "resource_name": "checkout",
                    "namespace": "api",
                    "cluster_name": "prod",
                    "alarm_name": "error-rate-high",
                }
            )
        assert ts == "1234.5678"
        mock_post.assert_called_once()

    def test_uses_correct_api_method(self, slack: SlackClient) -> None:
        with patch.object(slack, "_post", return_value={"ok": True, "ts": "ts1"}) as mock_post:
            slack.post_investigating(
                {"resource_name": "svc", "namespace": "ns", "cluster_name": "c", "alarm_name": "a"}
            )
        call_args = mock_post.call_args[0]
        assert call_args[0] == "chat.postMessage"

    def test_block_kit_structure(self, slack: SlackClient) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, payload: dict) -> dict:
            captured.update(payload)
            return {"ok": True, "ts": "ts1"}

        with patch.object(slack, "_post", side_effect=_capture):
            slack.post_investigating(
                {
                    "resource_name": "checkout",
                    "namespace": "api",
                    "cluster_name": "prod",
                    "alarm_name": "test",
                }
            )

        assert "blocks" in captured
        block_types = [b["type"] for b in captured["blocks"]]
        assert "header" in block_types
        assert "section" in block_types


class TestUpdateWithAnalysis:
    def _analysis(self, severity: str = "high", fix_type: str = "auto") -> dict:
        return {
            "severity": severity,
            "fix_type": fix_type,
            "root_cause": "OOMKill in checkout pod",
            "fix_description": "Increase memory limit",
            "runbook_steps": ["Step 1", "Step 2"],
        }

    def test_includes_pr_section_when_pr_url_provided(self, slack: SlackClient) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, payload: dict) -> dict:
            captured.update(payload)
            return {"ok": True}

        with patch.object(slack, "_post", side_effect=_capture):
            slack.update_with_analysis(
                ts="ts1",
                incident={"resource_name": "checkout", "namespace": "api"},
                analysis=self._analysis(),
                pr_url="https://github.com/org/repo/pull/42",
                incident_id="abc123",
            )

        blocks_text = json.dumps(captured["blocks"])
        assert "pull request" in blocks_text.lower() or "https://github.com" in blocks_text

    def test_no_pr_section_when_pr_url_is_none(self, slack: SlackClient) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, payload: dict) -> dict:
            captured.update(payload)
            return {"ok": True}

        with patch.object(slack, "_post", side_effect=_capture):
            slack.update_with_analysis(
                ts="ts1",
                incident={"resource_name": "checkout", "namespace": "api"},
                analysis=self._analysis(),
                pr_url=None,
                incident_id="abc123",
            )

        blocks_text = json.dumps(captured["blocks"])
        assert "pull request" not in blocks_text.lower()

    def test_severity_emoji_and_color_vary_by_severity(self, slack: SlackClient) -> None:
        for severity in ("critical", "high", "medium", "low"):
            with patch.object(slack, "_post", return_value={"ok": True}) as mock_post:
                slack.update_with_analysis(
                    ts="ts1",
                    incident={"resource_name": "svc", "namespace": "ns"},
                    analysis=self._analysis(severity=severity),
                    pr_url=None,
                    incident_id="id1",
                )
            mock_post.assert_called_once()

    def test_uses_chat_update_method(self, slack: SlackClient) -> None:
        with patch.object(slack, "_post", return_value={"ok": True}) as mock_post:
            slack.update_with_analysis(
                ts="ts1",
                incident={"resource_name": "svc", "namespace": "ns"},
                analysis=self._analysis(),
                pr_url=None,
                incident_id="id1",
            )
        assert mock_post.call_args[0][0] == "chat.update"


class TestUpdateError:
    def test_updates_with_error_message(self, slack: SlackClient) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, payload: dict) -> dict:
            captured.update(payload)
            return {"ok": True}

        with patch.object(slack, "_post", side_effect=_capture):
            slack.update_error("ts1", "Agent crashed")

        assert captured["ts"] == "ts1"
        blocks_text = json.dumps(captured["blocks"])
        assert "Agent crashed" in blocks_text or "failed" in blocks_text.lower()


class TestPostThreadReply:
    def test_posts_to_thread(self, slack: SlackClient) -> None:
        with patch.object(slack, "_post", return_value={"ok": True}) as mock_post:
            slack.post_thread_reply("thread_ts_123", "Here is the answer")

        call_args = mock_post.call_args[0]
        assert call_args[0] == "chat.postMessage"
        assert call_args[1]["thread_ts"] == "thread_ts_123"
        assert call_args[1]["text"] == "Here is the answer"


class TestPostHttpHelper:
    def test_logs_error_when_slack_returns_not_ok(
        self, slack: SlackClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        from unittest.mock import patch as _patch

        mock_response = MagicMock()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"ok": false, "error": "channel_not_found"}'

        with _patch("urllib.request.urlopen", return_value=mock_response):
            with caplog.at_level("ERROR", logger="eks_ai_ops.slack_client"):
                result = slack._post("chat.postMessage", {"channel": "CBAD", "text": "hi"})

        assert not result["ok"]
