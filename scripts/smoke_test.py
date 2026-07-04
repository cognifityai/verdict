"""Self-contained smoke test for the parts of Verdict that don't require
scipy/sklearn. Run from repo root:

    python scripts/smoke_test.py

For the full pytest suite (which exercises drift/clustering/Bradley-Terry too),
install the heavy deps: pip install -e packages/verdict packages/verdict_eval[dev]
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))

# ---- 1. redaction ----------------------------------------------------------
from verdict.redaction import redact

assert "<EMAIL>" in (redact("contact me at user@example.com") or "")
assert "<SSN>" in (redact("My SSN is 123-45-6789") or "")
assert "<CREDIT_CARD>" in (redact("Card 4111 1111 1111 1111") or "")  # Luhn-valid test card
assert "<PHONE>" in (redact("Call (415) 555-0199") or "")
hashed = redact("My email is foo@example.com", mode="hash", secret="secret")
assert "<EMAIL:" in (hashed or "") and "foo@example.com" not in (hashed or "")
print("✓ redaction")

# ---- 2. schema -------------------------------------------------------------
from verdict.schema import (
    DimensionScore,
    DriftDirection,
    DriftSignal,
    Judgment,
    Operation,
    Trace,
    Verdict,
)

t = Trace(provider="anthropic", operation=Operation.CHAT, request_model="claude")
assert t.trace_id
j = Judgment(
    trace_id=t.trace_id,
    judge_models=["fake"],
    dimensions=[
        DimensionScore(name="x", verdict=Verdict.PASS, judge_model="fake"),
        DimensionScore(name="y", verdict=Verdict.FAIL, judge_model="fake"),
    ],
)
assert j.pass_count == 1 and j.fail_count == 1 and j.pass_rate == 0.5
print("✓ schema")

# ---- 3. storage (memory + sqlite) -----------------------------------------
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage

for label, storage in [("memory", InMemoryStorage()), ("sqlite", SQLiteStorage(":memory:"))]:
    storage.insert_trace(Trace(trace_id="a", cluster_id="c1", tenant_id="t1"))
    storage.insert_trace(Trace(trace_id="b", cluster_id="c2", tenant_id="t1"))
    storage.insert_trace(Trace(trace_id="c", cluster_id="c1", tenant_id="t2"))
    assert storage.get_trace("a") is not None
    assert storage.get_trace("zzz") is None
    assert len(storage.list_traces(tenant_id="t1")) == 2
    assert len(storage.list_traces(tenant_id="t1", cluster_id="c1")) == 1
    j = Judgment(
        trace_id="a",
        judge_models=["x"],
        dimensions=[DimensionScore(name="g", verdict=Verdict.PASS, judge_model="x")],
    )
    storage.insert_judgment(j)
    assert len(storage.list_judgments_for_cluster("c1")) == 1
    sig = DriftSignal(
        cluster_id="c1",
        dimension="g",
        direction=DriftDirection.REGRESSION,
        p_value=0.01,
        p_value_adjusted=0.02,
        effect_size_cohens_d=-0.7,
        sample_size_current=100,
        sample_size_baseline=900,
    )
    storage.insert_drift_signal(sig)
    found = storage.list_drift_signals()
    assert len(found) == 1 and found[0].direction == DriftDirection.REGRESSION
    storage.close()
    print(f"✓ storage:{label}")

# ---- 4. trace decorator ----------------------------------------------------
from verdict.trace import current_span, span, trace as trace_decorator

with span("retrieve", k=10) as s:
    assert current_span() is s
    s.set_attribute("docs", 7)
assert s.attributes["docs"] == 7
assert s.duration_ms is not None and s.duration_ms >= 0

@trace_decorator("compute")
def add(a, b):
    return a + b

assert add(1, 2) == 3

import asyncio
@trace_decorator("acompute")
async def aadd(a, b):
    await asyncio.sleep(0)
    return a + b

assert asyncio.run(aadd(2, 3)) == 5
print("✓ trace decorator")

# ---- 5. injector -----------------------------------------------------------
from verdict_eval.injector import (
    CorruptionInjector,
    CorruptionKind,
    build_corruption_battery,
)

inj = CorruptionInjector(seed=1)
out = inj.corrupt("What is 2+2?", "Four.", CorruptionKind.REFUSAL)
assert "can't help" in out.corrupted_response.lower()

out = inj.corrupt("Q", "Original.", CorruptionKind.TRUNCATION, strength=1.0)
assert len(out.corrupted_response) < len("Original.")

out = inj.corrupt("Q", "Original.", CorruptionKind.PROMPT_INJECTION_ECHO)
assert "Ignore previous instructions" in out.corrupted_response

# Deterministic
a = CorruptionInjector(seed=99).corrupt("Q", "R", CorruptionKind.HALLUCINATION, 1.0)
b = CorruptionInjector(seed=99).corrupt("Q", "R", CorruptionKind.HALLUCINATION, 1.0)
assert a.corrupted_response == b.corrupted_response

battery = build_corruption_battery(
    [("What is 2+2?", "Four."), ("Capital of France?", "Paris.")],
    strengths=[1.0],
)
assert len(battery) == 2 * (1 + 7)
kinds = {s.kind for s in battery}
assert CorruptionKind.NONE in kinds
print(f"✓ injector ({len(battery)} samples across {len(kinds)} kinds)")

# ---- 6. judge (using FakeProvider) ----------------------------------------
from verdict_eval.judge import DEFAULT_RUBRIC, Judge, JudgeEnsemble
from verdict_eval.providers import FakeProvider

def fake_payload(**verdicts) -> str:
    return json.dumps({k: {"reasoning": "r", "verdict": v} for k, v in verdicts.items()})

payload = fake_payload(
    groundedness="PASS",
    relevance="PASS",
    completeness="FAIL",
    safety="PASS",
    instruction_following="PASS",
)
judge = Judge(provider=FakeProvider(payload), model="fake-judge")
j = judge.judge(query="Q", response="R", trace_id="t1")
by_name = {d.name: d.verdict for d in j.dimensions}
assert by_name["completeness"] == Verdict.FAIL
assert by_name["groundedness"] == Verdict.PASS
assert j.pass_count == 4 and j.fail_count == 1

# Code-fence tolerance
payload_fenced = "```json\n" + fake_payload(**{d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions}) + "\n```"
judge2 = Judge(provider=FakeProvider(payload_fenced), model="fake-judge")
j2 = judge2.judge(query="Q", response="R")
assert j2.pass_count == 5

# Missing dimension → UNCLEAR
judge3 = Judge(provider=FakeProvider('{"groundedness": {"reasoning": "r", "verdict": "PASS"}}'),
               model="fake-judge")
j3 = judge3.judge(query="Q", response="R")
assert any(d.verdict == Verdict.UNCLEAR for d in j3.dimensions)
print("✓ judge (single)")

# Ensemble majority vote
pass_p = fake_payload(**{d.name: "PASS" for d in DEFAULT_RUBRIC.dimensions})
fail_p = fake_payload(**{d.name: "FAIL" for d in DEFAULT_RUBRIC.dimensions})
ens = JudgeEnsemble([
    Judge(provider=FakeProvider(pass_p), model="A"),
    Judge(provider=FakeProvider(pass_p), model="B"),
    Judge(provider=FakeProvider(fail_p), model="C"),
])
j4 = ens.judge(query="Q", response="R")
assert all(d.verdict == Verdict.PASS for d in j4.dimensions)
print("✓ judge (ensemble majority)")

# ---- 7. client lifecycle (no actual instrumentor install since no anthropic SDK) ----
from verdict.client import get_client, init, shutdown

client = init(service_name="smoke", storage="memory://")
assert client is not None
assert get_client() is client
shutdown()
assert get_client() is None
print("✓ client init/shutdown")

print("\n🎉 ALL SMOKE TESTS PASS")
