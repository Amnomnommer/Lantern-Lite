"""Source-aware evidence packages for Gate 6 — Internet retrieval gateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse


EVIDENCE_ROLES = frozenset(
    {
        "formal_classification",
        "descriptive_terminology",
        "direct_empirical_observation",
        "interpretation",
        "quotation",
        "institutional_position",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievedClaim:
    subject: str
    predicate: str
    object_text: str
    evidence_role: str
    source_url: str
    supporting_passage: str
    published_at: str
    retrieved_at: str
    confidence: float

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.subject,
                self.predicate,
                self.object_text,
                self.evidence_role,
                self.source_url,
                self.supporting_passage,
            )
        ):
            raise ValueError("retrieved claim requires content, role, and provenance")
        if self.evidence_role not in EVIDENCE_ROLES:
            allowed = ", ".join(sorted(EVIDENCE_ROLES))
            raise ValueError(f"unsupported evidence role; choose one of: {allowed}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("claim confidence must be from zero through one")


@dataclass(frozen=True, slots=True)
class EvidenceRoleGroup:
    evidence_role: str
    supported_values: tuple[str, ...]
    source_urls: tuple[str, ...]
    conflict: bool


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    subject: str
    predicate: str
    supported_values: tuple[str, ...]
    independent_sources: tuple[str, ...]
    evidence_roles: tuple[str, ...]
    role_groups: tuple[EvidenceRoleGroup, ...]
    conflict: bool
    cross_role_difference: bool
    comparison_justification: str | None
    comparison_justification_required: bool
    stale_sources: tuple[str, ...]
    importable: bool
    resolution: str


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if not host:
        raise ValueError("source URL must include a hostname")
    return host


def assess_claims(
    claims: tuple[RetrievedClaim, ...],
    *,
    now: datetime,
    freshness: timedelta,
    consequential: bool,
    comparison_justification: str | None = None,
) -> EvidenceAssessment:
    if not claims:
        raise ValueError("evidence assessment requires at least one claim")
    keys = {(claim.subject, claim.predicate) for claim in claims}
    if len(keys) != 1:
        raise ValueError("one assessment cannot mix claim subjects or predicates")

    justification = (
        comparison_justification.strip()
        if comparison_justification is not None
        else None
    )
    if comparison_justification is not None and not justification:
        raise ValueError("comparison justification cannot be blank")

    sources = tuple(sorted({_host(claim.source_url) for claim in claims}))
    values = tuple(sorted({claim.object_text for claim in claims}))
    roles = tuple(sorted({claim.evidence_role for claim in claims}))
    if len(roles) > 1 and justification is None:
        raise ValueError(
            "mixed evidence roles require an owner-visible "
            "comparison justification"
        )
    role_groups: list[EvidenceRoleGroup] = []
    for role in roles:
        role_claims = tuple(
            claim for claim in claims if claim.evidence_role == role
        )
        role_values = tuple(sorted({claim.object_text for claim in role_claims}))
        role_groups.append(
            EvidenceRoleGroup(
                evidence_role=role,
                supported_values=role_values,
                source_urls=tuple(
                    sorted({claim.source_url for claim in role_claims})
                ),
                conflict=len(role_values) > 1,
            )
        )

    stale: list[str] = []
    for claim in claims:
        published = datetime.fromisoformat(claim.published_at)
        if published.tzinfo is None or now.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone aware")
        if now - published > freshness:
            stale.append(claim.source_url)

    conflict = any(group.conflict for group in role_groups)
    comparison_justification_required = len(roles) > 1
    cross_role_difference = comparison_justification_required and len(values) > 1
    enough_sources = len(sources) >= (2 if consequential else 1)
    importable = (
        enough_sources
        and not conflict
        and not stale
        and not comparison_justification_required
    )

    if conflict:
        resolution = "unresolved_source_disagreement"
    elif comparison_justification_required:
        resolution = "evidence_roles_require_separate_review"
    elif stale:
        resolution = "recheck_for_freshness"
    elif not enough_sources:
        resolution = "insufficient_independent_sources"
    else:
        resolution = "supported_for_review"

    subject, predicate = next(iter(keys))
    return EvidenceAssessment(
        subject=subject,
        predicate=predicate,
        supported_values=values,
        independent_sources=sources,
        evidence_roles=roles,
        role_groups=tuple(role_groups),
        conflict=conflict,
        cross_role_difference=cross_role_difference,
        comparison_justification=justification,
        comparison_justification_required=comparison_justification_required,
        stale_sources=tuple(sorted(stale)),
        importable=importable,
        resolution=resolution,
    )
