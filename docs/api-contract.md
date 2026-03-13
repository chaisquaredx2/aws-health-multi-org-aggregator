# API Contract — AWS Health Multi-Org Aggregator

**Base URL**: `https://{api-id}.execute-api.{region}.amazonaws.com/v1`

**Auth**: Two modes (both supported simultaneously):
- **IAM SigV4** — default for service-to-service consumers. API GW method auth = `AWS_IAM`. Callers need `execute-api:Invoke` on the resource ARN.
- **API key** — opt-in for lightweight/external consumers. Pass `x-api-key` header. Issued via API GW Usage Plan. Rate-limited to the same WAF rules.

**Content-Type**: `application/json`

**Global response headers**:
```
Content-Type: application/json
X-Request-Id: {uuid}
X-Collection-Age: {seconds since last successful collection}
```

---

## Response Envelope

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

When `next_token` is non-null, pass it as `?next_token=<value>` in the next request to get the next page (default page size: 100).

## Error Responses

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

## Endpoints

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
            {
              "account_id":    "111122223333",
              "account_name":  "acme-prod-us",
              "business_unit": "Engineering",
              "environment":   "production"
            },
            {
              "account_id":    "444455556666",
              "account_name":  "acme-prod-eu",
              "business_unit": "Engineering",
              "environment":   "production"
            },
            {
              "account_id":    "777788889999",
              "account_name":  "acme-data-prod",
              "business_unit": "Data Platform",
              "environment":   "production"
            }
          ]
        },
        {
          "org_id":   "o-xyz987uvw654",
          "org_name": "Beta Industries",
          "affected_accounts": [
            {
              "account_id":    "000011112222",
              "account_name":  "beta-prod-east",
              "business_unit": "Platform",
              "environment":   "production"
            },
            {
              "account_id":    "333344445555",
              "account_name":  "beta-prod-west",
              "business_unit": "Platform",
              "environment":   "production"
            }
          ]
        }
      ]
    },
    {
      "event_arn":          "arn:aws:health:eu-west-1::event/RDS/AWS_RDS_OPERATIONAL_ISSUE/AWS_RDS_OPERATIONAL_ISSUE_ABC",
      "category":           "issue",
      "service":            "RDS",
      "event_type_code":    "AWS_RDS_OPERATIONAL_ISSUE",
      "region":             "eu-west-1",
      "status":             "open",
      "start_time":         "2026-03-13T06:30:00Z",
      "last_updated_time":  "2026-03-13T11:00:00Z",
      "end_time":           null,
      "affected_account_count": 1,
      "affected_orgs": [
        {
          "org_id":   "o-abc123def456",
          "org_name": "Acme Corp",
          "affected_accounts": [
            {
              "account_id":    "444455556666",
              "account_name":  "acme-prod-eu",
              "business_unit": "Engineering",
              "environment":   "production"
            }
          ]
        }
      ]
    }
  ]
}
```

> When `?org_id=o-abc123` is supplied, only that org's records are fetched so `affected_orgs` will always contain exactly one entry. This is the efficient path when the caller only cares about one org.

#### Investigation-category example

```
GET /v1/events?category=investigation
```

```json
{
  "meta": {
    "window_start": "2026-03-06T12:34:56Z",
    "window_end":   "2026-03-13T12:34:56Z",
    "window_days":  7,
    "total":        1,
    "returned":     1,
    "next_token":   null
  },
  "data": [
    {
      "event_arn":          "arn:aws:health:us-west-2::event/Lambda/AWS_LAMBDA_INVESTIGATION/AWS_LAMBDA_INVESTIGATION_001",
      "category":           "investigation",
      "service":            "Lambda",
      "event_type_code":    "AWS_LAMBDA_INVESTIGATION",
      "region":             "us-west-2",
      "status":             "open",
      "start_time":         "2026-03-13T09:00:00Z",
      "last_updated_time":  "2026-03-13T11:45:00Z",
      "end_time":           null,
      "affected_account_count": 2,
      "affected_orgs": [
        {
          "org_id":   "o-xyz987uvw654",
          "org_name": "Beta Industries",
          "affected_accounts": [
            {
              "account_id":    "000011112222",
              "account_name":  "beta-workloads-west",
              "business_unit": "Platform",
              "environment":   "production"
            },
            {
              "account_id":    "333344445555",
              "account_name":  "beta-sandbox",
              "business_unit": "Platform",
              "environment":   "non-production"
            }
          ]
        }
      ]
    }
  ]
}
```

---

### GET /v1/events/{event_arn_b64}/details

Returns full event details including the AWS-provided event description text, which typically contains updates, workarounds, and resolution notes.

`event_arn_b64` is the event ARN base64-URL-encoded (to avoid slash characters in path).

#### Path Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `event_arn_b64` | string | Yes | Base64URL-encoded event ARN |

#### Query Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `org_id` | string | No | Scope description fetch to a specific org's assumed credentials. If omitted, the API uses the first org that has a record for this event ARN. |

#### Example Request

```
GET /v1/events/YXJuOmF3czpoZWFsdGg6dXMtZWFzdC0xOjpldmVudC9FQzI.../details?org_id=o-abc123def456
```

#### Response 200

The details response follows the same merged `affected_orgs` shape as the list endpoint, plus a `description` block fetched live from `describe_event_details_for_organization`. `affected_entities` per account is also fetched live.

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
    "latest_description":     "We are investigating increased error rates for EC2 instance launches in the US-EAST-1 region. Customers may experience failures when launching new instances. We will provide an update in 60 minutes.",
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
            {
              "entity_value": "i-0abc123def456789",
              "entity_type":  "INSTANCE",
              "status":       "IMPAIRED",
              "last_updated": "2026-03-13T10:00:00Z"
            }
          ]
        }
      ]
    },
    {
      "org_id":   "o-xyz987uvw654",
      "org_name": "Beta Industries",
      "affected_accounts": [
        {
          "account_id":    "000011112222",
          "account_name":  "beta-prod-east",
          "business_unit": "Platform",
          "environment":   "production",
          "affected_entities": []
        }
      ]
    }
  ]
}
```

