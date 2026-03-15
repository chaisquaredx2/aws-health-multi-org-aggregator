# AWS Health Multi-Org Aggregator

Aggregate AWS Health events across **multiple AWS Organizations** using delegated Health administration. Exposes a unified REST API with a 7-day sliding window, proactive alerting, and a daily Excel report.

---

## Features

| Capability | Details |
|---|---|
| **Multi-org collection** | Assumes IAM roles into each org's delegated Health admin account every 15 minutes |
| **Two event sections** | `issue` (confirmed disruptions) + `investigation` (AWS team investigating, scope unknown) |
| **REST API** | Events, summary, org status — filterable by service, region, status, environment, org |
| **Event classification** | Operational vs. control-plane; severity `standard` / `critical` |
| **Proactive alerts** | SNS publish after each cycle; triggers on us-east-1 or multi-region events; HIGH priority if >100 accounts |
| **Excel export** | Daily Lambda → S3: pivot tables by service/account/region, summary charts, delta (new opens / resolved) |
| **VPC-only** | All Lambdas are VPC-attached with no internet egress — AWS Health reached via private API GW proxy |

---

## Architecture

```
EventBridge (15 min)
      │
      ▼
┌─────────────────┐   STS AssumeRole   ┌─────────────────────────┐
│  Collector      │ ──────────────────▶ │  Org A delegated admin  │
│  Lambda (VPC)   │ ──────────────────▶ │  Org B delegated admin  │
│                 │ ──────────────────▶ │  Org N ...              │
└────────┬────────┘                     └─────────────────────────┘
         │ upsert
         ▼
┌─────────────────┐        ┌────────────────────────────────────┐
│  DynamoDB       │        │  Health Proxy API GW (private)     │
│  events table   │        │  AWS Service Integration (VTL)     │
│  (7-day TTL)    │        │  ▶ health.us-east-1.amazonaws.com  │
└────────┬────────┘        └────────────────────────────────────┘
         │                          ▲
         ├── API Lambda ────────────┘  (execute-api VPC endpoint)
         │         ▲
         │         └── API Gateway (REGIONAL) ◀── consumers
         │
         ├── Exporter Lambda (daily) ──▶ S3 (Excel report)
         │
         └── alert_dispatcher ──▶ SNS ──▶ PagerDuty / email / Slack
```

**Why a private API GW proxy for Health?**
AWS Health has no VPC Interface Endpoint. The private API Gateway with an AWS Service Integration acts as a proxy — Lambda calls it via the `execute-api` VPC endpoint; API GW calls `health.us-east-1.amazonaws.com` using its own managed-network credentials (VTL passthrough, Lambda loops on `nextToken`).

---

## Project Structure

```
aws-health-multi-org-aggregator/
├── frontend/
│   └── index.html                  # Single-file dashboard (Bootstrap 5, SigV4 via Web Crypto API)
├── lambda/
│   ├── collector/
│   │   ├── handler.py              # Entry point — fan-out per org, collect → DynamoDB
│   │   ├── health_proxy_client.py  # SigV4-signed calls to Health Proxy API GW
│   │   ├── event_classifier.py     # Operational flag + severity (standard/critical)
│   │   ├── alert_dispatcher.py     # SNS publish for new/changed operational events
│   │   ├── account_cache.py        # DynamoDB-backed 24h account metadata cache
│   │   └── org_registry.py         # SSM SecureString org config loader
│   ├── api/
│   │   ├── handler.py              # Routes API GW proxy events + OPTIONS preflight
│   │   ├── health_proxy_client.py  # Shared copy (synced by deploy.sh)
│   │   └── routes/
│   │       ├── events.py           # GET /v1/events, GET /v1/events/{arn}/details
│   │       ├── summary.py          # GET /v1/summary
│   │       └── orgs.py             # GET /v1/orgs
│   └── exporter/
│       ├── handler.py              # Daily Lambda: DynamoDB → Excel → S3
│       └── excel_writer.py         # Workbook builder (pivots, delta, charts)
├── terraform/
│   ├── api_gateway_health_proxy.tf # Private API GW + VTL AWS Service Integration
│   ├── api_key.tf                  # Consumer API resource policy (IAM auth + IP allowlist)
│   ├── dynamodb.tf                 # events, account-metadata, collection-state tables
│   ├── eventbridge.tf              # 15-min collector schedule + daily exporter schedule
│   ├── iam.tf                      # IAM roles + dashboard-consumer managed policy
│   ├── kms.tf                      # Single KMS key for DynamoDB, SSM, S3, Lambda env
│   ├── lambda.tf                   # Lambda functions + consumer API GW (AWS_IAM auth)
│   ├── locals.tf                   # Resolves create_vpc / bring-your-own VPC + SNS
│   ├── monitoring.tf               # CloudWatch alarms + dashboard
│   ├── networking.tf               # Optional VPC + subnets (create_vpc = true)
│   ├── outputs.tf
│   ├── s3.tf                       # KMS-encrypted export bucket
│   ├── ssm_documents.tf            # SSM Automation docs + execution role
│   ├── variables.tf
│   ├── vpc_endpoints.tf            # execute-api, DynamoDB, SSM, STS, logs, SNS, S3
│   ├── waf.tf                      # WAF WebACL on consumer API GW only
│   └── terraform.tfvars.example
├── ssm/
│   ├── HealthAggregator-RegisterOrg.yaml    # Add/update/remove org in SSM registry
│   └── HealthAggregator-TestCollection.yaml # Trigger collector + fetch metrics/logs
├── docs/
│   ├── api-contract.md
│   └── data-model.md
├── scripts/
│   ├── deploy.sh                   # pip install + terraform plan/apply
│   ├── register_org.sh             # CLI wrapper for RegisterOrg SSM document
│   └── test_collection.sh          # CLI wrapper for TestCollection SSM document
└── SPEC.md                         # Full system specification
```

