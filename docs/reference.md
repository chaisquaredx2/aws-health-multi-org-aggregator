# Reference — AWS Health Multi-Org Aggregator

This document covers the REST API contract and the underlying DynamoDB data model.

---

## API Contract

**Base URL**: `https://{api-id}.execute-api.{region}.amazonaws.com/v1`

**Auth**: AWS IAM SigV4 (`authorization = AWS_IAM`). Callers must sign requests with valid AWS credentials that have `execute-api:Invoke` on the resource ARN. The managed IAM policy `health-aggregator-dashboard-consumer` grants exactly this permission — attach it to any user or role that needs dashboard access.

**IP restriction**: Requests are also subject to an API Gateway resource policy. Only source IPs matching `consumer_api_allowed_cidrs` (set in `terraform.tfvars`) are permitted. Requests from unlisted IPs receive `403` before auth is checked.

**WAF**: A WAF WebACL (AWS managed rules + rate limiting) is attached to the consumer API stage. Blocked requests receive `403`.

**Content-Type**: `application/json`

**Global response headers**:
```
Content-Type: application/json
Access-Control-Allow-Origin: *
```

---

### Response Envelope

All list endpoints follow this envelope:

```json
{
  "meta": {
    "window_start":  "2026-03-06T12:34:56Z",
    "window_end":    "2026-03-13T12:34:56Z",
    "window_days":   7,
    "total":         42,
    "returned":      42,
    "next_token":    null
  },
  "data": [ ... ]
}
```

When `next_token` is non-null, pass it as `?next_token=<value>` in the next request (default page size: 100).

### Error Responses

```json
{
  "error": {
    "code":    "INVALID_PARAMETER",
    "message": "category must be one of: issue, investigation",
    "field":   "category"
  }
}
```

| HTTP Status | Code | When |
|---|---|---|
| 400 | `INVALID_PARAMETER` | Unknown or invalid query param value |
| 400 | `MISSING_PARAMETER` | Required param absent |
| 404 | `NOT_FOUND` | Event ARN not found |
| 429 | `RATE_LIMITED` | WAF rate limit hit |
| 500 | `INTERNAL_ERROR` | Lambda/DynamoDB error |

---

### GET /v1/events

Returns health events within the 7-day sliding window, filtered by category and optional criteria.

#### Query Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `category` | string | Yes | — | `issue` or `investigation` |
| `window_days` | integer | No | `7` | Lookback window (1–7) |
| `org_id` | string | No | all | Filter to a single org (`o-xxxx`) |
| `service` | string | No | all | AWS service name e.g. `EC2`, `RDS`, `S3` |
| `region` | string | No | all | AWS region e.g. `us-east-1`, or `global` |
| `status` | string (repeatable) | No | all | `open`, `closed`, `upcoming` |
| `environment` | string | No | all | `production` or `non-production` |
| `page_size` | integer | No | `100` | Max 200 |
| `next_token` | string | No | — | Pagination token from previous response |

#### Example Request

```
GET /v1/events?category=issue&status=open&environment=production
```

#### Response 200

Events are merged by `event_arn` across orgs. Each event object has a top-level `affected_orgs` array; accounts from each org are grouped under their org entry. `affected_account_count` is the total across all orgs.

```json
{
  "meta": {
    "window_start": "2026-03-06T12:34:56Z",
    "window_end":   "2026-03-13T12:34:56Z",
    "window_days":  7,
    "total":        2,
    "returned":     2,
    "next_token":   null
  },
  "data": [
    {
      "event_arn":          "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/AWS_EC2_OPERATIONAL_ISSUE_XYZ",
      "category":           "issue",
      "service":            "EC2",
      "event_type_code":    "AWS_EC2_OPERATIONAL_ISSUE",
      "region":             "us-east-1",
      "status":             "open",
      "start_time":         "2026-03-12T08:00:00Z",
      "last_updated_time":  "2026-03-13T10:15:00Z",
      "end_time":           null,
      "affected_account_count": 5,
      "affected_orgs": [
        {
          "org_id":   "o-abc123def456",
          "org_name": "Acme Corp",
          "affected_accounts": [
            { "account_id": "111122223333", "account_name": "acme-prod-us",   "business_unit": "Engineering",    "environment": "production" },
            { "account_id": "444455556666", "account_name": "acme-prod-eu",   "business_unit": "Engineering",    "environment": "production" },
            { "account_id": "777788889999", "account_name": "acme-data-prod", "business_unit": "Data Platform",  "environment": "production" }
          ]
        },
        {
          "org_id":   "o-xyz987uvw654",
          "org_name": "Beta Industries",
          "affected_accounts": [
            { "account_id": "000011112222", "account_name": "beta-prod-east", "business_unit": "Platform", "environment": "production" },
            { "account_id": "333344445555", "account_name": "beta-prod-west", "business_unit": "Platform", "environment": "production" }
          ]
        }
      ]
    }
  ]
}
```

