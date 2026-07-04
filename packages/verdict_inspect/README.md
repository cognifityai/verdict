# verdict-inspect

One-shot drift analysis on a chat export. Drop in a `conversations.json` from
ChatGPT, a Claude.ai export, a Cursor `.jsonl`, or any OpenAI-format
messages dump, and get back a rigorous drift / quality report.

## Why this exists

Continuous LLM observability (the SDK + monitoring path) is the right answer
for production agent traffic. But before a team commits to instrumenting
their stack, they want to know: **does Verdict actually find anything
interesting in my data?**

`verdict-inspect` answers that in 60 seconds against a file the user already
has on their laptop. Local-first; nothing leaves the machine.

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

For any conversation longer than ~30 substantive turns:

1. **Semantic drift** — embedding-distribution shifts across temporal windows
2. **Judge sample** — PASS/FAIL by dimension on stride-sampled turns (requires `ANTHROPIC_API_KEY`)
3. **Structural metrics** — response length, hedge density, refusal rate, apology rate per window

Semantic drift runs key-free. By default it tries the optional
`sentence-transformers/all-MiniLM-L6-v2` embedder when installed, then falls
back to the built-in `HashingEmbedder` if the dependency/model is unavailable.
Install the heavier local embedder with
`pip install -e "packages/verdict_eval[semantic]"`.

For shorter conversations: just structural metrics (the others need ~30 turns/window for meaningful statistics).

## Privacy

Everything runs on your machine. No data is uploaded anywhere. The judge layer
makes API calls to Anthropic if you set `ANTHROPIC_API_KEY`, but you can run
without it to get drift + structural metrics only.