> `affected_entities` and `description` are fetched live from `describe_affected_entities_for_organization` and `describe_event_details_for_organization` at query time (not stored in DynamoDB). Entity data is scoped to the org whose credentials are used for the fetch (`org_id` param or first-found org). May add caching in v2.

---

### GET /v1/summary

Returns aggregated counts and breakdowns. Useful for dashboard header tiles.

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
    "issues": {
      "total":    12,
      "open":     4,
      "closed":   8,
      "upcoming": 0
    },
    "investigations": {
      "total":  3,
      "open":   2,
      "closed": 1
    },
    "by_org": [
      {
        "org_id":   "o-abc123def456",
        "org_name": "Acme Corp",
        "issues":         { "open": 3, "closed": 5 },
        "investigations": { "open": 1, "closed": 0 }
      },
      {
        "org_id":   "o-xyz987uvw654",
        "org_name": "Beta Industries",
        "issues":         { "open": 1, "closed": 3 },
        "investigations": { "open": 1, "closed": 1 }
      }
    ],
    "top_affected_services": [
      { "service": "EC2",    "event_count": 5 },
      { "service": "RDS",    "event_count": 3 },
      { "service": "Lambda", "event_count": 2 },
      { "service": "S3",     "event_count": 2 }
    ],
    "top_affected_regions": [
      { "region": "us-east-1",  "event_count": 6 },
      { "region": "eu-west-1",  "event_count": 4 },
      { "region": "us-west-2",  "event_count": 3 },
      { "region": "global",     "event_count": 2 }
    ],
    "affected_account_count": 27
  }
}
```

---

### GET /v1/orgs

Lists configured organizations with collection health metadata.

#### Query Parameters

None.

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
        "last_successful_at":  "2026-03-13T12:00:00Z",
        "last_attempted_at":   "2026-03-13T12:00:00Z",
        "last_error":          null,
        "events_in_window":    9
      }
    },
    {
      "org_id":                    "o-xyz987uvw654",
      "org_name":                  "Beta Industries",
      "delegated_admin_account_id": "987654321098",
      "enabled":                   true,
      "collection": {
        "last_successful_at":  "2026-03-13T11:58:00Z",
        "last_attempted_at":   "2026-03-13T12:00:00Z",
        "last_error":          "AssumeRole failed: AccessDenied",
        "events_in_window":    6
      }
    }
  ]
}
```

---

## Pagination

Pagination uses DynamoDB `LastEvaluatedKey` encoded as a base64 JSON string, returned as `next_token` in the response `meta`. To fetch the next page:

```
GET /v1/events?category=issue&next_token=<value-from-previous-response>
```

All other filter params must be repeated identically on subsequent page requests.

When `next_token` is `null` in the response, there are no more results.

---

## Field Reference

### Event Object (list and detail response)

| Field | Type | Description |
|---|---|---|
| `event_arn` | string | Global AWS Health event ARN |
| `category` | string | `issue` or `investigation` |
| `service` | string | AWS service name (e.g. `EC2`, `RDS`) |
| `event_type_code` | string | AWS event type code |
| `region` | string | AWS region or `global` |
| `status` | string | `open`, `closed`, or `upcoming` |
| `start_time` | ISO 8601 string | When the event was first reported by AWS |
| `last_updated_time` | ISO 8601 string | Most recent update from AWS |
| `end_time` | ISO 8601 string \| null | When the event was resolved; `null` if still open |
| `affected_account_count` | integer | Total affected accounts summed across all orgs |
| `affected_orgs` | array | One entry per org that has affected accounts for this event |

### Affected Org Object (element of `affected_orgs`)

| Field | Type | Description |
|---|---|---|
| `org_id` | string | AWS Organization ID |
| `org_name` | string | Human-readable org name from registry |
| `affected_accounts` | array | Account objects for this org (see below) |

### Affected Account Object (element of `affected_accounts`)

| Field | Type | Description |
|---|---|---|
| `account_id` | string | 12-digit AWS account ID |
| `account_name` | string | Account name from AWS Organizations |
| `business_unit` | string | Value of `BusinessUnit` org tag (or `Unknown`) |
| `environment` | string | `production` or `non-production` from `Environment` org tag |
| `affected_entities` | array | _(details endpoint only)_ Live entity data from `describe_affected_entities_for_organization` |

### Description Object (details endpoint only)

| Field | Type | Description |
|---|---|---|
| `latest_description` | string | AWS-provided event description text (updated as the event progresses) |
| `description_updated_at` | ISO 8601 string | Timestamp of the description |
| `fetched_from_org_id` | string | Which org's credentials were used to fetch the description |
