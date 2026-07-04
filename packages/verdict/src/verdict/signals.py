"""User-signal capture — record implicit/explicit user feedback for a trace.

The eval-side correlator (``verdict_eval.correlator``) joins the judge's
PASS/FAIL verdicts with *user signals* (thumbs up/down, regenerate, abandon,
copy, accept, retry, follow-up, ...) to measure whether the judge actually
predicts what users care about. That join is only as good as the signals it
sees — so this is the write side an application calls to record one.

Design notes:

* An unknown ``kind`` is a **programming error** (a typo in the signal name
  silently corrupts the correlation, because the correlator buckets an
  unrecognized kind as "no usable label"). We raise ``ValueError`` so the
  mistake is caught at the call site, not months later in a skewed metric.

* Everything else is telemetry and must never break the caller: if Verdict was
  never initialized this is a no-op, and a storage failure is swallowed
  (logged, not raised). A dropped feedback event should never take down the
  request that produced it.

The valid kinds are kept in lockstep with the strings the correlator expects;
see ``verdict_eval.correlator.UserSignalKind``.
"""

from __future__ import annotations

import logging

from verdict.client import get_client
from verdict.schema import UserSignalRecord

log = logging.getLogger("verdict")

# Canonical set of valid user-signal kinds. These MUST match the strings the
# correlator recognizes (verdict_eval.correlator.UserSignalKind); a kind not in
# this set is bucketed by the correlator as "no usable label" and silently
# corrupts the agreement measurement, which is exactly why we reject it here.
VALID_SIGNAL_KINDS: frozenset[str] = frozenset(
    {
        "thumbs_up",
        "thumbs_down",
        "copy",
        "regenerate",
        "retry",
        "abandon",
        "accept",
        "follow_up_question",
        "no_signal",
    }
)


def record_user_signal(trace_id: str, kind: str) -> None:
    """Record a user-feedback signal against a captured trace.

    Args:
        trace_id: The trace the feedback is attributed to (from the LLM call
            the user reacted to).
        kind: One of :data:`VALID_SIGNAL_KINDS` — e.g. ``"thumbs_up"``,
            ``"regenerate"``, ``"abandon"``.

    Raises:
        ValueError: If ``kind`` is not a recognized signal kind. This is a
            programming error and is surfaced deliberately (a mis-typed kind
            would silently skew the judge/user correlation).

    Behavior:
        * If Verdict was never initialized (no global client), this is a no-op.
        * If the underlying storage write fails, the error is logged and
          swallowed — recording feedback must never break the caller.
    """
    # Validate FIRST, before touching the client. A bad kind is a bug in the
    # caller and must be surfaced whether or not Verdict is initialized —
    # otherwise the typo hides until someone happens to run with an active
    # client, which defeats the "catch it early" purpose.
    if kind not in VALID_SIGNAL_KINDS:
        raise ValueError(
            f"Unknown user-signal kind {kind!r}. "
            f"Valid kinds are: {sorted(VALID_SIGNAL_KINDS)}"
        )

    client = get_client()
    if client is None:
        # Verdict not initialized — telemetry no-op, do not crash the caller.
        return

    record = UserSignalRecord(trace_id=trace_id, kind=kind)

    # Best-effort persistence: a storage failure must never propagate to the
    # caller's request path. (We do NOT wrap the validation above in this —
    # that's a programming error and is meant to escape.)
    try:
        client.storage.insert_user_signal(record)
    except Exception:  # pragma: no cover — defensive; telemetry must not crash caller
        log.warning("Failed to record user signal (trace_id=%s kind=%s)", trace_id, kind, exc_info=True)
