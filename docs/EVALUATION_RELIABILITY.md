# Scoped evaluation evidence and historical audit

Scoped evaluation previews one selected session, run, adjacent-turn window, or
tool action before a separately approved external judge call. The default
`original_response` mode returns one assessment per dimension for the selection.
The optional `historical_audit` mode returns separate original and retrospective
assessments for each selected turn, with findings and recovery evidence.

## Judgment states

| State | Meaning |
|---|---|
| PASS | Available evidence supports the declared criterion. |
| FAIL | Available evidence demonstrates a violation. A complete actual answer that omits a requested requirement may fail. |
| UNCLEAR | Evidence is insufficient or the criterion is inapplicable. |
| error | Provider execution or structured reply validation failed. |
| not run | Evidence, budget, selection bounds, or approval did not permit a call. |

Missing capture alone is not an application failure. Explicit rubric and user
requirements govern length, style, and format. The shared judge decoder rejects
missing or extra dimensions, duplicate keys, contradictory reason aliases,
multiple answers, invalid verdicts, and truncated or oversized replies as errors.
Old saved results retain their original evaluator identities and semantics.

PASS rate remains `PASS / (PASS + FAIL)` using the shared metric helpers.
UNCLEAR, missing, error, and not-run are separate coverage states, outside that
denominator. An audit's deterministic dimension verdict is a criterion-specific
reduction, not a new pass-rate formula or a measure of judge accuracy.

## Capture coverage and suitability

Structural `coverage` describes the existing capture bounds and completeness.
`suitability` separately discloses request, response, and source content roles,
missing required evidence, producer provenance, and limitations. Structurally
complete rows may contain summaries or references instead of actual answers.

An application can declare roles through the existing SDK context API:

```python
with run.turn(user_input="Describe the supplied shape") as turn:
    turn.record_context(
        name="evaluation_capture",
        value={"request": "substantive", "response": "substantive",
               "sources": "reference"},
    )
    turn.set_output("The shape is a hexagon.")
```

The fields are `request`, `response`, and `sources`. Their allowed roles are
`substantive`, `summary`, `count`, `reference`, `missing`, and `unknown`.
Declarations describe what was captured; they do not certify its truth.
Conflicting or invalid declarations are disclosed as conflicting. Known missing,
truncated, summary-only, count-only, or reference-only required content withholds
evaluation. Undeclared legacy roles stay unknown; Verdict does not infer roles
from text length or words and does not disable all legacy evaluations.

Source bodies must be captured as context or tool-result events; a declaration
alone cannot supply them. A source cannot satisfy a required source when its body
is empty or whitespace only (including containers of blank values), when it is
marked `available=False`, when its event status is failed, timed out, or
cancelled, or when it is an error-flagged tool result. Its status stays visible in
the evidence. Suitability and the exact missing field/role remain
visible in blocked run/session views and previews. An integration that currently records only a completion
summary, title, count, or source locator needs separate producer wiring to record
the actual answer and available source bodies, under its existing privacy and
size limits. An upgrade cannot reconstruct missing historical content.

## Historical audit contract

In a scoped evaluation panel, enable **Historical audit**, choose aggregation and
evidence authority, and review the exact preview and output allowance before
approving one call. Each audit rubric dimension explicitly declares:

```json
{
  "name": "geometry_check",
  "description": "No material false geometric claims.",
  "aggregation": "historical_all_turns",
  "evidenceAuthority": "public_knowledge",
  "requiredEvidence": ["request", "response"]
}
```

| Policy | Meaning |
|---|---|
| `historical_all_turns` | Any retrospective turn failure remains FAIL, including after recovery. Otherwise any UNCLEAR prevents PASS. |
| `final_response` | Use the final selected response's retrospective verdict; earlier assessments and findings remain visible. |
| `holistic` | Request a separate judgment of the selection as a whole. |
| `recorded_sources` | Factual conclusions require support from recorded sources. A user allegation alone does not establish truth. |
| `public_knowledge` | Recorded sources and well-established public facts may establish factual support. |

