"""Inspectable SQLite-backed durable memory for Lantern."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from lantern.hive.events import HiveEvent, HiveValidationError
from lantern.memory.schema import MIGRATIONS, REPLACEABLE_INDEXES, SCHEMA_VERSION
from lantern.vault_guard import require_vault_open


PRIME_NODE_ID = "node-lantern-prime-v1"


EVIDENCE_TYPES = frozenset(
    {
        "observed",
        "reported_by_amelie",
        "retrieved",
        "remembered",
        "inferred",
        "predicted",
        "imagined",
        "unknown",
    }
)
RETENTION_CLASSES = frozenset(
    {"working", "temporary", "ordinary", "protected", "required_record"}
)
CLAIM_RELATIONS = frozenset(
    {"supports", "contradicts", "derived_from", "corrects", "supersedes"}
)
PREFERENCE_PERSPECTIVES = frozenset(
    {"lantern_self", "owner_report", "owner_approval_prediction"}
)
PREFERENCE_LEVEL_CONFIDENCE = {
    "inclination": 1.0 / 3.0,
    "provisional": 2.0 / 3.0,
    "established": 1.0,
}
FORGETTABLE_KINDS = frozenset(
    {
        "claim", "episode", "preference", "source", "entity",
        "cognitive_schema", "deliberation",
    }
)
DELETION_REASON_CODES = frozenset(
    {"owner_request", "privacy", "correction", "expiry", "obsolete"}
)
_RETENTION_RANK = {
    "working": 0,
    "temporary": 1,
    "ordinary": 2,
    "protected": 3,
    "required_record": 4,
}
COGNITIVE_SCHEMA_FAMILIES = frozenset(
    {
        "concept", "situation", "roles_social", "causal", "procedure",
        "creative", "self", "ethical",
    }
)
COGNITIVE_SCHEMA_STATES = frozenset(
    {"candidate", "provisional", "active", "contested", "superseded", "retired"}
)
COGNITIVE_SCHEMA_OPERATIONS = frozenset(
    {
        "creation", "assimilation", "accommodation", "differentiation",
        "integration", "inhibition", "annotation",
    }
)
COGNITIVE_SCHEMA_PRIVACY_CLASSES = frozenset(
    {"private", "owner_shared", "public"}
)
COGNITIVE_SCHEMA_EVIDENCE_RELATIONS = frozenset(
    {"supports", "counterexample", "contradicts", "exception"}
)
_SCHEMA_STATE_TRANSITIONS = {
    "candidate": {"candidate", "provisional", "contested", "retired"},
    "provisional": {"provisional", "active", "contested", "superseded", "retired"},
    "active": {"active", "contested", "superseded", "retired"},
    "contested": {"contested", "provisional", "active", "superseded", "retired"},
    "superseded": {"superseded", "retired"},
    "retired": {"retired"},
}


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    subject_entity_id: str
    predicate: str
    object_text: str
    evidence_type: str
    confidence: float
    source_id: str | None
    episode_id: str | None
    consequential: bool
    status: str
    retention_class: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Preference:
    id: str
    holder_entity_id: str
    topic: str
    preferred_option: str
    perspective: str
    level: str
    confidence: float
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CognitiveSchema:
    id: str
    family: str
    scope: str
    privacy_class: str
    retention_class: str
    lifecycle_state: str
    current_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CognitiveSchemaVersion:
    id: str
    schema_id: str
    version_number: int
    parent_version_id: str | None
    operation: str
    description: str
    representation: dict[str, object]
    confidence: float
    revision_reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    id: str
    target_kind: str
    retention_class: str
    requested_by: str
    reason_code: str
    protected_reconfirmed: bool
    completed_at: str
    backup_purge_due_at: str


class ProtectedMemoryConfirmationRequired(PermissionError):
    """Deletion may continue only after the owner explicitly reconfirms."""

    def __init__(self, target_kind: str, retention_class: str) -> None:
        super().__init__(
            f"{retention_class} {target_kind} requires owner reconfirmation"
        )
        self.target_kind = target_kind
        self.retention_class = retention_class


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    healthy: bool
    writable: bool
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    detail: str


@dataclass(frozen=True, slots=True)
class IntegritySnapshot:
    id: str
    schema_version: int
    state_digest: str
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HiveNode:
    node_id: str
    identity_name: str
    node_kind: str
    lifecycle_state: str
    authority_scope: str
    credential_state: str
    credential_fingerprint: str | None
    authoritative: bool
    enrolled_at: str


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    receipt_id: str
    operation: str
    target_kind: str
    outcome: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Deliberation:
    id: str
    subject: str
    predicate: str
    lifecycle_state: str
    retention_class: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DeliberationPosition:
    id: str
    deliberation_id: str
    label: str
    interpretation: str
    confidence: float
    assumptions: tuple[str, ...]
    disposition: str
    created_at: str


@dataclass(frozen=True, slots=True)
class HiveProjection:
    authority_mode: str
    node_id: str
    event_count: int
    head_event_id: str
    head_digest: str
    event_type_counts: tuple[tuple[str, int], ...]
    mutation_receipt_count: int
    latest_content_digest: str | None


class MemoryCorruptionError(RuntimeError):
    """The authoritative store failed validation before writable startup."""

    def __init__(self, report: IntegrityReport) -> None:
        super().__init__(
            "memory integrity validation failed; writable startup refused"
        )
        self.report = report


class MemoryStore:
    """Small transactional memory store with explicit evidence and history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        require_vault_open(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.exists() and self.path.stat().st_size > 0
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA secure_delete = ON")
        if existing:
            report = self._inspect_connection(self._connection)
            if not report.healthy:
                self._connection.close()
                raise MemoryCorruptionError(report)
        if sys.platform != "emscripten":  # a browser's emulated files have no shared memory for WAL (Lite page)
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        try:
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _new_id() -> str:
        return str(uuid.uuid4())

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:  # inside batch(): joins it, committed once at its end
            yield
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Many writes as one transaction: one sync to disk instead of one per write (all or none)."""
        with self._transaction():
            yield

    def _migrate(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["version"]
            for row in self._connection.execute(
                "SELECT version FROM schema_migrations"
            )
        }
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            with self._transaction():
                legacy_digest = None
                legacy_counts = None
                genesis_time = None
                if version == 7:
                    legacy_digest = self._content_digest_for_connection(
                        self._connection
                    )
                    legacy_counts = self._content_counts_for_connection(
                        self._connection
                    )
                    row = self._connection.execute(
                        """
                        SELECT MIN(applied_at) AS applied_at
                        FROM schema_migrations
                        """
                    ).fetchone()
                    genesis_time = (
                        str(row["applied_at"])
                        if row is not None and row["applied_at"] is not None
                        else self._now()
                    )
                for statement in MIGRATIONS[version]:
                    self._connection.execute(statement)
                if version == 7:
                    assert legacy_digest is not None
                    assert legacy_counts is not None
                    assert genesis_time is not None
                    self._populate_hive_genesis(
                        legacy_digest=legacy_digest,
                        legacy_counts=legacy_counts,
                        created_at=genesis_time,
                    )
                self._connection.execute(
                    """
                    INSERT INTO schema_migrations(version, name, applied_at)
                    VALUES (?, ?, ?)
                    """,
                    (version, f"memory_schema_v{version}", self._now()),
                )

        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported schema version {self.schema_version}; "
                f"expected {SCHEMA_VERSION}"
            )

    def _populate_hive_genesis(
        self,
        *,
        legacy_digest: str,
        legacy_counts: Mapping[str, int],
        created_at: str,
    ) -> None:
        """Anchor the pre-hive store without granting distributed authority."""

        event = HiveEvent.create(
            origin_node_id=PRIME_NODE_ID,
            origin_sequence=1,
            event_type="memory.genesis",
            lifecycle_state="hive_shared",
            payload={
                "authority_mode": "owner_controlled_single_host",
                "legacy_content_counts": dict(sorted(legacy_counts.items())),
                "legacy_content_digest": legacy_digest,
                "migration": "memory_schema_v7",
                "schema_version_from": 6,
            },
            created_at=created_at,
        )
        self._connection.execute(
            """
            INSERT INTO hive_nodes(
                node_id, identity_name, node_kind, lifecycle_state,
                authority_scope, credential_state, credential_fingerprint,
                authoritative, enrolled_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                PRIME_NODE_ID,
                "Lantern-Prime",
                "cognitive",
                "active",
                "owner_controlled_single_host",
                "pending",
                None,
                1,
                event.created_at,
            ),
        )
        self._connection.execute(
            """
            INSERT INTO hive_events(
                event_id, origin_node_id, origin_sequence, event_type,
                lifecycle_state, payload_json, created_at, digest
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.origin_node_id,
                event.origin_sequence,
                event.event_type,
                event.lifecycle_state,
                event.payload_json,
                event.created_at,
                event.digest,
            ),
        )

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        assert row is not None
        return int(row["version"])

    @staticmethod
    def _inspect_connection(
        connection: sqlite3.Connection,
    ) -> IntegrityReport:
        try:
            quick = tuple(
                str(row[0])
                for row in connection.execute("PRAGMA quick_check")
            )
            violations = sum(
                1 for _ in connection.execute("PRAGMA foreign_key_check")
            )
            hive_error = MemoryStore._hive_ledger_error(connection)
        except sqlite3.DatabaseError as error:
            return IntegrityReport(
                healthy=False,
                writable=False,
                quick_check=(),
                foreign_key_violations=0,
                detail=f"sqlite validation error: {type(error).__name__}",
            )
        healthy = quick == ("ok",) and violations == 0 and hive_error is None
        detail = (
            "integrity checks passed"
            if healthy
            else (
                f"hive ledger validation failed: {hive_error}"
                if hive_error is not None
                else "quick-check or foreign-key validation failed"
            )
        )
        return IntegrityReport(
            healthy=healthy,
            writable=healthy,
            quick_check=quick,
            foreign_key_violations=violations,
            detail=detail,
        )

    @staticmethod
    def _hive_ledger_error(connection: sqlite3.Connection) -> str | None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"hive_nodes", "hive_events", "hive_event_parents"}
        if not (tables & required):
            return None
        if not required <= tables:
            return "incomplete hive ledger schema"
        original_factory = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            nodes = tuple(
                connection.execute(
                    "SELECT * FROM hive_nodes ORDER BY node_id"
                )
            )
            if len(nodes) != 1:
                return "Gate 2H requires exactly one enrolled node"
            node = nodes[0]
            if (
                node["node_id"] != PRIME_NODE_ID
                or node["identity_name"] != "Lantern-Prime"
                or node["lifecycle_state"] != "active"
                or int(node["authoritative"]) != 1
            ):
                return "authoritative Lantern-Prime registry invariant failed"

            parents: dict[str, list[str]] = {}
            for row in connection.execute(
                """
                SELECT event_id, parent_id
                FROM hive_event_parents
                ORDER BY event_id, parent_id
                """
            ):
                parents.setdefault(str(row["event_id"]), []).append(
                    str(row["parent_id"])
                )
            events: list[HiveEvent] = []
            for row in connection.execute(
                """
                SELECT * FROM hive_events
                ORDER BY origin_node_id, origin_sequence
                """
            ):
                value = dict(row)
                value["parent_ids"] = parents.get(str(row["event_id"]), [])
                events.append(HiveEvent.from_dict(value))
            if not events:
                return "Gate 2H requires a genesis event"
            genesis = events[0]
            if (
                genesis.origin_node_id != PRIME_NODE_ID
                or genesis.origin_sequence != 1
                or genesis.event_type != "memory.genesis"
                or genesis.lifecycle_state != "hive_shared"
                or genesis.parent_ids
            ):
                return "genesis event invariant failed"
            sequences = [event.origin_sequence for event in events]
            if sequences != list(range(1, len(events) + 1)):
                return "Lantern-Prime event sequence is not contiguous"
            event_ids = {event.event_id for event in events}
            for event in events[1:]:
                if not event.parent_ids:
                    return "post-genesis event lacks a causal parent"
                if event.event_id in event.parent_ids:
                    return "event cannot parent itself"
                if any(parent not in event_ids for parent in event.parent_ids):
                    return "event has a missing causal parent"

            visiting: set[str] = set()
            visited: set[str] = set()
            by_id = {event.event_id: event for event in events}

            def visit(event_id: str) -> None:
                if event_id in visited:
                    return
                if event_id in visiting:
                    raise HiveValidationError("event parent graph contains a cycle")
                visiting.add(event_id)
                for parent_id in by_id[event_id].parent_ids:
                    visit(parent_id)
                visiting.remove(event_id)
                visited.add(event_id)

            for event in events:
                visit(event.event_id)
        except (HiveValidationError, json.JSONDecodeError, TypeError, ValueError) as error:
            return str(error)
        finally:
            connection.row_factory = original_factory
        return None

    @classmethod
    def inspect_database(cls, path: str | Path) -> IntegrityReport:
        database_path = Path(path)
        if not database_path.is_file():
            return IntegrityReport(
                healthy=False,
                writable=False,
                quick_check=(),
                foreign_key_violations=0,
                detail="database file is missing",
            )
        uri = database_path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.DatabaseError as error:
            return IntegrityReport(
                healthy=False,
                writable=False,
                quick_check=(),
                foreign_key_violations=0,
                detail=f"sqlite open error: {type(error).__name__}",
            )
        try:
            return cls._inspect_connection(connection)
        finally:
            connection.close()

    def check_integrity(self) -> IntegrityReport:
        return self._inspect_connection(self._connection)

    def assert_integrity(self) -> None:
        report = self.check_integrity()
        if not report.healthy:
            raise MemoryCorruptionError(report)

    def missing_replaceable_indexes(self) -> tuple[str, ...]:
        existing = {
            str(row["name"])
            for row in self._connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                """
            )
        }
        return tuple(
            name for name in REPLACEABLE_INDEXES if name not in existing
        )

    def rebuild_replaceable_indexes(self) -> tuple[str, ...]:
        """Recreate derived indexes without changing authoritative records."""

        self.assert_integrity()
        before = self._state_digest_for_connection(self._connection)
        rebuilt = tuple(REPLACEABLE_INDEXES)
        with self._transaction():
            for name, statement in REPLACEABLE_INDEXES.items():
                safe_name = name.replace('"', '""')
                self._connection.execute(
                    f'DROP INDEX IF EXISTS "{safe_name}"'
                )
                self._connection.execute(statement)
            after = self._state_digest_for_connection(self._connection)
            if after != before:
                raise RuntimeError(
                    "index rebuild changed authoritative memory state"
                )
        self.assert_integrity()
        if self.missing_replaceable_indexes():
            raise RuntimeError("index rebuild left required indexes missing")
        return rebuilt

    @staticmethod
    def _state_digest_for_connection(
        connection: sqlite3.Connection,
    ) -> str:
        original_factory = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            tables = [
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%'
                      AND name <> 'integrity_snapshots'
                    ORDER BY name
                    """
                )
            ]
            canonical: list[dict[str, object]] = []
            for table in tables:
                safe_table = table.replace('"', '""')
                serialized_rows = [
                    json.dumps(
                        dict(row),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    for row in connection.execute(
                        f'SELECT * FROM "{safe_table}"'
                    )
                ]
                canonical.append(
                    {
                        "table": table,
                        "rows": sorted(serialized_rows),
                    }
                )
            payload = json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
        finally:
            connection.row_factory = original_factory

    @staticmethod
    def _content_tables_for_connection(
        connection: sqlite3.Connection,
    ) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name NOT IN ('schema_migrations', 'integrity_snapshots')
                  AND name NOT LIKE 'hive_%'
                ORDER BY name
                """
            )
        )

    @classmethod
    def _content_digest_for_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> str:
        original_factory = connection.row_factory
        connection.row_factory = sqlite3.Row
        try:
            canonical: list[dict[str, object]] = []
            for table in cls._content_tables_for_connection(connection):
                safe_table = table.replace('"', '""')
                rows = [
                    json.dumps(
                        dict(row), sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    for row in connection.execute(f'SELECT * FROM "{safe_table}"')
                ]
                canonical.append({"table": table, "rows": sorted(rows)})
            payload = json.dumps(
                canonical, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            return hashlib.sha256(payload).hexdigest()
        finally:
            connection.row_factory = original_factory

    @classmethod
    def _content_counts_for_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        return {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for table in cls._content_tables_for_connection(connection)
        }

    def hive_nodes(self) -> tuple[HiveNode, ...]:
        """Return the immutable Gate 2H authoritative node registry."""

        return tuple(
            HiveNode(
                node_id=str(row["node_id"]),
                identity_name=str(row["identity_name"]),
                node_kind=str(row["node_kind"]),
                lifecycle_state=str(row["lifecycle_state"]),
                authority_scope=str(row["authority_scope"]),
                credential_state=str(row["credential_state"]),
                credential_fingerprint=(
                    None
                    if row["credential_fingerprint"] is None
                    else str(row["credential_fingerprint"])
                ),
                authoritative=bool(row["authoritative"]),
                enrolled_at=str(row["enrolled_at"]),
            )
            for row in self._connection.execute(
                "SELECT * FROM hive_nodes ORDER BY node_id"
            )
        )

    def hive_events(self) -> tuple[HiveEvent, ...]:
        """Return verified immutable events from the authoritative ledger."""

        self.assert_integrity()
        parents: dict[str, list[str]] = {}
        for row in self._connection.execute(
            """
            SELECT event_id, parent_id FROM hive_event_parents
            ORDER BY event_id, parent_id
            """
        ):
            parents.setdefault(str(row["event_id"]), []).append(
                str(row["parent_id"])
            )
        return tuple(
            HiveEvent.from_dict(
                {
                    **dict(row),
                    "parent_ids": parents.get(str(row["event_id"]), []),
                }
            )
            for row in self._connection.execute(
                """
                SELECT * FROM hive_events
                ORDER BY origin_node_id, origin_sequence
                """
            )
        )

    def append_hive_event(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        lifecycle_state: str = "node_local",
        parent_ids: Sequence[str] | None = None,
    ) -> HiveEvent:
        """Append one local Prime event without granting new authority."""

        if event_type == "memory.genesis":
            raise ValueError("memory genesis can only be created by migration")
        if lifecycle_state not in {"node_local", "hive_pending"}:
            raise ValueError(
                "Gate 2H authoritative events must remain local or pending"
            )
        self.assert_integrity()
        with self._transaction():
            event = self._append_hive_event_in_transaction(
                event_type,
                payload,
                lifecycle_state=lifecycle_state,
                parent_ids=parent_ids,
            )
            error = self._hive_ledger_error(self._connection)
            if error is not None:
                raise RuntimeError(f"hive event append failed validation: {error}")
        self.assert_integrity()
        return event

    def _append_hive_event_in_transaction(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        lifecycle_state: str = "node_local",
        parent_ids: Sequence[str] | None = None,
    ) -> HiveEvent:
        latest = self._connection.execute(
                """
                SELECT event_id, origin_sequence
                FROM hive_events
                WHERE origin_node_id = ?
                ORDER BY origin_sequence DESC
                LIMIT 1
                """,
                (PRIME_NODE_ID,),
        ).fetchone()
        if latest is None:
            raise RuntimeError("authoritative hive genesis is missing")
        normalized_parents = (
            (str(latest["event_id"]),)
            if parent_ids is None
            else tuple(parent_ids)
        )
        if not normalized_parents:
            raise ValueError("post-genesis events require a causal parent")
        known = {
            str(row["event_id"])
            for row in self._connection.execute(
                "SELECT event_id FROM hive_events"
            )
        }
        if any(parent not in known for parent in normalized_parents):
            raise ValueError("event causal parent is not in the local ledger")
        event = HiveEvent.create(
            origin_node_id=PRIME_NODE_ID,
            origin_sequence=int(latest["origin_sequence"]) + 1,
            event_type=event_type,
            lifecycle_state=lifecycle_state,
            payload=payload,
            parent_ids=normalized_parents,
            created_at=self._now(),
        )
        self._connection.execute(
                """
                INSERT INTO hive_events(
                    event_id, origin_node_id, origin_sequence, event_type,
                    lifecycle_state, payload_json, created_at, digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.origin_node_id,
                    event.origin_sequence,
                    event.event_type,
                    event.lifecycle_state,
                    event.payload_json,
                    event.created_at,
                    event.digest,
                ),
        )
        for parent_id in event.parent_ids:
            self._connection.execute(
                    """
                    INSERT INTO hive_event_parents(event_id, parent_id)
                    VALUES (?, ?)
                    """,
                (event.event_id, parent_id),
            )
        return event

    def _record_mutation_receipt_in_transaction(
        self,
        operation: str,
        target_kind: str,
        *,
        created_at: str | None = None,
    ) -> MutationReceipt:
        """Record a content-free, unlinkable receipt in the causal journal.

        Receipt identifiers are fresh random values.  They are never derived
        from a memory identifier, content, or digest, and no durable mapping
        back to the affected record is retained.  This lets the journal prove
        that a class of mutation committed without defeating hard forgetting.
        """

        if operation not in {
            "claim.corrected", "memory.forgotten",
            "schema.created", "schema.revised",
            "deliberation.opened", "deliberation.resolved",
        }:
            raise ValueError(f"unsupported mutation receipt: {operation}")
        if target_kind not in {
            "claim", "memory", "cognitive_schema", "deliberation",
        }:
            raise ValueError(f"unsupported receipt target: {target_kind}")
        receipt = MutationReceipt(
            receipt_id=f"receipt-{self._new_id()}",
            operation=operation,
            target_kind=target_kind,
            outcome="committed",
            created_at=created_at or self._now(),
        )
        self._connection.execute(
            """
            INSERT INTO mutation_receipts(
                receipt_id, operation, target_kind, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt.receipt_id,
                receipt.operation,
                receipt.target_kind,
                receipt.outcome,
                receipt.created_at,
            ),
        )
        self._append_hive_event_in_transaction(
            operation,
            {
                "outcome": receipt.outcome,
                "receipt_id": receipt.receipt_id,
                "target_kind": receipt.target_kind,
                "unlinkable": True,
            },
        )
        return receipt

    def mutation_receipts(self) -> tuple[MutationReceipt, ...]:
        """Return content-free mutation receipts in causal order."""

        return tuple(
            MutationReceipt(
                receipt_id=str(row["receipt_id"]),
                operation=str(row["operation"]),
                target_kind=str(row["target_kind"]),
                outcome=str(row["outcome"]),
                created_at=str(row["created_at"]),
            )
            for row in self._connection.execute(
                """
                SELECT receipt_id, operation, target_kind, outcome, created_at
                FROM mutation_receipts
                ORDER BY created_at, receipt_id
                """
            )
        )

    def rebuild_hive_projection(self) -> HiveProjection:
        """Rebuild the inspectable Gate 2H current view from ledger events."""

        events = self.hive_events()
        if not events:
            raise RuntimeError("authoritative hive genesis is missing")
        counts: dict[str, int] = {}
        latest_content_digest: str | None = None
        receipt_count = 0
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
            if event.event_type == "memory.checkpointed":
                digest = event.payload.get("content_digest")
                if isinstance(digest, str):
                    latest_content_digest = digest
            if isinstance(event.payload.get("receipt_id"), str):
                receipt_count += 1
        head = max(events, key=lambda event: event.origin_sequence)
        return HiveProjection(
            authority_mode="owner_controlled_single_host",
            node_id=PRIME_NODE_ID,
            event_count=len(events),
            head_event_id=head.event_id,
            head_digest=head.digest,
            event_type_counts=tuple(sorted(counts.items())),
            mutation_receipt_count=receipt_count,
            latest_content_digest=latest_content_digest,
        )

    def verify_hive_projection(self) -> HiveProjection:
        """Fail closed if event-derived state disagrees with durable metadata."""

        projection = self.rebuild_hive_projection()
        nodes = self.hive_nodes()
        if len(nodes) != 1 or nodes[0].node_id != projection.node_id:
            raise MemoryCorruptionError(
                IntegrityReport(
                    healthy=False,
                    writable=False,
                    quick_check=("ok",),
                    foreign_key_violations=0,
                    detail="hive projection disagrees with node registry",
                )
            )
        if projection.mutation_receipt_count != len(self.mutation_receipts()):
            raise MemoryCorruptionError(
                IntegrityReport(
                    healthy=False,
                    writable=False,
                    quick_check=("ok",),
                    foreign_key_violations=0,
                    detail="hive projection disagrees with mutation receipts",
                )
            )
        return projection

    def create_deliberation(
        self,
        subject: str,
        predicate: str,
        positions: Sequence[Mapping[str, object]],
        *,
        retention_class: str = "ordinary",
    ) -> Deliberation:
        """Preserve competing interpretations without choosing by node count."""

        normalized_subject = subject.strip()
        normalized_predicate = predicate.strip()
        if not normalized_subject or not normalized_predicate:
            raise ValueError("deliberation subject and predicate are required")
        self._validate_retention(retention_class)
        normalized: list[
            tuple[str, str, float, tuple[str, ...], str, tuple[tuple[str, str, str], ...]]
        ] = []
        labels: set[str] = set()
        interpretations: set[str] = set()
        for item in positions:
            label = str(item.get("label", "")).strip()
            interpretation = str(item.get("interpretation", "")).strip()
            try:
                confidence = float(item.get("confidence", -1.0))
            except (TypeError, ValueError) as error:
                raise ValueError("position confidence must be numeric") from error
            assumptions_value = item.get("assumptions", ())
            if isinstance(assumptions_value, (str, bytes)):
                raise ValueError("position assumptions must be a sequence")
            try:
                assumptions = tuple(
                    str(value).strip() for value in assumptions_value  # type: ignore[union-attr]
                )
            except TypeError as error:
                raise ValueError("position assumptions must be a sequence") from error
            disposition = str(item.get("disposition", "unresolved"))
            if not label or not interpretation:
                raise ValueError("positions require a label and interpretation")
            if label in labels or interpretation in interpretations:
                raise ValueError("deliberation positions must be distinct")
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("position confidence must be between zero and one")
            if any(not assumption for assumption in assumptions):
                raise ValueError("position assumptions cannot be empty")
            if disposition not in {"candidate", "unresolved"}:
                raise ValueError("new positions must be candidate or unresolved")
            evidence_items = item.get("evidence", ())
            if isinstance(evidence_items, (str, bytes)):
                raise ValueError("position evidence must be a sequence")
            evidence: list[tuple[str, str, str]] = []
            seen_evidence: set[tuple[str, str]] = set()
            try:
                iterator = iter(evidence_items)  # type: ignore[arg-type]
            except TypeError as error:
                raise ValueError("position evidence must be a sequence") from error
            for evidence_item in iterator:
                if not isinstance(evidence_item, Mapping):
                    raise ValueError("position evidence must contain objects")
                claim_id = str(evidence_item.get("claim_id", "")).strip()
                relation = str(evidence_item.get("relation", "")).strip()
                rationale = str(evidence_item.get("rationale", "")).strip()
                if not claim_id or not rationale:
                    raise ValueError("deliberation evidence requires claim and rationale")
                if relation not in {"supports", "contradicts", "qualifies"}:
                    raise ValueError(f"unsupported deliberation relation: {relation}")
                if (claim_id, relation) in seen_evidence:
                    raise ValueError("duplicate deliberation evidence")
                seen_evidence.add((claim_id, relation))
                evidence.append((claim_id, relation, rationale))
            labels.add(label)
            interpretations.add(interpretation)
            normalized.append(
                (
                    label, interpretation, confidence, assumptions,
                    disposition, tuple(evidence),
                )
            )
        if len(normalized) < 2:
            raise ValueError("a deliberation requires at least two positions")

        deliberation_id = self._new_id()
        created_at = self._now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO deliberations(
                    id, subject, predicate, lifecycle_state,
                    retention_class, created_at, updated_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    deliberation_id, normalized_subject, normalized_predicate,
                    retention_class, created_at, created_at,
                ),
            )
            for label, interpretation, confidence, assumptions, disposition, evidence in normalized:
                position_id = self._new_id()
                self._connection.execute(
                    """
                    INSERT INTO deliberation_positions(
                        id, deliberation_id, label, interpretation, confidence,
                        assumptions_json, disposition, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position_id, deliberation_id, label, interpretation,
                        confidence,
                        json.dumps(
                            assumptions, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":"),
                        ),
                        disposition, created_at,
                    ),
                )
                for claim_id, relation, rationale in evidence:
                    self._connection.execute(
                        """
                        INSERT INTO deliberation_evidence(
                            position_id, claim_id, relation, rationale
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (position_id, claim_id, relation, rationale),
                    )
            self._record_mutation_receipt_in_transaction(
                "deliberation.opened",
                "deliberation",
                created_at=created_at,
            )
        return self.get_deliberation(deliberation_id)

    def get_deliberation(self, deliberation_id: str) -> Deliberation:
        row = self._connection.execute(
            "SELECT * FROM deliberations WHERE id = ?",
            (deliberation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(deliberation_id)
        return Deliberation(
            id=str(row["id"]),
            subject=str(row["subject"]),
            predicate=str(row["predicate"]),
            lifecycle_state=str(row["lifecycle_state"]),
            retention_class=str(row["retention_class"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def deliberation_positions(
        self,
        deliberation_id: str,
    ) -> tuple[DeliberationPosition, ...]:
        self.get_deliberation(deliberation_id)
        return tuple(
            DeliberationPosition(
                id=str(row["id"]),
                deliberation_id=str(row["deliberation_id"]),
                label=str(row["label"]),
                interpretation=str(row["interpretation"]),
                confidence=float(row["confidence"]),
                assumptions=tuple(json.loads(str(row["assumptions_json"]))),
                disposition=str(row["disposition"]),
                created_at=str(row["created_at"]),
            )
            for row in self._connection.execute(
                """
                SELECT * FROM deliberation_positions
                WHERE deliberation_id = ? ORDER BY label, id
                """,
                (deliberation_id,),
            )
        )

    def deliberation_evidence(
        self,
        position_id: str,
    ) -> tuple[dict[str, str], ...]:
        return tuple(
            dict(row)
            for row in self._connection.execute(
                """
                SELECT claim_id, relation, rationale
                FROM deliberation_evidence
                WHERE position_id = ? ORDER BY claim_id, relation
                """,
                (position_id,),
            )
        )

    def resolve_deliberation(
        self,
        deliberation_id: str,
        selected_labels: Sequence[str],
        *,
        rationale: str,
    ) -> Deliberation:
        """Resolve while retaining rejected and unresolved alternatives."""

        deliberation = self.get_deliberation(deliberation_id)
        if deliberation.lifecycle_state != "open":
            raise ValueError("only an open deliberation can be resolved")
        selected = tuple(sorted({label.strip() for label in selected_labels}))
        if not selected or any(not label for label in selected):
            raise ValueError("at least one selected position is required")
        reason = rationale.strip()
        if not reason:
            raise ValueError("resolution rationale is required")
        positions = self.deliberation_positions(deliberation_id)
        known = {position.label for position in positions}
        if not set(selected) <= known:
            raise ValueError("selected position is not part of the deliberation")
        resolved_at = self._now()
        with self._transaction():
            for position in positions:
                disposition = (
                    "supported" if position.label in selected else "rejected"
                )
                self._connection.execute(
                    """
                    UPDATE deliberation_positions SET disposition = ?
                    WHERE id = ?
                    """,
                    (disposition, position.id),
                )
            self._connection.execute(
                """
                INSERT INTO deliberation_resolutions(
                    id, deliberation_id, selected_labels_json,
                    rationale, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._new_id(), deliberation_id,
                    json.dumps(selected, separators=(",", ":")),
                    reason, resolved_at,
                ),
            )
            self._connection.execute(
                """
                UPDATE deliberations
                SET lifecycle_state = 'resolved', updated_at = ?
                WHERE id = ?
                """,
                (resolved_at, deliberation_id),
            )
            self._record_mutation_receipt_in_transaction(
                "deliberation.resolved",
                "deliberation",
                created_at=resolved_at,
            )
        return self.get_deliberation(deliberation_id)

    def record_hive_checkpoint(
        self,
        reason: str = "manual",
    ) -> HiveEvent:
        """Bind the journal to current memory without copying private content."""

        if reason not in {"manual", "pre_backup", "post_restore", "pre_deploy"}:
            raise ValueError(f"unsupported hive checkpoint reason: {reason}")
        return self.append_hive_event(
            "memory.checkpointed",
            {
                "content_counts": self._content_counts_for_connection(
                    self._connection
                ),
                "content_digest": self._content_digest_for_connection(
                    self._connection
                ),
                "reason": reason,
                "schema_version": self.schema_version,
            },
        )

    def latest_hive_checkpoint(self) -> HiveEvent | None:
        """Return the newest verified checkpoint, if one has been recorded."""

        checkpoints = tuple(
            event
            for event in self.hive_events()
            if event.event_type == "memory.checkpointed"
        )
        return checkpoints[-1] if checkpoints else None

    def state_digest(self) -> str:
        self.assert_integrity()
        return self._state_digest_for_connection(self._connection)

    def content_digest(self) -> str:
        """Hash durable memory content without migration or hive metadata."""

        self.assert_integrity()
        return self._content_digest_for_connection(self._connection)

    @classmethod
    def digest_database(cls, path: str | Path) -> str:
        report = cls.inspect_database(path)
        if not report.healthy:
            raise MemoryCorruptionError(report)
        database_path = Path(path)
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            return cls._state_digest_for_connection(connection)
        finally:
            connection.close()

    def record_integrity_snapshot(
        self,
        reason: str = "manual",
    ) -> IntegritySnapshot:
        if reason not in {"manual", "pre_backup", "post_restore"}:
            raise ValueError(f"unsupported snapshot reason: {reason}")
        digest = self.state_digest()
        snapshot = IntegritySnapshot(
            id=self._new_id(),
            schema_version=self.schema_version,
            state_digest=digest,
            reason=reason,
            created_at=self._now(),
        )
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO integrity_snapshots(
                    id, schema_version, state_digest, reason, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.schema_version,
                    snapshot.state_digest,
                    snapshot.reason,
                    snapshot.created_at,
                ),
            )
        return snapshot

    def integrity_snapshots(self) -> list[IntegritySnapshot]:
        return [
            IntegritySnapshot(
                id=row["id"],
                schema_version=int(row["schema_version"]),
                state_digest=row["state_digest"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in self._connection.execute(
                """
                SELECT id, schema_version, state_digest, reason, created_at
                FROM integrity_snapshots
                ORDER BY created_at, id
                """
            )
        ]

    def copy_to(self, destination: str | Path) -> None:
        """Create a consistent SQLite copy after a full integrity check."""

        self.assert_integrity()
        destination_path = Path(destination)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination_path)
        try:
            self._connection.backup(target)
        finally:
            target.close()

    def add_entity(self, kind: str, canonical_name: str) -> str:
        entity_id = self._new_id()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO entities(id, kind, canonical_name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (entity_id, kind, canonical_name, self._now()),
            )
        return entity_id

    def add_source(
        self,
        kind: str,
        reference: str,
        *,
        excerpt: str | None = None,
    ) -> str:
        source_id = self._new_id()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO sources(id, kind, reference, excerpt, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, kind, reference, excerpt, self._now()),
            )
        return source_id

    def add_episode(
        self,
        summary: str,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        retention_class: str = "ordinary",
    ) -> str:
        self._validate_retention(retention_class)
        episode_id = self._new_id()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO episodes(
                    id, summary, started_at, ended_at,
                    retention_class, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    summary,
                    started_at,
                    ended_at,
                    retention_class,
                    self._now(),
                ),
            )
        return episode_id

    def add_consolidated_episode(
        self,
        staging_session_id: str,
        source_digest: str,
        summary: str,
        facts: Sequence[Mapping[str, object]],
        *,
        retention_class: str = "ordinary",
    ) -> tuple[str, bool]:
        """Commit a verified episode, or return its prior idempotent result."""

        self._validate_retention(retention_class)
        if not staging_session_id.strip() or not source_digest.strip():
            raise ValueError("consolidation identity and source digest are required")
        if not summary.strip():
            raise ValueError("episode summary must not be empty")

        normalized: list[tuple[str, str, str, str, int, int]] = []
        seen_keys: set[str] = set()
        for sequence, fact in enumerate(facts):
            observation_key = str(fact["observation_key"]).strip()
            category = str(fact["category"]).strip()
            content = str(fact["content"]).strip()
            evidence_type = str(fact["evidence_type"])
            required = int(bool(fact["required"]))
            if not observation_key or not category or not content:
                raise ValueError("episode facts require key, category, and content")
            if observation_key in seen_keys:
                raise ValueError("episode observation keys must be unique")
            if evidence_type not in EVIDENCE_TYPES:
                raise ValueError(f"unsupported evidence type: {evidence_type}")
            seen_keys.add(observation_key)
            normalized.append(
                (
                    observation_key,
                    category,
                    content,
                    evidence_type,
                    required,
                    sequence,
                )
            )
        if not normalized:
            raise ValueError("a consolidated episode requires at least one fact")

        existing = self._connection.execute(
            """
            SELECT episode_id, source_digest
            FROM consolidation_records
            WHERE staging_session_id = ?
            """,
            (staging_session_id,),
        ).fetchone()
        if existing is not None:
            if existing["source_digest"] != source_digest:
                raise ValueError(
                    "staging session identity was reused for different source data"
                )
            return str(existing["episode_id"]), True

        episode_id = self._new_id()
        created_at = self._now()
        summary_digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO episodes(
                    id, summary, started_at, ended_at,
                    retention_class, created_at
                )
                VALUES (?, ?, NULL, NULL, ?, ?)
                """,
                (episode_id, summary, retention_class, created_at),
            )
            for (
                observation_key,
                category,
                content,
                evidence_type,
                required,
                sequence,
            ) in normalized:
                self._connection.execute(
                    """
                    INSERT INTO episode_facts(
                        id, episode_id, observation_key, category, content,
                        evidence_type, required, sequence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._new_id(),
                        episode_id,
                        observation_key,
                        category,
                        content,
                        evidence_type,
                        required,
                        sequence,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO consolidation_records(
                    staging_session_id, source_digest, summary_digest,
                    episode_id, fidelity_status, created_at
                )
                VALUES (?, ?, ?, ?, 'passed', ?)
                """,
                (
                    staging_session_id,
                    source_digest,
                    summary_digest,
                    episode_id,
                    created_at,
                ),
            )
        return episode_id, False

    def get_episode(self, episode_id: str) -> dict[str, object]:
        row = self._connection.execute(
            """
            SELECT id, summary, started_at, ended_at,
                   retention_class, created_at
            FROM episodes
            WHERE id = ?
            """,
            (episode_id,),
        ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return dict(row)

    def episode_facts(self, episode_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT observation_key, category, content,
                   evidence_type, required, sequence
            FROM episode_facts
            WHERE episode_id = ?
            ORDER BY sequence
            """,
            (episode_id,),
        )
        facts: list[dict[str, object]] = []
        for row in rows:
            fact = dict(row)
            fact["required"] = bool(fact["required"])
            facts.append(fact)
        return facts

    def consolidation_record(
        self,
        staging_session_id: str,
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            """
            SELECT staging_session_id, source_digest, summary_digest,
                   episode_id, fidelity_status, created_at
            FROM consolidation_records
            WHERE staging_session_id = ?
            """,
            (staging_session_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def add_claim(
        self,
        subject_entity_id: str,
        predicate: str,
        object_text: str,
        *,
        evidence_type: str,
        confidence: float,
        source_id: str | None = None,
        episode_id: str | None = None,
        consequential: bool = True,
        retention_class: str = "ordinary",
    ) -> str:
        self._validate_claim(
            evidence_type=evidence_type,
            confidence=confidence,
            source_id=source_id,
            episode_id=episode_id,
            consequential=consequential,
            retention_class=retention_class,
        )
        claim_id = self._new_id()
        with self._transaction():
            self._insert_claim(
                claim_id=claim_id,
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                object_text=object_text,
                evidence_type=evidence_type,
                confidence=confidence,
                source_id=source_id,
                episode_id=episode_id,
                consequential=consequential,
                retention_class=retention_class,
            )
        return claim_id

    def _insert_claim(
        self,
        *,
        claim_id: str,
        subject_entity_id: str,
        predicate: str,
        object_text: str,
        evidence_type: str,
        confidence: float,
        source_id: str | None,
        episode_id: str | None,
        consequential: bool,
        retention_class: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO claims(
                id, subject_entity_id, predicate, object_text,
                evidence_type, confidence, source_id, episode_id,
                consequential, retention_class, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                subject_entity_id,
                predicate,
                object_text,
                evidence_type,
                confidence,
                source_id,
                episode_id,
                int(consequential),
                retention_class,
                self._now(),
            ),
        )

    def link_claims(
        self,
        from_claim_id: str,
        to_claim_id: str,
        *,
        relation: str,
        reason: str,
    ) -> str:
        if relation not in CLAIM_RELATIONS:
            raise ValueError(f"unsupported claim relation: {relation}")
        link_id = self._new_id()
        with self._transaction():
            self._insert_link(
                link_id=link_id,
                from_claim_id=from_claim_id,
                to_claim_id=to_claim_id,
                relation=relation,
                reason=reason,
            )
        return link_id

    def _insert_link(
        self,
        *,
        link_id: str,
        from_claim_id: str,
        to_claim_id: str,
        relation: str,
        reason: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO claim_links(
                id, from_claim_id, to_claim_id, relation, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                link_id,
                from_claim_id,
                to_claim_id,
                relation,
                reason,
                self._now(),
            ),
        )

    def correct_claim(
        self,
        claim_id: str,
        *,
        object_text: str,
        reason: str,
        actor: str,
        evidence_type: str,
        confidence: float,
        source_id: str | None = None,
        episode_id: str | None = None,
    ) -> str:
        old = self.get_claim(claim_id)
        if old.status != "active":
            raise ValueError("only an active claim can be corrected")
        self._validate_claim(
            evidence_type=evidence_type,
            confidence=confidence,
            source_id=source_id,
            episode_id=episode_id,
            consequential=old.consequential,
            retention_class=old.retention_class,
        )

        new_claim_id = self._new_id()
        changed_at = self._now()
        with self._transaction():
            self._insert_claim(
                claim_id=new_claim_id,
                subject_entity_id=old.subject_entity_id,
                predicate=old.predicate,
                object_text=object_text,
                evidence_type=evidence_type,
                confidence=confidence,
                source_id=source_id,
                episode_id=episode_id,
                consequential=old.consequential,
                retention_class=old.retention_class,
            )
            self._insert_link(
                link_id=self._new_id(),
                from_claim_id=new_claim_id,
                to_claim_id=old.id,
                relation="corrects",
                reason=reason,
            )
            self._connection.execute(
                "UPDATE claims SET status = 'superseded' WHERE id = ?",
                (old.id,),
            )
            self._connection.execute(
                """
                INSERT INTO claim_status_history(
                    claim_id, old_status, new_status, reason, actor, changed_at
                )
                VALUES (?, 'active', 'superseded', ?, ?, ?)
                """,
                (old.id, reason, actor, changed_at),
            )
            self._record_mutation_receipt_in_transaction(
                "claim.corrected",
                "claim",
                created_at=changed_at,
            )
        return new_claim_id


    def record_preference_choice(
        self,
        holder_entity_id: str,
        topic: str,
        preferred_option: str,
        *,
        episode_id: str,
        independence_key: str,
        reason: str,
        perspective: str = "lantern_self",
        alternative_text: str | None = None,
        tradeoff_present: bool = False,
        voluntary: bool = True,
        independent: bool = True,
        copied: bool = False,
        coerced: bool = False,
    ) -> Preference:
        """Record one qualifying choice and recompute its preference level."""

        if perspective not in PREFERENCE_PERSPECTIVES:
            raise ValueError(f"unsupported preference perspective: {perspective}")
        if not topic.strip() or not preferred_option.strip():
            raise ValueError("preference topic and option must not be empty")
        if not independence_key.strip() or not reason.strip():
            raise ValueError("preference evidence requires a key and reason")
        if not voluntary or coerced:
            raise ValueError("coerced or involuntary choices cannot count")
        if not independent or copied:
            raise ValueError("copied or non-independent choices cannot count")

        now = self._now()
        with self._transaction():
            row = self._connection.execute(
                """
                SELECT * FROM preferences
                WHERE holder_entity_id = ?
                  AND topic = ?
                  AND preferred_option = ?
                  AND perspective = ?
                """,
                (
                    holder_entity_id,
                    topic,
                    preferred_option,
                    perspective,
                ),
            ).fetchone()
            if row is None:
                preference_id = self._new_id()
                self._connection.execute(
                    """
                    INSERT INTO preferences(
                        id, holder_entity_id, topic, preferred_option,
                        perspective, level, confidence, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'inclination', ?, ?, ?)
                    """,
                    (
                        preference_id,
                        holder_entity_id,
                        topic,
                        preferred_option,
                        perspective,
                        PREFERENCE_LEVEL_CONFIDENCE["inclination"],
                        now,
                        now,
                    ),
                )
            else:
                preference_id = str(row["id"])

            duplicate = self._connection.execute(
                """
                SELECT 1 FROM preference_evidence
                WHERE preference_id = ? AND independence_key = ?
                """,
                (preference_id, independence_key),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("duplicate preference evidence cannot count")

            self._connection.execute(
                """
                INSERT INTO preference_evidence(
                    id, preference_id, episode_id, independence_key,
                    reason, alternative_text, tradeoff_present, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_id(),
                    preference_id,
                    episode_id,
                    independence_key,
                    reason,
                    alternative_text,
                    int(tradeoff_present),
                    now,
                ),
            )
            support = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS choice_count,
                    COUNT(DISTINCT episode_id) AS session_count,
                    MAX(
                        CASE
                            WHEN tradeoff_present = 1
                              OR alternative_text IS NOT NULL
                            THEN 1 ELSE 0
                        END
                    ) AS has_alternative
                FROM preference_evidence
                WHERE preference_id = ?
                """,
                (preference_id,),
            ).fetchone()
            assert support is not None
            choice_count = int(support["choice_count"])
            session_count = int(support["session_count"])
            has_alternative = bool(support["has_alternative"])

            if (
                choice_count >= 3
                and session_count >= 2
                and has_alternative
            ):
                level = "established"
            elif choice_count >= 2 and session_count >= 2:
                level = "provisional"
            else:
                level = "inclination"

            self._connection.execute(
                """
                UPDATE preferences
                SET level = ?, confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    level,
                    PREFERENCE_LEVEL_CONFIDENCE[level],
                    now,
                    preference_id,
                ),
            )

        return self.get_preference(preference_id)

    def get_preference(self, preference_id: str) -> Preference:
        row = self._connection.execute(
            "SELECT * FROM preferences WHERE id = ?",
            (preference_id,),
        ).fetchone()
        if row is None:
            raise KeyError(preference_id)
        return self._preference_from_row(row)

    def preference_evidence(
        self,
        preference_id: str,
    ) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT id, preference_id, episode_id, independence_key,
                   reason, alternative_text, tradeoff_present, created_at
            FROM preference_evidence
            WHERE preference_id = ?
            ORDER BY created_at, id
            """,
            (preference_id,),
        )
        evidence: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["tradeoff_present"] = bool(item["tradeoff_present"])
            evidence.append(item)
        return evidence

    def create_cognitive_schema(
        self,
        family: str,
        scope: str,
        description: str,
        representation: Mapping[str, object],
        *,
        confidence: float,
        evidence: Sequence[Mapping[str, str]],
        lifecycle_state: str = "candidate",
        privacy_class: str = "private",
        retention_class: str = "ordinary",
    ) -> CognitiveSchema:
        """Create an evidenced schema and its immutable first version."""

        self._validate_schema_fields(
            family=family,
            scope=scope,
            description=description,
            representation=representation,
            confidence=confidence,
            lifecycle_state=lifecycle_state,
            privacy_class=privacy_class,
            retention_class=retention_class,
        )
        normalized_evidence = self._normalize_schema_evidence(evidence)
        schema_id = self._new_id()
        version_id = self._new_id()
        created_at = self._now()
        representation_json = self._canonical_json(representation)
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO cognitive_schemas(
                    id, family, scope, privacy_class, retention_class,
                    lifecycle_state, current_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    schema_id, family, scope.strip(), privacy_class,
                    retention_class, lifecycle_state, created_at, created_at,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO cognitive_schema_versions(
                    id, schema_id, version_number, parent_version_id,
                    operation, description, representation_json, confidence,
                    revision_reason, created_at
                )
                VALUES (?, ?, 1, NULL, 'creation', ?, ?, ?, ?, ?)
                """,
                (
                    version_id, schema_id, description.strip(),
                    representation_json, confidence, "initial formulation",
                    created_at,
                ),
            )
            self._insert_schema_evidence(
                version_id,
                normalized_evidence,
                created_at,
            )
            self._record_mutation_receipt_in_transaction(
                "schema.created",
                "cognitive_schema",
                created_at=created_at,
            )
        return self.get_cognitive_schema(schema_id)

    def revise_cognitive_schema(
        self,
        schema_id: str,
        description: str,
        representation: Mapping[str, object],
        *,
        confidence: float,
        evidence: Sequence[Mapping[str, str]],
        operation: str,
        revision_reason: str,
        lifecycle_state: str | None = None,
    ) -> CognitiveSchemaVersion:
        """Append a schema version; prior formulations remain inspectable."""

        current = self.get_cognitive_schema(schema_id)
        target_state = lifecycle_state or current.lifecycle_state
        self._validate_schema_fields(
            family=current.family,
            scope=current.scope,
            description=description,
            representation=representation,
            confidence=confidence,
            lifecycle_state=target_state,
            privacy_class=current.privacy_class,
            retention_class=current.retention_class,
        )
        if operation not in COGNITIVE_SCHEMA_OPERATIONS - {"creation"}:
            raise ValueError(f"unsupported schema operation: {operation}")
        if target_state not in _SCHEMA_STATE_TRANSITIONS[current.lifecycle_state]:
            raise ValueError(
                f"invalid schema lifecycle transition: "
                f"{current.lifecycle_state} -> {target_state}"
            )
        reason = revision_reason.strip()
        if not reason:
            raise ValueError("schema revision reason is required")
        normalized_evidence = self._normalize_schema_evidence(evidence)
        parent = self._connection.execute(
            """
            SELECT id FROM cognitive_schema_versions
            WHERE schema_id = ? AND version_number = ?
            """,
            (schema_id, current.current_version),
        ).fetchone()
        assert parent is not None
        version_number = current.current_version + 1
        version_id = self._new_id()
        created_at = self._now()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO cognitive_schema_versions(
                    id, schema_id, version_number, parent_version_id,
                    operation, description, representation_json, confidence,
                    revision_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, schema_id, version_number, parent["id"],
                    operation, description.strip(),
                    self._canonical_json(representation), confidence, reason,
                    created_at,
                ),
            )
            self._insert_schema_evidence(
                version_id,
                normalized_evidence,
                created_at,
            )
            self._connection.execute(
                """
                UPDATE cognitive_schemas
                SET lifecycle_state = ?, current_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_state, version_number, created_at, schema_id),
            )
            self._record_mutation_receipt_in_transaction(
                "schema.revised",
                "cognitive_schema",
                created_at=created_at,
            )
        return self.cognitive_schema_versions(schema_id)[-1]

    def get_cognitive_schema(self, schema_id: str) -> CognitiveSchema:
        row = self._connection.execute(
            "SELECT * FROM cognitive_schemas WHERE id = ?",
            (schema_id,),
        ).fetchone()
        if row is None:
            raise KeyError(schema_id)
        return self._cognitive_schema_from_row(row)

    def list_cognitive_schemas(
        self,
        *,
        family: str | None = None,
        lifecycle_state: str | None = None,
    ) -> list[CognitiveSchema]:
        clauses: list[str] = []
        parameters: list[str] = []
        if family is not None:
            if family not in COGNITIVE_SCHEMA_FAMILIES:
                raise ValueError(f"unsupported schema family: {family}")
            clauses.append("family = ?")
            parameters.append(family)
        if lifecycle_state is not None:
            if lifecycle_state not in COGNITIVE_SCHEMA_STATES:
                raise ValueError(f"unsupported schema state: {lifecycle_state}")
            clauses.append("lifecycle_state = ?")
            parameters.append(lifecycle_state)
        query = "SELECT * FROM cognitive_schemas"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id"
        return [
            self._cognitive_schema_from_row(row)
            for row in self._connection.execute(query, parameters)
        ]

    def cognitive_schema_versions(
        self,
        schema_id: str,
    ) -> list[CognitiveSchemaVersion]:
        if self._connection.execute(
            "SELECT 1 FROM cognitive_schemas WHERE id = ?",
            (schema_id,),
        ).fetchone() is None:
            raise KeyError(schema_id)
        return [
            self._cognitive_schema_version_from_row(row)
            for row in self._connection.execute(
                """
                SELECT * FROM cognitive_schema_versions
                WHERE schema_id = ?
                ORDER BY version_number
                """,
                (schema_id,),
            )
        ]

    def cognitive_schema_evidence(
        self,
        version_id: str,
    ) -> list[dict[str, str]]:
        rows = self._connection.execute(
            """
            SELECT id, schema_version_id, claim_id, relation,
                   rationale, created_at
            FROM cognitive_schema_evidence
            WHERE schema_version_id = ?
            ORDER BY created_at, id
            """,
            (version_id,),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _canonical_json(value: Mapping[str, object]) -> str:
        try:
            return json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("schema representation must be JSON-compatible") from error

    @classmethod
    def _validate_schema_fields(
        cls,
        *,
        family: str,
        scope: str,
        description: str,
        representation: Mapping[str, object],
        confidence: float,
        lifecycle_state: str,
        privacy_class: str,
        retention_class: str,
    ) -> None:
        if family not in COGNITIVE_SCHEMA_FAMILIES:
            raise ValueError(f"unsupported schema family: {family}")
        if lifecycle_state not in COGNITIVE_SCHEMA_STATES:
            raise ValueError(f"unsupported schema state: {lifecycle_state}")
        if privacy_class not in COGNITIVE_SCHEMA_PRIVACY_CLASSES:
            raise ValueError(f"unsupported privacy class: {privacy_class}")
        cls._validate_retention(retention_class)
        if not scope.strip() or not description.strip():
            raise ValueError("schema scope and description are required")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("schema confidence must be between zero and one")
        cls._canonical_json(representation)

    @staticmethod
    def _normalize_schema_evidence(
        evidence: Sequence[Mapping[str, str]],
    ) -> list[tuple[str, str, str]]:
        normalized: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in evidence:
            claim_id = str(item.get("claim_id", "")).strip()
            relation = str(item.get("relation", "")).strip()
            rationale = str(item.get("rationale", "")).strip()
            if not claim_id or not rationale:
                raise ValueError("schema evidence requires claim_id and rationale")
            if relation not in COGNITIVE_SCHEMA_EVIDENCE_RELATIONS:
                raise ValueError(f"unsupported schema evidence relation: {relation}")
            if (claim_id, relation) in seen:
                raise ValueError("duplicate schema evidence")
            seen.add((claim_id, relation))
            normalized.append((claim_id, relation, rationale))
        if not normalized:
            raise ValueError("a cognitive schema requires claim evidence")
        return normalized

    def _insert_schema_evidence(
        self,
        version_id: str,
        evidence: Sequence[tuple[str, str, str]],
        created_at: str,
    ) -> None:
        for claim_id, relation, rationale in evidence:
            self._connection.execute(
                """
                INSERT INTO cognitive_schema_evidence(
                    id, schema_version_id, claim_id, relation,
                    rationale, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_id(), version_id, claim_id, relation,
                    rationale, created_at,
                ),
            )

    def record_counts(self) -> dict[str, int]:
        """Return content-free record counts for owner inspection."""

        tables = (
            "entities",
            "sources",
            "episodes",
            "claims",
            "relationships",
            "preferences",
            "cognitive_schemas",
            "deliberations",
        )
        return {
            table: int(
                self._connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
            for table in tables
        }

    def protect(
        self,
        target_kind: str,
        target_id: str,
        *,
        retention_class: str = "protected",
    ) -> str:
        """Raise a supported memory to protected or required-record retention."""

        tables = {
            "claim": "claims",
            "episode": "episodes",
            "cognitive_schema": "cognitive_schemas",
            "deliberation": "deliberations",
        }
        if target_kind not in tables:
            raise ValueError(
                "only claims, episodes, cognitive schemas, and deliberations "
                "can be protected"
            )
        if retention_class not in {"protected", "required_record"}:
            raise ValueError(
                "protection must be protected or required_record"
            )
        table = tables[target_kind]
        row = self._connection.execute(
            f"SELECT retention_class FROM {table} WHERE id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise KeyError(target_id)
        current = str(row["retention_class"])
        effective = max(
            (current, retention_class),
            key=_RETENTION_RANK.__getitem__,
        )
        if effective != current:
            with self._transaction():
                self._connection.execute(
                    f"UPDATE {table} SET retention_class = ? WHERE id = ?",
                    (effective, target_id),
                )
        return effective

    def forget(
        self,
        target_kind: str,
        target_id: str,
        *,
        requested_by: str,
        reason_code: str = "owner_request",
        reconfirmed: bool = False,
    ) -> DeletionReceipt:
        """Hard-delete one memory and retain only non-content audit metadata."""

        if target_kind not in FORGETTABLE_KINDS:
            raise ValueError(f"unsupported forgetting target: {target_kind}")
        actor = requested_by.strip()
        if not actor or len(actor) > 80 or "\n" in actor:
            raise ValueError("requested_by must be a short actor identifier")
        if reason_code not in DELETION_REASON_CODES:
            raise ValueError(f"unsupported deletion reason: {reason_code}")

        retention_class = self._effective_retention(target_kind, target_id)
        protected = retention_class in {"protected", "required_record"}
        if protected and not reconfirmed:
            raise ProtectedMemoryConfirmationRequired(
                target_kind,
                retention_class,
            )

        completed = datetime.now(timezone.utc)
        completed_at = completed.isoformat(timespec="microseconds")
        backup_purge_due_at = (
            completed + timedelta(days=93)
        ).isoformat(timespec="microseconds")
        deletion_id = self._new_id()

        with self._transaction():
            self._delete_target(target_kind, target_id)
            self._connection.execute(
                """
                INSERT INTO deletion_audit(
                    id, target_kind, retention_class, requested_by,
                    reason_code, protected_reconfirmed, process,
                    completed_at, backup_purge_due_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'MemoryStore.forget', ?, ?)
                """,
                (
                    deletion_id,
                    target_kind,
                    retention_class,
                    actor,
                    reason_code,
                    int(protected and reconfirmed),
                    completed_at,
                    backup_purge_due_at,
                ),
            )
            self._record_mutation_receipt_in_transaction(
                "memory.forgotten",
                "memory",
                created_at=completed_at,
            )

        self._scrub_deleted_bytes()
        return DeletionReceipt(
            id=deletion_id,
            target_kind=target_kind,
            retention_class=retention_class,
            requested_by=actor,
            reason_code=reason_code,
            protected_reconfirmed=protected and reconfirmed,
            completed_at=completed_at,
            backup_purge_due_at=backup_purge_due_at,
        )

    def deletion_audit(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT id, target_kind, retention_class, requested_by,
                   reason_code, protected_reconfirmed, process,
                   completed_at, backup_purge_due_at
            FROM deletion_audit
            ORDER BY completed_at, id
            """
        )
        records: list[dict[str, object]] = []
        for row in rows:
            record = dict(row)
            record["protected_reconfirmed"] = bool(
                record["protected_reconfirmed"]
            )
            records.append(record)
        return records

    def _effective_retention(
        self,
        target_kind: str,
        target_id: str,
    ) -> str:
        direct_table = {
            "claim": "claims",
            "episode": "episodes",
            "cognitive_schema": "cognitive_schemas",
            "deliberation": "deliberations",
        }
        if target_kind in direct_table:
            row = self._connection.execute(
                f"SELECT retention_class FROM {direct_table[target_kind]} "
                "WHERE id = ?",
                (target_id,),
            ).fetchone()
            if row is None:
                raise KeyError(target_id)
            classes = [str(row["retention_class"])]
            if target_kind == "episode":
                classes.extend(
                    str(item["retention_class"])
                    for item in self._connection.execute(
                        """
                        SELECT retention_class FROM claims
                        WHERE episode_id = ?
                        """,
                        (target_id,),
                    )
                )
            return max(classes, key=_RETENTION_RANK.__getitem__)

        table = {
            "preference": "preferences",
            "source": "sources",
            "entity": "entities",
        }[target_kind]
        exists = self._connection.execute(
            f"SELECT 1 FROM {table} WHERE id = ?",
            (target_id,),
        ).fetchone()
        if exists is None:
            raise KeyError(target_id)
        if target_kind == "preference":
            return "ordinary"

        column = (
            "source_id" if target_kind == "source" else "subject_entity_id"
        )
        classes = [
            str(row["retention_class"])
            for row in self._connection.execute(
                f"SELECT retention_class FROM claims WHERE {column} = ?",
                (target_id,),
            )
        ]
        return (
            max(classes, key=_RETENTION_RANK.__getitem__)
            if classes
            else "ordinary"
        )

    def _delete_target(self, target_kind: str, target_id: str) -> None:
        if target_kind == "claim":
            source_ids = self._delete_claims([target_id])
            self._delete_unreferenced_sources(source_ids)
            return

        if target_kind == "cognitive_schema":
            self._connection.execute(
                "DELETE FROM cognitive_schemas WHERE id = ?",
                (target_id,),
            )
            return

        if target_kind == "deliberation":
            self._connection.execute(
                "DELETE FROM deliberations WHERE id = ?",
                (target_id,),
            )
            return

        if target_kind == "preference":
            self._connection.execute(
                "DELETE FROM preference_evidence WHERE preference_id = ?",
                (target_id,),
            )
            self._connection.execute(
                "DELETE FROM preferences WHERE id = ?",
                (target_id,),
            )
            return

        if target_kind == "episode":
            claim_rows = list(
                self._connection.execute(
                    "SELECT id FROM claims WHERE episode_id = ?",
                    (target_id,),
                )
            )
            source_ids = self._delete_claims(
                [str(row["id"]) for row in claim_rows]
            )
            preference_ids = [
                str(row["preference_id"])
                for row in self._connection.execute(
                    """
                    SELECT DISTINCT preference_id
                    FROM preference_evidence
                    WHERE episode_id = ?
                    """,
                    (target_id,),
                )
            ]
            self._connection.execute(
                "DELETE FROM preference_evidence WHERE episode_id = ?",
                (target_id,),
            )
            self._refresh_preferences(preference_ids)
            self._connection.execute(
                "DELETE FROM relationships WHERE episode_id = ?",
                (target_id,),
            )
            self._connection.execute(
                "DELETE FROM consolidation_records WHERE episode_id = ?",
                (target_id,),
            )
            self._connection.execute(
                "DELETE FROM episode_facts WHERE episode_id = ?",
                (target_id,),
            )
            self._connection.execute(
                "DELETE FROM episodes WHERE id = ?",
                (target_id,),
            )
            self._delete_unreferenced_sources(source_ids)
            return

        if target_kind == "source":
            claim_rows = list(
                self._connection.execute(
                    "SELECT id FROM claims WHERE source_id = ?",
                    (target_id,),
                )
            )
            self._delete_claims([str(row["id"]) for row in claim_rows])
            self._connection.execute(
                "DELETE FROM relationships WHERE source_id = ?",
                (target_id,),
            )
            self._connection.execute(
                "DELETE FROM sources WHERE id = ?",
                (target_id,),
            )
            return

        if target_kind == "entity":
            claim_rows = list(
                self._connection.execute(
                    "SELECT id FROM claims WHERE subject_entity_id = ?",
                    (target_id,),
                )
            )
            source_ids = self._delete_claims(
                [str(row["id"]) for row in claim_rows]
            )
            preference_ids = [
                str(row["id"])
                for row in self._connection.execute(
                    "SELECT id FROM preferences WHERE holder_entity_id = ?",
                    (target_id,),
                )
            ]
            for preference_id in preference_ids:
                self._connection.execute(
                    """
                    DELETE FROM preference_evidence
                    WHERE preference_id = ?
                    """,
                    (preference_id,),
                )
            self._connection.execute(
                "DELETE FROM preferences WHERE holder_entity_id = ?",
                (target_id,),
            )
            self._connection.execute(
                """
                DELETE FROM relationships
                WHERE subject_entity_id = ? OR object_entity_id = ?
                """,
                (target_id, target_id),
            )
            self._connection.execute(
                "DELETE FROM entities WHERE id = ?",
                (target_id,),
            )
            self._delete_unreferenced_sources(source_ids)
            return

        raise AssertionError(f"unhandled target kind: {target_kind}")

    def _delete_claims(self, claim_ids: Sequence[str]) -> set[str]:
        if not claim_ids:
            return set()
        placeholders = ", ".join("?" for _ in claim_ids)
        source_ids = {
            str(row["source_id"])
            for row in self._connection.execute(
                f"""
                SELECT DISTINCT source_id FROM claims
                WHERE id IN ({placeholders}) AND source_id IS NOT NULL
                """,
                tuple(claim_ids),
            )
        }
        self._connection.execute(
            f"""
            DELETE FROM claim_links
            WHERE from_claim_id IN ({placeholders})
               OR to_claim_id IN ({placeholders})
            """,
            tuple(claim_ids) + tuple(claim_ids),
        )
        self._connection.execute(
            f"DELETE FROM claim_status_history "
            f"WHERE claim_id IN ({placeholders})",
            tuple(claim_ids),
        )
        self._connection.execute(
            f"DELETE FROM claims WHERE id IN ({placeholders})",
            tuple(claim_ids),
        )
        return source_ids

    def _delete_unreferenced_sources(self, source_ids: set[str]) -> None:
        for source_id in source_ids:
            referenced = self._connection.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM claims WHERE source_id = ?
                    )
                    OR EXISTS(
                        SELECT 1 FROM relationships WHERE source_id = ?
                    ) AS referenced
                """,
                (source_id, source_id),
            ).fetchone()
            assert referenced is not None
            if not bool(referenced["referenced"]):
                self._connection.execute(
                    "DELETE FROM sources WHERE id = ?",
                    (source_id,),
                )

    def _refresh_preferences(self, preference_ids: Sequence[str]) -> None:
        for preference_id in preference_ids:
            support = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS choice_count,
                    COUNT(DISTINCT episode_id) AS session_count,
                    MAX(
                        CASE
                            WHEN tradeoff_present = 1
                              OR alternative_text IS NOT NULL
                            THEN 1 ELSE 0
                        END
                    ) AS has_alternative
                FROM preference_evidence
                WHERE preference_id = ?
                """,
                (preference_id,),
            ).fetchone()
            assert support is not None
            choice_count = int(support["choice_count"])
            if choice_count == 0:
                self._connection.execute(
                    "DELETE FROM preferences WHERE id = ?",
                    (preference_id,),
                )
                continue
            session_count = int(support["session_count"])
            has_alternative = bool(support["has_alternative"])
            if (
                choice_count >= 3
                and session_count >= 2
                and has_alternative
            ):
                level = "established"
            elif choice_count >= 2 and session_count >= 2:
                level = "provisional"
            else:
                level = "inclination"
            self._connection.execute(
                """
                UPDATE preferences
                SET level = ?, confidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    level,
                    PREFERENCE_LEVEL_CONFIDENCE[level],
                    self._now(),
                    preference_id,
                ),
            )

    def _scrub_deleted_bytes(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.execute("VACUUM")
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def get_claim(self, claim_id: str) -> Claim:
        row = self._connection.execute(
            "SELECT * FROM claims WHERE id = ?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise KeyError(claim_id)
        return self._claim_from_row(row)

    def current_claims(
        self,
        *,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
    ) -> list[Claim]:
        clauses = ["status = 'active'"]
        parameters: list[str] = []
        if subject_entity_id is not None:
            clauses.append("subject_entity_id = ?")
            parameters.append(subject_entity_id)
        if predicate is not None:
            clauses.append("predicate = ?")
            parameters.append(predicate)
        query = (
            "SELECT * FROM claims WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, id"
        )
        return [
            self._claim_from_row(row)
            for row in self._connection.execute(query, parameters)
        ]

    def links_for_claim(self, claim_id: str) -> list[dict[str, str]]:
        rows = self._connection.execute(
            """
            SELECT id, from_claim_id, to_claim_id, relation, reason, created_at
            FROM claim_links
            WHERE from_claim_id = ? OR to_claim_id = ?
            ORDER BY created_at, id
            """,
            (claim_id, claim_id),
        )
        return [dict(row) for row in rows]

    def status_history(self, claim_id: str) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT id, claim_id, old_status, new_status,
                   reason, actor, changed_at
            FROM claim_status_history
            WHERE claim_id = ?
            ORDER BY id
            """,
            (claim_id,),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _preference_from_row(row: sqlite3.Row) -> Preference:
        return Preference(
            id=row["id"],
            holder_entity_id=row["holder_entity_id"],
            topic=row["topic"],
            preferred_option=row["preferred_option"],
            perspective=row["perspective"],
            level=row["level"],
            confidence=float(row["confidence"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _cognitive_schema_from_row(row: sqlite3.Row) -> CognitiveSchema:
        return CognitiveSchema(
            id=row["id"],
            family=row["family"],
            scope=row["scope"],
            privacy_class=row["privacy_class"],
            retention_class=row["retention_class"],
            lifecycle_state=row["lifecycle_state"],
            current_version=int(row["current_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _cognitive_schema_version_from_row(
        row: sqlite3.Row,
    ) -> CognitiveSchemaVersion:
        representation = json.loads(row["representation_json"])
        if not isinstance(representation, dict):
            raise ValueError("stored schema representation is not an object")
        return CognitiveSchemaVersion(
            id=row["id"],
            schema_id=row["schema_id"],
            version_number=int(row["version_number"]),
            parent_version_id=row["parent_version_id"],
            operation=row["operation"],
            description=row["description"],
            representation=representation,
            confidence=float(row["confidence"]),
            revision_reason=row["revision_reason"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        return Claim(
            id=row["id"],
            subject_entity_id=row["subject_entity_id"],
            predicate=row["predicate"],
            object_text=row["object_text"],
            evidence_type=row["evidence_type"],
            confidence=float(row["confidence"]),
            source_id=row["source_id"],
            episode_id=row["episode_id"],
            consequential=bool(row["consequential"]),
            status=row["status"],
            retention_class=row["retention_class"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _validate_retention(retention_class: str) -> None:
        if retention_class not in RETENTION_CLASSES:
            raise ValueError(f"unsupported retention class: {retention_class}")

    @classmethod
    def _validate_claim(
        cls,
        *,
        evidence_type: str,
        confidence: float,
        source_id: str | None,
        episode_id: str | None,
        consequential: bool,
        retention_class: str,
    ) -> None:
        if evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"unsupported evidence type: {evidence_type}")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        cls._validate_retention(retention_class)
        if (
            consequential
            and source_id is None
            and episode_id is None
            and evidence_type != "unknown"
        ):
            raise ValueError(
                "consequential claims require a source, episode, "
                "or explicit unknown evidence"
            )

