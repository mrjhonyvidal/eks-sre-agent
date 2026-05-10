"""Unit tests for sre_agent/handler.py."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch


class TestHandlerMainFlow:
    def _make_event(self) -> dict[str, Any]:
        return {
            "source": "aws.cloudwatch",
            "detail": {
                "alarmName": "sre-prod-api-checkout-error-rate-high",
                "state": {"value": "ALARM", "reason": "Error rate high"},
                "previousState": {"value": "OK"},
                "configuration": {},
            },
        }

    def test_happy_path_returns_200(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "sre-prod-api-checkout-error-rate-high",
                "namespace": "api",
                "resource_type": "deployment",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "1234567890.123456"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.return_value = {
                "root_cause": "OOMKill",
                "severity": "low",
                "fix_type": "manual",
                "fix_description": "Check memory",
                "pr_files": [],
                "runbook_steps": ["Step 1"],
            }
            mock_agent_cls.return_value = mock_agent

            mock_gh_cls.return_value = MagicMock()

            from sre_agent.handler import handler

            result = handler(self._make_event(), None)

        assert result["statusCode"] == 200
        assert result["body"] == "ok"

    def test_duplicate_incident_returns_early(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler._is_duplicate", return_value=True),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }

            from sre_agent.handler import handler

            result = handler(self._make_event(), None)

        assert result["statusCode"] == 200
        assert result["body"] == "duplicate"

    def test_agent_failure_returns_500(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient"),
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "ts123"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.side_effect = RuntimeError("Claude API timeout")
            mock_agent_cls.return_value = mock_agent

            from sre_agent.handler import handler

            result = handler(self._make_event(), None)

        assert result["statusCode"] == 500
        assert "Claude API timeout" in result["body"]

    def test_auto_pr_created_for_high_severity_auto_fix(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "ts_high"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.return_value = {
                "root_cause": "OOMKill",
                "severity": "high",
                "fix_type": "auto",
                "fix_description": "Increase memory",
                "pr_files": [{"path": "k8s/fix.yaml", "content": "...", "description": "fix"}],
                "runbook_steps": [],
            }
            mock_agent_cls.return_value = mock_agent

            mock_gh = MagicMock()
            mock_gh.create_fix_pr.return_value = "https://github.com/org/repo/pull/1"
            mock_gh_cls.return_value = mock_gh

            from sre_agent.handler import handler

            result = handler(self._make_event(), None)

        assert result["statusCode"] == 200
        mock_gh.create_fix_pr.assert_called_once()

    def test_no_pr_for_manual_fix_type(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "ts_manual"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.return_value = {
                "root_cause": "Unknown",
                "severity": "high",
                "fix_type": "manual",
                "fix_description": "Investigate",
                "pr_files": [],
                "runbook_steps": [],
            }
            mock_agent_cls.return_value = mock_agent

            mock_gh = MagicMock()
            mock_gh_cls.return_value = mock_gh

            from sre_agent.handler import handler

            handler(self._make_event(), None)

        mock_gh.create_fix_pr.assert_not_called()

    def test_no_pr_for_low_severity_auto_fix(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "ts_low"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.return_value = {
                "root_cause": "Minor issue",
                "severity": "low",
                "fix_type": "auto",
                "fix_description": "Small fix",
                "pr_files": [{"path": "k8s/fix.yaml", "content": "fix", "description": "x"}],
                "runbook_steps": [],
            }
            mock_agent_cls.return_value = mock_agent

            mock_gh = MagicMock()
            mock_gh_cls.return_value = mock_gh

            from sre_agent.handler import handler

            handler(self._make_event(), None)

        mock_gh.create_fix_pr.assert_not_called()

    def test_pr_failure_does_not_abort_handler(self) -> None:
        with (
            patch("sre_agent.handler.enrich_event") as mock_enrich,
            patch("sre_agent.handler.SREAgent") as mock_agent_cls,
            patch("sre_agent.handler.SlackClient") as mock_slack_cls,
            patch("sre_agent.handler.GitHubClient") as mock_gh_cls,
            patch("sre_agent.handler._is_duplicate", return_value=False),
            patch("sre_agent.handler.incident_table"),
        ):
            mock_enrich.return_value = {
                "source": "cloudwatch_alarm",
                "cluster_name": "prod",
                "alarm_name": "test",
                "namespace": "api",
                "resource_name": "checkout",
            }
            mock_slack = MagicMock()
            mock_slack.post_investigating.return_value = "ts_pr_fail"
            mock_slack_cls.return_value = mock_slack

            mock_agent = MagicMock()
            mock_agent.analyze.return_value = {
                "root_cause": "OOMKill",
                "severity": "critical",
                "fix_type": "auto",
                "fix_description": "Increase memory",
                "pr_files": [{"path": "x.yaml", "content": "y", "description": "z"}],
                "runbook_steps": [],
            }
            mock_agent_cls.return_value = mock_agent

            mock_gh = MagicMock()
            mock_gh.create_fix_pr.side_effect = RuntimeError("GitHub API rate limit")
            mock_gh_cls.return_value = mock_gh

            from sre_agent.handler import handler

            result = handler(self._make_event(), None)

        assert result["statusCode"] == 200  # should still succeed


class TestIncidentId:
    def test_stable_hash_for_same_inputs(self) -> None:
        from sre_agent.handler import _incident_id

        incident = {
            "source": "cloudwatch_alarm",
            "cluster_name": "prod",
            "namespace": "api",
            "resource_name": "checkout",
            "alarm_name": "error-rate-high",
        }
        assert _incident_id(incident) == _incident_id(incident)

    def test_different_inputs_produce_different_ids(self) -> None:
        from sre_agent.handler import _incident_id

        incident1 = {
            "source": "a",
            "cluster_name": "prod",
            "namespace": "api",
            "resource_name": "svc1",
            "alarm_name": "x",
        }
        incident2 = {
            "source": "a",
            "cluster_name": "prod",
            "namespace": "api",
            "resource_name": "svc2",
            "alarm_name": "x",
        }
        assert _incident_id(incident1) != _incident_id(incident2)

    def test_id_is_16_chars(self) -> None:
        from sre_agent.handler import _incident_id

        incident = {
            "source": "s",
            "cluster_name": "c",
            "namespace": "n",
            "resource_name": "r",
            "alarm_name": "a",
        }
        assert len(_incident_id(incident)) == 16


class TestIsDuplicate:
    def test_fresh_incident_is_not_duplicate(self) -> None:
        with patch("sre_agent.handler.incident_table") as mock_table:
            mock_table.get_item.return_value = {"Item": None}
            from sre_agent.handler import _is_duplicate

            assert not _is_duplicate("new_id")

    def test_recent_incident_is_duplicate(self) -> None:
        recent_ts = datetime.now(UTC).isoformat()
        with patch("sre_agent.handler.incident_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {"incident_id": "abc", "created_at": recent_ts}
            }
            from sre_agent.handler import _is_duplicate

            assert _is_duplicate("abc")

    def test_old_incident_is_not_duplicate(self) -> None:
        old_ts = datetime.fromtimestamp(time.time() - 7200, tz=UTC).isoformat()
        with patch("sre_agent.handler.incident_table") as mock_table:
            mock_table.get_item.return_value = {
                "Item": {"incident_id": "old", "created_at": old_ts}
            }
            from sre_agent.handler import _is_duplicate

            assert not _is_duplicate("old", window_seconds=3600)

    def test_dynamodb_error_is_not_duplicate(self) -> None:
        with patch("sre_agent.handler.incident_table") as mock_table:
            mock_table.get_item.side_effect = Exception("DynamoDB error")
            from sre_agent.handler import _is_duplicate

            assert not _is_duplicate("error_id")