> When `?org_id=o-abc123` is supplied, only that org's records are fetched — `affected_orgs` will always contain exactly one entry. This is the efficient path when the caller only cares about one org.

---

### GET /v1/events/{event_arn_b64}/details

Returns full event details including the AWS-provided event description text (updates, workarounds, resolution notes) and per-entity impact data.

`event_arn_b64` is the event ARN base64-URL-encoded (to avoid slash characters in path).

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `org_id` | string | No | Scope description fetch to a specific org's assumed credentials. If omitted, uses the first org that has a record for this event ARN. |

#### Response 200

```json
{
  "event_arn":          "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/AWS_EC2_OPERATIONAL_ISSUE_XYZ",
  "category":           "issue",
  "service":            "EC2",
  "event_type_code":    "AWS_EC2_OPERATIONAL_ISSUE",
  "region":             "us-east-1",
  "status":             "open",
  "start_time":         "2026-03-12T08:00:00Z",
  "last_updated_time":  "2026-03-13T10:15:00Z",
  "end_time":           null,
  "description": {
    "latest_description":     "We are investigating increased error rates for EC2 instance launches in the US-EAST-1 region.",
    "description_updated_at": "2026-03-13T10:15:00Z",
    "fetched_from_org_id":    "o-abc123def456"
  },
  "affected_account_count": 4,
  "affected_orgs": [
    {
      "org_id":   "o-abc123def456",
      "org_name": "Acme Corp",
      "affected_accounts": [
        {
          "account_id":    "111122223333",
          "account_name":  "acme-prod-us",
          "business_unit": "Engineering",
          "environment":   "production",
          "affected_entities": [
            { "entity_value": "i-0abc123def456789", "entity_type": "INSTANCE", "status": "IMPAIRED", "last_updated": "2026-03-13T10:00:00Z" }
          ]
        }
      ]
    }
  ]
}
```

> `affected_entities` and `description` are fetched live at query time from `describe_affected_entities_for_organization` and `describe_event_details_for_organization` — not stored in DynamoDB.

---

### GET /v1/summary

Returns aggregated counts and breakdowns for dashboard header tiles.

#### Query Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `category` | string | No | `all` | `issue`, `investigation`, or `all` |
| `org_id` | string | No | all | Scope to one org |
| `window_days` | integer | No | `7` | Lookback window (1–7) |

#### Response 200

```json
{
  "meta": {
    "window_start": "2026-03-06T12:34:56Z",
    "window_end":   "2026-03-13T12:34:56Z",
    "window_days":  7
  },
  "summary": {
    "issues":         { "total": 12, "open": 4, "closed": 8, "upcoming": 0 },
    "investigations": { "total":  3, "open": 2, "closed": 1 },
    "by_org": [
      {
        "org_id":   "o-abc123def456",
        "org_name": "Acme Corp",
        "issues":         { "open": 3, "closed": 5 },
        "investigations": { "open": 1, "closed": 0 }
      }
    ],
    "top_affected_services": [
      { "service": "EC2", "event_count": 5 },
      { "service": "RDS", "event_count": 3 }
    ],
    "top_affected_regions": [
      { "region": "us-east-1", "event_count": 6 },
      { "region": "eu-west-1", "event_count": 4 }
    ],
    "affected_account_count": 27
  }
}
```

---

### GET /v1/orgs

Lists configured organizations with collection health metadata.

#### Response 200

```json
{
  "data": [
    {
      "org_id":                    "o-abc123def456",
      "org_name":                  "Acme Corp",
      "delegated_admin_account_id": "123456789012",
      "enabled":                   true,
      "collection": {
        "last_successful_at": "2026-03-13T12:00:00Z",
        "last_attempted_at":  "2026-03-13T12:00:00Z",
        "last_error":         null,
        "events_in_window":   9
      }
    }
  ]
}
```

---

### POST /v1/export

Triggers an on-demand Excel export. Invokes the exporter Lambda asynchronously and returns immediately. The workbook is written to S3 within ~60 seconds.

