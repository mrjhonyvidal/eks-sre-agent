from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import boto3

from eks_ai_ops.interactive.strands_agent import InteractiveEKSAgent

logger = logging.getLogger(__name__)

K8S_KEYWORDS = {
    # Direct k8s/eks vocabulary
    "k8s",
    "kubernetes",
    "kubectl",
    "eks",
    "pod",
    "pods",
    "deployment",
    "daemonset",
    "statefulset",
    "replicaset",
    "namespace",
    "node",
    "nodes",
    "container",
    "crashloopbackoff",
    "oomkill",
    "oomkilled",
    "service",
    "ingress",
    "helm",
    "hpa",
    "configmap",
    "secret",
    "cluster",
    # Generic SRE / incident vocabulary — when the user is in an
    # incident thread, these are virtually always about the cluster.
    "runbook",
    "root cause",
    "rootcause",
    "incident",
    "alert",
    "alarm",
    "error",
    "errors",
    "log",
    "logs",
    "restart",
    "scale",
    "replica",
    "replicas",
    "fix",
    "why",
    "failing",
    "failed",
    "down",
    "crash",
    "crashing",
}


@dataclass(frozen=True)
class IntentDecision:
    is_k8s: bool
    reason: str


class K8sIntentClassifier:
    """
    Lightweight intent classifier for Slack questions.

    - Fast keyword path (default)
    - Optional Bedrock Nova Micro classification for stricter routing
    """

    def __init__(self) -> None:
        self._use_llm = os.environ.get("INTENT_USE_LLM", "false").strip().lower() == "true"
        self._region = os.environ.get("AWS_REGION", "us-east-1")
        self._intent_model_id = os.environ.get("INTENT_MODEL_ID", "us.amazon.nova-micro-v1:0")

    def classify(self, question: str) -> IntentDecision:
        keyword_hit = self._keyword_is_k8s(question=question)
        if not self._use_llm:
            return IntentDecision(
                is_k8s=keyword_hit,
                reason="keyword match" if keyword_hit else "no k8s keywords",
            )

        llm_decision = self._llm_is_k8s(question=question)
        if llm_decision is not None:
            return IntentDecision(
                is_k8s=llm_decision,
                reason=f"llm intent via {self._intent_model_id}",
            )
        return IntentDecision(
            is_k8s=keyword_hit,
            reason="llm unavailable, keyword fallback",
        )

    @staticmethod
    def _keyword_is_k8s(*, question: str) -> bool:
        text = question.lower()
        return any(token in text for token in K8S_KEYWORDS)

    def _llm_is_k8s(self, *, question: str) -> bool | None:
        try:
            client = boto3.client("bedrock-runtime", region_name=self._region)
            response = client.converse(
                modelId=self._intent_model_id,
                system=[
                    {
                        "text": (
                            "You are an intent classifier. "
                            "Reply with exactly one token: K8S or NON_K8S."
                        )
                    }
                ],
                messages=[{"role": "user", "content": [{"text": question}]}],
                inferenceConfig={"maxTokens": 8, "temperature": 0.0},
            )
            content = response.get("output", {}).get("message", {}).get("content", [])
            text = ""
            if content and "text" in content[0]:
                text = str(content[0]["text"]).strip().upper()
            if "K8S" in text and "NON_K8S" not in text:
                return True
            if "NON_K8S" in text:
                return False
            return None
        except Exception:
            logger.exception("Intent LLM classification failed")
            return None


class K8sOrchestratorAgent:
    """
    Orchestrates Slack question routing:
    1) classify intent
    2) early-exit for non-K8s
    3) route troubleshooting questions to K8s specialist agent
    """

    def __init__(
        self,
        *,
        classifier: K8sIntentClassifier | None = None,
        specialist: InteractiveEKSAgent | None = None,
    ) -> None:
        self._classifier = classifier or K8sIntentClassifier()
        self._specialist = specialist or InteractiveEKSAgent()

    def respond(self, *, question: str, incident_context: dict[str, Any]) -> str:
        decision = self._classifier.classify(question)
        # Inside an incident thread the user has already opted in to a
        # K8s context — don't gate them out for using natural language.
        if not decision.is_k8s and not incident_context:
            return (
                "This request does not look Kubernetes/EKS-related, so I will not run cluster tools. "
                "Ask an EKS or kubectl troubleshooting question to continue."
            )
        return self._specialist.answer(question=question, incident_context=incident_context)
