# SPEC — AWS Health Multi-Org Aggregator

> **This file is the single source of truth.**
> All code, infrastructure, and documentation are derived from it.
> When you change this spec, update the affected code.
> When you change code, update this spec to match.

---

## How To Use This Spec

**Modifying the system:**
1. Edit the relevant section below.
2. Ask Claude (or any LLM): *"Update the code to match SPEC.md — section X changed."*
3. The spec is written so that each section maps 1-to-1 with source files.

**Adding a new feature:**
1. Add a new sub-section to the relevant component (or create a new component section).
2. Include: purpose, file path, inputs/outputs, exact behavior rules.
3. Ask Claude to generate the code from the spec.

**Regenerating from scratch:**
The spec is complete enough to reproduce the entire codebase. Ask:
*"Generate all source files for this project from SPEC.md."*

**Section map → files:**

| SPEC Section | Files |
|---|---|
| §3 Architecture | (reference only) |
| §4 Org Registry | `lambda/collector/org_registry.py` |
| §5 Health Proxy | `terraform/api_gateway_health_proxy.tf`, `lambda/collector/health_proxy_client.py` |
| §6 Collector Lambda | `lambda/collector/handler.py` |
| §7 Event Classifier | `lambda/collector/event_classifier.py` |
| §8 Alert Dispatcher | `lambda/collector/alert_dispatcher.py` |
| §9 Account Cache | `lambda/collector/account_cache.py` |
| §10 API Lambda | `lambda/api/handler.py`, `lambda/api/routes/*.py` |
| §11 Exporter Lambda | `lambda/exporter/handler.py`, `lambda/exporter/excel_writer.py` |
| §12 Data Model | `terraform/dynamodb.tf`, `docs/data-model.md` |
| §13 API Contract | `docs/api-contract.md` |
| §14 Infrastructure | `terraform/*.tf` |
| §15 Security | `terraform/iam.tf`, `terraform/waf.tf`, `terraform/kms.tf` |
| §16 Monitoring | `terraform/monitoring.tf` |
| §17 Configuration | `terraform/variables.tf`, `terraform/terraform.tfvars.example` |
| §18 Scripts | `scripts/*.sh` |
| §19 Decision Log | (reference only) |
| §20 Changelog | (reference only) |
| §21 Future Work | (reference only) |

---

## §1 Purpose

Aggregate AWS Health events across **multiple AWS Organizations** using delegated Health administration. Expose a unified REST API with a 7-day sliding window. Proactively alert on new operational events. Generate a daily Excel report.

**Two event sections exposed:**

| Section | `eventTypeCategory` | Description |
|---|---|---|
| **Issues / Incidents** | `issue` | Confirmed service disruptions and outages |
| **Investigations** | `investigation` | AWS team investigating a signal; scope/impact not yet confirmed |

> `investigation` maps to the "Other notifications" section in the AWS Health console. Surface separately — operators should be aware without assuming a confirmed outage.

---

## §2 Goals and Non-Goals

**Goals:**
- Collect `issue` + `investigation` events from N configured orgs every 15 minutes.
- 7-day sliding window; DynamoDB TTL handles expiry automatically.
- Filter by org, service, region, status, environment (production / non-production).
- Enrich events with account metadata (name, BU, env) from AWS Organizations.
- Classify events as operational / control-plane; assign severity standard / critical.
- Alert via SNS when new critical operational events are detected.
- Generate a daily Excel workbook with pivot tables, delta tracking, and charts.
- All Lambdas VPC-attached, no internet egress — Health API accessed via private API GW proxy.

**Non-Goals:**
- Real-time streaming (EventBridge Health events, webhooks) — polling is sufficient for v1.
- Frontend/UI — the API is the boundary.
- Cross-org deduplication at storage — one DynamoDB item per (event_arn, org_id). Merging happens at the API layer.
- `scheduledChange` and `accountNotification` categories — v2.

---

## §3 Architecture

```
EventBridge (rate 15 min)
      │
      ▼
┌──────────────────────────────────────────────────────────┐
│  Collector Lambda (VPC)                                   │
│                                                           │
│  1. Load org registry from SSM                           │
│  2. For each org (parallel, ThreadPoolExecutor):         │
│     a. AssumeRole → org delegated admin account          │
│     b. Load account metadata (DynamoDB cache, 24h TTL)   │
│     c. GET /describe-events-for-organization (loop pages)│
│        via Health Proxy API GW (execute-api VPC endpoint)│
│     d. GET /describe-affected-accounts (loop pages)      │
│     e. Classify event (is_operational, severity)         │
│     f. Upsert to DynamoDB events table                   │
│  3. Dispatch alerts via SNS for new operational events   │
└──────────────┬───────────────────────────────────────────┘
               │ put_item
               ▼
        ┌──────────────┐       ┌────────────────────────────┐
        │  DynamoDB    │       │  Health Proxy API GW       │
        │  3 tables    │       │  (private REST API)        │
        │  events      │       │  AWS Service Integration   │
        │  acct-cache  │       │  VTL passthrough           │
        │  coll-state  │       │  → health.us-east-1.aws    │
        └──────┬───────┘       └────────────────────────────┘
               │                         ▲
               │                   execute-api VPC endpoint
               ├── API Lambda ───────────┘
               │   (VPC) ▲
               │         └── Consumer API GW (REGIONAL)
               │               WAF, IAM SigV4, IP allowlist
               │               ◀── external consumers
               │
               └── Exporter Lambda (VPC, daily)
                   → S3 Excel report
                   (state: open ARNs, delta log)

SNS ──▶ PagerDuty HTTPS subscription
    ──▶ Email subscription
    ──▶ Slack / webhook (user-configured)
```

**Why a private API GW proxy for Health?**
AWS Health has no VPC Interface Endpoint. Lambda → execute-api VPC endpoint → private API GW REST API → AWS Service Integration (VTL) → `health.us-east-1.amazonaws.com`. API GW calls Health from AWS's managed network, bypassing the VPC egress constraint. Lambda handles `nextToken` pagination by looping. One API GW resource per Health API method (4 total).

---

## §4 Org Registry

**File:** `lambda/collector/org_registry.py`

**Purpose:** Load and cache the list of configured orgs from SSM Parameter Store.

