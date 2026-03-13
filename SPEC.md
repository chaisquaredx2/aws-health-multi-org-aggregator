# AWS Health Multi-Org Aggregator — Project Specification

## 1. Purpose

Aggregate AWS Health events across **multiple AWS Organizations** using delegated Health administration. Expose a unified REST API (API Gateway + Lambda) that presents events in two categories over a rolling 7-day window:

| Section | AWS `eventTypeCategory` | Description |
|---|---|---|
| **Issues / Incidents** | `issue` | Active or recently resolved service disruptions and outages |
| **Investigations** | `investigation` | AWS service team is investigating a potential problem; root cause and impact are not yet fully known |

> **Why two sections?** The AWS Health console itself separates these. "Investigations" represent early-signal events where AWS has detected an anomaly but cannot yet confirm scope or impact. Surface them separately so operators can begin awareness without assuming a confirmed outage.

---

## 2. Goals and Non-Goals

### Goals
- Poll AWS Health for **N configured organizations** by assuming IAM roles into each org's delegated Health admin account.
- Collect both `issue` and `investigation` event categories.
- Store events in DynamoDB with a **7-day TTL** and serve them through a time-windowed API (sliding `now - 7d` to `now`).
- Allow filtering by org, AWS service, region, status, and environment (production vs non-production).
- Return affected account metadata (account name, business unit, environment) enriched from each org's AWS Organizations.
- Provide a summary endpoint for quick dashboard rendering (counts, top services, per-org breakdown).

