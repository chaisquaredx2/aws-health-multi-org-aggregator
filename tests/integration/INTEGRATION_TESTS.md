# Integration Test Strategy

## Goal

Validate end-to-end correctness of the collector, API, and exporter Lambda packages
against real (or moto-emulated) AWS service calls. Integration tests complement unit
tests by exercising the interaction between components — DynamoDB GSI queries, STS
credential chaining, SNS publish, S3 round-trips, etc.

---

## Test Infrastructure

### Option A — moto (recommended for CI)

`moto` intercepts boto3 calls at the HTTP layer and emulates DynamoDB, S3, SNS, SSM,
STS, and CloudWatch. No AWS account is required.

```python
import pytest
from moto import mock_aws
import boto3

@pytest.fixture(scope="session")
def aws_credentials():
    # Already set by tests/conftest.py
    pass

@pytest.fixture
def ddb_tables(aws_credentials):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        events_table = ddb.create_table(
            TableName="test-events",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "category", "AttributeType": "S"},
                {"AttributeName": "start_time", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "category-starttime-index",
                "KeySchema": [
                    {"AttributeName": "category", "KeyType": "HASH"},
                    {"AttributeName": "start_time", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        state_table = ddb.create_table(
            TableName="test-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield events_table, state_table
```

### Option B — AWS LocalStack (for richer emulation)

Run `localstack start` locally or in CI via the official Docker image:

```yaml
# docker-compose.yml (CI)
services:
  localstack:
    image: localstack/localstack
    environment:
      - SERVICES=dynamodb,s3,sns,ssm,sts,lambda,cloudwatch
    ports:
      - "4566:4566"
```

Point boto3 at LocalStack with `endpoint_url="http://localhost:4566"`.

### Option C — Real AWS account (sandbox)

Set `AWS_PROFILE=sandbox` and run against a dedicated integration test account.
Use resource names prefixed with `inttest-` and clean up in a `finally` block or
pytest fixture teardown. This is the highest-fidelity option but requires IAM setup.

---

## Test Scenarios

### 1. Collector — Full pipeline per org

**Description:** Simulate `_collect_org()` end-to-end with a mocked Health Proxy API and
real (moto) DynamoDB/STS.

**Setup:**
- Create `test-events` table with GSI
- Create `test-state` table
- Seed SSM `/health-aggregator/orgs` with one enabled org
- Stub `HealthProxyClient._signed_post` to return fixture events

**Assertions:**
- [ ] Event items written to DynamoDB with correct `pk`, `sk`, and all enriched fields
- [ ] `affected_accounts` contains only accounts present in `account_map`
- [ ] `is_operational` and `severity` populated from `event_classifier`
- [ ] Collection state written to `test-state` with `last_successful_at`
- [ ] CloudWatch metric emitted with `EventsCollected` value

**Test function sketch:**
```python
@mock_aws
def test_collect_org_writes_events_to_dynamo(ddb_tables):
    events_table, state_table = ddb_tables
    # stub HealthProxyClient
    # stub load_account_map
    count, items = _collect_org(org, health_client, window_start)
    assert count > 0
    scan = events_table.scan()
    assert len(scan["Items"]) == count
```

---

### 2. Collector — Alert dispatch after collection

**Description:** Run `dispatch_alerts()` with a set of open events in DynamoDB and
verify SNS messages are published.

**Setup:**
- Seed `test-state` with no prior alert state (fresh incident)
- Prepare a list of `all_written_items` with `status=open, is_operational=True`
- Create SNS topic in moto

**Assertions:**
- [ ] SNS `publish()` called once for each new incident group
- [ ] SNS message contains `service`, `affected_accounts`, `priority` attributes
- [ ] `_mark_alerted` updates state table with `last_alerted_account_count`

---

### 3. API — GET /v1/events

**Description:** Write items directly to DynamoDB and call the API handler end-to-end.

**Setup:**
- Create `test-events` table with GSI
- Write 5 `issue` items and 2 `investigation` items
- Call `api_handler.handler({"httpMethod": "GET", "path": "/v1/events", "queryStringParameters": {"category": "issue"}})`

