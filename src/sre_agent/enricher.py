"""
Enricher — normalises raw EventBridge events and attaches live k8s context.

Supported sources:
  - aws.cloudwatch  (alarm state change)
  - aws.eks         (control-plane audit events)
  - sre.scheduled   (periodic health sweep, internal format)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "eks-cluster")


def enrich_event(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Return a normalised incident dict that the agent can reason about.
    Always includes: source, cluster_name, alarm_name, namespace,
    resource_type, resource_name, raw_event.
    """
    source = raw.get("source", "unknown")

    if source == "aws.cloudwatch":
        return _from_cloudwatch(raw)
    elif source == "aws.eks":
        return _from_eks_audit(raw)
    elif source == "sre.scheduled":
        return _from_scheduled(raw)
    else:
        # Generic fallback — pass through as-is with minimal normalisation
        return {
            "source": source,
            "cluster_name": CLUSTER_NAME,
            "alarm_name": raw.get("detail", {}).get("alarmName", "unknown"),
            "namespace": "unknown",
            "resource_type": "unknown",
            "resource_name": "unknown",
            "raw_event": raw,
        }


# ------------------------------------------------------------------ #
#  Source-specific normalisers                                         #
# ------------------------------------------------------------------ #


def _from_cloudwatch(raw: dict) -> dict:
    detail = raw.get("detail", {})
    config = detail.get("configuration", {})
    alarm_name = detail.get("alarmName", "")

    # Try to parse cluster/namespace/service from the alarm name convention:
    # sre-{cluster}-{namespace}-{service}-{metric}
    parts = alarm_name.split("-")
    cluster = parts[1] if len(parts) > 1 else CLUSTER_NAME
    namespace = parts[2] if len(parts) > 2 else "default"
    service = parts[3] if len(parts) > 3 else alarm_name

    # Attach recent metric data for the agent
    cw = boto3.client("cloudwatch")
    related_alarms = _get_related_alarms(cw, service)

    return {
        "source": "cloudwatch_alarm",
        "cluster_name": cluster,
        "alarm_name": alarm_name,
        "alarm_description": config.get("description", ""),
        "previous_state": detail.get("previousState", {}).get("value"),
        "current_state": detail.get("state", {}).get("value"),
        "state_reason": detail.get("state", {}).get("reason", ""),
        "namespace": namespace,
        "resource_type": "deployment",
        "resource_name": service,
        "related_alarms": related_alarms,
        "raw_event": raw,
    }


def _from_eks_audit(raw: dict) -> dict:
    detail = raw.get("detail", {})
    req = detail.get("requestParameters", {})
    obj = detail.get("responseElements", {})

    namespace = req.get("namespace") or obj.get("metadata", {}).get("namespace", "default")
    resource_name = req.get("name") or obj.get("metadata", {}).get("name", "unknown")
    verb = detail.get("verb", "unknown")
    resource_type = detail.get("resource", {}).get("resource", "unknown")

    return {
        "source": "eks_audit",
        "cluster_name": CLUSTER_NAME,
        "alarm_name": f"{verb} {resource_type}/{resource_name}",
        "namespace": namespace,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "user": detail.get("user", {}).get("username", "unknown"),
        "verb": verb,
        "raw_event": raw,
    }


def _from_scheduled(raw: dict) -> dict:
    """Internal scheduled sweep — payload already pre-normalised by the scheduler."""
    d = raw.get("detail", {})
    return {
        "source": "scheduled_sweep",
        "cluster_name": d.get("cluster", CLUSTER_NAME),
        "alarm_name": d.get("check_name", "scheduled-health-check"),
        "namespace": d.get("namespace", "default"),
        "resource_type": d.get("resource_type", "deployment"),
        "resource_name": d.get("resource_name", "unknown"),
        "findings": d.get("findings", []),
        "raw_event": raw,
    }


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #


def _get_related_alarms(cw_client: Any, service_name: str) -> list[dict]:
    try:
        resp = cw_client.describe_alarms(StateValue="ALARM", MaxRecords=20)
        return [
            {
                "name": a["AlarmName"],
                "reason": a["StateReason"][:200],
            }
            for a in resp.get("MetricAlarms", [])
            if service_name.lower() in a["AlarmName"].lower()
        ][:5]
    except Exception as exc:
        logger.warning("Could not fetch related alarms: %s", exc)
        return []