**SSM path:** `/health-aggregator/orgs` (SecureString, KMS-encrypted)

**SSM value format** (JSON array):
```json
[
  {
    "org_id":                     "o-abc123def456",
    "org_name":                   "Acme Corp",
    "account_id":                 "123456789012",
    "role_name":                  "HealthAggregatorReadRole",
    "assume_role_external_id":    "optional-external-id"
  }
]
```

**Behavior:**
- `load_orgs()` reads SSM on first call; result is module-level cached for Lambda warm reuse.
- Constructs `assume_role_arn = arn:aws:iam::{account_id}:role/{role_name}`.
- Returns list of dicts. Caller iterates over returned list.
- No `enabled` flag filtering — remove disabled orgs from SSM instead.

**Env vars used:** `ORG_REGISTRY_PATH` (default: `/health-aggregator/orgs`)

---

## §5 Health Proxy API Gateway

### 5.1 Terraform: `terraform/api_gateway_health_proxy.tf`

**Type:** AWS REST API — `PRIVATE`

**Auth method:** `AWS_IAM` on all methods

**Resource policy:** Restricts invocation to the `execute-api` VPC endpoint only (`aws:SourceVpce` condition).

**Integration type:** `AWS` (AWS Service Integration)

**4 methods** (one resource per Health API action):

| Path | `X-Amz-Target` header | IAM action |
|---|---|---|
| `POST /describe-events-for-organization` | `AmazonHealth.DescribeEventsForOrganization` | `health:DescribeEventsForOrganization` |
| `POST /describe-affected-accounts-for-organization` | `AmazonHealth.DescribeAffectedAccountsForOrganization` | `health:DescribeAffectedAccountsForOrganization` |
| `POST /describe-event-details-for-organization` | `AmazonHealth.DescribeEventDetailsForOrganization` | `health:DescribeEventDetailsForOrganization` |
| `POST /describe-affected-entities-for-organization` | `AmazonHealth.DescribeAffectedEntitiesForOrganization` | `health:DescribeAffectedEntitiesForOrganization` |

**VTL request template** (same for all methods):
```
$input.body
```
`Content-Type: application/x-amz-json-1.1` and `X-Amz-Target` are set via `request_parameters` as static literal values (single-quoted in HCL).

**VTL response templates** (passthrough for 200, 4xx, 5xx):
```
$input.body
```
Passthrough preserves `nextToken` in responses for Lambda's pagination loop.

**Integration role:** `health_proxy_apigw` — assumed by `apigateway.amazonaws.com`, has `health:Describe*` on `*`.

**Stage:** `prod`, X-Ray tracing enabled, CloudWatch access logging.

### 5.2 Lambda Client: `lambda/collector/health_proxy_client.py`

**Purpose:** SigV4-signed HTTP calls to the private Health Proxy API GW; handles pagination for all 4 methods.

**Class:** `HealthProxyClient`

**Constructor args:**
- `api_base_url: str` — stage URL, e.g. `https://{id}.execute-api.us-east-1.amazonaws.com/prod`
- `region: str` — always `"us-east-1"` (Health API constraint)

**Signing:** `SigV4Auth(credentials, "execute-api", region)` from `botocore.auth`. Content-Type sent to API GW is `application/json`; API GW rewrites to `application/x-amz-json-1.1` for Health.

**Retry:** Exponential backoff (1s base, doubles, max 4 attempts) on `ThrottlingError` (HTTP 429 or 400 + "ThrottlingException" in body).

**Methods and pagination:**

| Method | Pagination style | Returns |
|---|---|---|
| `describe_events_for_organization(categories, last_updated_from)` | `while True: … if not nextToken: break` | `list[dict]` (flat, all pages) |
| `describe_affected_accounts_for_organization(event_arn)` | same loop | `list[str]` account IDs |
| `describe_event_details_for_organization(event_arns, account_id=None)` | Chunks of 10 (API limit) | `dict` with `successfulSet`, `failedSet` |
| `describe_affected_entities_for_organization(event_arn, account_id)` | same loop | `list[dict]` entities |

**Constants:** `MAX_RESULTS = 100`, `MAX_RETRIES = 4`, `BASE_RETRY_DELAY_S = 1`

**Shared copy:** `lambda/api/health_proxy_client.py` is a copy of the collector version, synced by `scripts/deploy.sh`. Keep both identical.

---

## §6 Collector Lambda

**File:** `lambda/collector/handler.py`

**Trigger:** EventBridge scheduled rule — `rate(15 minutes)`

**Runtime:** Python 3.12, 512 MB, 300s timeout

**Entry point:** `handler(event, context) -> dict`

**Env vars:**

| Variable | Default | Description |
|---|---|---|
| `TABLE_NAME` | required | DynamoDB events table |
| `STATE_TABLE_NAME` | required | DynamoDB collection-state table |
| `ACCOUNT_METADATA_TABLE_NAME` | required | DynamoDB account-metadata cache table |
| `HEALTH_PROXY_API_URL` | required | Private API GW stage URL |
| `ORG_REGISTRY_PATH` | `/health-aggregator/orgs` | SSM path |
| `COLLECTION_WINDOW_DAYS` | `7` | Sliding window |
| `MAX_CONCURRENT_ORGS` | `5` | ThreadPoolExecutor max workers |
| `ACCOUNT_CACHE_TTL_HOURS` | `24` | Account metadata cache TTL |
| `HEALTH_ALERT_SNS_TOPIC_ARN` | `""` | SNS topic for health event alerts |
| `ALERTS_ENABLED` | `"true"` | Set `"false"` to disable alerting |
| `DIGEST_WINDOW_MINUTES` | `"30"` | Minutes to accumulate before sending first incident digest |
| `CORRELATION_WINDOW_MINUTES` | `"60"` | Minutes window for grouping same-service events into one incident |
| `LOG_LEVEL` | `"INFO"` | |

**Algorithm:**

```
1. load_orgs() from SSM
2. build HealthProxyClient(HEALTH_PROXY_API_URL)
3. ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ORGS):
   submit _collect_org(org, client, window_start) for each org
4. For each completed future:
   - count, written_items = future.result()
   - accumulate total_events and all_written_items
   - on exception: log error, emit CollectionErrors metric, put_collection_state(error)
5. emit EventsCollected metric (total across all orgs)
6. dispatch_alerts(all_written_items, state_table)
7. return {statusCode, total_events, errors}
```

