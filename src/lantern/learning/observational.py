"""Reviewed, reversible communication-schema proposals for Gate 7."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


SUPPORTED_BEHAVIORS = frozenset({"milestone_recommendation"})


@dataclass(frozen=True, slots=True)
class ConversationObservation:
    owner_event_reference: str
    reply_event_reference: str
    context_reference: str
    dialogue_act: str
    grounded_references: tuple[str, ...]
    owner_supplied: bool
    requires_owner_review: bool

    def __post_init__(self) -> None:
        if not self.owner_event_reference.strip() or not self.reply_event_reference.strip():
            raise ValueError("conversation observations require linked event references")
        if not self.context_reference.strip() or not self.dialogue_act.strip():
            raise ValueError("conversation observations require context and behavior")
        if not self.owner_supplied or not self.requires_owner_review:
            raise ValueError("conversation evidence must preserve owner and review boundaries")
        if not self.grounded_references:
            raise ValueError("conversation evidence must identify its grounding")


@dataclass(frozen=True, slots=True)
class CommunicationSchemaProposal:
    proposal_digest: str
    behavior: str
    operation: str
    scope: str
    schema_reference: str
    before: tuple[tuple[str, object], ...]
    after: tuple[tuple[str, object], ...]
    evidence_references: tuple[str, ...]
    counterevidence_references: tuple[str, ...]
    observation_count: int
    context_count: int
    confidence: float
    review_status: str
    improvement_claimed: bool
    issues: tuple[str, ...]
    measurement_plan: tuple[str, ...]
    rollback_conditions: tuple[str, ...]
    reversible: bool = True
    applied: bool = False
    authoritative_write: bool = False
    external_effect: bool = False
    authority_granted: bool = False


def _canonical_digest(payload: dict[str, object]) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(rendered.encode("utf-8")).hexdigest()


def propose_communication_schema(
    *,
    behavior: str,
    observations: tuple[ConversationObservation, ...],
    counterevidence_references: tuple[str, ...] = (),
) -> CommunicationSchemaProposal:
    """Derive a review-only proposal without changing conversation behavior."""
    if behavior not in SUPPORTED_BEHAVIORS:
        raise ValueError("unsupported communication behavior")
    if not observations:
        raise ValueError("communication learning requires selected evidence")
    if len(observations) > 10:
        raise ValueError("communication learning accepts at most ten observations")
    if any(item.dialogue_act != behavior for item in observations):
        raise ValueError("selected replies must match the predeclared behavior")

    evidence_references = tuple(sorted({
        reference
        for item in observations
        for reference in (
            item.owner_event_reference,
            item.reply_event_reference,
            *item.grounded_references,
        )
    }))
    counterevidence = tuple(sorted(set(counterevidence_references)))
    contexts = tuple(sorted({item.context_reference for item in observations}))
    issues: list[str] = []
    if len(observations) < 2:
        issues.append("single_observation_not_improvement")
    if len(contexts) < 2:
        issues.append("cross_context_evaluation_pending")
    if not counterevidence:
        issues.append("counterevidence_not_selected")

    before = {
        "response_policy": "current_project_aware_milestone_reply",
        "generalization_status": "unmeasured",
    }
    after = {
        "response_policy": "cite_objective_and_constraints_compare_bounded_options",
        "missing_grounding_action": "ask",
        "owner_review_required": True,
        "generalization_status": "candidate",
    }
    observation_count = len(observations)
    confidence = round(min(0.79, 0.4 + (0.1 * observation_count)), 2)
    review_status = "provisional" if observation_count >= 2 else "candidate"
    payload = {
        "behavior": behavior,
        "operation": "revise",
        "scope": "communication_behavior",
        "schema_reference": f"communication:{behavior}",
        "before": before,
        "after": after,
        "evidence_references": evidence_references,
        "counterevidence_references": counterevidence,
        "observation_count": observation_count,
        "context_count": len(contexts),
        "confidence": confidence,
        "review_status": review_status,
        "issues": tuple(issues),
    }
    return CommunicationSchemaProposal(
        proposal_digest=_canonical_digest(payload),
        behavior=behavior,
        operation="revise",
        scope="communication_behavior",
        schema_reference=f"communication:{behavior}",
        before=tuple(sorted(before.items())),
        after=tuple(sorted(after.items())),
        evidence_references=evidence_references,
        counterevidence_references=counterevidence,
        observation_count=observation_count,
        context_count=len(contexts),
        confidence=confidence,
        review_status=review_status,
        improvement_claimed=False,
        issues=tuple(issues),
        measurement_plan=(
            "collect owner-reviewed outcomes in at least two distinct project contexts",
            "compare grounded usefulness against the unchanged current behavior",
            "reject retention after any safety regression or brittle context gap",
        ),
        rollback_conditions=(
            "owner rejects the proposal",
            "a later observation contradicts the proposed response policy",
            "cross-context evaluation fails",
            "any safety or authority boundary regresses",
        ),
    )


def proposal_payload(proposal: CommunicationSchemaProposal) -> dict[str, object]:
    """Return a JSON-safe, inspectable event payload."""
    return asdict(proposal)
