#!/usr/bin/env bash
# Run the test suite on the target machine (it has the browser): scripts/test-remote.sh [pytest args]
set -euo pipefail
source "$(dirname "$0")/_remote.sh"
DEST="${SCRAPE_TEST_DIR:-.cache/scrape-api-test}"

ship "$DEST"
remote "cd $(q "$DEST") && uv sync --frozen --quiet && uv run --frozen pytest $(quote_args "$@")"