**Per-org collection** (`_collect_org`):
```
1. _assume_org_role(org) → raw STS credentials
2. load_account_map(org_id, credentials) → {account_id: {name,bu,env}}
3. health_client.describe_events_for_organization(
     categories=["issue","investigation"],
     last_updated_from=now - COLLECTION_WINDOW_DAYS
   ) → list[event]
4. for each event:
   _process_event(ev, org_id, org_name, account_map, health_client)
   → (1, item) on success, (0, None) if no org accounts affected
5. emit OrgCollectionDurationMs metric
6. put_collection_state(org, success=True, count=records_written)
7. return (records_written, written_items)
```

**Per-event processing** (`_process_event`):
```
1. health_client.describe_affected_accounts_for_organization(event_arn)
2. filter to accounts present in account_map
3. if no org accounts → return (0, None)
4. classify_event(service, event_type_code, status) → ClassificationResult
5. build DynamoDB item:
   pk = f"{event_arn}#{org_id}"
   sk = f"{category}#{start_time}"
   + all event fields
   + affected_accounts (enriched with name/bu/env from account_map)
   + is_operational, severity (from classifier)
   + ttl = now + COLLECTION_WINDOW_DAYS * 86400
6. events_table.put_item(Item=item)
7. return (1, item)
```

**Error handling:**
- Per-org: exception caught, org marked failed, collection continues for other orgs.
- Per-event: exception caught, event skipped with warning log.
- CloudWatch metrics: `CollectionErrors` (per org), `EventsCollected` (total), `OrgCollectionDurationMs` (per org).
- All metrics in namespace `HealthAggregator`, dimension `OrgId`.

---

## §7 Event Classifier

**File:** `lambda/collector/event_classifier.py`

**Purpose:** Tag each event with `is_operational` and `severity` at collection time. Stored on DynamoDB item; available in API responses and Excel export.

**Public function:** `classify_event(service, event_type_code, status, description="") -> ClassificationResult`

**ClassificationResult fields:** `is_operational: bool`, `severity: Literal["standard","critical"]`, `reasons: list[str]`

### Operational classification

An event is **operational** (`is_operational=True`) if:
- `service` matches any pattern in `_OPERATIONAL_SERVICE_PATTERNS`, **OR**
- `description` contains any keyword in `_OPERATIONAL_DESCRIPTION_KEYWORDS`

**Operational services** (regex patterns, case-insensitive):
```
EC2, ECS, Lambda, Fargate, RDS, Aurora, DynamoDB, ElastiCache, MemoryDB,
S3, EBS, EFS, FSx,
ELB, ALB, NLB, VPC, CloudFront, Route 53,
CloudWatch, CloudWatch Logs,
Auto Scaling, Application Auto Scaling,
CodeBuild, CodeDeploy, CodePipeline,
SQS, SNS, Kinesis, MSK,
EKS, EMR, Glue, Athena,
Redshift, OpenSearch, ElasticSearch Service,
API Gateway, AppSync,
Secrets Manager, ACM
```

**Description keywords:** `operational, data, storage, database, unavailable, degraded, performance, failure, outage, scaling, monitoring, logging, connectivity, latency, timeout, disruption`

All other services (IAM, Organizations, CloudFormation, Config, CloudTrail, Billing, etc.) are **not operational**.

### Severity rules (evaluated in order, first match wins)

| Condition | Severity |
|---|---|
| event_type_code contains `OPERATIONAL_ISSUE\|OUTAGE\|DEGRADATION\|CONNECTIVITY` AND service is `RDS\|Aurora\|DynamoDB\|S3\|EBS\|EFS\|FSx\|ElastiCache\|MemoryDB` AND status=`open` | `critical` |
| event_type_code contains `OPERATIONAL_ISSUE\|OUTAGE\|DEGRADATION` AND service is `EC2\|ECS\|Lambda\|Fargate\|EKS` AND status=`open` | `critical` |
| event_type_code contains `OPERATIONAL_ISSUE\|OUTAGE\|CONNECTIVITY\|DEGRADATION` AND service is `ELB\|ALB\|NLB\|VPC\|CloudFront\|Route 53\|API Gateway` AND status=`open` | `critical` |
| Any other open event | `standard` |
| upcoming or closed events | `standard` |

---

## §8 Alert Dispatcher

**File:** `lambda/collector/alert_dispatcher.py`

**Purpose:** After each collection cycle, correlate new operational events into incidents and send digest alerts for mature incidents. Designed to suppress the per-ARN / per-region event storm AWS emits during large outages.

**Public function:** `dispatch(events: list[dict], state_table) -> int`
- `events`: DynamoDB items written in this collection cycle
- `state_table`: boto3 DynamoDB Table resource for collection-state
- Returns: number of SNS messages published this cycle

**Env vars used:** `HEALTH_ALERT_SNS_TOPIC_ARN`, `ALERTS_ENABLED`, `DIGEST_WINDOW_MINUTES`, `CORRELATION_WINDOW_MINUTES`

**Step 1 — Filter:** keep only events where `status == "open"` AND `is_operational == True`.

**Step 2 — Correlate into incidents:**
- Group filtered events by `(service, start_time_bucket)` where `start_time_bucket = floor(start_time / CORRELATION_WINDOW_MINUTES)`
- For each group, load or create an incident record in `collection_state` with `pk = "incident#{service}#{bucket}"`
- Merge into the incident: deduplicated `event_arns`, `regions`, `org_ids`, `severities`, `event_type_codes`; take max `affected_account_count`

**Step 3 — Flush digests:**
- Scan `collection_state` for all `pk` values beginning with `"incident#"`
- For each incident where `first_seen <= now - DIGEST_WINDOW_MINUTES`:
  - If never alerted → send digest
  - If previously alerted AND (`affected_account_count` doubled OR new regions added) → send update digest
  - Otherwise → suppress

**Priority:**
- `HIGH` if multi-region AND `affected_account_count > 100`
- `STANDARD` otherwise

