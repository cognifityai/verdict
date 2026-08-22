"""Assemble raw (query, response, context) examples into a JSONL ready for labeling.

The maintainer needs ~30-100 real (query, response) pairs so they can hand-label
PASS/FAIL per dimension and then run verify_rubric_alignment.py to measure how well
the judge agrees. This script is step 0: it produces that raw.jsonl from whatever
source they happen to have — a Verdict SQLite DB, an arbitrary JSONL export, or (if
`datasets` is installed) a public dataset to practice labeling on.

OUTPUT FORMAT (one JSON object per line):
    {"query": "...", "response": "...", "context": ""}

This is exactly what `verify_rubric_alignment.py --make-template` consumes.

SOURCES (via --source):

  sqlite   Read from a Verdict SQLite DB's `traces` table. Uses `prompt_redacted`
           as the query and `response_redacted` as the response. Rows where either
           is NULL/empty are skipped. --sample recent orders by started_at DESC;
           --sample random orders by RANDOM(). Output is redacted again so rows
           written before the current storage boundary cannot leak through the
           labeling export.

  jsonl    Pass through / normalize arbitrary JSONL rows that already carry
           query/response-ish fields. Common key names are mapped:
             query|prompt|question            -> query
             response|answer|completion|output -> response
             context|retrieved_context         -> context
           Rows missing a query OR a response are skipped.

  mtbench  OPTIONAL. If the `datasets` lib is importable, pull a few (query, response)
           pairs from a public dataset as practice-labeling material. If `datasets`
           is NOT installed, print a clear message and exit 0 (does not crash).

USAGE:
    # From a Verdict SQLite DB (most recent 50 traces):
    python scripts/sample_to_label.py --source sqlite --db verdict.db --out raw.jsonl

    # A random sample of 80:
    python scripts/sample_to_label.py --source sqlite --db verdict.db \
        --sample random --limit 80 --out raw.jsonl

    # Normalize an existing JSONL export:
    python scripts/sample_to_label.py --source jsonl --in dump.jsonl --out raw.jsonl

    # Practice rows from a public dataset (needs `datasets`):
    python scripts/sample_to_label.py --source mtbench --limit 40 --out raw.jsonl

Then, to build a labeling template from the raw.jsonl this produced:
    python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from verdict.redaction import redact

# Key aliases for the jsonl source. First hit (in listed order) wins.
_QUERY_KEYS = ("query", "prompt", "question")
_RESPONSE_KEYS = ("response", "answer", "completion", "output")
_CONTEXT_KEYS = ("context", "retrieved_context")


def _first_str(row: dict, keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among `keys`, else ""."""
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = v if isinstance(v, str) else str(v)
        if s.strip():
            return s
    return ""


def _write_rows(out_path: str, rows: list[dict]) -> int:
    """Write normalized rows through the final redaction boundary as JSONL."""
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps({
                "query": redact(r["query"]),
                "response": redact(r["response"]),
                "context": redact(r.get("context", "")),
            }) + "\n")
    return len(rows)


# -- sqlite source --------------------------------------------------------------

def from_sqlite(db: str, limit: int, sample: str,
                dedupe_by_prompt: bool = False) -> tuple[list[dict], int]:
    """Read (query, response) from the traces table. Returns (rows, skipped).

    With dedupe_by_prompt=True, keep only ONE (query, response) per distinct
    prompt — useful when the DB repeats the same prompts many times (e.g. an
    experiment that sent the same 50 prompts across providers/rounds), so your
    labeling effort covers diverse questions instead of duplicates.
    """
    order = "started_at DESC" if sample == "recent" else "RANDOM()"
    # Read-only connect so we never mutate the maintainer's DB.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        # When deduping we must scan enough rows to find `limit` DISTINCT prompts,
        # so don't cap the SQL query; otherwise cap at `limit`.
        sql = f"SELECT prompt_redacted, response_redacted FROM traces ORDER BY {order}"
        if not dedupe_by_prompt:
            sql += " LIMIT ?"
            cur = conn.execute(sql, (limit,))
        else:
            cur = conn.execute(sql)
        raw = cur.fetchall()
    finally:
        conn.close()

    rows: list[dict] = []
    skipped = 0
    seen: set[str] = set()
    for r in raw:
        q = r["prompt_redacted"]
        a = r["response_redacted"]
        if not q or not str(q).strip() or not a or not str(a).strip():
            skipped += 1
            continue
        if dedupe_by_prompt:
            key = str(q).strip()
            if key in seen:
                continue
            seen.add(key)
        rows.append({"query": str(q), "response": str(a), "context": ""})
        if len(rows) >= limit:
            break
    return rows, skipped


