# Changelog

All notable changes to this project will be documented in this file.

The format follows common GitHub conventions inspired by **Keep a Changelog** and semantic versioning.

---

## [1.0.0] — 2026-03-15

Initial production release of **AWS Health Multi-Org Aggregator**.

This release introduces a VPC-only platform that aggregates AWS Health events across multiple AWS Organizations, exposes a unified REST API, performs operational event classification, sends correlated incident alerts, and generates daily Excel reports.

---

## Added

### Multi-Organization Health Aggregation
- Collector Lambda that polls AWS Health across multiple Organizations using delegated administration
- STS AssumeRole support for delegated admin accounts
- SSM-backed org registry for configuring target organizations
- DynamoDB-backed account metadata cache with 24-hour TTL
- 7-day sliding event window using DynamoDB TTL
- Per-org collection state tracking and metrics

### REST API
New IAM-authenticated API endpoints:

- `GET /v1/events`
- `GET /v1/events/{arn_b64}/details`
- `GET /v1/summary`
- `GET /v1/orgs`
- `POST /v1/export` (async Excel export)

API capabilities:
- Filtering by org, service, region, status, environment
- Pagination using encoded DynamoDB tokens
- Cross-org event merging by `event_arn`

### Event Classification
- Added `event_classifier.py`
- Flags events with:
  - `is_operational`
  - `severity` (`standard` or `critical`)
- Critical severity detection for open outage/degradation/connectivity events affecting compute, database, storage, and networking services

### Digest-Based Alerting
- Added `alert_dispatcher.py`
- Incident correlation to suppress AWS Health alert storms
- Service-level incident grouping
- Digest alerts after configurable accumulation window
- Update alerts when incident impact expands
- SNS message attributes for routing and automation

### Excel Reporting
- Exporter Lambda generating daily Excel reports in S3
- Workbook sheets:
  - Summary
  - Events
  - AffectedEntities
  - Delta_Latest
  - Delta_Log
  - Pivot_Service
  - Pivot_Account
  - Pivot_Region
- Delta tracking for newly opened and resolved events
- On-demand export via API endpoint

### Infrastructure
Terraform-managed infrastructure including:

- DynamoDB tables:
  - events
  - account metadata cache
  - collection state
- KMS CMK encryption for DynamoDB, SSM, S3, and Lambda environment variables
- Private Health Proxy API Gateway
- Consumer API Gateway with IAM authentication
- EventBridge schedules for collector and exporter
- S3 export bucket with lifecycle policies
- CloudWatch alarms and operational dashboard

### Networking
Full private-network deployment using VPC endpoints:

- execute-api
- dynamodb
- ssm
- sts
- logs
- sns
- s3
- lambda

AWS Health is accessed via a **private API Gateway proxy** because AWS Health does not provide a VPC interface endpoint.

### Testing and Tooling
- Pytest-based unit test structure with moto-based AWS mocking
- Operational scripts:
  - `deploy.sh`
  - `register_org.sh`
  - `test_collection.sh`

---

## Changed

### Collection Behavior
- Collection interval reduced **15 minutes → 5 minutes**
- Digest alert window reduced **30 minutes → 15 minutes**

This reduces worst-case alert latency to approximately **~20 minutes**.

### Alert Correlation
- Replaced per-event alert deduplication with **service-level incident correlation**
- Added configurable incident grouping window (`CORRELATION_WINDOW_MINUTES`)

### Monitoring
- CloudWatch alarms and dashboard updated to align with the **5-minute collection cycle**

### Infrastructure Simplification
- Removed WAF dependency
- API security handled via:
  - IAM SigV4 authentication
  - API Gateway IP allowlist resource policy

---

## Security

- All services encrypted with a dedicated **KMS CMK**
- API authentication via **IAM SigV4 only**
- API Gateway resource policies enforce **IP allowlists**
- Lambdas run in **private subnets with no internet egress**
- AWS services accessed exclusively via **VPC endpoints**
- S3 bucket policy enforces **TLS-only access**
- Cross-org access restricted via dedicated AssumeRole roles

---

## Known Issues / Limitations

- No real-time streaming support (polling model only)
- AWS Health categories currently limited to:
  - `issue`
  - `investigation`
- `scheduledChange` and `accountNotification` not yet supported
- No built-in CI/CD pipeline
- No Cognito authentication for browser-based access
- Latest export retrieval requires direct S3 access (no API endpoint yet)
- Event details lookup uses DynamoDB scan (acceptable for low-frequency detail queries)

---

## Future Work

Planned enhancements include:

- Support for `scheduledChange` and `accountNotification`
- Service/region alert suppression rules
- Presigned export download endpoint
- CI/CD pipeline automation
- Optional Cognito authentication
- DynamoDB provisioned capacity tuning
- Rich HTML alert notifications via SES
