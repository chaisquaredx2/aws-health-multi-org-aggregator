#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/.build"
mkdir -p "$BUILD"

echo "==> Installing collector dependencies..."
pip install -r "$ROOT/lambda/collector/requirements.txt" \
    -t "$ROOT/lambda/collector/" --quiet

echo "==> Installing api dependencies..."
pip install -r "$ROOT/lambda/api/requirements.txt" \
    -t "$ROOT/lambda/api/" --quiet

# health_proxy_client lives in lambda/shared/ — copy into each package before zip.
echo "==> Distributing shared health_proxy_client to collector and api packages..."
cp "$ROOT/lambda/shared/health_proxy_client.py" \
   "$ROOT/lambda/collector/health_proxy_client.py"
cp "$ROOT/lambda/shared/health_proxy_client.py" \
   "$ROOT/lambda/api/health_proxy_client.py"

echo "==> Installing exporter dependencies..."
pip install -r "$ROOT/lambda/exporter/requirements.txt" \
    -t "$ROOT/lambda/exporter/" --quiet

echo "==> Terraform init..."
cd "$ROOT/terraform"
terraform init

echo "==> Terraform plan..."
terraform plan -out="$BUILD/tfplan"

echo "==> Terraform apply..."
terraform apply "$BUILD/tfplan"

echo ""
echo "==> Outputs:"
terraform output
