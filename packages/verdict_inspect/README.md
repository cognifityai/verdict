# Verdict Inspect

PyPI distribution: `cognifity-verdict-inspect`. Command:
`verdict-inspect`.

One-shot drift analysis on a chat export. Drop in a `conversations.json` from
ChatGPT, a Claude.ai export, a supported agent-session JSONL file, or an
OpenAI-format messages dump, and get back a local drift / quality report.

## Why this exists

Continuous LLM observability (the SDK + monitoring path) is the right answer
for production agent traffic. But before a team commits to instrumenting
their stack, they want to know: **does Verdict actually find anything
interesting in my data?**

`verdict-inspect` runs against a file the user already has on their laptop.
Structural and embedding analysis stay local; the optional judge has a separate
privacy boundary described below.

## Usage

```bash
# Auto-detect format
verdict-inspect analyze ~/Downloads/conversations.json

# Force a format
verdict-inspect analyze --format chatgpt ~/Downloads/conversations.json

# Specify report output
verdict-inspect analyze --report ./drift_report.md ~/Downloads/chatlog.jsonl

# JSON output for piping
verdict-inspect analyze --json ~/Downloads/conversations.json | jq .
```

## Supported formats (v0)

- **ChatGPT data export** — the `conversations.json` from Settings → Data Controls → Export
- **Claude.ai data export** — the `conversations.json` from Settings → Account → Export
- **Generic OpenAI messages JSONL** — one JSON object per line, each with `messages: [{role, content}]`
- **Agent-session JSONL** — type-tagged local agent session logs
- **Auto-detect** — looks at file structure and picks a parser

Planned (v1): Cursor `.cursor/chats/`, Gemini Takeout, LangChain message
history files, Llama Index conversation logs.

## What you get back

For a file with enough substantive assistant turns:

1. **Semantic drift** — embedding-distribution shifts across temporal windows
2. **Judge sample** — PASS/FAIL by dimension on stride-sampled turns (requires `ANTHROPIC_API_KEY`)
3. **Structural metrics** — response length, hedge density, refusal rate, apology rate per window

Semantic drift runs key-free. By default it tries
`sentence-transformers/all-MiniLM-L6-v2`, then falls back to the built-in
`HashingEmbedder` if the dependency/model is unavailable. That fallback detects
lexical embedding-distribution changes; it is not a semantic model, and the
report labels it explicitly. Install the local semantic embedder with
`pip install "cognifity-verdict-eval[semantic]"`.

Turns with fewer than 10 assistant-response words are excluded from windowed
analysis. At least 16 substantive turns are required for a two-window
comparison; 24 create the default early/middle/late split. Each window's judge
sample is capped at 25 turns. Treat small-window output as exploratory rather
than calibrated production evidence.

## Privacy

Structural metrics and embedding inference run on your machine. The first
MiniLM run may download model weights, but it does not upload the analyzed
conversation. If `ANTHROPIC_API_KEY` is set and the judge is enabled,
`verdict-inspect` sends stride-sampled user and assistant text (up to 4,000
characters each) to the configured Anthropic model. Anthropic credentials and
data-handling terms apply. Pass `--no-judge` or omit the key to keep conversation
content local and receive structural plus embedding analysis only. The v0
inspect judge is Anthropic-only.
