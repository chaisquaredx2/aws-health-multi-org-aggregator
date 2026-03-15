"""
health_proxy_client.py

Calls the internal Health Proxy API Gateway (reachable via execute-api VPC
endpoint) instead of calling health.us-east-1.amazonaws.com directly.

Why: AWS Health API has no VPC Interface Endpoint. This module signs requests
for the execute-api service (the API GW layer); the API GW's own IAM execution
role then signs and forwards each call to health.us-east-1.amazonaws.com.

Multi-org note: The API GW integration role must be (or have access to) a
delegated Health admin for the org whose events you want. For a single org
(aggregator = delegated admin) one proxy covers everything. For truly separate
AWS Organizations each org needs its own proxy deployment — see SPEC.md §10.
"""

import json
import logging
import time
from typing import Optional

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)


class HealthAPIError(Exception):
    pass


class ThrottlingError(HealthAPIError):
    pass


class HealthProxyClient:
    """
    Thin HTTP client over the Health Proxy API Gateway.

    Signs every call with SigV4 for the `execute-api` service so the private
    API GW method (auth = AWS_IAM) accepts it. The API GW integration then
    forwards to health.us-east-1.amazonaws.com with its own credentials.

    Pagination contract:
      Every paginated method loops internally until `nextToken` is absent and
      returns a flat list. Callers get a complete result set in one call with
      no pagination awareness required.
    """

    MAX_RESULTS = 100  # max per page for all Health org API methods
    MAX_RETRIES = 4
    BASE_RETRY_DELAY_S = 1  # doubles on each retry (exponential back-off)

    def __init__(self, api_base_url: str, region: str = "us-east-1") -> None:
        """
        Args:
            api_base_url: Stage URL of the health proxy API GW, e.g.
                          https://{api-id}.execute-api.us-east-1.amazonaws.com/prod
                          Resolved privately via the execute-api VPC endpoint.
            region:       AWS region used for SigV4 signing (always us-east-1;
                          the Health API only exists in that region).
        """
        self.api_base_url = api_base_url.rstrip("/")
        self.region = region

        session = boto3.Session()
        self.credentials = session.get_credentials()
        self.http = requests.Session()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _signed_post(self, path: str, body: dict) -> dict:
        """
        POST `body` to `{api_base_url}{path}` signed with execute-api SigV4.

        The Content-Type sent to API GW is application/json. The API GW
        integration sets Content-Type: application/x-amz-json-1.1 on the
        outbound call to the Health service via a request_parameters mapping
        in Terraform — Lambda does not need to set it.
        """
        url = f"{self.api_base_url}{path}"
        body_bytes = json.dumps(body).encode("utf-8")

        aws_req = AWSRequest(
            method="POST",
            url=url,
            data=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(self.credentials, "execute-api", self.region).add_auth(aws_req)

        resp = self.http.send(aws_req.prepare())

        if resp.status_code == 429 or (
            resp.status_code == 400 and "ThrottlingException" in resp.text
        ):
            raise ThrottlingError(f"Throttled on {path}: {resp.text}")

        if resp.status_code != 200:
            raise HealthAPIError(
                f"Health proxy {path} returned HTTP {resp.status_code}: {resp.text}"
            )

        return resp.json()

    def _call(self, path: str, body: dict) -> dict:
        """Wraps _signed_post with exponential back-off on throttling."""
        for attempt in range(self.MAX_RETRIES):
            try:
                return self._signed_post(path, body)
            except ThrottlingError:
                if attempt == self.MAX_RETRIES - 1:
                    raise
                delay = self.BASE_RETRY_DELAY_S * (2 ** attempt)
                logger.warning(
                    "Throttled calling %s (attempt %d/%d), retrying in %ss",
                    path, attempt + 1, self.MAX_RETRIES, delay,
                )
                time.sleep(delay)
        raise HealthAPIError("Unreachable")  # satisfies type checker

    # ── Paginated API methods ─────────────────────────────────────────────────

    def describe_events_for_organization(
        self,
        categories: list,
        last_updated_from: str,
    ) -> list:
        """
        Collect every health event matching the filter, across all pages.

        Args:
            categories:        e.g. ['issue', 'investigation']
            last_updated_from: ISO 8601 UTC lower bound for lastUpdatedTime,
                               e.g. '2026-03-06T12:00:00Z' (7 days ago).

        Returns:
            Flat list of event dicts. Fields include:
              arn, service, eventTypeCode, eventTypeCategory, region,
              statusCode, startTime, lastUpdatedTime, endTime (optional).

        Pagination loop:
            Requests MAX_RESULTS items per page. Passes nextToken from each
            response back as nextToken in the next request. Stops when the
            response contains no nextToken field.
        """
        events: list = []
        next_token: Optional[str] = None
        page = 0

        while True:
            body: dict = {
                "filter": {
                    "eventTypeCategories": categories,
                    "lastUpdatedTimes": [{"from": last_updated_from}],
                },
                "maxResults": self.MAX_RESULTS,
            }
            if next_token:
                body["nextToken"] = next_token

            result = self._call("/describe-events-for-organization", body)

            batch = result.get("events", [])
            events.extend(batch)
            page += 1
            logger.debug(
                "describe_events page=%d batch=%d total=%d",
                page, len(batch), len(events),
            )

            next_token = result.get("nextToken")
            if not next_token:
                break

        logger.info("describe_events_for_organization: %d events across %d pages", len(events), page)
        return events

    def describe_affected_accounts_for_organization(
        self,
        event_arn: str,
    ) -> list:
        """
        Collect every account ID affected by `event_arn`, across all pages.

        Args:
            event_arn: ARN of the health event.

        Returns:
            Flat list of 12-digit account ID strings.

        Pagination loop:
            Same nextToken pattern as describe_events_for_organization.
        """
        accounts: list = []
        next_token: Optional[str] = None

        while True:
            body: dict = {
                "eventArn": event_arn,
                "maxResults": self.MAX_RESULTS,
            }
            if next_token:
                body["nextToken"] = next_token

            result = self._call(
                "/describe-affected-accounts-for-organization", body
            )

            accounts.extend(result.get("affectedAccounts", []))
            next_token = result.get("nextToken")
            if not next_token:
                break

        return accounts

    def describe_event_details_for_organization(
        self,
        event_arns: list,
        account_id: Optional[str] = None,
    ) -> dict:
        """
        Fetch event description text for up to N event ARNs.

        The API accepts at most 10 ARNs per call; this method batches
        automatically and merges results.

        Args:
            event_arns: List of event ARNs (any length).
            account_id: Optional — scope description to a specific account.

        Returns:
            {'successfulSet': [...], 'failedSet': [...]}
        """
        merged: dict = {"successfulSet": [], "failedSet": []}

        for i in range(0, len(event_arns), 10):
            chunk = event_arns[i : i + 10]
            filters = [{"eventArn": arn} for arn in chunk]
            if account_id:
                filters = [
                    {"eventArn": arn, "awsAccountId": account_id} for arn in chunk
                ]

            result = self._call(
                "/describe-event-details-for-organization",
                {"organizationEventDetailFilters": filters, "locale": "en"},
            )
            merged["successfulSet"].extend(result.get("successfulSet", []))
            merged["failedSet"].extend(result.get("failedSet", []))

        return merged

    def describe_affected_entities_for_organization(
        self,
        event_arn: str,
        account_id: str,
    ) -> list:
        """
        Collect every affected entity for (event_arn, account_id), all pages.

        Used by the /v1/events/{arn}/details API endpoint (Phase 2).
        Entities are resource-level identifiers (instance IDs, bucket names…).

        Args:
            event_arn:  ARN of the health event.
            account_id: 12-digit AWS account ID.

        Returns:
            Flat list of entity dicts with entityValue, entityType, statusCode.
        """
        entities: list = []
        next_token: Optional[str] = None

        while True:
            body: dict = {
                "organizationEntityFilters": [
                    {"eventArn": event_arn, "awsAccountId": account_id}
                ],
                "maxResults": self.MAX_RESULTS,
            }
            if next_token:
                body["nextToken"] = next_token

            result = self._call(
                "/describe-affected-entities-for-organization", body
            )
            entities.extend(result.get("entities", []))
            next_token = result.get("nextToken")
            if not next_token:
                break

        return entities
