"""
llm_client.py — Multi-LLM abstraction layer.

Supports:
  - Anthropic Claude  (LLM_PROVIDER=anthropic, default)
  - Amazon Bedrock    (LLM_PROVIDER=bedrock)

Bedrock is the AWS-native choice: it uses your existing IAM role,
has no extra API key to manage, and supports both Claude models via
Cross-Region Inference and AWS-native models (Titan, Nova, Llama).

Cost note:
  - Bedrock charges per token with no markup; no monthly seat fee.
  - On-Demand pricing is the default; use Provisioned Throughput for
    predictable high-volume workloads.
  - Bedrock model IDs use the format:
      us.anthropic.claude-sonnet-4-5-20250514-v1:0   (cross-region)
      anthropic.claude-3-5-sonnet-20240620-v1:0       (single-region)

Usage:
    # Auto-selects based on LLM_PROVIDER env var
    client = get_llm_client()
    response = client.create_message(
        system="You are an SRE agent.",
        messages=[{"role": "user", "content": "Analyse this incident."}],
        tools=[...],
        max_tokens=4096,
    )
    # response is a normalised MessageResponse regardless of backend
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared data types (backend-agnostic)
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    """A single content block in a model response."""

    type: str  # "text" | "tool_use"
    text: str = ""
    tool_use_id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class MessageResponse:
    """Normalised response from any LLM backend."""

    content: list[ContentBlock]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    model: str
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseLLMClient(ABC):
    """Common interface for all LLM providers."""

    @abstractmethod
    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> MessageResponse:
        """Send a messages-API request and return a normalised response."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Human-readable model identifier for logging."""


# ---------------------------------------------------------------------------
# Anthropic Claude (direct API)
# ---------------------------------------------------------------------------


