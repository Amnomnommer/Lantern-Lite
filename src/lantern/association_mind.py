"""The association mind (owner amendment, 2026-10-24, `MULTIPLE_MINDS.md`): what goes with what.

Ideas that turn up in the same sentence are counted together. Strength is positive pointwise
mutual information: how much more often two ideas appear together than chance would give.
It is pure counting, so every link can explain itself ("together in 14 sentences"); there is no
neural network and nothing is generated.

It learns from:
- page text while reading is digested (before the raw text is deleted);
- the owner's messages;
- Lantern talks.

It gives:
- "this reminds me of…";
- which ideas are close;
- what to wonder about next.

The algorithm is shared with the phone (`PhoneAssociations.kt`) and checked with shared vectors
(`association-vectors-v1.json`). Prime keeps its counts in SQLite in the vault; the phone keeps a
pruned file.
"""

from __future__ import annotations

import math
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from lantern.explorer import is_idea
from lantern.learning.concepts import concept_words

MAX_IDEAS_PER_SENTENCE = 12
MAX_PAIRS = 400_000  # do more with less (owner, 2026-10-24): the idea space carries the rest
MIN_TOGETHER = 2  # once is coincidence
_SENTENCES = re.compile(r"[.!?\n]+")


def sentence_ideas(text: str) -> list[list[str]]:
    """The ideas in each sentence, in order (identical to PhoneAssociations.sentenceIdeas)."""
    found = []
    for sentence in _SENTENCES.split(text):
        ideas = [w for w in concept_words(sentence) if is_idea(w)][:MAX_IDEAS_PER_SENTENCE]
        if ideas:
            found.append(ideas)
    return found


def strength(together: int, count_a: int, count_b: int, sentences: int) -> float:
    """Positive pointwise mutual information in bits; 0 below MIN_TOGETHER."""
    if together < MIN_TOGETHER or not count_a or not count_b or not sentences:
        return 0.0
    return max(0.0, math.log2(together * sentences / (count_a * count_b)))


class AssociationStore:
    """Prime's association counts (in the vault, beside its memory)."""

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA cache_size = -16000")  # 16 MB: fewer disk reads while reading
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS ideas (idea TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pairs (a TEXT, b TEXT, count INTEGER NOT NULL, PRIMARY KEY (a, b));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS pairs_b ON pairs(b);
        """)

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def __enter__(self) -> "AssociationStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def sentences(self) -> int:
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'sentences'").fetchone()
        return int(row[0]) if row else 0

    def observe(self, text: str, community: str = "") -> int:
        """Count one text; returns how many sentences carried ideas (the community does not matter here)."""
        rows = sentence_ideas(text)
        if not rows:
            return 0
        execute = self.connection.execute
        for ideas in rows:
            for idea in ideas:
                execute("INSERT INTO ideas VALUES (?, 1) ON CONFLICT(idea) DO UPDATE SET count = count + 1", (idea,))
            for i, a in enumerate(ideas):
                for b in ideas[i + 1:]:
                    first, second = sorted((a, b))
                    execute("INSERT INTO pairs VALUES (?, ?, 1) ON CONFLICT(a, b) DO UPDATE SET count = count + 1",
                            (first, second))
        execute("INSERT INTO meta VALUES ('sentences', ?) ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (len(rows),))
        self.connection.commit()
        return len(rows)

    def count(self, idea: str) -> int:
        row = self.connection.execute("SELECT count FROM ideas WHERE idea = ?", (idea,)).fetchone()
        return int(row[0]) if row else 0

    def neighbours(self, idea: str, limit: int = 5) -> list[dict[str, object]]:
        """The ideas closest to one idea: strongest first, then most often together, then by name."""
        own = self.count(idea)
        total = self.sentences()
        rows = self.connection.execute(
            "SELECT CASE WHEN p.a = ? THEN p.b ELSE p.a END, p.count, i.count FROM pairs AS p "
            "JOIN ideas AS i ON i.idea = CASE WHEN p.a = ? THEN p.b ELSE p.a END "
            "WHERE (p.a = ? OR p.b = ?) AND p.count >= ?", (idea, idea, idea, idea, MIN_TOGETHER)).fetchall()
        scored = [{"idea": other, "together": together, "strength": round(strength(together, own, count, total), 4)}
                  for other, together, count in rows]
        scored = [s for s in scored if s["strength"] > 0]
        scored.sort(key=lambda s: (-s["strength"], -s["together"], s["idea"]))
        return scored[:limit]

    def reminds(self, ideas: Iterable[str], *, exclude: Iterable[str] = (), minimum: float = 1.0) -> list[str]:
        """Up to two ideas the given ones bring to mind, strongly enough to mention."""
        skip = set(exclude) | set(ideas)
        found: list[str] = []
        for idea in ideas:
            for n in self.neighbours(idea, limit=5):
                if n["strength"] >= minimum and n["idea"] not in skip and n["idea"] not in found:
                    found.append(str(n["idea"]))
        return found[:2]

    def size(self) -> dict[str, int]:
        ideas = self.connection.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]
        pairs = self.connection.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        return {"ideas": int(ideas), "pairs": int(pairs), "sentences": self.sentences()}

    def prune(self, max_pairs: int = MAX_PAIRS) -> int:
        """Keep the store bounded: coincidences (pairs seen once) go first, then the weakest pairs; the file
        shrinks too (the idea space now carries what goes with what, so this table can stay small)."""
        total = self.connection.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        if total <= max_pairs:
            return 0
        removed = self.connection.execute("DELETE FROM pairs WHERE count < ?", (MIN_TOGETHER,)).rowcount
        left = self.connection.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        if left > max_pairs:
            removed += self.connection.execute(
                "DELETE FROM pairs WHERE rowid IN (SELECT rowid FROM pairs ORDER BY count, a, b LIMIT ?)",
                (left - max_pairs,)).rowcount
        self.connection.commit()
        self.connection.execute("VACUUM")
        return int(removed)


def explain(store: AssociationStore, idea: str) -> str:
    idea = " ".join(idea.lower().split())
    found = store.neighbours(idea, limit=6)
    if not found:
        seen = store.count(idea)
        return (f"Nothing stands out with {idea} yet." if seen
                else f"I haven't come across {idea} often enough to have associations.")
    return f"{idea} goes with: " + "; ".join(
        f"{n['idea']} (together in {n['together']} sentences, strength {n['strength']:.1f})" for n in found)
