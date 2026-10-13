"""Wholly synthetic, deterministic task function for the local experiment demo."""

from __future__ import annotations


def run(task_input: str, context: str | None, configuration: dict) -> str:
    """Illustrate a fixed on-call route and a collateral regression."""
    _ = context
    version = configuration.get("version")
    if task_input == "Synthetic incident: checkout unavailable for all users; severity P1":
        return "create_backlog_ticket" if version == "baseline-v1" else "page_on_call"
    if task_input == "Synthetic request: correct a dashboard label; severity P3":
        return "create_backlog_ticket" if version == "baseline-v1" else "page_on_call"
    return "unsupported synthetic task"
