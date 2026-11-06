"""Validate an approved static list-price limit before optional judge egress."""

from __future__ import annotations

import math


def within_static_spend_limit(bound: object, limit: object) -> bool:
    """A planning bound is usable only with a finite, positive user limit."""
    return (
        type(bound) in (int, float) and math.isfinite(bound) and bound >= 0
        and type(limit) in (int, float) and math.isfinite(limit)
        and 0 < limit <= 100 and bound <= limit
    )
