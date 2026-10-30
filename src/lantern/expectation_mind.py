"""The expectation mind, Lantern's fifth mind (owner, 2026-10-24: "I'm not afraid of giving you the keys to try
and design a new type of programmed intelligence we can connect as a 5th brain").

Lantern keeps predicting the next idea in its own experience (reading, the owner's words, files, talks) and
learns from how surprised it was. Four ideas, combined, make it more than a counter:

1. **Prediction by context.** A small variable-order model (the last two ideas, the last one, or none)
   blends what tends to follow, Witten-Bell style, so a new context still predicts something sensible.
2. **Surprise is information.** Each idea costs -log2 P bits under the model *before* it learns from it.
   Summed over a day, that is how many bits Lantern needs to encode its own experience: compression as
   understanding. When bits per idea fall, its world has become more predictable to it. That is a growth
   measure that needs no teacher.
3. **Salience.** Ideas that surprise it far more than usual are remembered as surprises. Sleep keeps
   them (they are never trimmed as quiet), and `!surprises` shows them.
4. **Curiosity from learning progress.** For each idea, surprise early on is compared with surprise
   lately. Ideas whose surprise is falling fastest are where learning is happening; ideas still
   surprising after many meetings are where understanding is missing. Both become things to read about
   next (`wonders.json`), so curiosity grows from Lantern's own learning, not from a list.

Everything is counts, inspectable and explainable ("after lighthouse, keeper followed 12 times"). It never
generates text. This Lantern's own mind, never shared.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from lantern.association_mind import sentence_ideas

ORDER = 2
BOUNDARY = "|"
SURPRISE_SIGMA = 2.0
MIN_MEETINGS = 6
MAX_CONTEXT_ROWS = 800_000  # do more with less (owner, 2026-10-24)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class ExpectationStore:
    """Prime's expectations (in the vault, beside its memory)."""

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA cache_size = -16000")  # 16 MB: fewer disk reads while reading
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS follows (context TEXT, next TEXT, count INTEGER NOT NULL, PRIMARY KEY (context, next));
            CREATE TABLE IF NOT EXISTS contexts (context TEXT PRIMARY KEY, total INTEGER NOT NULL, kinds INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS days (day TEXT, source TEXT, bits REAL, ideas INTEGER, PRIMARY KEY (day, source));
            CREATE TABLE IF NOT EXISTS ideas (idea TEXT PRIMARY KEY, meetings INTEGER, early_bits REAL, early_n INTEGER,
                recent_bits REAL);
            CREATE TABLE IF NOT EXISTS surprises (idea TEXT, after TEXT, bits REAL, source TEXT, at TEXT);
        """)
        self._stats: tuple[float, float, int] | None = None  # running mean, variance, n of surprise

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    # -- the model ---------------------------------------------------------------------

    def _context(self, key: str) -> tuple[int, int]:
        row = self.connection.execute("SELECT total, kinds FROM contexts WHERE context = ?", (key,)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _count(self, key: str, idea: str) -> int:
        row = self.connection.execute("SELECT count FROM follows WHERE context = ? AND next = ?", (key, idea)).fetchone()
        return int(row[0]) if row else 0

    def probability(self, history: list[str], idea: str) -> float:
        """P(idea | the last ideas), blending orders 0..ORDER (Witten-Bell); never zero."""
        vocabulary = self._context("")[1]
        p = 1.0 / (vocabulary + 1)  # a new idea is always possible
        for order in range(0, ORDER + 1):
            if order > len(history):
                break
            key = "\t".join(history[len(history) - order:]) if order else ""
            total, kinds = self._context(key)
            if total == 0:
                continue
            p = (self._count(key, idea) + kinds * p) / (total + kinds)
        return p

    def _learn(self, history: list[str], idea: str) -> None:
        for order in range(0, ORDER + 1):
            if order > len(history):
                break
            key = "\t".join(history[len(history) - order:]) if order else ""
            new = self._count(key, idea) == 0
            self.connection.execute(
                "INSERT INTO follows VALUES (?, ?, 1) ON CONFLICT(context, next) DO UPDATE SET count = count + 1", (key, idea))
            self.connection.execute(
                "INSERT INTO contexts VALUES (?, 1, ?) ON CONFLICT(context) DO UPDATE SET total = total + 1, "
                "kinds = kinds + excluded.kinds", (key, 1 if new else 0))

    # -- experience --------------------------------------------------------------------

    def observe(self, text: str, community: str = "reading") -> float:
        """Predict, measure surprise, then learn, idea by idea; returns the bits this text cost."""
        stream = [idea for sentence in sentence_ideas(text) for idea in sentence]
        if not stream:
            return 0.0
        history = [BOUNDARY]
        cost = 0.0
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for idea in stream:
            bits = -math.log2(self.probability(history, idea))
            cost += bits
            self._note(idea, bits, history[-1], community, now)
            self._learn(history, idea)
            history = (history + [idea])[-ORDER:]
        source = community or "reading"
        self.connection.execute(
            "INSERT INTO days VALUES (?, ?, ?, ?) ON CONFLICT(day, source) DO UPDATE SET bits = bits + excluded.bits, "
            "ideas = ideas + excluded.ideas", (_today(), source, cost, len(stream)))
        self.connection.commit()
        return cost

    def _note(self, idea: str, bits: float, after: str, source: str, at: str) -> None:
        row = self.connection.execute("SELECT meetings, early_bits, early_n, recent_bits FROM ideas WHERE idea = ?",
                                      (idea,)).fetchone()
        if row is None:
            self.connection.execute("INSERT INTO ideas VALUES (?, 1, ?, 1, ?)", (idea, bits, bits))
        else:
            meetings, early, early_n, recent = row
            if early_n < MIN_MEETINGS:  # the first meetings set the baseline
                early, early_n = early + bits, early_n + 1
            recent = 0.8 * recent + 0.2 * bits  # lately: a moving average
            self.connection.execute("UPDATE ideas SET meetings = ?, early_bits = ?, early_n = ?, recent_bits = ? "
                                    "WHERE idea = ?", (meetings + 1, early, early_n, recent, idea))
        mean, variance, n = self._stats or self._load_stats()
        n += 1
        delta = bits - mean
        mean += delta / n
        variance += delta * (bits - mean)
        self._stats = (mean, variance, n)
        spread = math.sqrt(variance / n) if n > 1 else 0.0
        if n > 50 and bits > mean + SURPRISE_SIGMA * spread and row is not None:
            # A familiar idea in a place it was not expected: that is a surprise worth keeping.
            self.connection.execute("INSERT INTO surprises VALUES (?, ?, ?, ?, ?)", (idea, after, round(bits, 2), source, at))

    def _load_stats(self) -> tuple[float, float, int]:
        row = self.connection.execute("SELECT SUM(bits), SUM(ideas) FROM days").fetchone()
        n = int(row[1] or 0)
        mean = (row[0] or 0.0) / n if n else 0.0
        return (mean, (mean * 0.5) ** 2 * n, n)  # a rough prior spread until it has seen enough

    # -- what it gives -------------------------------------------------------------------

    def growth(self, days: int = 14) -> list[dict[str, object]]:
        """Bits per idea, day by day: falling means Lantern's world is becoming more predictable to it."""
        rows = self.connection.execute(
            "SELECT day, SUM(bits), SUM(ideas) FROM days GROUP BY day ORDER BY day DESC LIMIT ?", (days,)).fetchall()
        return [{"day": d, "bits_per_idea": round(b / n, 2), "ideas": int(n)} for d, b, n in reversed(rows) if n]

    def expect(self, idea: str, limit: int = 5) -> list[tuple[str, int]]:
        """What usually follows an idea in Lantern's experience."""
        return [(str(r[0]), int(r[1])) for r in self.connection.execute(
            "SELECT next, count FROM follows WHERE context = ? AND next != ? ORDER BY count DESC, next LIMIT ?",
            (idea, BOUNDARY, limit))]

    def surprises(self, limit: int = 8) -> list[dict[str, object]]:
        return [{"idea": r[0], "after": r[1], "bits": r[2], "source": r[3], "at": r[4]} for r in self.connection.execute(
            "SELECT idea, after, MAX(bits), source, at FROM surprises GROUP BY idea, after ORDER BY MAX(bits) DESC LIMIT ?",
            (limit,))]

    def salient(self) -> set[str]:
        """Ideas that surprised Lantern: sleep keeps them."""
        return {str(r[0]) for r in self.connection.execute("SELECT DISTINCT idea FROM surprises")}

    def curious(self, limit: int = 3) -> list[dict[str, object]]:
        """Where learning is happening (surprise falling fastest) or missing (still surprising after many meetings)."""
        rows = self.connection.execute(
            "SELECT idea, meetings, early_bits / early_n, recent_bits FROM ideas WHERE meetings >= ? AND early_n >= ?",
            (MIN_MEETINGS * 2, MIN_MEETINGS)).fetchall()
        progress = sorted(((early - recent, idea, recent) for idea, _m, early, recent in rows), reverse=True)
        stuck = sorted(((recent, idea) for idea, _m, _e, recent in rows), reverse=True)
        found: list[dict[str, object]] = []
        for gain, idea, recent in progress:
            if gain > 0.5 and len(found) < limit - 1:
                found.append({"idea": idea, "why": f"I'm learning it fast ({gain:.1f} bits less surprising than at first)"})
        for recent, idea in stuck:
            if len(found) >= limit:
                break
            if idea not in {f["idea"] for f in found}:
                found.append({"idea": idea, "why": f"it still surprises me ({recent:.1f} bits each time)"})
        return found[:limit]

    def wonder(self, wonders: Path) -> list[str]:
        """Curiosity from learning progress joins what Lantern wants to read about next."""
        wanted = [str(c["idea"]) for c in self.curious()]
        if not wanted:
            return []
        try:
            current = [w for w in json.loads(wonders.read_text(encoding="utf-8")) if isinstance(w, str)]
        except (OSError, ValueError, TypeError):
            current = []
        added = [w for w in wanted if w not in current]
        if added:
            wonders.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            wonders.write_text(json.dumps((current + added)[-20:], indent=1) + "\n", encoding="utf-8")
        return added

    def prune(self) -> int:
        total = self.connection.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
        if total <= MAX_CONTEXT_ROWS:
            return 0
        removed = self.connection.execute("DELETE FROM follows WHERE count = 1 AND context != ''").rowcount
        left = self.connection.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
        if left > MAX_CONTEXT_ROWS:  # still too many: the least seen go, longest contexts first
            removed += self.connection.execute(
                "DELETE FROM follows WHERE rowid IN (SELECT rowid FROM follows WHERE context != '' "
                "ORDER BY count, length(context) DESC LIMIT ?)", (left - MAX_CONTEXT_ROWS,)).rowcount
        self.connection.execute("DELETE FROM contexts WHERE context NOT IN (SELECT DISTINCT context FROM follows)")
        self.connection.commit()
        self.connection.execute("VACUUM")
        return int(removed)


def explain_growth(store: ExpectationStore) -> str:
    days = store.growth()
    if not days:
        return "I haven't measured my surprise yet."
    lines = ["How predictable my world is becoming (bits per idea; lower means I understand more):"]
    lines += [f"• {d['day']}: {d['bits_per_idea']} bits per idea over {d['ideas']} ideas" for d in days[-7:]]
    if len(days) >= 2:
        change = days[-1]["bits_per_idea"] - days[0]["bits_per_idea"]
        lines.append("My experience is getting easier to predict." if change < 0 else
                     "My experience is getting harder to predict: I'm meeting a lot that is new.")
    curious = store.curious()
    if curious:
        lines.append("Where I'm curious: " + "; ".join(f"{c['idea']} ({c['why']})" for c in curious))
    return "\n".join(lines)


def explain_surprises(store: ExpectationStore) -> str:
    found = store.surprises()
    if not found:
        return "Nothing has surprised me much yet."
    return "What surprised me (an idea I knew, where I didn't expect it):\n" + "\n".join(
        f"• {s['idea']} right after {s['after'] if s['after'] != BOUNDARY else 'the start'} ({s['bits']} bits, in {s['source']})"
        for s in found)


def explain_expect(store: ExpectationStore, idea: str) -> str:
    idea = " ".join(idea.lower().split())
    follows = store.expect(idea)
    if not follows:
        return f"I don't have expectations about what follows {idea} yet."
    return f"After {idea} I expect: " + ", ".join(f"{n} ({c} time{'s' if c != 1 else ''})" for n, c in follows)
