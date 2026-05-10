"""
SRE Agent — multi-LLM root cause analysis + fix generation.

The agent receives an enriched incident dict and returns:
  - root_cause: str
  - severity: "critical" | "high" | "medium" | "low"
  - fix_type: "auto" | "manual"
  - fix_description: str
  - pr_files: list[dict]   # only when fix_type == "auto"
  - runbook_steps: list[str]

LLM backend is selected via the LLM_PROVIDER env var:
  LLM_PROVIDER=anthropic  (default — uses ANTHROPIC_API_KEY)
  LLM_PROVIDER=bedrock    (AWS-native — uses IAM execution role, no extra key)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any

import boto3

from eks_ai_ops.llm_client import BaseLLMClient, ContentBlock, get_llm_client
from eks_ai_ops.shared.prompts import PROACTIVE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tools the agent may call while reasoning.
# ---------------------------------------------------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_pod_logs",
        "description": (
            "Fetch the last N lines of logs from a Kubernetes pod. "
            "Use this to find crash/OOM messages, stack traces, or error patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 100},
            },
            "required": ["namespace", "pod_name"],
        },
    },
    {
        "name": "describe_k8s_resource",
        "description": (
            "Run 'kubectl describe' on a resource (pod, deployment, node, hpa). "
            "Returns events, resource limits, conditions, and annotations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "enum": ["pod", "deployment", "node", "hpa", "service"],
                },
                "resource_name": {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["resource_type", "resource_name"],
        },
    },
    {
        "name": "get_cloudwatch_metrics",
        "description": (
            "Fetch a CloudWatch metric time series for the past hour. "
            "Useful for CPU, memory, error-rate, and latency spikes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "e.g. ContainerInsights"},
                "metric_name": {"type": "string"},
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Name": {"type": "string"},
                            "Value": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["namespace", "metric_name", "dimensions"],
        },
    },
    {
        "name": "get_recent_deployments",
        "description": (
            "List the last 5 Helm or kubectl rollout events for a service. "
            "Helps correlate errors with recent deploys."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_name": {"type": "string"},
                "service_name": {"type": "string"},
                "namespace": {"type": "string", "default": "default"},
            },
            "required": ["cluster_name", "service_name"],
        },
    },
    {
        "name": "list_related_alerts",
        "description": "Fetch other open CloudWatch alarms in the same namespace / service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "cluster_name": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["service_name"],
        },
    },
]

SYSTEM_PROMPT = PROACTIVE_SYSTEM_PROMPT

_FALLBACK_RESPONSE: dict[str, Any] = {
    "root_cause": "Analysis timed out after maximum tool-call rounds",
    "severity": "medium",
    "fix_type": "manual",
    "fix_description": "Manual investigation required. Review CloudWatch Logs for raw agent output.",
    "pr_files": [],
    "runbook_steps": [
        "Check CloudWatch Logs for the eks-ai-ops-toolkit Lambda function.",
        "Review the incident context in DynamoDB sre-incidents table.",
        "Escalate to the on-call engineer if the issue persists.",
    ],
}


class SREAgent:
    """
    Orchestrates the agentic RCA loop using a pluggable LLM backend.

    Supports Anthropic Claude and Amazon Bedrock out of the box.
    Select via the LLM_PROVIDER environment variable.
    """

    MAX_TOOL_ROUNDS = 8  # max tool-call/response cycles per incident

    def __init__(self, llm_client: BaseLLMClient | None = None) -> None:
        """
        Args:
            llm_client: Optional pre-built LLM client (useful for testing).
                        If None, auto-selects based on LLM_PROVIDER env var.
        """
        self._llm = llm_client or get_llm_client()
        self._cluster_name = os.environ.get("CLUSTER_NAME", "eks-cluster")
        self._eks_client = boto3.client("eks")
        self._cw_client = boto3.client("cloudwatch")
        self._logs_client = boto3.client("logs")
        logger.info("SREAgent initialised with backend=%s", self._llm.model_id)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyze(self, incident: dict[str, Any]) -> dict[str, Any]:
        """
        Run the agentic loop: LLM reasons, calls tools, reasons again,
        and finally produces a structured RCA + fix.

        Args:
            incident: Normalised incident dict from enricher.py.

        Returns:
            Analysis dict with root_cause, severity, fix_type, etc.
        """
        cluster = incident.get("cluster_name", self._cluster_name)
        resource = incident.get("resource_name", "unknown")
        logger.info(
            "Starting analysis | cluster=%s resource=%s alarm=%s",
            cluster,
            resource,
            incident.get("alarm_name", "unknown"),
        )

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    "An incident has been detected. Here is the initial context:\n\n"
                    + json.dumps(incident, indent=2, default=str)
                    + "\n\nInvestigate and return your analysis as JSON."
                ),
            }
        ]

        for round_num in range(self.MAX_TOOL_ROUNDS):
            response = self._llm.create_message(
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=TOOLS,
                max_tokens=4096,
            )

            logger.debug(
                "Round %d | stop_reason=%s | input_tokens=%d output_tokens=%d",
                round_num + 1,
                response.stop_reason,
                response.usage_input_tokens,
                response.usage_output_tokens,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            # Append assistant turn to history (backend-specific format)
            messages.append(
                {"role": "assistant", "content": self._serialise_content(response.content)}
            )

            if not tool_uses:
                raw = text_blocks[-1].text.strip() if text_blocks else "{}"
                result = self._parse_result(raw)
                logger.info(
                    "Analysis complete | severity=%s fix_type=%s",
                    result.get("severity"),
                    result.get("fix_type"),
                )
                return result

            # Execute tools and feed results back
            tool_results = self._execute_tools(tool_uses)
            messages.append({"role": "user", "content": tool_results})

        logger.warning(
            "Agent hit max rounds (%d) without final answer | cluster=%s resource=%s",
            self.MAX_TOOL_ROUNDS,
            cluster,
            resource,
        )
        return dict(_FALLBACK_RESPONSE)

    # ------------------------------------------------------------------
    # Tool dispatcher
    # ------------------------------------------------------------------

    def _execute_tools(self, tool_uses: list[ContentBlock]) -> list[dict[str, Any]]:
        """Execute all tool calls and return formatted results."""
        results = []
        for tu in tool_uses:
            logger.info("Calling tool=%s args=%s", tu.name, list(tu.input.keys()))
            result = self._dispatch_tool(tu.name, tu.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.tool_use_id,
                    "content": json.dumps(result, default=str),
                }
            )
        return results

    def _dispatch_tool(self, name: str, inputs: dict[str, Any]) -> Any:
        handlers: dict[str, Any] = {
            "get_pod_logs": self._tool_get_pod_logs,
            "describe_k8s_resource": self._tool_describe_k8s_resource,
            "get_cloudwatch_metrics": self._tool_get_cloudwatch_metrics,
            "get_recent_deployments": self._tool_get_recent_deployments,
            "list_related_alerts": self._tool_list_related_alerts,
        }
        fn = handlers.get(name)
        if not fn:
            logger.warning("Unknown tool requested: %s", name)
            return {"error": f"Unknown tool: {name}"}
        try:
            return fn(**inputs)
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_get_pod_logs(
        self, namespace: str, pod_name: str, tail_lines: int = 100
    ) -> dict[str, Any]:
        """Pull logs from CloudWatch Logs (EKS Container Insights log group)."""
        log_group = f"/aws/containerinsights/{self._cluster_name}/application"
        try:
            response = self._logs_client.filter_log_events(
                logGroupName=log_group,
                logStreamNamePrefix=f"{namespace}/{pod_name}",
                limit=tail_lines,
            )
            events = [e["message"] for e in response.get("events", [])]
            return {"pod": pod_name, "namespace": namespace, "logs": events[-tail_lines:]}
        except self._logs_client.exceptions.ResourceNotFoundException:
            logger.warning("Log group not found: %s", log_group)
            return {"error": f"Log group {log_group} not found"}

    def _tool_describe_k8s_resource(
        self, resource_type: str, resource_name: str, namespace: str = "default"
    ) -> dict[str, Any]:
        """
        Invoke the kubectl-helper Lambda (runs inside the EKS VPC).
        Falls back to a stub if the helper Lambda is not configured.
        """
        try:
            lambda_client = boto3.client("lambda")
            payload = {
                "resource_type": resource_type,
                "resource_name": resource_name,
                "namespace": namespace,
                "cluster": self._cluster_name,
            }
            resp = lambda_client.invoke(
                FunctionName=os.environ.get("KUBECTL_LAMBDA", "sre-kubectl-helper"),
                Payload=json.dumps(payload),
            )
            return json.loads(resp["Payload"].read())  # type: ignore[arg-type]
        except Exception as exc:
            logger.error("kubectl-helper Lambda invocation failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    def _tool_get_cloudwatch_metrics(
        self, namespace: str, metric_name: str, dimensions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Fetch CloudWatch metric statistics for the past hour."""
        end = datetime.datetime.now(tz=datetime.UTC)
        start = end - datetime.timedelta(hours=1)
        response = self._cw_client.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=300,
            Statistics=["Average", "Maximum"],
        )
        points = sorted(response["Datapoints"], key=lambda x: x["Timestamp"])
        return {
            "metric": metric_name,
            "datapoints": [
                {
                    "timestamp": p["Timestamp"].isoformat(),
                    "avg": round(p["Average"], 2),
                    "max": round(p["Maximum"], 2),
                }
                for p in points
            ],
        }

    def _tool_get_recent_deployments(
        self, cluster_name: str, service_name: str, namespace: str = "default"
    ) -> dict[str, Any]:
        """Fetch deployment history from DynamoDB (written by a CI/CD hook)."""
        ddb = boto3.resource("dynamodb")
        table = ddb.Table(os.environ.get("DEPLOY_TABLE", "sre-deployments"))
        try:
            resp = table.query(
                KeyConditionExpression="service_name = :s",
                ExpressionAttributeValues={":s": f"{cluster_name}/{namespace}/{service_name}"},
                ScanIndexForward=False,
                Limit=5,
            )
            return {"deployments": resp.get("Items", [])}
        except Exception as exc:
            logger.error("DynamoDB deployment query failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    def _tool_list_related_alerts(
        self, service_name: str, cluster_name: str = "", max_results: int = 10
    ) -> dict[str, Any]:
        """List open CloudWatch alarms matching this service."""
        response = self._cw_client.describe_alarms(
            StateValue="ALARM",
            MaxRecords=max_results,
        )
        alarms = [
            {
                "name": a["AlarmName"],
                "state": a["StateValue"],
                "reason": a["StateReason"],
                "updated": a["StateUpdatedTimestamp"].isoformat(),
            }
            for a in response.get("MetricAlarms", [])
            if service_name.lower() in a["AlarmName"].lower()
        ]
        return {"related_alarms": alarms}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialise_content(blocks: list[ContentBlock]) -> list[dict[str, Any]]:
        """
        Convert ContentBlock list back to the dict format expected by the
        messages history (compatible with both Anthropic and Bedrock wrappers).
        """
        result = []
        for b in blocks:
            if b.type == "text":
                result.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                result.append(
                    {
                        "type": "tool_use",
                        "id": b.tool_use_id,
                        "name": b.name,
                        "input": b.input,
                    }
                )
        return result

    @staticmethod
    def _parse_result(raw: str) -> dict[str, Any]:
        """Strip markdown fences / chain-of-thought and parse the JSON response."""
        import re

        # Strip chain-of-thought tags some models leak (Nova, DeepSeek, etc.)
        cleaned = re.sub(r"<thinking>.*?</thinking>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        cleaned = cleaned.strip()
        # Strip markdown code fences
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        # Fast path: whole string is JSON
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Fallback: extract the first {...} block (greedy on outer braces)
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        logger.error("Could not parse agent output (first 200 chars): %s", raw[:200])
        return {
            "root_cause": cleaned[:500] or raw[:500],
            "severity": "medium",
            "fix_type": "manual",
            "fix_description": "Parse error — see raw output in CloudWatch Logs.",
            "pr_files": [],
            "runbook_steps": ["Review raw agent output in CloudWatch Logs."],
        }