**SNS message format:**
- `Subject`: `{priority} [{NEW INCIDENT|UPDATE}]: {service} — N event(s), M region(s), K accounts` (max 100 chars)
- `Message` body: human-readable incident summary + JSON payload for programmatic subscribers
- `MessageAttributes`: `priority`, `service`, `affected_accounts`, `regions`, `alert_type`

**Incident state schema** (stored in `collection_state` table):
```
pk                         "incident#{service}#{start_bucket}"
service                    e.g. "EC2"
start_bucket               e.g. "20260315T1000"
event_arns                 list[str]   — deduplicated ARNs seen
regions                    list[str]   — deduplicated regions
org_ids                    list[str]
severities                 list[str]
event_type_codes           list[str]
affected_account_count     int         — max across all events
event_count                int
first_seen                 ISO 8601
last_updated               ISO 8601
alert_sent_at              ISO 8601 | absent
last_alerted_account_count int
last_alerted_regions       list[str]
```

**VPC delivery:** Lambda publishes to SNS via SNS VPC Interface Endpoint. SNS delivers from AWS-managed network to PagerDuty HTTPS subscription, email, Slack, etc. No internet egress from Lambda needed.

---

## §9 Account Cache

**File:** `lambda/collector/account_cache.py`

**Purpose:** DynamoDB-backed cache for account metadata (name, business_unit, environment). Reduces Organizations API calls from O(accounts × runs) to O(accounts / 24h).

**Public function:** `load_account_map(org_id: str, credentials: dict) -> dict[str, dict]`
- Returns `{account_id: {"name": str, "bu": str, "env": str}}`
- `credentials`: raw STS credential dict (AccessKeyId, SecretAccessKey, SessionToken)

**Algorithm:**
```
1. Scan account-metadata table for pk beginning with "{org_id}#"
2. Separate into: cache_hits (ttl > now), cache_misses
3. If any misses OR first run (no cache):
   - Call organizations.list_accounts() with assumed credentials
   - Call organizations.list_tags_for_resource() for new accounts
   - Write new entries to cache with ttl = now + ACCOUNT_CACHE_TTL_HOURS * 3600
4. Return merged map: cache_hits + newly fetched
```

**DynamoDB table:** `health-aggregator-account-metadata`
**PK:** `{org_id}#{account_id}`
**TTL attribute:** `ttl`

**Tag extraction:**
- `account_name` from `list_accounts` response `.Name`
- `business_unit` from tag key `BusinessUnit` (default: `"Unknown"`)
- `environment` from tag key `Environment` (default: `"unknown"`; normalize to `"production"` or `"non-production"`)

**Env vars:** `ACCOUNT_METADATA_TABLE_NAME`, `ACCOUNT_CACHE_TTL_HOURS`

---

## §10 API Lambda

### 10.1 Handler: `lambda/api/handler.py`

**Trigger:** Consumer API Gateway REST API (AWS_PROXY integration)

**Runtime:** Python 3.12, 256 MB, 30s timeout

**Entry point:** `handler(event, context) -> dict`

**Routing:** Match `event["path"]` against regex patterns:
- `^/v1/events$` → `list_events`
- `^/v1/events/([^/]+)/details$` → `get_event_details` (capture group = `arn_b64`)
- `^/v1/summary$` → `get_summary`
- `^/v1/orgs$` → `list_orgs`
- No match → 404

**Query param parsing:** Extract `queryStringParameters` (single) and `multiValueQueryStringParameters` (repeatable params like `status`).

**Error handling:** Catch `ValueError` → 400 with `INVALID_PARAMETER`. Catch all others → 500 with `INTERNAL_ERROR`.

**Env vars:** `TABLE_NAME`, `STATE_TABLE_NAME`, `HEALTH_PROXY_API_URL`, `ORG_REGISTRY_PATH`, `COLLECTION_WINDOW_DAYS`, `LOG_LEVEL`

### 10.2 Events Route: `lambda/api/routes/events.py`

#### `GET /v1/events`

**Query params:**

| Param | Type | Required | Default | Validation |
|---|---|---|---|---|
| `category` | string | Yes | — | must be `issue` or `investigation` |
| `window_days` | int | No | env `COLLECTION_WINDOW_DAYS` | 1 ≤ value ≤ env `COLLECTION_WINDOW_DAYS` |
| `page_size` | int | No | 100 | max 200 |
| `next_token` | string | No | — | base64-decoded DynamoDB LastEvaluatedKey |
| `org_id` | string | No | — | in-memory filter |
| `service` | string | No | — | in-memory filter (case-insensitive) |
| `region` | string | No | — | in-memory filter |
| `status` | string (repeatable) | No | — | must be `open`, `closed`, or `upcoming` |
| `environment` | string | No | — | in-memory filter on `affected_accounts[*].environment` |

**DynamoDB query:** GSI `category-starttime-index`
```python
KeyConditionExpression = Key("category").eq(category) & Key("start_time").gte(window_start_str)
Limit = page_size
ExclusiveStartKey = decoded(next_token) if next_token else absent
```

**Post-query filtering** (in-memory — applied after DynamoDB Limit, not before):
- `org_id`: `item["org_id"] == org_id_filter`
- `service`: `item["service"].upper() == service_filter.upper()`
- `region`: `item["region"] == region_filter`
- `status`: `item["status"] in status_filters`
- `environment`: any `affected_accounts[*].environment == env_filter`

**Merging:** `_merge_by_arn(items)` — groups items by `event_arn`, collapses into one object per ARN with `affected_orgs[]`. Latest `last_updated_time` wins for top-level status fields.

**Response:** Standard envelope (see §13).

#### `GET /v1/events/{arn_b64}/details`

**Path param:** `arn_b64` — base64url-encoded event ARN

**Query params:** `org_id` (optional)

**Lookup:** `table.scan(FilterExpression=begins_with(pk, f"{event_arn}#"))` — acceptable for single-event lookup.

**Description fetch:** If `HEALTH_PROXY_API_URL` is set, call `HealthProxyClient.describe_event_details_for_organization([event_arn])` and attach `description` block to the response.

**Response:** Single merged event object (not in envelope).

### 10.3 Summary Route: `lambda/api/routes/summary.py`

