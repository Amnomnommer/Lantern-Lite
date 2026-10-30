"""Evidence-driven memory (owner amendment, 2026-10-24): how sure Lantern is of each thing it read.

Belief changes only from evidence:
- how many pages say it;
- from how many different organisations (one origin is one voice);
- whether sources disagree;
- the owner's verdicts.

Ratings and reactions never move a belief: they shape how Lantern talks, not what it holds true.

Beliefs are weighed during sleep and kept beside the sleep store. Every change is logged per night
and undone with that night (`!sleep undo`). Answers use them to say how sure Lantern is ("I'm sure",
"one page says", "my sources disagree"). The fact memory itself is untouched: beliefs are a layer of
judgement about it, and each one explains itself (`!belief IDEA`).
"""

from __future__ import annotations

import json
import math
import sqlite3
from urllib.parse import urlsplit

from lantern import reading_digest
from lantern.explorer import origin
from lantern.memory import MemoryStore

LEVELS = ((0.9, "I'm sure"), (0.75, "I'm fairly sure"), (0.55, "I think"), (0.0, "I'm not sure, but"))


def strength(pages: int, origins: int, disputed: bool, verdict: int) -> float:
    """Belief from evidence only: more pages and more independent origins raise it; disagreement lowers it;
    the owner's verdict for (+1) or against (-1) weighs most."""
    # The owner's verdict settles a disagreement for Lantern: the dispute no longer counts against it.
    score = -0.4 + 0.8 * math.log2(1 + pages) + 0.6 * max(0, origins - 1) - 1.4 * (disputed and not verdict) + 2.5 * verdict
    return round(1 / (1 + math.exp(-score)), 3)


def words(belief: float) -> str:
    return next(phrase for floor, phrase in LEVELS if belief >= floor)


def ensure(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS beliefs (concept TEXT, predicate TEXT, object TEXT, strength REAL, evidence TEXT,
            night TEXT, PRIMARY KEY (concept, predicate, object));
        CREATE TABLE IF NOT EXISTS belief_changes (night TEXT, concept TEXT, predicate TEXT, object TEXT, before REAL,
            after REAL, evidence TEXT);
    """)


def weigh(memory: MemoryStore, connection: sqlite3.Connection, night: str) -> dict[str, int]:
    """Re-weigh every belief from its evidence; returns how many rose, fell or are new (each change logged)."""
    ensure(connection)
    groups: dict[tuple[str, str, str], set[str]] = {}
    for concept, predicate, obj, reference in memory._connection.execute(
            "SELECT e.canonical_name, c.predicate, c.object_text, s.reference FROM claims AS c JOIN entities AS e "
            "ON e.id = c.subject_entity_id LEFT JOIN sources AS s ON s.id = c.source_id WHERE e.kind = ? AND "
            "c.status = 'active' AND c.evidence_type = ?", (reading_digest.CONCEPT_KIND, reading_digest.EVIDENCE)):
        groups.setdefault((str(concept), str(predicate), str(obj).casefold()), set()).add(
            reading_digest._page_of(str(reference or "")))
    verdicts: dict[tuple[str, str, str], int] = {}
    disputed: set[tuple[str, str]] = set()
    for dispute in reading_digest.disputes(memory, limit=10_000):
        disputed.add((str(dispute["concept"]), str(dispute["object"]).casefold()))
        if dispute.get("verdict") is not None:
            for index, side in enumerate(dispute["sides"]):
                for claim in side["claims"]:
                    row = memory._connection.execute("SELECT predicate, object_text FROM claims WHERE id = ?",
                                                      (claim,)).fetchone()
                    if row is not None:
                        key = (str(dispute["concept"]), str(row[0]), str(row[1]).casefold())
                        verdicts[key] = 1 if index == dispute["verdict"] else -1
    old = {(r[0], r[1], r[2]): float(r[3]) for r in connection.execute(
        "SELECT concept, predicate, object, strength FROM beliefs")}
    counts = {"rose": 0, "fell": 0, "new": 0}
    connection.execute("DELETE FROM beliefs")
    for key, pages in groups.items():
        concept, predicate, obj = key
        origins = {origin(urlsplit(p).hostname or p) for p in pages}
        is_disputed = (concept, obj) in disputed or any(
            c == concept and o == obj for c, o in disputed)
        evidence = {"pages": len(pages), "origins": len(origins), "disputed": is_disputed,
                    "verdict": verdicts.get(key, 0)}
        value = strength(len(pages), len(origins), is_disputed, evidence["verdict"])
        connection.execute("INSERT INTO beliefs VALUES (?, ?, ?, ?, ?, ?)",
                           (concept, predicate, obj, value, json.dumps(evidence, sort_keys=True), night))
        before = old.get(key)
        if before is None or abs(before - value) >= 0.01:
            counts["new" if before is None else "rose" if value > before else "fell"] += 1
            connection.execute("INSERT INTO belief_changes VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (night, concept, predicate, obj, before, value, json.dumps(evidence, sort_keys=True)))
    connection.commit()
    return counts


def undo(connection: sqlite3.Connection, night: str) -> int:
    """Put back every belief as it was before that night."""
    ensure(connection)
    rows = connection.execute("SELECT concept, predicate, object, before FROM belief_changes WHERE night = ?",
                              (night,)).fetchall()
    for concept, predicate, obj, before in rows:
        if before is None:
            connection.execute("DELETE FROM beliefs WHERE concept = ? AND predicate = ? AND object = ?",
                               (concept, predicate, obj))
        else:
            connection.execute("UPDATE beliefs SET strength = ? WHERE concept = ? AND predicate = ? AND object = ?",
                               (before, concept, predicate, obj))
    connection.execute("DELETE FROM belief_changes WHERE night = ?", (night,))
    connection.commit()
    return len(rows)


def of(connection: sqlite3.Connection, concept: str) -> list[dict[str, object]]:
    """What Lantern believes about an idea, surest first, each with its evidence."""
    ensure(connection)
    return [{"predicate": p, "object": o, "strength": s, "evidence": json.loads(e)} for p, o, s, e in connection.execute(
        "SELECT predicate, object, strength, evidence FROM beliefs WHERE concept = ? ORDER BY strength DESC, predicate, object",
        (concept,))]


def explain(connection: sqlite3.Connection, concept: str) -> str:
    concept = " ".join(concept.lower().split())
    found = of(connection, concept)
    if not found:
        return f"I haven't weighed anything about {concept} yet (I weigh my beliefs while I sleep)."
    lines = [f"How sure I am about {concept} (from evidence only, never from ratings):"]
    for b in found[:8]:
        e = b["evidence"]
        why = [f"{e['pages']} page{'s' if e['pages'] != 1 else ''}", f"{e['origins']} source{'s' if e['origins'] != 1 else ''}"]
        if e["disputed"]:
            why.append("sources disagree")
        if e["verdict"]:
            why.append("you agreed" if e["verdict"] > 0 else "you disagreed")
        lines.append(f"• {words(b['strength'])}: {concept} {reading_digest._with_article(b['predicate'], b['object'])} "
                     f"({round(b['strength'] * 100)}%; {', '.join(why)})")
    return "\n".join(lines)


def sureness(connection: sqlite3.Connection, concept: str) -> str | None:
    """How sure Lantern is of what it knows about an idea, in words (from its surest belief)."""
    found = of(connection, concept)
    return None if not found else words(float(found[0]["strength"]))
