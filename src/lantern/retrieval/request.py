"""Privacy-bounded research requests and explicit offline fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3


_SECRET = re.compile(
    r"(?:password|passphrase|private[_ -]?key|api[_ -]?key|secret|/srv/lantern|lantern\.db)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    id: str
    question: str
    purpose: str
    privacy_class: str
    maximum_sources: int
    maximum_bytes: int
    stopping_condition: str
    created_at: str


def prepare_request(
    *,
    question: str,
    purpose: str,
    privacy_class: str,
    maximum_sources: int,
    maximum_bytes: int,
    stopping_condition: str,
    private_references: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ResearchRequest:
    """Create a public-only request; private memory is excluded by construction."""
    values = (question, purpose, stopping_condition)
    if not all(value.strip() for value in values):
        raise ValueError("research question, purpose, and stopping condition are required")
    if privacy_class != "public":
        raise PermissionError("autonomous retrieval is limited to public requests")
    if private_references:
        raise PermissionError("private memory references cannot enter a retrieval request")
    if _SECRET.search(" ".join(values)):
        raise PermissionError("request appears to contain private or credential material")
    if not 1 <= maximum_sources <= 20 or not 1 <= maximum_bytes <= 2_000_000:
        raise ValueError("research resource estimate exceeds the bounded policy")
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    identifier = hashlib.sha256(
        "\x1f".join((question.strip(), purpose.strip(), timestamp)).encode("utf-8")
    ).hexdigest()
    return ResearchRequest(
        identifier,
        question.strip(),
        purpose.strip(),
        privacy_class,
        maximum_sources,
        maximum_bytes,
        stopping_condition.strip(),
        timestamp,
    )


class OfflineResearchQueue:
    """Durable queue that records uncertainty instead of inventing an answer."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS research_queue (id TEXT PRIMARY KEY, question TEXT NOT NULL, status TEXT NOT NULL)"
        )
        self._db.commit()

    def __enter__(self) -> "OfflineResearchQueue":
        return self

    def __exit__(self, *_: object) -> None:
        self._db.close()

    def enqueue(self, request: ResearchRequest, *, internet_available: bool) -> str:
        if internet_available:
            return "ready"
        self._db.execute(
            "INSERT OR IGNORE INTO research_queue VALUES (?, ?, 'waiting_for_network')",
            (request.id, request.question),
        )
        self._db.commit()
        return "waiting_for_network"

    def pending(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            self._db.execute(
                "SELECT id, question FROM research_queue WHERE status = 'waiting_for_network' ORDER BY id"
            )
        )
