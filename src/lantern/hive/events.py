"""Deterministic, inspectable event primitives for synthetic hive tests.

This module does not grant distributed authority.  Its authoritative mode is
deliberately restricted to one Lantern-Prime registry entry; multi-node work
must use simulation mode and cannot emit privileged event types.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Iterable, Mapping


LIFECYCLE_STATES = frozenset(
    {
        "node_local",
        "hive_pending",
        "hive_shared",
        "contested",
        "consolidated",
        "restricted",
        "revoked",
    }
)
NODE_STATES = frozenset({"active", "dormant", "quarantined", "revoked"})
NODE_KINDS = frozenset({"cognitive", "relay"})
PRIVILEGED_EVENT_TYPES = frozenset(
    {
        "authority.transferred",
        "external.action",
        "node.enrolled",
        "permission.granted",
        "relay.unlock",
    }
)
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class HiveValidationError(ValueError):
    """Raised when an event or registry violates hive invariants."""


class HiveForkError(HiveValidationError):
    """Raised when one origin sequence identifies different events."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _aware_iso8601(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise HiveValidationError("created_at must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HiveValidationError("created_at must include a UTC offset")
    return parsed.isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class NodeRecord:
    node_id: str
    identity_name: str
    kind: str
    state: str
    enrolled_at: str
    credential_fingerprint: str

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.identity_name.strip():
            raise HiveValidationError("node identity fields cannot be empty")
        if self.kind not in NODE_KINDS:
            raise HiveValidationError(f"unsupported node kind: {self.kind}")
        if self.state not in NODE_STATES:
            raise HiveValidationError(f"unsupported node state: {self.state}")
        if not self.credential_fingerprint.strip():
            raise HiveValidationError("credential fingerprint cannot be empty")
        object.__setattr__(self, "enrolled_at", _aware_iso8601(self.enrolled_at))


