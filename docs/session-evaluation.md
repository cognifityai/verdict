# Conversation evaluation in the existing Verdict workflow

This is a local candidate rebased onto current mainline; these additions are not
in the published a21 wheel. Build this checkout to exercise them. No private data or credentials
are supplied in the image or examples.

## One data path

1. **Settings → Data Sources → Existing telemetry → Voice:** preview and import
   JSON/JSONL text transcripts. This extends the existing voice adapter; it does
   not transcribe audio. Give daily exports the same stable feed name. Source
   conversation IDs must be unique within that feed.
2. **Evaluate → Evaluator Lab:** select Conversation or contextual reply, or load
   an executable rubric JSON file. Inspect its name, version, target and criteria.
   Preview selects exact transcript revisions and shows the call budget.
3. Approve inference only to an operator-approved provider or compatible local
   endpoint. Preview and local review make no inference calls. See messages,
   scores and recurring issues in **Conversation evidence** on the same screen.
4. **Monitor → Compare History:** choose Conversation, a judge/rubric version and
   explicit dates or a count split. Start with all conversations or language and
   workflow populations. Inspect exclusions and measured changes. Activation
   freezes the reference and starts a fresh prospective bucket. **Monitor →
   Status** uses the same analysis-unit selector: choose Conversation to reload
   its saved monitor and run the next cohort. Trace and conversation policies
   have separate scopes. Logical sessions remain descriptive only.
5. **Explore → Compare:** saved matched replies require identical captured query
   and prior/system context, one exact evaluator, and at most one pair per input
   and known conversation. Coverage and 95% rate intervals are shown. Unmatched
   traffic remains descriptive. Captured redacted equality does not establish
   equality of hidden tools, retrieval or sampling settings.
   This does not implement interactive full-conversation model replay.

## Exact input contract

See [synthetic transcript](../examples/session-evaluation/conversation.jsonl).
Supply an opaque `conversation_id`, ordered `messages` (or `turns`), `role` or
`speaker`, and `content`/`text`. Supported roles include system, user and
assistant, with the existing Voice aliases. Complete assistant replies still
produce normal Verdict Trace records. The snapshot also retains trailing user messages and
interrupted replies. Duplicate exports do not create another conversation.

For time monitoring, provide a source timezone-aware `event_at` (normally the
conversation completion timestamp), plus a distinct conversation `end_status`:
`complete`, `premature`, `user_stop`, `technical_failure` or
`handoff`. `open` and `unknown` remain visible but excluded. Explicit
`ended_at`, `timestamp`, or `started_at` are accepted timestamp alternatives;
never substitute file modification time or import time. Choose and document one
consistent source-time definition. Message timestamps are optional for snapshot
review, but required to produce timed Trace records.

Optional metadata: `language`, `workflow`, `provider`, `model`, `prompt_version`.
UTF-8 and right-to-left text are retained and rendered with automatic text
direction. Transcription quality and multilingual judge validity require
separate human review.

Use non-sensitive machine identifiers for rubric names/versions, dimensions,
message IDs and native record keys. Validation rejects identifiers changed by
the existing best-effort redaction scanner instead of rewriting references.
Invalid imported native or message identifiers skip the snapshot visibly;
direct writes and rubric validation reject invalid identifiers. These checks
cannot identify every possible secret or personal identifier. Native feed
scopes use a `scope_` prefix around their generated hash; published Trace IDs
and provenance are unchanged. Previously stored native identifiers that fail
validation require an explicit operator correction, not automatic rewriting.

A changed snapshot creates an immutable revision. Grading attaches to that
revision; earlier results remain available for audit. An old revision cannot
become current merely because its file is replayed. A frozen Monitor stops and
requires a new bootstrap when a selected transcript changes or disappears.

An existing Voice Trace represents the original imported reply. If a corrected
export changes that reply, its prior context or provider/model identity, the
atomic import preserves the original Trace and reports
`voice_reply_revision_retained`. The corrected text belongs to the new native
snapshot and can be judged with a response rubric there. This avoids inventing
additional model calls. Contextual Trace judging and matched comparisons reject
Voice rows whose prior-message prompt or terminal assistant reply disagrees
with the stored prompt/response, or whose raw history is missing or malformed.
Coherent historical rows remain usable. Repairing incoherent historical rows
requires a separate explicit migration.

## Rubric JSON and judge result

See [conversation rubric](../examples/session-evaluation/rubric.json) and
[reply rubric](../examples/session-evaluation/reply-rubric.json).
Required fields: `name`, `version`, `target` (`response` or `conversation`),
`dimensions` (1–12 objects with `name`, `description`, optional `type`). Optional
`instructions` apply to this rubric. Binary dimensions retain PASS, FAIL and
UNCLEAR. Numeric dimensions require finite `min < max` and
`direction` (`higher_is_better` or `lower_is_better`). Numeric scores retain their
value without implicit pass/fail. An explicit `passThreshold` can define a
binary interpretation for review; native numeric Monitor still tests raw scores.

Response grading receives only the selected completed reply and its preceding
messages, never future messages. Full-conversation grading receives the ordered
snapshot, ending status and evidence warnings. The evaluator identity binds
rubric contents, target, provider/model, prompt/scorer versions, output budget
and configured endpoint fingerprint. Editing instructions changes the identity
even if the display version is unchanged. Historical judgments are never
rewritten or pooled across identities.

Generic judge output is:

```json
{"dimensions":{"repetition":{"verdict":"FAIL","reason":"Repeated an answered question.","findings":[{"issue":"repeated_question","message_id":"message-3","quote":"When did it start?","reason":"Already answered."}]},"summary":{"score":85,"reason":"Accurate synthetic summary.","findings":[]}}}
```

Every declared dimension must be returned. Numeric dimensions use `score`,
not a fabricated binary verdict. Quotes must occur in the referenced available
message. Malformed, nonfinite, duplicate-key and invented-evidence outputs become
judge errors, never successful findings. Imported grader findings use
`POST /api/evaluators/import` with setup authorization, `sessionId`, current
`revision`, executable `rubric`, attributable `provider`/`model`, and `findings`.
For a response rubric also supply `targetMessageId`. This endpoint imports a
judgment on already ingested evidence; it does not ingest conversations or call
a provider. Imported attribution is a claim by the operator, not verified
provider provenance.

Rubric files declare dimensions and optional numeric ranges. Verdict does not
interpret an external scoring formula or convert a Markdown/PDF rubric into an
executable one. Validate judge decisions against independently labeled examples
before using the scores for operational decisions.

## Monitoring definitions

One explicitly closed, timed, complete-evidence conversation is one observation;
a long conversation cannot inflate the sample denominator.

- A whole-conversation evaluator measures each selected binary dimension and
  each raw numeric dimension. Recorded `session.completed` is a source
  workflow fact, not a quality judgment.
- A binary response rubric can be selected explicitly with
  `responseAggregation=all_completed_replies_v1`: **any completed reply fails,
  among fully evaluated conversations**. Each dimension aggregates all completed
  assistant targets at the exact revision. Precedence is missing → error →
  unclear → any FAIL → all PASS. Zero targets are missing. Unknown, interrupted
  and unassessed replies never become PASS. Known failing replies in partial
  conversations remain visible in Evaluate and the partial-failure coverage.
  Numeric/mixed response monitoring is unsupported and rejected.
- Missing, error and unclear evidence are separate from PASS/FAIL denominators.
  Numeric evaluable means a valid raw score, not a binary PASS. No zero
  imputation. Selected late grading finalizes a pending cohort once; it does not
  add another conversation or modify frozen completed evidence.
- Binary tests use Fisher exact. Numeric tests use
  `mann_whitney_asymptotic_v1`, with tie and continuity correction, at least
  `max(policy minimum,30)` scored conversations per group and cohort. Constant
  identical scores give p=1/effect=0. Signed current rank-biserial effect controls
  numeric movement and effect thresholds; medians are descriptive. This test
  does not establish a median shift.
- One Benjamini–Hochberg adjustment covers the combined eligible Boolean and
  numeric cells. Prospective looks use the existing quadratic alpha spending.
  The Benjamini–Hochberg method assumes independent or suitable positive dependence; arbitrary rubric
  dependence is not established. These controls are not a universal 5%
  familywise false-alert guarantee. Conversations from the same user or
  shared workflow may be dependent; resolve sampling before alerts.
- Historical comparisons and small-sample measured rates/medians are exploratory.
  If selected grading is pending, the preview still shows observed rates,
  intervals, sample sizes and missing/error/unclear coverage. It saves no
  candidate, has no p-values or activation path, and names the required repair.
  Only a preselected prospective policy can support an authoritative software
  signal. Statistical significance does not establish operational importance.

Population grouping uses captured language/workflow labels; model grouping uses
captured provider/model. Trace clustering remains
available for existing traffic, but conversation clustering is not implemented.
A changed workload mix can invalidate a reference; inspect group coverage before
attributing a change to the model.

## Daily local operation

Import through the existing CLI (use the same stable feed):

```bash
verdict-import file /imports/daily.jsonl --format voice \
  --storage "$VERDICT_STORAGE_URL" --tenant-id local --source-scope conversation-feed
verdict-session-evaluate --tenant local --rubric-file /rubrics/quality.json \
  --provider openai --model your-approved-model --max-calls 20
```

The evaluator previews by default. Add `--execute --approve-inference` only for
an approved endpoint; add `--monitor` to advance an already active conversation
Monitor. `VERDICT_STORAGE_URL` is read from the environment. Alternatively run
`verdict-monitor run --storage "$VERDICT_STORAGE_URL" --tenant local --unit conversation`.
A host scheduler can execute import → evaluation → Monitor daily. The one-shot
evaluator holds a host lock and skips completed exact-identity results. Designate
one inference worker per deployment; the host lock is not a distributed lock.
Use sequential jobs on the same host/container. Durable Monitor successor writes
use existing compare-and-swap conflict handling. Duplicate ingestion and repeated
cycles do not create new observations.

The legacy `verdict-pipeline` retains its old default evaluator behavior; opt into
`--conversation-history` to include prior messages with a distinct identity.
Evaluator Lab Trace preview explicitly uses prior-message evidence.

Core SDK imports remain independent of eval/dashboard provider dependencies.
SQLite, memory, buffered storage and Postgres implement the same session contract.
This candidate bounds snapshots to 1,000 messages/512 KB, rubric files to 512 KB,
review/planning to 5,000 conversations or stored assessment attempts per scan, numeric snapshots
to 60,000 values and serialized Monitor snapshots to 4 MiB. Oversize inputs fail
visibly; they are not silently used for complete-conversation claims. Plan retention
and workload capacity before a larger deployment. Existing trace retention does
not delete new session history; session retention/deletion is currently an
operator database procedure. Redaction is best effort, not de-identification.