**`GET /v1/summary`** — Query both categories from GSI, aggregate in-memory:
```json
{
  "meta": { "window_start", "window_end", "window_days" },
  "summary": {
    "issues":         { "total", "open", "closed", "upcoming" },
    "investigations": { "total", "open", "closed" },
    "by_org":         [ { "org_id", "org_name", "issues", "investigations" } ],
    "top_affected_services": [ { "service", "event_count" } ],
    "top_affected_regions":  [ { "region",  "event_count" } ],
    "affected_account_count": int
  }
}
```

### 10.4 Orgs Route: `lambda/api/routes/orgs.py`

**`GET /v1/orgs`** — Combine SSM org registry + DynamoDB collection-state table:
```json
{
  "data": [
    {
      "org_id", "org_name", "account_id", "role_name", "enabled": true,
      "collection": {
        "last_successful_at", "last_attempted_at",
        "last_error",         "events_in_window"
      }
    }
  ]
}
```

---

## §11 Exporter Lambda

**File:** `lambda/exporter/handler.py`

**Trigger:** EventBridge scheduled rule — `rate(1 day)` (configurable via `excel_export_schedule`)

**Runtime:** Python 3.12, 1024 MB, 300s timeout

**Entry point:** `handler(event, context) -> dict`

**Env vars:** `TABLE_NAME`, `EXPORT_BUCKET`, `COLLECTION_WINDOW_DAYS`, `LOG_LEVEL`

**Algorithm:**
```
1. Scan DynamoDB events table:
   FilterExpression = Attr("last_updated_time").gte(now - COLLECTION_WINDOW_DAYS)
2. Load state from S3:
   - prev_open_arns  = s3://{EXPORT_BUCKET}/exports/state/open_arns.json
   - delta_log_rows  = s3://{EXPORT_BUCKET}/exports/delta-log/delta_log.json
3. excel_writer.write_excel(items, prev_open_arns, delta_log_rows) → xlsx bytes
4. Upload xlsx to S3:
   key = exports/{YYYY}/{MM}/{DD}/aws-health-events.xlsx
   SSE = aws:kms, ContentType = application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
5. Persist state:
   - Save new open ARNs to open_arns.json
   - Append delta rows to delta_log.json (cap at 2000 rows)
6. Return { statusCode, report_s3_key, events_exported, new_open_events, delta_new, delta_resolved }
```

### 11.1 Excel Writer: `lambda/exporter/excel_writer.py`

**Public functions:**
- `write_excel(events, prev_open_arns, delta_log_rows) -> bytes`
- `current_open_arns(events) -> list[str]`

**Workbook sheets:**

| Sheet | Content |
|---|---|
| `Events` | One row per (event × org). Columns: event_arn, org_id, org_name, category, service, event_type_code, region, status, severity, is_operational, start_time, last_updated_time, end_time, affected_account_count, collected_at. Excel table with filters. Auto-fit columns. Freeze row 1. |
| `AffectedEntities` | Denormalized — one row per (event × org × account). Columns: event_arn, org_id, org_name, service, region, status, account_id, account_name, environment, business_unit. |
| `Delta_Latest` | New-open events (top), then "Resolved since last run" label, then resolved events (below). |
| `Delta_Log` | Rolling history of all delta runs (delta_type, run_timestamp_utc + event columns). |
| `Pivot_Service` | Excel pivot: service (row) × status (column). Filters: org_name, region, category, severity. |
| `Pivot_Account` | Excel pivot: org_name (row) × status (column). Filters: service, region. |
| `Pivot_Region` | Excel pivot: region (row) × status (column). Filters: service, org_name. |
| `Summary` | KPI counts (open/closed), status column chart, severity counts, top-services bar chart, delta summary table, navigation hyperlinks to all sheets. |

**Delta computation:**
- `current_open = {item["event_arn"] for item where status=="open"}`
- `new_open = current_open - set(prev_open_arns)`
- `resolved = set(prev_open_arns) - current_open`

**Dependencies:** `pandas==2.2.3`, `xlsxwriter==3.2.0`

---

## §12 Data Model

### Table: `health-aggregator-events`

**Primary key:**
- PK `pk` (S): `{event_arn}#{org_id}`
- SK `sk` (S): `{category}#{start_time_iso}`

**Attributes:**

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | Composite key |
| `sk` | S | Composite sort key |
| `event_arn` | S | AWS Health event ARN |
| `org_id` | S | AWS Organization ID |
| `org_name` | S | Org display name |
| `category` | S | `issue` or `investigation` |
| `service` | S | AWS service, e.g. `EC2` |
| `event_type_code` | S | e.g. `AWS_EC2_OPERATIONAL_ISSUE` |
| `region` | S | AWS region or `global` |
| `status` | S | `open`, `closed`, `upcoming` |
| `start_time` | S | ISO 8601 UTC |
| `last_updated_time` | S | ISO 8601 UTC |
| `end_time` | S | ISO 8601 UTC (absent if open) |
| `affected_accounts` | L | List of account maps |
| `affected_account_count` | N | Length of affected_accounts |
| `is_operational` | BOOL | From event_classifier |
| `severity` | S | `standard` or `critical` (from event_classifier) |
| `collected_at` | S | ISO 8601 UTC when item was written |
| `ttl` | N | Unix epoch; DynamoDB auto-deletes after expiry |

**Affected account map** (element of `affected_accounts`):
```json
{ "account_id": "111122223333", "name": "acme-prod-us", "bu": "Engineering", "env": "production" }
```

**GSIs:**

| Name | PK | SK | Projection | Primary use |
|---|---|---|---|---|
| `category-starttime-index` | `category` (S) | `start_time` (S) | ALL | `GET /v1/events` sliding window query |
| `org-lastupdate-index` | `org_id` (S) | `last_updated_time` (S) | KEYS_ONLY + select attrs | Per-org event listing |

**TTL:** `ttl` attribute. DynamoDB deletes within 48h of expiry. API applies redundant time-filter for not-yet-deleted items.

**Billing:** On-demand (PAY_PER_REQUEST). KMS CMK encryption. PITR enabled.

---

### Table: `health-aggregator-account-metadata`

**PK:** `pk` (S): `{org_id}#{account_id}` (no sort key)

