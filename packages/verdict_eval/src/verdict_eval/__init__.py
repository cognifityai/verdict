"""Verdict eval engine.

Components are independently usable. Imports are LAZY so that
importing `verdict_eval` doesn't pull scipy/sklearn unless you reach for
the components that need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.1.0a17"

if TYPE_CHECKING:
    from verdict_eval.judge import DEFAULT_RUBRIC, Judge, Rubric
    from verdict_eval.providers import LLMProvider
    from verdict_eval.semantic_drift import SemanticDriftDetector, SemanticDriftSignal
    from verdict_eval.structural import (
        StructuralChecker,
        StructuralDriftSignal,
        StructuralSignal,
    )


def __getattr__(name: str):
    # Lazy module-level attribute resolution.
    if name in {"Judge", "Rubric", "DEFAULT_RUBRIC"}:
        from verdict_eval.judge import DEFAULT_RUBRIC, Judge, Rubric
        return {"Judge": Judge, "Rubric": Rubric, "DEFAULT_RUBRIC": DEFAULT_RUBRIC}[name]
    if name == "LLMProvider":
        from verdict_eval.providers import LLMProvider
        return LLMProvider
    if name in {"SemanticDriftDetector", "SemanticDriftSignal"}:
        from verdict_eval.semantic_drift import SemanticDriftDetector, SemanticDriftSignal
        return {"SemanticDriftDetector": SemanticDriftDetector,
                "SemanticDriftSignal": SemanticDriftSignal}[name]
    if name in {"StructuralChecker", "StructuralSignal", "StructuralDriftSignal"}:
        from verdict_eval.structural import (
            StructuralChecker,
            StructuralDriftSignal,
            StructuralSignal,
        )
        return {
            "StructuralChecker": StructuralChecker,
            "StructuralSignal": StructuralSignal,
            "StructuralDriftSignal": StructuralDriftSignal,
        }[name]
    raise AttributeError(name)


__all__ = [
    "DEFAULT_RUBRIC",
    "Judge",
    "LLMProvider",
    "Rubric",
    "SemanticDriftDetector",
    "SemanticDriftSignal",
    "StructuralChecker",
    "StructuralDriftSignal",
    "StructuralSignal",
    "__version__",
]
