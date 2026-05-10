"""
kubectl_helper Lambda — runs kubectl commands inside the EKS VPC.

This Lambda is deployed in the same VPC as the EKS cluster and uses the
EKS token API to authenticate with the Kubernetes API server.

Required env vars:
  CLUSTER_NAME   — EKS cluster name
  AWS_REGION     — AWS region (set automatically by Lambda runtime)

IAM permissions required:
  eks:DescribeCluster
  eks:AccessKubernetesApi

The Lambda execution role must be mapped to a Kubernetes RBAC group
(edit the aws-auth ConfigMap or use EKS access entries).
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Execute a kubectl describe / logs command inside the EKS VPC.

    Expected event payload:
      {
        "resource_type": "pod" | "deployment" | "node" | "hpa" | "service",
        "resource_name": "my-pod-abc",
        "namespace": "default",
        "cluster": "my-eks-cluster"
      }
    """
    resource_type = event.get("resource_type", "pod")
    resource_name = event.get("resource_name", "")
    namespace = event.get("namespace", "default")
    cluster = event.get("cluster", os.environ.get("CLUSTER_NAME", ""))

    logger.info(
        "kubectl describe %s/%s -n %s cluster=%s",
        resource_type,
        resource_name,
        namespace,
        cluster,
    )

    if not resource_name or not cluster:
        return {"error": "resource_name and cluster are required"}

    # Write a temporary kubeconfig using the EKS token
    kubeconfig_path = _write_kubeconfig(cluster)
    if not kubeconfig_path:
        return {"error": "Could not generate kubeconfig for cluster"}

    try:
        result = _run_kubectl(
            kubeconfig_path,
            ["describe", resource_type, resource_name, "-n", namespace],
        )
        return {
            "output": result,
            "cluster": cluster,
            "resource": f"{resource_type}/{resource_name}",
        }
    finally:
        # Clean up temp kubeconfig
        try:
            import pathlib

            pathlib.Path(kubeconfig_path).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_kubeconfig(cluster_name: str) -> str | None:
    """Generate a temporary kubeconfig using the EKS describe-cluster API."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        eks = boto3.client("eks", region_name=region)
        cluster_info = eks.describe_cluster(name=cluster_name)["cluster"]

        endpoint = cluster_info["endpoint"]
        ca_data = cluster_info["certificateAuthority"]["data"]

        # Get a short-lived token for kubectl authentication
        token = _get_eks_token(cluster_name, region)

        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "cluster": {
                        "server": endpoint,
                        "certificate-authority-data": ca_data,
                    },
                    "name": cluster_name,
                }
            ],
            "contexts": [
                {
                    "context": {"cluster": cluster_name, "user": "sre-agent"},
                    "name": "sre-agent",
                }
            ],
            "current-context": "sre-agent",
            "users": [
                {
                    "name": "sre-agent",
                    "user": {"token": token},
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            import yaml  # pip install pyyaml

            yaml.safe_dump(kubeconfig, f)
            return f.name

    except Exception as exc:
        logger.error("Could not write kubeconfig: %s", exc, exc_info=True)
        return None


def _get_eks_token(cluster_name: str, region: str) -> str:
    """Generate a pre-signed EKS authentication token (valid for 15 minutes)."""

    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.session.Session()
    credentials = session.get_credentials().get_frozen_credentials()

    url = f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
    headers = {
        "x-k8s-aws-id": cluster_name,
    }
    request = AWSRequest(method="GET", url=url, headers=headers)
    SigV4Auth(credentials, "sts", region).add_auth(request)

    presigned_url = request.prepare().url
    token = (
        "k8s-aws-v1."
        + base64.urlsafe_b64encode(
            presigned_url.encode()  # type: ignore[arg-type]
        )
        .rstrip(b"=")
        .decode()
    )
    return token


def _run_kubectl(kubeconfig: str, args: list[str]) -> str:
    """Run kubectl with the given arguments and return stdout."""
    cmd = ["kubectl", "--kubeconfig", kubeconfig, *args]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("kubectl stderr: %s", result.stderr[:500])
            return result.stderr or "kubectl returned non-zero exit code"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "kubectl command timed out after 20 seconds"
    except FileNotFoundError:
        return "kubectl binary not found in Lambda layer"
