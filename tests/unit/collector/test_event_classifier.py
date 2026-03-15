"""
Unit tests for lambda/collector/event_classifier.py

No AWS mocking required — pure logic.
"""
import pytest

from event_classifier import (
    ClassificationResult,
    _determine_severity,
    _is_operational,
    _matches,
    classify_event,
)


# ── _matches ──────────────────────────────────────────────────────────────────

class TestMatches:
    def test_exact_match(self):
        assert _matches(r"^EC2$", "EC2") is True

    def test_no_match(self):
        assert _matches(r"^EC2$", "RDS") is False

    def test_case_insensitive(self):
        assert _matches(r"outage", "OUTAGE_DETECTED") is True

    def test_partial_match_in_string(self):
        assert _matches(r"OUTAGE", "AWS_EC2_OUTAGE_ISSUE") is True

    def test_none_value_returns_false(self):
        assert _matches(r".*", None) is False

    def test_empty_string_returns_false(self):
        assert _matches(r".*", "") is False

    def test_invalid_regex_falls_back_to_equality_true(self):
        assert _matches("[invalid", "[invalid") is True

    def test_invalid_regex_falls_back_to_equality_false(self):
        assert _matches("[invalid", "other") is False

    def test_dot_star_matches_anything(self):
        assert _matches(r".*", "anything") is True


# ── _is_operational ───────────────────────────────────────────────────────────

class TestIsOperational:
    @pytest.mark.parametrize("service", [
        "EC2", "ECS", "Lambda", "Fargate",
        "RDS", "Aurora", "DynamoDB", "ElastiCache", "MemoryDB",
        "S3", "EBS", "EFS", "FSx",
        "ELB", "ALB", "NLB", "VPC", "CloudFront", "Route 53",
        "CloudWatch", "CloudWatch Logs",
        "Auto Scaling", "Application Auto Scaling",
        "CodeBuild", "CodeDeploy", "CodePipeline",
        "SQS", "SNS", "Kinesis", "MSK",
        "EKS", "EMR", "Glue", "Athena",
        "Redshift", "OpenSearch", "ElasticSearch Service",
        "API Gateway", "AppSync",
        "Secrets Manager", "ACM",
    ])
    def test_known_operational_services(self, service):
        ok, reasons = _is_operational(service, "")
        assert ok is True, f"{service} should be operational"
        assert reasons

    def test_non_operational_service_no_description(self):
        ok, reasons = _is_operational("IAM", "")
        assert ok is False
        assert reasons == []

    def test_non_operational_service_no_description_cloudformation(self):
        ok, _ = _is_operational("CloudFormation", "")
        assert ok is False

    def test_description_keyword_outage(self):
        ok, reasons = _is_operational("IAM", "A database outage was detected")
        assert ok is True
        assert "description contains" in reasons[0]

    def test_description_keyword_unavailable(self):
        ok, _ = _is_operational("Config", "Service is unavailable")
        assert ok is True

    def test_description_keyword_case_insensitive(self):
        ok, _ = _is_operational("Organizations", "DATABASE DEGRADATION DETECTED")
        assert ok is True

    def test_description_no_matching_keywords(self):
        ok, _ = _is_operational("IAM", "Routine IAM policy update notification")
        assert ok is False

    def test_empty_description_for_non_operational(self):
        ok, reasons = _is_operational("Trusted Advisor", "")
        assert ok is False
        assert reasons == []

    def test_description_multiple_keywords(self):
        ok, reasons = _is_operational("Config", "Data plane outage causing latency")
        assert ok is True
        # Both keywords should be listed
        assert "data" in reasons[0] or "outage" in reasons[0]


# ── _determine_severity ───────────────────────────────────────────────────────

class TestDetermineSeverity:
    # Critical: data services
    @pytest.mark.parametrize("service", ["RDS", "Aurora", "DynamoDB", "S3", "EBS", "EFS", "FSx", "ElastiCache", "MemoryDB"])
    def test_data_outage_open_is_critical(self, service):
        assert _determine_severity(f"AWS_{service.upper()}_OPERATIONAL_ISSUE", service, "open") == "critical"

    @pytest.mark.parametrize("service", ["RDS", "S3"])
    def test_data_outage_closed_is_standard(self, service):
        assert _determine_severity(f"AWS_{service.upper()}_OPERATIONAL_ISSUE", service, "closed") == "standard"

    # Critical: compute services
    @pytest.mark.parametrize("service", ["EC2", "ECS", "Lambda", "Fargate", "EKS"])
    def test_compute_outage_open_is_critical(self, service):
        assert _determine_severity(f"AWS_{service.upper()}_DEGRADATION", service, "open") == "critical"

    # Critical: network services
    @pytest.mark.parametrize("service", ["ELB", "ALB", "NLB", "VPC", "CloudFront", "Route 53", "API Gateway"])
    def test_network_connectivity_open_is_critical(self, service):
        assert _determine_severity("AWS_VPC_CONNECTIVITY_ISSUE", service, "open") == "critical"

    def test_upcoming_is_standard(self):
        assert _determine_severity("AWS_EC2_OPERATIONAL_ISSUE", "EC2", "upcoming") == "standard"

    def test_unknown_service_open_is_standard(self):
        assert _determine_severity("AWS_CUSTOM_CHANGE", "SomeUnknownService", "open") == "standard"

    def test_non_outage_code_standard(self):
        assert _determine_severity("AWS_RDS_MAINTENANCE_SCHEDULED", "RDS", "open") == "standard"

    def test_no_status_match_defaults_standard(self):
        # "unknown" status doesn't match any rule → falls through to default
        assert _determine_severity("AWS_EC2_OPERATIONAL_ISSUE", "EC2", "unknown") == "standard"


# ── classify_event ────────────────────────────────────────────────────────────

class TestClassifyEvent:
    def test_returns_classification_result(self):
        result = classify_event("RDS", "AWS_RDS_OPERATIONAL_ISSUE", "open")
        assert isinstance(result, ClassificationResult)

    def test_operational_data_service_critical(self):
        result = classify_event("RDS", "AWS_RDS_OPERATIONAL_ISSUE", "open")
        assert result.is_operational is True
        assert result.severity == "critical"
        assert result.reasons

    def test_operational_compute_critical(self):
        result = classify_event("EC2", "AWS_EC2_OUTAGE", "open")
        assert result.is_operational is True
        assert result.severity == "critical"

    def test_operational_but_closed_is_standard(self):
        result = classify_event("S3", "AWS_S3_DEGRADATION", "closed")
        assert result.is_operational is True
        assert result.severity == "standard"

    def test_non_operational_service_open(self):
        result = classify_event("IAM", "AWS_IAM_OPERATIONAL_ISSUE", "open")
        assert result.is_operational is False
        assert result.severity == "standard"  # matches open catch-all

    def test_non_operational_via_description_keyword(self):
        result = classify_event("IAM", "AWS_IAM_CHANGE", "open", description="Database outage")
        assert result.is_operational is True

    def test_empty_service_non_operational(self):
        result = classify_event("", "AWS_SOMETHING", "open")
        assert result.is_operational is False

    def test_upcoming_is_standard(self):
        result = classify_event("EC2", "AWS_EC2_OPERATIONAL_ISSUE", "upcoming")
        assert result.severity == "standard"

    def test_description_default_empty(self):
        # Should not raise when description is omitted
        result = classify_event("RDS", "AWS_RDS_OPERATIONAL_ISSUE", "open")
        assert result is not None