`requiredEvidence` always includes `request` and `response`; add `sources` when
actual source bodies are required. A dimension using `requiresContext` must
explicitly require sources in audit mode. Policies are never inferred from names.

The first bounded audit supports session, run, and adjacent-turn selections with
at most four turns, four turn-by-dimension entries, and one holistic dimension.
It requires 2,048–4,096 requested output tokens; the UI requests 4,096. Existing
scope limits still apply, and the structured audit user payload is bounded to
8,000 UTF-8 bytes. Oversized selections are withheld, never silently truncated.
The preview prices the actual audit prompt and includes any provider reasoning
allowance in the maximum output ceiling. Mode, prompt/parser fingerprints,
policies, provider/model configuration, output allowance, destination, and evidence
bind the evaluator and approval. There is one provider attempt, no per-turn
fan-out and no automatic retry. Scheduling preserves the same exact approval.
OpenAI audit judges are asked for JSON mode, which makes a reply that is not
cut off one syntactically valid JSON object; it does not check fields, so the
reply is still decoded and validated against the contract. Anthropic requests
are unchanged.

Ordered pairs carry available timestamps and short positional references: `T1`
is the first selected turn in order, `Q1` its request, `R1` its response, and
`E1` the first selected event. References carry no stored source, session, run,
turn, or event identifier, and the judge request omits those identifiers. Pair
text is passed through the current redaction rules before it is fingerprinted,
previewed, or sent, including text stored by an earlier release. Source turn sequence owns order within
a run; timestamps cannot reverse it. Overlapping runs or disagreement between
source order and timestamps remain uncertain. Original assessments can cite only
evidence recorded as available at the original response; retrospective assessments
may use later recorded evidence. The original, later, and uncertain-availability
reference lists partition all selected evidence, including a source recorded after
its own response. Timestamps are source-reported, not causal proof. Unknown
availability remains uncertain. Findings belong to a specific response and
dimension; Verdict numbers them `F1`, `F2`, … in reply order, and a judge-supplied
finding ID is a contract error.
Each finding has its own recovered, unresolved, or unclear state; recovered
requires a cited, unambiguously later response. A supported original FAIL cannot
be changed into a retrospective PASS. Missing entries or contradictory structured
results are errors. Partial evidence, ambiguous chronology, or ambiguous recovery
cannot produce a clean aggregate PASS. The reply may be wrapped in a Markdown
fence or a sentence, but must contain exactly one JSON object with a `turns` key;
a second answer or malformed JSON is an error. A JSON object that does not
decode makes the whole reply an error, for both the audit and the trace judge,
rather than letting a valid object inside or after it stand as the answer.

When ordinary evaluation of a run or session is unavailable only because its
event capture is partial or its event order is uncertain, and its turn text is
complete, the panel still opens in historical-audit mode, because the audit
discloses those limitations. Preview applies the audit's own suitability, shape,
and size checks.

Results persist `turnAssessments`, `historicalFindings`, `holisticAssessments`,
ordered `turnPairs`, criterion policies, identities, and output allowance. Saved
criteria and policies use typed lists; response maps remain available to API
consumers. Ignored caller metadata is not persisted. Static result metadata is
checked before egress. A provider reply exceeding durable result limits becomes
a saved technical error and is not retried automatically. Saved
results are immutable. The result API reports `evidenceStatus` separately as
current, stale, or unknown; older records without selection metadata are unknown,
and so is a result whose selected run or session ID the redactor alters (for
example, an ID containing an IP address), because the redacted ID no longer
selects the evidence.
A matching preview reopens the saved result without another call. Changed
evidence requires a new preview and preserves prior assessments.

Citation validation checks selected-reference membership, phase availability,
and exact quote presence for audits. It does **not** verify entailment or factual
truth. A real quote can fail to support a conclusion. Synthetic mocked-provider
tests exercise the workflow and validation logic; they do not semantically
validate the judge. Calibrate each evaluator against independently labeled,
held-out examples before relying on its factual or writing judgments.
