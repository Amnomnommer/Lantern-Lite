"""Inspectable and reversible cognitive-schema change proposals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


OPERATIONS = frozenset({"create", "specialize", "merge", "revise", "retire"})
PROTECTED_SCOPES = frozenset({"constitution", "permissions", "safety_governor", "code", "database_schema"})


@dataclass(frozen=True, slots=True)
class SchemaChangeProposal:
    operation: str
    schema_references: tuple[str, ...]
    before: tuple[tuple[str, object], ...]
    after: tuple[tuple[str, object], ...]
    evidence_references: tuple[str, ...]
    counterevidence_references: tuple[str, ...]
    reason: str
    review_status: str = "provisional"
    reversible: bool = True
    applied: bool = False


def propose_schema_change(
    *,
    operation: str,
    scope: str,
    schema_references: tuple[str, ...],
    before: Mapping[str, object],
    after: Mapping[str, object],
    evidence_references: tuple[str, ...],
    counterevidence_references: tuple[str, ...] = (),
    reason: str,
) -> SchemaChangeProposal:
    if operation not in OPERATIONS:
        raise ValueError("unsupported schema-learning operation")
    if scope in PROTECTED_SCOPES:
        raise PermissionError("learning cannot modify protected structure")
    if not schema_references or not evidence_references or not reason.strip():
        raise ValueError("schema proposal requires targets, evidence, and reason")
    if operation != "retire" and dict(before) == dict(after):
        raise ValueError("schema proposal must describe an actual change")
    return SchemaChangeProposal(
        operation,
        tuple(sorted(set(schema_references))),
        tuple(sorted(before.items())),
        tuple(sorted(after.items())),
        tuple(sorted(set(evidence_references))),
        tuple(sorted(set(counterevidence_references))),
        reason.strip(),
    )
