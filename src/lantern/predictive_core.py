"""The predictive core (owner, 2026-10-24: "we still need to take the next step"): a small network that
learns by guessing what comes next.

It lives on top of the idea space (`lantern.idea_space`). As Lantern reads, the core sees the last few
ideas (their meaning vectors, the most recent weighted most) and guesses the next idea's index vector.
Then it sees what really came next and learns from the difference. That one habit gives:
- **expectation with context:** what Lantern expects next given everything just said, not only the
  last word (`!predict IDEA [IDEA…]`);
- **surprise:** how far its guess was from what came;
- **growth you can see:** its average error per day (`!core`); falling means its world is becoming
  more predictable to it, which is understanding.

Rules (owner amendment, 2026-10-24, `AGENTS.md`): it grows inside this Lantern from its own reading
and the owner's words, runs locally, keeps one version per day that the owner can restore
(`!core restore N`) or switch off (`!core off`), and explains its guesses by naming ideas. It never
writes text or facts and never touches memory, goals, permissions or identity. Each Lantern's own.

Cost is kept small for the Workstation and the phone alike: 128 inputs, 32 hidden units, 128
outputs, and at most SAMPLE learning steps per piece of text.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random

from lantern import idea_space
from lantern.association_mind import sentence_ideas

DIM = idea_space.DIMENSIONS
HIDDEN = 32
RATE = 0.05
SAMPLE = 24  # learning steps per piece of text, at most
CONTEXT = 3  # ideas of context
DECAY = 0.5  # older context counts this much less per step
MIN_STEPS = 500  # before this, its guesses are not offered
GUESS_MIN_COUNT = 10
MAX_VERSIONS = 30


def _unit(vector) -> list[float]:
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm else values


def target(idea: str) -> list[float]:
    """The next idea's index vector as a unit vector: what the core tries to guess."""
    out = [0.0] * DIM
    for position, sign in idea_space.index_vector(idea):
        out[position] += sign
    return _unit(out)