# -- jsonl source ---------------------------------------------------------------

def from_jsonl(in_path: str) -> tuple[list[dict], int]:
    """Normalize arbitrary JSONL rows. Returns (rows, skipped)."""
    rows: list[dict] = []
    skipped = 0
    with open(in_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            q = _first_str(obj, _QUERY_KEYS)
            a = _first_str(obj, _RESPONSE_KEYS)
            if not q or not a:
                skipped += 1
                continue
            rows.append({
                "query": q,
                "response": a,
                "context": _first_str(obj, _CONTEXT_KEYS),
            })
    return rows, skipped


# -- mtbench source (optional) --------------------------------------------------

def from_mtbench(limit: int) -> tuple[list[dict], int] | None:
    """Pull a few (query, response) from a public dataset for practice labeling.

    Returns (rows, skipped), or None if `datasets` is not installed (caller then
    prints a message and exits 0). This path is intentionally simple and cannot be
    exercised in the stdlib-only sandbox.
    """
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        return None

    rows: list[dict] = []
    skipped = 0
    # HuggingFaceH4/mt_bench_prompts carries multi-turn prompts; we take the first
    # turn as a query and leave the response empty for the maintainer to fill/label
    # against a model of their choosing. If a reference answer field exists we use
    # it; otherwise response stays "" (still a usable practice row to label a real
    # model's output against later).
    ds = load_dataset("HuggingFaceH4/mt_bench_prompts", split="train")
    for ex in ds:
        if len(rows) >= limit:
            break
        prompts = ex.get("prompt") or []
        q = prompts[0] if isinstance(prompts, list) and prompts else ""
        refs = ex.get("reference") or []
        a = refs[0] if isinstance(refs, list) and refs else ""
        if not q:
            skipped += 1
            continue
        rows.append({"query": str(q), "response": str(a), "context": ""})
    return rows, skipped


# -- main -----------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True, choices=["sqlite", "jsonl", "mtbench"],
                   help="Where to pull (query, response) pairs from.")
    p.add_argument("--out", required=True, help="Output raw.jsonl path.")
    # sqlite
    p.add_argument("--db", help="[sqlite] Path to the Verdict SQLite DB.")
    p.add_argument("--sample", choices=["random", "recent"], default="recent",
                   help="[sqlite] recent = ORDER BY started_at DESC; random = RANDOM().")
    p.add_argument("--dedupe-by-prompt", action="store_true",
                   help="[sqlite] Keep only one (query, response) per distinct prompt "
                        "(covers diverse questions when the DB repeats prompts).")
    # jsonl
    p.add_argument("--in", dest="in_path", help="[jsonl] Input JSONL path.")
    # shared
    p.add_argument("--limit", type=int, default=50,
                   help="[sqlite/mtbench] Max rows to pull (default 50).")
    args = p.parse_args()

    if args.limit <= 0:
        print("--limit must be positive.")
        return 2

    if args.source == "sqlite":
        if not args.db:
            print("--source sqlite requires --db PATH.")
            return 2
        try:
            rows, skipped = from_sqlite(args.db, args.limit, args.sample,
                                        dedupe_by_prompt=args.dedupe_by_prompt)
        except sqlite3.OperationalError as e:
            print(f"Could not read SQLite DB {args.db!r}: {e}")
            return 1
    elif args.source == "jsonl":
        if not args.in_path:
            print("--source jsonl requires --in PATH.")
            return 2
        try:
            rows, skipped = from_jsonl(args.in_path)
        except OSError as e:
            print(f"Could not read {args.in_path!r}: {e}")
            return 1
    else:  # mtbench
        result = from_mtbench(args.limit)
        if result is None:
            print("The `datasets` library is not installed, so the mtbench source "
                  "is unavailable. Install it (`pip install datasets`) to pull "
                  "practice rows, or use --source sqlite / --source jsonl instead.")
            return 0
        rows, skipped = result

    if not rows:
        print(f"No usable rows found (skipped {skipped}). Nothing written to {args.out}.")
        return 1

    n = _write_rows(args.out, rows)
    print(f"Wrote {n} rows to {args.out} (skipped {skipped} bad/empty rows).")
    print("Next, build a labeling template:")
    print(f"  python scripts/verify_rubric_alignment.py --make-template {args.out} labels.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