### Non-Goals
- Real-time push/streaming (EventBridge Health events are not in scope for v1; polling is sufficient).
- Frontend/UI — the API is the boundary; a separate consumer builds the dashboard.
- Cross-org event deduplication at the storage layer — each org's view of an event is stored as a separate DynamoDB item. Merging happens at the API response layer (see Section 9).
- Scheduled-change (`scheduledChange`) or account-notification (`accountNotification`) categories in v1 (can add later).

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Aggregator Account                        │
│                                                             │
│  EventBridge (cron)                                         │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────────┐    AssumeRole    ┌──────────────────────┐  │
│  │  Collector  │ ────────────────▶│  Org A Delegated     │  │
│  │   Lambda    │                  │  Health Admin Acct   │  │
│  │             │ ────────────────▶│  Org B Delegated     │  │
│  │  (fan-out   │                  │  Health Admin Acct   │  │
│  │   per org)  │ ────────────────▶│  Org N ...           │  │
│  └──────┬──────┘                  └──────────────────────┘  │
│         │                                                   │
│         │ upsert (batch_writer)                             │
│         ▼                                                   │
│  ┌─────────────┐                                            │
│  │  DynamoDB   │                                            │
│  │  health-    │                                            │
│  │  events     │                                            │
│  └──────┬──────┘                                            │
│         │                                                   │
│  ┌──────┴──────┐                                            │
│  │  API Lambda │◀── API Gateway REST API ◀── consumers      │
│  └─────────────┘                                            │
│                                                             │
│  SSM Parameter Store: org registry                          │
│  KMS: table + lambda env encryption                         │
│  WAF: API Gateway protection                                │
└─────────────────────────────────────────────────────────────┘
```

### Collection Flow

1. EventBridge scheduled rule triggers **Collector Lambda** every 30 minutes.
2. Collector reads the **org registry** from SSM Parameter Store — a JSON list of org configurations.
3. For each org, Collector assumes the configured **cross-account IAM role** in the org's delegated Health admin account.
4. Using the assumed credentials, Collector calls `describe_events_for_organization` with:
   - `eventTypeCategories: [issue, investigation]`
   - `lastUpdatedTimes: [{from: now - 7d}]` — sliding window filter at source
5. For each event, Collector calls `describe_affected_accounts_for_organization` to get the account list.
6. Collector calls `list_accounts` (or `describe_affected_entities_for_organization`) on the org to enrich accounts with metadata.
7. Enriched event records are **upserted** into DynamoDB (put_item overwrites stale data with fresh `last_updated_time`).
8. TTL is set to `now + 7 days` on every upsert, so records expire automatically.

### Query Flow

1. API consumer sends `GET /v1/events?category=issue&...` to API Gateway.
2. API Gateway invokes **API Lambda**.
3. API Lambda queries DynamoDB GSI `category-starttime-index` with a `KeyConditionExpression` bounding `start_time >= now - 7d`.
4. Results are filtered/projected and returned as JSON.

---

## 4. Multi-Org Configuration

Org registry is stored in SSM Parameter Store as a JSON array at:

```
/health-aggregator/orgs
```

Each entry:

```json
{
  "org_id": "o-abc123def456",
  "org_name": "Acme Corp",
  "delegated_admin_account_id": "123456789012",
  "assume_role_arn": "arn:aws:iam::123456789012:role/HealthAggregatorReadRole",
  "assume_role_external_id": "optional-external-id",
  "enabled": true
}
```

**Trust relationship on the remote role** (`HealthAggregatorReadRole`) must trust the aggregator account's Collector Lambda execution role:

```json
{
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::<aggregator-account-id>:role/health-aggregator-collector-role"
  },
  "Action": "sts:AssumeRole",
  "Condition": {
    "StringEquals": {
      "sts:ExternalId": "<external-id>"
    }
  }
}
```

**Minimum permissions on the remote role:**

```json
{
  "health:DescribeEventsForOrganization",
  "health:DescribeEventDetailsForOrganization",
  "health:DescribeAffectedAccountsForOrganization",
  "health:DescribeAffectedEntitiesForOrganization",
  "organizations:ListAccounts",
  "organizations:ListTagsForResource"
}
```

> Note: `health:*` for organizational view requires the Health API call to be made to `us-east-1` endpoint (`health.us-east-1.amazonaws.com`). This is an AWS constraint regardless of which region your accounts are in.

---

## 5. Event Categories In Depth

### 5.1 Issues (`eventTypeCategory: issue`)

- **What it is**: Confirmed service disruptions, degradations, or outages.
- **Status codes to include**: `open`, `upcoming`, `closed` (closed only if within the 7-day window).
- **Operator action**: Assess impact on their accounts, prepare communications, apply workarounds.

### 5.2 Investigations (`eventTypeCategory: investigation`)

- **What it is**: AWS service teams have detected a signal (elevated error rates, anomalous metrics, customer reports) and opened an investigation. Impact scope, affected regions, and root cause are not yet fully known.
- **Status codes to include**: `open`, `closed` (if within 7-day window).
- **Operator action**: Monitor for escalation; pre-emptively check service health; no confirmed impact yet but awareness is valuable.
- **UI guidance**: Display with lower visual severity than issues (e.g., amber/yellow vs red). Label clearly as "Under Investigation" with a tooltip explaining AWS may not have full details yet.

### Category Mapping Reference

| AWS API value | Console label | API section | v1 included |
|---|---|---|---|
| `issue` | Issues | Issues/Incidents | Yes |
| `investigation` | Other notifications | Investigations | Yes |
| `scheduledChange` | Scheduled changes | — | No (v2) |
| `accountNotification` | Other notifications | — | No (v2) |

---

## 6. 7-Day Sliding Window

### Collection-side window

Collector applies the window at the source API call:

```python
from datetime import datetime, timedelta, timezone

window_start = datetime.now(timezone.utc) - timedelta(days=7)

filter = {
    'eventTypeCategories': ['issue', 'investigation'],
    'lastUpdatedTimes': [{'from': window_start}]
}
```

This reduces data transferred and avoids storing stale events.

### Storage TTL

Every upserted record sets:

```python
'ttl': int(time.time()) + (7 * 24 * 60 * 60)  # 7 days from now
```

DynamoDB automatically deletes expired records. Since we upsert on every collection cycle, an event that stays open for 10 days will continuously have its TTL refreshed — it will remain in the table as long as it was updated in the last 7 days.

### Query-side window

API queries add a time-bound even though TTL handles eventual cleanup (TTL deletion is not instantaneous in DynamoDB):

```python
window_start_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

