# Verdict Eval

PyPI distribution: `cognifity-verdict-eval`. Python import: `verdict_eval`.

Verdict's evaluation engine provides binary rubric judging, structural checks,
semantic drift analysis, sampling, and versioned intent clustering. Current
cohort comparisons live in the core Verdict Monitor workflow.

## Binary judge

`Judge` evaluates one response against a configurable PASS/FAIL rubric. PASS
rate is `PASS / (PASS + FAIL)`. `UNCLEAR`, missing dimensions, and judge errors
remain visible as coverage states and do not enter that denominator.

The evaluator identity includes the provider, model, rubric name/version,
behavior-relevant configuration, expected dimensions, and an effective
prompt/rubric fingerprint. Results from different identities are never pooled.

```python
from verdict_eval import DEFAULT_RUBRIC, Judge
from verdict_eval.providers import AnthropicAdapter

judge = Judge(
    provider=AnthropicAdapter(),
    model="claude-haiku-4-5-20251001",
    rubric=DEFAULT_RUBRIC,
)
result = judge.judge(query="...", response="...")
```

Dimensions marked `requires_context=True` can be skipped when a caller enables
`skip_context_dependent_when_missing`. If every dimension requires unavailable
context, evaluation fails before a provider call.

Every judge reply is decoded by `verdict_eval.judge_output`, which both the
Trace/Turn judge and conversation grading share. It finds the JSON inside a
Markdown fence or surrounding prose and accepts the per-dimension results as
an object keyed by dimension name, inside an outer `dimensions` object, or as
a list of objects that carry `name`. It rejects duplicate keys, duplicate
dimension names, non-finite numbers and replies over 64 KB. A complete but
malformed reply is normalized to `UNCLEAR` per dimension; a reply the provider
cut off at the output ceiling, or an oversized one, raises
`JudgeOutputUnusable` so callers record a retryable judge error. Conversation
grading stores a finding's `issue` label in identifier form (words joined with
underscores, lower-cased) and still requires each quoted finding to match the
exact message at the stated 0-based position. The decoder's
contract version is part of the evaluator identity (`evaluator_config`
`output_contract`, and the conversation prompt version's suffix), so a decoder
change never pools with earlier judgments.

`Judge.max_tokens` (default 1024) is the answer budget. For models that think
by default (Claude Fable, Mythos, Opus 5 and Sonnet 5 families; OpenAI `gpt-5`,
`gpt-6`, `o1`, `o3`, `o4`; Gemini 2.5 and later) each provider adapter sends
that budget plus `REASONING_TOKEN_ALLOWANCE` (16,384 tokens) as the output
ceiling, because such models spend output tokens on reasoning before the
answer; every one of those families accepts at least 64k output tokens. Any
other model is sent the budget as is, so a model with a small output limit is
never asked for more than it supports. The Anthropic adapter streams its
request; the OpenAI adapter sends `max_completion_tokens`, which reasoning
models require (an OpenAI-compatible server behind `OPENAI_BASE_URL` must honor
it; Ollama 0.34 ignores it and runs without a ceiling); the Google adapter
sends `max_output_tokens`. The allowance is a ceiling, not spend. OpenAI reasoning families
(`gpt-5*`, `o1*`, `o3*`, `o4*`) accept only their default temperature, so the
OpenAI adapter omits it for them and the evaluator identity records
`temperature_applied: false`; the Anthropic adapter sends a temperature only
when the installed SDK still accepts one (SDK 1.x does not).

Evaluator Lab also has an opt-in conversation unit for imported, redacted text
snapshots. Its uploaded JSON rubric explicitly selects `conversation` or
`response` and declares 1–12 binary or bounded numeric dimensions. A numeric
`passThreshold` derives PASS/FAIL from the score; without one the result is
UNCLEAR for binary aggregation. The evaluator stores a grade only for the
current exact transcript revision, target, rubric, provider, model, prompt
version, and endpoint. Partial coverage and judge errors stay visible.
Conversation grading supports Anthropic, OpenAI, and Google providers; Jev is
available for Trace and Turn evaluation only.
This does not execute a custom scoring formula or establish agreement with
human labels. See the repository's example rubric and onboarding guide.
This conversation unit is included in the synchronized `0.1.0b1` core and eval builds.

## Jev judge

Install `cognifity-verdict-eval[jev]` and set `TYPESAFE_API_KEY` in the process
running the judge. In Evaluator Lab, select Jev, preview the planned calls, and
approve the external transfer. The equivalent CLI choice is
`verdict-pipeline --judge-provider jev --judge-model jev-1.13.0`; its optional
judge-telemetry capture flag is unavailable for Jev. Other explicit versioned
Jev model IDs can be selected in Evaluator Lab or passed to the CLI; the API
must support the chosen version. Moving aliases such as `jev-latest` are
rejected because evaluator health must refer to one fixed model version.

For production judgments, Jev receives redacted request and response text as a
structured state and one Choice question per rubric dimension. It returns PASS,
FAIL, or UNCLEAR without explanatory reasoning. The requested model and exact
dimension set are checked before a result is stored. Evaluator fingerprints
separate results by judge, model version, endpoint URL, and prompt/rubric
version. Set `TYPESAFE_BASE_URL` only
when using a custom Jev endpoint; Evaluator Lab shows the effective URL before
approval and pins it for the call. Dashboard calibration sends label-set query
and response text plus the rubric without Verdict redaction. It ignores optional
context to match production judging; labels for context-required dimensions are
rejected, and the set fingerprint excludes ignored context. Inspect the file
before approving. Calibrate on held-out human labels for
the intended workload before relying on its quality scores. Monitor's cohort
comparison and Fisher test remain unchanged.

## Clustering and semantic analysis

The versioned registry requires an explicit `verdict-cluster fit --strategy`
choice. Exact-key `explicit` clustering is supported. Automatic `semantic`
clustering and the semantic fallback in `hybrid` remain experimental opt-ins.
Local semantic work uses the pinned
`sentence-transformers/all-MiniLM-L6-v2` model; the built-in hash embedder is a
lexical fallback and is labeled as such.

```python
import verdict

with verdict.intent_context("billing.v1"):
    response = provider.messages.create(...)
```

The registry lifecycle is `normalize`, `fit`, `assign`, `validate`, then
`activate`. Current analysis can then use the tenant's active immutable version:

```bash
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  normalize --limit 1000
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  fit --strategy explicit --target-workload agent
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  assign --version "$VERSION"
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  validate --version "$VERSION"
verdict-cluster --storage sqlite:///verdict.db --tenant tenant-a --actor ops \
  activate --version "$VERSION" --expected-generation 0
verdict-pipeline --storage sqlite:///verdict.db --registry-mode active \
  --tenant-id tenant-a
```

The pipeline prepares clusters and judgments. It does not publish a separate
fixed-window drift run. Monitor owns current reviewed cohort comparisons;
fixed-window rows written by older releases remain readable through the Python
storage API but are excluded from `/api/data` and the UI because they have no
tenant owner.

## Calibration

Calibrate judges on held-out examples from the workload where they will be
used. `scripts/verify_rubric_alignment.py` reports binary rubric agreement and
bootstrap confidence intervals. Optional sentinel runs persist only aggregate
health, evaluator identity, and label-set identity. Any sentinel execution
error prevents a healthy result.

```python
from verdict_eval import (
    DEFAULT_RUBRIC,
    Judge,
    SemanticDriftDetector,
    StructuralChecker,
)
```

See the [repository README](https://github.com/cognifityai/verdict#readme),
[ADR-002](https://github.com/cognifityai/verdict/blob/main/docs/adrs/002-judge-methodology.md),
and the [verification scripts](https://github.com/cognifityai/verdict/tree/main/scripts).

Apache 2.0.
