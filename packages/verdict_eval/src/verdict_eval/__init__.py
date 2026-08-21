"""Verdict eval engine.

Five components, each independently usable. Imports are LAZY so that
importing `verdict_eval` doesn't pull scipy/sklearn unless you reach for
the components that need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.1.0a7"

if TYPE_CHECKING:
    from verdict_eval.compare import BradleyTerryComparator, PairwiseResult
    from verdict_eval.drift import DriftDetector, DriftWindow
    from verdict_eval.injector import CorruptionInjector, CorruptionKind
    from verdict_eval.judge import DEFAULT_RUBRIC, Judge, Rubric
    from verdict_eval.pairwise import PairwiseJudge, PairwiseJudgeEnsemble, PairwiseStatus
    from verdict_eval.providers import LLMProvider


def __getattr__(name: str):
    # Lazy module-level attribute resolution.
    if name in {"Judge", "Rubric", "DEFAULT_RUBRIC"}:
        from verdict_eval.judge import DEFAULT_RUBRIC, Judge, Rubric
        return {"Judge": Judge, "Rubric": Rubric, "DEFAULT_RUBRIC": DEFAULT_RUBRIC}[name]
    if name == "LLMProvider":
        from verdict_eval.providers import LLMProvider
        return LLMProvider
    if name in {"DriftDetector", "DriftWindow"}:
        from verdict_eval.drift import DriftDetector, DriftWindow
        return {"DriftDetector": DriftDetector, "DriftWindow": DriftWindow}[name]
    if name in {"SemanticDriftDetector", "SemanticDriftSignal"}:
        from verdict_eval.semantic_drift import SemanticDriftDetector, SemanticDriftSignal
        return {"SemanticDriftDetector": SemanticDriftDetector,
                "SemanticDriftSignal": SemanticDriftSignal}[name]
    if name in {"BradleyTerryComparator", "PairwiseResult"}:
        from verdict_eval.compare import BradleyTerryComparator, PairwiseResult
        return {"BradleyTerryComparator": BradleyTerryComparator, "PairwiseResult": PairwiseResult}[name]
    if name in {"PairwiseJudge", "PairwiseJudgeEnsemble", "PairwiseStatus"}:
        from verdict_eval.pairwise import PairwiseJudge, PairwiseJudgeEnsemble, PairwiseStatus
        return {
            "PairwiseJudge": PairwiseJudge,
            "PairwiseJudgeEnsemble": PairwiseJudgeEnsemble,
            "PairwiseStatus": PairwiseStatus,
        }[name]
    if name in {"CorruptionInjector", "CorruptionKind"}:
        from verdict_eval.injector import CorruptionInjector, CorruptionKind
        return {"CorruptionInjector": CorruptionInjector, "CorruptionKind": CorruptionKind}[name]
    if name == "FaithfulnessGain":
        from verdict_eval.information_gain import FaithfulnessGain
        return FaithfulnessGain
    if name == "LangfuseSource":
        from verdict_eval.langfuse_source import LangfuseSource
        return LangfuseSource
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
    if name in {"ProbeRunner", "ProbeSuite", "Probe", "ProbeExpectation",
                "ProbeResult", "ProbeRun", "default_suite", "load_suite_yaml"}:
        from verdict_eval.probes import (
            Probe,
            ProbeExpectation,
            ProbeResult,
            ProbeRun,
            ProbeRunner,
            ProbeSuite,
            default_suite,
            load_suite_yaml,
        )
        return {
            "ProbeRunner": ProbeRunner, "ProbeSuite": ProbeSuite,
            "Probe": Probe, "ProbeExpectation": ProbeExpectation,
            "ProbeResult": ProbeResult, "ProbeRun": ProbeRun,
            "default_suite": default_suite, "load_suite_yaml": load_suite_yaml,
        }[name]
    if name in {"UserSignalCorrelator", "CorrelationPair", "CorrelationReport"}:
        from verdict_eval.correlator import (
            CorrelationPair,
            CorrelationReport,
            UserSignalCorrelator,
        )
        return {
            "UserSignalCorrelator": UserSignalCorrelator,
            "CorrelationPair": CorrelationPair,
            "CorrelationReport": CorrelationReport,
        }[name]
    raise AttributeError(name)


__all__ = [
    "DEFAULT_RUBRIC",
    "BradleyTerryComparator",
    "CorrelationPair",
    "CorrelationReport",
    "CorruptionInjector",
    "CorruptionKind",
    "DriftDetector",
    "DriftWindow",
    "Judge",
    "LLMProvider",
    "PairwiseJudge",
    "PairwiseJudgeEnsemble",
    "PairwiseResult",
    "PairwiseStatus",
    "Probe",
    "ProbeExpectation",
    "ProbeResult",
    "ProbeRun",
    "ProbeRunner",
    "ProbeSuite",
    "Rubric",
    "StructuralChecker",
    "StructuralDriftSignal",
    "StructuralSignal",
    "UserSignalCorrelator",
    "__version__",
    "default_suite",
    "load_suite_yaml",
]
