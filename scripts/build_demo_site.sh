#!/usr/bin/env bash
# Builds the clickable design preview (sample data, no backend) into site/hub-demo.
set -euo pipefail
cd "$(dirname "$0")/../ui/hub"
npm ci --no-audit --no-fund
NEXT_PUBLIC_DEMO=1 npx next build
rm -rf ../../site/hub-demo
mkdir -p ../../site/hub-demo
cp -r out/. ../../site/hub-demo/