KeyConditionExpression = (
    Key('category').eq(category) &
    Key('start_time').gte(window_start_iso)
)
```

### Window Boundary Behavior

| Event state | Behavior |
|---|---|
| Open, started 3 days ago | Included |
| Open, started 10 days ago, last updated 1 day ago | Included (TTL refreshed, within window via `lastUpdatedTime` filter) |
| Closed 8 days ago | Excluded (TTL expired, not collected) |
| Closed 2 days ago | Included (within 7-day window) |

---

## 7. Lambda Functions

### 7.1 Collector Lambda

**Trigger**: EventBridge scheduled rule — every 15 minutes

**Runtime**: Python 3.12

**Environment variables**:
- `TABLE_NAME` — DynamoDB events table name
- `ACCOUNT_METADATA_TABLE_NAME` — DynamoDB account metadata cache table name
- `ORG_REGISTRY_PATH` — SSM path (default `/health-aggregator/orgs`)
- `COLLECTION_WINDOW_DAYS` — default `7`
- `MAX_CONCURRENT_ORGS` — default `5` (fan-out parallelism with `ThreadPoolExecutor`)
- `ACCOUNT_CACHE_TTL_HOURS` — default `24`

**Algorithm**:

```
load_orgs_from_ssm()
for each enabled org (parallel, up to MAX_CONCURRENT_ORGS):
    credentials = sts.assume_role(org.assume_role_arn)
    health_client = boto3.client('health', credentials=credentials, region='us-east-1')
    orgs_client  = boto3.client('organizations', credentials=credentials)

    account_map = load_account_map(org_id)
    # load_account_map: batch-get from account-metadata cache table first;
    # fetch missing entries from orgs_client.list_accounts + list_tags_for_resource;
    # write newly fetched entries to cache with TTL = now + 24h
    events = paginate(health_client.describe_events_for_organization, filter={
        categories: [issue, investigation],
        lastUpdatedTimes: [{from: now - 7d}]
    })

    for event in events:
        affected = health_client.describe_affected_accounts_for_organization(event.arn)
        record = build_record(event, org, affected, account_map)
        upsert_to_dynamodb(record)
```

**Error handling**:
- Per-org errors are caught and logged; failure of one org does not abort others.
- CloudWatch metric `CollectionErrors` incremented per failure.
- Lambda timeout set to 5 minutes; large orgs may need pagination chunking.

### 7.2 API Lambda

**Trigger**: API Gateway REST API

**Runtime**: Python 3.12

**Environment variables**:
- `TABLE_NAME` — DynamoDB table name
- `COLLECTION_WINDOW_DAYS` — default `7`

**Routes handled** (via path parameter from API GW):

| Method | Path | Handler function |
|---|---|---|
| GET | /v1/events | `list_events` |
| GET | /v1/events/{event_arn_encoded}/details | `get_event_details` |
| GET | /v1/summary | `get_summary` |
| GET | /v1/orgs | `list_orgs` |

---

## 8. DynamoDB Schema

See [docs/data-model.md](docs/data-model.md) for full schema with GSI definitions and example items.

**Tables**: `health-aggregator-events`, `health-aggregator-account-metadata`, `health-aggregator-collection-state`

**Primary key**:
- Partition key: `pk` = `{event_arn}#{org_id}` (String)
- Sort key: `sk` = `{category}#{start_time_iso}` (String)

**Global Secondary Indexes**:

| Index name | PK | SK | Purpose |
|---|---|---|---|
| `category-starttime-index` | `category` (S) | `start_time` (S) | Sliding window queries by category |
| `org-lastupdate-index` | `org_id` (S) | `last_updated_time` (S) | Per-org event listing |

**TTL attribute**: `ttl` (Number, Unix timestamp)

---

## 9. API Contract

See [docs/api-contract.md](docs/api-contract.md) for full request/response shapes.

### Auth

Two modes, both supported simultaneously via API GW resource policy:

