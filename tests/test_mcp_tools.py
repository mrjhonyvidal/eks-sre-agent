from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import patch

import pytest

from eks_ai_ops.interactive.mcp_tools import (
    MCPToolClient,
    default_eks_mcp_tools,
    dispatch_eks_mcp_tool,
)


class TestMCPToolClient:
    def test_disabled_when_no_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_GATEWAY_URL", raising=False)
        client = MCPToolClient()
        assert client.enabled() is False

    def test_enabled_when_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.example.com")
        client = MCPToolClient()
        assert client.enabled() is True

    def test_call_tool_returns_error_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MCP_GATEWAY_URL", raising=False)
        client = MCPToolClient()
        result = client.call_tool(server="eks", tool="list_pods", arguments={})
        assert "error" in result
        assert "MCP gateway not configured" in result["error"]

    def test_call_tool_posts_payload_and_returns_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.example.com/")
        monkeypatch.setenv("MCP_GATEWAY_API_KEY", "secret-key")
        client = MCPToolClient()

        captured: dict[str, Any] = {}

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"result": "ok", "items": [1, 2]}).encode()

        def _fake_urlopen(req: Any) -> Any:
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data.decode())
            captured["headers"] = dict(req.header_items())
            return _Resp()

        with patch("eks_ai_ops.interactive.mcp_tools.urllib.request.urlopen", _fake_urlopen):
            result = client.call_tool(
                server="eks", tool="list_pods", arguments={"namespace": "api"}
            )

        assert result == {"result": "ok", "items": [1, 2]}
        assert captured["url"] == "https://mcp.example.com/tools/call"
        assert captured["data"] == {
            "server": "eks",
            "tool": "list_pods",
            "arguments": {"namespace": "api"},
        }
        # Header keys are normalised by urllib (Title-Case)
        assert captured["headers"].get("Authorization") == "Bearer secret-key"
        assert captured["headers"].get("Content-type") == "application/json"

    def test_call_tool_wraps_non_dict_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.example.com")
        client = MCPToolClient()

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(["a", "b"]).encode()

        with patch("eks_ai_ops.interactive.mcp_tools.urllib.request.urlopen", return_value=_Resp()):
            result = client.call_tool(server="eks", tool="t", arguments={})

        assert result == {"result": ["a", "b"]}

    def test_call_tool_omits_auth_header_when_no_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_GATEWAY_URL", "https://mcp.example.com")
        monkeypatch.delenv("MCP_GATEWAY_API_KEY", raising=False)
        client = MCPToolClient()

        captured: dict[str, Any] = {}

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self) -> bytes:
                return b"{}"

        def _fake_urlopen(req: Any) -> Any:
            captured["headers"] = dict(req.header_items())
            return _Resp()

        with patch("eks_ai_ops.interactive.mcp_tools.urllib.request.urlopen", _fake_urlopen):
            client.call_tool(server="eks", tool="t", arguments={})

        assert "Authorization" not in captured["headers"]


class TestDefaultEksMcpTools:
    def test_returns_expected_tool_schema(self) -> None:
        client = MCPToolClient()
        tools = default_eks_mcp_tools(client)
        names = {t["name"] for t in tools}
        assert names == {"mcp_get_pods", "mcp_describe_resource", "mcp_get_logs"}
        for tool in tools:
            assert "description" in tool
            assert tool["input_schema"]["type"] == "object"


class TestDispatchEksMcpTool:
    def test_unknown_tool_returns_error(self) -> None:
        client = MCPToolClient()
        result = dispatch_eks_mcp_tool(client=client, name="unknown_tool", inputs={}, server="eks")
        assert "error" in result
        assert "Unknown MCP tool" in result["error"]

    def test_get_pods_dispatches_to_list_pods(self) -> None:
        client = MCPToolClient()
        with patch.object(client, "call_tool", return_value={"pods": []}) as mocked:
            result = dispatch_eks_mcp_tool(
                client=client,
                name="mcp_get_pods",
                inputs={"namespace": "api"},
                server="eks",
            )
        assert result == {"pods": []}
        mocked.assert_called_once_with(
            server="eks", tool="list_pods", arguments={"namespace": "api"}
        )

    def test_get_pods_uses_default_namespace(self) -> None:
        client = MCPToolClient()
        with patch.object(client, "call_tool", return_value={}) as mocked:
            dispatch_eks_mcp_tool(client=client, name="mcp_get_pods", inputs={}, server="eks")
        mocked.assert_called_once_with(
            server="eks", tool="list_pods", arguments={"namespace": "default"}
        )

    def test_describe_resource_passes_arguments(self) -> None:
        client = MCPToolClient()
        with patch.object(client, "call_tool", return_value={"ok": True}) as mocked:
            dispatch_eks_mcp_tool(
                client=client,
                name="mcp_describe_resource",
                inputs={
                    "resource_type": "deployment",
                    "resource_name": "checkout",
                    "namespace": "api",
                },
                server="eks",
            )
        mocked.assert_called_once_with(
            server="eks",
            tool="describe_resource",
            arguments={
                "resource_type": "deployment",
                "resource_name": "checkout",
                "namespace": "api",
            },
        )

    def test_get_logs_coerces_tail_lines_to_int(self) -> None:
        client = MCPToolClient()
        with patch.object(client, "call_tool", return_value={}) as mocked:
            dispatch_eks_mcp_tool(
                client=client,
                name="mcp_get_logs",
                inputs={"pod_name": "checkout-abc", "tail_lines": "50"},
                server="eks",
            )
        mocked.assert_called_once_with(
            server="eks",
            tool="get_pod_logs",
            arguments={
                "pod_name": "checkout-abc",
                "namespace": "default",
                "tail_lines": 50,
            },
        )


# Suppress unused-import warning for io (kept for forward compat in tests)
_ = io
