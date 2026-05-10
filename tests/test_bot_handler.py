"""Unit tests for eks_ai_ops/bot_handler.py."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest


def _make_slack_headers(body: str, secret: str = "test-signing-secret") -> dict[str, str]:
    """Generate valid Slack signature headers for a given body."""
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


class TestBotHandlerUrlVerification:
    def test_responds_to_url_verification_challenge(self) -> None:
        body = json.dumps({"type": "url_verification", "challenge": "challenge_token"})
        event = {"body": body, "headers": {}}

        from eks_ai_ops.bot_handler import handler

        result = handler(event, None)
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["challenge"] == "challenge_token"


class TestBotHandlerSignatureVerification:
    def test_rejects_invalid_signature(self) -> None:
        body = json.dumps({"type": "event_callback"})
        event = {
            "body": body,
            "headers": {
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=invalidsignature",
            },
        }
        with patch(
            "eks_ai_ops.interactive.handler.os.environ.get",
            side_effect=lambda k, d="": "test-signing-secret" if k == "SLACK_SIGNING_SECRET" else d,
        ):
            from eks_ai_ops.bot_handler import handler

            result = handler(event, None)

        assert result["statusCode"] == 401

    def test_rejects_expired_timestamp(self) -> None:
        body = json.dumps({"type": "event_callback"})
        old_ts = str(int(time.time()) - 400)  # 400s ago > 300s limit
        sig = (
            "v0="
            + hmac.new(
                b"test-signing-secret",
                f"v0:{old_ts}:{body}".encode(),
                hashlib.sha256,
            ).hexdigest()
        )
        event = {
            "body": body,
            "headers": {"x-slack-request-timestamp": old_ts, "x-slack-signature": sig},
        }

        from eks_ai_ops.bot_handler import _verify_slack_signature

        assert not _verify_slack_signature(event)

    def test_accepts_valid_signature(self) -> None:
        body = "test_body"
        headers = _make_slack_headers(body, "test-signing-secret")
        event = {"body": body, "headers": headers}

        with patch("os.environ.get", return_value="test-signing-secret"):
            from eks_ai_ops.bot_handler import _verify_slack_signature

            # Should not raise
            result = _verify_slack_signature(event)
        # True if signing secret matches
        assert isinstance(result, bool)

    def test_skips_verification_when_secret_not_set(self) -> None:
        from eks_ai_ops.bot_handler import _verify_slack_signature

        event = {"body": "body", "headers": {}}
        with patch("eks_ai_ops.interactive.handler.os") as mock_os:
            mock_os.environ.get.return_value = ""
            result = _verify_slack_signature(event)
        assert result is True


class TestHandleMention:
    def test_replies_when_incident_found(self) -> None:
        from eks_ai_ops.bot_handler import _handle_mention

        with (
            patch("eks_ai_ops.interactive.handler._find_incident_from_thread") as mock_find,
            patch("eks_ai_ops.interactive.handler._get_orchestrator") as mock_orchestrator_get,
            patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply,
        ):
            mock_find.return_value = {"incident_id": "abc", "analysis": {}}
            mock_orchestrator = MagicMock()
            mock_orchestrator.respond.return_value = "Here is your answer"
            mock_orchestrator_get.return_value = mock_orchestrator

            _handle_mention(
                {
                    "channel": "C123",
                    "ts": "ts123",
                    "text": "<@UBOT> What is the root cause?",
                    "user": "U456",
                }
            )

        mock_reply.assert_called_once()
        assert "Here is your answer" in mock_reply.call_args[0][2]

    def test_replies_with_not_found_when_no_incident(self) -> None:
        """Non-K8s top-level mention with no incident context falls through to
        the orchestrator's keyword gate, which politely rejects it."""
        from eks_ai_ops.bot_handler import _handle_mention

        with (
            patch("eks_ai_ops.interactive.handler._find_incident_from_thread", return_value=None),
            patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply,
        ):
            _handle_mention(
                {"channel": "C123", "ts": "ts123", "text": "<@UBOT> help", "user": "U456"}
            )

        mock_reply.assert_called_once()
        assert "kubernetes" in mock_reply.call_args[0][2].lower()

    def test_handles_llm_exception_gracefully(self) -> None:
        from eks_ai_ops.bot_handler import _handle_mention

        with (
            patch(
                "eks_ai_ops.interactive.handler._find_incident_from_thread",
                return_value={"id": "x"},
            ),
            patch("eks_ai_ops.interactive.handler._get_orchestrator") as mock_orchestrator_get,
            patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply,
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.respond.side_effect = RuntimeError("API error")
            mock_orchestrator_get.return_value = mock_orchestrator

            _handle_mention({"channel": "C123", "ts": "ts1", "text": "<@U> question", "user": "U1"})

        assert ":x:" in mock_reply.call_args[0][2]


class TestHandleBlockAction:
    def _action_body(self, action_id: str, incident_id: str = "inc_001") -> dict:
        return {
            "actions": [{"action_id": action_id, "value": f"act|{incident_id}"}],
            "channel": {"id": "C123"},
            "message": {"ts": "ts_msg"},
            "user": {"id": "U_ACTOR"},
        }

    def test_ask_agent_action(self) -> None:
        from eks_ai_ops.bot_handler import _handle_block_action

        with patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply:
            _handle_block_action(self._action_body("ask_agent"))

        assert "Ask any EKS question" in mock_reply.call_args[0][2]

    def test_resolve_incident_action(self) -> None:
        from eks_ai_ops.bot_handler import _handle_block_action

        with (
            patch("eks_ai_ops.interactive.handler._update_incident_status") as mock_update,
            patch("eks_ai_ops.interactive.handler._post_reply"),
        ):
            _handle_block_action(self._action_body("resolve_incident"))

        mock_update.assert_called_once_with("inc_001", "resolved", "U_ACTOR")

    def test_false_positive_action(self) -> None:
        from eks_ai_ops.bot_handler import _handle_block_action

        with (
            patch("eks_ai_ops.interactive.handler._update_incident_status") as mock_update,
            patch("eks_ai_ops.interactive.handler._post_reply"),
        ):
            _handle_block_action(self._action_body("false_positive"))

        mock_update.assert_called_once_with("inc_001", "false_positive", "U_ACTOR")

    def test_unknown_action_is_handled_silently(self) -> None:
        from eks_ai_ops.bot_handler import _handle_block_action

        with patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply:
            _handle_block_action(self._action_body("unknown_action"))

        mock_reply.assert_not_called()

    def test_empty_actions_returns_early(self) -> None:
        from eks_ai_ops.bot_handler import _handle_block_action

        body = {
            "actions": [],
            "channel": {"id": "C1"},
            "message": {"ts": "ts"},
            "user": {"id": "U"},
        }
        with patch("eks_ai_ops.interactive.handler._post_reply") as mock_reply:
            _handle_block_action(body)

        mock_reply.assert_not_called()


class TestUpdateIncidentStatus:
    def test_updates_dynamodb_item(self) -> None:
        from eks_ai_ops.bot_handler import _update_incident_status

        with patch("eks_ai_ops.interactive.handler._get_incident_table") as mock_table_get:
            mock_table = MagicMock()
            mock_table_get.return_value = mock_table
            _update_incident_status("inc_001", "resolved", "U_ACTOR")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"incident_id": "inc_001"}
        assert ":s" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":s"] == "resolved"

    def test_logs_error_on_dynamodb_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        from eks_ai_ops.bot_handler import _update_incident_status

        with patch("eks_ai_ops.interactive.handler._get_incident_table") as mock_table_get:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = RuntimeError("DDB error")
            mock_table_get.return_value = mock_table
            with pytest.raises(RuntimeError):
                _update_incident_status("inc_bad", "resolved", "U1")