1. **IAM SigV4** (default for service-to-service consumers — other Lambdas, SDK clients, CLI tools). API GW method auth = `AWS_IAM`. Callers need `execute-api:Invoke` permission on the resource ARN.
2. **API key** (opt-in for lightweight/external consumers). Enabled via API GW Usage Plan. Callers pass `x-api-key` header. API keys are scoped to a Usage Plan that enforces the same WAF rate limits.

Cognito is not used in v1 — no user-facing frontend is in scope.

### Cross-Org Event Merging

The same global event ARN can affect accounts in multiple organizations. Storage keeps per-org records (PK = `event_arn#org_id`) for auditability. The **API response merges records with the same `event_arn`** into a single event object:

- `affected_orgs` — array, one entry per org that saw this event
- Each entry contains `org_id`, `org_name`, `affected_accounts[]`
- Top-level `affected_account_count` is the sum across all orgs
- Event metadata (`service`, `region`, `status`, etc.) is taken from the most-recently-updated org record (they should be identical)

This is the default behavior. No opt-in param required.

### Quick Reference

```
GET /v1/events
  ?category=issue|investigation          (required)
  &window_days=7                         (optional, default 7, max 7)
  &org_id=o-abc123                       (optional, scopes merge to one org)
  &service=EC2                           (optional)
  &region=us-east-1                      (optional)
  &status=open|closed|upcoming           (optional, repeatable)
  &environment=production|non-production (optional)

GET /v1/events/{event_arn_b64}/details
  # event_arn base64URL-encoded to avoid slash characters in path
  ?org_id=o-abc123                       (optional; if omitted, uses first matching org)

GET /v1/summary
  ?category=issue|investigation|all      (optional, default all)
  &org_id=o-abc123                       (optional)

GET /v1/orgs
  # Lists configured orgs with last collection timestamp and event counts
```

### Response envelope

All list responses follow:

```json
{
  "meta": {
    "window_start": "2026-03-06T12:00:00Z",
    "window_end":   "2026-03-13T12:00:00Z",
    "total":        42,
    "page":         1,
    "page_size":    100,
    "next_token":   "base64-encoded-lastkey-or-null"
  },
  "data": [ ... ]
}
```

---

## 10. Infrastructure (Terraform)

### Lambda VPC Configuration

**Company requirement: all Lambdas must be VPC-attached.**

This creates a constraint: the AWS Health API (`health.us-east-1.amazonaws.com`) has **no VPC Interface Endpoint**. A VPC-bound Lambda with no internet egress cannot reach it directly. Three egress patterns are available:

---

#### Pattern A: NAT Gateway _(preferred if allowed)_

```
Lambda (private subnet)
  → route table → NAT Gateway (public subnet)
    → Internet Gateway → health.us-east-1.amazonaws.com
```

- Lambda code unchanged — normal Boto3 paginators work.
- Full pagination, full SDK feature set.
- Cost: ~$32–45/month per NAT GW + data transfer.
- **Recommended first choice.** "Must be in VPC" and "must have internet egress" are separate requirements — NAT GW is the standard AWS egress pattern for VPC workloads. Confirm whether NAT GW is permitted before ruling it out.

---

#### Pattern B: HTTP Forward Proxy _(not recommended — no AWS managed option)_

```
Lambda (private subnet, HTTPS_PROXY env var set)
  → proxy (Squid/Nginx on Fargate, private subnet)
    → NAT Gateway (on proxy's subnet) → health.us-east-1.amazonaws.com
```

- AWS SDK honours `HTTPS_PROXY` / `HTTP_PROXY` environment variables transparently — Lambda code unchanged.
- **AWS does not provide a managed HTTP forward proxy.** This requires self-managed Squid or Nginx on EC2/Fargate: patching, scaling, and availability are your responsibility.
- Still requires a NAT Gateway on the proxy's subnet.
- Operationally more complex than Pattern A for no architectural benefit over A.
- **Only consider if** an existing corporate proxy already exists in the VPC (e.g., Zscaler, Palo Alto via Prisma) — in that case it is free to use and Lambda just sets `HTTPS_PROXY`.

