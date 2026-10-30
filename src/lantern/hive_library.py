"""The hive library: enrolled Lanterns pool what they learned, never who they are.

Owner decisions (2026-10-23): the owner's phone is enrolled as a hive node; what
a node contributes is decided by Lantern (today's reading rules) and every batch
can be undone; the transport is the existing Lantern-Commons.

- Only concept relations travel: (concept, predicate, object, page). Personality,
  conversation, goals, permissions and identity never do.
- A `library` envelope body is `lantern_hive_library_v1` and stays under the
  commons 64 KiB body limit; a large library goes in several envelopes.
- Prime files each envelope from a node as one reading session named
  `hive-NODE-BATCH`, so a batch is undone like any reading session
  (`reading_digest.unlearn_session`) and answers name the original page.
- Lantern decides: a relation is kept only if today's reading rules keep it, its
  predicate is one reading produces, and its page is an https address or one of
  the owner's files. Anything else is declined and counted, never stored.
- What Prime sends a node never includes what that node sent it.
- Received content is untrusted data; it is never followed as an instruction and
  can only add retrieved concept claims.
"""

from __future__ import annotations

import json
import re
from typing import Sequence

from lantern.memory import MemoryStore
from lantern.reading_digest import (
    CONCEPT_KIND, EVIDENCE, SOURCE_KIND, VERBS, Known, Relation, _page_of, keeps, learn,
)

FORMAT = "lantern_hive_library_v1"
MAX_BODY_BYTES = 60_000  # under the commons 64 KiB body limit
MAX_RELATIONS = 1_000
MAX_PER_SYNC = 20_000  # relations Prime sends one node in one sync; the rest follow next time
PREDICATES = frozenset(predicate for _, predicate in VERBS)
_PAGE = re.compile(r"https://[^\s]{1,2000}|file:[^\n\r/\\]{1,255}")
_NODE = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
_BATCH = re.compile(r"[0-9a-f]{16}")
_FIELDS = {"concept", "object", "page", "predicate"}


def _value(known: Known) -> dict[str, str]:
    return {"concept": known.concept, "object": known.object, "page": known.page, "predicate": known.predicate}


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def bodies(known: Sequence[Known]) -> list[dict[str, object]]:
    """Envelope bodies carrying `known`, in order, each under MAX_BODY_BYTES. Shared with the phone."""
    parts: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    size = 0
    for item in known:
        value = _value(item)
        length = _size(value) + 1
        if current and (size + length > MAX_BODY_BYTES - 100 or len(current) >= MAX_RELATIONS):
            parts.append(current)
            current, size = [], 0
        current.append(value)
        size += length
    if current:
        parts.append(current)
    return [{"format": FORMAT, "relations": relations} for relations in parts]


def acceptable(known: Known) -> bool:
    """Lantern's own decision about one received relation (the same rules its reading follows)."""
    return (known.predicate in PREDICATES and bool(_PAGE.fullmatch(known.page))
            and known.concept == known.concept.lower() and keeps(known.concept, known.object, known.page))


def validate(body: object) -> tuple[list[Known], int]:
    """(kept relations, how many Lantern declined); ValueError if the body is not a library body at all."""
    if type(body) is not dict or set(body) != {"format", "relations"} or body["format"] != FORMAT:
        raise ValueError("unsupported hive library body")
    relations = body["relations"]
    if type(relations) is not list or not 1 <= len(relations) <= MAX_RELATIONS:
        raise ValueError(f"hive library body needs 1 to {MAX_RELATIONS} relations")
    kept: list[Known] = []
    declined = 0
    for value in relations:
        if type(value) is not dict or set(value) != _FIELDS or any(type(v) is not str for v in value.values()):
            raise ValueError("hive library relation has unexpected fields")
        if len(value["concept"]) > 120 or len(value["object"]) > 400 or len(value["predicate"]) > 40:
            raise ValueError("hive library relation is too long")
        known = Known(value["concept"], value["predicate"], value["object"], value["page"])
        if acceptable(known):
            kept.append(known)
        else:
            declined += 1
    return kept, declined


def session_name(node: str, batch: str) -> str:
    if not _NODE.fullmatch(node) or not _BATCH.fullmatch(batch):
        raise ValueError("hive batch names need a node identity and a 16-hex batch")
    return f"hive-{node}-{batch}"


class PrimeLibrary:
    """Prime's side of the hive library, over Prime's own memory."""

    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def receive(self, node: str, batch: str, relations: Sequence[Known]) -> int:
        """File one node's batch as a reading session; returns how many relations were new."""
        session = session_name(node, batch)
        sources: dict[str, str] = {}
        return sum(learn(self.memory, session, k.page, Relation(k.concept, k.predicate, k.object), sources)
                   for k in relations if acceptable(k))

    def outgoing(self, node: str, after: int) -> tuple[list[Known], int]:
        """What Prime learned since claim row `after`, minus what `node` sent; and the new cursor."""
        session_name(node, "0" * 16)  # node identity check
        rows = self.memory._connection.execute(
            "SELECT c.rowid, e.canonical_name, c.predicate, c.object_text, s.reference FROM claims AS c "
            "JOIN entities AS e ON e.id = c.subject_entity_id LEFT JOIN sources AS s ON s.id = c.source_id "
            "WHERE e.kind = ? AND c.status = 'active' AND c.evidence_type = ? AND c.rowid > ? "
            "ORDER BY c.rowid LIMIT ?",
            (CONCEPT_KIND, EVIDENCE, after, MAX_PER_SYNC),
        ).fetchall()
        own = f"{SOURCE_KIND}:hive-{node}-"
        known = [Known(str(r[1]), str(r[2]), str(r[3]), _page_of(str(r[4] or ""))) for r in rows
                 if not str(r[4] or "").startswith(own)]
        return [k for k in known if acceptable(k)], (int(rows[-1][0]) if rows else after)


def batches(memory: MemoryStore) -> list[dict[str, object]]:
    """Hive batches Prime holds, newest first, with how many relations each still adds (for undo)."""
    rows = memory._connection.execute(
        "SELECT s.reference, COUNT(c.id) FROM sources AS s LEFT JOIN claims AS c ON c.source_id = s.id "
        "AND c.status = 'active' WHERE s.kind = ? AND s.reference LIKE 'reading:hive-%' "
        "GROUP BY s.id ORDER BY s.rowid DESC", (SOURCE_KIND,),
    ).fetchall()
    found: dict[str, int] = {}
    for reference, count in rows:
        session = str(reference).split(":", 2)[1]
        found[session] = found.get(session, 0) + int(count)
    return [{"session": session, "relations": count} for session, count in found.items()]
