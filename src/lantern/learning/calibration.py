"""Bounded outcome-based confidence adjustment for Gate 7."""

from __future__ import annotations

from dataclasses import dataclass


MAX_EMPIRICAL_CONFIDENCE = 0.999999999


@dataclass(frozen=True, slots=True)
class OutcomeEvidence:
    reference: str
    context: str
    predicted_probability: float
    occurred: bool
    evidence_reference: str

    def __post_init__(self) -> None:
        if not self.reference.strip() or not self.context.strip() or not self.evidence_reference.strip():
            raise ValueError("outcome evidence requires identity, context, and provenance")
        if not 0.0 <= self.predicted_probability < 1.0:
            raise ValueError("empirical predicted probability must be below one")


@dataclass(frozen=True, slots=True)
class ConfidenceUpdate:
    previous: float
    proposed: float
    delta: float
    evidence_references: tuple[str, ...]
    contexts: tuple[str, ...]
    reversible: bool = True
    applied: bool = False


def adjust_confidence(
    previous: float,
    outcomes: tuple[OutcomeEvidence, ...],
    *,
    learning_rate: float = 0.2,
    maximum_delta: float = 0.15,
) -> ConfidenceUpdate:
    """Propose a calibrated update; never mutate the underlying belief."""
    if not 0.0 <= previous < 1.0:
        raise ValueError("empirical confidence must be below one")
    if not outcomes:
        raise ValueError("confidence adjustment requires outcome evidence")
    if not 0.0 < learning_rate <= 1.0 or not 0.0 < maximum_delta <= 1.0:
        raise ValueError("learning controls must be within zero and one")
    errors = tuple((1.0 if item.occurred else 0.0) - item.predicted_probability for item in outcomes)
    raw_delta = learning_rate * sum(errors) / len(errors)
    delta = max(-maximum_delta, min(maximum_delta, raw_delta))
    proposed = max(0.0, min(MAX_EMPIRICAL_CONFIDENCE, previous + delta))
    return ConfidenceUpdate(
        previous,
        round(proposed, 9),
        round(proposed - previous, 9),
        tuple(sorted({item.evidence_reference for item in outcomes})),
        tuple(sorted({item.context for item in outcomes})),
    )
