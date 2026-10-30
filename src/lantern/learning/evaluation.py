"""Cross-context learning evaluation and shortcut detection."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True, slots=True)
class StrategyTrial:
    reference: str
    context: str
    baseline_score: float
    candidate_score: float
    safety_passed: bool
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    improved: bool
    retain_candidate: bool
    mean_improvement: float
    contexts: tuple[str, ...]
    issues: tuple[str, ...]
    evidence_references: tuple[str, ...]


def evaluate_strategy(
    trials: tuple[StrategyTrial, ...],
    *,
    minimum_contexts: int = 2,
    maximum_context_gap: float = 0.35,
) -> StrategyEvaluation:
    if not trials:
        raise ValueError("strategy evaluation requires trials")
    contexts = tuple(sorted({trial.context for trial in trials}))
    improvements = tuple(trial.candidate_score - trial.baseline_score for trial in trials)
    issues: list[str] = []
    if len(contexts) < minimum_contexts:
        issues.append("benchmark_overfit_risk")
    if not all(trial.safety_passed for trial in trials):
        issues.append("safety_regression")
    if max(improvements) - min(improvements) > maximum_context_gap:
        issues.append("brittle_context_gap")
    if any(trial.candidate_score > 1.0 or trial.candidate_score < 0.0 for trial in trials):
        issues.append("invalid_metric")
    average = mean(improvements)
    improved = average > 0.0
    retain = improved and not issues
    return StrategyEvaluation(
        improved,
        retain,
        round(average, 9),
        contexts,
        tuple(issues),
        tuple(sorted({trial.evidence_reference for trial in trials})),
    )