Lambda environment variable (if a corporate proxy exists):
```
HTTPS_PROXY=http://<existing-proxy-dns>:3128
NO_PROXY=169.254.169.254,169.254.170.2  # exclude IMDSv2 and ECS credential provider
```

---

#### Pattern C: API Gateway AWS Service Integration _(last resort — no internet egress at all)_

```
Lambda (VPC)
  → execute-api VPC Interface Endpoint [private]
    → API Gateway REST API
      → AWS Service integration (API GW IAM role signs requests)
        → health.us-east-1.amazonaws.com  [API GW runs in AWS public network]
```

The `execute-api` service has a VPC Interface Endpoint, so Lambda-to-API GW is fully private. API GW makes the Health API call from AWS's own network (outside the customer VPC), bypassing the VPC egress constraint.

**Trade-offs:**
- One API Gateway resource per Health API method (4 methods needed: `DescribeEventsForOrganization`, `DescribeAffectedAccountsForOrganization`, `DescribeEventDetailsForOrganization`, `DescribeAffectedEntitiesForOrganization`).
- Request/response mapping via VTL for each method.
- **Pagination must be handled by Lambda looping** — API GW makes one downstream call per invocation, so Lambda calls API GW repeatedly until `nextToken` is absent. This adds one HTTP round-trip per page (vs one in-process SDK call with Pattern A/B).
- Higher latency per collection run; more complex Lambda code.
- API GW execution role needs `health:Describe*` permissions.

**Additional VPC endpoints required for Pattern C:**

| Service | Endpoint type | Why |
|---|---|---|
| `execute-api` | Interface | Lambda → API GW (private) |
| `dynamodb` | Gateway (free) | Lambda → DynamoDB (private) |
| `ssm` | Interface | Lambda → SSM (private) |
| `sts` | Interface | Lambda → STS AssumeRole (private) |

---

#### Pattern D (evaluated, rejected): SSM Automation `aws:executeAwsApi`

SSM Automation's `aws:executeAwsApi` action runs in **AWS-managed infrastructure outside the customer VPC** and can reach `health.us-east-1.amazonaws.com` without internet egress. Lambda (in VPC) triggers it via the SSM VPC endpoint. This was evaluated as a potential simplification over Pattern C.

```
Lambda (VPC) → ssm VPC endpoint → SSM Automation (AWS-managed) → health.us-east-1.amazonaws.com
```

**Rejected for three reasons:**

1. **Async only with polling overhead.** `StartAutomationExecution` is fire-and-forget. Lambda must poll `GetAutomationExecution` in a loop, adding 15–60+ seconds of mandatory wait per collection run on top of the actual API call latency.

2. **Output size limits.** SSM Automation step string outputs are truncated at ~100 events worth of JSON. A single paginated Health API response across a large org exceeds this silently — data is lost with no error raised. The API GW approach is size-unbounded (Lambda holds results in memory).

3. **Pagination complexity.** `aws:executeAwsApi` makes one call. Looping on `nextToken` requires `aws:loop` + conditional branch steps in SSM YAML — more complex and less testable than a Python `while` loop in Lambda.

`aws:executeScript` (Python inside SSM) could avoid #3 but moves business logic into SSM YAML runbooks, making local unit testing impossible and iteration slower.

**Verdict: not simpler than Pattern C.** SSM Automation is suitable for operational runbooks (remediations, one-off tasks); it is not well-suited as a synchronous data-fetching intermediary for a 15-minute polling loop.

---

#### Decision — **Pattern C selected**

NAT Gateway and internet egress are not permitted by company policy. AWS does not offer a managed HTTP forward proxy. SSM Automation evaluated and rejected (see Pattern D above). **Pattern C (API Gateway AWS Service Integration) is the chosen approach.**