---

## Prerequisites

- AWS Organizations with [delegated Health administration](https://docs.aws.amazon.com/health/latest/ug/aggregate-events.html) enabled
- An IAM role in each org's delegated admin account (default name: `HealthAggregatorReadRole`) that trusts the aggregator account
- Terraform ≥ 1.5
- Python ≥ 3.12 and `pip`
- AWS CLI configured for the aggregator account
- An existing VPC with private subnets (no internet egress required)

---

## Quick Start

### 1. Configure

Copy the example and fill in your values:

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Key settings to fill in:

```hcl
create_vpc              = false          # use existing VPC
vpc_id                  = "vpc-..."
private_subnet_ids      = ["subnet-...", "subnet-..."]
private_subnet_cidrs    = ["10.0.1.0/24", "10.0.2.0/24"]
private_route_table_ids = ["rtb-...", "rtb-..."]

consumer_api_allowed_cidrs = ["203.0.113.0/24"]  # your egress IP(s)
```

### 2. Deploy

```bash
./scripts/deploy.sh
```

This runs `pip install` for all three Lambda packages, then `terraform init / plan / apply`.

### 3. Register orgs

Use the **SSM Automation document** in the AWS Console (Systems Manager → Automation → `HealthAggregator-RegisterOrg`) or via CLI:

```bash
aws ssm start-automation-execution \
  --document-name HealthAggregator-RegisterOrg \
  --parameters Action=AddOrUpdate,OrgId=o-abc123def45,OrgName="Acme Corp",AccountId=123456789012
```

### 4. Verify collection

Use the **SSM Automation document** `HealthAggregator-TestCollection` (AWS Console), or via CLI:

```bash
aws ssm start-automation-execution \
  --document-name HealthAggregator-TestCollection \
  --parameters FunctionName=health-aggregator-collector
```

### 5. Grant dashboard access

Attach the managed IAM policy to any user or role that needs to query the API:

```bash
aws iam attach-user-policy \
  --user-name <username> \
  --policy-arn $(terraform -chdir=terraform output -raw dashboard_consumer_policy_arn)
```

### 6. Query the API

```bash
# List open issues (SigV4-signed with aws-curl)
curl -s \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  "https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/v1/events?category=issue&status=open" \
  | python3 -m json.tool

# Summary
curl -s \
  --aws-sigv4 "aws:amz:us-east-1:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  "https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/v1/summary" \
  | python3 -m json.tool
```

Or open `frontend/index.html` in a browser and enter your AWS credentials in the settings modal — it signs requests with SigV4 via the Web Crypto API.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/events` | List events — filter by `category`, `status`, `service`, `region`, `org_id`, `environment` |
| `GET` | `/v1/events/{arn_b64}/details` | Full event detail, merged across orgs |
| `GET` | `/v1/summary` | Counts by category, top services, top regions, per-org breakdown |
| `GET` | `/v1/orgs` | Org registry + last collection state per org |

Query parameters for `/v1/events`:

| Parameter | Values | Default |
|---|---|---|
| `category` | `issue` \| `investigation` | required |
| `status` | `open` \| `closed` \| `upcoming` | all |
| `service` | e.g. `RDS`, `EC2` | all |
| `region` | e.g. `us-east-1` | all |
| `org_id` | org ID | all |
| `environment` | `production` \| `non-production` | all |
| `window_days` | 1–7 | 7 |
| `page_size` | 1–200 | 100 |
| `next_token` | pagination cursor | — |

---

## Event Classification

Each collected event is tagged by `event_classifier.py`:

| Field | Values | Description |
|---|---|---|
| `is_operational` | `true` / `false` | Affects running workloads (compute, storage, DB, network). Control-plane events (IAM, Orgs, CFN) are `false`. |
| `severity` | `standard` / `critical` | `critical` for OPERATIONAL_ISSUE / OUTAGE / DEGRADATION on data services (RDS, DynamoDB, S3, EBS) or compute (EC2, ECS, Lambda, EKS) while `open`. |

---

## Alerting

`alert_dispatcher.py` runs after every collection cycle. It publishes a structured message to the configured SNS topic.

**Trigger criteria** (from `aws-health-monitor`):
- Any new operational event is in `us-east-1`, **OR**
- New operational events span **2+ regions**

**Priority:**
- `HIGH` — multi-region AND more than 100 affected accounts
- `STANDARD` — everything else

**Delivery:** Lambda publishes to SNS via the SNS VPC endpoint. SNS delivers from AWS's managed network — add PagerDuty, email, or Slack subscriptions directly to the SNS topic.

**Deduplication:** Events already alerted with the same status are suppressed (tracked in the `collection-state` DynamoDB table).

Configure in `terraform.tfvars`:

```hcl
health_alert_sns_topic_arn = "arn:aws:sns:us-east-1:123456789012:health-event-alerts"
alerts_enabled             = true
```

---

## Excel Export

The exporter Lambda runs daily and uploads a workbook to S3:

```
s3://<bucket>/exports/YYYY/MM/DD/aws-health-events.xlsx
```

**Sheets:**

| Sheet | Contents |
|---|---|
| `Summary` | KPI counts, status chart, top-services chart, delta summary, navigation links |
| `Events` | One row per (event × org), all fields, Excel table with filters |
| `AffectedEntities` | Denormalized — one row per (event × org × account) |
| `Pivot_Service` | Pivot table: service × status, filterable by org/region/category/severity |
| `Pivot_Account` | Pivot table: account × status |
| `Pivot_Region` | Pivot table: region × status |
| `Delta_Latest` | New-open events + resolved events since the previous run |
| `Delta_Log` | Rolling history of all delta runs |

Trigger an on-demand export:

```bash
aws lambda invoke \
  --function-name health-aggregator-exporter \
  --invocation-type RequestResponse \
  /tmp/export-response.json && cat /tmp/export-response.json
```

---

## VPC Endpoints

All deployed in the configured private subnets (no internet egress required):

| Endpoint | Type | Used by |
|---|---|---|
| `execute-api` | Interface | Lambda → Health Proxy API GW |
| `dynamodb` | Gateway (free) | All Lambdas → DynamoDB |
| `ssm` | Interface | Collector → SSM org registry |
| `sts` | Interface | Collector → STS AssumeRole |
| `logs` | Interface | All Lambdas → CloudWatch Logs |
| `sns` | Interface | Collector → SNS alert publish |
| `s3` | Gateway (free) | Exporter → S3 Excel upload |

---

## Monitoring

CloudWatch alarms (routed to `alarm_sns_topic_arn`):

| Alarm | Threshold |
|---|---|
| Collector Lambda errors | > 0 over 2 × 15-min periods |
| Collector duration (p95) | > 80% of timeout over 3 periods |
| Custom `CollectionErrors` metric | > 0 (any org failed) |
| `EventsCollected` = 0 | ≤ 0 over 1 hour (breaching if collector didn't run) |
| API Lambda errors | > 5 over 5 minutes |
| API p99 latency | > 3 s over 3 periods |
| Health Proxy 4xx / 5xx | > 0 / > 5 over 15 minutes |
| DynamoDB throttles | > 10 over 3 periods |

CloudWatch Dashboard: `health-aggregator` — 6 metric widgets across collection health, proxy errors, API performance, and DynamoDB capacity.

---

## Configuration Reference

Key variables in `terraform.tfvars`:

```hcl
# ── VPC (Option A: create new)
create_vpc = true
vpc_cidr   = "10.0.0.0/16"

# ── VPC (Option B: bring your own)
create_vpc              = false
vpc_id                  = "vpc-..."
private_subnet_ids      = ["subnet-...", "subnet-..."]
private_subnet_cidrs    = ["10.0.1.0/24", "10.0.2.0/24"]
private_route_table_ids = ["rtb-...", "rtb-..."]

# ── API access control
consumer_api_allowed_cidrs = ["203.0.113.0/24"]  # restrict to your egress IP(s)

# ── Cross-org IAM
cross_org_role_name = "HealthAggregatorReadRole"  # must exist in every delegated admin account

# ── SNS (Option A: create new topics)
create_alarm_topic = true
create_alert_topic = true

# ── SNS (Option B: bring your own)
create_alarm_topic         = false
alarm_sns_topic_arn        = "arn:aws:sns:..."
create_alert_topic         = false
health_alert_sns_topic_arn = "arn:aws:sns:..."

# ── Alerting
alerts_enabled = true

# ── Excel export
excel_export_enabled  = true
excel_export_schedule = "rate(1 day)"
export_retention_days = 90

# ── Tuning (defaults are fine for most deployments)
collection_window_days  = 7
collection_schedule     = "rate(15 minutes)"
max_concurrent_orgs     = 5
account_cache_ttl_hours = 24
log_retention_days      = 90
```

---

## Multi-Org IAM Setup

Create `HealthAggregatorReadRole` in **each org's delegated Health admin account**:

**Trust policy** (trusts the aggregator account):
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<AGGREGATOR_ACCOUNT_ID>:root" },
    "Action": "sts:AssumeRole"
  }]
}
```

**Permission policy:**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["organizations:ListAccounts", "organizations:ListTagsForResource"],
    "Resource": "*"
  }]
}
```

> The Health API permissions live on the `health_proxy_apigw` role (in the aggregator account), which must itself be registered as delegated Health admin for the target org.

---

## License

MIT
