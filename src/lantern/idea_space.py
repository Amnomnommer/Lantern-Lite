"""One shared idea space (owner, 2026-10-24: "a way to do more with less… take the next step").

Every idea Lantern meets gets one small vector, learned from its own reading by random indexing:
- each idea has a fixed *index vector* made from its name (DIMENSIONS numbers, a few of them +1 or -1,
  computed from a SHA-256 of the name and never stored);
- its *meaning vector* adds up the index vectors of the ideas around it, sentence by sentence, so
  ideas that keep the same company end up pointing the same way;
- its *next vector* adds up the index vector of the idea that came next, so the space also knows
  what tends to follow what.

From those two vectors come what used to need large count tables: what an idea is like
(`similar`), what usually follows it (`following`), and how two ideas relate (`relatedness`). A
128-bit signature per meaning vector (the sign of each number) makes finding neighbours a matter of
counting differing bits; the closest are then checked exactly. Memory stays fixed: at most
MAX_IDEAS ideas, the least met go first at the morning sleep.

It is this Lantern's own mind (never shared), learns only from text it took in, never writes text
or facts, and every answer explains itself by naming the ideas involved.
"""

from __future__ import annotations

from array import array
import hashlib
import math
import sqlite3
from pathlib import Path
from typing import Iterable

from lantern.association_mind import sentence_ideas

DIMENSIONS = 128
NONZERO = 8  # entries of an index vector that are +1 or -1
WINDOW = 4  # ideas this far apart in a sentence keep each other's company
MAX_IDEAS = 60_000
MIN_COUNT = 3  # met fewer times than this, an idea has no reliable neighbours yet
LIMIT = 32_000  # numbers saturate here (int16); sleep halves a vector that comes close


def index_vector(idea: str) -> list[tuple[int, int]]:
    """The idea's fixed random signature: (position, +1 or -1) pairs, the same on every Lantern."""
    digest = hashlib.sha256(idea.encode("utf-8")).digest()
    return [((digest[2 * k] << 8 | digest[2 * k + 1]) % DIMENSIONS, 1 if digest[16 + k] & 1 else -1)
            for k in range(NONZERO)]


def signature(vector: Iterable[int]) -> int:
    """One bit per number: set when it is positive."""
    bits = 0
    for position, value in enumerate(vector):
        if value > 0:
            bits |= 1 << position
    return bits


def cosine(a: Iterable[int], b: Iterable[int]) -> float:
    a, b = list(a), list(b)
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _add(vector: array, index: list[tuple[int, int]], weight: int = 1) -> None:
    for position, sign in index:
        vector[position] = max(-LIMIT, min(LIMIT, vector[position] + sign * weight))