Implementation files:
- `terraform/api_gateway_health_proxy.tf` — private API GW, 4 methods, VTL mappings
- `terraform/vpc_endpoints.tf` — execute-api, DynamoDB, SSM, STS, CloudWatch Logs endpoints
- `lambda/collector/health_proxy_client.py` — SigV4 signing + pagination loops

The implementation section below is written for **Pattern A/B** (Boto3 SDK used directly in Lambda). If Pattern C is chosen, the collector's `health_collector.py` must be rewritten to call API GW endpoints rather than the AWS SDK, and a `terraform/api_gateway_health_proxy.tf` module must be added.

---

**VPC endpoints that should exist in the VPC regardless of pattern chosen:**

| Service | Endpoint type | Notes |
|---|---|---|
| `dynamodb` | Gateway (free) | Lambda → DynamoDB without traversing NAT |
| `ssm` | Interface | Lambda → SSM Parameter Store |
| `sts` | Interface | Lambda → AssumeRole for cross-org access |

### AWS Services

| Service | Purpose |
|---|---|
| API Gateway (REST) | HTTPS endpoint, WAF integration, usage plans |
| Lambda (x2) | Collector, API handler — VPC-attached (company requirement); egress pattern TBD |
| DynamoDB | Event store with TTL + GSIs |
| EventBridge | Scheduled trigger for collector |
| SSM Parameter Store | Org registry (SecureString) |
| STS | Cross-account role assumption |
| KMS | DynamoDB, SSM, Lambda env encryption |
| WAF v2 | Rate limiting, AWS managed rules on API GW |
| CloudWatch | Logs, metrics, alarms |
| SNS | Alert notifications |
| IAM | Least-privilege roles |

### Directory Structure (planned)

```
aws-health-multi-org-aggregator/
├── SPEC.md
├── docs/
│   ├── api-contract.md
│   └── data-model.md
├── lambda/
│   ├── collector/
│   │   ├── handler.py
│   │   ├── org_registry.py
│   │   ├── health_collector.py
│   │   └── requirements.txt
│   └── api/
│       ├── handler.py
│       ├── routes/
│       │   ├── events.py
│       │   ├── summary.py
│       │   └── orgs.py
│       └── requirements.txt
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── api_gateway.tf
│   ├── lambda.tf
│   ├── dynamodb.tf
│   ├── eventbridge.tf
│   ├── iam.tf
│   ├── kms.tf
│   ├── waf.tf
│   ├── monitoring.tf
│   └── terraform.tfvars.example
└── scripts/
    ├── deploy.sh
    ├── register_org.sh       # helper to add an org to SSM registry
    └── test_collection.sh    # trigger collector manually
```

---

## 11. Security Design

### IAM Least Privilege

**Collector Lambda execution role** (aggregator account):
- `sts:AssumeRole` scoped to registered role ARNs only
- `ssm:GetParameter` on `/health-aggregator/*`
- `dynamodb:PutItem`, `dynamodb:BatchWriteItem` on the events table
- `kms:Decrypt`, `kms:GenerateDataKey` on the CMK

**API Lambda execution role** (aggregator account):
- `dynamodb:Query`, `dynamodb:GetItem`, `dynamodb:Scan` on the events table (read-only)
- `ssm:GetParameter` on `/health-aggregator/orgs` (for org list endpoint)
- `kms:Decrypt` on the CMK

**Cross-account remote role** (per org, in delegated admin account):
- Minimum permissions listed in Section 4
- Trust policy scoped to collector role ARN + ExternalId

### Network Security

Both Lambdas are VPC-attached (company requirement). Egress to the AWS Health API is provided by one of the patterns in Section 10 (NAT GW, HTTP proxy, or API GW integration — to be confirmed). Security controls:

- **VPC** — Lambdas in private subnets; no direct inbound internet path.
- **IAM** — least-privilege execution roles; Lambdas can only call the specific API actions they need.
- **Resource-based policies** — DynamoDB table policy restricts access to the two Lambda execution role ARNs only.
- **KMS** — all data encrypted at rest; key policy restricts `Decrypt` to the Lambda execution roles.
- **TLS** — all AWS SDK calls use TLS 1.2+ by default; API Gateway enforces TLS 1.2+.
- **WAF** — rate limiting and managed rule sets on the API Gateway endpoint.
- **CloudTrail** — all API calls (Health, STS AssumeRole, DynamoDB writes) are logged for audit.

