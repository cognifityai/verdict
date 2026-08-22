#!/usr/bin/env bash
# Run MT-Bench judge alignment across 4 judges (3 cheap + 1 frontier).
# Produces machine-readable per-judge results and a side-by-side Markdown table.
#
# USAGE (from the verdict repo root):
#   bash scripts/run_alignment_sweep.sh
#   bash scripts/run_alignment_sweep.sh --mode offline  # key-free wiring check
#
# Expected cost: ~$1-3 across all four judges.
# Expected runtime: 20-40 minutes.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

exec python scripts/run_alignment_sweep.py "$@"
