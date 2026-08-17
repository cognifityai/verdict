"""Layer 3 — scheduled probe suites.

A **probe** is a deterministic prompt whose expected behavior we know in
advance. Run probes against your live model on a schedule (cron, GitHub
Action, whatever). If a probe that passed last week fails this week, that's
a hard drift signal: the model itself or your serving config changed in a
way that broke a known-good behavior.

Probes complement the other layers:
  - Layer 1 (semantic drift) tells you responses *changed*.
  - Layer 2 (structural) tells you they hedged/refused more.
  - Layer 3 (probes) tells you they got specific known-good behaviors wrong.
  - Layer 4 (judge ensemble) tells you about subjective quality.
  - Layer 5 (user signals) tells you whether users agreed.

A probe is independent of captured traffic — it's a synthetic regression
test that catches model degradation even when your traffic distribution is
quiet. The default suite ships with probes for:

  - Sycophancy (does the model cave when challenged?)
  - Prompt injection resistance
  - Basic factuality (numerical, temporal)
  - Format adherence (JSON output)
  - Refusal calibration (refuses dangerous prompts; doesn't over-refuse benign ones)

Reference probe-style methodologies:
  - METR / Apollo Research dangerous-capability evals
  - Anthropic's "Sleeper Agents" detection probes
  - OpenAI Evals (open-source) — pre-canned test suites
"""

from verdict_eval.probes.library import default_suite, load_suite_yaml
from verdict_eval.probes.runner import ProbeRunner
from verdict_eval.probes.schema import (
    Probe,
    ProbeExpectation,
    ProbeResult,
    ProbeRun,
    ProbeSuite,
)

__all__ = [
    "Probe",
    "ProbeExpectation",
    "ProbeResult",
    "ProbeRun",
    "ProbeRunner",
    "ProbeSuite",
    "default_suite",
    "load_suite_yaml",
]