**Attributes:** `pk`, `org_id`, `account_id`, `account_name`, `business_unit`, `environment`, `cached_at` (S, ISO 8601), `ttl` (N, now+24h)

---

### Table: `health-aggregator-collection-state`

**PK:** `pk` (S) — three item types share this table:

**Collection state item:** `pk = {org_id}`, `org_id`, `org_name`, `last_successful_at`, `last_attempted_at`, `last_error`, `events_in_window`, `updated_at`

**Incident item** (alert digest): `pk = "incident#{service}#{start_bucket}"` — see §8 incident state schema for full attribute list.

No longer used: `"alert#{event_arn}"` per-ARN dedup items (replaced by incident-level dedup).

---

### SSM Parameters

| Path | Type | Content |
|---|---|---|
| `/health-aggregator/orgs` | SecureString | JSON array of org configs (see §4) |

---

### S3 Bucket: `health-aggregator-excel-exports-{account_id}`

**Key structure:**
- `exports/{YYYY}/{MM}/{DD}/aws-health-events.xlsx` — daily report
- `exports/state/open_arns.json` — current open ARNs for delta computation
- `exports/delta-log/delta_log.json` — rolling delta history (max 2000 rows)

**Settings:** KMS encryption, versioning enabled, lifecycle: expire after `export_retention_days` (default 90), TLS-only bucket policy.

---

## §13 API Contract

**Base URL:** `https://{api-id}.execute-api.{region}.amazonaws.com/{stage}/v1`

**Auth:** `AWS_IAM` (SigV4). All methods require a valid AWS signature. Callers must have `execute-api:Invoke` on the API ARN — attach the `health-aggregator-dashboard-consumer` managed IAM policy.

**IP allowlist:** An API GW resource policy denies requests from IPs not in `consumer_api_allowed_cidrs` (set in `terraform.tfvars`).

**WAF:** AWS managed rule groups + rate limiting applied to the consumer stage.

**Response envelope** (list endpoints):
```json
{
  "meta": {
    "window_start": "ISO 8601",
    "window_end":   "ISO 8601",
    "window_days":  7,
    "total":        42,
    "returned":     42,
    "next_token":   "base64-or-null"
  },
  "data": [ ... ]
}
```

**Error response:**
```json
{ "error": { "code": "INVALID_PARAMETER", "message": "...", "field": "..." } }
```

**HTTP status codes:** 400 INVALID_PARAMETER / MISSING_PARAMETER, 404 NOT_FOUND, 429 RATE_LIMITED, 500 INTERNAL_ERROR.

**Endpoints:**

```
GET /v1/events
  ?category=issue|investigation   (required)
  &window_days=1-7                (optional, default 7)
  &org_id=o-xxx                   (optional)
  &service=EC2                    (optional)
  &region=us-east-1               (optional)
  &status=open|closed|upcoming    (optional, repeatable)
  &environment=production|non-production (optional)
  &page_size=1-200                (optional, default 100)
  &next_token=...                 (optional)

GET /v1/events/{event_arn_b64}/details
  ?org_id=o-xxx                   (optional)

GET /v1/summary
  ?category=issue|investigation|all (optional, default all)
  &org_id=o-xxx                   (optional)
  &window_days=1-7                (optional, default 7)

GET /v1/orgs
```

**Event object fields (list response):**
`event_arn`, `category`, `service`, `event_type_code`, `region`, `status`, `severity`, `is_operational`, `start_time`, `last_updated_time`, `end_time`, `affected_account_count`, `affected_orgs[]`

**Affected org:** `org_id`, `org_name`, `affected_accounts[]`

**Affected account:** `account_id`, `account_name`, `business_unit`, `environment`

**Details endpoint adds:** `description.latest_description`, `description.description_updated_at`, `description.fetched_from_org_id`

**Pagination:** DynamoDB `LastEvaluatedKey` base64-encoded as `next_token`. Repeat all filter params on subsequent pages.

**Cross-org merging:** Same `event_arn` stored once per org in DynamoDB. API merges into one event object; `affected_orgs` has one entry per org. `affected_account_count` is summed across all orgs.

---

## §14 Infrastructure (Terraform)

### File: `terraform/main.tf`
Provider: `hashicorp/aws ~> 5.0`. Required Terraform: `>= 1.5`.

### File: `terraform/kms.tf`
Single KMS CMK (`aws_kms_key.main`): used for DynamoDB tables (events, acct-metadata, collection-state), SSM SecureString, Lambda environment variables, S3 export bucket, WAF logs CloudWatch log group. Key rotation enabled.

### File: `terraform/dynamodb.tf`
Three tables: `health-aggregator-events` (PK+SK, GSI1, GSI2, TTL, PITR, KMS), `health-aggregator-account-metadata` (PK only, TTL, KMS), `health-aggregator-collection-state` (PK only, KMS).

### File: `terraform/api_gateway_health_proxy.tf`
Private REST API + 4 resources (one per Health API method). See §5.1 for full spec.

### File: `terraform/lambda.tf`
Three Lambda functions + log groups + consumer API GW:
- `{project_name}-collector` — VPC-attached, 512 MB, 300s
- `{project_name}-api` — VPC-attached, 256 MB, 30s
- `{project_name}-exporter` — VPC-attached, 1024 MB, 300s (conditional on `excel_export_enabled`)

Consumer API GW: REGIONAL REST API, `{proxy+}` resource, AWS_IAM auth, AWS_PROXY integration to api Lambda. Stage with X-Ray, access logs (JSON format including `requestId`, `ip`, `httpMethod`, `resourcePath`, `status`, `responseLength`, `integrationLatency`).

### File: `terraform/eventbridge.tf`
- Collector: `rate(15 minutes)` (configurable via `collection_schedule`)
- Exporter: `rate(1 day)` (configurable via `excel_export_schedule`; conditional on `excel_export_enabled`)

### File: `terraform/iam.tf`
Five IAM roles:

| Role | Principal | Key permissions |
|---|---|---|
| `health_proxy_apigw` | `apigateway.amazonaws.com` | `health:Describe*` on `*` |
| `collector` | `lambda.amazonaws.com` | `execute-api:Invoke` (health proxy), `sts:AssumeRole` (cross-org roles), DynamoDB write (events + state + acct-metadata), DynamoDB read (acct-metadata), SSM read (`/health-aggregator/*`), KMS, CloudWatch PutMetricData (namespace `HealthAggregator`), SNS Publish (health alert topic) |
| `api` | `lambda.amazonaws.com` | DynamoDB read (events + indexes + state), SSM read, KMS, `execute-api:Invoke` (health proxy for descriptions) |
| `exporter` | `lambda.amazonaws.com` | DynamoDB scan/query (events), S3 PutObject/GetObject (export bucket), KMS |
| `apigw_cloudwatch` | `apigateway.amazonaws.com` | `AmazonAPIGatewayPushToCloudWatchLogs` managed policy |

### File: `terraform/vpc_endpoints.tf`
Seven endpoints in private subnets:

| Service | Type | Why |
|---|---|---|
| `execute-api` | Interface | Lambda → Health Proxy API GW + consumer API GW |
| `dynamodb` | Gateway (free) | Lambda → DynamoDB |
| `ssm` | Interface | Lambda → SSM |
| `sts` | Interface | Collector → STS AssumeRole |
| `logs` | Interface | Lambda → CloudWatch Logs |
| `sns` | Interface | Collector → SNS alert publish |
| `s3` | Gateway (free) | Exporter → S3 |

All Interface endpoints share one security group: HTTPS (443) ingress from private subnet CIDRs only.

### File: `terraform/waf.tf`
WAF WebACL (REGIONAL scope) on **consumer API GW only** (private health proxy API is protected by resource policy + IAM — WAF not applicable to private REST APIs).

Rules:
1. `RateLimit` — block if > 1000 requests per 5-minute window per IP
2. `AWSManagedRulesCommonRuleSet` — SQLi, XSS, bad inputs (priority 2)
3. `AWSManagedRulesKnownBadInputsRuleSet` — log4j etc. (priority 3)

Logging: CloudWatch log group `aws-waf-logs-{project_name}-consumer-api` (name must start with `aws-waf-logs-`). Log filter: KEEP only BLOCK actions (DROP allowed traffic to reduce volume).

### File: `terraform/s3.tf`
S3 bucket `{project_name}-excel-exports-{account_id}`. KMS SSE, versioning, lifecycle (expire after `export_retention_days`), public access blocked, TLS-only bucket policy.

### File: `terraform/monitoring.tf`
CloudWatch alarms (all route to `var.alarm_sns_topic_arn`):

| Alarm | Metric | Threshold | Periods |
|---|---|---|---|
| `collector-errors` | Lambda Errors (collector) | > 0 | 2 × 15min |
| `collector-duration-high` | Lambda Duration p95 (collector) | > 80% of timeout | 3 × 15min |
| `org-collection-errors` | `HealthAggregator/CollectionErrors` | > 0 | 1 × 15min |
| `no-events-collected` | `HealthAggregator/EventsCollected` | ≤ 0 | 1 × 1h (breaching if missing) |
| `api-errors` | Lambda Errors (api) | > 5 | 5 × 1min |
| `api-latency-high` | Lambda Duration p99 (api) | > 3000ms | 3 × 5min |
| `health-proxy-5xx` | ApiGateway 5XXError (health proxy) | > 5 | 2 × 15min |
| `health-proxy-4xx` | ApiGateway 4XXError (health proxy) | > 0 | 1 × 15min |
| `dynamodb-system-errors` | DynamoDB SystemErrors (events table) | > 0 | 1 × 5min |
| `dynamodb-throttled-requests` | DynamoDB ThrottledRequests (events table) | > 10 | 3 × 5min |

CloudWatch Dashboard `{project_name}`: 6 widgets, 3 rows — (1) collection health (EventsCollected, CollectionErrors, OrgCollectionDurationMs), (2) health proxy errors and API Lambda errors/duration, (3) DynamoDB capacity and throttles.

---

## §15 Security

### Encryption
- **At rest:** DynamoDB, SSM SecureString, Lambda env vars, S3, WAF logs — all encrypted with CMK `aws_kms_key.main`.
- **In transit:** TLS 1.2+ enforced on all AWS SDK calls. API GW enforces TLS 1.2+ (REGIONAL endpoint). S3 bucket policy denies non-TLS requests.

### Network
- All Lambdas in private subnets — no inbound internet path.
- No internet egress — all AWS services reached via VPC endpoints.
- Lambda security group: HTTPS egress only to VPC endpoint CIDRs.
- Health Proxy API GW resource policy: invocations restricted to execute-api VPC endpoint only.

### WAF
Rate limit 1000 req/5min/IP. AWS managed common rule set + known bad inputs.

### IAM
Least privilege — see §14 iam.tf. Cross-org roles scoped by `cross_org_role_name` pattern. External ID supported (`assume_role_external_id` in org registry).

---

## §16 Monitoring

See §14 monitoring.tf for full alarm and dashboard spec.

**Custom CloudWatch metrics** (namespace `HealthAggregator`, dimension `OrgId`):
- `EventsCollected` — count per collection run (dimension `OrgId=all`)
- `CollectionErrors` — count per failed org (dimension `OrgId={org_id}`)
- `OrgCollectionDurationMs` — ms per org (dimension `OrgId={org_id}`)

---

## §17 Configuration Reference

All variables defined in `terraform/variables.tf`. Example values in `terraform/terraform.tfvars.example`.

**Required:**

| Variable | Description |
|---|---|
| `vpc_id` | VPC ID |
| `private_subnet_ids` | Private subnet IDs for Lambda and Interface VPC endpoint ENIs |
| `private_subnet_cidrs` | CIDR blocks (used in security group) |
| `private_route_table_ids` | Route tables for Gateway endpoint (DynamoDB, S3) |
| `cross_org_role_name` | IAM role name in each org's delegated admin account |