class PredictiveCore:
    def __init__(self, path: Path | None, space: idea_space.IdeaSpace, *, seed: str = "Prime") -> None:
        self.path, self.space = (None if path is None else Path(path)), space
        state = None
        if self.path is not None and self.path.is_file():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        if state is None:
            rng = random.Random(f"core:{seed}")
            scale1, scale2 = 1 / math.sqrt(DIM), 1 / math.sqrt(HIDDEN)
            state = {"format": "lantern_predictive_core_v1", "enabled": True, "steps": 0,
                     "w1": [[rng.uniform(-scale1, scale1) for _ in range(DIM)] for _ in range(HIDDEN)], "b1": [0.0] * HIDDEN,
                     "w2": [[rng.uniform(-scale2, scale2) for _ in range(HIDDEN)] for _ in range(DIM)], "b2": [0.0] * DIM,
                     "days": {}, "versions": {}}
        self.state = state
        self._dirty = 0

    # -- the network ---------------------------------------------------------------------

    def _forward(self, x: list[float]) -> tuple[list[float], list[float]]:
        hidden = [math.tanh(sum(w * xi for w, xi in zip(row, x)) + b) for row, b in zip(self.state["w1"], self.state["b1"])]
        out = [sum(w * h for w, h in zip(row, hidden)) + b for row, b in zip(self.state["w2"], self.state["b2"])]
        return hidden, out

    def _context(self, ideas: list[str]) -> list[float] | None:
        """The last few ideas' meaning vectors, the most recent weighted most."""
        total = [0.0] * DIM
        weight, found = 1.0, False
        for idea in reversed(ideas[-CONTEXT:]):
            vectors = self.space.vectors(idea)
            if vectors is not None and vectors[0] >= idea_space.MIN_COUNT:
                for i, v in enumerate(_unit(vectors[1])):
                    total[i] += weight * v
                found = True
            weight *= DECAY
        return _unit(total) if found else None

    def _learn(self, x: list[float], goal: list[float]) -> float:
        """One step: guess, compare, adjust. Returns the error (1 - cosine of guess and truth)."""
        hidden, out = self._forward(x)
        error = 1.0 - sum(o * g for o, g in zip(_unit(out), goal))
        grad_out = [(o - g) * RATE for o, g in zip(out, goal)]
        w2, w1 = self.state["w2"], self.state["w1"]
        grad_hidden = [sum(grad_out[k] * w2[k][j] for k in range(DIM)) * (1 - hidden[j] * hidden[j]) for j in range(HIDDEN)]
        for k in range(DIM):
            row, g = w2[k], grad_out[k]
            if g:
                for j in range(HIDDEN):
                    row[j] -= g * hidden[j]
                self.state["b2"][k] -= g
        for j in range(HIDDEN):
            row, g = w1[j], grad_hidden[j]
            for i in range(DIM):
                if x[i]:
                    row[i] -= g * x[i]
            self.state["b1"][j] -= g
        return error

    # -- experience ------------------------------------------------------------------------

    def enabled(self) -> bool:
        return bool(self.state.get("enabled", True))

    def observe(self, text: str, community: str = "reading") -> float | None:
        """Learn from some text: guess each sampled next idea from what came before. Returns the mean error."""
        if not self.enabled():
            return None
        moments = []
        for ideas in sentence_ideas(text):
            for position in range(1, len(ideas)):
                moments.append((ideas[:position], ideas[position]))
        if not moments:
            return None
        step = max(1, len(moments) // SAMPLE)
        errors = []
        for before, nxt in moments[::step][:SAMPLE]:
            x = self._context(before)
            if x is None:
                continue
            errors.append(self._learn(x, target(nxt)))
        if not errors:
            return None
        self.state["steps"] = int(self.state.get("steps", 0)) + len(errors)
        day = datetime.now(timezone.utc).date().isoformat()
        days = self.state.setdefault("days", {})
        total, count = days.get(day, [0.0, 0])
        days[day] = [round(total + sum(errors), 6), count + len(errors)]
        for old in sorted(days)[:-60]:
            days.pop(old)
        self._dirty += len(errors)
        if self._dirty >= 200:
            self.save()
        return sum(errors) / len(errors)

    def guess(self, ideas: list[str], limit: int = 5) -> list[dict[str, object]]:
        """What it expects next after these ideas: the known ideas whose index vectors its guess points to."""
        if not self.enabled() or int(self.state.get("steps", 0)) < MIN_STEPS:
            return []
        x = self._context(ideas)
        if x is None:
            return []
        _, out = self._forward(x)
        _, usual = self._forward([0.0] * DIM)  # its guess with no context: what is common anyway
        out = [o - u for o, u in zip(out, usual)]  # what this context adds over the usual
        out = _unit(out)
        scored = []
        for other, (_sig, count) in self.space._all().items():
            if other in ideas or count < GUESS_MIN_COUNT:
                continue  # a guess is among ideas met often enough to be known, not one-off words
            score = sum(sign * out[position] for position, sign in idea_space.index_vector(other)) / math.sqrt(idea_space.NONZERO)
            if score > 0.1:
                scored.append({"idea": other, "score": round(score, 4)})
        scored.sort(key=lambda s: (-s["score"], s["idea"]))
        return scored[:limit]

    def growth(self) -> list[tuple[str, float, int]]:
        return [(day, round(total / count, 3), count) for day, (total, count) in sorted(self.state.get("days", {}).items()) if count]

    # -- the owner's controls --------------------------------------------------------------

    def save(self) -> None:
        self._dirty = 0
        if self.path is None:
            return
        day = datetime.now(timezone.utc).date().isoformat()
        versions = self.state.setdefault("versions", {})
        versions[day] = {k: self.state[k] for k in ("w1", "b1", "w2", "b2", "steps")}
        for old in sorted(versions)[:-MAX_VERSIONS]:
            versions.pop(old)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state), encoding="utf-8")
        temporary.replace(self.path)

    def set_enabled(self, on: bool) -> None:
        self.state["enabled"] = on
        self.save()

    def versions(self) -> list[str]:
        return sorted(self.state.get("versions", {}))

    def restore(self, number: int) -> str:
        days = self.versions()
        if not 1 <= number <= len(days):
            raise ValueError("no such version")
        self.state.update(json.loads(json.dumps(self.state["versions"][days[number - 1]])))
        self.save()
        return days[number - 1]

    def explain(self) -> str:
        days = self.growth()
        lines = [f"My predictive core ({'on' if self.enabled() else 'off'}; {self.state.get('steps', 0)} guesses learned from):"]
        if days:
            lines.append("How wrong my guesses about what comes next are, day by day (0 is perfect, 1 is no idea):")
            lines += [f"• {day}: {error} over {count} guesses" for day, error, count in days[-7:]]
            if len(days) >= 2:
                trend = days[-1][1] - days[0][1]
                lines.append("I'm getting better at expecting what comes next." if trend < -0.01 else
                             "Not much change yet; I need more reading." if abs(trend) <= 0.01 else
                             "I'm meeting newer things than before, so I guess worse for now.")
        else:
            lines.append("I haven't learned from any reading yet.")
        if self.versions():
            lines.append(f"Versions: {', '.join(f'{i}. {d}' for i, d in enumerate(self.versions(), 1))}. "
                         "!core restore N goes back; !core off stops it.")
        return "\n".join(lines)


def explain_guess(core: PredictiveCore, words: str) -> str:
    ideas = [w for line in sentence_ideas(words) for w in line] or words.lower().split()
    if not ideas:
        return "Use !predict IDEA (or a few ideas)."
    found = core.guess(ideas)
    if not found:
        return (f"I don't have a guess after {' '.join(ideas)} yet."
                + (" I need more reading first." if int(core.state.get("steps", 0)) < MIN_STEPS else ""))
    return (f"After {', '.join(ideas)}, I'd expect: " + ", ".join(str(f["idea"]) for f in found)
            + ". (A guess my predictive core learned from my reading, not a fact.)")