class IdeaSpace:
    """The idea space's store (in the vault, beside Prime's memory): one row per idea, two small vectors."""

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA cache_size = -8000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS ideas (idea TEXT PRIMARY KEY, count INTEGER NOT NULL, meaning BLOB NOT NULL,
                next BLOB NOT NULL, signature BLOB NOT NULL);
        """)
        self._signatures: dict[str, tuple[int, int]] | None = None  # idea -> (signature, count), loaded on first use

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    # -- learning ------------------------------------------------------------------------

    def _row(self, idea: str, cache: dict[str, list]) -> list:
        if idea not in cache:
            found = self.connection.execute("SELECT count, meaning, next FROM ideas WHERE idea = ?", (idea,)).fetchone()
            if found is None:
                cache[idea] = [0, array("h", bytes(2 * DIMENSIONS)), array("h", bytes(2 * DIMENSIONS))]
            else:
                meaning, following = array("h"), array("h")
                meaning.frombytes(found[1])
                following.frombytes(found[2])
                cache[idea] = [int(found[0]), meaning, following]
        return cache[idea]

    def observe(self, text: str, community: str = "reading") -> int:
        """Take in some text: each idea's vectors learn the company it keeps and what follows it."""
        cache: dict[str, list] = {}
        met = 0
        for ideas in sentence_ideas(text):
            for position, idea in enumerate(ideas):
                row = self._row(idea, cache)
                row[0] += 1
                met += 1
                for other in ideas[max(0, position - WINDOW):position + WINDOW + 1]:
                    if other != idea:
                        _add(row[1], index_vector(other))
                if position + 1 < len(ideas):
                    _add(row[2], index_vector(ideas[position + 1]))
        for idea, (count, meaning, following) in cache.items():
            mark = signature(meaning).to_bytes(DIMENSIONS // 8, "big")
            self.connection.execute(
                "INSERT INTO ideas VALUES (?, ?, ?, ?, ?) ON CONFLICT(idea) DO UPDATE SET count = excluded.count, "
                "meaning = excluded.meaning, next = excluded.next, signature = excluded.signature",
                (idea, count, meaning.tobytes(), following.tobytes(), mark))
            if self._signatures is not None:
                self._signatures[idea] = (int.from_bytes(mark, "big"), count)
        self.connection.commit()
        return met

    # -- what it gives -------------------------------------------------------------------

    def _all(self) -> dict[str, tuple[int, int]]:
        if self._signatures is None:
            self._signatures = {str(idea): (int.from_bytes(mark, "big"), int(count)) for idea, mark, count in
                                self.connection.execute("SELECT idea, signature, count FROM ideas")}
        return self._signatures

    def vectors(self, idea: str) -> tuple[int, array, array] | None:
        found = self.connection.execute("SELECT count, meaning, next FROM ideas WHERE idea = ?", (idea,)).fetchone()
        if found is None:
            return None
        meaning, following = array("h"), array("h")
        meaning.frombytes(found[1])
        following.frombytes(found[2])
        return int(found[0]), meaning, following

    def count(self, idea: str) -> int:
        found = self.vectors(idea)
        return 0 if found is None else found[0]

    def similar(self, idea: str, limit: int = 5, *, shortlist: int = 60) -> list[dict[str, object]]:
        """Ideas that keep the same company: a fast shortlist by signature, then exact similarity."""
        own = self.vectors(idea)
        if own is None or own[0] < MIN_COUNT:
            return []
        mark = signature(own[1])
        candidates = sorted(((bin(mark ^ sig).count("1"), other) for other, (sig, count) in self._all().items()
                             if other != idea and count >= MIN_COUNT), key=lambda t: (t[0], t[1]))[:shortlist]
        scored = []
        for _distance, other in candidates:
            found = self.vectors(other)
            if found is not None:
                scored.append({"idea": other, "similarity": round(cosine(own[1], found[1]), 4)})
        scored = [s for s in scored if s["similarity"] > 0.2]
        scored.sort(key=lambda s: (-s["similarity"], s["idea"]))
        return scored[:limit]

    def following(self, idea: str, limit: int = 5) -> list[dict[str, object]]:
        """What usually comes next after an idea: the ideas whose index vectors its next vector points to."""
        own = self.vectors(idea)
        if own is None or own[0] < MIN_COUNT:
            return []
        weight = math.sqrt(sum(x * x for x in own[2])) or 1.0
        scored = []
        for other, (_sig, count) in self._all().items():
            if other == idea or count < MIN_COUNT:
                continue
            score = sum(sign * own[2][position] for position, sign in index_vector(other)) / (weight * math.sqrt(NONZERO))
            if score > 0.15:
                scored.append({"idea": other, "score": round(score, 4)})
        scored.sort(key=lambda s: (-s["score"], s["idea"]))
        return scored[:limit]

    def relatedness(self, a: str, b: str) -> float:
        va, vb = self.vectors(a), self.vectors(b)
        return 0.0 if va is None or vb is None else round(cosine(va[1], vb[1]), 4)

    def size(self) -> dict[str, int]:
        return {"ideas": int(self.connection.execute("SELECT COUNT(*) FROM ideas").fetchone()[0])}

    # -- the morning sleep -----------------------------------------------------------------

    def tidy(self) -> int:
        """Keep the space bounded: the least met ideas go first; vectors near the limit are halved."""
        total = self.size()["ideas"]
        removed = 0
        if total > MAX_IDEAS:
            removed = self.connection.execute(
                "DELETE FROM ideas WHERE idea IN (SELECT idea FROM ideas ORDER BY count, idea LIMIT ?)",
                (total - MAX_IDEAS,)).rowcount
        for idea, meaning_bytes, next_bytes in self.connection.execute(
                "SELECT idea, meaning, next FROM ideas").fetchall():
            meaning, following = array("h"), array("h")
            meaning.frombytes(meaning_bytes)
            following.frombytes(next_bytes)
            if max(map(abs, meaning)) > LIMIT * 0.9 or max(map(abs, following)) > LIMIT * 0.9:
                meaning = array("h", (x // 2 for x in meaning))
                following = array("h", (x // 2 for x in following))
                self.connection.execute("UPDATE ideas SET meaning = ?, next = ? WHERE idea = ?",
                                        (meaning.tobytes(), following.tobytes(), idea))
        self.connection.commit()
        self._signatures = None
        return int(removed)


def explain(space: IdeaSpace, idea: str) -> str:
    idea = " ".join(idea.lower().split())
    alike = space.similar(idea)
    after = space.following(idea, limit=4)
    if not alike and not after:
        met = space.count(idea)
        return (f"I've met {idea} {met} time(s); not enough yet to know what it's like." if met
                else f"{idea} isn't in my idea space yet.")
    lines = [f"In my idea space, {idea}:"]
    if alike:
        lines.append("• keeps the same company as " + ", ".join(f"{s['idea']} ({round(float(s['similarity']) * 100)}%)"
                                                             for s in alike))
    if after:
        lines.append("• is usually followed by " + ", ".join(str(s["idea"]) for s in after))
    lines.append(f"(I've met it {space.count(idea)} time(s); this is what my own reading taught me, not a fact.)")
    return "\n".join(lines)
