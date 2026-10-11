#!/usr/bin/env bash
# Vercel build step for the Python projects (root directory = deploy/vercel/<project>):
# the function bundle needs the ytcore package from the repository root.
set -euo pipefail
rm -rf ytcore config
cp -r ../../../ytcore ./ytcore
cp -r ../../../config ./config
find ytcore -name __pycache__ -type d -prune -exec rm -rf {} +
echo "<!doctype html><title>yt-agentic</title>" > public/index.html
