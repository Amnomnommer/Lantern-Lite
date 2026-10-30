"""Deterministic synthetic deliberation over Lantern hive events."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from lantern.hive.events import EventJournal, HiveEvent, HiveValidationError


@dataclass(frozen=True, slots=True)
class Interpretation:
    event_id: str
    node_id: str
    subject: str
    predicate: str
    object_text: str
    evidence_digest: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    subject: str
    predicate: str
    state: str
    positions: tuple[str, ...]
    event_ids: tuple[str, ...]
    evidence_digests: tuple[str, ...]


def interpretation_from_event(event: HiveEvent) -> Interpretation:
    if event.event_type != "interpretation.proposed":
        raise HiveValidationError("event is not an interpretation proposal")
    payload = event.payload
    required = {
        "subject",
        "predicate",
        "object_text",
        "evidence_digest",
        "confidence",
    }
    if set(payload) != required:
        raise HiveValidationError("interpretation payload has unexpected fields")
    confidence = float(payload["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise HiveValidationError("interpretation confidence is out of range")
    fields = {
        key: str(payload[key]).strip()
        for key in ("subject", "predicate", "object_text", "evidence_digest")
    }
    if any(not value for value in fields.values()):
        raise HiveValidationError("interpretation fields cannot be empty")
    return Interpretation(
        event_id=event.event_id,
        node_id=event.origin_node_id,
        subject=fields["subject"],
        predicate=fields["predicate"],
        object_text=fields["object_text"],
        evidence_digest=fields["evidence_digest"],
        confidence=confidence,
    )


def reconcile(journals: Iterable[EventJournal]) -> tuple[ReconciliationResult, ...]:
    """Merge proposals without using node count as evidence or choosing a winner."""

    unique: dict[str, HiveEvent] = {}
    for journal in journals:
        for event in journal.events:
            prior = unique.get(event.event_id)
            if prior is not None and prior != event:
                raise HiveValidationError("same event id has different content")
            unique[event.event_id] = event

    grouped: dict[tuple[str, str], list[Interpretation]] = {}
    for event in unique.values():
        if event.event_type != "interpretation.proposed":
            continue
        item = interpretation_from_event(event)
        grouped.setdefault((item.subject, item.predicate), []).append(item)

    results: list[ReconciliationResult] = []
    for (subject, predicate), items in sorted(grouped.items()):
        positions = tuple(sorted({item.object_text for item in items}))
        state = "hive_shared" if len(positions) == 1 else "contested"
        results.append(
            ReconciliationResult(
                subject=subject,
                predicate=predicate,
                state=state,
                positions=positions,
                event_ids=tuple(sorted(item.event_id for item in items)),
                evidence_digests=tuple(
                    sorted({item.evidence_digest for item in items})
                ),
            )
        )
    return tuple(results)


def reconciliation_digest(results: Iterable[ReconciliationResult]) -> str:
    payload = [
        {
            "evidence_digests": list(result.evidence_digests),
            "event_ids": list(result.event_ids),
            "positions": list(result.positions),
            "predicate": result.predicate,
            "state": result.state,
            "subject": result.subject,
        }
        for result in results
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()

