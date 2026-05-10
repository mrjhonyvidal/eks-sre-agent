from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger(__name__)


class MCPToolClient:
    """
    Simple HTTP bridge to an MCP gateway/service.

    Expected endpoint:
      POST {MCP_GATEWAY_URL}/tools/call
      {"server":"...","tool":"...","arguments":{...}}
    """

    def __init__(self) -> None:
        self._base_url = os.environ.get("MCP_GATEWAY_URL", "").rstrip("/")
        self._api_key = os.environ.get("MCP_GATEWAY_API_KEY", "")

    def enabled(self) -> bool:
        return bool(self._base_url)

    def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url:
            return {"error": "MCP gateway not configured. Set MCP_GATEWAY_URL to enable MCP calls."}

        payload = {"server": server, "tool": tool, "arguments": arguments}
        req = urllib.request.Request(
            f"{self._base_url}/tools/call",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
            },
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            body = json.loads(resp.read())
            return body if isinstance(body, dict) else {"result": body}


def default_eks_mcp_tools(client: MCPToolClient) -> list[dict[str, Any]]:
    """
    Tool schema list for agent use.
    """
    return [
        {
            "name": "mcp_get_pods",
            "description": "List pods in a namespace via MCP-backed EKS tools.",
            "input_schema": {
                "type": "object",
                "properties": {"namespace": {"type": "string", "default": "default"}},
            },
        },
        {
            "name": "mcp_describe_resource",
            "description": "Describe a Kubernetes resource via MCP-backed EKS tools.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_type": {"type": "string"},
                    "resource_name": {"type": "string"},
                    "namespace": {"type": "string", "default": "default"},
                },
                "required": ["resource_type", "resource_name"],
            },
        },
        {
            "name": "mcp_get_logs",
            "description": "Fetch recent pod logs via MCP-backed EKS tools.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pod_name": {"type": "string"},
                    "namespace": {"type": "string", "default": "default"},
                    "tail_lines": {"type": "integer", "default": 100},
                },
                "required": ["pod_name"],
            },
        },
        {
            "name": "kubectl_describe",
            "description": (
                "Run 'kubectl describe' against the live EKS cluster via the in-VPC "
                "kubectl_helper Lambda. Works without an MCP gateway. Use this to get "
                "real cluster state for a pod, deployment, node, service, or hpa."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["pod", "deployment", "node", "service", "hpa"],
                    },
                    "resource_name": {"type": "string"},
                    "namespace": {"type": "string", "default": "default"},
                },
                "required": ["resource_type", "resource_name"],
            },
        },
    ]


def dispatch_eks_mcp_tool(
    *,
    client: MCPToolClient,
    name: str,
    inputs: dict[str, Any],
    server: str,
) -> dict[str, Any]:
    if name == "kubectl_describe":
        return _invoke_kubectl_describe(inputs)

    mapping = {
        "mcp_get_pods": ("list_pods", {"namespace": inputs.get("namespace", "default")}),
        "mcp_describe_resource": (
            "describe_resource",
            {
                "resource_type": inputs.get("resource_type", "pod"),
                "resource_name": inputs.get("resource_name", ""),
                "namespace": inputs.get("namespace", "default"),
            },
        ),
        "mcp_get_logs": (
            "get_pod_logs",
            {
                "pod_name": inputs.get("pod_name", ""),
                "namespace": inputs.get("namespace", "default"),
                "tail_lines": int(inputs.get("tail_lines", 100)),
            },
        ),
    }
    tool_call = mapping.get(name)
    if not tool_call:
        return {"error": f"Unknown MCP tool: {name}"}

    tool_name, arguments = tool_call
    return client.call_tool(server=server, tool=tool_name, arguments=arguments)


def _invoke_kubectl_describe(inputs: dict[str, Any]) -> dict[str, Any]:
    """Direct Lambda invoke of the in-VPC kubectl_helper. No MCP gateway needed."""
    fn = os.environ.get("KUBECTL_LAMBDA", "")
    if not fn:
        return {"error": "KUBECTL_LAMBDA env var not set; cannot run kubectl describe."}
    payload = {
        "resource_type": inputs.get("resource_type", "pod"),
        "resource_name": inputs.get("resource_name", ""),
        "namespace": inputs.get("namespace", "default"),
        "cluster": os.environ.get("CLUSTER_NAME", ""),
    }
    try:
        client = boto3.client("lambda")
        resp = client.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
        body = resp["Payload"].read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"output": body}
    except Exception as exc:
        logger.exception("kubectl_helper invoke failed")
        return {"error": f"kubectl_helper invoke failed: {exc}"}
