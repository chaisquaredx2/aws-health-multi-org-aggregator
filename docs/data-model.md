# Data Model — AWS Health Multi-Org Aggregator

## DynamoDB Tables

### Table: `health-aggregator-events`

Primary store for health events collected from all configured organizations.

#### Key Design

```
PK (String): {event_arn}#{org_id}
SK (String): {category}#{start_time_iso}
```

**Rationale**:
- `PK = event_arn#org_id` ensures that the same global event ARN appearing in multiple orgs is stored as separate items (intentional — affected accounts differ per org).
- `SK = category#start_time_iso` enables efficient range scans on the GSI (`category-starttime-index`) where `category` is the GSI partition key and `start_time` is the GSI sort key.
- ISO 8601 strings sort lexicographically, so range queries like `start_time >= 7 days ago` work correctly without epoch conversion.

#### Attributes

| Attribute | DynamoDB Type | Description |
|---|---|---|
| `pk` | S | `{event_arn}#{org_id}` |
| `sk` | S | `{category}#{start_time_iso}` |
| `event_arn` | S | Original AWS Health event ARN |
| `org_id` | S | AWS Organization ID |
| `org_name` | S | Human-readable org name (from registry) |
| `category` | S | `issue` or `investigation` |
| `service` | S | AWS service (e.g. `EC2`, `RDS`) |
| `event_type_code` | S | AWS event type code |
| `region` | S | AWS region or `global` |
| `status` | S | `open`, `closed`, `upcoming` |
| `start_time` | S | ISO 8601 UTC — event start |
| `last_updated_time` | S | ISO 8601 UTC — last AWS update |
| `end_time` | S (nullable) | ISO 8601 UTC — resolution time, absent if open |
| `affected_accounts` | L | List of account maps (see below) |
| `affected_account_count` | N | Length of `affected_accounts` (denormalized for scan projection) |
| `collected_at` | S | ISO 8601 UTC — when this item was last written by collector |
| `ttl` | N | Unix timestamp — auto-expiry at 7 days from `collected_at` |

#### Affected Account Map (element of `affected_accounts` list)

```json
{
  "account_id":    "111122223333",
  "account_name":  "acme-prod-us",
  "business_unit": "Engineering",
  "environment":   "production"
}
```

#### Global Secondary Indexes

**GSI 1: `category-starttime-index`**

| | Attribute | Type |
|---|---|---|
| PK | `category` | S |
| SK | `start_time` | S |

Projection: ALL

Use case: Primary query path for `GET /v1/events?category=issue`. KeyConditionExpression:
```python
Key('category').eq('issue') & Key('start_time').gte('2026-03-06T...')
```
Additional filter expression applied for `org_id`, `service`, `region`, `status`.

---

**GSI 2: `org-lastupdate-index`**

| | Attribute | Type |
|---|---|---|
| PK | `org_id` | S |
| SK | `last_updated_time` | S |

Projection: KEYS_ONLY + `category`, `status`, `service`, `region`, `affected_account_count`

Use case: List events scoped to a single org; also used by `GET /v1/orgs` to compute `events_in_window` per org via a count query.

---

#### TTL Behavior

- TTL attribute: `ttl` (Number)
- DynamoDB deletes expired items within 48 hours of expiry (eventually consistent).
- API Lambda applies a redundant time-filter (`start_time >= now - 7d`) to exclude any not-yet-deleted expired items.
- Collector refreshes `ttl = now + 7d` on every upsert — open events that stay active for weeks will remain in the table continuously.

#### Item Size Estimate

Worst case per item: ~2 KB (event metadata + 50 affected accounts × ~30 bytes each).
Expected case: ~500 bytes (5–10 affected accounts).
DynamoDB item limit: 400 KB — well within limits.

#### Capacity Mode

On-demand (pay-per-request). Switch to provisioned if query volume exceeds ~1M requests/month.

---

#### Example Item (raw DynamoDB JSON)

