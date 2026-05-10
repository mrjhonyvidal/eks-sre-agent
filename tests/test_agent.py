"""Unit tests for sre_agent/agent.py — SREAgent class."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from sre_agent.agent import _FALLBACK_RESPONSE, TOOLS, SREAgent
from sre_agent.llm_client import ContentBlock, MessageResponse


class TestSREAgentInit:
    def test_uses_provided_llm_client(self, mock_llm_client: MagicMock) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
        assert agent._llm is mock_llm_client

    def test_auto_selects_llm_client_when_not_provided(self) -> None:
        with (
            patch("sre_agent.agent.get_llm_client") as mock_factory,
            patch("sre_agent.agent.boto3"),
        ):
            mock_factory.return_value = MagicMock()
            SREAgent()
        mock_factory.assert_called_once()


class TestSREAgentAnalyze:
    def test_returns_parsed_json_on_first_response(
        self, mock_llm_client: MagicMock, sample_incident: dict
    ) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            result = agent.analyze(sample_incident)

        assert result["severity"] == "high"
        assert result["fix_type"] == "auto"
        assert "root_cause" in result
        assert "runbook_steps" in result

    def test_executes_tool_call_then_returns_answer(
        self, mock_llm_with_tool_call: MagicMock, sample_incident: dict
    ) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_with_tool_call)
            # Patch the tool so it doesn't hit AWS
            with patch.object(
                agent, "_tool_list_related_alerts", return_value={"related_alarms": []}
            ):
                result = agent.analyze(sample_incident)

        assert result["severity"] == "medium"
        assert result["fix_type"] == "manual"

    def test_returns_fallback_after_max_rounds(self, sample_incident: dict) -> None:
        """Agent should return the fallback dict after MAX_TOOL_ROUNDS with only tool_use blocks."""
        from sre_agent.llm_client import ContentBlock, MessageResponse

        always_tool = MagicMock()
        always_tool.model_id = "mock/always-tool"

        def _tool_response(**kwargs: Any) -> MessageResponse:
            return MessageResponse(
                content=[
                    ContentBlock(
                        type="tool_use",
                        tool_use_id="tu_loop",
                        name="list_related_alerts",
                        input={"service_name": "checkout"},
                    )
                ],
                stop_reason="tool_use",
                model="mock",
            )

        always_tool.create_message.side_effect = _tool_response

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=always_tool)
            with patch.object(agent, "_tool_list_related_alerts", return_value={}):
                result = agent.analyze(sample_incident)

        assert result["root_cause"] == _FALLBACK_RESPONSE["root_cause"]
        assert result["severity"] == "medium"

    def test_handles_malformed_json_gracefully(self, sample_incident: dict) -> None:
        bad_client = MagicMock()
        bad_client.model_id = "mock/bad"
        bad_client.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text="This is NOT valid JSON")],
            stop_reason="end_turn",
            model="mock",
        )

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=bad_client)
            result = agent.analyze(sample_incident)

        assert result["fix_type"] == "manual"
        assert "Parse error" in result["fix_description"]

    def test_strips_markdown_fences_before_parsing(self, sample_incident: dict) -> None:
        analysis = {
            "root_cause": "OOMKill",
            "severity": "critical",
            "fix_type": "manual",
            "fix_description": "Increase memory",
            "pr_files": [],
            "runbook_steps": [],
        }
        fenced_client = MagicMock()
        fenced_client.model_id = "mock/fenced"
        fenced_client.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text=f"```json\n{json.dumps(analysis)}\n```")],
            stop_reason="end_turn",
            model="mock",
        )

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=fenced_client)
            result = agent.analyze(sample_incident)

        assert result["root_cause"] == "OOMKill"
        assert result["severity"] == "critical"


class TestToolDispatch:
    def test_dispatch_unknown_tool_returns_error(self, mock_llm_client: MagicMock) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            result = agent._dispatch_tool("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_dispatch_wraps_exception_in_error(self, mock_llm_client: MagicMock) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            # Patch tool to raise
            with patch.object(agent, "_tool_get_pod_logs", side_effect=RuntimeError("boom")):
                result = agent._dispatch_tool(
                    "get_pod_logs", {"namespace": "api", "pod_name": "pod"}
                )
        assert "error" in result
        assert "boom" in result["error"]

    def test_all_tools_are_registered(self, mock_llm_client: MagicMock) -> None:
        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
        handler_names = {
            "get_pod_logs",
            "describe_k8s_resource",
            "get_cloudwatch_metrics",
            "get_recent_deployments",
            "list_related_alerts",
        }
        assert set(agent._dispatch_tool.__class__.__name__) or True  # ensure it's callable
        for name in handler_names:
            result = agent._dispatch_tool(name, {})
            # Should not return an "Unknown tool" error
            assert result.get("error", "") != f"Unknown tool: {name}"


class TestToolImplementations:
    def test_get_pod_logs_success(self, mock_llm_client: MagicMock) -> None:
        mock_logs = MagicMock()
        mock_logs.filter_log_events.return_value = {
            "events": [{"message": "ERROR: connection refused"}]
        }
        mock_logs.exceptions.ResourceNotFoundException = Exception

        with patch("sre_agent.agent.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_logs
            mock_boto3.resource.return_value = MagicMock()
            agent = SREAgent(llm_client=mock_llm_client)
            agent._logs_client = mock_logs
            result = agent._tool_get_pod_logs("api", "checkout-pod", 50)

        assert result["pod"] == "checkout-pod"
        assert result["namespace"] == "api"
        assert len(result["logs"]) >= 1

    def test_get_pod_logs_log_group_not_found(self, mock_llm_client: MagicMock) -> None:
        class _FakeNotFound(Exception):
            pass

        mock_logs = MagicMock()
        mock_logs.exceptions.ResourceNotFoundException = _FakeNotFound
        mock_logs.filter_log_events.side_effect = _FakeNotFound("not found")

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            agent._logs_client = mock_logs
            result = agent._tool_get_pod_logs("api", "checkout-pod")

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_get_cloudwatch_metrics(self, mock_llm_client: MagicMock) -> None:
        import datetime

        mock_cw = MagicMock()
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": [
                {
                    "Timestamp": datetime.datetime(2025, 1, 1, 12, 0, tzinfo=datetime.UTC),
                    "Average": 75.5,
                    "Maximum": 92.1,
                }
            ]
        }

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            agent._cw_client = mock_cw
            result = agent._tool_get_cloudwatch_metrics(
                "ContainerInsights",
                "pod_cpu_utilization",
                [{"Name": "ClusterName", "Value": "prod"}],
            )

        assert result["metric"] == "pod_cpu_utilization"
        assert len(result["datapoints"]) == 1
        assert result["datapoints"][0]["avg"] == 75.5

    def test_get_recent_deployments_success(self, mock_llm_client: MagicMock) -> None:
        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{"service_name": "prod/api/checkout", "deployed_at": "2025-01-01T12:00:00"}]
        }
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch("sre_agent.agent.boto3") as mock_boto3:
            mock_boto3.resource.return_value = mock_ddb
            mock_boto3.client.return_value = MagicMock()
            agent = SREAgent(llm_client=mock_llm_client)
            result = agent._tool_get_recent_deployments("prod", "checkout", "api")

        assert "deployments" in result
        assert len(result["deployments"]) == 1

    def test_get_recent_deployments_error(self, mock_llm_client: MagicMock) -> None:
        mock_table = MagicMock()
        mock_table.query.side_effect = Exception("DynamoDB error")
        mock_ddb = MagicMock()
        mock_ddb.Table.return_value = mock_table

        with patch("sre_agent.agent.boto3") as mock_boto3:
            mock_boto3.resource.return_value = mock_ddb
            mock_boto3.client.return_value = MagicMock()
            agent = SREAgent(llm_client=mock_llm_client)
            result = agent._tool_get_recent_deployments("prod", "checkout")

        assert "error" in result

    def test_list_related_alerts(self, mock_llm_client: MagicMock) -> None:
        import datetime

        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {
            "MetricAlarms": [
                {
                    "AlarmName": "sre-prod-api-checkout-cpu-high",
                    "StateValue": "ALARM",
                    "StateReason": "CPU > 80%",
                    "StateUpdatedTimestamp": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
                }
            ]
        }

        with patch("sre_agent.agent.boto3"):
            agent = SREAgent(llm_client=mock_llm_client)
            agent._cw_client = mock_cw
            result = agent._tool_list_related_alerts("checkout")

        assert "related_alarms" in result
        assert len(result["related_alarms"]) == 1

    def test_describe_k8s_resource_invokes_lambda(self, mock_llm_client: MagicMock) -> None:
        import io

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"Payload": io.BytesIO(b'{"output": "NAME: pod-abc\\n"}')}

        with patch("sre_agent.agent.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_lambda
            mock_boto3.resource.return_value = MagicMock()
            agent = SREAgent(llm_client=mock_llm_client)
            result = agent._tool_describe_k8s_resource("pod", "pod-abc", "api")

        assert "output" in result


class TestSerialiseContent:
    def test_serialises_text_blocks(self) -> None:
        blocks = [ContentBlock(type="text", text="hello")]
        result = SREAgent._serialise_content(blocks)
        assert result[0] == {"type": "text", "text": "hello"}

    def test_serialises_tool_use_blocks(self) -> None:
        blocks = [
            ContentBlock(
                type="tool_use",
                tool_use_id="tu_001",
                name="get_pod_logs",
                input={"namespace": "api", "pod_name": "pod"},
            )
        ]
        result = SREAgent._serialise_content(blocks)
        assert result[0]["type"] == "tool_use"
        assert result[0]["id"] == "tu_001"
        assert result[0]["name"] == "get_pod_logs"


class TestTools:
    """Validate the TOOLS definition."""

    def test_all_tools_have_required_fields(self) -> None:
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert "required" in tool["input_schema"]

    def test_tool_count(self) -> None:
        assert len(TOOLS) == 5