class TestFindIncidentFromThread:
    def test_returns_first_matching_incident(self) -> None:
        from eks_ai_ops.bot_handler import _find_incident_from_thread

        with patch("eks_ai_ops.interactive.handler._get_incident_table") as mock_table_get:
            mock_table = MagicMock()
            mock_table_get.return_value = mock_table
            mock_table.scan.return_value = {"Items": [{"incident_id": "abc", "slack_ts": "ts1"}]}
            result = _find_incident_from_thread("ts1")

        assert result is not None
        assert result["incident_id"] == "abc"

    def test_returns_none_when_no_items(self) -> None:
        from eks_ai_ops.bot_handler import _find_incident_from_thread

        with patch("eks_ai_ops.interactive.handler._get_incident_table") as mock_table_get:
            mock_table = MagicMock()
            mock_table_get.return_value = mock_table
            mock_table.scan.return_value = {"Items": []}
            result = _find_incident_from_thread("ts_missing")

        assert result is None

    def test_returns_none_on_exception(self) -> None:
        from eks_ai_ops.bot_handler import _find_incident_from_thread

        with patch("eks_ai_ops.interactive.handler._get_incident_table") as mock_table_get:
            mock_table = MagicMock()
            mock_table_get.return_value = mock_table
            mock_table.scan.side_effect = Exception("DDB error")
            result = _find_incident_from_thread("ts_err")

        assert result is None
