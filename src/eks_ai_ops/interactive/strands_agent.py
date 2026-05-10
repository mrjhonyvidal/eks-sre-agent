from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from eks_ai_ops.interactive.mcp_tools import (
    MCPToolClient,
    default_eks_mcp_tools,
    dispatch_eks_mcp_tool,
)
from eks_ai_ops.llm_client import get_llm_client
from eks_ai_ops.shared.prompts import INTERACTIVE_SYSTEM_PROMPT

_THINKING_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def _sanitize(text: str) -> str:
    """Strip chain-of-thought tags Bedrock models occasionally leak."""
    cleaned = _THINKING_RE.sub("", text or "").strip()
    return cleaned or "(no response)"


logger = logging.getLogger(__name__)


class InteractiveEKSAgent:
    """
    Slack query assistant with a pragmatic backend switch:
    - INTERACTIVE_AGENT_BACKEND=strands (default): uses Strands if installed
    - INTERACTIVE_AGENT_BACKEND=llm: direct tool loop via llm_client
    """

    def __init__(self) -> None:
        self._backend = os.environ.get("INTERACTIVE_AGENT_BACKEND", "strands").strip().lower()
        self._mcp_server = os.environ.get("EKS_MCP_SERVER", "eks")
        self._mcp_client = MCPToolClient()
        self._llm = get_llm_client()
        self._tools = default_eks_mcp_tools(self._mcp_client)

    def answer(self, question: str, incident_context: dict[str, Any]) -> str:
        if self._backend == "strands":
            try:
                return self._answer_with_strands(
                    question=question, incident_context=incident_context
                )
            except Exception:
                logger.exception("Strands backend failed, falling back to llm backend")
        return self._answer_with_llm_loop(question=question, incident_context=incident_context)

    def _answer_with_strands(self, *, question: str, incident_context: dict[str, Any]) -> str:
        """
        Optional Strands path.
        Falls back to direct tool loop if Strands package is not installed.
        """
        try:
            import strands  # type: ignore # noqa: F401
        except Exception as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError("Strands SDK not installed") from exc
        return self._answer_with_llm_loop(question=question, incident_context=incident_context)

    def _answer_with_llm_loop(self, *, question: str, incident_context: dict[str, Any]) -> str:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    "Incident context:\n"
                    + json.dumps(incident_context, indent=2, default=str)
                    + "\n\nUser question:\n"
                    + question
                ),
            }
        ]

        for _ in range(5):
            response = self._llm.create_message(
                system=INTERACTIVE_SYSTEM_PROMPT,
                messages=messages,
                tools=self._tools,
                max_tokens=1024,
            )
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]
            messages.append(
                {"role": "assistant", "content": self._serialise_content(response.content)}
            )

            if not tool_uses:
                if text_blocks:
                    return _sanitize(text_blocks[-1].text)
                break

            tool_results = []
            for tu in tool_uses:
                result = dispatch_eks_mcp_tool(
                    client=self._mcp_client,
                    name=tu.name,
                    inputs=tu.input,
                    server=self._mcp_server,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.tool_use_id,
                        "content": json.dumps(result, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return (
            "I could not complete the full tool-assisted analysis. "
            "Please verify MCP gateway connectivity and try again."
        )

    @staticmethod
    def _serialise_content(content: list[Any]) -> list[dict[str, Any]]:
        result = []
        for block in content:
            if block.type == "text":
                result.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                result.append(
                    {
                        "type": "tool_use",
                        "id": block.tool_use_id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return result
