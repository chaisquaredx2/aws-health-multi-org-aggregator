#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# test_collection.sh — Manually trigger the collector Lambda and tail its logs
#
# Usage:
#   ./scripts/test_collection.sh [OPTIONS]
#
# Options:
#   -f, --function   FUNCTION_NAME  Lambda function name or ARN
#                                   (default: health-aggregator-collector)
#   -R, --region     AWS_REGION     AWS region (default: us-east-1)
#   -t, --tail-mins  MINUTES        How many minutes of logs to tail after
#                                   invocation (default: 5)
#   -s, --sync                      Wait for Lambda to complete and print
#                                   response payload (default: async/Event)
#   -h, --help                      Show this help
#
# Examples:
#   # Trigger with defaults and watch logs for 5 minutes
#   ./scripts/test_collection.sh
#
#   # Trigger a specific function name synchronously
#   ./scripts/test_collection.sh -f my-project-collector -s
#
#   # Trigger and tail logs for 10 minutes
#   ./scripts/test_collection.sh -t 10
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
FUNCTION_NAME="health-aggregator-collector"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
TAIL_MINS=5
SYNC=false

# ── Argument parsing ───────────────────────────────────────────────────────────
usage() {
  sed -n '/^# Usage:/,/^# ─/p' "$0" | head -n -1 | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -f|--function)  FUNCTION_NAME="$2"; shift 2 ;;
    -R|--region)    AWS_REGION="$2";    shift 2 ;;
    -t|--tail-mins) TAIL_MINS="$2";     shift 2 ;;
    -s|--sync)      SYNC=true;          shift   ;;
    -h|--help)      usage ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PAYLOAD='{"source":"manual-test","detail-type":"Scheduled Event"}'
TMPFILE="$(mktemp /tmp/lambda-response-XXXXXX.json)"
trap 'rm -f "$TMPFILE"' EXIT

# ── Determine log group ────────────────────────────────────────────────────────
LOG_GROUP="/aws/lambda/${FUNCTION_NAME}"

# Strip any ARN prefix so the log group name is predictable
if [[ "$FUNCTION_NAME" == arn:* ]]; then
  # Extract function name from ARN: arn:aws:lambda:region:acct:function:name
  FUNCTION_SHORT="$(echo "$FUNCTION_NAME" | awk -F: '{print $7}')"
  LOG_GROUP="/aws/lambda/${FUNCTION_SHORT}"
fi

# ── Invoke ─────────────────────────────────────────────────────────────────────
START_EPOCH="$(date +%s)"  # used later for log filtering

if [[ "$SYNC" == "true" ]]; then
  echo "==> Invoking ${FUNCTION_NAME} synchronously (RequestResponse)..."
  aws lambda invoke \
    --function-name "$FUNCTION_NAME" \
    --invocation-type RequestResponse \
    --payload "$PAYLOAD" \
    --cli-binary-format raw-in-base64-out \
    --region "$AWS_REGION" \
    --log-type Tail \
    "$TMPFILE" \
    --query "LogResult" \
    --output text | base64 --decode || true

  echo ""
  echo "==> Response payload:"
  python3 -m json.tool "$TMPFILE" 2>/dev/null || cat "$TMPFILE"
else
  echo "==> Invoking ${FUNCTION_NAME} asynchronously (Event)..."
  aws lambda invoke \
    --function-name "$FUNCTION_NAME" \
    --invocation-type Event \
    --payload "$PAYLOAD" \
    --cli-binary-format raw-in-base64-out \
    --region "$AWS_REGION" \
    "$TMPFILE" > /dev/null
  echo "    Invocation accepted (HTTP 202). Lambda is running asynchronously."
fi

# ── Tail CloudWatch logs ───────────────────────────────────────────────────────
echo ""
echo "==> Tailing logs from ${LOG_GROUP} for ${TAIL_MINS} minutes..."
echo "    (Press Ctrl+C to stop early)"
echo ""

START_MS=$(( START_EPOCH * 1000 ))
END_EPOCH=$(( START_EPOCH + TAIL_MINS * 60 ))

# Poll logs every 5 seconds until TAIL_MINS have elapsed
SEEN_EVENTS=""
while [[ "$(date +%s)" -lt "$END_EPOCH" ]]; do
  NEW_EVENTS="$(
    aws logs filter-log-events \
      --log-group-name "$LOG_GROUP" \
      --start-time "$START_MS" \
      --region "$AWS_REGION" \
      --query "events[].message" \
      --output text 2>/dev/null || true
  )"

  if [[ -n "$NEW_EVENTS" && "$NEW_EVENTS" != "$SEEN_EVENTS" ]]; then
    # Print only lines not already printed
    DIFF="$(comm -23 \
      <(echo "$NEW_EVENTS" | sort) \
      <(echo "$SEEN_EVENTS" | sort) \
    )"
    [[ -n "$DIFF" ]] && echo "$DIFF"
    SEEN_EVENTS="$NEW_EVENTS"
  fi

  sleep 5
done

echo ""
echo "==> Log tail complete."
echo ""

# ── Quick stats check ──────────────────────────────────────────────────────────
echo "==> Checking CloudWatch custom metrics (HealthAggregator namespace)..."
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
WINDOW_START="$(date -u -v-${TAIL_MINS}M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -d "-${TAIL_MINS} minutes" +%Y-%m-%dT%H:%M:%SZ)"

for METRIC in EventsCollected CollectionErrors; do
  VALUE="$(
    aws cloudwatch get-metric-statistics \
      --namespace HealthAggregator \
      --metric-name "$METRIC" \
      --dimensions Name=OrgId,Value=all \
      --start-time "$WINDOW_START" \
      --end-time "$NOW" \
      --period $(( TAIL_MINS * 60 )) \
      --statistics Sum \
      --region "$AWS_REGION" \
      --query "Datapoints[0].Sum" \
      --output text 2>/dev/null || echo "N/A"
  )"
  printf "    %-25s %s\n" "$METRIC" "$VALUE"
done

echo ""
echo "==> Done. To query collected events, hit the consumer API:"
echo "    curl -s -H 'x-api-key: YOUR_KEY' \\"
echo "      'https://YOUR_API_ID.execute-api.${AWS_REGION}.amazonaws.com/prod/v1/events?category=issue' \\"
echo "      | python3 -m json.tool | head -60"
