#!/usr/bin/env bash
# Build a clean distribution zip for gis-assistant-mcp.
# Produces dist/gis-assistant-mcp.zip
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

cd "$REPO_ROOT"
zip -rX "$DIST_DIR/gis-assistant-mcp.zip" \
    src/ pyproject.toml README.md LICENSE .python-version uv.lock \
    -x "*.pyc" \
    -x "__pycache__/*" \
    -x "*/__pycache__/*" \
    -x ".venv/*" \
    -x ".git/*" \
    -x ".mypy_cache/*" \
    -x ".pytest_cache/*" \
    -x "dist/*" \
    -x "*.egg-info/*"

echo "Built artifact in $DIST_DIR:"
ls -lh "$DIST_DIR"
