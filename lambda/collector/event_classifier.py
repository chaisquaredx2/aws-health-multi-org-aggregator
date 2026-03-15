"""
event_classifier.py

Classifies collected AWS Health events as:
  - operational vs. control-plane (should consumers be alerted?)
  - severity: "standard" or "critical"

Rules are ported from aws-health-monitor and hardcoded as defaults.
Override via CLASSIFIER_RULES_SSM_PATH env var (JSON SecureString) if needed.

Operational services: compute, storage, databases, networking, monitoring.
Excluded (control plane): IAM, Organizations, CloudFormation, Config, etc.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

Severity = Literal["standard", "critical"]

# ── Default classification rules ──────────────────────────────────────────────

# Services whose events are considered "operational" (affect running workloads).
_OPERATIONAL_SERVICE_PATTERNS: List[str] = [
    r"^EC2$", r"^ECS$", r"^Lambda$", r"^Fargate$",
    r"^RDS$", r"^Aurora$", r"^DynamoDB$", r"^ElastiCache$", r"^MemoryDB$",
    r"^S3$", r"^EBS$", r"^EFS$", r"^FSx$",
    r"^ELB$", r"^ALB$", r"^NLB$", r"^VPC$", r"^CloudFront$", r"^Route 53$",
    r"^CloudWatch$", r"^CloudWatch Logs$",
    r"^Auto Scaling$", r"^Application Auto Scaling$",
    r"^CodeBuild$", r"^CodeDeploy$", r"^CodePipeline$",
    r"^SQS$", r"^SNS$", r"^Kinesis$", r"^MSK$",
    r"^EKS$", r"^EMR$", r"^Glue$", r"^Athena$",
    r"^Redshift$", r"^OpenSearch$", r"^ElasticSearch Service$",
    r"^API Gateway$", r"^AppSync$",
    r"^Secrets Manager$", r"^ACM$",
]

# Keywords in the event description that indicate an operational impact.
_OPERATIONAL_DESCRIPTION_KEYWORDS: List[str] = [
    "operational", "data", "storage", "database",
    "unavailable", "degraded", "performance", "failure",
    "outage", "scaling", "monitoring", "logging",
    "connectivity", "latency", "timeout", "disruption",
]

# Severity rules: (event_type_code_pattern, service_pattern, status) → severity
# Evaluated in order; first match wins.
@dataclass
class _SeverityRule:
    event_type_code_pattern: str
    service_pattern: str
    status_values: List[str]
    severity: Severity


_SEVERITY_RULES: List[_SeverityRule] = [
    # Critical: data-service outages/degradations while open
    _SeverityRule(
        event_type_code_pattern=r"(OPERATIONAL_ISSUE|OUTAGE|DEGRADATION|CONNECTIVITY)",
        service_pattern=r"^(RDS|Aurora|DynamoDB|S3|EBS|EFS|FSx|ElastiCache|MemoryDB)$",
        status_values=["open"],
        severity="critical",
    ),
    # Critical: compute outages while open
    _SeverityRule(
        event_type_code_pattern=r"(OPERATIONAL_ISSUE|OUTAGE|DEGRADATION)",
        service_pattern=r"^(EC2|ECS|Lambda|Fargate|EKS)$",
        status_values=["open"],
        severity="critical",
    ),
    # Critical: network outages
    _SeverityRule(
        event_type_code_pattern=r"(OPERATIONAL_ISSUE|OUTAGE|CONNECTIVITY|DEGRADATION)",
        service_pattern=r"^(ELB|ALB|NLB|VPC|CloudFront|Route 53|API Gateway)$",
        status_values=["open"],
        severity="critical",
    ),
    # Standard: everything else open
    _SeverityRule(
        event_type_code_pattern=r".*",
        service_pattern=r".*",
        status_values=["open"],
        severity="standard",
    ),
    # Standard: upcoming/closed changes
    _SeverityRule(
        event_type_code_pattern=r".*",
        service_pattern=r".*",
        status_values=["upcoming", "closed"],
        severity="standard",
    ),
]


# ── Classifier ────────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    is_operational: bool
    severity: Severity
    reasons: List[str] = field(default_factory=list)


def classify_event(
    service: str,
    event_type_code: str,
    status: str,
    description: str = "",
) -> ClassificationResult:
    """
    Classify a single event.

    Args:
        service:         AWS service name, e.g. "RDS"
        event_type_code: e.g. "AWS_RDS_OPERATIONAL_ISSUE"
        status:          "open", "closed", or "upcoming"
        description:     Optional event description text

    Returns:
        ClassificationResult with is_operational, severity, and reasons.
    """
    is_operational, reasons = _is_operational(service, description)
    severity = _determine_severity(event_type_code, service, status)

    return ClassificationResult(
        is_operational=is_operational,
        severity=severity,
        reasons=reasons,
    )


def _is_operational(service: str, description: str) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    for pattern in _OPERATIONAL_SERVICE_PATTERNS:
        if _matches(pattern, service):
            reasons.append(f"service matches {pattern}")
            return True, reasons

    if description:
        desc_lower = description.lower()
        matched = [kw for kw in _OPERATIONAL_DESCRIPTION_KEYWORDS if kw in desc_lower]
        if matched:
            reasons.append(f"description contains: {', '.join(matched)}")
            return True, reasons

    return False, reasons


def _determine_severity(event_type_code: str, service: str, status: str) -> Severity:
    for rule in _SEVERITY_RULES:
        if (
            _matches(rule.event_type_code_pattern, event_type_code)
            and _matches(rule.service_pattern, service)
            and status in rule.status_values
        ):
            return rule.severity
    return "standard"


def _matches(pattern: str, value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        return bool(re.search(pattern, value, re.IGNORECASE))
    except re.error:
        return pattern.lower() == value.lower()
