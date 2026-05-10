"""Unit tests for sre_agent/enricher.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sre_agent.enricher import (
    _from_cloudwatch,
    _from_eks_audit,
    _from_scheduled,
    _get_related_alarms,
    enrich_event,
)


class TestEnrichEvent:
    """Tests for the main enrich_event dispatcher."""

    def test_cloudwatch_alarm_dispatches_correctly(self, cloudwatch_alarm_event: dict) -> None:
        with patch("sre_agent.enricher.boto3") as mock_boto3:
            mock_cw = MagicMock()
            mock_cw.describe_alarms.return_value = {"MetricAlarms": []}
            mock_boto3.client.return_value = mock_cw
            result = enrich_event(cloudwatch_alarm_event)
        assert result["source"] == "cloudwatch_alarm"
        assert result["alarm_name"] == "sre-prod-api-checkout-error-rate-high"

    def test_eks_audit_event_dispatches_correctly(self, eks_audit_event: dict) -> None:
        result = enrich_event(eks_audit_event)
        assert result["source"] == "eks_audit"
        assert result["namespace"] == "api"

    def test_scheduled_sweep_dispatches_correctly(self, scheduled_sweep_event: dict) -> None:
        result = enrich_event(scheduled_sweep_event)
        assert result["source"] == "scheduled_sweep"
        assert result["cluster_name"] == "prod"

    def test_unknown_source_falls_back_to_generic(self, unknown_source_event: dict) -> None:
        result = enrich_event(unknown_source_event)
        assert result["source"] == "custom.internal"
        assert result["namespace"] == "unknown"
        assert result["resource_type"] == "unknown"
        assert "raw_event" in result

    def test_missing_source_falls_back_gracefully(self) -> None:
        result = enrich_event({})
        assert result["source"] == "unknown"
        assert result["cluster_name"] == "test-cluster"


class TestFromCloudwatch:
    """Tests for CloudWatch alarm normalisation."""

    def test_parses_cluster_namespace_service_from_alarm_name(self) -> None:
        raw = {
            "source": "aws.cloudwatch",
            "detail": {
                "alarmName": "sre-prod-api-checkout-error-rate-high",
                "state": {"value": "ALARM", "reason": "Threshold crossed"},
                "previousState": {"value": "OK"},
                "configuration": {"description": "Test alarm"},
            },
        }
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {"MetricAlarms": []}
        with patch("sre_agent.enricher.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_cw
            result = _from_cloudwatch(raw)

        assert result["cluster_name"] == "prod"
        assert result["namespace"] == "api"
        assert result["resource_name"] == "checkout"
        assert result["current_state"] == "ALARM"
        assert result["previous_state"] == "OK"
        assert result["alarm_description"] == "Test alarm"

    def test_alarm_name_with_insufficient_parts_falls_back(self) -> None:
        """An alarm name with no standard prefix — namespace/resource fall back to defaults."""
        raw = {
            "source": "aws.cloudwatch",
            "detail": {
                "alarmName": "simple-alarm",
                "state": {"value": "ALARM", "reason": ""},
                "previousState": {"value": "OK"},
                "configuration": {},
            },
        }
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {"MetricAlarms": []}
        with patch("sre_agent.enricher.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_cw
            result = _from_cloudwatch(raw)

        # "simple-alarm".split("-") → ["simple", "alarm"] — parts[1]="alarm" used as cluster
        # No namespace part available, so falls back to "default"
        assert result["alarm_name"] == "simple-alarm"
        assert result["namespace"] == "default"

    def test_related_alarms_are_fetched(self) -> None:
        raw = {
            "source": "aws.cloudwatch",
            "detail": {
                "alarmName": "sre-prod-api-checkout-cpu-high",
                "state": {"value": "ALARM", "reason": "CPU > 80%"},
                "previousState": {"value": "OK"},
                "configuration": {},
            },
        }
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {
            "MetricAlarms": [
                {
                    "AlarmName": "sre-prod-api-checkout-memory-high",
                    "StateReason": "Memory > 90%",
                }
            ]
        }
        with patch("sre_agent.enricher.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_cw
            result = _from_cloudwatch(raw)

        assert len(result["related_alarms"]) == 1
        assert result["related_alarms"][0]["name"] == "sre-prod-api-checkout-memory-high"


class TestFromEksAudit:
    """Tests for EKS audit event normalisation."""

    def test_extracts_namespace_resource_name_verb(self, eks_audit_event: dict) -> None:
        result = _from_eks_audit(eks_audit_event)
        assert result["namespace"] == "api"
        assert result["resource_name"] == "checkout-6f9b4c-xkpj2"
        assert result["verb"] == "delete"
        assert result["resource_type"] == "pods"
        assert result["user"] == "system:node:ip-10-0-1-5"

    def test_falls_back_when_request_params_missing(self) -> None:
        raw = {
            "source": "aws.eks",
            "detail": {
                "verb": "create",
                "resource": {"resource": "deployments"},
                "requestParameters": {},
                "responseElements": {"metadata": {"namespace": "kube-system", "name": "coredns"}},
                "user": {"username": "admin"},
            },
        }
        result = _from_eks_audit(raw)
        assert result["namespace"] == "kube-system"
        assert result["resource_name"] == "coredns"

    def test_missing_fields_fall_back_to_defaults(self) -> None:
        raw = {"source": "aws.eks", "detail": {}}
        result = _from_eks_audit(raw)
        assert result["namespace"] == "default"
        assert result["resource_name"] == "unknown"
        assert result["verb"] == "unknown"


class TestFromScheduled:
    """Tests for scheduled sweep event normalisation."""

    def test_extracts_all_scheduled_fields(self, scheduled_sweep_event: dict) -> None:
        result = _from_scheduled(scheduled_sweep_event)
        assert result["source"] == "scheduled_sweep"
        assert result["cluster_name"] == "prod"
        assert result["alarm_name"] == "pod-crashloop-check"
        assert result["namespace"] == "api"
        assert result["resource_type"] == "pod"
        assert result["resource_name"] == "checkout-6f9b4c-xkpj2"
        assert "CrashLoopBackOff" in result["findings"][0]

    def test_defaults_when_detail_empty(self) -> None:
        result = _from_scheduled({"source": "sre.scheduled", "detail": {}})
        assert result["cluster_name"] == "test-cluster"
        assert result["alarm_name"] == "scheduled-health-check"
        assert result["namespace"] == "default"


class TestGetRelatedAlarms:
    """Tests for _get_related_alarms helper."""

    def test_filters_by_service_name(self) -> None:
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {
            "MetricAlarms": [
                {"AlarmName": "sre-prod-api-checkout-cpu", "StateReason": "CPU high"},
                {"AlarmName": "sre-prod-api-payments-error", "StateReason": "Error high"},
            ]
        }
        result = _get_related_alarms(mock_cw, "checkout")
        assert len(result) == 1
        assert result[0]["name"] == "sre-prod-api-checkout-cpu"

    def test_truncates_reason_at_200_chars(self) -> None:
        long_reason = "A" * 300
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {
            "MetricAlarms": [{"AlarmName": "sre-prod-svc-checkout", "StateReason": long_reason}]
        }
        result = _get_related_alarms(mock_cw, "checkout")
        assert len(result[0]["reason"]) <= 200

    def test_returns_at_most_5_alarms(self) -> None:
        mock_cw = MagicMock()
        mock_cw.describe_alarms.return_value = {
            "MetricAlarms": [
                {"AlarmName": f"sre-prod-api-checkout-metric{i}", "StateReason": "x"}
                for i in range(10)
            ]
        }
        result = _get_related_alarms(mock_cw, "checkout")
        assert len(result) <= 5

    def test_returns_empty_list_on_exception(self) -> None:
        mock_cw = MagicMock()
        mock_cw.describe_alarms.side_effect = Exception("boto3 error")
        result = _get_related_alarms(mock_cw, "checkout")
        assert result == []