Only available when `excel_export_enabled = true` (default). Returns `501` if disabled.

#### Request body

None required. Send an empty body or omit entirely.

#### Response 202

```json
{
  "message":          "Export started",
  "estimated_s3_key": "exports/2026/03/15/aws-health-events.xlsx",
  "s3_uri":           "s3://<bucket>/exports/2026/03/15/aws-health-events.xlsx"
}
```

The export will overwrite any existing file for today's date. Check S3 or CloudWatch Logs (`/aws/lambda/health-aggregator-exporter`) to confirm completion.

| HTTP Status | Meaning |
|---|---|
| 202 | Export started successfully |
| 501 | `excel_export_enabled = false` — endpoint not configured |
| 500 | Failed to invoke exporter Lambda — check CloudWatch logs |

---

### Pagination

Uses DynamoDB `LastEvaluatedKey` encoded as a base64 JSON string, returned as `next_token` in the response `meta`. All filter params must be repeated identically on subsequent page requests. When `next_token` is `null`, there are no more results.

---

### Field Reference

#### Event Object

| Field | Type | Description |
|---|---|---|
| `event_arn` | string | Global AWS Health event ARN |
| `category` | string | `issue` or `investigation` |
| `service` | string | AWS service name (e.g. `EC2`, `RDS`) |
| `event_type_code` | string | AWS event type code |
| `region` | string | AWS region or `global` |
| `status` | string | `open`, `closed`, or `upcoming` |
| `start_time` | ISO 8601 | When the event was first reported by AWS |
| `last_updated_time` | ISO 8601 | Most recent update from AWS |
| `end_time` | ISO 8601 \| null | When the event was resolved; `null` if still open |
| `affected_account_count` | integer | Total affected accounts summed across all orgs |
| `affected_orgs` | array | One entry per org that has affected accounts for this event |

#### Affected Org / Account Objects

| Field | Type | Description |
|---|---|---|
| `org_id` | string | AWS Organization ID |
| `org_name` | string | Human-readable org name from registry |
| `affected_accounts` | array | Account objects for this org |
| `account_id` | string | 12-digit AWS account ID |
| `account_name` | string | Account name from AWS Organizations |
| `business_unit` | string | Value of `BusinessUnit` org tag (or `Unknown`) |
| `environment` | string | `production` or `non-production` from `Environment` org tag |
| `affected_entities` | array | _(details endpoint only)_ Live entity data |

---

## Data Model

### Table: `health-aggregator-events`

Primary store for health events collected from all configured organizations.

#### Key Design

```
PK (String): {event_arn}#{org_id}
SK (String): {category}#{start_time_iso}
```

`event_arn#org_id` ensures the same global event ARN appearing in multiple orgs is stored as separate items — affected accounts differ per org. ISO 8601 sort key strings sort lexicographically so range queries work without epoch conversion.

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | `{event_arn}#{org_id}` |
| `sk` | S | `{category}#{start_time_iso}` |
| `event_arn` | S | Original AWS Health event ARN |
| `org_id` | S | AWS Organization ID |
| `org_name` | S | Human-readable org name |
| `category` | S | `issue` or `investigation` |
| `service` | S | AWS service (e.g. `EC2`, `RDS`) |
| `event_type_code` | S | AWS event type code |
| `region` | S | AWS region or `global` |
| `status` | S | `open`, `closed`, `upcoming` |
| `start_time` | S | ISO 8601 UTC — event start |
| `last_updated_time` | S | ISO 8601 UTC — last AWS update |
| `end_time` | S (nullable) | ISO 8601 UTC — resolution time |
| `affected_accounts` | L | List of account maps |
| `affected_account_count` | N | Length of `affected_accounts` (denormalized) |
| `collected_at` | S | ISO 8601 UTC — when collector last wrote this item |
| `ttl` | N | Unix timestamp — auto-expiry at 7 days from `collected_at` |

#### Global Secondary Indexes

**GSI 1: `category-starttime-index`** — primary query path for `/v1/events`

| | Attribute | Type |
|---|---|---|
| PK | `category` | S |
| SK | `start_time` | S |

**GSI 2: `org-lastupdate-index`** — scoped org queries and `/v1/orgs` event counts

| | Attribute | Type |
|---|---|---|
| PK | `org_id` | S |
| SK | `last_updated_time` | S |

Projection: KEYS_ONLY + `category`, `status`, `service`, `region`, `affected_account_count`

#### TTL Behavior