```json
{
  "pk":                  { "S": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/XYZ#o-abc123def456" },
  "sk":                  { "S": "issue#2026-03-12T08:00:00+00:00" },
  "event_arn":           { "S": "arn:aws:health:us-east-1::event/EC2/AWS_EC2_OPERATIONAL_ISSUE/XYZ" },
  "org_id":              { "S": "o-abc123def456" },
  "org_name":            { "S": "Acme Corp" },
  "category":            { "S": "issue" },
  "service":             { "S": "EC2" },
  "event_type_code":     { "S": "AWS_EC2_OPERATIONAL_ISSUE" },
  "region":              { "S": "us-east-1" },
  "status":              { "S": "open" },
  "start_time":          { "S": "2026-03-12T08:00:00+00:00" },
  "last_updated_time":   { "S": "2026-03-13T10:15:00+00:00" },
  "affected_accounts":   {
    "L": [
      { "M": {
        "account_id":    { "S": "111122223333" },
        "account_name":  { "S": "acme-prod-us" },
        "business_unit": { "S": "Engineering" },
        "environment":   { "S": "production" }
      }},
      { "M": {
        "account_id":    { "S": "444455556666" },
        "account_name":  { "S": "acme-prod-eu" },
        "business_unit": { "S": "Engineering" },
        "environment":   { "S": "production" }
      }}
    ]
  },
  "affected_account_count": { "N": "2" },
  "collected_at":        { "S": "2026-03-13T12:00:00+00:00" },
  "ttl":                 { "N": "1742558400" }
}
```

---

### Table: `health-aggregator-account-metadata`

Account metadata cache. Populated by Collector Lambda; reduces `organizations:ListAccounts` + `organizations:ListTagsForResource` calls from O(accounts × runs) to O(accounts) per 24h.

#### Key Design

```
PK (String): org_id#account_id
```

No sort key — one item per account per org.

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

#### Collector Cache Logic

```
1. batch_get all accounts for this org_id from account-metadata table
2. identify missing or expired entries (ttl < now)
3. for missing entries only: call orgs_client.list_accounts() + list_tags_for_resource()
4. write new/updated entries with ttl = now + 24h
5. return full account_map for this org
```

On the very first run (cold cache), the full `ListAccounts` + `ListTagsForResource` scan is performed and all entries are cached. Subsequent runs within 24h hit the cache for all known accounts and only call Orgs API for newly created accounts.

#### Example Item

```json
{
  "pk":            "o-abc123def456#111122223333",
  "org_id":        "o-abc123def456",
  "account_id":    "111122223333",
  "account_name":  "acme-prod-us",
  "business_unit": "Engineering",
  "environment":   "production",
  "cached_at":     "2026-03-13T12:00:00Z",
  "ttl":           1742644800
}
```

---

### Table: `health-aggregator-collection-state`

Tracks the last collection run per org for the `GET /v1/orgs` endpoint.

#### Key Design

```
PK (String): org_id
```

#### Attributes

| Attribute | Type | Description |
|---|---|---|
| `org_id` | S | AWS Organization ID (PK) |
| `org_name` | S | Human-readable name |
| `last_successful_at` | S | ISO 8601 UTC of last successful collection |
| `last_attempted_at` | S | ISO 8601 UTC of last attempt |
| `last_error` | S (nullable) | Error message if last attempt failed, absent if success |
| `events_in_window` | N | Count of events collected in last run |
| `updated_at` | S | ISO 8601 UTC of this record's last update |

#### Example Item (org collection state)

```json
{
  "org_id":              "o-abc123def456",
  "org_name":            "Acme Corp",
  "last_successful_at":  "2026-03-13T12:00:00Z",
  "last_attempted_at":   "2026-03-13T12:00:00Z",
  "last_error":          null,
  "events_in_window":    9,
  "updated_at":          "2026-03-13T12:00:00Z"
}
```

#### Incident Items (alert_dispatcher correlation)

The same table also stores incident records written by `alert_dispatcher.py`. These use a different `pk` prefix and do **not** share the org-state schema.

```
PK (String): incident#{service}#{start_bucket}
```

`start_bucket` is the ISO UTC string of the `start_time` floored to the nearest `CORRELATION_WINDOW_MINUTES` boundary (e.g. `20260313T0800` for a 60-min window).

| Attribute | Type | Description |
|---|---|---|
| `pk` | S | `incident#{service}#{start_bucket}` |
| `service` | S | AWS service name (e.g. `EC2`) |
| `start_bucket` | S | `YYYYMMDDTHHMM` floor of earliest event start |
| `event_arns` | L | Deduplicated list of correlated event ARNs |
| `regions` | L | Deduplicated list of AWS regions |
| `org_ids` | L | Deduplicated list of org IDs |
| `severities` | L | Deduplicated list of severity values |
| `event_type_codes` | L | Deduplicated list of event type codes |
| `affected_account_count` | N | Maximum `affected_account_count` seen across all correlated events |
| `event_count` | N | Total number of correlated event ARNs |
| `first_seen` | S | ISO 8601 UTC when incident was first created |
| `last_updated` | S | ISO 8601 UTC of most recent merge |
| `alert_sent_at` | S (nullable) | ISO 8601 UTC when the first digest was published; absent until first alert |
| `last_alerted_account_count` | N | `affected_account_count` snapshot at last alert (used for re-alert threshold) |
| `last_alerted_regions` | L | Regions snapshot at last alert (used to detect spreading) |

