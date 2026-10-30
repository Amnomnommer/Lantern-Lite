"""Fail-closed SQLite persistence for synthetic Lantern hive journals."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from lantern.hive.events import (
    EventJournal,
    HiveEvent,
    HiveValidationError,
    NodeRecord,
)


class SyntheticHiveCorruptionError(RuntimeError):
    """Raised when a synthetic hive ledger cannot be trusted."""


class SyntheticHiveStore:
    """Persistent synthetic journal isolated from authoritative memory."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.path.exists() and self.path.stat().st_size > 0
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA secure_delete = ON")
        if existing and not self._sqlite_healthy():
            self._connection.close()
            raise SyntheticHiveCorruptionError("synthetic ledger SQLite integrity failed")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._create_schema()
        if existing:
            try:
                self.load_journal()
            except (HiveValidationError, sqlite3.DatabaseError) as error:
                self._connection.close()
                raise SyntheticHiveCorruptionError(
                    "synthetic ledger event verification failed"
                ) from error

    def __enter__(self) -> "SyntheticHiveStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _sqlite_healthy(self) -> bool:
        try:
            quick = tuple(str(row[0]) for row in self._connection.execute("PRAGMA quick_check"))
            violations = tuple(self._connection.execute("PRAGMA foreign_key_check"))
        except sqlite3.DatabaseError:
            return False
        return quick == ("ok",) and not violations

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS synthetic_hive_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO synthetic_hive_metadata(key, value)
            VALUES ('schema_version', '1');

            CREATE TABLE IF NOT EXISTS synthetic_nodes (
                node_id TEXT PRIMARY KEY,
                identity_name TEXT NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                enrolled_at TEXT NOT NULL,
                credential_fingerprint TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS synthetic_events (
                event_id TEXT PRIMARY KEY,
                origin_node_id TEXT NOT NULL REFERENCES synthetic_nodes(node_id),
                origin_sequence INTEGER NOT NULL CHECK (origin_sequence >= 1),
                event_type TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                digest TEXT NOT NULL,
                UNIQUE(origin_node_id, origin_sequence)
            );

            CREATE TABLE IF NOT EXISTS synthetic_event_parents (
                event_id TEXT NOT NULL REFERENCES synthetic_events(event_id),
                parent_id TEXT NOT NULL REFERENCES synthetic_events(event_id),
                PRIMARY KEY(event_id, parent_id),
                CHECK(event_id <> parent_id)
            );

            CREATE TRIGGER IF NOT EXISTS synthetic_nodes_no_update
            BEFORE UPDATE ON synthetic_nodes BEGIN
                SELECT RAISE(ABORT, 'synthetic node registry is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS synthetic_nodes_no_delete
            BEFORE DELETE ON synthetic_nodes BEGIN
                SELECT RAISE(ABORT, 'synthetic node registry is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS synthetic_events_no_update
            BEFORE UPDATE ON synthetic_events BEGIN
                SELECT RAISE(ABORT, 'synthetic events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS synthetic_events_no_delete
            BEFORE DELETE ON synthetic_events BEGIN
                SELECT RAISE(ABORT, 'synthetic events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS synthetic_parents_no_update
            BEFORE UPDATE ON synthetic_event_parents BEGIN
                SELECT RAISE(ABORT, 'synthetic event parents are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS synthetic_parents_no_delete
            BEFORE DELETE ON synthetic_event_parents BEGIN
                SELECT RAISE(ABORT, 'synthetic event parents are immutable');
            END;
            COMMIT;
            """
        )
        row = self._connection.execute(
            "SELECT value FROM synthetic_hive_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or int(row["value"]) != self.SCHEMA_VERSION:
            raise SyntheticHiveCorruptionError("unsupported synthetic ledger schema")

    def save_journal(self, journal: EventJournal) -> None:
        """Atomically import a verified journal without changing existing events."""

        journal_digest = journal.digest()
        existing = self.load_journal(allow_empty=True)
        if existing is not None:
            combined = EventJournal(journal.nodes.values(), mode=journal.mode)
            combined.import_events(existing.events)
            combined.import_events(journal.events)
            if combined.digest() != journal_digest:
                raise HiveValidationError(
                    "stored and incoming journals do not form the incoming state"
                )

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            stored_nodes = {
                str(row["node_id"]): dict(row)
                for row in self._connection.execute("SELECT * FROM synthetic_nodes")
            }
            for node_id, node in journal.nodes.items():
                record = asdict(node)
                prior = stored_nodes.get(node_id)
                if prior is not None and prior != record:
                    raise HiveValidationError(f"node registry mismatch: {node_id}")
                if prior is None:
                    self._connection.execute(
                        """
                        INSERT INTO synthetic_nodes(
                            node_id, identity_name, kind, state, enrolled_at,
                            credential_fingerprint
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        tuple(record[key] for key in (
                            "node_id", "identity_name", "kind", "state",
                            "enrolled_at", "credential_fingerprint"
                        )),
                    )
            if set(stored_nodes) - set(journal.nodes):
                raise HiveValidationError("incoming registry omits stored nodes")

            # Insert every event before its edges. A valid parent may sort after
            # its child when different nodes have skewed clocks, and the foreign
            # key must not turn that harmless ordering into a failed import.
            for event in journal.events:
                prior = self._connection.execute(
                    "SELECT event_id FROM synthetic_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if prior is not None:
                    continue
                self._connection.execute(
                    """
                    INSERT INTO synthetic_events(
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
            for event in journal.events:
                for parent_id in event.parent_ids:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO synthetic_event_parents(event_id, parent_id)
                        VALUES (?, ?)
                        """,
                        (event.event_id, parent_id),
                    )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        loaded = self.load_journal()
        if loaded.digest() != journal_digest:
            raise SyntheticHiveCorruptionError("synthetic ledger changed during commit")

    def load_journal(self, *, allow_empty: bool = False) -> EventJournal | None:
        nodes = tuple(
            NodeRecord(**dict(row))
            for row in self._connection.execute(
                "SELECT * FROM synthetic_nodes ORDER BY node_id"
            )
        )
        if not nodes:
            if allow_empty:
                return None
            raise HiveValidationError("synthetic ledger node registry is empty")
        mode = "authoritative" if len(nodes) == 1 and nodes[0].identity_name == "Lantern-Prime" else "simulation"
        journal = EventJournal(nodes, mode=mode)
        parents: dict[str, list[str]] = {}
        for row in self._connection.execute(
            "SELECT event_id, parent_id FROM synthetic_event_parents ORDER BY event_id, parent_id"
        ):
            parents.setdefault(str(row["event_id"]), []).append(str(row["parent_id"]))
        events = []
        for row in self._connection.execute(
            "SELECT * FROM synthetic_events ORDER BY origin_node_id, origin_sequence"
        ):
            value = dict(row)
            value["parent_ids"] = parents.get(str(row["event_id"]), [])
            events.append(HiveEvent.from_dict(value))
        journal.import_events(events)
        if not self._sqlite_healthy():
            raise SyntheticHiveCorruptionError("synthetic ledger integrity failed")
        return journal

