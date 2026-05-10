from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SharedConfig:
    aws_region: str
    cluster_name: str
    incident_table: str
    deploy_table: str
    slack_channel: str

    @classmethod
    def from_env(cls) -> SharedConfig:
        return cls(
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
            cluster_name=os.environ.get("CLUSTER_NAME", "eks-cluster"),
            incident_table=os.environ.get("INCIDENT_TABLE", "sre-incidents"),
            deploy_table=os.environ.get("DEPLOY_TABLE", "sre-deployments"),
            slack_channel=os.environ.get("SLACK_CHANNEL", "#sre-alerts"),
        )
