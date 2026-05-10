from __future__ import annotations

from unittest.mock import MagicMock

from eks_ai_ops.interactive.orchestrator import K8sIntentClassifier, K8sOrchestratorAgent


class TestK8sIntentClassifier:
    def test_keyword_classifies_k8s(self) -> None:
        classifier = K8sIntentClassifier()
        decision = classifier.classify("show me pod restarts in eks")
        assert decision.is_k8s is True

    def test_keyword_classifies_non_k8s(self) -> None:
        classifier = K8sIntentClassifier()
        decision = classifier.classify("what is our product roadmap for q4")
        assert decision.is_k8s is False


class TestK8sOrchestratorAgent:
    def test_non_k8s_exits_early(self) -> None:
        classifier = MagicMock()
        classifier.classify.return_value = MagicMock(is_k8s=False, reason="non-k8s")
        specialist = MagicMock()
        orchestrator = K8sOrchestratorAgent(classifier=classifier, specialist=specialist)

        response = orchestrator.respond(question="tell me a joke", incident_context={})
        assert "does not look Kubernetes/EKS-related" in response
        specialist.answer.assert_not_called()

    def test_k8s_routes_to_specialist(self) -> None:
        classifier = MagicMock()
        classifier.classify.return_value = MagicMock(is_k8s=True, reason="k8s")
        specialist = MagicMock()
        specialist.answer.return_value = "specialist result"
        orchestrator = K8sOrchestratorAgent(classifier=classifier, specialist=specialist)

        response = orchestrator.respond(question="kubectl get pods", incident_context={"id": "1"})
        assert response == "specialist result"
        specialist.answer.assert_called_once()