TTL attribute: `ttl` (Number). DynamoDB deletes expired items within 48h. The API Lambda applies a redundant `start_time >= now - 7d` filter to exclude not-yet-deleted items. Open events that stay active for weeks are refreshed (`ttl = now + 7d`) on every collector upsert.

---

### Table: `health-aggregator-account-metadata`

Account metadata cache. Reduces `organizations:ListAccounts` + `organizations:ListTagsForResource` calls from O(accounts × runs) to O(accounts) per 24h.

#### Key Design

```
PK (String): {org_id}#{account_id}
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | `{org_id}#{account_id}` |
| `org_id` | S | AWS Organization ID |
| `account_id` | S | 12-digit AWS account ID |
| `account_name` | S | Account name from `list_accounts` |
| `business_unit` | S | Value of `BusinessUnit` org tag (or `Unknown`) |
| `environment` | S | `production` or `non-production` from `Environment` org tag |
| `cached_at` | S | ISO 8601 UTC when this entry was written |
| `ttl` | N | Unix timestamp — expires 24h after `cached_at` |

---

### Table: `health-aggregator-collection-state`

Two item types share this table:

#### 1. Org collection state (`pk = {org_id}`)

Tracks last collection run per org — powers `GET /v1/orgs`.

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | AWS Organization ID |
| `org_name` | S | Human-readable name |
| `last_successful_at` | S | ISO 8601 UTC of last successful collection |
| `last_attempted_at` | S | ISO 8601 UTC of last attempt |
| `last_error` | S (nullable) | Error message if last attempt failed |
| `events_in_window` | N | Count of events collected in last run |
| `updated_at` | S | ISO 8601 UTC of this record's last update |

#### 2. Incident items (`pk = incident#{service}#{start_bucket}`)

Written by `alert_dispatcher.py` to correlate per-ARN/per-region events from the same outage into a single alert.

`start_bucket` = `start_time` floored to the nearest `CORRELATION_WINDOW_MINUTES` boundary (e.g. `20260313T0800` for a 60-min window).

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | `incident#{service}#{start_bucket}` |
| `service` | S | AWS service name |
| `start_bucket` | S | `YYYYMMDDTHHMM` bucket of earliest event start |
| `event_arns` | L | Deduplicated list of correlated event ARNs |
| `regions` | L | Deduplicated list of AWS regions |
| `org_ids` | L | Deduplicated list of org IDs |
| `severities` | L | Deduplicated list of severity values |
| `event_type_codes` | L | Deduplicated list of event type codes |
| `affected_account_count` | N | Max `affected_account_count` across all correlated events |
| `event_count` | N | Total correlated event ARNs |
| `first_seen` | S | ISO 8601 UTC when incident was first created |
| `last_updated` | S | ISO 8601 UTC of most recent merge |
| `alert_sent_at` | S (nullable) | ISO 8601 UTC when first digest was published |
| `last_alerted_account_count` | N | Account count snapshot at last alert |
| `last_alerted_regions` | L | Regions snapshot at last alert |

---

### SSM Parameter Store

**`/health-aggregator/orgs`** (SecureString, KMS-encrypted)

JSON array of org configurations:

```json
[
  {
    "org_id":                     "o-abc123def456",
    "org_name":                   "Acme Corp",
    "delegated_admin_account_id": "123456789012",
    "assume_role_arn":            "arn:aws:iam::123456789012:role/HealthAggregatorReadRole",
    "assume_role_external_id":    "acme-health-agg-2026",
    "enabled":                    true
  }
]
```

---

### Access Patterns

| Query | Table/Index | Key Condition | Filter |
|---|---|---|---|
| All `issue` events in last 7 days | `category-starttime-index` | `category = issue AND start_time >= T-7d` | — |
| All `investigation` events | `category-starttime-index` | `category = investigation AND start_time >= T-7d` | — |
| Issues for a specific org | `category-starttime-index` | `category = issue AND start_time >= T-7d` | `org_id = X` |
| Issues for a specific service | `category-starttime-index` | `category = issue AND start_time >= T-7d` | `service = EC2` |
| Production issues only | `category-starttime-index` | `category = issue AND start_time >= T-7d` | `affected_accounts[*].environment = production` |
| All events for an org | `org-lastupdate-index` | `org_id = X AND last_updated_time >= T-7d` | — |
| Account metadata for an org | `account-metadata` table | batch_get by `org_id#account_id` | — |
| Collection state per org | `collection-state` table | `pk = org_id` | — |