**Assertions:**
- [ ] `statusCode` == 200
- [ ] `data` contains 5 merged events
- [ ] `meta.total` == 5
- [ ] Filtered by `service=EC2` returns only EC2 events
- [ ] Paginated response with `next_token` when > page_size events exist
- [ ] Second call with `next_token` returns next page

---

### 4. API — GET /v1/events/{arn_b64}/details

**Description:** Write one event to DynamoDB, call details endpoint.

**Assertions:**
- [ ] Returns 200 with the event's full fields
- [ ] Returns 404 for an unknown ARN
- [ ] Multi-org: two records for same ARN merged into `affected_orgs[]`

---

### 5. API — GET /v1/summary

**Description:** Write a mix of open/closed issues and investigations, verify aggregate
counts.

**Assertions:**
- [ ] `issues.total`, `issues.open`, `issues.closed` match seeded data
- [ ] `investigations.total` match seeded data
- [ ] `top_affected_services` returns correct top N (max 10)
- [ ] `affected_account_count` deduplicates across events
- [ ] `by_org` breakdown correct per org

---

### 6. API — GET /v1/orgs

**Description:** Seed SSM and DynamoDB state, verify list_orgs returns merged result.

**Assertions:**
- [ ] Each org entry has `org_id`, `org_name`, `enabled`, `collection`
- [ ] `collection.events_in_window` matches state table value
- [ ] `collection.last_error` populated when failure state exists

---

### 7. Exporter — Excel generation and S3 upload

**Description:** Run `exporter_handler.handler()` with real moto S3 and DynamoDB.

**Setup:**
- Create `test-events` DynamoDB table with 10 items
- Create `test-export-bucket` S3 bucket

**Assertions:**
- [ ] Excel file appears in S3 at `exports/YYYY/MM/DD/aws-health-events.xlsx`
- [ ] File is non-empty and has PK ZIP header bytes
- [ ] `exports/state/open_arns.json` written with current open ARNs
- [ ] `exports/delta-log/delta_log.json` written
- [ ] Return value contains `statusCode=200`, `events_exported`, `delta_new`, `delta_resolved`

---

### 8. Exporter — Delta computation across two runs

**Description:** Run the exporter twice: first run with no prior state, second run
after one event closes.

**Run 1:**
- 3 events all `status=open`
- Expected: `delta_new=3`, `delta_resolved=0`

**Run 2:**
- Same events, one flipped to `status=closed`
- Expected: `delta_new=0`, `delta_resolved=1`

**Assertions:**
- [ ] `delta_log.json` contains 5 total rows after both runs
- [ ] Row `delta_type=resolved` has the closed ARN

---

### 9. Health Proxy Client — Pagination and retry

**Description:** Stub the HTTP transport to return paginated responses with a
`nextToken`, then a 429 followed by a 200.

**Assertions:**
- [ ] All pages concatenated in result
- [ ] `time.sleep` called on 429 retry
- [ ] Final result contains events from all pages

---

## Running Integration Tests

```bash
# Install deps
pip install -r requirements-test.txt

# Run only integration tests
pytest tests/integration/ -v

# Run with coverage
pytest tests/ --cov=lambda --cov-report=term-missing
```

## CI Configuration

Add to GitHub Actions workflow:

```yaml
- name: Run tests
  run: |
    pip install -r requirements-test.txt
    pytest tests/ --cov=lambda --cov-report=xml --cov-fail-under=90
```

---

## Coverage Targets

| Package     | Target | Notes                                        |
|-------------|--------|----------------------------------------------|
| collector/  | ≥ 90%  | handler, account_cache, alert_dispatcher     |
| api/        | ≥ 90%  | handler, all routes, pagination, response    |
| exporter/   | ≥ 90%  | handler, excel_writer                        |
| shared/     | 100%   | health_proxy_client                          |

Run `pytest --cov=lambda --cov-report=term-missing` to check current coverage.
