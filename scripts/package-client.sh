#!/bin/bash
# Local equivalent of the GitHub Actions build (for testing). Produces iim-client.tar.gz in repo root.
set -e
cd "$(dirname "$0")/.."
DIST=iim-client-dist
rm -rf "$DIST"
mkdir -p "$DIST/templates"
cp client/app.py                 "$DIST/"
cp client/templates/index.html   "$DIST/templates/"
cp client/install.sh             "$DIST/"
cp client/ii-manager.service     "$DIST/"
chmod +x "$DIST/install.sh"
tar -czf iim-client.tar.gz "$DIST"
rm -rf "$DIST"
echo "Built: $(pwd)/iim-client.tar.gz ($(du -h iim-client.tar.gz | cut -f1))"
