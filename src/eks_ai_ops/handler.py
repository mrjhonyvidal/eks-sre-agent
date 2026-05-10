"""
Backward-compatible proactive Lambda entrypoint.

This module is a thin re-export shim. The actual implementation lives in
`eks_ai_ops.proactive.handler` (Lambda handler) and `eks_ai_ops.proactive.flow`
(orchestration). The SAM template references `handler.handler` here so that
existing deployments and integrations keep working without code changes.

If you are adding new behaviour, edit the modules under `eks_ai_ops/proactive/`
rather than this file.
"""

from datetime import datetime

from eks_ai_ops.agent import SREAgent
from eks_ai_ops.enricher import enrich_event
from eks_ai_ops.github_client import GitHubClient
from eks_ai_ops.proactive.flow import ProactiveIncidentFlow
from eks_ai_ops.proactive.handler import handler as handler
from eks_ai_ops.slack_client import SlackClient

_flow = None
incident_table = None


def _get_flow() -> ProactiveIncidentFlow:
    global _flow, incident_table
    if _flow is None:
        _flow = ProactiveIncidentFlow()
        incident_table = _flow._incident_table
    return _flow


def _incident_id(incident: dict) -> str:
    return ProactiveIncidentFlow._incident_id(incident)


def _is_duplicate(incident_id: str, window_seconds: int = 1800) -> bool:
    return _get_flow()._is_duplicate(incident_id=incident_id, window_seconds=window_seconds)


__all__ = [
    "GitHubClient",
    "SREAgent",
    "SlackClient",
    "_incident_id",
    "_is_duplicate",
    "datetime",
    "enrich_event",
    "handler",
    "incident_table",
]