### Encryption
- DynamoDB: KMS CMK (customer-managed key)
- SSM SecureString: KMS CMK
- Lambda environment variables: KMS CMK
- In-transit: TLS 1.2+ enforced on all AWS SDK calls and API Gateway

### WAF Rules on API Gateway
- AWS Managed Rules: `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`
- Rate limit: 1000 requests per 5 minutes per IP
- Geo-block: optional (configurable via variable)

---

## 12. Monitoring and Alerting

### CloudWatch Metrics (custom)
- `HealthAggregator/CollectionErrors` — per org, per run
- `HealthAggregator/EventsCollected` — count per run
- `HealthAggregator/OrgCollectionDuration` — latency per org

### Alarms
- Collector Lambda error rate > 0 for 2 consecutive periods → SNS alert
- API Lambda p99 duration > 3s → SNS alert
- DynamoDB `SystemErrors` > 0 → SNS alert
- `CollectionErrors` > 0 → SNS alert

### Dashboard
- CloudWatch Dashboard with: events collected over time, errors by org, API latency, DynamoDB capacity

---

## 13. Open Questions / Decision Log

| # | Question | Status | Decision |
|---|---|---|---|
| 1 | Should we deduplicate events that appear in multiple orgs with the same ARN? | **Decided** | Store per-org in DynamoDB. API layer merges records with the same `event_arn` into one response object with `affected_orgs[]`. Operators see one row per event, not N rows for N orgs. |
| 2 | Collector frequency: 30 min vs 15 min? | **Decided** | **15 minutes.** Investigations can escalate quickly; cost delta is negligible (~3k Lambda invocations/month total). |
| 3 | Pagination strategy: DynamoDB `LastEvaluatedKey` vs cursor token? | **Decided** | Base64-encoded JSON of DynamoDB `LastEvaluatedKey` returned as `next_token` in response `meta`. Consistent with AWS SDK conventions. |
| 4 | Auth on API Gateway: API key, IAM SigV4, or Cognito? | **Decided** | **IAM SigV4 required** (method auth = `AWS_IAM`) for all methods. **API key opt-in** via Usage Plan for lightweight or external consumers. No Cognito in v1 — no user-facing frontend in scope. |
| 5 | `investigation` events: include `closed` ones in the 7-day window? | **Decided** | **Yes.** Closed investigations within the 7-day window are included. Useful for post-incident review. TTL handles expiry automatically. |
| 6 | Account metadata caching: re-fetch from Orgs every run? | **Decided** | **Cache in DynamoDB** — `health-aggregator-account-metadata` table, PK=`org_id#account_id`, 24h TTL. Without cache: 200 accounts × `ListTagsForResource` × 96 runs/day = 19,200 API calls/day/org. Cache reduces this to one full refresh per 24h plus incremental misses. |
| 7 | Should `window_days` be adjustable via API or fixed at 7? | **Decided** | **API query param**, default `7`, min `1`, max `7` (capped server-side to match DynamoDB TTL). Allows callers to request narrower windows for fresh-data-only views. |

---

## 14. Phases

### Phase 1 — Core (this spec)
- Collector + API Lambda
- DynamoDB schema
- SSM org registry
- `GET /v1/events` (issue + investigation)
- `GET /v1/summary`
- Terraform infrastructure

### Phase 2 — Enrichment
- `GET /v1/events/{arn}/details` (full event description text from `describe_event_details_for_organization`)
- Account metadata caching table
- `GET /v1/orgs` with collection health status

### Phase 3 — Notifications
- EventBridge event bus integration (push on new `issue` or `investigation` events)
- SNS/Slack webhook fanout for critical issues

### Phase 4 — Extended categories
- `scheduledChange` and `accountNotification` support
- Per-service and per-region suppression rules