@dataclass(frozen=True, slots=True)
class HiveEvent:
    event_id: str
    origin_node_id: str
    origin_sequence: int
    event_type: str
    lifecycle_state: str
    payload_json: str
    parent_ids: tuple[str, ...]
    created_at: str
    digest: str

    @classmethod
    def create(
        cls,
        *,
        origin_node_id: str,
        origin_sequence: int,
        event_type: str,
        lifecycle_state: str,
        payload: Mapping[str, object],
        created_at: str,
        parent_ids: Iterable[str] = (),
    ) -> "HiveEvent":
        if not origin_node_id.strip():
            raise HiveValidationError("origin node cannot be empty")
        if origin_sequence < 1:
            raise HiveValidationError("origin sequence must begin at one")
        if not _EVENT_TYPE.fullmatch(event_type):
            raise HiveValidationError("event type must be a dotted identifier")
        if lifecycle_state not in LIFECYCLE_STATES:
            raise HiveValidationError(
                f"unsupported lifecycle state: {lifecycle_state}"
            )
        normalized_parents = tuple(sorted(set(parent_ids)))
        if any(not parent.strip() for parent in normalized_parents):
            raise HiveValidationError("parent identifiers cannot be empty")
        normalized_time = _aware_iso8601(created_at)
        payload_json = _canonical_json(dict(payload))
        body = {
            "created_at": normalized_time,
            "event_type": event_type,
            "lifecycle_state": lifecycle_state,
            "origin_node_id": origin_node_id,
            "origin_sequence": origin_sequence,
            "parent_ids": normalized_parents,
            "payload_json": payload_json,
        }
        digest = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
        return cls(
            event_id=f"evt-{digest}",
            origin_node_id=origin_node_id,
            origin_sequence=origin_sequence,
            event_type=event_type,
            lifecycle_state=lifecycle_state,
            payload_json=payload_json,
            parent_ids=normalized_parents,
            created_at=normalized_time,
            digest=digest,
        )

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise HiveValidationError("event payload must be an object")
        return value

    def verify(self) -> None:
        rebuilt = self.create(
            origin_node_id=self.origin_node_id,
            origin_sequence=self.origin_sequence,
            event_type=self.event_type,
            lifecycle_state=self.lifecycle_state,
            payload=self.payload,
            created_at=self.created_at,
            parent_ids=self.parent_ids,
        )
        if rebuilt.event_id != self.event_id or rebuilt.digest != self.digest:
            raise HiveValidationError(f"event integrity failure: {self.event_id}")

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["parent_ids"] = list(self.parent_ids)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "HiveEvent":
        try:
            event = cls(
                event_id=str(value["event_id"]),
                origin_node_id=str(value["origin_node_id"]),
                origin_sequence=int(value["origin_sequence"]),
                event_type=str(value["event_type"]),
                lifecycle_state=str(value["lifecycle_state"]),
                payload_json=str(value["payload_json"]),
                parent_ids=tuple(str(item) for item in value["parent_ids"]),
                created_at=str(value["created_at"]),
                digest=str(value["digest"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HiveValidationError("malformed event record") from error
        event.verify()
        return event


class EventJournal:
    """An in-memory deterministic journal used before distributed activation."""

    def __init__(
        self,
        nodes: Iterable[NodeRecord],
        *,
        mode: str = "authoritative",
    ) -> None:
        if mode not in {"authoritative", "simulation"}:
            raise HiveValidationError(f"unsupported journal mode: {mode}")
        registry = {node.node_id: node for node in nodes}
        if not registry:
            raise HiveValidationError("node registry cannot be empty")
        if mode == "authoritative":
            active = [node for node in registry.values() if node.state == "active"]
            if len(registry) != 1 or len(active) != 1:
                raise HiveValidationError(
                    "Gate 2 authoritative registry must contain one active node"
                )
            if active[0].identity_name != "Lantern-Prime":
                raise HiveValidationError(
                    "Gate 2 authoritative node must be Lantern-Prime"
                )
        self.mode = mode
        self.nodes = dict(sorted(registry.items()))
        self._events: dict[str, HiveEvent] = {}
        self._sequences: dict[tuple[str, int], str] = {}

    @property
    def events(self) -> tuple[HiveEvent, ...]:
        return tuple(
            sorted(
                self._events.values(),
                key=lambda event: (
                    event.created_at,
                    event.origin_node_id,
                    event.origin_sequence,
                    event.event_id,
                ),
            )
        )

    def append(self, event: HiveEvent) -> None:
        self.import_events((event,))

    def import_events(self, events: Iterable[HiveEvent]) -> None:
        incoming = tuple(events)
        candidate_events = dict(self._events)
        candidate_sequences = dict(self._sequences)
        for event in incoming:
            event.verify()
            node = self.nodes.get(event.origin_node_id)
            if node is None:
                raise HiveValidationError(
                    f"event origin is not enrolled: {event.origin_node_id}"
                )
            if node.state != "active":
                raise HiveValidationError(
                    f"inactive node cannot append: {event.origin_node_id}"
                )
            if event.event_type in PRIVILEGED_EVENT_TYPES:
                raise HiveValidationError(
                    f"synthetic journal cannot grant authority: {event.event_type}"
                )
            existing = candidate_events.get(event.event_id)
            if existing is not None:
                if existing != event:
                    raise HiveForkError("event identifier collision")
                continue
            sequence_key = (event.origin_node_id, event.origin_sequence)
            prior_id = candidate_sequences.get(sequence_key)
            if prior_id is not None and prior_id != event.event_id:
                raise HiveForkError(
                    f"origin sequence fork: {event.origin_node_id} "
                    f"#{event.origin_sequence}"
                )
            candidate_events[event.event_id] = event
            candidate_sequences[sequence_key] = event.event_id

        self._verify_candidate(candidate_events, candidate_sequences)
        self._events = candidate_events
        self._sequences = candidate_sequences

    def _verify_candidate(
        self,
        events: Mapping[str, HiveEvent],
        sequences: Mapping[tuple[str, int], str],
    ) -> None:
        per_origin: dict[str, list[int]] = {}
        for origin, sequence in sequences:
            per_origin.setdefault(origin, []).append(sequence)
        for origin, values in per_origin.items():
            ordered = sorted(values)
            if ordered != list(range(1, len(ordered) + 1)):
                raise HiveValidationError(
                    f"non-contiguous sequence for {origin}: {ordered}"
                )
        for event in events.values():
            missing = [parent for parent in event.parent_ids if parent not in events]
            if missing:
                raise HiveValidationError(
                    f"event {event.event_id} has missing parents: {missing}"
                )
            if event.event_id in event.parent_ids:
                raise HiveValidationError("event cannot parent itself")
        self._verify_acyclic(events)

    @staticmethod
    def _verify_acyclic(events: Mapping[str, HiveEvent]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(event_id: str) -> None:
            if event_id in visited:
                return
            if event_id in visiting:
                raise HiveValidationError("event parent graph contains a cycle")
            visiting.add(event_id)
            for parent_id in events[event_id].parent_ids:
                visit(parent_id)
            visiting.remove(event_id)
            visited.add(event_id)

        for event_id in sorted(events):
            visit(event_id)

    def digest(self) -> str:
        payload = {
            "events": [event.as_dict() for event in self.events],
            "mode": self.mode,
            "nodes": [asdict(self.nodes[key]) for key in sorted(self.nodes)],
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