**Optional (with defaults):**

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | Deploy region |
| `project_name` | `health-aggregator` | Resource name prefix |
| `environment` | `prod` | Stage name |
| `collection_window_days` | `7` | Sliding window (days) |
| `collection_schedule` | `rate(15 minutes)` | EventBridge schedule |
| `max_concurrent_orgs` | `5` | ThreadPoolExecutor workers |
| `account_cache_ttl_hours` | `24` | Account metadata cache TTL |
| `alarm_sns_topic_arn` | `""` | SNS topic for CloudWatch alarms |
| `health_alert_sns_topic_arn` | `""` | SNS topic for health event alerts |
| `alerts_enabled` | `true` | Enable proactive health alerts |
| `digest_window_minutes` | `30` | Minutes to accumulate before sending first incident digest |
| `correlation_window_minutes` | `60` | Minutes window for grouping same-service events into one incident |
| `excel_export_enabled` | `true` | Deploy exporter Lambda |
| `excel_export_schedule` | `rate(1 day)` | Exporter trigger |
| `export_retention_days` | `90` | S3 lifecycle expiry |
| `log_retention_days` | `90` | CloudWatch log retention |
| `lambda_runtime` | `python3.12` | Lambda runtime |
| `collector_timeout_seconds` | `300` | Collector Lambda timeout |
| `api_timeout_seconds` | `30` | API Lambda timeout |
| `exporter_timeout_seconds` | `300` | Exporter Lambda timeout |
| `collector_memory_mb` | `512` | |
| `api_memory_mb` | `256` | |
| `exporter_memory_mb` | `1024` | pandas + xlsxwriter |

---

## §18 Scripts

### `scripts/deploy.sh`
```
pip install lambda/collector/requirements.txt → lambda/collector/
pip install lambda/api/requirements.txt       → lambda/api/
cp lambda/collector/health_proxy_client.py    → lambda/api/
pip install lambda/exporter/requirements.txt  → lambda/exporter/
terraform init / plan -out .build/tfplan / apply .build/tfplan
terraform output
```

### `scripts/register_org.sh`
Add / update / remove an org entry in SSM `/health-aggregator/orgs` SecureString.

Flags: `--name`, `--org-id`, `--account-id`, `--role` (default `HealthAggregatorReadRole`), `--param`, `--region`, `--delete`.

Behavior: fetch existing JSON from SSM → upsert/remove entry → prompt for confirmation → `ssm put-parameter --overwrite`.

### `scripts/test_collection.sh`
Manually trigger collector Lambda and stream CloudWatch Logs.

Flags: `--function` (default `health-aggregator-collector`), `--region`, `--tail-mins` (default 5), `--sync` (RequestResponse vs Event invocation).

After tail: print `HealthAggregator/EventsCollected` and `CollectionErrors` metrics for the run window.

---

## §19 Decision Log

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Deduplicate cross-org events at storage or API layer? | API layer — store per-org, merge by ARN in response | Preserves per-org affected account data; auditability |
| 2 | Collection frequency? | 15 minutes | Investigations can escalate quickly; cost delta negligible |
| 3 | Pagination token format? | Base64-encoded DynamoDB `LastEvaluatedKey` as `next_token` | Consistent with AWS SDK conventions |
| 4 | API auth? | IAM SigV4 only + IP allowlist resource policy | API key auth removed — browser dashboard uses Web Crypto API for SigV4 signing |
| 5 | Include closed investigations in 7-day window? | Yes | Useful for post-incident review; TTL handles expiry |
| 6 | Account metadata caching? | DynamoDB cache, 24h TTL | 200 accounts × 96 runs/day = 19,200 API calls without cache |
| 7 | `window_days` fixed or API param? | API query param, default 7, max 7 | Narrower windows for fresh-data-only views |
| 8 | Health API VPC access pattern? | Pattern C: Private API GW AWS Service Integration | No NAT GW allowed; no AWS managed HTTP proxy; SSM Automation rejected (async, size limits, pagination complexity) |
| 9 | Alert delivery channel? | SNS publish via SNS VPC endpoint | Lambda → SNS (no internet egress); SNS delivers to PagerDuty/email/Slack from AWS-managed network |
| 10 | Excel export storage? | S3 with KMS SSE, S3 Gateway endpoint | S3 Gateway is free; no internet egress needed; natural blob store for binary files |

---

## §20 Changelog

| Date | Change | Sections affected |
|---|---|---|
| 2026-03-14 | Initial implementation: collector, API, health proxy, DynamoDB, Terraform | All |
| 2026-03-14 | Added `event_classifier.py` — operational flag + severity | §7 |
| 2026-03-14 | Added `alert_dispatcher.py` — SNS alerting with dedup | §8 |
| 2026-03-15 | Rewrote `alert_dispatcher.py` — digest mode + service-level incident correlation; added `DIGEST_WINDOW_MINUTES` / `CORRELATION_WINDOW_MINUTES` env vars | §8 |
| 2026-03-14 | Added `exporter/` Lambda — daily Excel report to S3 | §11 |
| 2026-03-14 | Added SNS + S3 VPC endpoints | §14 vpc_endpoints |
| 2026-03-14 | Added exporter IAM role; collector gains SNS Publish | §14 iam |
| 2026-03-14 | Added `terraform/s3.tf` — export bucket | §12, §14 |
| 2026-03-14 | Added `scripts/register_org.sh` + `test_collection.sh` | §18 |
| 2026-03-14 | WAF access log format field added to `access_log_settings` | §14 lambda |

---

## §21 Future Work

| Item | Priority | Notes |
|---|---|---|
| `scheduledChange` + `accountNotification` categories | Medium | Add to `_CATEGORIES` in handler; add filter support to API |
| Per-service / per-region alert suppression rules | Medium | Store suppression list in SSM; check in alert_dispatcher before publish |
| Excel report presigned URL in API response | Low | `GET /v1/export/latest` → S3 presigned URL (15min TTL) |
| Cognito auth on consumer API GW | Low | For direct browser access; not needed while API is service-to-service |
| CI/CD pipeline (GitHub Actions) | Medium | `pip install` + `terraform plan` on PR; `apply` on merge to main |
| Unit tests | High | `lambda/collector/tests/`, `lambda/api/tests/` — mock DynamoDB and SSM |
| Multiple proxy deployments for separate AWS Organizations | Low | See §3 architecture note; each org needs own API GW proxy if org isolation required |
| DynamoDB provisioned capacity | Low | Switch if query volume > ~1M requests/month to reduce cost |
| Alert HTML email via SES | Low | Add SES VPC endpoint; format rich HTML in alert_dispatcher; send alongside SNS |