class AnthropicClient(BaseLLMClient):
    """
    Uses the Anthropic Python SDK.

    Required env vars:
      ANTHROPIC_API_KEY  — sk-ant-… API key from console.anthropic.com
      ANTHROPIC_MODEL    — default: claude-sonnet-4-20250514
    """

    DEFAULT_MODEL = "claude-sonnet-4-20250514"

    def __init__(self) -> None:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            msg = "ANTHROPIC_API_KEY environment variable is required when LLM_PROVIDER=anthropic"
            raise OSError(msg)
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = os.environ.get("ANTHROPIC_MODEL", self.DEFAULT_MODEL)
        logger.info("AnthropicClient initialised with model=%s", self._model)

    @property
    def model_id(self) -> str:
        return f"anthropic/{self._model}"

    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> MessageResponse:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,  # type: ignore[arg-type]
            messages=messages,
        )

        blocks: list[ContentBlock] = []
        for block in resp.content:
            if block.type == "text":
                blocks.append(ContentBlock(type="text", text=block.text))
            elif block.type == "tool_use":
                blocks.append(
                    ContentBlock(
                        type="tool_use",
                        tool_use_id=block.id,
                        name=block.name,
                        input=block.input,
                    )
                )

        return MessageResponse(
            content=blocks,
            stop_reason=resp.stop_reason or "end_turn",
            model=resp.model,
            usage_input_tokens=resp.usage.input_tokens,
            usage_output_tokens=resp.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Amazon Bedrock (AWS-native, IAM-authenticated)
# ---------------------------------------------------------------------------


class BedrockClient(BaseLLMClient):
    """
    Uses the AWS Bedrock Converse API via boto3.

    No extra API key needed — authentication uses the Lambda execution role
    (or your local ~/.aws/credentials for local testing).

    Recommended model IDs (cost-efficient, 2025):
      us.anthropic.claude-3-5-haiku-20241022-v1:0        — fastest/cheapest Claude
      us.anthropic.claude-sonnet-4-5-20250514-v1:0       — best balance
      us.amazon.nova-pro-v1:0                            — AWS-native, cost-efficient
      us.amazon.nova-lite-v1:0                           — cheapest, great for simple tasks

    Required env vars:
      BEDROCK_MODEL_ID   — default: us.anthropic.claude-sonnet-4-5-20250514-v1:0
      AWS_REGION         — default: us-east-1
    """

    DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250514-v1:0"

    def __init__(self) -> None:
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        self._model = os.environ.get("BEDROCK_MODEL_ID", self.DEFAULT_MODEL)
        self._client = boto3.client("bedrock-runtime", region_name=region)
        logger.info("BedrockClient initialised with model=%s region=%s", self._model, region)

    @property
    def model_id(self) -> str:
        return f"bedrock/{self._model}"

    def create_message(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> MessageResponse:
        """
        Translates Anthropic-style messages/tools to the Bedrock Converse API
        and normalises the response back to MessageResponse.
        """
        # Convert Anthropic message format → Bedrock Converse format
        bedrock_messages = self._convert_messages(messages)
        bedrock_tools = self._convert_tools(tools)

        resp = self._client.converse(
            modelId=self._model,
            system=[{"text": system}],
            messages=bedrock_messages,
            toolConfig={"tools": bedrock_tools} if bedrock_tools else {},
            inferenceConfig={
                "maxTokens": max_tokens,
                "temperature": 1.0,
            },
        )

        return self._parse_response(resp)

    # ------------------------------------------------------------------
    # Private conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Anthropic-style message list to Bedrock Converse format."""
        converted = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                converted.append({"role": role, "content": [{"text": content}]})
            elif isinstance(content, list):
                bedrock_content = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            bedrock_content.append({"text": block["text"]})
                        elif block.get("type") == "tool_use":
                            bedrock_content.append(
                                {
                                    "toolUse": {
                                        "toolUseId": block["id"],
                                        "name": block["name"],
                                        "input": block["input"],
                                    }
                                }
                            )
                        elif block.get("type") == "tool_result":
                            bedrock_content.append(
                                {
                                    "toolResult": {
                                        "toolUseId": block["tool_use_id"],
                                        "content": [{"text": block.get("content", "")}],
                                    }
                                }
                            )
                    else:
                        # SDK objects (AnthropicTextBlock, etc.) — convert via dict
                        obj = block
                        if hasattr(obj, "type"):
                            if obj.type == "text":
                                bedrock_content.append({"text": obj.text})
                            elif obj.type == "tool_use":
                                bedrock_content.append(
                                    {
                                        "toolUse": {
                                            "toolUseId": obj.id,
                                            "name": obj.name,
                                            "input": obj.input,
                                        }
                                    }
                                )
                converted.append({"role": role, "content": bedrock_content})
            else:
                logger.warning("Unexpected message content type: %s", type(content))
        return converted

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Anthropic tool format to Bedrock ToolSpec format."""
        return [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": {"json": t.get("input_schema", {})},
                }
            }
            for t in tools
        ]

    def _parse_response(self, resp: dict[str, Any]) -> MessageResponse:
        """Parse Bedrock Converse response into a normalised MessageResponse."""
        output = resp.get("output", {})
        message = output.get("message", {})
        stop_reason = resp.get("stopReason", "end_turn")
        model_id = resp.get("metrics", {}).get("latencyMs", self._model)
        usage = resp.get("usage", {})

        blocks: list[ContentBlock] = []
        for item in message.get("content", []):
            if "text" in item:
                blocks.append(ContentBlock(type="text", text=item["text"]))
            elif "toolUse" in item:
                tu = item["toolUse"]
                tool_input = tu.get("input", {})
                # Bedrock may return input as a JSON string
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        tool_input = {}
                blocks.append(
                    ContentBlock(
                        type="tool_use",
                        tool_use_id=tu.get("toolUseId", ""),
                        name=tu.get("name", ""),
                        input=tool_input,
                    )
                )

        # Normalise stop reason to Anthropic conventions
        stop_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }
        normalised_stop = stop_map.get(stop_reason, "end_turn")

        return MessageResponse(
            content=blocks,
            stop_reason=normalised_stop,
            model=str(model_id),
            usage_input_tokens=usage.get("inputTokens", 0),
            usage_output_tokens=usage.get("outputTokens", 0),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_llm_client() -> BaseLLMClient:
    """
    Return the appropriate LLM client based on LLM_PROVIDER env var.

    Values:
      "anthropic" (default) — Anthropic Claude direct API
      "bedrock"             — Amazon Bedrock (Claude / Nova, IAM-auth)

    Amazon Bedrock is the recommended choice for AWS-native deployments:
      • No additional API keys — uses the Lambda IAM execution role
      • Supports Claude + AWS-native models (Nova, Titan)
      • Cross-Region Inference for higher availability
      • Single AWS bill
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower().strip()
    logger.info("LLM_PROVIDER=%s", provider)

    if provider == "bedrock":
        return BedrockClient()
    if provider == "anthropic":
        return AnthropicClient()

    msg = f"Unknown LLM_PROVIDER '{provider}'. Choose 'anthropic' or 'bedrock'."
    raise ValueError(msg)
