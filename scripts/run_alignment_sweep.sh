#!/usr/bin/env bash
# Run MT-Bench judge alignment across 4 judges (3 cheap + 1 frontier).
# Produces a side-by-side table of agreement / kappa numbers.
#
# USAGE (from the verdict repo root):
#   bash scripts/run_alignment_sweep.sh
#
# Expected cost: ~$1-3 across all four judges.
# Expected runtime: 20-40 minutes.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

TS=$(date +%Y%m%d-%H%M%S)
OUT="${ALIGNMENT_OUT:-$REPO_DIR/research/results/alignment-sweep-$TS}"
mkdir -p "$OUT"
echo "Saving to: $OUT"

N="${ALIGNMENT_N:-50}"
echo "Pairs per judge: $N"
echo ""

run_judge () {
    local label="$1"
    local provider="$2"
    local model="$3"
    local file="$OUT/$label.txt"
    echo "── Running $label ($provider :: $model) ──"
    if python scripts/verify_judge_alignment.py --mode online \
            --provider "$provider" --judge-model "$model" --n "$N" \
            > "$file" 2>&1; then
        echo "   ✓ done → $(basename "$file")"
    else
        echo "   ✗ failed (exit $?) → $(basename "$file")"
    fi
}

run_judge "01_haiku"      "anthropic" "claude-haiku-4-5"
run_judge "02_gpt4omini"  "openai"    "gpt-4o-mini"
run_judge "03_gemini"     "google"    "gemini-2.5-flash"
run_judge "04_sonnet"     "anthropic" "claude-sonnet-4-5"

# Build the side-by-side table
{
    echo "# Judge alignment sweep — $TS"
    echo ""
    echo "Pairs per judge: $N"
    echo ""
    echo "## Headline numbers"
    echo ""
    echo "| Judge | Provider :: model | 3-way κ | Binarized κ | Non-tie agree | Inconsistent |"
    echo "|---|---|---:|---:|---:|---:|"
    for f in "$OUT"/0*.txt; do
        [ -f "$f" ] || continue
        bn=$(basename "$f" .txt)
        judge_line=$(grep -E "^  Judge:" "$f" | head -1 | sed 's/^  Judge:[[:space:]]*//')
        # Find the 3-way kappa: the first "Cohen's κ:" line after "3-way kappa"
        k3=$(awk '/3-way kappa/,/Binarized/' "$f" | grep -oE "Cohen.s [κk]:[[:space:]]+[-]?[0-9]+\.[0-9]+" | head -1 | grep -oE "[-]?[0-9]+\.[0-9]+")
        # Find the binarized kappa: the "Cohen's κ:" line between "Binarized" and "Non-tie"
        kbin=$(awk '/Binarized/,/Non-tie/' "$f" | grep -oE "Cohen.s [κk]:[[:space:]]+[-]?[0-9]+\.[0-9]+" | head -1 | grep -oE "[-]?[0-9]+\.[0-9]+")
        # Non-tie agreement (the "Agreement:" line in the Non-tie section)
        nontie=$(awk '/Non-tie/,/Interpretation/' "$f" | grep -oE "Agreement:[[:space:]]+[0-9]+\.[0-9]+" | head -1 | grep -oE "[0-9]+\.[0-9]+")
        incon=$(grep -oE "Inconsistent \(swap\):[[:space:]]+[0-9]+" "$f" | head -1 | grep -oE "[0-9]+$")
        echo "| $bn | $judge_line | ${k3:-—} | ${kbin:-—} | ${nontie:-—} | ${incon:-—} |"
    done
    echo ""
    echo "## Per-judge details"
    echo ""
    for f in "$OUT"/0*.txt; do
        [ -f "$f" ] || continue
        echo "### $(basename "$f" .txt)"
        echo '```'
        tail -25 "$f"
        echo '```'
        echo ""
    done
} > "$OUT/SUMMARY.md"

echo ""
echo "=== Done ==="
echo "Summary: $OUT/SUMMARY.md"
echo ""
cat "$OUT/SUMMARY.md" | head -20
