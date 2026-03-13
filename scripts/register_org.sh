#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# register_org.sh — Add or update an org entry in SSM Parameter Store
#
# Usage:
#   ./scripts/register_org.sh [OPTIONS]
#
# Options:
#   -n, --name        NAME        Display name for the org (required)
#   -i, --org-id      ORG_ID     AWS Org ID, e.g. o-abc123def45 (required)
#   -a, --account-id  ACCOUNT_ID Delegated admin account ID for this org (required)
#   -r, --role        ROLE_NAME  Cross-org IAM role name to assume
#                                (default: HealthAggregatorReadRole)
#   -p, --param       PARAM_PATH SSM parameter path
#                                (default: /health-aggregator/orgs)
#   -R, --region      AWS_REGION AWS region (default: us-east-1)
#   -d, --delete               Remove the org entry instead of adding
#   -h, --help                 Show this help
#
# Examples:
#   # Add org-1
#   ./scripts/register_org.sh -n "Acme Corp" -i o-abc123def45 -a 123456789012
#
#   # Add org-2 with a non-default role name
#   ./scripts/register_org.sh -n "Beta LLC" -i o-xyz987ghi65 -a 987654321098 \
#       -r "MyHealthReaderRole"
#
#   # Remove an org
#   ./scripts/register_org.sh -i o-abc123def45 --delete
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
PARAM_PATH="/health-aggregator/orgs"
ROLE_NAME="HealthAggregatorReadRole"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
DELETE=false

ORG_NAME=""
ORG_ID=""
ACCOUNT_ID=""

# ── Argument parsing ───────────────────────────────────────────────────────────
usage() {
  sed -n '/^# Usage:/,/^# ─/p' "$0" | head -n -1 | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -n|--name)        ORG_NAME="$2";    shift 2 ;;
    -i|--org-id)      ORG_ID="$2";     shift 2 ;;
    -a|--account-id)  ACCOUNT_ID="$2"; shift 2 ;;
    -r|--role)        ROLE_NAME="$2";  shift 2 ;;
    -p|--param)       PARAM_PATH="$2"; shift 2 ;;
    -R|--region)      AWS_REGION="$2"; shift 2 ;;
    -d|--delete)      DELETE=true;     shift   ;;
    -h|--help)        usage ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$ORG_ID" ]]; then
  echo "ERROR: --org-id is required." >&2
  exit 1
fi

# ── Delete path ────────────────────────────────────────────────────────────────
if [[ "$DELETE" == "true" ]]; then
  echo "==> Fetching current registry from SSM..."
  CURRENT_JSON="$(
    aws ssm get-parameter \
      --name "$PARAM_PATH" \
      --with-decryption \
      --region "$AWS_REGION" \
      --query "Parameter.Value" \
      --output text 2>/dev/null || echo "[]"
  )"

  UPDATED_JSON="$(
    echo "$CURRENT_JSON" | python3 -c "
import json, sys
orgs = json.load(sys.stdin)
orgs = [o for o in orgs if o.get('org_id') != '${ORG_ID}']
print(json.dumps(orgs, indent=2))
"
  )"

  echo "==> Writing updated registry (removed org_id=${ORG_ID})..."
  aws ssm put-parameter \
    --name "$PARAM_PATH" \
    --value "$UPDATED_JSON" \
    --type "SecureString" \
    --overwrite \
    --region "$AWS_REGION"

  echo "Done. Remaining orgs:"
  echo "$UPDATED_JSON" | python3 -c "
import json, sys
for o in json.load(sys.stdin):
    print(f\"  {o['org_id']:22}  {o.get('name','')}\")
"
  exit 0
fi

# ── Add / update path ──────────────────────────────────────────────────────────
if [[ -z "$ORG_NAME" || -z "$ACCOUNT_ID" ]]; then
  echo "ERROR: --name and --account-id are required when adding an org." >&2
  exit 1
fi

echo "==> Fetching current registry from SSM..."
CURRENT_JSON="$(
  aws ssm get-parameter \
    --name "$PARAM_PATH" \
    --with-decryption \
    --region "$AWS_REGION" \
    --query "Parameter.Value" \
    --output text 2>/dev/null || echo "[]"
)"

# Build new entry and upsert (replace if org_id already exists)
UPDATED_JSON="$(
  echo "$CURRENT_JSON" | python3 -c "
import json, sys
orgs = json.load(sys.stdin)
new_entry = {
    'org_id':     '${ORG_ID}',
    'name':       '${ORG_NAME}',
    'account_id': '${ACCOUNT_ID}',
    'role_name':  '${ROLE_NAME}',
}
orgs = [o for o in orgs if o.get('org_id') != new_entry['org_id']]
orgs.append(new_entry)
print(json.dumps(orgs, indent=2))
"
)"

echo "==> Preview of new registry:"
echo "$UPDATED_JSON"
echo ""

read -r -p "Write to SSM parameter '${PARAM_PATH}'? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
  echo "Aborted."
  exit 0
fi

aws ssm put-parameter \
  --name "$PARAM_PATH" \
  --value "$UPDATED_JSON" \
  --type "SecureString" \
  --overwrite \
  --region "$AWS_REGION"

echo ""
echo "==> Done. Org '${ORG_ID}' (${ORG_NAME}) registered in ${PARAM_PATH}."
echo "    The collector Lambda reads this parameter at invocation time."
