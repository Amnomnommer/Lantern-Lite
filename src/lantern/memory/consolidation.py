"""Deterministic episodic consolidation for Lantern's sleep cycle."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from lantern.memory.store import EVIDENCE_TYPES, MemoryStore
from lantern.vault_guard import require_vault_open


EPISODE_CATEGORIES = frozenset(
    {
        "event",
        "participant",
        "prior_belief",
        "goal",
        "owner_intent",
        "choice",
        "lantern_action",
        "outcome",
        "correction",
        "uncertainty",
        "dissent",
        "unresolved",
        "lesson",
    }
)
CATEGORY_ORDER = (
    "event",
    "participant",
    "prior_belief",
    "goal",
    "owner_intent",
    "choice",
    "lantern_action",
    "outcome",
    "correction",
    "uncertainty",
    "dissent",
    "unresolved",
    "lesson",
)
CATEGORY_LABELS = {
    "event": "What happened",
    "participant": "Who was involved",
    "prior_belief": "Prior belief",
    "goal": "Goal",
    "owner_intent": "Owner intent",
    "choice": "Choice",
    "lantern_action": "Lantern action",
    "outcome": "Outcome",
    "correction": "Correction",
    "uncertainty": "Uncertainty",
    "dissent": "Dissent",
    "unresolved": "Unresolved",
    "lesson": "Possible lesson",
}


STAGING_QUOTA_BYTES = 64 * 1024 * 1024
STAGING_BACKGROUND_THRESHOLD = 0.60
STAGING_PRIORITY_THRESHOLD = 0.75
STAGING_CRITICAL_THRESHOLD = 0.90
STAGING_MAX_AGE_DAYS = 30
TOPIC_STATUSES = frozenset({"active", "resolved"})
SIGNIFICANCE_LEVELS = frozenset({"low", "normal", "high"})


@dataclass(frozen=True, slots=True)
class StagingUsage:
    raw_bytes: int
    quota_bytes: int
    utilization: float
    pressure: str
    accepts_large_input: bool


@dataclass(frozen=True, slots=True)
class ConsolidationCandidate:
    session_id: str
    reason: str
    topic_status: str
    significance: str
    raw_bytes: int
    created_at: str


class StagingQuotaExceeded(RuntimeError):
    """A staging write would exceed its hard raw-text quota."""


class StagingConsolidationRequired(RuntimeError):
    """Critical pressure requires consolidation before another large input."""


@dataclass(frozen=True, slots=True)
class StagedObservation:
    key: str
    category: str
    content: str
    evidence_type: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class StagedSession:
    id: str
    raw_text: str | None
    source_digest: str
    status: str
    topic_status: str
    significance: str
    episode_id: str | None
    failed_attempts: int
    last_fidelity_error: str | None
    created_at: str
    consolidated_at: str | None


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    episode_id: str
    summary: str
    included_keys: tuple[str, ...]
    source_expired: bool
    reused_durable_episode: bool


class FidelityError(ValueError):
    """A candidate omitted required meaning or introduced unsupported content."""

    def __init__(
        self,
        message: str,
        *,
        missing_keys: Sequence[str] = (),
        unsupported_keys: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.missing_keys = tuple(missing_keys)
        self.unsupported_keys = tuple(unsupported_keys)


class StagingStore:
    """Separate bounded-purpose SQLite store for temporary raw conversation."""

    SCHEMA_VERSION = 2

    def __init__(
        self,
        path: str | Path,
        *,
        quota_bytes: int = STAGING_QUOTA_BYTES,
        large_input_bytes: int = 1024 * 1024,
    ) -> None:
        if quota_bytes <= 0:
            raise ValueError("staging quota must be positive")
        if large_input_bytes <= 0 or large_input_bytes > quota_bytes:
            raise ValueError("large-input threshold must fit within quota")
        self.path = Path(path)
        self.quota_bytes = quota_bytes
        self.large_input_bytes = large_input_bytes
        require_vault_open(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA secure_delete = ON")
        self._create_schema()

    def __enter__(self) -> "StagingStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def _create_schema(self) -> None:
        version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if version not in (0, 1, self.SCHEMA_VERSION):
            raise RuntimeError(f"unsupported staging schema version {version}")
        if version == self.SCHEMA_VERSION:
            return

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if version == 0:
                self._connection.execute(
                    """
                    CREATE TABLE staged_sessions (
                        id TEXT PRIMARY KEY,
                        raw_text TEXT,
                        source_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'consolidated')
                        ),
                        topic_status TEXT NOT NULL DEFAULT 'active' CHECK (
                            topic_status IN ('active', 'resolved')
                        ),
                        significance TEXT NOT NULL DEFAULT 'normal' CHECK (
                            significance IN ('low', 'normal', 'high')
                        ),
                        episode_id TEXT,
                        failed_attempts INTEGER NOT NULL DEFAULT 0,
                        last_fidelity_error TEXT,
                        created_at TEXT NOT NULL,
                        consolidated_at TEXT,
                        CHECK (
                            (status = 'pending' AND raw_text IS NOT NULL)
                            OR
                            (status = 'consolidated' AND raw_text IS NULL)
                        )
                    )
                    """
                )
                self._connection.execute(
                    """
                    CREATE TABLE staged_observations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL
                            REFERENCES staged_sessions(id) ON DELETE CASCADE,
                        observation_key TEXT NOT NULL,
                        category TEXT NOT NULL,
                        content TEXT NOT NULL,
                        evidence_type TEXT NOT NULL,
                        required INTEGER NOT NULL CHECK (required IN (0, 1)),
                        sequence INTEGER NOT NULL,
                        UNIQUE(session_id, observation_key)
                    )
                    """
                )
            else:
                self._connection.execute(
                    """
                    ALTER TABLE staged_sessions
                    ADD COLUMN topic_status TEXT NOT NULL DEFAULT 'active'
                    CHECK (topic_status IN ('active', 'resolved'))
                    """
                )
                self._connection.execute(
                    """
                    ALTER TABLE staged_sessions
                    ADD COLUMN significance TEXT NOT NULL DEFAULT 'normal'
                    CHECK (significance IN ('low', 'normal', 'high'))
                    """
                )
            self._connection.execute(
                f"PRAGMA user_version = {self.SCHEMA_VERSION}"
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def stage_session(
        self,
        raw_text: str,
        observations: Sequence[StagedObservation],
        *,
        topic_status: str = "active",
        significance: str = "normal",
    ) -> str:
        if not raw_text:
            raise ValueError("raw staging text must not be empty")
        if not observations:
            raise ValueError("at least one structured observation is required")
        if topic_status not in TOPIC_STATUSES:
            raise ValueError("unsupported topic status")
        if significance not in SIGNIFICANCE_LEVELS:
            raise ValueError("unsupported significance level")

        incoming_bytes = len(raw_text.encode("utf-8"))
        before = self.usage()
        if before.raw_bytes + incoming_bytes > self.quota_bytes:
            raise StagingQuotaExceeded(
                "raw staging input would exceed the hard quota"
            )
        if (
            before.pressure == "consolidate_required"
            and incoming_bytes >= self.large_input_bytes
        ):
            raise StagingConsolidationRequired(
                "consolidate staged topics before accepting another large input"
            )

        keys: set[str] = set()
        for observation in observations:
            if (
                not observation.key.strip()
                or not observation.content.strip()
                or observation.category not in EPISODE_CATEGORIES
            ):
                raise ValueError("invalid staged observation")
            if observation.evidence_type not in EVIDENCE_TYPES:
                raise ValueError(
                    f"unsupported evidence type: {observation.evidence_type}"
                )
            if observation.key in keys:
                raise ValueError("staged observation keys must be unique")
            keys.add(observation.key)

        session_id = str(uuid.uuid4())
        created_at = self._now()
        source_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                INSERT INTO staged_sessions(
                    id, raw_text, source_digest, status,
                    topic_status, significance, created_at
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    session_id,
                    raw_text,
                    source_digest,
                    topic_status,
                    significance,
                    created_at,
                ),
            )
            for sequence, observation in enumerate(observations):
                self._connection.execute(
                    """
                    INSERT INTO staged_observations(
                        session_id, observation_key, category, content,
                        evidence_type, required, sequence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        observation.key,
                        observation.category,
                        observation.content,
                        observation.evidence_type,
                        int(observation.required),
                        sequence,
                    ),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        return session_id

    def get_session(self, session_id: str) -> StagedSession:
        row = self._connection.execute(
            "SELECT * FROM staged_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return StagedSession(
            id=row["id"],
            raw_text=row["raw_text"],
            source_digest=row["source_digest"],
            status=row["status"],
            topic_status=row["topic_status"],
            significance=row["significance"],
            episode_id=row["episode_id"],
            failed_attempts=int(row["failed_attempts"]),
            last_fidelity_error=row["last_fidelity_error"],
            created_at=row["created_at"],
            consolidated_at=row["consolidated_at"],
        )

    def pending_sessions(self) -> list[StagedSession]:
        """Return pending raw sessions for explicit owner review."""

        rows = self._connection.execute(
            """
            SELECT id FROM staged_sessions
            WHERE status = 'pending'
            ORDER BY created_at, id
            """
        )
        return [self.get_session(str(row["id"])) for row in rows]

    def discard_pending(self, session_id: str) -> None:
        """Hard-delete an uncommitted session after explicit dismissal."""

        current = self.get_session(session_id)
        if current.status != "pending":
            raise ValueError("only pending sessions can be discarded")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "DELETE FROM staged_observations WHERE session_id = ?",
                (session_id,),
            )
            self._connection.execute(
                "DELETE FROM staged_sessions WHERE id = ?",
                (session_id,),
            )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.execute("VACUUM")

    def observations(self, session_id: str) -> list[StagedObservation]:
        rows = self._connection.execute(
            """
            SELECT observation_key, category, content,
                   evidence_type, required
            FROM staged_observations
            WHERE session_id = ?
            ORDER BY sequence
            """,
            (session_id,),
        )
        return [
            StagedObservation(
                key=row["observation_key"],
                category=row["category"],
                content=row["content"],
                evidence_type=row["evidence_type"],
                required=bool(row["required"]),
            )
            for row in rows
        ]

    def record_fidelity_failure(self, session_id: str, reason: str) -> None:
        self._connection.execute(
            """
            UPDATE staged_sessions
            SET failed_attempts = failed_attempts + 1,
                last_fidelity_error = ?
            WHERE id = ? AND status = 'pending'
            """,
            (reason, session_id),
        )

    def usage(self) -> StagingUsage:
        row = self._connection.execute(
            """
            SELECT COALESCE(
                SUM(length(CAST(raw_text AS BLOB))),
                0
            )
            FROM staged_sessions
            WHERE status = 'pending'
            """
        ).fetchone()
        raw_bytes = int(row[0])
        utilization = raw_bytes / self.quota_bytes
        if utilization >= STAGING_CRITICAL_THRESHOLD:
            pressure = "consolidate_required"
        elif utilization >= STAGING_PRIORITY_THRESHOLD:
            pressure = "priority"
        elif utilization >= STAGING_BACKGROUND_THRESHOLD:
            pressure = "background"
        else:
            pressure = "normal"
        return StagingUsage(
            raw_bytes=raw_bytes,
            quota_bytes=self.quota_bytes,
            utilization=utilization,
            pressure=pressure,
            accepts_large_input=pressure != "consolidate_required",
        )

    def consolidation_candidates(
        self,
        *,
        as_of: datetime | None = None,
    ) -> list[ConsolidationCandidate]:
        moment = as_of or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("candidate time must be timezone-aware")
        expiry_cutoff = moment - timedelta(days=STAGING_MAX_AGE_DAYS)
        pressure = self.usage().pressure
        rows = list(
            self._connection.execute(
                """
                SELECT id, topic_status, significance, created_at,
                       length(CAST(raw_text AS BLOB)) AS raw_bytes
                FROM staged_sessions
                WHERE status = 'pending'
                """
            )
        )

        candidates: list[tuple[tuple[object, ...], ConsolidationCandidate]] = []
        for row in rows:
            created = datetime.fromisoformat(str(row["created_at"]))
            age_due = created <= expiry_cutoff
            resolved = row["topic_status"] == "resolved"
            if pressure == "normal" and not age_due:
                continue
            if pressure == "background" and not (age_due or resolved):
                continue
            reason = "age" if age_due else pressure
            candidate = ConsolidationCandidate(
                session_id=str(row["id"]),
                reason=reason,
                topic_status=str(row["topic_status"]),
                significance=str(row["significance"]),
                raw_bytes=int(row["raw_bytes"]),
                created_at=str(row["created_at"]),
            )
            significance_rank = {"low": 0, "normal": 1, "high": 2}[
                candidate.significance
            ]
            sort_key = (
                0 if age_due else 1,
                0 if resolved else 1,
                significance_rank,
                candidate.created_at,
                candidate.session_id,
            )
            candidates.append((sort_key, candidate))
        candidates.sort(key=lambda item: item[0])
        return [candidate for _, candidate in candidates]

    def mark_consolidated(self, session_id: str, episode_id: str) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                """
                UPDATE staged_sessions
                SET raw_text = NULL,
                    status = 'consolidated',
                    episode_id = ?,
                    last_fidelity_error = NULL,
                    consolidated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (episode_id, self._now(), session_id),
            )
            if cursor.rowcount == 0:
                current = self.get_session(session_id)
                if (
                    current.status != "consolidated"
                    or current.episode_id != episode_id
                ):
                    raise RuntimeError(
                        "staging state changed during consolidation"
                    )
            else:
                self._connection.execute(
                    """
                    DELETE FROM staged_observations
                    WHERE session_id = ?
                    """,
                    (session_id,),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class EpisodeConsolidator:
    """Turn structured staging evidence into a verified durable episode."""

    def __init__(
        self,
        memory: MemoryStore,
        staging: StagingStore,
    ) -> None:
        self.memory = memory
        self.staging = staging

    def consolidate(
        self,
        session_id: str,
        *,
        include_keys: Sequence[str] | None = None,
        retention_class: str = "ordinary",
    ) -> ConsolidationResult:
        session = self.staging.get_session(session_id)
        if session.status == "consolidated":
            assert session.episode_id is not None
            episode = self.memory.get_episode(session.episode_id)
            facts = self.memory.episode_facts(session.episode_id)
            return ConsolidationResult(
                episode_id=session.episode_id,
                summary=str(episode["summary"]),
                included_keys=tuple(
                    str(fact["observation_key"]) for fact in facts
                ),
                source_expired=session.raw_text is None,
                reused_durable_episode=True,
            )

        observations = self.staging.observations(session_id)
        available = {item.key: item for item in observations}
        requested = (
            [item.key for item in observations]
            if include_keys is None
            else list(include_keys)
        )

        if len(requested) != len(set(requested)):
            error = FidelityError("candidate contains duplicate observation keys")
            self.staging.record_fidelity_failure(session_id, str(error))
            raise error

        unsupported = [key for key in requested if key not in available]
        included = set(requested)
        missing = [
            item.key
            for item in observations
            if item.required and item.key not in included
        ]
        if unsupported or missing:
            error = FidelityError(
                "candidate failed summary fidelity",
                missing_keys=missing,
                unsupported_keys=unsupported,
            )
            self.staging.record_fidelity_failure(session_id, str(error))
            raise error

        selected = [item for item in observations if item.key in included]
        summary = self.render_summary(selected)
        facts = [
            {
                "observation_key": item.key,
                "category": item.category,
                "content": item.content,
                "evidence_type": item.evidence_type,
                "required": item.required,
            }
            for item in selected
        ]
        episode_id, reused = self.memory.add_consolidated_episode(
            session_id,
            session.source_digest,
            summary,
            facts,
            retention_class=retention_class,
        )
        self.staging.mark_consolidated(session_id, episode_id)
        return ConsolidationResult(
            episode_id=episode_id,
            summary=summary,
            included_keys=tuple(item.key for item in selected),
            source_expired=True,
            reused_durable_episode=reused,
        )

    @staticmethod
    def render_summary(
        observations: Sequence[StagedObservation],
    ) -> str:
        """Render only supplied evidence, in a stable inspectable form."""

        grouped: dict[str, list[StagedObservation]] = {
            category: [] for category in CATEGORY_ORDER
        }
        for observation in observations:
            if observation.category not in EPISODE_CATEGORIES:
                raise ValueError(
                    f"unsupported episode category: {observation.category}"
                )
            grouped[observation.category].append(observation)

        lines: list[str] = []
        for category in CATEGORY_ORDER:
            items = grouped[category]
            if not items:
                continue
            rendered = "; ".join(
                f"[{item.evidence_type}] {item.content}" for item in items
            )
            lines.append(f"{CATEGORY_LABELS[category]}: {rendered}")
        if not lines:
            raise ValueError("cannot render an empty episode")
        return "\n".join(lines)

