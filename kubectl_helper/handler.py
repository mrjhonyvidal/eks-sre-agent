"""
kubectl_helper Lambda — fetches resource info from the EKS API server.

Runs inside the EKS VPC and talks directly to the Kubernetes API using a
short-lived EKS pre-signed token. Returns the resource as JSON (the calling
LLM can summarize it just like `kubectl describe` output).

No kubectl binary or extra Lambda layer required — only boto3 (already in
the Lambda runtime).

Required env:
  CLUSTER_NAME — default cluster name (overridable per-event)
  AWS_REGION   — set automatically by the Lambda runtime

IAM:
  eks:DescribeCluster
  eks:AccessKubernetesApi (the helper role must be mapped via an EKS access
  entry with at least the AmazonEKSViewPolicy)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import tempfile
import urllib.error
import urllib.request
from typing import Any

import boto3
from botocore.signers import RequestSigner

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# Map friendly resource type → (api path template, is_namespaced)
_RESOURCE_PATHS = {
    "pod": ("/api/v1/namespaces/{ns}/pods/{name}", True),
    "service": ("/api/v1/namespaces/{ns}/services/{name}", True),
    "deployment": ("/apis/apps/v1/namespaces/{ns}/deployments/{name}", True),
    "hpa": ("/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers/{name}", True),
    "node": ("/api/v1/nodes/{name}", False),
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Fetch a Kubernetes resource and return a triage-friendly JSON summary.

    Expected event payload:
      {
        "resource_type": "pod" | "deployment" | "node" | "hpa" | "service",
        "resource_name": "my-pod-abc",
        "namespace":     "default",
        "cluster":       "my-eks-cluster"
      }
    """
    resource_type = event.get("resource_type", "pod")
    resource_name = event.get("resource_name", "")
    namespace = event.get("namespace") or "default"
    cluster = event.get("cluster") or os.environ.get("CLUSTER_NAME", "")

    logger.info(
        "describe %s/%s -n %s cluster=%s",
        resource_type,
        resource_name,
        namespace,
        cluster,
    )

    if not resource_name or not cluster:
        return {"error": "resource_name and cluster are required"}

    if resource_type not in _RESOURCE_PATHS:
        return {"error": f"unsupported resource_type: {resource_type}"}

    path_template, _ = _RESOURCE_PATHS[resource_type]
    api_path = path_template.format(ns=namespace, name=resource_name)

    try:
        cluster_info = _describe_cluster(cluster)
    except Exception as exc:  # pragma: no cover
        logger.error("describe-cluster failed: %s", exc, exc_info=True)
        return {"error": f"describe-cluster failed: {exc}"}

    endpoint = cluster_info["endpoint"].rstrip("/")
    ca_data = cluster_info["certificateAuthority"]["data"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    token = _get_eks_token(cluster, region)

    ca_path = _write_ca_bundle(ca_data)
    try:
        body = _kube_get(f"{endpoint}{api_path}", token=token, ca_path=ca_path)
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode(errors="replace")[:500]
        logger.warning("k8s API %s -> %s: %s", api_path, exc.code, msg)
        return {"error": f"kube API returned {exc.code}", "details": msg}
    except Exception as exc:  # pragma: no cover
        logger.error("kube GET failed: %s", exc, exc_info=True)
        return {"error": f"kube GET failed: {exc}"}
    finally:
        try:
            import pathlib

            pathlib.Path(ca_path).unlink(missing_ok=True)
        except Exception:
            pass

    try:
        data = json.loads(body)
    except ValueError:
        data = {"raw": body[:4000]}

    summary = _summarize(resource_type, data)
    return {
        "output": json.dumps(summary, default=str)[:8000],
        "cluster": cluster,
        "resource": f"{resource_type}/{resource_name}",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_cluster(name: str) -> dict[str, Any]:
    region = os.environ.get("AWS_REGION", "us-east-1")
    eks = boto3.client("eks", region_name=region)
    return eks.describe_cluster(name=name)["cluster"]


def _get_eks_token(cluster_name: str, region: str) -> str:
    """Generate a pre-signed EKS authentication token (valid ~60 seconds).

    EKS validates the token by replaying the pre-signed GetCallerIdentity URL,
    which must carry SigV4 query-string auth and an `x-k8s-aws-id` header
    binding the signature to the target cluster.
    """
    session = boto3.session.Session()
    client = session.client("sts", region_name=region)
    signer = RequestSigner(
        client.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        client.meta.events,
    )
    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    signed_url = signer.generate_presigned_url(
        params, region_name=region, expires_in=60, operation_name=""
    )
    return (
        "k8s-aws-v1."
        + base64.urlsafe_b64encode(signed_url.encode()).rstrip(b"=").decode()
    )


def _write_ca_bundle(b64_ca: str) -> str:
    pem = base64.b64decode(b64_ca)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False) as f:
        f.write(pem)
        return f.name


def _kube_get(url: str, *, token: str, ca_path: str) -> str:
    ctx = ssl.create_default_context(cafile=ca_path)
    req = urllib.request.Request(  # noqa: S310 - https-only by construction
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _summarize(resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Trim the K8s object down to fields most useful for incident triage."""
    meta = data.get("metadata", {}) or {}
    base: dict[str, Any] = {
        "kind": data.get("kind"),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels"),
        "creationTimestamp": meta.get("creationTimestamp"),
    }

    if resource_type == "pod":
        spec = data.get("spec", {}) or {}
        status = data.get("status", {}) or {}
        base.update(
            {
                "nodeName": spec.get("nodeName"),
                "phase": status.get("phase"),
                "reason": status.get("reason"),
                "message": status.get("message"),
                "podIP": status.get("podIP"),
                "containers": [
                    {
                        "name": c.get("name"),
                        "image": c.get("image"),
                        "resources": c.get("resources"),
                    }
                    for c in spec.get("containers", [])
                ],
                "containerStatuses": [
                    {
                        "name": c.get("name"),
                        "ready": c.get("ready"),
                        "restartCount": c.get("restartCount"),
                        "state": c.get("state"),
                        "lastState": c.get("lastState"),
                    }
                    for c in status.get("containerStatuses", [])
                ],
                "conditions": status.get("conditions"),
            }
        )
    elif resource_type == "deployment":
        spec = data.get("spec", {}) or {}
        status = data.get("status", {}) or {}
        base.update(
            {
                "replicas": spec.get("replicas"),
                "strategy": spec.get("strategy"),
                "availableReplicas": status.get("availableReplicas"),
                "readyReplicas": status.get("readyReplicas"),
                "unavailableReplicas": status.get("unavailableReplicas"),
                "conditions": status.get("conditions"),
            }
        )
    elif resource_type == "node":
        status = data.get("status", {}) or {}
        base.update(
            {
                "addresses": status.get("addresses"),
                "capacity": status.get("capacity"),
                "allocatable": status.get("allocatable"),
                "conditions": status.get("conditions"),
                "nodeInfo": status.get("nodeInfo"),
            }
        )
    elif resource_type == "service":
        spec = data.get("spec", {}) or {}
        base.update(
            {
                "type": spec.get("type"),
                "clusterIP": spec.get("clusterIP"),
                "ports": spec.get("ports"),
                "selector": spec.get("selector"),
            }
        )
    elif resource_type == "hpa":
        spec = data.get("spec", {}) or {}
        status = data.get("status", {}) or {}
        base.update(
            {
                "minReplicas": spec.get("minReplicas"),
                "maxReplicas": spec.get("maxReplicas"),
                "metrics": spec.get("metrics"),
                "currentReplicas": status.get("currentReplicas"),
                "desiredReplicas": status.get("desiredReplicas"),
                "conditions": status.get("conditions"),
            }
        )
    return base
