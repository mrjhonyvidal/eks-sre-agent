from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from eks_ai_ops.interactive.strands_agent import InteractiveEKSAgent
from eks_ai_ops.llm_client import ContentBlock, MessageResponse


def _make_agent(monkeypatch: pytest.MonkeyPatch, backend: str = "llm") -> InteractiveEKSAgent:
    """Build an InteractiveEKSAgent with a mocked LLM and MCP client."""
    monkeypatch.setenv("INTERACTIVE_AGENT_BACKEND", backend)
    monkeypatch.setenv("EKS_MCP_SERVER", "eks")
    monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.example.com")

    with patch("eks_ai_ops.interactive.strands_agent.get_llm_client") as get_llm:
        get_llm.return_value = MagicMock()
        agent = InteractiveEKSAgent()
    return agent


class TestInteractiveEKSAgentLLMLoop:
    def test_returns_text_when_no_tool_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _make_agent(monkeypatch)
        agent._llm.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text="Direct answer.")],
            stop_reason="end_turn",
            model="mock",
        )

        result = agent.answer(question="What's wrong?", incident_context={"id": "1"})

        assert result == "Direct answer."
        assert agent._llm.create_message.call_count == 1

    def test_runs_tool_then_returns_final_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _make_agent(monkeypatch)
        responses = [
            MessageResponse(
                content=[
                    ContentBlock(
                        type="tool_use",
                        tool_use_id="tu_1",
                        name="mcp_get_pods",
                        input={"namespace": "api"},
                    )
                ],
                stop_reason="tool_use",
                model="mock",
            ),
            MessageResponse(
                content=[ContentBlock(type="text", text="Final analysis.")],
                stop_reason="end_turn",
                model="mock",
            ),
        ]
        agent._llm.create_message.side_effect = responses

        with patch(
            "eks_ai_ops.interactive.strands_agent.dispatch_eks_mcp_tool",
            return_value={"pods": []},
        ) as dispatcher:
            result = agent.answer(question="pods?", incident_context={"id": "1"})

        assert result == "Final analysis."
        dispatcher.assert_called_once()
        kwargs = dispatcher.call_args.kwargs
        assert kwargs["name"] == "mcp_get_pods"
        assert kwargs["server"] == "eks"
        assert kwargs["inputs"] == {"namespace": "api"}

    def test_returns_fallback_after_max_iterations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _make_agent(monkeypatch)
        # Always return a tool_use → loop never terminates with text
        agent._llm.create_message.return_value = MessageResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu_x",
                    name="mcp_get_pods",
                    input={},
                )
            ],
            stop_reason="tool_use",
            model="mock",
        )

        with patch(
            "eks_ai_ops.interactive.strands_agent.dispatch_eks_mcp_tool",
            return_value={"pods": []},
        ):
            result = agent.answer(question="loop", incident_context={})

        assert "could not complete" in result.lower()
        assert agent._llm.create_message.call_count == 5

    def test_strands_backend_falls_back_to_llm_loop_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _make_agent(monkeypatch, backend="strands")
        agent._llm.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text="ok")],
            stop_reason="end_turn",
            model="mock",
        )

        # Force the strands path to raise → fallback should kick in
        with patch.object(agent, "_answer_with_strands", side_effect=RuntimeError("boom")):
            result = agent.answer(question="q", incident_context={})

        assert result == "ok"

    def test_serialise_content_handles_text_and_tool_use(self) -> None:
        text_block = ContentBlock(type="text", text="hello")
        tool_block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_1",
            name="mcp_get_pods",
            input={"namespace": "api"},
        )
        result = InteractiveEKSAgent._serialise_content([text_block, tool_block])
        assert result == [
            {"type": "text", "text": "hello"},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "mcp_get_pods",
                "input": {"namespace": "api"},
            },
        ]

    def test_message_payload_includes_incident_context_and_question(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _make_agent(monkeypatch)
        agent._llm.create_message.return_value = MessageResponse(
            content=[ContentBlock(type="text", text="answer")],
            stop_reason="end_turn",
            model="mock",
        )

        agent.answer(
            question="why is it crashing?",
            incident_context={"incident_id": "abc", "severity": "high"},
        )

        kwargs = agent._llm.create_message.call_args.kwargs
        first_message = kwargs["messages"][0]
        assert first_message["role"] == "user"
        content: str = first_message["content"]
        assert "why is it crashing?" in content
        assert "incident_id" in content
        # incident context is JSON-encoded into the prompt
        assert json.dumps({"incident_id": "abc", "severity": "high"}, indent=2) in content


class TestInteractiveEKSAgentStrandsPath:
    def test_strands_raises_when_package_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = _make_agent(monkeypatch, backend="strands")

        # Simulate strands import failure inside the method
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "strands":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_fake_import):
            with pytest.raises(RuntimeError, match="Strands SDK not installed"):
                agent._answer_with_strands(question="q", incident_context={})
