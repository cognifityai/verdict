"""Validate an approved static list-price limit before optional judge egress."""

from __future__ import annotations


def within_static_spend_limit(bound: object, limit: object) -> bool:
    """A planning bound is usable only with a finite, positive user limit."""
    return (
        type(bound) in (int, float) and type(limit) in (int, float)
        and 0 <= bound <= limit <= 100 and limit > 0
    )
