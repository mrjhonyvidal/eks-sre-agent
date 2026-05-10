"""Unit tests for sre_agent/llm_client.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sre_agent.llm_client import (
    AnthropicClient,
    BedrockClient,
    ContentBlock,
    MessageResponse,
    get_llm_client,
)


class TestContentBlock:
    def test_defaults(self) -> None:
        b = ContentBlock(type="text")
        assert b.text == ""
        assert b.tool_use_id == ""
        assert b.name == ""
        assert b.input == {}

    def test_tool_use_block(self) -> None:
        b = ContentBlock(
            type="tool_use", tool_use_id="id1", name="get_pod_logs", input={"ns": "api"}
        )
        assert b.type == "tool_use"
        assert b.tool_use_id == "id1"
        assert b.input["ns"] == "api"


class TestMessageResponse:
    def test_defaults(self) -> None:
        r = MessageResponse(content=[], stop_reason="end_turn", model="test")
        assert r.usage_input_tokens == 0
        assert r.usage_output_tokens == 0


class TestGetLlmClient:
    def test_returns_anthropic_client_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("anthropic.Anthropic"):
            client = get_llm_client()
        assert isinstance(client, AnthropicClient)

    def test_returns_bedrock_client_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "bedrock")
        with patch("boto3.client"):
            client = get_llm_client()
        assert isinstance(client, BedrockClient)

    def test_raises_on_unknown_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
            get_llm_client()

    def test_case_insensitive_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "BEDROCK")
        with patch("boto3.client"):
            client = get_llm_client()
        assert isinstance(client, BedrockClient)


class TestAnthropicClient:
    def test_raises_when_api_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
            AnthropicClient()

    def test_model_id_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("anthropic.Anthropic"):
            client = AnthropicClient()
        assert client.model_id.startswith("anthropic/")

    def test_custom_model_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
        with patch("anthropic.Anthropic"):
            client = AnthropicClient()
        assert "haiku" in client.model_id

    def test_create_message_normalises_text_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = "Hello"
        mock_resp = MagicMock()
        mock_resp.content = [mock_block]
        mock_resp.stop_reason = "end_turn"
        mock_resp.model = "claude-test"
        mock_resp.usage.input_tokens = 10
        mock_resp.usage.output_tokens = 5

        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.messages.create.return_value = mock_resp

        with patch("anthropic.Anthropic", return_value=mock_anthropic_instance):
            client = AnthropicClient()
            result = client.create_message(
                system="sys",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
            )

        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello"
        assert result.stop_reason == "end_turn"
        assert result.usage_input_tokens == 10

    def test_create_message_normalises_tool_use_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        mock_block = MagicMock()
        mock_block.type = "tool_use"
        mock_block.id = "tu_123"
        mock_block.name = "get_pod_logs"
        mock_block.input = {"namespace": "api", "pod_name": "checkout-abc"}
        mock_resp = MagicMock()
        mock_resp.content = [mock_block]
        mock_resp.stop_reason = "tool_use"
        mock_resp.model = "claude-test"
        mock_resp.usage.input_tokens = 20
        mock_resp.usage.output_tokens = 10

        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.messages.create.return_value = mock_resp

        with patch("anthropic.Anthropic", return_value=mock_anthropic_instance):
            client = AnthropicClient()
            result = client.create_message(
                system="sys",
                messages=[{"role": "user", "content": "investigate"}],
                tools=[],
            )

        assert len(result.content) == 1
        assert result.content[0].type == "tool_use"
        assert result.content[0].tool_use_id == "tu_123"
        assert result.content[0].name == "get_pod_logs"


class TestBedrockClient:
    def test_model_id_format(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        assert client.model_id.startswith("bedrock/")

    def test_custom_model_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
        with patch("boto3.client"):
            client = BedrockClient()
        assert "nova" in client.model_id

    def test_convert_tools(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        tools = [
            {
                "name": "get_pod_logs",
                "description": "Fetch pod logs",
                "input_schema": {"type": "object", "properties": {"pod_name": {"type": "string"}}},
            }
        ]
        result = client._convert_tools(tools)
        assert result[0]["toolSpec"]["name"] == "get_pod_logs"
        assert result[0]["toolSpec"]["description"] == "Fetch pod logs"
        assert "json" in result[0]["toolSpec"]["inputSchema"]

    def test_convert_messages_string_content(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        messages = [{"role": "user", "content": "hello"}]
        result = client._convert_messages(messages)
        assert result[0]["content"][0]["text"] == "hello"

    def test_convert_messages_list_content_text(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        messages = [{"role": "user", "content": [{"type": "text", "text": "analyse this"}]}]
        result = client._convert_messages(messages)
        assert result[0]["content"][0]["text"] == "analyse this"

    def test_convert_messages_tool_use_block(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_001",
                        "name": "get_pod_logs",
                        "input": {"namespace": "api", "pod_name": "pod-abc"},
                    }
                ],
            }
        ]
        result = client._convert_messages(messages)
        assert "toolUse" in result[0]["content"][0]
        assert result[0]["content"][0]["toolUse"]["toolUseId"] == "tu_001"

    def test_convert_messages_tool_result_block(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_001",
                        "content": '{"logs": ["error: oom"]}',
                    }
                ],
            }
        ]
        result = client._convert_messages(messages)
        assert "toolResult" in result[0]["content"][0]
        assert result[0]["content"][0]["toolResult"]["toolUseId"] == "tu_001"

    def test_parse_response_text_block(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        raw_resp = {
            "output": {"message": {"content": [{"text": "The root cause is..."}]}},
            "stopReason": "end_turn",
            "metrics": {},
            "usage": {"inputTokens": 50, "outputTokens": 100},
        }
        result = client._parse_response(raw_resp)
        assert result.content[0].type == "text"
        assert result.content[0].text == "The root cause is..."
        assert result.stop_reason == "end_turn"
        assert result.usage_input_tokens == 50

    def test_parse_response_tool_use_block(self) -> None:
        with patch("boto3.client"):
            client = BedrockClient()
        raw_resp = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tu_002",
                                "name": "describe_k8s_resource",
                                "input": {"resource_type": "pod", "resource_name": "my-pod"},
                            }
                        }
                    ]
                }
            },
            "stopReason": "tool_use",
            "metrics": {},
            "usage": {"inputTokens": 80, "outputTokens": 20},
        }
        result = client._parse_response(raw_resp)
        assert result.content[0].type == "tool_use"
        assert result.content[0].name == "describe_k8s_resource"

    def test_parse_response_json_string_input(self) -> None:
        """Bedrock sometimes returns tool input as a JSON string."""
        with patch("boto3.client"):
            client = BedrockClient()
        raw_resp = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tu_003",
                                "name": "get_pod_logs",
                                "input": json.dumps({"namespace": "api", "pod_name": "my-pod"}),
                            }
                        }
                    ]
                }
            },
            "stopReason": "tool_use",
            "metrics": {},
            "usage": {},
        }
        result = client._parse_response(raw_resp)
        assert result.content[0].input["namespace"] == "api"

    def test_create_message_calls_converse_api(self) -> None:
        mock_bedrock_client = MagicMock()
        mock_bedrock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "response"}]}},
            "stopReason": "end_turn",
            "metrics": {},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        with patch("boto3.client", return_value=mock_bedrock_client):
            client = BedrockClient()
            result = client.create_message(
                system="You are an SRE",
                messages=[{"role": "user", "content": "investigate"}],
                tools=[],
            )

        mock_bedrock_client.converse.assert_called_once()
        assert result.content[0].text == "response"
