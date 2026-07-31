"""Verdict — the five-line install.

Run:
    pip install -e "packages/verdict[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/basic_anthropic.py

After running, open verdict.db in any SQLite viewer to see the captured trace.
"""

import verdict
from anthropic import Anthropic
from verdict.client import get_client

# 1. Initialize Verdict once at startup.
verdict.init(
    service_name="example-customer-support",
    storage="sqlite:///./verdict.db",
    # capture_content=True,    # off by default; enable to log prompts/completions
)

# 2. Use Anthropic normally. Verdict captures everything transparently.
client = Anthropic()

resp = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=512,
    messages=[
        {"role": "user", "content": "In one sentence: what is observability?"},
    ],
)
print(resp.content[0].text)

# 3. (Optional) check what was captured.
storage = get_client().storage
traces = storage.list_traces(limit=5)
print(f"\nVerdict captured {len(traces)} trace(s).")
for t in traces:
    print(
        f"  - {t.provider} {t.request_model}: "
        f"in={t.input_tokens} out={t.output_tokens} "
        f"finish={t.finish_reason} latency={t.latency_ms:.0f}ms"
    )