#### Example Item (incident)

```json
{
  "pk":                         "incident#EC2#20260313T0800",
  "service":                    "EC2",
  "start_bucket":               "20260313T0800",
  "event_arns":                 ["arn:aws:health:us-east-1::event/EC2/...XYZ", "arn:aws:health:eu-west-1::event/EC2/...ABC"],
  "regions":                    ["us-east-1", "eu-west-1"],
  "org_ids":                    ["o-abc123def456", "o-xyz987uvw654"],
  "severities":                 ["critical"],
  "event_type_codes":           ["AWS_EC2_OPERATIONAL_ISSUE"],
  "affected_account_count":     142,
  "event_count":                2,
  "first_seen":                 "2026-03-13T08:02:00Z",
  "last_updated":               "2026-03-13T08:07:00Z",
  "alert_sent_at":              "2026-03-13T08:17:00Z",
  "last_alerted_account_count": 142,
  "last_alerted_regions":       ["us-east-1", "eu-west-1"]
}
```

> Incident items have no TTL — they persist until manually removed or until DynamoDB on-demand scaling would warrant cleanup. In practice volumes are low (one item per `(service, 60-min window)` per outage).

---

## SSM Parameter Store

### `/health-aggregator/orgs` (SecureString)

JSON array of org configurations. KMS-encrypted.

```json
[
  {
    "org_id":                      "o-abc123def456",
    "org_name":                    "Acme Corp",
    "delegated_admin_account_id":  "123456789012",
    "assume_role_arn":             "arn:aws:iam::123456789012:role/HealthAggregatorReadRole",
    "assume_role_external_id":     "acme-health-agg-2026",
    "enabled":                     true
  },
  {
    "org_id":                      "o-xyz987uvw654",
    "org_name":                    "Beta Industries",
    "delegated_admin_account_id":  "987654321098",
    "assume_role_arn":             "arn:aws:iam::987654321098:role/HealthAggregatorReadRole",
    "assume_role_external_id":     "beta-health-agg-2026",
    "enabled":                     true
  }
]
```

---

## Access Patterns Summary

| Query | Table/Index | Key Condition | Filter |
|---|---|---|---|
| All `issue` events in last 7 days | `category-starttime-index` | `category = issue AND start_time >= T-7d` | — |
| All `investigation` events in last 7 days | `category-starttime-index` | `category = investigation AND start_time >= T-7d` | — |
| Issues for a specific org | `category-starttime-index` | `category = issue AND start_time >= T-7d` | `org_id = X` |
| Issues for a specific service | `category-starttime-index` | `category = issue AND start_time >= T-7d` | `service = EC2` |
| Issues in production only | `category-starttime-index` | `category = issue AND start_time >= T-7d` | filter `affected_accounts[*].environment = production` (stored inline on item) |
| Event detail by ARN + org | Table (PK) | `pk = arn#org_id` | — |
| All event ARN records (for merge) | Table (PK) begins_with | `pk begins_with arn:...` _(not efficient — use GSI1 then group in Lambda)_ | — |
| All events for an org | `org-lastupdate-index` | `org_id = X AND last_updated_time >= T-7d` | — |
| Account metadata for an org | `account-metadata` table | `pk begins_with org_id#` _(batch_get by known account IDs)_ | — |
| Collection state per org | `collection-state` table | `pk = org_id` | — |

---

## Indexing Notes

### Why not use `event_arn` alone as PK?

The same `event_arn` can appear in multiple orgs (different affected account sets). Using `event_arn#org_id` as PK ensures each org's view of the event is stored independently, avoiding overwrite conflicts and preserving per-org affected account data.

### Why ISO 8601 strings for sort keys instead of epoch numbers?

DynamoDB Number sort keys support numeric range queries, but String sort keys support lexicographic range queries. ISO 8601 UTC strings in the format `YYYY-MM-DDTHH:MM:SS+00:00` sort correctly lexicographically. This avoids the need to convert timestamps in query code and keeps values human-readable when inspecting items in the AWS console.

### Why `category` as a GSI partition key?

Two values only (`issue`, `investigation`) means hot partition risk in theory. In practice, AWS Health events are low-volume (typically tens to hundreds per month across all orgs), so this is not a concern. If volume ever spikes, add a suffix shard (e.g., `issue#0`) and scatter-gather.
