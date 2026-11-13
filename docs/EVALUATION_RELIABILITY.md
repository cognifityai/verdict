# Bounded evaluation reliability

A completed judgment uses PASS when sufficient evidence meets the criterion,
FAIL when sufficient evidence demonstrates a violation, and UNCLEAR for
insufficient evidence or inapplicability. Missing capture alone is not an
application failure. A complete actual answer that omits an explicit requirement
can FAIL. Explicit length, style, and formatting requirements take precedence
over generic preferences about concise writing.

Malformed, contradictory, missing-dimension, oversized, truncated, and failed
provider replies are technical errors, separate from evidence uncertainty.
Finding IDs must be unique opaque values that remain unchanged by privacy
redaction; sensitive IDs are technical output errors rather than merged findings.
The shared output contract is `judge_output_v3`. Changed prompts and decoding
rules produce new evaluator identities. Existing stored results keep their old
identities and labels; an upgrade neither regrades them nor reconstructs missing
historical content. PASS-rate denominators remain PASS / (PASS + FAIL); UNCLEAR,
missing judgments, and execution errors remain separate coverage states.

## Capture coverage and suitability

Structural coverage describes whether the selected capture has its required
records and complete text. Evaluation suitability separately reports whether
substantive task, output, and source evidence is available. A structurally
complete capture can still contain a summary, counts, references, or a completion
summary in place of an actual answer.

Applications may explicitly declare `content_kind` as `substantive`, `summary`,
`counts`, `references`, `completion_summary`, or `unknown`:

```python
with run.turn(user_input="Compute 2 + 3.", content_kind="substantive") as turn:
    turn.record_context(name="calculation", value="2 + 3 = 5.",
                        content_kind="substantive")
    with turn.tool("calculator", arguments={"expression": "2 + 3"}) as tool:
        tool.set_output("5", content_kind="substantive")
    turn.set_output("2 + 3 = 5.", content_kind="substantive")
```

Normalized `AgentTurn` rows expose optional `request_content_kind` and
`response_content_kind`; context and tool-result event attributes may carry
`content_kind`. These are producer declarations, not content classifiers.
Legacy rows and unchanged producer calls default to `unknown`, remain eligible
where structurally complete, and disclose their limitations. Verdict does not
classify content by length or keywords. Explicitly non-substantive task/output
evidence withholds a full run/session/window judgment. A required source that
is known unavailable also withholds evaluation. Empty/blank source content and
explicit failed, timed-out, cancelled, or error results do not satisfy that
requirement; their status and error provenance remain disclosed. Existing structural coverage
is preserved. Identifiers must be safe to preserve across preview and saved
replay. Sensitive lookup selectors are rejected; sensitive identifiers discovered
inside a selection withhold evaluation before reference encoding. Use opaque
producer identifiers. Verdict does not create a reversible identifier mapping. Producer wiring must supply the actual final answer and relevant
source content; upgrading Verdict alone cannot recover that content.

## One-call historical audit

The existing session, run, and adjacent-turn evaluation panel offers an optional
**Historical audit**. This makes one bounded call after a preview and explicit
approval. There is no automatic per-turn fan-out, retry, or judge ensemble.
The same optional one-attempt scheduling and saved-result replay apply.

Each dimension must explicitly declare its aggregation and evidence authority.
Names do not select behavior:

| Aggregation | Meaning |
| --- | --- |
| `historical_no_errors` | Any supported material failure in the selected history remains FAIL even after recovery. Otherwise any UNCLEAR remains UNCLEAR. |
| `final_turn` | Assess final task completion; retain earlier assessments and findings separately. |
| `holistic` | Use a separate full-selection assessment, such as writing quality; retain per-turn assessments. |

`recorded_sources` requires recorded source evidence for historical factual
findings; `public_knowledge` also permits well-established public knowledge.
A user allegation alone does not establish factual truth. The judge must follow
the declared authority and use UNCLEAR when it cannot establish a claim.

Example scoped configuration:

```json
{
  "scope": "run",
  "runId": "example-run",
  "provider": "anthropic",
  "model": "claude-haiku-4-5",
  "auditMode": "historical",
  "maxOutputTokens": 4096,
  "rubric": {
    "name": "calculation_history",
    "version": "1",
    "dimensions": [{
      "name": "calculation_accuracy",
      "description": "Every supplied calculation must be correct.",
      "aggregation": "historical_no_errors",
      "evidenceAuthority": "recorded_sources"
    }]
  }
}
```

Submit to `/api/scoped/preview`; approve its exact `planFingerprint`, destination,
output allowance, and static price bound before `/api/scoped/run`, with
`confirmExternalEgress: true` and `maxEstimatedUsd`. The default audit output
allowance is 4096 tokens, configurable from 512 to 32768. Provider reasoning
allowances are included in the preview's maximum and price estimate. Rubric dimensions are allowlisted and recursively redacted before preview,
identity, provider messages, and durable replay; unknown extra fields are omitted.
Changing the allowance, authority, aggregation, prompt, evidence, or destination changes
the approval identity. Unsupported or oversized selections are withheld rather
than silently truncated; a provider reply cut off at its ceiling is an error.
Existing limits of 20 turns, 80 events, and 8000 evidence characters apply.
Audit replies must also fit the existing bounded durable result envelope.

The evidence preview presents ordered request/response pairs, source/session/run
qualified references, recorded timestamps, source provenance, and each turn's
`originalEvidenceReferences`. Timestamp ordering is not causal proof. Evidence
from another run is not assumed available to the original response. Uncertain
chronology remains explicit and cannot yield a clean aggregate PASS.

The structured reply has ordered `turns`, each with `original` and
`retrospective` dimension maps, plus specific `findings` and a `holistic` map.
Each assessment has `verdict`, `reason`, and exact reference/quote evidence.
Each finding identifies its affected turn, dimension, original or retrospective
basis, claim, support, and its own `recovery` (`recovered`, `unresolved`, or
`unclear`) quoting later recorded answers. User requests and source events alone cannot
establish response recovery. Every original factual PASS/FAIL under recorded-source
authority must cite an original-phase source; later sources support retrospective
assessment only. Missing turns, duplicate entries, contradictory
labels, missing findings for FAIL, invalid references/quotes, or a recovery
citing its own original answer are errors. Ambiguous recovery or an UNCLEAR
retrospective assessment prevents a clean aggregate PASS. Final completion can
PASS after a demonstrated recovery while the historical factual dimension
continues to FAIL.

Saved results expose aggregate `dimensions`, the structured `audit`, the
redacted `auditEvidence` snapshot, and evaluator identity. Original and
retrospective assessments remain distinct in the UI. Reading
`/api/scoped/result/{resultId}` calculates `stale` without changing the saved
assessment; old results without a selection identity have unknown freshness.
Changed evidence needs a new preview and approval. Tenant and evaluator
identities remain isolated.

## Limits of citation checks

Ordinary scoped checks validate reference membership. Historical audit checks
also validate exact copied quotes and original-versus-later reference scope.
Neither proves that the cited evidence supports the conclusion. Results and UI
explicitly label semantic support as an **unverified judge assessment**. A real
but irrelevant citation can survive mechanical checks. Review the stated
support, source authority, and finding-specific recovery before relying on a
judge result.

Synthetic tests with mocked providers verify transport, parsing, persistence,
API, and presentation contracts. They do not validate judge accuracy. Validate
judge semantics separately on independently labeled held-out examples, with
sample sizes, chance-corrected agreement, and confidence intervals.
